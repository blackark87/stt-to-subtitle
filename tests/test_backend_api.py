from importlib.util import find_spec
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from stt_to_subtitle.backend_config import BackendSettings


BACKEND_TESTS_AVAILABLE = all(
    find_spec(module) is not None
    for module in ("fastapi", "httpx", "requests")
)


@unittest.skipUnless(
    BACKEND_TESTS_AVAILABLE,
    "backend test dependencies are not installed",
)
class BackendAPIBoundaryTests(unittest.TestCase):
    def test_exposes_complete_versioned_api_without_html_routes(self) -> None:
        from stt_to_subtitle.backend_api import create_backend_app

        with TemporaryDirectory() as directory:
            root = Path(directory)
            media_root = root / "media"
            media_root.mkdir()
            app = create_backend_app(
                BackendSettings(
                    state_dir=root / "state",
                    media_root=media_root,
                    stt_base_url="http://runtime:8100",
                    stt_token="",
                )
            )

        paths = {str(getattr(route, "path", "")) for route in app.routes}
        self.assertIn("/healthz", paths)
        self.assertIn("/readyz", paths)
        self.assertNotIn("/api/jobs", paths)
        self.assertIn("/api/v1/jobs", paths)
        self.assertIn("/api/v1/jobs/actions/retry", paths)
        self.assertIn("/api/v1/jobs/actions/translate", paths)
        self.assertIn("/api/v1/jobs/{job_id}/retry", paths)
        self.assertIn("/api/v1/jobs/{job_id}/reprocess", paths)
        self.assertIn("/api/v1/jobs/{job_id}/artifacts/{kind}", paths)
        self.assertIn("/api/v1/media", paths)
        self.assertIn("/api/v1/media/file", paths)
        self.assertIn("/api/v1/dashboard/library-progress", paths)
        self.assertIn("/api/v1/settings", paths)
        self.assertIn("/api/v1/settings/servers", paths)
        self.assertIn("/api/v1/runtimes", paths)
        self.assertIn("/api/v1/runtimes/{runtime_id}", paths)
        self.assertIn("/api/v1/runtimes/{runtime_id}/probe", paths)
        self.assertIn(
            "/api/v1/translation-groups/{stage}/servers/{endpoint_id}/model",
            paths,
        )
        self.assertIn("/api/v1/translation-groups/{stage}/servers", paths)
        self.assertIn(
            "/api/v1/translation-groups/{stage}/servers/{endpoint_id}/routing",
            paths,
        )
        self.assertIn("/api/v1/settings/subtitle-validator", paths)
        self.assertIn("/api/v1/settings/external-models", paths)
        self.assertIn(
            "/api/v1/settings/external-models/{provider}/probe",
            paths,
        )
        self.assertIn("/api/v1/jobs/actions/draft-translate", paths)
        self.assertIn("/api/v1/jobs/actions/review-translate", paths)
        self.assertIn("/api/v1/jobs/actions/external-review", paths)
        self.assertIn(
            "/api/v1/jobs/{job_id}/translation-generations/"
            "{generation_id}/items/{segment_id}",
            paths,
        )
        self.assertIn("/api/v1/settings/path-display-rules", paths)
        self.assertIn("/api/v1/settings/prompt-categories", paths)
        self.assertIn("/api/v1/comparisons", paths)
        self.assertIn("/api/v1/operations/metrics", paths)
        self.assertIn(
            "/api/v1/operations/metrics/media-durations",
            paths,
        )
        self.assertGreaterEqual(
            len([path for path in paths if path.startswith("/api/v1/")]),
            35,
        )
        self.assertNotIn("/jobs/events", paths)
        self.assertNotIn("/", paths)
        self.assertNotIn("/jobs", paths)
        self.assertNotIn("/settings", paths)
        self.assertNotIn("/static", paths)

        selection_schema = app.openapi()["components"]["schemas"][
            "TranslationSelectionRequest"
        ]
        translation_mode = selection_schema["properties"]["translation_mode"]
        self.assertEqual(translation_mode["default"], "draft_only")
        self.assertEqual(
            translation_mode["enum"],
            ["draft_only", "review_existing", "draft_and_review"],
        )
        self.assertIn("target_stage", selection_schema["properties"])

    def test_public_api_rejects_combined_pipeline_requests(self) -> None:
        from fastapi.testclient import TestClient

        from stt_to_subtitle.backend_api import create_backend_app

        with TemporaryDirectory() as directory:
            root = Path(directory)
            media_root = root / "media"
            media_root.mkdir()
            settings = BackendSettings(
                state_dir=root / "state",
                media_root=media_root,
                stt_base_url="http://runtime:8100",
                stt_token="",
            )
            with TestClient(create_backend_app(settings)) as client:
                client.app.state.orchestrator.stop()

                create_response = client.post(
                    "/api/v1/jobs",
                    json={"operation": "full"},
                )
                combined_response = client.post(
                    "/api/v1/jobs/actions/translate",
                    json={
                        "job_ids": ["previous-phase"],
                        "prompt_category_id": "jav",
                        "translation_mode": "draft_and_review",
                    },
                )
                reprocess_response = client.post(
                    "/api/v1/jobs/previous-phase/reprocess",
                    json={"operation": "full"},
                )

            self.assertEqual(create_response.status_code, 409)
            self.assertEqual(combined_response.status_code, 409)
            self.assertEqual(reprocess_response.status_code, 422)

    def test_health_and_job_list_run_without_html_application(self) -> None:
        from fastapi.testclient import TestClient

        from stt_to_subtitle.backend_api import create_backend_app

        with TemporaryDirectory() as directory:
            root = Path(directory)
            media_root = root / "media"
            media_root.mkdir()
            settings = BackendSettings(
                state_dir=root / "state",
                media_root=media_root,
                stt_base_url="http://runtime:8100",
                stt_token="",
            )
            with TestClient(create_backend_app(settings)) as client:
                health = client.get("/healthz")
                readiness = client.get("/readyz")
                jobs = client.get("/api/v1/jobs")
                dashboard = client.get("/api/v1/dashboard")
                settings_response = client.get("/api/v1/settings")

        self.assertEqual(health.status_code, 200)
        self.assertEqual(health.json()["status"], "ok")
        self.assertEqual(readiness.status_code, 200)
        self.assertEqual(jobs.status_code, 200)
        self.assertEqual(jobs.json()["items"], [])
        self.assertEqual(jobs.json()["total"], 0)
        self.assertEqual(dashboard.status_code, 200)
        self.assertEqual(dashboard.json()["active_jobs"], [])
        self.assertEqual(dashboard.json()["attention_jobs"], [])
        self.assertEqual(dashboard.json()["recent_completed"], [])
        self.assertEqual(
            dashboard.json()["completion_counts"],
            {"audio": 0, "transcription": 0, "subtitle": 0},
        )
        self.assertEqual(
            dashboard.json()["state_samples"],
            {
                "waiting": [],
                "paused": [],
                "blocked": [],
                "stopped": [],
                "failed": [],
            },
        )
        self.assertEqual(settings_response.status_code, 200)
        self.assertNotIn(
            "stt_token",
            settings_response.json()["servers"],
        )

    def test_job_list_includes_optional_nfo_title_and_poster(self) -> None:
        from fastapi.testclient import TestClient

        from stt_to_subtitle.backend_api import create_backend_app

        with TemporaryDirectory() as directory:
            root = Path(directory)
            media_root = root / "media"
            media_root.mkdir()
            (media_root / "with-metadata.mkv").write_bytes(b"media")
            (media_root / "with-metadata.nfo").write_text(
                "<movie><title>작업 목록 제목</title></movie>",
                encoding="utf-8",
            )
            (media_root / "with-metadata-poster.jpg").write_bytes(b"poster")
            settings = BackendSettings(
                state_dir=root / "state",
                media_root=media_root,
                stt_base_url="http://runtime:8100",
                stt_token="",
            )
            with TestClient(create_backend_app(settings)) as client:
                service = client.app.state.orchestrator
                service.store.create(
                    job_id="with-metadata",
                    source_rel="with-metadata.mkv",
                    force_overwrite=False,
                    options={},
                )
                service.store.create(
                    job_id="missing-media",
                    source_rel="missing-media.mkv",
                    force_overwrite=False,
                    options={},
                )
                response = client.get("/api/v1/jobs")

        self.assertEqual(response.status_code, 200)
        jobs = {item["id"]: item for item in response.json()["items"]}
        self.assertEqual(jobs["with-metadata"]["nfo_title"], "작업 목록 제목")
        self.assertEqual(
            jobs["with-metadata"]["poster_path"],
            "with-metadata-poster.jpg",
        )
        self.assertIsNone(jobs["missing-media"]["nfo_title"])
        self.assertIsNone(jobs["missing-media"]["poster_path"])

    def test_job_detail_groups_independent_phase_runs_as_one_workflow(self) -> None:
        from fastapi.testclient import TestClient

        from stt_to_subtitle.backend_api import create_backend_app

        with TemporaryDirectory() as directory:
            root = Path(directory)
            media_root = root / "media"
            media_root.mkdir()
            (media_root / "sample.mp4").write_bytes(b"media")
            settings = BackendSettings(
                state_dir=root / "state",
                media_root=media_root,
                stt_base_url="http://runtime:8100",
                stt_token="",
            )
            with TestClient(create_backend_app(settings)) as client:
                service = client.app.state.orchestrator
                service.stop()
                service.store.create(
                    job_id="transcription-root",
                    source_rel="sample.mp4",
                    force_overwrite=False,
                    options={},
                    operation="transcribe",
                )
                service.store.create(
                    job_id="draft-phase",
                    source_rel="sample.mp4",
                    force_overwrite=False,
                    options={"pipeline_parent_job_id": "transcription-root"},
                    operation="draft_translate",
                )
                service.store.create(
                    job_id="review-phase",
                    source_rel="sample.mp4",
                    force_overwrite=False,
                    options={"pipeline_parent_job_id": "draft-phase"},
                    operation="review_translate",
                )
                service.store.create(
                    job_id="unrelated-run",
                    source_rel="sample.mp4",
                    force_overwrite=False,
                    options={},
                    operation="transcribe",
                )
                response = client.get("/api/v1/jobs/review-phase")

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertEqual(payload["workflow_root_job_id"], "transcription-root")
        self.assertEqual(
            [item["id"] for item in payload["workflow_jobs"]],
            ["transcription-root", "draft-phase", "review-phase"],
        )
        self.assertEqual(payload["parent_job"]["id"], "draft-phase")
        self.assertEqual(payload["child_jobs"], [])
        self.assertEqual(payload["workflow_history_jobs"], [])

    def test_job_list_groups_phases_and_filters_by_current_job(self) -> None:
        from fastapi.testclient import TestClient

        from stt_to_subtitle.backend_api import create_backend_app

        with TemporaryDirectory() as directory:
            root = Path(directory)
            media_root = root / "media"
            media_root.mkdir()
            settings = BackendSettings(
                state_dir=root / "state",
                media_root=media_root,
                stt_base_url="http://runtime:8100",
                stt_token="",
            )
            with TestClient(create_backend_app(settings)) as client:
                service = client.app.state.orchestrator
                service.stop()
                service.store.create(
                    job_id="workflow-root",
                    source_rel="workflow.mp4",
                    force_overwrite=False,
                    options={},
                    operation="transcribe",
                )
                service.store.create(
                    job_id="workflow-draft",
                    source_rel="workflow.mp4",
                    force_overwrite=False,
                    options={"pipeline_parent_job_id": "workflow-root"},
                    operation="draft_translate",
                )
                service.store.create(
                    job_id="workflow-review",
                    source_rel="workflow.mp4",
                    force_overwrite=False,
                    options={"pipeline_parent_job_id": "workflow-draft"},
                    operation="review_translate",
                )
                service.store.create(
                    job_id="separate-root",
                    source_rel="workflow.mp4",
                    force_overwrite=False,
                    options={},
                    operation="transcribe",
                )
                response = client.get("/api/v1/jobs")
                filtered = client.get(
                    "/api/v1/jobs",
                    params={"operation": "review_translate"},
                )
                historical = client.get(
                    "/api/v1/jobs",
                    params={"operation": "draft_translate"},
                )
                paged = client.get("/api/v1/jobs", params={"limit": 1})

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertEqual(payload["total"], 2)
        workflows = {
            item["workflow_root_job_id"]: item for item in payload["items"]
        }
        self.assertEqual(set(workflows), {"workflow-root", "separate-root"})
        self.assertEqual(workflows["workflow-root"]["id"], "workflow-review")
        self.assertNotIn("workflow_stages", workflows["workflow-root"])
        self.assertEqual(filtered.status_code, 200)
        self.assertEqual(filtered.json()["total"], 1)
        self.assertEqual(filtered.json()["items"][0]["id"], "workflow-review")
        self.assertEqual(historical.status_code, 200)
        self.assertEqual(historical.json()["total"], 0)
        self.assertEqual(paged.status_code, 200)
        self.assertEqual(paged.json()["total"], 2)
        self.assertEqual(len(paged.json()["items"]), 1)

    def test_job_detail_separates_duplicate_branch_from_primary_lineage(self) -> None:
        from fastapi.testclient import TestClient

        from stt_to_subtitle.backend_api import create_backend_app

        with TemporaryDirectory() as directory:
            root = Path(directory)
            media_root = root / "media"
            media_root.mkdir()
            (media_root / "sample.mp4").write_bytes(b"media")
            settings = BackendSettings(
                state_dir=root / "state",
                media_root=media_root,
                stt_base_url="http://runtime:8100",
                stt_token="",
            )
            with TestClient(create_backend_app(settings)) as client:
                service = client.app.state.orchestrator
                service.stop()
                service.store.create(
                    job_id="root",
                    source_rel="sample.mp4",
                    force_overwrite=False,
                    options={},
                    operation="transcribe",
                )
                service.store.create(
                    job_id="draft",
                    source_rel="sample.mp4",
                    force_overwrite=False,
                    options={"pipeline_parent_job_id": "root"},
                    operation="draft_translate",
                )
                service.store.create(
                    job_id="review-used",
                    source_rel="sample.mp4",
                    force_overwrite=False,
                    options={"pipeline_parent_job_id": "draft"},
                    operation="review_translate",
                )
                service.store.create(
                    job_id="external",
                    source_rel="sample.mp4",
                    force_overwrite=False,
                    options={"pipeline_parent_job_id": "review-used"},
                    operation="external_review",
                )
                service.store.create(
                    job_id="review-orphan",
                    source_rel="sample.mp4",
                    force_overwrite=False,
                    options={"pipeline_parent_job_id": "draft"},
                    operation="review_translate",
                )
                response = client.get("/api/v1/jobs/root")

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertEqual(
            [item["id"] for item in payload["workflow_jobs"]],
            ["root", "draft", "review-used", "external"],
        )
        self.assertEqual(
            [item["id"] for item in payload["workflow_history_jobs"]],
            ["review-orphan"],
        )

    def test_dashboard_returns_recent_samples_for_each_large_state(self) -> None:
        from fastapi.testclient import TestClient

        from stt_to_subtitle.backend_api import create_backend_app

        with TemporaryDirectory() as directory:
            root = Path(directory)
            media_root = root / "media"
            media_root.mkdir()
            settings = BackendSettings(
                state_dir=root / "state",
                media_root=media_root,
                stt_base_url="http://runtime:8100",
                stt_token="",
            )
            with TestClient(create_backend_app(settings)) as client:
                service = client.app.state.orchestrator
                for index in range(5):
                    job = service.store.create(
                        job_id=f"stopped-{index}",
                        source_rel=f"movie-{index}.mkv",
                        force_overwrite=False,
                        options={},
                    )
                    service.store.update(
                        job.id,
                        status="blocked",
                        state="stopped",
                        reason_code="user_stop",
                        blocked_stage="extraction",
                    )
                dashboard = client.get("/api/v1/dashboard")

        self.assertEqual(dashboard.status_code, 200)
        self.assertEqual(dashboard.json()["state_counts"]["stopped"], 5)
        self.assertEqual(
            len(dashboard.json()["state_samples"]["stopped"]),
            3,
        )

    def test_dashboard_separates_terminal_operation_counts(self) -> None:
        from fastapi.testclient import TestClient

        from stt_to_subtitle.backend_api import create_backend_app

        with TemporaryDirectory() as directory:
            root = Path(directory)
            media_root = root / "media"
            media_root.mkdir()
            settings = BackendSettings(
                state_dir=root / "state",
                media_root=media_root,
                stt_base_url="http://runtime:8100",
                stt_token="",
            )
            with TestClient(create_backend_app(settings)) as client:
                service = client.app.state.orchestrator
                completed_jobs = (
                    ("audio", "extract", "audio_completed"),
                    ("transcription", "transcribe", "transcription_completed"),
                    ("subtitle", "full", "completed"),
                )
                for job_id, operation, completion_status in completed_jobs:
                    job = service.store.create(
                        job_id=job_id,
                        source_rel=f"{job_id}.mkv",
                        force_overwrite=False,
                        options={},
                        operation=operation,
                    )
                    service.store.update(job.id, status=completion_status)
                dashboard = client.get("/api/v1/dashboard")

        payload = dashboard.json()
        self.assertEqual(dashboard.status_code, 200)
        self.assertEqual(payload["state_counts"]["done"], 3)
        self.assertEqual(
            payload["completion_counts"],
            {"audio": 1, "transcription": 1, "subtitle": 1},
        )
        self.assertEqual(
            [job["id"] for job in payload["recent_completed"]],
            ["subtitle"],
        )

    def test_dashboard_describes_completed_job_models_prompt_and_timing(
        self,
    ) -> None:
        from fastapi.testclient import TestClient

        from stt_to_subtitle.backend_api import create_backend_app

        with TemporaryDirectory() as directory:
            root = Path(directory)
            media_root = root / "media"
            media_root.mkdir()
            settings = BackendSettings(
                state_dir=root / "state",
                media_root=media_root,
                stt_base_url="http://runtime:8100",
                stt_token="",
            )
            with TestClient(create_backend_app(settings)) as client:
                orchestrator = client.app.state.orchestrator
                orchestrator.stop()
                store = orchestrator.store
                job = store.create(
                    job_id="subtitle",
                    source_rel="subtitle.mkv",
                    force_overwrite=False,
                    options={
                        "backend": "whisperjav",
                        "translation_prompt": {
                            "category_id": "variety",
                            "category_name": "이 이름 대신 분류 ID를 사용",
                            "revision_number": 4,
                        },
                    },
                    operation="full",
                )
                store.record_transcript_revision(
                    revision_id="transcript-1",
                    job_id=job.id,
                    audio_revision_id=None,
                    remote_job_id="remote-1",
                    backend="whisperjav",
                    model_revision="ensemble-v2",
                    options_hash="options-hash",
                    artifact_path=str(root / "transcript.json"),
                    content_hash="transcript-hash",
                    origin="automatic",
                    status=None,
                    chunks_total=1,
                )
                for phase, event_code in (
                    ("transcription", "stage.started"),
                    ("transcription", "stage.completed"),
                    ("translation", "stage.started"),
                    ("translation", "stage.completed"),
                    ("render", "stage.started"),
                    ("render", "stage.completed"),
                ):
                    store.add_event(
                        job.id,
                        "info",
                        event_code,
                        event_code=event_code,
                        phase=phase,
                        attempt=1,
                    )
                with store._connect() as connection:
                    rows = connection.execute(
                        """
                        SELECT id FROM job_events
                        WHERE event_code IN ('stage.started', 'stage.completed')
                        ORDER BY id
                        """
                    ).fetchall()
                    for row, created_at in zip(
                        rows,
                        (100.0, 130.0, 200.0, 220.0, 300.0, 310.0),
                        strict=True,
                    ):
                        connection.execute(
                            "UPDATE job_events SET created_at = ? WHERE id = ?",
                            (created_at, int(row["id"])),
                        )
                store.update(job.id, status="completed")
                dashboard = client.get("/api/v1/dashboard")

        self.assertEqual(dashboard.status_code, 200)
        summary = dashboard.json()["recent_completed"][0][
            "completion_summary"
        ]
        self.assertEqual(summary["transcription_backend"], "whisperjav")
        self.assertEqual(
            summary["transcription_model_revision"],
            "ensemble-v2",
        )
        self.assertEqual(summary["translation_prompt_name"], "버라이어티")
        self.assertEqual(summary["translation_prompt_version"], 4)
        self.assertEqual(
            summary["started_at"],
            "1970-01-01T09:01:40.000+09:00",
        )
        self.assertEqual(
            summary["ended_at"],
            "1970-01-01T09:05:10.000+09:00",
        )
        self.assertEqual(summary["processing_seconds"], 60.0)
        self.assertEqual(summary["timing_source"], "events")

    def test_manages_external_runtime_without_exposing_its_token(self) -> None:
        from fastapi.testclient import TestClient

        from stt_to_subtitle.backend_api import create_backend_app

        with TemporaryDirectory() as directory:
            root = Path(directory)
            media_root = root / "media"
            media_root.mkdir()
            settings = BackendSettings(
                state_dir=root / "state",
                media_root=media_root,
                stt_base_url="http://runtime:8100",
                stt_token="",
            )
            with TestClient(create_backend_app(settings)) as client:
                created = client.post(
                    "/api/v1/runtimes",
                    json={
                        "name": "GPU Runtime 02",
                        "base_url": "http://runtime-02.test:8100",
                        "token": "secret-runtime-token",
                        "capacity": 2,
                        "kotoba_batch_size": 4,
                        "whisperx_batch_size": 16,
                        "enabled": False,
                    },
                )
                runtime_id = created.json()["id"]
                listed = client.get("/api/v1/runtimes")
                builtin_updated = client.put(
                    "/api/v1/runtimes/builtin",
                    json={
                        "name": "기본 Runtime",
                        "base_url": "http://runtime:8100",
                        "enabled": True,
                        "capacity": 1,
                        "kotoba_batch_size": 8,
                        "whisperx_batch_size": 24,
                    },
                )
                deleted = client.delete(f"/api/v1/runtimes/{runtime_id}")

        self.assertEqual(created.status_code, 201)
        self.assertTrue(created.json()["token_configured"])
        self.assertEqual(created.json()["kotoba_batch_size"], 4)
        self.assertEqual(created.json()["whisperx_batch_size"], 16)
        self.assertNotIn("token", created.json())
        self.assertEqual(listed.status_code, 200)
        self.assertEqual(listed.json()["total"], 2)
        self.assertEqual(builtin_updated.status_code, 200)
        self.assertEqual(builtin_updated.json()["kotoba_batch_size"], 8)
        self.assertEqual(builtin_updated.json()["whisperx_batch_size"], 24)
        self.assertEqual(deleted.status_code, 204)

    def test_media_api_applies_display_rules_and_actor_profiles(self) -> None:
        from fastapi.testclient import TestClient

        from stt_to_subtitle.backend_api import create_backend_app

        with TemporaryDirectory() as directory:
            root = Path(directory)
            media_root = root / "media"
            actor = media_root / "av" / "japan" / "Actor"
            title = actor / "ABC-001"
            (actor / ".actors").mkdir(parents=True)
            title.mkdir()
            (actor / ".actors" / "Actor.jpg").write_bytes(b"profile")
            (title / "ABC-001.mp4").write_bytes(b"media")
            settings = BackendSettings(
                state_dir=root / "state",
                media_root=media_root,
                stt_base_url="http://runtime:8100",
                stt_token="",
            )
            with TestClient(create_backend_app(settings)) as client:
                actor_listing = client.get(
                    "/api/v1/media",
                    params={"folder": "av/japan"},
                )
                media_listing = client.get(
                    "/api/v1/media",
                    params={"folder": "av/japan/Actor"},
                )

        self.assertEqual(actor_listing.status_code, 200)
        self.assertEqual(
            actor_listing.json()["folders"][0]["actor_image_path"],
            "av/japan/Actor/.actors/Actor.jpg",
        )
        self.assertEqual(media_listing.status_code, 200)
        self.assertEqual(media_listing.json()["folders"], [])
        self.assertEqual(
            media_listing.json()["files"][0]["path"],
            "av/japan/Actor/ABC-001/ABC-001.mp4",
        )
        self.assertEqual(
            media_listing.json()["files"][0]["display_path"],
            "av/japan/Actor/ABC-001.mp4",
        )

    def test_media_api_pages_folders_without_hiding_the_total(self) -> None:
        from fastapi.testclient import TestClient

        from stt_to_subtitle.backend_api import create_backend_app

        with TemporaryDirectory() as directory:
            root = Path(directory)
            media_root = root / "media"
            for name in ("alpha", "bravo", "charlie"):
                (media_root / name).mkdir(parents=True)
            settings = BackendSettings(
                state_dir=root / "state",
                media_root=media_root,
                stt_base_url="http://runtime:8100",
                stt_token="",
            )
            with TestClient(create_backend_app(settings)) as client:
                response = client.get(
                    "/api/v1/media",
                    params={"folder_offset": 1, "folder_limit": 1},
                )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["folder_total"], 3)
        self.assertEqual(response.json()["folder_offset"], 1)
        self.assertEqual(response.json()["folder_limit"], 1)
        self.assertEqual(
            [folder["name"] for folder in response.json()["folders"]],
            ["bravo"],
        )


if __name__ == "__main__":
    unittest.main()
