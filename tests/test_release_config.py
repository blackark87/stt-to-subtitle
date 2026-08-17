from pathlib import Path
import tomllib
import unittest

from stt_to_subtitle import __version__


ROOT = Path(__file__).resolve().parents[1]


class ReleaseConfigurationTests(unittest.TestCase):
    def test_compose_builds_web_and_stt_images_locally(self) -> None:
        compose = (ROOT / "compose.yaml").read_text(encoding="utf-8")
        workflow = (ROOT / ".github/workflows/ci.yaml").read_text(
            encoding="utf-8"
        )

        self.assertIn("dockerfile: Dockerfile.web", compose)
        self.assertIn("dockerfile: Dockerfile", compose)
        self.assertIn("dockerfile: Dockerfile.stt-runtime", compose)
        self.assertEqual(compose.count("context: ${WORKSPACE:-.}"), 3)
        self.assertIn("STT_RUNTIME_IMAGE:", compose)
        self.assertIn("stt-to-subtitle-stt-runtime:py311-cuda-v1", compose)
        stt_dockerfile = (ROOT / "Dockerfile").read_text(encoding="utf-8")
        runtime_dockerfile = (ROOT / "Dockerfile.stt-runtime").read_text(
            encoding="utf-8"
        )
        self.assertIn("FROM ${STT_RUNTIME_IMAGE} AS runtime", stt_dockerfile)
        self.assertNotIn("requirements-kotoba.txt", stt_dockerfile)
        self.assertIn("requirements-kotoba.txt", runtime_dockerfile)
        self.assertIn("requirements-whisperx-cuda.txt", runtime_dockerfile)
        self.assertIn(
            "nvidia-npp-cu12==12.3.3.100",
            (ROOT / "requirements-whisperx-cuda.in").read_text(
                encoding="utf-8"
            ),
        )
        self.assertIn("nvidia/npp/lib", runtime_dockerfile)
        self.assertIn("STT_BASE_URL: http://stt:8100", compose)
        self.assertIn("container_name: stt-web", compose)
        self.assertIn("container_name: stt-backend", compose)
        self.assertIn(
            "WEB_STATE_DIR: /var/lib/stt", compose
        )
        self.assertIn(
            "STT_STATE_DIR: /var/lib/stt", compose
        )
        self.assertIn("target: /var/cache/stt", compose)
        self.assertNotIn("${STT_BASE_URL", compose)
        self.assertNotIn("STT_API_TOKEN:", compose)
        self.assertIn("${WEB_PUID:-1026}:${WEB_PGID:-100}", compose)
        self.assertIn("${PUID:-1000}:${PGID:-1000}", compose)
        self.assertNotIn("${WEB_PORT", compose)
        self.assertIn('traefik.enable: "true"', compose)
        self.assertIn("${TRAEFIK_HOST:?TRAEFIK_HOST must be set}", compose)
        self.assertIn(
            "WEB_SECURE_COOKIE: ${WEB_SECURE_COOKIE:-true}", compose
        )
        self.assertIn(
            "WEB_FORWARDED_ALLOW_IPS: \"${WEB_FORWARDED_ALLOW_IPS:-*}\"",
            compose,
        )
        self.assertIn(
            "traefik.http.services.stt-to-subtitle."
            'loadbalancer.server.port: "8080"',
            compose,
        )
        self.assertIn("external: true", compose)
        self.assertNotIn("packages" + ": write", workflow)
        self.assertNotIn("build-push-action", workflow)
        registry_name = "gh" + "cr.io"
        self.assertNotIn(registry_name, compose.lower())
        self.assertNotIn(registry_name, workflow.lower())

    def test_compose_launcher_uses_the_calling_non_root_account(self) -> None:
        launcher = (ROOT / "scripts/compose.sh").read_text(encoding="utf-8")

        self.assertIn("runtime_uid=$(id -u)", launcher)
        self.assertIn("runtime_gid=$(id -g)", launcher)
        self.assertIn('if [ "$runtime_uid" -eq 0 ]', launcher)
        self.assertIn('export PUID="$runtime_uid"', launcher)
        self.assertIn('export PGID="$runtime_gid"', launcher)

    def test_runtime_requirements_have_no_platform_wrapper_files(self) -> None:
        self.assertTrue((ROOT / "requirements-api.txt").is_file())
        self.assertTrue((ROOT / "requirements-kotoba.txt").is_file())
        self.assertFalse((ROOT / "requirements-cuda.txt").exists())

    def test_project_and_package_versions_are_3_3_1(self) -> None:
        project = tomllib.loads(
            (ROOT / "pyproject.toml").read_text(encoding="utf-8")
        )
        self.assertEqual(project["project"]["version"], "3.3.1")
        self.assertEqual(__version__, "3.3.1")


if __name__ == "__main__":
    unittest.main()
