import json
from pathlib import Path
import sqlite3
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import Mock, patch

from stt_to_subtitle.service_clients import ExternalServiceError, RetryingJSONClient
from stt_to_subtitle.translation_routing import (
    BackendTranslationRouting,
    DEFAULT_DRAFT_MODEL,
    DEFAULT_REVIEW_MODEL,
    TranslationRoutingDefaults,
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


def completion_response(text: str = "ok") -> Mock:
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


class BackendTranslationRoutingTests(unittest.TestCase):
    @staticmethod
    def stores(root: Path) -> dict[str, TranslationServerGroupStore]:
        return {
            "draft": TranslationServerGroupStore(root / "draft.sqlite3"),
            "review": TranslationServerGroupStore(root / "review.sqlite3"),
        }

    def routing(
        self,
        root: Path,
        *,
        stores: dict[str, TranslationServerGroupStore] | None = None,
        **changes: object,
    ) -> BackendTranslationRouting:
        values = {
            "state_dir": root,
            "builtin_name": "기본 서버",
            "builtin_base_url": "http://builtin.test/v1",
            "builtin_token": "builtin-secret",
            "draft_enabled": True,
            "review_enabled": False,
        }
        values.update(changes)
        return BackendTranslationRouting(
            TranslationRoutingDefaults(**values),
            stores=stores,
        )

    def test_lists_two_independent_groups_without_exposing_tokens(self) -> None:
        with TemporaryDirectory() as directory:
            routing = self.routing(Path(directory))

            groups = routing.groups()

            self.assertEqual([item["stage"] for item in groups], ["draft", "review"])
            self.assertEqual(groups[0]["model"], DEFAULT_DRAFT_MODEL)
            self.assertEqual(groups[1]["model"], DEFAULT_REVIEW_MODEL)
            self.assertTrue(groups[0]["servers"][0]["enabled"])
            self.assertFalse(groups[1]["servers"][0]["enabled"])
            self.assertNotIn("token", groups[0]["servers"][0])

    def test_builtin_address_is_editable_per_group_and_survives_restart(self) -> None:
        with TemporaryDirectory() as directory, patch(
            "stt_to_subtitle.translation_routing.requests.get",
            return_value=model_response("local-model"),
        ):
            root = Path(directory)
            stores = self.stores(root)
            routing = self.routing(root, stores=stores)

            updated = routing.update_server(
                "draft",
                "builtin",
                {
                    "name": "로컬 API",
                    "base_url": "http://127.0.0.1:11434/v1",
                    "enabled": True,
                    "capacity": 2,
                },
            )

            self.assertEqual(updated["base_url"], "http://127.0.0.1:11434/v1")
            self.assertEqual(updated["models"], ["local-model"])
            review_builtin = stores["review"].get("builtin")
            self.assertIsNotNone(review_builtin)
            assert review_builtin is not None
            self.assertEqual(review_builtin.base_url, "http://builtin.test/v1")

            restarted = self.routing(
                root,
                stores=stores,
                builtin_name="다른 기본값",
                builtin_base_url="http://replacement.test/v1",
            )
            builtin = restarted.group("draft")["servers"][0]
            self.assertEqual(builtin["name"], "로컬 API")
            self.assertEqual(builtin["base_url"], "http://127.0.0.1:11434/v1")
            self.assertEqual(builtin["status"], "unknown")

    def test_renames_only_the_legacy_builtin_default(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            stores = self.stores(root)
            stores["draft"].sync_builtin(
                name="기본 Runtime",
                base_url="http://builtin.test/v1",
                token="",
                enabled=True,
                capacity=1,
                batch_preferred=False,
            )

            routing = self.routing(root, stores=stores)

            self.assertEqual(
                routing.group("draft")["servers"][0]["name"],
                "기본 서버",
            )

    def test_calls_selected_openai_server_directly_without_router_headers(self) -> None:
        with TemporaryDirectory() as directory:
            routing = self.routing(Path(directory))
            response = completion_response()
            with patch.object(
                RetryingJSONClient,
                "request",
                return_value=response,
            ) as request:
                result = routing.request_completion(
                    "draft",
                    "live",
                    {
                        "model": "placeholder",
                        "messages": [],
                        "runtime": "gpu-3080",
                        "worker": "transcription-worker-7",
                        "stt_model": "whisperjav",
                    },
                )

            self.assertIs(result, response)
            request.assert_called_once()
            self.assertEqual(
                request.call_args.args,
                ("POST", "http://builtin.test/v1/chat/completions"),
            )
            headers = request.call_args.kwargs["headers"]
            self.assertEqual(
                headers,
                {
                    "Accept": "application/json",
                    "Authorization": "Bearer builtin-secret",
                    "Content-Type": "application/json",
                },
            )
            self.assertNotIn("X-Translation-Pass", headers)
            self.assertEqual(
                request.call_args.kwargs["json"]["model"],
                DEFAULT_DRAFT_MODEL,
            )
            self.assertEqual(
                set(request.call_args.kwargs["json"]),
                {"model", "messages"},
            )

    def test_explicit_probe_checks_a_disabled_server(self) -> None:
        with TemporaryDirectory() as directory, patch(
            "stt_to_subtitle.translation_routing.requests.get",
            return_value=model_response("disabled-server-model"),
        ) as request:
            routing = self.routing(Path(directory))

            result = routing.probe_server("review", "builtin")

            request.assert_called_once_with(
                "http://builtin.test/v1/models",
                headers={
                    "Accept": "application/json",
                    "Authorization": "Bearer builtin-secret",
                    "Content-Type": "application/json",
                },
                timeout=(10.0, 30.0),
            )
            self.assertFalse(result["enabled"])
            self.assertEqual(result["status"], "disabled")
            self.assertEqual(result["models"], ["disabled-server-model"])

    def test_draft_and_review_use_their_own_server_groups(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            stores = self.stores(root)
            stores["review"].create(
                name="review",
                base_url="http://review.test/v1",
                token="",
                enabled=True,
                capacity=1,
            )
            routing = self.routing(root, stores=stores)
            with patch.object(
                RetryingJSONClient,
                "request",
                return_value=completion_response(),
            ) as request:
                routing.request_completion("draft", "live", {"messages": []})
                routing.request_completion("review", "live", {"messages": []})

            self.assertEqual(
                [call.args[1] for call in request.call_args_list],
                [
                    "http://builtin.test/v1/chat/completions",
                    "http://review.test/v1/chat/completions",
                ],
            )

    def test_batch_requires_the_selected_server(self) -> None:
        with TemporaryDirectory() as directory:
            routing = self.routing(Path(directory))

            with self.assertRaisesRegex(ExternalServiceError, "설정되지"):
                routing.request_completion("draft", "batch", {"messages": []})

    def test_direct_request_fails_over_inside_one_group(self) -> None:
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
            routing = self.routing(root, stores=stores)
            with patch.object(
                RetryingJSONClient,
                "request",
                side_effect=[
                    ExternalServiceError("offline"),
                    completion_response(),
                ],
            ) as request:
                response = routing.request_completion(
                    "draft",
                    "live",
                    {"messages": []},
                )

            self.assertEqual(response.status_code, 200)
            self.assertEqual(request.call_count, 2)
            self.assertEqual(
                request.call_args_list[1].args[1],
                f"{fallback.base_url}/chat/completions",
            )


if __name__ == "__main__":
    unittest.main()
