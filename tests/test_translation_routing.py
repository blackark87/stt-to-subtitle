from collections.abc import Callable
import json
from pathlib import Path
import sqlite3
from tempfile import TemporaryDirectory
import threading
import time
import unittest
from unittest.mock import Mock, patch

from stt_to_subtitle.service_clients import (
    ExternalServiceError,
    RetryingJSONClient,
    TranslationDeferred,
)
from stt_to_subtitle.translation_routing import (
    BackendTranslationRouting,
    TranslationRoutingDefaults,
)
from stt_to_subtitle.translation_store import (
    TranslationServerGroupStore,
    migrate_legacy_translation_endpoints,
)


DRAFT_MODEL = "draft-model"
REVIEW_MODEL = "review-model"


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
    def test_groups_own_server_model_selections_independently(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            draft = TranslationServerGroupStore(root / "draft.sqlite3")
            review = TranslationServerGroupStore(root / "review.sqlite3")
            draft.create(
                name="draft-only",
                base_url="http://draft.test/v1",
                token="",
                enabled=True,
                capacity=1,
                selected_model=DRAFT_MODEL,
                models=(DRAFT_MODEL,),
            )

            self.assertEqual(draft.list()[0].selected_model, DRAFT_MODEL)
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

    def test_disabling_server_clears_batch_selection(self) -> None:
        with TemporaryDirectory() as directory:
            store = TranslationServerGroupStore(Path(directory) / "draft.sqlite3")
            server = store.create(
                name="batch",
                base_url="http://batch.test/v1",
                token="",
                enabled=True,
                capacity=1,
                batch_preferred=True,
            )

            disabled = store.set_routing(
                server.id,
                enabled=False,
                batch_preferred=True,
            )

            self.assertFalse(disabled.enabled)
            self.assertFalse(disabled.batch_preferred)

    def test_disabled_server_cannot_be_created_as_batch_selection(self) -> None:
        with TemporaryDirectory() as directory:
            store = TranslationServerGroupStore(Path(directory) / "draft.sqlite3")

            server = store.create(
                name="disabled",
                base_url="http://disabled.test/v1",
                token="",
                enabled=False,
                capacity=1,
                batch_preferred=True,
            )

            self.assertFalse(server.enabled)
            self.assertFalse(server.batch_preferred)

    def test_editing_selected_server_to_disabled_clears_selection(self) -> None:
        with TemporaryDirectory() as directory:
            store = TranslationServerGroupStore(Path(directory) / "draft.sqlite3")
            server = store.create(
                name="selected",
                base_url="http://selected.test/v1",
                token="",
                enabled=True,
                capacity=1,
                batch_preferred=True,
            )

            disabled = store.update(
                server.id,
                name=server.name,
                base_url=server.base_url,
                token=server.token,
                enabled=False,
                capacity=server.capacity,
            )

            self.assertFalse(disabled.enabled)
            self.assertFalse(disabled.batch_preferred)

    def test_migrates_group_model_to_each_existing_server(self) -> None:
        with TemporaryDirectory() as directory:
            path = Path(directory) / "draft.sqlite3"
            with sqlite3.connect(path) as connection:
                connection.executescript(
                    """
                    CREATE TABLE translation_servers (
                        id TEXT PRIMARY KEY, name TEXT NOT NULL,
                        base_url TEXT NOT NULL UNIQUE,
                        token TEXT NOT NULL DEFAULT '',
                        enabled INTEGER NOT NULL DEFAULT 1,
                        capacity INTEGER NOT NULL DEFAULT 1,
                        builtin INTEGER NOT NULL DEFAULT 0,
                        batch_preferred INTEGER NOT NULL DEFAULT 0,
                        models_json TEXT NOT NULL DEFAULT '[]',
                        checked_at REAL, created_at REAL NOT NULL,
                        updated_at REAL NOT NULL
                    );
                    CREATE TABLE translation_group_settings (
                        key TEXT PRIMARY KEY, value TEXT NOT NULL,
                        updated_at REAL NOT NULL
                    );
                    """
                )
                connection.execute(
                    "INSERT INTO translation_group_settings VALUES ('model', ?, 1)",
                    (DRAFT_MODEL,),
                )
                connection.execute(
                    "INSERT INTO translation_servers VALUES "
                    "('exact', 'exact', 'http://exact.test/v1', '', 1, 1, 0, 0, ?, 1, 1, 1)",
                    (json.dumps([DRAFT_MODEL, "other-model"]),),
                )
                connection.execute(
                    "INSERT INTO translation_servers VALUES "
                    "('single', 'single', 'http://single.test/v1', '', 1, 1, 0, 0, ?, 1, 1, 1)",
                    (json.dumps(["provider-specific-model"]),),
                )

            store = TranslationServerGroupStore(path)
            selected = {item.id: item.selected_model for item in store.list()}

            self.assertEqual(selected["exact"], DRAFT_MODEL)
            self.assertEqual(selected["single"], "provider-specific-model")

    def test_server_model_must_come_from_that_server(self) -> None:
        with TemporaryDirectory() as directory:
            store = TranslationServerGroupStore(Path(directory) / "draft.sqlite3")
            server = store.create(
                name="models",
                base_url="http://models.test/v1",
                token="",
                enabled=True,
                capacity=1,
                models=("first", "second"),
            )

            selected = store.set_selected_model(server.id, "second")

            self.assertEqual(selected.selected_model, "second")
            with self.assertRaisesRegex(ValueError, "제공하지 않습니다"):
                store.set_selected_model(server.id, "another-server-model")

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
                        DRAFT_MODEL,
                        REVIEW_MODEL,
                        json.dumps([DRAFT_MODEL, REVIEW_MODEL]),
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
            self.assertEqual(draft.list()[0].selected_model, DRAFT_MODEL)
            self.assertEqual(review.list()[0].selected_model, REVIEW_MODEL)


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
        stt_hard_breaker_active: Callable[[], bool] | None = None,
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
        routing = BackendTranslationRouting(
            TranslationRoutingDefaults(**values),
            stores=stores,
            stt_hard_breaker_active=stt_hard_breaker_active,
        )
        routing.stores["draft"].save_models("builtin", [DRAFT_MODEL])
        routing.stores["review"].save_models("builtin", [REVIEW_MODEL])
        return routing

    def test_lists_two_independent_groups_without_exposing_tokens(self) -> None:
        with TemporaryDirectory() as directory:
            routing = self.routing(Path(directory))

            groups = routing.groups()

            self.assertEqual([item["stage"] for item in groups], ["draft", "review"])
            self.assertNotIn("model", groups[0])
            self.assertEqual(groups[0]["servers"][0]["selected_model"], DRAFT_MODEL)
            self.assertEqual(groups[1]["servers"][0]["selected_model"], REVIEW_MODEL)
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
                        "max_tokens": 4096,
                        "messages": [],
                        "reasoning_effort": "none",
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
                DRAFT_MODEL,
            )
            self.assertEqual(
                set(request.call_args.kwargs["json"]),
                {"model", "max_tokens", "messages", "reasoning_effort"},
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
            self.assertEqual(result["status"], "ready")
            self.assertEqual(result["models"], ["disabled-server-model"])

            disabled = routing.update_routing(
                "review",
                "builtin",
                {"enabled": False, "batch_preferred": False},
            )

            self.assertFalse(disabled["enabled"])
            self.assertEqual(disabled["status"], "ready")

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
                selected_model="review-server-model",
                models=("review-server-model",),
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
                selected_model="fallback-model",
                models=("fallback-model",),
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
            self.assertEqual(
                request.call_args_list[0].kwargs["json"]["model"],
                DRAFT_MODEL,
            )
            self.assertEqual(
                request.call_args_list[1].kwargs["json"]["model"],
                "fallback-model",
            )

    def test_hard_breaker_routes_away_from_shared_stt_host(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            stores = self.stores(root)
            fallback = stores["draft"].create(
                name="separate accelerator",
                base_url="http://fallback.test/v1",
                token="",
                enabled=True,
                capacity=1,
                selected_model="fallback-model",
                models=("fallback-model",),
            )
            routing = self.routing(
                root,
                stores=stores,
                stt_hard_breaker_hosts=("builtin.test",),
                stt_hard_breaker_active=lambda: True,
            )
            with patch.object(
                RetryingJSONClient,
                "request",
                return_value=completion_response(),
            ) as request:
                response = routing.request_completion(
                    "draft",
                    "live",
                    {"messages": []},
                )

            builtin = routing.group("draft")["servers"][0]
            self.assertEqual(response.status_code, 200)
            self.assertEqual(builtin["status"], "suspended")
            self.assertEqual(builtin["available_slots"], 0)
            self.assertIn("전사 모델", builtin["message"])
            self.assertEqual(
                request.call_args.args[1],
                f"{fallback.base_url}/chat/completions",
            )

    def test_hard_breaker_defers_when_every_route_shares_stt_memory(self) -> None:
        with TemporaryDirectory() as directory:
            routing = self.routing(
                Path(directory),
                stt_hard_breaker_hosts=("builtin.test",),
                stt_hard_breaker_active=lambda: True,
            )

            self.assertTrue(routing.is_configured())
            self.assertFalse(routing.has_routable_server())
            with self.assertRaisesRegex(TranslationDeferred, "일시 중지"):
                routing.request_completion("draft", "live", {"messages": []})

    def test_engaging_hard_breaker_unloads_and_verifies_ollama_models(
        self,
    ) -> None:
        with TemporaryDirectory() as directory:
            routing = self.routing(
                Path(directory),
                stt_hard_breaker_hosts=("builtin.test",),
            )
            loaded = Mock(status_code=200)
            loaded.json.return_value = {"models": [{"name": DRAFT_MODEL}]}
            empty = Mock(status_code=200)
            empty.json.return_value = {"models": []}
            unloaded = Mock(status_code=200)
            with patch(
                "stt_to_subtitle.translation_routing.requests.get",
                side_effect=[loaded, empty],
            ) as get, patch(
                "stt_to_subtitle.translation_routing.requests.post",
                return_value=unloaded,
            ) as post:
                result = routing.engage_stt_hard_breaker()
                suspended = routing.group("draft")["servers"][0]
                routing.release_stt_hard_breaker()

            self.assertTrue(result["enabled"])
            self.assertEqual(result["unloaded_models"], [DRAFT_MODEL])
            self.assertEqual(suspended["status"], "suspended")
            self.assertEqual(get.call_count, 2)
            post.assert_called_once_with(
                "http://builtin.test/api/generate",
                headers={
                    "Accept": "application/json",
                    "Authorization": "Bearer builtin-secret",
                    "Content-Type": "application/json",
                },
                json={
                    "model": DRAFT_MODEL,
                    "prompt": "",
                    "stream": False,
                    "keep_alive": 0,
                },
                timeout=(10.0, 30.0),
            )

    def test_hard_breaker_drains_active_request_before_unloading(self) -> None:
        with TemporaryDirectory() as directory:
            routing = self.routing(
                Path(directory),
                stt_hard_breaker_hosts=("builtin.test",),
            )
            key = ("draft", "builtin")
            with routing._condition:
                routing._active_requests[key] = 1
            finished = threading.Event()
            failures: list[BaseException] = []

            def engage() -> None:
                try:
                    routing.engage_stt_hard_breaker()
                except BaseException as error:
                    failures.append(error)
                finally:
                    finished.set()

            with patch.object(
                routing,
                "_unload_ollama_models",
                return_value=[],
            ) as unload:
                thread = threading.Thread(target=engage)
                thread.start()
                deadline = time.monotonic() + 1.0
                while not routing.hard_breaker_active():
                    self.assertLess(time.monotonic(), deadline)
                    time.sleep(0.01)
                self.assertFalse(finished.is_set())
                unload.assert_not_called()
                with routing._condition:
                    routing._active_requests[key] = 0
                    routing._condition.notify_all()
                thread.join(timeout=1.0)

            self.assertFalse(thread.is_alive())
            self.assertEqual(failures, [])
            unload.assert_called_once_with()
            routing.release_stt_hard_breaker()

    def test_hard_breaker_fails_closed_when_ollama_cannot_be_verified(
        self,
    ) -> None:
        with TemporaryDirectory() as directory:
            routing = self.routing(
                Path(directory),
                stt_hard_breaker_hosts=("builtin.test",),
            )
            unavailable = Mock(status_code=503)

            with patch(
                "stt_to_subtitle.translation_routing.requests.get",
                return_value=unavailable,
            ), self.assertRaisesRegex(
                ExternalServiceError,
                "확인할 수 없습니다",
            ):
                routing.engage_stt_hard_breaker()

            self.assertFalse(routing.hard_breaker_active())


if __name__ == "__main__":
    unittest.main()
