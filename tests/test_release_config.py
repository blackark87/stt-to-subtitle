from pathlib import Path
import stat
import tomllib
import unittest

from stt_to_subtitle import __version__


ROOT = Path(__file__).resolve().parents[1]


class ReleaseConfigurationTests(unittest.TestCase):
    def test_compose_builds_isolated_web_backend_and_runtime_images(self) -> None:
        compose = (ROOT / "compose.yaml").read_text(encoding="utf-8")
        workflow = (ROOT / ".github/workflows/ci.yaml").read_text(
            encoding="utf-8"
        )

        self.assertIn("dockerfile: Dockerfile.web", compose)
        self.assertIn("dockerfile: Dockerfile.backend", compose)
        self.assertNotIn("dockerfile: Dockerfile.translation", compose)
        self.assertIn("dockerfile: Dockerfile.runtime", compose)
        self.assertIn("dockerfile: Dockerfile.stt-runtime", compose)
        self.assertEqual(compose.count("context: ${WORKSPACE:-.}"), 4)
        self.assertIn("STT_RUNTIME_BASE_IMAGE:", compose)
        self.assertIn("stt-to-subtitle-runtime-base:py311-cuda-v4", compose)
        backend_dockerfile = (ROOT / "Dockerfile.backend").read_text(
            encoding="utf-8"
        )
        self.assertFalse((ROOT / "Dockerfile.translation").exists())
        runtime_dockerfile = (ROOT / "Dockerfile.runtime").read_text(
            encoding="utf-8"
        )
        runtime_base_dockerfile = (ROOT / "Dockerfile.stt-runtime").read_text(
            encoding="utf-8"
        )
        self.assertIn(
            "FROM ${STT_RUNTIME_BASE_IMAGE} AS runtime",
            runtime_dockerfile,
        )
        self.assertNotIn("AS whisperjav-builder", runtime_dockerfile)
        self.assertIn(
            "FROM ${PYTHON_IMAGE} AS whisperjav-builder",
            runtime_base_dockerfile,
        )
        self.assertIn(
            "COPY --from=whisperjav-builder /opt/venvs/whisperjav",
            runtime_base_dockerfile,
        )
        # WhisperJAV is vendored, so the image installs pinned dependencies
        # instead of cloning the upstream project.
        self.assertIn("requirements-whisperjav.txt", runtime_base_dockerfile)
        self.assertNotIn(
            "github.com/meizhong986/WhisperJAV",
            runtime_base_dockerfile,
        )
        self.assertNotIn("--extra qwen", runtime_base_dockerfile)
        whisperjav_requirements = (
            ROOT / "requirements-whisperjav.txt"
        ).read_text(encoding="utf-8")
        self.assertIn("ten-vad==1.0.6.8", whisperjav_requirements)
        self.assertIn("onnxruntime-gpu==1.23.2", whisperjav_requirements)
        for excluded in ("faster-whisper", "ctranslate2", "auditok"):
            self.assertNotIn(f"\n{excluded}==", whisperjav_requirements)
        self.assertIn("libc++1", runtime_base_dockerfile)
        self.assertIn("libc++abi1", runtime_base_dockerfile)
        self.assertIn("from ten_vad import TenVad", runtime_base_dockerfile)
        self.assertIn("PYTHONPATH=/opt/stt", runtime_dockerfile)
        self.assertIn(
            "WHISPERJAV_PYTHON=/opt/venvs/whisperjav/bin/python",
            runtime_dockerfile,
        )
        self.assertNotIn("requirements-kotoba.txt", runtime_dockerfile)
        self.assertIn("requirements-kotoba.txt", runtime_base_dockerfile)
        self.assertIn(
            "requirements-whisperx-cuda.txt",
            runtime_base_dockerfile,
        )
        self.assertIn(
            "nvidia-npp-cu12==12.3.3.100",
            (ROOT / "requirements-whisperx-cuda.in").read_text(
                encoding="utf-8"
            ),
        )
        self.assertIn("nvidia/npp/lib", runtime_base_dockerfile)
        self.assertIn(
            "STT_BASE_URL: ${STT_BASE_URL:-http://runtime:8100}",
            compose,
        )
        self.assertNotIn("container_name:", compose)
        self.assertIn(
            "BACKEND_STATE_DIR: /var/lib/stt", compose
        )
        self.assertIn("BACKEND_WORK_DIR: /var/lib/stt-work", compose)
        self.assertIn(
            "STT_STATE_DIR: /var/lib/stt", compose
        )
        self.assertIn("STT_WORK_DIR: /var/lib/stt-work", compose)
        self.assertEqual(compose.count("target: /var/lib/stt-work"), 2)
        self.assertNotIn("target: /var/lib/stt/jobs", compose)
        self.assertNotIn("target: /var/lib/stt/incoming", compose)
        self.assertEqual(compose.count("create_host_path: false"), 7)
        self.assertIn("target: /var/cache/stt", compose)
        self.assertEqual(compose.count("${STT_BASE_URL:-"), 1)
        self.assertEqual(compose.count("\n      STT_API_TOKEN:"), 2)
        self.assertIn("${BACKEND_PUID:-${WEB_PUID:-1026}}", compose)
        self.assertIn("${BACKEND_PGID:-${WEB_PGID:-100}}", compose)
        self.assertIn("${PUID:-1000}:${PGID:-1000}", compose)
        self.assertNotIn("${WEB_PORT", compose)
        self.assertIn('traefik.enable: "true"', compose)
        self.assertIn("${TRAEFIK_HOST:?TRAEFIK_HOST must be set}", compose)
        self.assertIn(
            "BACKEND_FORWARDED_ALLOW_IPS: "
            '"${BACKEND_FORWARDED_ALLOW_IPS:-*}"',
            compose,
        )
        self.assertNotIn("WEB_ADMIN_PASSWORD:", compose)
        self.assertNotIn("WEB_SESSION_SECRET:", compose)
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

        web_dockerfile = (ROOT / "Dockerfile.web").read_text(
            encoding="utf-8"
        )
        next_config = (ROOT / "web" / "next.config.ts").read_text(
            encoding="utf-8"
        )
        next_proxy = (ROOT / "web" / "src" / "proxy.ts").read_text(
            encoding="utf-8"
        )
        api_proxy_route = (
            ROOT / "web" / "src" / "app" / "api" / "v1" / "[...path]" / "route.ts"
        ).read_text(encoding="utf-8")

        def without_comments(source: str) -> str:
            """주석은 설명이지 설정이 아니다. 단정은 실제 코드에 대해서만 한다."""
            return "\n".join(
                line
                for line in source.splitlines()
                if not line.lstrip().startswith(("//", "*", "/*"))
            )

        next_config_code = without_comments(next_config)
        next_proxy_code = without_comments(next_proxy)
        # web 은 Next.js 정적/서버 렌더만 담당한다. 파이썬·미디어 처리는 없다.
        self.assertIn("FROM ${NODE_IMAGE}", web_dockerfile)
        self.assertNotIn("python", web_dockerfile.lower())
        self.assertNotIn("ffmpeg", web_dockerfile.lower())
        self.assertNotIn("jinja", web_dockerfile.lower())
        self.assertNotIn("nginx", web_dockerfile.lower())
        self.assertIn("standalone", web_dockerfile)
        # /api/v1 만 backend 로 넘긴다. web 이 API 를 자체 구현하지 않는다.
        # rewrites 는 쓰지 않는다 — next.config 값은 빌드 시점에 구워져서
        # 컨테이너 런타임의 BACKEND_ORIGIN 이 무시된다.
        self.assertNotIn("rewrites", next_config_code)
        self.assertNotIn("BACKEND_ORIGIN", next_config_code)
        self.assertIn("process.env.BACKEND_ORIGIN", api_proxy_route)
        # nginx 가 붙이던 보안 헤더는 Next proxy 로 옮겼다.
        self.assertIn("Content-Security-Policy", next_proxy)
        self.assertIn("X-Content-Type-Options", next_proxy)
        self.assertNotIn("unsafe-inline", next_proxy_code)
        self.assertFalse((ROOT / "deploy" / "nginx").exists())
        self.assertIn("requirements-backend.txt", backend_dockerfile)
        self.assertNotIn("requirements-web.txt", backend_dockerfile)
        self.assertFalse((ROOT / "requirements-web.txt").exists())
        backend_requirements = (ROOT / "requirements-backend.txt").read_text(
            encoding="utf-8"
        )
        self.assertNotIn("jinja2", backend_requirements.lower())
        self.assertNotIn("itsdangerous", backend_requirements.lower())
        backend_source = (
            ROOT / "src" / "stt_to_subtitle" / "backend_api.py"
        ).read_text(encoding="utf-8")
        self.assertNotIn("web_app", backend_source)
        self.assertFalse(
            (ROOT / "src" / "stt_to_subtitle" / "web_app.py").exists()
        )
        self.assertFalse(
            (ROOT / "src" / "stt_to_subtitle" / "templates").exists()
        )
        self.assertFalse(
            (ROOT / "src" / "stt_to_subtitle" / "static").exists()
        )
        self.assertIn(
            'ENTRYPOINT ["python", "-m", "stt_to_subtitle.backend_api"]',
            backend_dockerfile,
        )
        self.assertIn(
            'ENTRYPOINT ["python", "-m", "stt_to_subtitle.runtime_api"]',
            runtime_dockerfile,
        )
        self.assertIn("build_service_package.py", backend_dockerfile)
        self.assertIn("build_service_package.py", runtime_dockerfile)
        self.assertEqual(compose.count("cap_drop:"), 3)
        self.assertIn("condition: service_healthy", compose)

    def test_standalone_runtime_compose_publishes_only_the_runtime_api(self) -> None:
        compose = (ROOT / "compose.runtime.yaml").read_text(encoding="utf-8")
        example = (ROOT / ".env.runtime.example").read_text(encoding="utf-8")

        self.assertIn("dockerfile: Dockerfile.runtime", compose)
        self.assertIn("dockerfile: Dockerfile.stt-runtime", compose)
        self.assertIn("STT_RUNTIME_BIND_ADDRESS", compose)
        self.assertIn("STT_RUNTIME_ID", compose)
        self.assertIn("STT_RUNTIME_NAME", compose)
        self.assertIn("STT_API_TOKEN", compose)
        self.assertNotIn("OPENAI_COMPATIBLE", compose)
        self.assertNotIn("Dockerfile.web", compose)
        self.assertNotIn("Dockerfile.backend", compose)
        self.assertIn("STT_RUNTIME_ID=runtime-node-01", example)

    def test_compose_launcher_uses_the_calling_non_root_account(self) -> None:
        launcher = (ROOT / "scripts/compose.sh").read_text(encoding="utf-8")

        self.assertIn("runtime_uid=$(id -u)", launcher)
        self.assertIn("runtime_gid=$(id -g)", launcher)
        self.assertIn('if [ "$runtime_uid" -eq 0 ]', launcher)
        self.assertIn('export PUID="$runtime_uid"', launcher)
        self.assertIn('export PGID="$runtime_gid"', launcher)

    def test_backend_and_gpu_monitoring_compose_are_independent(self) -> None:
        compose = (ROOT / "compose.yaml").read_text(encoding="utf-8")
        observability = (ROOT / "gpu-observability/compose.yaml").read_text(
            encoding="utf-8"
        )

        web = compose.split("\n  web:\n    platform:", 1)[1].split(
            "\n  backend:\n    platform:", 1
        )[0]
        backend = compose.split("\n  backend:\n    platform:", 1)[1].split(
            "\n  runtime:\n    platform:", 1
        )[0]
        runtime = compose.split("\n  runtime:\n    platform:", 1)[1].split(
            "\nnetworks:\n", 1
        )[0]
        networks = compose.rsplit("\nnetworks:\n", 1)[1]
        self.assertNotIn("depends_on:", web)
        self.assertNotIn("depends_on:", backend)
        self.assertIn("host.docker.internal:host-gateway", backend)
        self.assertIn(
            "STT_BASE_URL: ${STT_BASE_URL:-http://runtime:8100}",
            backend,
        )
        self.assertIn("WEB_APP_IPV4", web)
        self.assertIn("RUNTIME_APP_IPV4", runtime)
        self.assertNotIn("BACKEND_RUNTIME_IPV4", backend)
        self.assertNotIn("RUNTIME_NETWORK_SUBNET", networks)
        self.assertNotIn("  runtime:\n", networks)
        self.assertFalse((ROOT / "compose.gpu-monitoring.yaml").exists())
        self.assertFalse((ROOT / "scripts/compose-gpu.sh").exists())
        self.assertIn(
            "${DOCKER_HOST_BIND_ADDRESS:-172.17.0.1}", observability
        )

    def test_runtime_requirements_have_no_platform_wrapper_files(self) -> None:
        self.assertTrue((ROOT / "requirements-api.txt").is_file())
        self.assertTrue((ROOT / "requirements-kotoba.txt").is_file())
        self.assertFalse((ROOT / "requirements-cuda.txt").exists())

    def test_job_detail_preserves_same_page_monocular_vr180_preview(self) -> None:
        job_detail = (
            ROOT / "web" / "src" / "app" / "jobs" / "[id]" / "page.tsx"
        ).read_text(encoding="utf-8")
        player_component = (
            ROOT / "web" / "src" / "components" / "ResultPlayer.tsx"
        ).read_text(encoding="utf-8")
        renderer = (ROOT / "web" / "public" / "vr180-player.js").read_text(
            encoding="utf-8"
        )
        proxy = (ROOT / "web" / "src" / "proxy.ts").read_text(
            encoding="utf-8"
        )

        self.assertIn("<ResultPlayer", job_detail)
        self.assertNotIn("href={`/vr?", job_detail)
        self.assertFalse(
            (ROOT / "web" / "src" / "app" / "vr" / "page.tsx").exists()
        )
        self.assertIn("180° 단안 미리보기", player_component)
        self.assertIn("video_u = u_eye_offset + eye_u * 0.5", renderer)
        self.assertIn("eyeOffset: 0,", renderer)
        self.assertIn(
            'eyeOffset: view.eye === "right" ? 0.5 : 0,',
            renderer,
        )
        self.assertIn("xr-spatial-tracking=(self)", proxy)

    def test_project_and_package_versions_are_5_0_0(self) -> None:
        project = tomllib.loads(
            (ROOT / "pyproject.toml").read_text(encoding="utf-8")
        )
        self.assertEqual(project["project"]["version"], "5.0.0")
        self.assertEqual(__version__, "5.0.0")


if __name__ == "__main__":
    unittest.main()
