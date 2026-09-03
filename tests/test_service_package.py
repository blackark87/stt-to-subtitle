from importlib.util import module_from_spec, spec_from_file_location
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "build_service_package.py"
SPEC = spec_from_file_location("build_service_package", SCRIPT)
assert SPEC is not None and SPEC.loader is not None
MODULE = module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


class ServicePackageBoundaryTests(unittest.TestCase):
    def test_backend_package_excludes_ui_and_model_runtime(self) -> None:
        with TemporaryDirectory() as directory:
            destination = Path(directory) / "stt_to_subtitle"
            selected = MODULE.stage(
                "backend",
                ROOT / "src" / "stt_to_subtitle",
                destination,
            )

            self.assertIn("backend_api", selected)
            self.assertIn("translation_routing", selected)
            self.assertIn("translation_store", selected)
            self.assertIn("storage_migration", selected)
            for module in (
                "web_app",
                "runtime_api",
                "kotoba",
                "hybrid_stt",
                "whisperx_worker",
                "whisperjav_worker",
            ):
                self.assertNotIn(module, selected)
                self.assertFalse((destination / f"{module}.py").exists())
            self.assertFalse((destination / "templates").exists())
            self.assertFalse((destination / "static").exists())
            self.assertFalse((destination / "vendor").exists())

    def test_runtime_package_excludes_backend_and_ui(self) -> None:
        with TemporaryDirectory() as directory:
            destination = Path(directory) / "stt_to_subtitle"
            selected = MODULE.stage(
                "runtime",
                ROOT / "src" / "stt_to_subtitle",
                destination,
            )

            self.assertIn("runtime_api", selected)
            self.assertIn("owsm_audit_worker", selected)
            self.assertIn("speaker_worker", selected)
            self.assertIn("stable_ts_worker", selected)
            self.assertTrue((destination / "owsm_audit_worker.py").is_file())
            self.assertFalse((destination / "candidate_asr_worker.py").exists())
            self.assertTrue((destination / "speaker_worker.py").is_file())
            self.assertTrue((destination / "stable_ts_worker.py").is_file())
            for module in (
                "web_app",
                "backend_api",
                "backend_jobs_api",
                "orchestrator",
                "job_store",
            ):
                self.assertNotIn(module, selected)
                self.assertFalse((destination / f"{module}.py").exists())
            self.assertTrue((destination / "vendor" / "whisperjav").is_dir())
            self.assertFalse((destination / "templates").exists())
            self.assertFalse((destination / "static").exists())

if __name__ == "__main__":
    unittest.main()
