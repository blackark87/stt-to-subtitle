import json
from pathlib import Path
import sqlite3
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import Mock, patch

from fastapi.testclient import TestClient
import requests

from stt_to_subtitle.translation_api import (
    DEFAULT_DRAFT_MODEL,
    DEFAULT_REVIEW_MODEL,
    TranslationRouterSettings,
    create_translation_app,
)
from stt_to_subtitle.translation_store import (
    TranslationServerGroupStore,
    migrate_legacy_translation_endpoints,
)


def model_response(*model_ids: str) -> Mock:
    response = Mock(status_code=200)
    response.json.return_value = {
        "object": "list",
        "data": [{"id": model_id} for model_id in model_ids],
    }
    return response


def completion_response(text: str) -> Mock:
    payload = {"choices": [{"message": {"content": text}}]}
    response = Mock(status_code=200)
    response.content = json.dumps(payload).encode("utf-8")
    return response


class TranslationServerGroupStoreTests(unittest.TestCase):
    def test_groups_own_models_and_servers_independently(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            draft = TranslationServerGroupStore(root / "draft.sqlite3")
            review = TranslationServerGroupStore(root / "review.sqlite3")
            draft.ensure_model(DEFAULT_DRAFT_MODEL)
            review.ensure_model(DEFAULT_REVIEW_MODEL)
            draft.create(
                name="draft-only",
                base_url="http://draft.test/v1",
                token="",
                enabled=True,
                capacity=1,
            )

            self.assertEqual(draft.model(), DEFAULT_DRAFT_MODEL)
            self.assertEqual(review.model(), DEFAULT_REVIEW_MODEL)
            self.assertEqual([item.name for item in draft.list()], ["draft-only"])
            self.assertEqual(review.list(), [])

    def test_batch_selection_is_unique_inside_each_group(self) -> None:
        with TemporaryDirectory() as directory:
            store = TranslationServerGroupStore(Path(directory) / "draft.sqlite3")
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
            store.set_routing(first.id, enabled=True, batch_preferred=True)
            store.set_routing(second.id, enabled=True, batch_preferred=True)

            selected = {item.id: item.batch_preferred for item in store.list()}

            self.assertFalse(selected[first.id])
            self.assertTrue(selected[second.id])

    def test_migrates_shared_endpoint_into_independent_groups_once(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            legacy = root / "translation-endpoints.sqlite3"
            with sqlite3.connect(legacy) as connection:
                connection.execute(
                    """
                    CREATE TABLE translation_endpoints (
                        id TEXT, name TEXT, base_url TEXT, token TEXT,
                        enabled INTEGER, capacity INTEGER, builtin INTEGER,
                        draft_model TEXT, review_model TEXT,
                        review_enabled INTEGER, batch_preferred INTEGER,
                        models_json TEXT, checked_at REAL, created_at REAL
                    )
                    """
                )
                connection.execute(
                    "INSERT INTO translation_endpoints VALUES "
                    "(?, ?, ?, ?, 1, 3, 0, ?, ?, 1, 1, ?, NULL, 1)",
                    (
                        "server-1",
                        "server 01",
                        "http://server.test/v1",
                        "",
                        DEFAULT_DRAFT_MODEL,
                        DEFAULT_REVIEW_MODEL,
                        json.dumps([DEFAULT_DRAFT_MODEL, DEFAULT_REVIEW_MODEL]),
                    ),
                )
            draft = TranslationServerGroupStore(root / "draft.sqlite3")
            review = TranslationServerGroupStore(root / "review.sqlite3")

            migrate_legacy_translation_endpoints(
                legacy,
                draft_store=draft,
                review_store=review,
            )

            self.assertEqual([item.id for item in draft.list()], ["server-1"])
            self.assertEqual([item.id for item in review.list()], ["server-1"])
            self.assertEqual(draft.model(), DEFAULT_DRAFT_MODEL)
            self.assertEqual(review.model(), DEFAULT_REVIEW_MODEL)


class TranslationRouterAPITests(unittest.TestCase):
    def settings(self, root: Path, **changes: object) -> TranslationRouterSettings:
        values = {
            "state_dir": root,
            "api_token": "router-secret",
            "builtin_name": "기본 Runtime",
            "builtin_base_url": "http://builtin.test/v1",
            "builtin_token": "builtin-secret",
            "builtin_capacity": 1,
            "builtin_draft_enabled": True,
            "builtin_review_enabled": False,
            "builtin_draft_model": DEFAULT_DRAFT_MODEL,
            "builtin_review_model": DEFAULT_REVIEW_MODEL,
            "builtin_draft_batch_preferred": False,
            "builtin_review_batch_preferred": False,
        }
        values.update(changes)
        return TranslationRouterSettings(**values)

    @staticmethod
    def stores(root: Path) -> dict[str, TranslationServerGroupStore]:
        return {
            "draft": TranslationServerGroupStore(root / "draft.sqlite3"),
            "review": TranslationServerGroupStore(root / "review.sqlite3"),
        }

    def test_lists_two_independent_groups_and_never_exposes_tokens(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            app = create_translation_app(self.settings(root), stores=self.stores(root))
            with TestClient(app) as client:
                unauthorized = client.get("/v1/router/groups")
                groups = client.get(
                    "/v1/router/groups",
                    headers={"Authorization": "Bearer router-secret"},
                )

            self.assertEqual(unauthorized.status_code, 401)
            self.assertEqual(groups.status_code, 200)
            payload = groups.json()["items"]
            self.assertEqual([item["stage"] for item in payload], ["draft", "review"])
            self.assertEqual(payload[0]["model"], DEFAULT_DRAFT_MODEL)
            self.assertEqual(payload[1]["model"], DEFAULT_REVIEW_MODEL)
            self.assertEqual(payload[0]["servers"][0]["name"], "기본 Runtime")
            self.assertTrue(payload[0]["servers"][0]["enabled"])
            self.assertFalse(payload[1]["servers"][0]["enabled"])
            self.assertNotIn("token", payload[0]["servers"][0])

    def test_probe_discovers_models_for_only_the_addressed_group(self) -> None:
        with TemporaryDirectory() as directory, patch(
            "stt_to_subtitle.translation_api.requests.get",
            return_value=model_response(DEFAULT_DRAFT_MODEL, DEFAULT_REVIEW_MODEL),
        ):
            root = Path(directory)
            app = create_translation_app(self.settings(root), stores=self.stores(root))
            with TestClient(app) as client:
                response = client.post(
                    "/v1/router/groups/draft/servers/builtin/probe",
                    headers={"Authorization": "Bearer router-secret"},
                )

            self.assertEqual(response.status_code, 200)
            self.assertEqual(
                response.json()["models"],
                [DEFAULT_DRAFT_MODEL, DEFAULT_REVIEW_MODEL],
            )

    def test_builtin_address_is_editable_per_group_and_survives_restart(self) -> None:
        with TemporaryDirectory() as directory, patch(
            "stt_to_subtitle.translation_api.requests.get",
            return_value=model_response("local-ollama-model"),
        ):
            root = Path(directory)
            stores = self.stores(root)
            app = create_translation_app(self.settings(root), stores=stores)
            with TestClient(app) as client:
                response = client.put(
                    "/v1/router/groups/draft/servers/builtin",
                    headers={"Authorization": "Bearer router-secret"},
                    json={
                        "name": "로컬 Ollama",
                        "base_url": "http://127.0.0.1:11434/v1",
                        "enabled": True,
                        "capacity": 2,
                    },
                )

            self.assertEqual(response.status_code, 200)
            self.assertEqual(
                response.json()["base_url"],
                "http://127.0.0.1:11434/v1",
            )
            self.assertEqual(response.json()["models"], ["local-ollama-model"])
            self.assertEqual(
                stores["review"].get("builtin").base_url,
                "http://builtin.test/v1",
            )

            restarted = create_translation_app(
                self.settings(
                    root,
                    builtin_name="다른 배포 기본값",
                    builtin_base_url="http://replacement.test/v1",
                ),
                stores=stores,
            )
            with TestClient(restarted) as client:
                persisted = client.get(
                    "/v1/router/groups/draft",
                    headers={"Authorization": "Bearer router-secret"},
                )

            builtin = persisted.json()["servers"][0]
            self.assertEqual(builtin["name"], "로컬 Ollama")
            self.assertEqual(
                builtin["base_url"],
                "http://127.0.0.1:11434/v1",
            )

    def test_routes_each_stage_through_its_own_server_group(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            stores = self.stores(root)
            draft_external = stores["draft"].create(
                name="draft batch",
                base_url="http://draft-batch.test/v1",
                token="",
                enabled=True,
                capacity=1,
                batch_preferred=True,
            )
            review_external = stores["review"].create(
                name="review",
                base_url="http://review.test/v1",
                token="",
                enabled=True,
                capacity=1,
            )
            app = create_translation_app(self.settings(root), stores=stores)
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
            self.assertEqual(
                calls,
                [
                    ("http://builtin.test/v1/chat/completions", DEFAULT_DRAFT_MODEL),
                    (f"{draft_external.base_url}/chat/completions", DEFAULT_DRAFT_MODEL),
                    (f"{review_external.base_url}/chat/completions", DEFAULT_REVIEW_MODEL),
                ],
            )

    def test_disabled_builtin_review_never_calls_upstream(self) -> None:
        with TemporaryDirectory() as directory, patch(
            "stt_to_subtitle.translation_api.requests.post"
        ) as post:
            root = Path(directory)
            app = create_translation_app(self.settings(root), stores=self.stores(root))
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

    def test_batch_requires_preferred_server_in_addressed_group(self) -> None:
        with TemporaryDirectory() as directory, patch(
            "stt_to_subtitle.translation_api.requests.post"
        ) as post:
            root = Path(directory)
            app = create_translation_app(self.settings(root), stores=self.stores(root))
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

    def test_live_draft_fails_over_inside_draft_group(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            stores = self.stores(root)
            fallback = stores["draft"].create(
                name="fallback",
                base_url="http://fallback.test/v1",
                token="",
                enabled=True,
                capacity=1,
            )
            app = create_translation_app(self.settings(root), stores=stores)
            with patch(
                "stt_to_subtitle.translation_api.requests.post",
                side_effect=[requests.ConnectionError("offline"), completion_response("ok")],
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
                f"{fallback.base_url}/chat/completions",
            )


if __name__ == "__main__":
    unittest.main()
