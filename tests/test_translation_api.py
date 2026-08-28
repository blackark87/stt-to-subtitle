import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import Mock, patch

from fastapi.testclient import TestClient
import requests

from stt_to_subtitle.translation_api import (
    DEFAULT_DRAFT_MODEL,
    TranslationRouterSettings,
    create_translation_app,
)
from stt_to_subtitle.translation_store import TranslationEndpointStore


REVIEW_MODEL = "gemma-4-26b-a4b-it-ultra-uncensored-heretic"


def model_response(*model_ids: str) -> Mock:
    response = Mock(status_code=200)
    response.json.return_value = {
        "object": "list",
        "data": [{"id": model_id} for model_id in model_ids],
    }
    return response


def completion_response(text: str) -> Mock:
    payload = {
        "choices": [{"message": {"content": text}}],
    }
    response = Mock(status_code=200)
    response.content = json.dumps(payload).encode("utf-8")
    return response


class TranslationEndpointStoreTests(unittest.TestCase):
    def test_builtin_review_is_disabled_independently(self) -> None:
        with TemporaryDirectory() as directory:
            store = TranslationEndpointStore(Path(directory) / "routes.sqlite3")
            store.sync_builtin(
                name="내장 서버",
                base_url="http://builtin.test/v1",
                token="",
                capacity=1,
                draft_model=DEFAULT_DRAFT_MODEL,
                review_model=REVIEW_MODEL,
                review_enabled=False,
                batch_preferred=False,
            )

            endpoint = store.list()[0]

            self.assertTrue(endpoint.builtin)
            self.assertEqual(endpoint.draft_model, DEFAULT_DRAFT_MODEL)
            self.assertEqual(endpoint.review_model, REVIEW_MODEL)
            self.assertFalse(endpoint.review_enabled)

    def test_selecting_batch_server_clears_previous_selection(self) -> None:
        with TemporaryDirectory() as directory:
            store = TranslationEndpointStore(Path(directory) / "routes.sqlite3")
            first = store.create(
                name="first",
                base_url="http://first.test/v1",
                token="",
                enabled=True,
                capacity=1,
            )
            second = store.create(
                name="second",
                base_url="http://second.test/v1",
                token="",
                enabled=True,
                capacity=1,
            )
            for endpoint in (first, second):
                store.set_models(
                    endpoint.id,
                    draft_model=DEFAULT_DRAFT_MODEL,
                    review_model="",
                    review_enabled=False,
                    batch_preferred=True,
                )

            selected = {
                endpoint.id: endpoint.batch_preferred
                for endpoint in store.list()
            }

            self.assertFalse(selected[first.id])
            self.assertTrue(selected[second.id])

    def test_removes_builtin_when_deployment_configuration_is_removed(self) -> None:
        with TemporaryDirectory() as directory:
            store = TranslationEndpointStore(Path(directory) / "routes.sqlite3")
            store.sync_builtin(
                name="내장 서버",
                base_url="http://builtin.test/v1",
                token="",
                capacity=1,
                draft_model=DEFAULT_DRAFT_MODEL,
                review_model="",
                review_enabled=False,
                batch_preferred=False,
            )

            store.remove_builtin()

            self.assertEqual(store.list(), [])


