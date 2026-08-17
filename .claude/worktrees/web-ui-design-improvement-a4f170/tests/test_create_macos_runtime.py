from pathlib import Path
import tempfile
import unittest

from scripts.create_macos_runtime import create_runtime


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]


class CreateMacOSRuntimeTests(unittest.TestCase):
    def test_exports_runtime_outside_repository(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "runtime"

            result = create_runtime(
                target,
                repository_root=REPOSITORY_ROOT,
            )

            self.assertEqual(result, target.resolve())
            self.assertTrue((target / ".stt-macos-runtime").is_file())
            self.assertTrue((target / ".env.example").is_file())
            self.assertTrue((target / "setup.sh").is_file())
            self.assertTrue((target / "run.sh").is_file())
            self.assertTrue(
                (target / "src" / "stt_to_subtitle" / "macos_api.py").is_file()
            )
            self.assertFalse((target / ".env").exists())
            self.assertFalse((target / ".venv-macos").exists())

    def test_rejects_target_inside_repository(self) -> None:
        with self.assertRaisesRegex(ValueError, "outside the Git repository"):
            create_runtime(
                REPOSITORY_ROOT / "var" / "runtime",
                repository_root=REPOSITORY_ROOT,
            )

    def test_update_preserves_runtime_data_and_replaces_source(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "runtime"
            create_runtime(target, repository_root=REPOSITORY_ROOT)
            (target / ".env").write_text("HF_TOKEN=test\n", encoding="utf-8")
            (target / ".venv-macos").mkdir()
            state_file = target / "var" / "macos-stt" / "state.db"
            state_file.parent.mkdir(parents=True)
            state_file.write_text("state", encoding="utf-8")
            stale_source = (
                target / "src" / "stt_to_subtitle" / "removed_module.py"
            )
            stale_source.write_text("stale", encoding="utf-8")

            create_runtime(
                target,
                update=True,
                repository_root=REPOSITORY_ROOT,
            )

            self.assertEqual(
                (target / ".env").read_text(encoding="utf-8"),
                "HF_TOKEN=test\n",
            )
            self.assertTrue((target / ".venv-macos").is_dir())
            self.assertEqual(state_file.read_text(encoding="utf-8"), "state")
            self.assertFalse(stale_source.exists())


if __name__ == "__main__":
    unittest.main()
