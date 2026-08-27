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
                    lm_base_url="",
                    lm_token="",
                    lm_model="",
                )
            )

        paths = {str(getattr(route, "path", "")) for route in app.routes}
        self.assertIn("/healthz", paths)
        self.assertIn("/readyz", paths)
        self.assertNotIn("/api/jobs", paths)
        self.assertIn("/api/v1/jobs", paths)
        self.assertIn("/api/v1/jobs/actions/retry", paths)
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
        self.assertIn("/api/v1/settings/subtitle-validator", paths)
        self.assertIn("/api/v1/settings/path-display-rules", paths)
        self.assertIn("/api/v1/settings/prompt-categories", paths)
        self.assertIn("/api/v1/comparisons", paths)
        self.assertIn("/api/v1/operations/metrics", paths)
        self.assertGreaterEqual(
            len([path for path in paths if path.startswith("/api/v1/")]),
            35,
        )
        self.assertNotIn("/jobs/events", paths)
        self.assertNotIn("/", paths)
        self.assertNotIn("/jobs", paths)
        self.assertNotIn("/settings", paths)
        self.assertNotIn("/static", paths)

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
                lm_base_url="",
                lm_token="",
                lm_model="",
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
        self.assertEqual(settings_response.status_code, 200)
        self.assertNotIn(
            "stt_token",
            settings_response.json()["servers"],
        )

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
                lm_base_url="",
                lm_token="",
                lm_model="",
            )
            with TestClient(create_backend_app(settings)) as client:
                created = client.post(
                    "/api/v1/runtimes",
                    json={
                        "name": "GPU Runtime 02",
                        "base_url": "http://runtime-02.test:8100",
                        "token": "secret-runtime-token",
                        "capacity": 2,
                        "enabled": False,
                    },
                )
                runtime_id = created.json()["id"]
                listed = client.get("/api/v1/runtimes")
                deleted = client.delete(f"/api/v1/runtimes/{runtime_id}")

        self.assertEqual(created.status_code, 201)
        self.assertTrue(created.json()["token_configured"])
        self.assertNotIn("token", created.json())
        self.assertEqual(listed.status_code, 200)
        self.assertEqual(listed.json()["total"], 2)
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
                lm_base_url="",
                lm_token="",
                lm_model="",
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


if __name__ == "__main__":
    unittest.main()