class TranslationRouterAPITests(unittest.TestCase):
    def settings(self, root: Path, **changes: object) -> TranslationRouterSettings:
        values = {
            "state_dir": root,
            "api_token": "router-secret",
            "builtin_name": "기본 번역 서버",
            "builtin_base_url": "http://builtin.test/v1",
            "builtin_token": "builtin-secret",
            "builtin_capacity": 1,
            "builtin_draft_model": DEFAULT_DRAFT_MODEL,
            "builtin_review_model": "",
            "builtin_review_enabled": False,
            "builtin_batch_preferred": False,
        }
        values.update(changes)
        return TranslationRouterSettings(**values)

    def test_endpoint_registry_discovers_models_without_exposing_tokens(self) -> None:
        with TemporaryDirectory() as directory, patch(
            "stt_to_subtitle.translation_api.requests.get",
            return_value=model_response(DEFAULT_DRAFT_MODEL, REVIEW_MODEL),
        ):
            app = create_translation_app(self.settings(Path(directory)))
            with TestClient(app) as client:
                unauthorized = client.get("/v1/router/endpoints")
                probed = client.post(
                    "/v1/router/endpoints/builtin/probe",
                    headers={"Authorization": "Bearer router-secret"},
                )

            self.assertEqual(unauthorized.status_code, 401)
            self.assertEqual(probed.status_code, 200)
            payload = probed.json()
            self.assertEqual(payload["models"], [DEFAULT_DRAFT_MODEL, REVIEW_MODEL])
            self.assertTrue(payload["token_configured"])
            self.assertNotIn("token", payload)

    def test_routes_live_draft_and_batch_to_registered_server_policy(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            store = TranslationEndpointStore(root / "translation-endpoints.sqlite3")
            external = store.create(
                name="추가 번역 서버",
                base_url="http://external.test/v1",
                token="external-secret",
                enabled=True,
                capacity=2,
            )
            store.set_models(
                external.id,
                draft_model=DEFAULT_DRAFT_MODEL,
                review_model=REVIEW_MODEL,
                review_enabled=True,
                batch_preferred=True,
            )
            app = create_translation_app(self.settings(root), store=store)
            calls: list[tuple[str, str]] = []

            def complete(url: str, **kwargs: object) -> Mock:
                payload = kwargs["json"]
                assert isinstance(payload, dict)
                calls.append((url, str(payload["model"])))
                return completion_response("ok")

            headers = {"Authorization": "Bearer router-secret"}
            with patch(
                "stt_to_subtitle.translation_api.requests.post",
                side_effect=complete,
            ), TestClient(app) as client:
                live = client.post(
                    "/v1/chat/completions",
                    headers={**headers, "X-Translation-Pass": "draft"},
                    json={"model": "translation-router", "messages": []},
                )
                batch = client.post(
                    "/v1/chat/completions",
                    headers={
                        **headers,
                        "X-Translation-Pass": "draft",
                        "X-Translation-Mode": "batch",
                    },
                    json={"model": "translation-router", "messages": []},
                )
                review = client.post(
                    "/v1/chat/completions",
                    headers={**headers, "X-Translation-Pass": "review"},
                    json={"model": "translation-router", "messages": []},
                )

            self.assertEqual(live.status_code, 200)
            self.assertEqual(batch.status_code, 200)
            self.assertEqual(review.status_code, 200)
            self.assertEqual(calls[0], ("http://builtin.test/v1/chat/completions", DEFAULT_DRAFT_MODEL))
            self.assertEqual(calls[1], ("http://external.test/v1/chat/completions", DEFAULT_DRAFT_MODEL))
            self.assertEqual(calls[2], ("http://external.test/v1/chat/completions", REVIEW_MODEL))

    def test_disabled_builtin_review_never_calls_upstream(self) -> None:
        with TemporaryDirectory() as directory, patch(
            "stt_to_subtitle.translation_api.requests.post"
        ) as post:
            app = create_translation_app(self.settings(Path(directory)))
            with TestClient(app) as client:
                response = client.post(
                    "/v1/chat/completions",
                    headers={
                        "Authorization": "Bearer router-secret",
                        "X-Translation-Pass": "review",
                    },
                    json={"model": "translation-router", "messages": []},
                )

            self.assertEqual(response.status_code, 503)
            post.assert_not_called()

    def test_batch_draft_requires_an_explicit_preferred_server(self) -> None:
        with TemporaryDirectory() as directory, patch(
            "stt_to_subtitle.translation_api.requests.post"
        ) as post:
            app = create_translation_app(self.settings(Path(directory)))
            with TestClient(app) as client:
                response = client.post(
                    "/v1/chat/completions",
                    headers={
                        "Authorization": "Bearer router-secret",
                        "X-Translation-Mode": "batch",
                    },
                    json={"model": "translation-router", "messages": []},
                )

            self.assertEqual(response.status_code, 503)
            post.assert_not_called()

    def test_live_draft_fails_over_to_another_registered_server(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            store = TranslationEndpointStore(root / "translation-endpoints.sqlite3")
            fallback = store.create(
                name="fallback",
                base_url="http://fallback.test/v1",
                token="",
                enabled=True,
                capacity=1,
            )
            store.set_models(
                fallback.id,
                draft_model=DEFAULT_DRAFT_MODEL,
                review_model="",
                review_enabled=False,
                batch_preferred=False,
            )
            app = create_translation_app(self.settings(root), store=store)
            with patch(
                "stt_to_subtitle.translation_api.requests.post",
                side_effect=[
                    requests.ConnectionError("offline"),
                    completion_response("ok"),
                ],
            ) as post, TestClient(app) as client:
                response = client.post(
                    "/v1/chat/completions",
                    headers={"Authorization": "Bearer router-secret"},
                    json={"model": "translation-router", "messages": []},
                )

            self.assertEqual(response.status_code, 200)
            self.assertEqual(post.call_count, 2)
            self.assertEqual(
                post.call_args_list[1].args[0],
                "http://fallback.test/v1/chat/completions",
            )


if __name__ == "__main__":
    unittest.main()
