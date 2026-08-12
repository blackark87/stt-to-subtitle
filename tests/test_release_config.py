from pathlib import Path
import tomllib
import unittest


ROOT = Path(__file__).resolve().parents[1]


class ReleaseConfigurationTests(unittest.TestCase):
    def test_compose_builds_web_and_stt_images_locally(self) -> None:
        compose = (ROOT / "compose.yaml").read_text(encoding="utf-8")
        workflow = (ROOT / ".github/workflows/ci.yaml").read_text(
            encoding="utf-8"
        )

        self.assertIn("dockerfile: Dockerfile.web", compose)
        self.assertIn("dockerfile: Dockerfile", compose)
        self.assertIn("http://stt:8100", compose)
        self.assertNotIn("packages" + ": write", workflow)
        self.assertNotIn("build-push-action", workflow)
        registry_name = "gh" + "cr.io"
        self.assertNotIn(registry_name, compose.lower())
        self.assertNotIn(registry_name, workflow.lower())

    def test_project_version_is_1_0_0(self) -> None:
        project = tomllib.loads(
            (ROOT / "pyproject.toml").read_text(encoding="utf-8")
        )
        self.assertEqual(project["project"]["version"], "1.0.0")


if __name__ == "__main__":
    unittest.main()
