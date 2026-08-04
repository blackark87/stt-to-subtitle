from pathlib import Path
import tomllib
import unittest


ROOT = Path(__file__).resolve().parents[1]


class ReleaseConfigurationTests(unittest.TestCase):
    def test_container_tags_have_no_nas_prefix(self) -> None:
        workflow = (ROOT / ".github/workflows/publish-ghcr.yaml").read_text(
            encoding="utf-8"
        )
        compose = (ROOT / "compose.yaml").read_text(encoding="utf-8")
        example = (ROOT / ".env.nas.example").read_text(encoding="utf-8")

        self.assertIn("type=raw,value=latest", workflow)
        self.assertIn("steps.version.outputs.value", workflow)
        self.assertIn("steps.version.outputs.major_minor", workflow)
        self.assertIn("type=sha,prefix=,format=long", workflow)
        self.assertNotIn("value=nas-", workflow)
        self.assertIn("stt-to-subtitle:latest", compose)
        self.assertIn("stt-to-subtitle:latest", example)

    def test_project_version_is_0_13_0(self) -> None:
        project = tomllib.loads(
            (ROOT / "pyproject.toml").read_text(encoding="utf-8")
        )
        self.assertEqual(project["project"]["version"], "0.13.0")


if __name__ == "__main__":
    unittest.main()
