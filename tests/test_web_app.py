import asyncio
from dataclasses import replace
from importlib.util import find_spec
import json
from pathlib import Path
import os
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import Mock, patch

from stt_to_subtitle import __version__

WEB_TESTS_AVAILABLE = all(
    find_spec(module) is not None
    for module in ("itsdangerous", "jinja2", "multipart")
)
if WEB_TESTS_AVAILABLE:
    from fastapi.testclient import TestClient

from stt_to_subtitle.web_config import WebSettings
from stt_to_subtitle.gpu_monitoring import GpuDevice, GpuSnapshot
from stt_to_subtitle.job_state import structured_state_from_legacy

if WEB_TESTS_AVAILABLE:
    from stt_to_subtitle.web_app import (
        JobChangeHook,
        create_app,
        main,
        webgpu_scene_context,
    )


@unittest.skipUnless(
    WEB_TESTS_AVAILABLE,
    "web test dependencies are not installed",
)
class JobChangeHookTests(unittest.IsolatedAsyncioTestCase):
    async def test_wakes_waiters_when_a_job_changes(self) -> None:
        hook = JobChangeHook(asyncio.get_running_loop())
        version = hook.version

        hook.publish("job-1")
        updated_version = await hook.wait(version, timeout=0.1)

        self.assertEqual(updated_version, version + 1)


class _StageJob:
    def __init__(self, **values: object) -> None:
        self.status = "queued"
        self.operation = "full"
        self.blocked_stage = None
        self.chunks_created = 0
        self.chunks_completed = 0
        self.chunks_total_estimate = 0
        self.translation_chunks_total = 0
        self.translation_chunks_completed = 0
        for key, value in values.items():
            setattr(self, key, value)
        projected = structured_state_from_legacy(
            status=str(self.status),
            operation=str(self.operation),
            blocked_stage=(
                str(self.blocked_stage) if self.blocked_stage else None
            ),
        )
        if "phase" not in values:
            self.phase = projected.phase.value
        if "state" not in values:
            self.state = projected.state.value


@unittest.skipUnless(
    WEB_TESTS_AVAILABLE,
    "web test dependencies are not installed",
)
class MediaActorLabelTests(unittest.TestCase):
    def label(self, actors: object) -> str:
        from stt_to_subtitle.web_app import media_actor_label

        return media_actor_label(actors)

    def test_single_actor_is_named(self) -> None:
        self.assertEqual(self.label(["모리 히나코"]), "모리 히나코")

    def test_multiple_actors_collapse_to_group(self) -> None:
        self.assertEqual(self.label(["미야시타 레나", "사토 아이"]), "Group")
        self.assertEqual(self.label(["가", "나", "다"]), "Group")

    def test_missing_or_blank_actors_read_unknown(self) -> None:
        self.assertEqual(self.label([]), "Unknown")
        self.assertEqual(self.label(None), "Unknown")
        self.assertEqual(self.label(["", "   "]), "Unknown")

    def test_blank_entries_do_not_trigger_the_group_label(self) -> None:
        self.assertEqual(self.label(["모리 히나코", "  "]), "모리 히나코")


@unittest.skipUnless(
    WEB_TESTS_AVAILABLE,
    "web test dependencies are not installed",
)
class JobStageViewTests(unittest.TestCase):
    def states(self, **values: object) -> list[tuple[str, str]]:
        from stt_to_subtitle.web_app import job_stage_view

        return [
            (stage["label"], stage["state"])
            for stage in job_stage_view(_StageJob(**values))
        ]

    def test_running_stage_marks_earlier_stages_done_and_later_pending(
        self,
    ) -> None:
        self.assertEqual(
            self.states(status="transcription_running"),
            [
                ("추출", "done"),
                ("전사", "running"),
                ("번역", "pending"),
                ("작업 완료", "pending"),
            ],
        )

    def test_blocked_job_marks_the_blocked_stage(self) -> None:
        self.assertEqual(
            self.states(status="blocked", blocked_stage="translation"),
            [
                ("추출", "done"),
                ("전사", "done"),
                ("번역", "blocked"),
                ("작업 완료", "pending"),
            ],
        )

    def test_operation_exposes_selected_phase_and_job_endpoint(self) -> None:
        from stt_to_subtitle.web_app import job_pipeline_phase_view

        self.assertEqual(
            self.states(status="transcription_completed", operation="transcribe"),
            [
                ("전사", "done"),
                ("작업 완료", "done"),
            ],
        )
        self.assertEqual(
            self.states(status="extracting", operation="transcribe"),
            [("전사", "waiting"), ("작업 완료", "pending")],
        )
        self.assertEqual(
            [
                (phase["label"], phase["state"])
                for phase in job_pipeline_phase_view(
                    _StageJob(status="extracting", operation="transcribe")
                )
            ],
            [
                ("추출", "running"),
                ("전사", "pending"),
                ("번역", "pending"),
            ],
        )
        self.assertEqual(
            self.states(status="queued", operation="translate"),
            [
                ("번역", "waiting"),
                ("작업 완료", "pending"),
            ],
        )
        self.assertEqual(
            self.states(status="queued", operation="extract"),
            [("추출", "waiting"), ("작업 완료", "pending")],
        )

    def test_job_endpoint_tracks_internal_completion_states(self) -> None:
        from stt_to_subtitle.web_app import job_progress_view

        self.assertEqual(
            self.states(status="translated", operation="translate"),
            [("번역", "done"), ("작업 완료", "waiting")],
        )
        self.assertEqual(
            self.states(status="rendering", operation="translate"),
            [("번역", "done"), ("작업 완료", "running")],
        )
        progress = job_progress_view(
            _StageJob(status="rendering", operation="translate")
        )
        self.assertEqual(
            [(phase["label"], phase["kind"]) for phase in progress["phases"]],
            [("번역", "phase")],
        )
        self.assertEqual(progress["endpoint"]["kind"], "endpoint")
        self.assertEqual(
            progress["endpoint"]["display_label"],
            "작업 마무리 중",
        )

    def test_chunk_counts_drive_the_stage_progress_percentage(self) -> None:
        from stt_to_subtitle.web_app import job_stage_view

        stages = job_stage_view(
            _StageJob(
                status="transcription_running",
                chunks_created=12,
                chunks_completed=3,
            )
        )
        transcription = next(s for s in stages if s["key"] == "transcription")
        self.assertEqual(transcription["percent"], 25)
        self.assertEqual(transcription["completed"], 3)
        self.assertEqual(transcription["total"], 12)

    def test_pipeline_progress_combines_stage_and_chunk_progress(self) -> None:
        from stt_to_subtitle.web_app import job_progress_view

        progress = job_progress_view(
            _StageJob(
                status="transcription_running",
                chunks_created=12,
                chunks_completed=7,
            )
        )

        self.assertEqual(progress["percent"], 40)
        self.assertEqual(progress["current"]["label"], "전사")
        self.assertFalse(progress["complete"])

    def test_estimated_transcription_total_is_used_until_actual_count_exceeds_it(
        self,
    ) -> None:
        from stt_to_subtitle.web_app import job_stage_view

        estimated = job_stage_view(
            _StageJob(
                status="transcription_running",
                chunks_created=3,
                chunks_completed=2,
                chunks_total_estimate=10,
            )
        )[1]
        corrected = job_stage_view(
            _StageJob(
                status="transcription_running",
                chunks_created=12,
                chunks_completed=8,
                chunks_total_estimate=10,
            )
        )[1]

        self.assertEqual(estimated["total"], 10)
        self.assertTrue(estimated["total_is_estimate"])
        self.assertEqual(corrected["total"], 12)
        self.assertFalse(corrected["total_is_estimate"])

    def test_running_stage_never_reports_one_hundred_percent(self) -> None:
        from stt_to_subtitle.web_app import job_progress_view

        progress = job_progress_view(
            _StageJob(
                status="translation_running",
                translation_chunks_total=4,
                translation_chunks_completed=4,
            )
        )

        translation = next(
            stage
            for stage in progress["stages"]
            if stage["key"] == "translation"
        )
        self.assertEqual(translation["percent"], 99)
        self.assertLess(progress["percent"], 100)

    def test_completed_job_reports_every_stage_done(self) -> None:
        self.assertEqual(
            [state for _, state in self.states(status="completed")],
            ["done", "done", "done", "done"],
        )

    def test_paused_translation_is_distinct_from_a_failure(self) -> None:
        self.assertEqual(
            self.states(status="translation_paused"),
            [
                ("추출", "done"),
                ("전사", "done"),
                ("번역", "paused"),
                ("작업 완료", "pending"),
            ],
        )


@unittest.skipUnless(
    WEB_TESTS_AVAILABLE,
    "web test dependencies are not installed",
)
class WebAppTests(unittest.TestCase):
    def settings(self, root: Path, media_root: Path) -> WebSettings:
        return WebSettings(
            state_dir=root / "state",
            media_root=media_root,
            admin_password="",
            session_secret="",
            stt_base_url="http://stt.test",
            stt_token="",
            lm_base_url="http://lm.test/v1",
            lm_token="",
            lm_model="model",
        )

    def test_job_pages_use_change_events_instead_of_timed_polling(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            media_root = root / "media"
            media_root.mkdir()

            with TestClient(create_app(self.settings(root, media_root))) as client:
                service = client.app.state.orchestrator
                service.stop()
                job = service.store.create(
                    job_id="active-job",
                    source_rel="active.mp4",
                    force_overwrite=False,
                    options={},
                )
                dashboard = client.get("/")
                detail = client.get(f"/jobs/{job.id}")
                updates = client.get("/static/live-updates.js")
                route_paths = {route.path for route in client.app.routes}

            self.assertNotIn("변경 즉시 갱신", dashboard.text)
            self.assertIn(
                'data-update-url="/job-stats-fragment"',
                dashboard.text,
            )
            self.assertIn('data-update-url="/jobs-fragment?', dashboard.text)
            self.assertIn(
                f'data-update-url="/jobs/{job.id}/panel"',
                detail.text,
            )
            self.assertNotIn("data-poll", dashboard.text + detail.text)
            self.assertNotIn("5초마다 갱신", dashboard.text)
            self.assertIn("/jobs/events", route_paths)
            self.assertIn("new window.EventSource", updates.text)
            self.assertNotIn("setInterval", updates.text)

    def test_status_tiles_link_to_filtered_job_pages(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            media_root = root / "media"
            media_root.mkdir()
            with TestClient(create_app(self.settings(root, media_root))) as client:
                service = client.app.state.orchestrator
                service.stop()
                running = service.store.create(
                    job_id="running-job",
                    source_rel="running.mkv",
                    force_overwrite=False,
                    options={},
                )
                service.store.update(
                    running.id,
                    status="transcription_running",
                )
                blocked = service.store.create(
                    job_id="blocked-job",
                    source_rel="blocked.mkv",
                    force_overwrite=False,
                    options={},
                )
                service.store.update(blocked.id, status="blocked")
                failed = service.store.create(
                    job_id="failed-job",
                    source_rel="failed.mkv",
                    force_overwrite=False,
                    options={},
                )
                service.store.update(failed.id, status="failed")
                stopped = service.store.create(
                    job_id="stopped-job",
                    source_rel="stopped.mkv",
                    force_overwrite=False,
                    options={},
                )
                service.store.update(
                    stopped.id,
                    status="blocked",
                    state="stopped",
                    reason_code="user_stop",
                    blocked_stage="transcription",
                    error="사용자 요청으로 작업이 중단되었습니다.",
                )

                dashboard = client.get("/")
                running_page = client.get("/jobs?status_group=running")
                blocked_page = client.get("/jobs?status_group=blocked")
                stopped_page = client.get("/jobs?status_group=stopped")
                stopped_transcription_page = client.get(
                    "/jobs?stage_filter=transcription&status_group=stopped"
                )
                failed_page = client.get("/jobs?status_group=failed")
                invalid_page = client.get("/jobs?status_group=unknown")

            self.assertIn('href="/jobs?status_group=running"', dashboard.text)
            self.assertIn('href="/jobs?status_group=blocked"', dashboard.text)
            self.assertIn('href="/jobs?status_group=failed"', dashboard.text)
            self.assertIn('href="/jobs?status_group=paused"', dashboard.text)
            self.assertIn('href="/jobs?status_group=stopped"', dashboard.text)
            self.assertIn('href="/jobs?status_group=waiting"', dashboard.text)
            self.assertIn('href="/jobs?status_group=completed"', dashboard.text)
            self.assertIn("running.mkv", running_page.text)
            self.assertNotIn("blocked.mkv", running_page.text)
            self.assertNotIn("failed.mkv", running_page.text)
            self.assertIn("blocked.mkv", blocked_page.text)
            self.assertNotIn("stopped.mkv", blocked_page.text)
            self.assertNotIn("failed.mkv", blocked_page.text)
            self.assertIn("failed.mkv", failed_page.text)
            self.assertNotIn("blocked.mkv", failed_page.text)
            self.assertIn("stopped.mkv", stopped_page.text)
            self.assertNotIn("blocked.mkv", stopped_page.text)
            self.assertIn("stopped.mkv", stopped_transcription_page.text)
            self.assertNotIn("blocked.mkv", stopped_transcription_page.text)
            self.assertIn(
                "전사 · 정지 작업",
                stopped_transcription_page.text,
            )
            self.assertIn(
                "data-update-url=\"/jobs-fragment?status_group=running",
                running_page.text,
            )
            self.assertEqual(invalid_page.status_code, 400)

    def test_job_list_uses_two_level_pipeline_stage_filters(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            media_root = root / "media"
            media_root.mkdir()
            with TestClient(create_app(self.settings(root, media_root))) as client:
                service = client.app.state.orchestrator
                service.stop()
                for job_id, status in (
                    ("extracting", "extracting"),
                    ("transcription-waiting", "audio_ready"),
                    ("transcription-running", "transcription_running"),
                    ("transcription-completed", "transcription_completed"),
                    ("translation-waiting", "transcribed"),
                    ("translation-running", "translation_running"),
                    ("translation-completed", "translated"),
                    ("rendering", "rendering"),
                    ("completed", "completed"),
                ):
                    job = service.store.create(
                        job_id=job_id,
                        source_rel=f"{job_id}.mkv",
                        force_overwrite=False,
                        options={},
                    )
                    service.store.update(job.id, status=status)
                page = client.get("/jobs")
                transcription = client.get(
                    "/jobs?stage_filter=transcription"
                )
                transcription_waiting = client.get(
                    "/jobs?stage_filter=transcription_waiting"
                )
                translation_completed = client.get(
                    "/jobs?stage_filter=translation_completed"
                )
                completed = client.get("/jobs?stage_filter=completed")
                invalid = client.get("/jobs?stage_filter=unknown")
                stage_counts = client.get(
                    "/job-stage-filters-fragment"
                    "?stage_filter=transcription"
                )
                service.store.update("extracting", status="audio_ready")
                refreshed_stage_counts = client.get(
                    "/job-stage-filters-fragment"
                    "?stage_filter=transcription"
                )

            self.assertEqual(page.status_code, 200)
            for stage_filter, label in (
                ("extraction", "추출"),
                ("transcription", "전사"),
                ("translation", "번역"),
                ("completed", "완료"),
            ):
                self.assertIn(
                    f'href="/jobs?stage_filter={stage_filter}"',
                    page.text,
                )
                self.assertIn(f"<span>{label}</span>", page.text)
            for stage_filter in (
                "transcription_waiting",
                "transcription_running",
                "transcription_completed",
                "translation_waiting",
                "translation_running",
                "translation_completed",
            ):
                self.assertIn(
                    f'href="/jobs?stage_filter={stage_filter}"',
                    page.text,
                )
            self.assertEqual(page.text.count("job-stage-filter-children"), 2)
            self.assertEqual(page.text.count("job-stage-filter-count"), 4)
            self.assertIn(
                'data-update-url="/job-stage-filters-fragment?',
                page.text,
            )
            for stage_filter, label, count in (
                ("extraction", "추출", 1),
                ("transcription", "전사", 3),
                ("translation", "번역", 3),
                ("completed", "완료", 2),
            ):
                self.assertRegex(
                    stage_counts.text,
                    rf'href="/jobs\?stage_filter={stage_filter}"[^>]*>'
                    rf'\s*<span>{label}</span>\s*'
                    rf'<span\s+class="job-stage-filter-count"\s+'
                    rf'aria-label="{count}건"\s*>\s*{count}</span>',
                )
            self.assertRegex(
                refreshed_stage_counts.text,
                r'href="/jobs\?stage_filter=extraction"[^>]*>\s*'
                r'<span>추출</span>\s*<span\s+'
                r'class="job-stage-filter-count"\s+aria-label="0건"\s*>'
                r'\s*0</span>',
            )
            self.assertRegex(
                refreshed_stage_counts.text,
                r'href="/jobs\?stage_filter=transcription"[^>]*>\s*'
                r'<span>전사</span>\s*<span\s+'
                r'class="job-stage-filter-count"\s+aria-label="4건"\s*>'
                r'\s*4</span>',
            )
            self.assertIn("transcription-waiting.mkv", transcription.text)
            self.assertIn("transcription-running.mkv", transcription.text)
            self.assertIn("transcription-completed.mkv", transcription.text)
            self.assertNotIn("translation-waiting.mkv", transcription.text)
            self.assertIn(
                "transcription-waiting.mkv",
                transcription_waiting.text,
            )
            self.assertNotIn(
                "transcription-running.mkv",
                transcription_waiting.text,
            )
            self.assertIn(
                "translation-completed.mkv",
                translation_completed.text,
            )
            self.assertNotIn(
                "translation-running.mkv",
                translation_completed.text,
            )
            self.assertNotIn("rendering.mkv", completed.text)
            self.assertIn("transcription-completed.mkv", completed.text)
            self.assertIn("completed.mkv", completed.text)
            self.assertIn(
                "stage_filter=completed&amp;jobs_page=1",
                completed.text,
            )
            self.assertEqual(invalid.status_code, 400)

    def test_comparison_only_transcriptions_stay_out_of_job_history(
        self,
    ) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            media_root = root / "media"
            media_root.mkdir()
            for filename in (
                "regular.mkv",
                "comparison-only.mkv",
                "translated-comparison.mkv",
            ):
                (media_root / filename).write_bytes(b"media")
            with TestClient(create_app(self.settings(root, media_root))) as client:
                service = client.app.state.orchestrator
                service.stop()
                regular = service.store.create(
                    job_id="regular-transcription",
                    source_rel="regular.mkv",
                    force_overwrite=False,
                    options={},
                    operation="transcribe",
                )
                service.store.update(
                    regular.id,
                    status="transcription_completed",
                )
                comparison = service.store.create(
                    job_id="comparison-only",
                    source_rel="comparison-only.mkv",
                    force_overwrite=False,
                    options={"comparison_id": "comparison-1"},
                    operation="transcribe",
                )
                service.store.update(
                    comparison.id,
                    status="transcription_completed",
                )
                translated = service.store.create(
                    job_id="translated-comparison",
                    source_rel="translated-comparison.mkv",
                    force_overwrite=True,
                    options={"comparison_id": "comparison-1"},
                    operation="full",
                )
                service.store.update(translated.id, status="completed")

                jobs = client.get("/jobs")
                transcription = client.get(
                    "/jobs?stage_filter=transcription_completed"
                )
                comparison_history = client.get(
                    "/comparisons/comparison-1"
                )

            self.assertNotIn("comparison-only.mkv", jobs.text)
            self.assertNotIn("comparison-only.mkv", transcription.text)
            self.assertIn("regular.mkv", transcription.text)
            self.assertEqual(
                transcription.text.count('class="recent-job-item'),
                1,
            )
            self.assertIn("translated-comparison.mkv", jobs.text)
            self.assertEqual(jobs.text.count('class="recent-job-item'), 2)
            self.assertIn("엔진 작업 2개", comparison_history.text)

    def test_primary_pages_separate_overview_media_and_job_history(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            media_root = root / "media"
            media_root.mkdir()
            (media_root / "movie.mp4").write_bytes(b"media")

            with TestClient(create_app(self.settings(root, media_root))) as client:
                dashboard = client.get("/")
                media = client.get("/media")
                jobs = client.get("/jobs")
                stylesheet = client.get("/static/app.css")

            self.assertIn("<h1>대시보드</h1>", dashboard.text)
            self.assertNotIn("파이프라인 상태와 최근 작업", dashboard.text)
            self.assertNotIn('class="media-board"', dashboard.text)
            self.assertIn('aria-current="page"', dashboard.text)
            self.assertIn('href="/media"', dashboard.text)
            self.assertIn("movie.mp4", media.text)
            self.assertIn('class="media-board"', media.text)
            self.assertNotIn("최근 작업", media.text)
            self.assertIn("<h1>전체 작업</h1>", jobs.text)
            self.assertNotIn("상태별 작업", jobs.text)
            self.assertIn('href="/jobs" class="is-active"', jobs.text)
            self.assertNotIn('class="topbar"', dashboard.text)
            self.assertIn("position: fixed", stylesheet.text)
            self.assertIn(
                "grid-template-columns: repeat(4, minmax(0, 1fr))",
                stylesheet.text,
            )

    def test_job_mutations_keep_the_exact_current_page(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            media_root = root / "media"
            media_root.mkdir()
            (media_root / "show").mkdir()
            (media_root / "show" / "movie.mp4").write_bytes(b"media")
            (media_root / "show" / "new.mp4").write_bytes(b"media")

            with TestClient(create_app(self.settings(root, media_root))) as client:
                service = client.app.state.orchestrator
                service.stop()
                waiting = service.store.create(
                    job_id="stay-put",
                    source_rel="show/movie.mp4",
                    force_overwrite=False,
                    options={},
                )
                service.store.update(waiting.id, status="transcribed")

                jobs_location = (
                    "/jobs?stage_filter=translation_waiting&jobs_page=4"
                )
                paused = client.post(
                    f"/jobs/{waiting.id}/pause-translation",
                    headers={"referer": f"http://testserver{jobs_location}"},
                    follow_redirects=False,
                )
                media_location = "/media?folder=show&q=movie"
                queued = client.post(
                    "/jobs",
                    data={
                        "source_rels": "show/new.mp4",
                        "operation": "transcribe",
                    },
                    headers={"referer": f"http://testserver{media_location}"},
                    follow_redirects=False,
                )

            self.assertEqual(paused.status_code, 303)
            self.assertEqual(paused.headers["location"], jobs_location)
            self.assertEqual(queued.status_code, 303)
            self.assertEqual(queued.headers["location"], media_location)

    def test_dashboard_renders_live_gpu_metrics_without_leaving_stt(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            media_root = root / "media"
            media_root.mkdir()
            snapshot = GpuSnapshot(
                configured=True,
                available=True,
                devices=(
                    GpuDevice(
                        id="GPU-test",
                        index="0",
                        model_name="NVIDIA Test GPU",
                        hostname="test-host",
                        utilization_percent=73,
                        memory_used_mib=8192,
                        memory_total_mib=16384,
                        temperature_celsius=67,
                        power_watts=214.5,
                    ),
                ),
            )

            with TestClient(create_app(self.settings(root, media_root))) as client:
                client.app.state.gpu_monitor = Mock(
                    snapshot=Mock(return_value=snapshot)
                )
                dashboard = client.get("/")
                fragment = client.get("/gpu-stats-fragment")
                script = client.get("/static/gpu-monitoring.js")

            self.assertEqual(dashboard.status_code, 200)
            self.assertEqual(fragment.status_code, 200)
            self.assertIn('data-gpu-update-url="/gpu-stats-fragment"', dashboard.text)
            self.assertIn("NVIDIA Test GPU", dashboard.text)
            self.assertIn("73", dashboard.text)
            self.assertIn("8.0 / 16.0 GiB", dashboard.text)
            self.assertNotIn("GPU_DASHBOARD_URL", dashboard.text)
            self.assertNotIn("grafana", dashboard.text.lower())
            self.assertIn("window.setTimeout", script.text)

    def test_webgpu_dashboard_uses_live_jobs_and_vendored_renderer(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            media_root = root / "media"
            (media_root / "Shows").mkdir(parents=True)
            snapshot = GpuSnapshot(
                configured=True,
                available=True,
                devices=(
                    GpuDevice(
                        id="GPU-test",
                        index="0",
                        model_name="NVIDIA Test GPU",
                        hostname="test-host",
                        utilization_percent=73,
                        memory_used_mib=8192,
                        memory_total_mib=16384,
                        temperature_celsius=67,
                        power_watts=214.5,
                    ),
                ),
            )

            with TestClient(create_app(self.settings(root, media_root))) as client:
                service = client.app.state.orchestrator
                service.stop()
                active = service.store.create(
                    job_id="active-job",
                    source_rel="active.mkv",
                    force_overwrite=False,
                    options={"backend": "whisperx"},
                )
                service.store.update(
                    active.id,
                    status="transcription_running",
                    chunks_created=10,
                    chunks_completed=4,
                )
                queued = service.store.create(
                    job_id="queued-job",
                    source_rel="queued.mkv",
                    force_overwrite=False,
                    options={},
                )
                failed = service.store.create(
                    job_id="failed-job",
                    source_rel="failed.mkv",
                    force_overwrite=False,
                    options={},
                )
                service.store.update(
                    failed.id,
                    status="failed",
                    blocked_stage="transcription",
                    error="worker unavailable",
                )
                blocked = service.store.create(
                    job_id="blocked-job",
                    source_rel="blocked.mkv",
                    force_overwrite=False,
                    options={},
                )
                service.store.update(
                    blocked.id,
                    status="blocked",
                    blocked_stage="translation",
                    error="temporary upstream interruption",
                )
                stopped = service.store.create(
                    job_id="stopped-job",
                    source_rel="stopped.mkv",
                    force_overwrite=False,
                    options={},
                )
                service.store.update(
                    stopped.id,
                    status="blocked",
                    state="stopped",
                    reason_code="user_stop",
                    blocked_stage="transcription",
                    error="사용자 요청으로 작업이 중단되었습니다.",
                )
                paused = service.store.create(
                    job_id="paused-job",
                    source_rel="paused.mkv",
                    force_overwrite=False,
                    options={},
                )
                service.store.update(
                    paused.id,
                    status="translation_paused",
                )
                completed = service.store.create(
                    job_id="completed-job",
                    source_rel="completed.mkv",
                    force_overwrite=False,
                    options={},
                )
                service.store.update(completed.id, status="completed")
                rendering = service.store.create(
                    job_id="rendering-job",
                    source_rel="rendering.mkv",
                    force_overwrite=False,
                    options={},
                )
                service.store.update(rendering.id, status="rendering")
                scene = webgpu_scene_context(
                    service,
                    snapshot,
                    audio_workers=1,
                )
                settled_at = max(
                    job.status_updated_at
                    for job in service.store.list_jobs(limit=None)
                ) + 31
                with patch(
                    "stt_to_subtitle.web_app.time.time",
                    return_value=settled_at,
                ):
                    settled_scene = webgpu_scene_context(
                        service,
                        snapshot,
                        audio_workers=1,
                    )
                empty_gpu_page = client.get("/?view=3d")
                client.app.state.gpu_monitor = Mock(
                    snapshot=Mock(return_value=snapshot)
                )

                page = client.get("/")
                dashboard = client.get("/?view=2d")
                persisted_dashboard = client.get("/")
                pipeline_fragment = client.get(
                    "/jobs-fragment?dashboard_section=pipeline"
                )
                work_fragment = client.get(
                    "/jobs-fragment?dashboard_section=work"
                )
                side_fragment = client.get(
                    "/jobs-fragment?dashboard_section=side"
                )
                invalid_dashboard_fragment = client.get(
                    "/jobs-fragment?dashboard_section=unknown"
                )
                renderer = client.get(
                    "/static/vendor/three.webgpu.min.js"
                )
                renderer_core = client.get(
                    "/static/vendor/three.core.min.js"
                )
                stylesheet = client.get("/static/webgpu.css")
                app_stylesheet = client.get("/static/app.css")

            self.assertEqual(page.status_code, 200)
            self.assertEqual(empty_gpu_page.status_code, 200)
            self.assertIn("GPU 메트릭 없음", empty_gpu_page.text)
            self.assertIn('<script type="importmap">', page.text)
            self.assertIn("NVIDIA Test GPU", page.text)
            self.assertIn("active.mkv", page.text)
            self.assertIn("queued.mkv", page.text)
            self.assertIn("failed.mkv", page.text)
            self.assertIn("blocked.mkv", page.text)
            self.assertIn("stopped.mkv", page.text)
            self.assertIn("paused.mkv", page.text)
            self.assertIn("completed.mkv", page.text)
            self.assertIn("rendering.mkv", page.text)
            self.assertIn("Shows", page.text)
            self.assertEqual(
                {
                    job["source_rel"]: [
                        (phase["label"], phase["state"])
                        for phase in job["phases"]
                    ]
                    for job in scene["phase_jobs"]
                },
                {
                    "active.mkv": [
                        ("추출", "done"),
                        ("전사", "running"),
                        ("번역", "pending"),
                    ],
                    "rendering.mkv": [
                        ("추출", "done"),
                        ("전사", "done"),
                        ("번역", "done"),
                    ],
                },
            )
            self.assertEqual(
                {
                    job["source_rel"]: (
                        job["endpoint"]["display_label"],
                        job["endpoint"]["state"],
                    )
                    for job in scene["phase_jobs"]
                },
                {
                    "active.mkv": ("작업 완료 대기", "pending"),
                    "rendering.mkv": ("작업 마무리 중", "running"),
                },
            )
            self.assertEqual(scene["blocked"][0]["source_rel"], "blocked.mkv")
            self.assertEqual(scene["stopped"][0]["source_rel"], "stopped.mkv")
            self.assertEqual(scene["paused"][0]["source_rel"], "paused.mkv")
            self.assertEqual(scene["failed"][0]["source_rel"], "failed.mkv")
            self.assertNotIn("error", scene["blocked"][0])
            self.assertNotIn("error", scene["stopped"][0])
            self.assertNotIn("error", scene["paused"][0])
            self.assertEqual(
                scene["failed"][0]["error"],
                "worker unavailable",
            )
            transport_active = sum(
                bool(job["transport_active"])
                for state in ("paused", "blocked", "stopped", "failed")
                for job in scene[state]
            )
            self.assertEqual(
                scene["workers"]["transporting"],
                transport_active,
            )
            self.assertEqual(
                scene["workers"]["total"],
                scene["workers"]["active"]
                + scene["workers"]["transporting"]
                + scene["workers"]["idle"],
            )
            self.assertTrue(
                all(
                    not job["transport_active"]
                    for state in ("paused", "blocked", "stopped", "failed")
                    for job in settled_scene[state]
                )
            )
            self.assertEqual(settled_scene["workers"]["transporting"], 0)
            self.assertEqual(
                scene["state_counts"],
                {
                    "running": 2,
                    "waiting": 1,
                    "paused": 1,
                    "blocked": 1,
                    "stopped": 1,
                    "failed": 1,
                    "completed": 1,
                },
            )
            self.assertEqual(
                [item["title"] for item in scene["queue"]],
                ["queued.mkv"],
            )
            self.assertIn('id="phase-states"', page.text)
            self.assertNotIn("/media/Shows", page.text)
            self.assertIn('data-layer="blocked"', page.text)
            self.assertIn('data-layer="paused"', page.text)
            self.assertIn('data-layer="stopped"', page.text)
            self.assertIn('data-layer="failed"', page.text)
            self.assertIn("커피 한 잔?", page.text)
            self.assertIn("DRINK", page.text)
            self.assertIn("BREAK", page.text)
            self.assertIn("const LOUNGE_ROUTES = [", page.text)
            self.assertIn("function updateLoungeWorkers(t)", page.text)
            self.assertIn("updateLoungeWorkers(t);", page.text)
            self.assertIn("chatter.el.hidden = true", page.text)
            self.assertIn("function phaseStation(", page.text)
            self.assertIn("function vendingMachine(", page.text)
            self.assertIn("function arcadeMachine(", page.text)
            self.assertIn("function transportCart(", page.text)
            self.assertIn("function statusBay(", page.text)
            self.assertIn("updateStatusTransports(t);", page.text)
            self.assertIn("const handler = person(", page.text)
            self.assertIn("job.transport_active", page.text)
            self.assertIn("function conveyorLine(", page.text)
            self.assertIn("function packingStation(", page.text)
            self.assertIn("완제품 보관동", page.text)
            self.assertIn("ARCHIVE_FLOOR_CAPACITY", page.text)
            self.assertIn("function serverCabinet(", page.text)
            self.assertIn('touch-action: none', page.text)
            self.assertIn('mode: e.button === 2 || e.shiftKey ? "orbit" : "pan"', page.text)
            self.assertIn("renderer.setAnimationLoop(loop);", page.text)
            self.assertNotIn('id="fps"', page.text)
            self.assertNotIn("requestAnimationFrame(loop)", page.text)
            self.assertNotIn("최근 3건 표시", page.text)
            self.assertNotIn("temporary upstream interruption", page.text)
            self.assertNotIn(
                "사용자 요청으로 작업이 중단되었습니다.",
                page.text,
            )
            self.assertIn("worker unavailable", page.text)
            self.assertIn('href="/?view=2d"', page.text)
            self.assertIn('window.location.replace("/?view=2d")', page.text)
            self.assertIn(
                "(hover: none) and (pointer: coarse)",
                page.text,
            )
            self.assertIn('href="/media">미디어 선택</a>', page.text)
            self.assertIn("location.assign(o.userData.href)", page.text)
            self.assertNotIn("/option-B", page.text)
            self.assertNotIn("상주 모델 정보", page.text)
            self.assertNotIn("Prometheus 연결을 설정하면", page.text)
            self.assertIn('href="/?view=3d"', dashboard.text)
            self.assertIn("desktop-3d-only", dashboard.text)
            self.assertIn("<h1>대시보드</h1>", persisted_dashboard.text)
            self.assertIn(
                'class="dashboard-option-b"',
                persisted_dashboard.text,
            )
            self.assertIn("일시 정지 1", persisted_dashboard.text)
            self.assertIn("중단 1", persisted_dashboard.text)
            self.assertIn("정지 1", persisted_dashboard.text)
            self.assertIn("실패 1", persisted_dashboard.text)
            self.assertIn("최근 완료", persisted_dashboard.text)
            self.assertNotIn("남은 시간", persisted_dashboard.text)
            self.assertEqual(pipeline_fragment.status_code, 200)
            self.assertIn("파이프라인", pipeline_fragment.text)
            self.assertEqual(work_fragment.status_code, 200)
            self.assertIn("실행 중", work_fragment.text)
            self.assertIn("멈춤", work_fragment.text)
            self.assertEqual(side_fragment.status_code, 200)
            self.assertIn("대기 큐", side_fragment.text)
            self.assertEqual(invalid_dashboard_fragment.status_code, 400)
            self.assertNotIn(
                "/webgpu",
                {route.path for route in client.app.routes},
            )
            self.assertEqual(renderer.status_code, 200)
            self.assertIn(b"Three.js Authors", renderer.content[:200])
            self.assertEqual(renderer_core.status_code, 200)
            self.assertEqual(stylesheet.status_code, 200)
            self.assertIn("--accent", stylesheet.text)
            self.assertEqual(app_stylesheet.status_code, 200)
            self.assertIn(".desktop-3d-only", app_stylesheet.text)
            self.assertIn(
                "(hover: none) and (pointer: coarse)",
                app_stylesheet.text,
            )

    def test_webgpu_workers_rest_when_no_jobs_exist(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            media_root = root / "media"
            media_root.mkdir()
            snapshot = GpuSnapshot(configured=False, available=False)

            with TestClient(create_app(self.settings(root, media_root))) as client:
                service = client.app.state.orchestrator
                service.stop()
                scene = webgpu_scene_context(
                    service,
                    snapshot,
                    audio_workers=1,
                )
                page = client.get("/?view=3d")
                extract_job = service.store.create(
                    job_id="extract-only",
                    source_rel="extract-only.mkv",
                    force_overwrite=False,
                    options={},
                    operation="extract",
                )
                extract_scene = webgpu_scene_context(
                    service,
                    snapshot,
                    audio_workers=1,
                )
                service.store.update(
                    extract_job.id,
                    status="audio_completed",
                )
                completed_extract_scene = webgpu_scene_context(
                    service,
                    snapshot,
                    audio_workers=1,
                )

            self.assertEqual(
                scene["workers"],
                {
                    "total": 3,
                    "active": 0,
                    "transporting": 0,
                    "idle": 3,
                },
            )
            self.assertEqual(scene["phase_jobs"], [])
            self.assertEqual(
                [item["title"] for item in extract_scene["queue"]],
                ["extract-only.mkv"],
            )
            self.assertEqual(extract_scene["phase_jobs"], [])
            self.assertEqual(completed_extract_scene["phase_jobs"], [])
            self.assertEqual(completed_extract_scene["completed_total"], 1)
            self.assertEqual(page.status_code, 200)
            self.assertIn("휴게소", page.text)
            self.assertNotIn('id="phase-states"', page.text)
            self.assertNotIn("ROUTE_START", page.text)
            self.assertNotIn("dropQueue", page.text)

    def test_media_page_renders_media_cards_and_local_poster(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            media_root = root / "media"
            show = media_root / "show"
            show.mkdir(parents=True)
            (show / "episode.mkv").write_bytes(b"media")
            (show / "episode.ko.srt").write_text("subtitle", encoding="utf-8")
            (show / "episode.nfo").write_text(
                """
                <episodedetails>
                  <title>첫 번째 에피소드</title>
                  <thumb aspect="poster">poster.jpg</thumb>
                </episodedetails>
                """,
                encoding="utf-8",
            )
            (show / "poster.jpg").write_bytes(b"poster-bytes")
            (media_root / "plain.mp4").write_bytes(b"media")

            with patch(
                "stt_to_subtitle.orchestrator.probe_media_duration",
                return_value=6180.0,
            ), TestClient(
                create_app(self.settings(root, media_root))
            ) as client:
                root_response = client.get("/media")
                response = client.get("/media?folder=show")
                poster = client.get("/media/posters/show/poster.jpg")

            self.assertEqual(root_response.status_code, 200)
            self.assertIn('class="folder-card media-card', root_response.text)
            self.assertIn("data-folder-link", root_response.text)
            self.assertRegex(
                root_response.text,
                r'name="folder_rels"\s+type="checkbox"\s+'
                r'value="show"\s+data-auto-select',
            )
            self.assertIn("폴더 전체 선택", root_response.text)
            self.assertIn("하위 폴더까지 포함", root_response.text)
            self.assertIn("data-folder-loading", root_response.text)
            self.assertIn("data-media-loading", root_response.text)
            self.assertIn("data-media-search", root_response.text)
            self.assertIn("data-loading-message", root_response.text)
            self.assertIn("folder-browser.js", root_response.text)
            self.assertIn("show", root_response.text)
            self.assertIn("plain.mp4", root_response.text)
            self.assertIn('class="video-placeholder"', root_response.text)
            self.assertIn("0.00 GiB", root_response.text)
            self.assertIn("재생시간 1:43:00", root_response.text)
            self.assertNotIn('class="media-directory"', response.text)
            self.assertIn("미처리", root_response.text)
            self.assertNotIn("folder-glyph", root_response.text)
            self.assertEqual(response.status_code, 200)
            self.assertIn('name="source_rels"', response.text)
            self.assertNotIn("data-auto-select", response.text)
            self.assertIn('name="return_folder" value="show"', response.text)
            self.assertIn("첫 번째 에피소드", response.text)
            self.assertIn("한국어 자막 있음", response.text)
            self.assertIn("일본어 구두점 모델 사용", response.text)
            self.assertIn("소음 오인식 필터 사용", response.text)
            self.assertIn(
                '<select name="backend" data-transcription-backend>',
                response.text,
            )
            self.assertIn(
                '<option value="auto" selected>', response.text
            )
            self.assertNotIn('name="chunk_length_seconds"', response.text)
            self.assertIn(
                'name="kotoba_chunk_length_seconds"', response.text
            )
            self.assertIn(
                'name="whisperx_chunk_length_seconds"', response.text
            )
            self.assertNotIn("단독 엔진 청크", response.text)
            self.assertNotIn("하이브리드 Kotoba 청크", response.text)
            self.assertNotIn("하이브리드 WhisperX 청크", response.text)
            self.assertIn("Kotoba 청크(초)", response.text)
            self.assertIn("WhisperX 청크(초)", response.text)
            self.assertIn(
                'name="noise_filter" type="checkbox" value="true" checked',
                response.text,
            )
            self.assertEqual(poster.status_code, 200)
            self.assertEqual(poster.content, b"poster-bytes")

    def test_actor_folder_preview_and_option_b_progress_use_actor_image(
        self,
    ) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            media_root = root / "media"
            actor = media_root / "AV" / "japan" / "Actor"
            title = actor / "TITLE-001"
            variety = media_root / "Variety" / "Show"
            (actor / ".actors").mkdir(parents=True)
            title.mkdir()
            variety.mkdir(parents=True)
            profile = actor / ".actors" / "Actor.jpg"
            profile.write_bytes(b"actor-profile")
            for name in (
                "done.mp4",
                "running.mp4",
                "blocked.mp4",
                "queued.mp4",
                "unprocessed.mp4",
            ):
                (title / name).write_bytes(b"media")
            (title / "done.ko.srt").write_text("subtitle", encoding="utf-8")
            (variety / "done.mp4").write_bytes(b"media")
            (variety / "done.ko.srt").write_text("subtitle", encoding="utf-8")
            (variety / "unprocessed.mp4").write_bytes(b"media")

            with TestClient(
                create_app(self.settings(root, media_root))
            ) as client:
                service = client.app.state.orchestrator
                service.stop()
                running = service.store.create(
                    job_id="actor-running",
                    source_rel="AV/japan/Actor/TITLE-001/running.mp4",
                    force_overwrite=False,
                    options={},
                )
                service.store.update(
                    running.id,
                    status="transcription_running",
                )
                blocked = service.store.create(
                    job_id="actor-blocked",
                    source_rel="AV/japan/Actor/TITLE-001/blocked.mp4",
                    force_overwrite=False,
                    options={},
                )
                service.store.update(blocked.id, status="blocked")
                service.store.create(
                    job_id="actor-queued",
                    source_rel="AV/japan/Actor/TITLE-001/queued.mp4",
                    force_overwrite=False,
                    options={},
                )

                media_page = client.get("/media?folder=AV/japan")
                dashboard = client.get("/")
                actor_image = client.get(
                    "/media/actors/AV/japan/Actor/.actors/Actor.jpg"
                )
                invalid_image = client.get(
                    "/media/actors/AV/japan/Actor/TITLE-001/done.mp4"
                )

            self.assertEqual(media_page.status_code, 200)
            self.assertIn('class="folder-visual actor-folder-preview"', media_page.text)
            self.assertIn(
                "/media/actors/AV/japan/Actor/.actors/Actor.jpg",
                media_page.text,
            )
            self.assertEqual(dashboard.status_code, 200)
            self.assertIn("라이브러리 진척", dashboard.text)
            self.assertIn("현재 작업 우선 · 배우 최대 5명", dashboard.text)
            self.assertIn("버라이어티", dashboard.text)
            self.assertIn("1 / 5", dashboard.text)
            self.assertIn('class="is-done" style="width: 20.0%"', dashboard.text)
            self.assertIn('class="is-running" style="width: 20.0%"', dashboard.text)
            self.assertIn('class="is-queued" style="width: 20.0%"', dashboard.text)
            self.assertIn('class="is-attention" style="width: 20.0%"', dashboard.text)
            self.assertIn('class="is-unprocessed" style="width: 20.0%"', dashboard.text)
            self.assertIn("미처리", dashboard.text)
            self.assertEqual(actor_image.status_code, 200)
            self.assertEqual(actor_image.content, b"actor-profile")
            self.assertEqual(invalid_image.status_code, 404)

    def test_media_page_searches_nested_display_titles_and_preserves_query(
        self,
    ) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            media_root = root / "media"
            show = media_root / "show"
            show.mkdir(parents=True)
            episode = show / "episode-01.mkv"
            episode.write_bytes(b"media")
            episode.with_suffix(".nfo").write_text(
                "<episodedetails><title>첫 번째 에피소드</title>"
                "<actor><name>미야시타 레나</name></actor>"
                "</episodedetails>",
                encoding="utf-8",
            )
            (show / "second.mkv").write_bytes(b"media")
            (show / "second.nfo").write_text(
                "<movie><actor><name>사토 아이</name></actor></movie>",
                encoding="utf-8",
            )

            with patch(
                "stt_to_subtitle.orchestrator.probe_media_duration",
                return_value=60.0,
            ), TestClient(
                create_app(self.settings(root, media_root))
            ) as client:
                client.app.state.orchestrator.stop()
                response = client.get("/media?q=첫 번째")
                actor_response = client.get(
                    "/media?actor=미야시타 레나"
                )
                queued = client.post(
                    "/jobs",
                    data={
                        "source_rels": "show/episode-01.mkv",
                        "return_query": "첫 번째",
                        "return_actor": "미야시타 레나",
                        "operation": "transcribe",
                    },
                    follow_redirects=False,
                )
                app_version = client.app.version

            self.assertEqual(response.status_code, 200)
            self.assertIn('name="q"', response.text)
            self.assertIn('value="첫 번째"', response.text)
            self.assertIn('name="actor"', actor_response.text)
            self.assertIn('value="미야시타 레나"', actor_response.text)
            self.assertIn("첫 번째 에피소드", response.text)
            self.assertIn("첫 번째 에피소드", actor_response.text)
            self.assertNotIn("second.mkv", actor_response.text)
            self.assertIn("show/episode-01.mkv", response.text)
            self.assertNotIn("second.mkv", response.text)
            self.assertIn(
                'name="return_query" value="첫 번째"',
                response.text,
            )
            self.assertIn(
                'name="return_actor" value="미야시타 레나"',
                actor_response.text,
            )
            self.assertIn("필터 결과 1개", response.text)
            self.assertIn("배우 “미야시타 레나”", actor_response.text)
            self.assertEqual(app_version, __version__)
            self.assertEqual(queued.status_code, 303)
            self.assertEqual(
                queued.headers["location"],
                "/media?queued=1&q=%EC%B2%AB+%EB%B2%88%EC%A7%B8"
                "&actor=%EB%AF%B8%EC%95%BC%EC%8B%9C%ED%83%80+%EB%A0%88%EB%82%98",
            )

    def test_media_page_lifts_files_out_of_shortened_content_folder(
        self,
    ) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            media_root = root / "media"
            actress = media_root / "AV" / "japan" / "배우"
            content = actress / "ABC-001"
            content.mkdir(parents=True)
            (content / "ABC-001.mp4").write_bytes(b"media")

            with TestClient(
                create_app(self.settings(root, media_root))
            ) as client:
                client.app.state.orchestrator.stop()
                page = client.get(
                    "/media?folder=AV%2Fjapan%2F%EB%B0%B0%EC%9A%B0"
                )
                queued = client.post(
                    "/jobs",
                    data={
                        "source_rels": (
                            "AV/japan/배우/ABC-001/ABC-001.mp4"
                        ),
                        "operation": "extract",
                    },
                    follow_redirects=False,
                )
                job = client.app.state.orchestrator.store.list_jobs(
                    limit=None
                )[0]

            self.assertEqual(page.status_code, 200)
            self.assertIn("ABC-001.mp4", page.text)
            self.assertNotIn(
                '<strong class="folder-name">ABC-001</strong>',
                page.text,
            )
            self.assertIn(
                'value="AV/japan/배우/ABC-001/ABC-001.mp4"',
                page.text,
            )
            self.assertEqual(queued.status_code, 303)
            self.assertEqual(
                job.source_rel,
                "AV/japan/배우/ABC-001/ABC-001.mp4",
            )

    def test_updates_remote_servers_from_settings_page(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            media_root = root / "media"
            media_root.mkdir()

            with TestClient(
                create_app(self.settings(root, media_root))
            ) as client:
                page = client.get("/settings")
                response = client.post(
                    "/settings",
                    data={
                        "stt_base_url": "http://new-stt.test:8100/",
                        "stt_token": "new-stt-token",
                        "lm_base_url": "http://new-lm.test:1234/v1/",
                        "lm_token": "new-lm-token",
                        "lm_model": "new-model",
                        "translation_workers": "3",
                    },
                    follow_redirects=False,
                )
                service = client.app.state.orchestrator
                saved_page = client.get("/settings?saved=true")

            self.assertEqual(page.status_code, 200)
            self.assertIn("서버 설정", page.text)
            self.assertIn("Docker를", page.text)
            self.assertNotIn("stt-token", page.text)
            self.assertEqual(response.status_code, 303)
            self.assertEqual(
                response.headers["location"],
                "/settings?saved=true",
            )
            self.assertEqual(
                service.stt_client.base_url,
                "http://new-stt.test:8100",
            )
            self.assertEqual(service.stt_client.token, "new-stt-token")
            self.assertEqual(service.lm_client.model, "new-model")
            self.assertEqual(service.remote_servers.translation_workers, 3)
            self.assertIn("서버 설정을 저장했습니다.", saved_page.text)
            self.assertNotIn("new-stt-token", saved_page.text)
            self.assertNotIn("new-lm-token", saved_page.text)

            with TestClient(
                create_app(self.settings(root, media_root))
            ) as client:
                reloaded = client.app.state.orchestrator
                reloaded_page = client.get("/settings")
                self.assertEqual(reloaded.remote_servers.translation_workers, 3)
                self.assertIn('value="3"', reloaded_page.text)

            self.assertEqual(
                reloaded.stt_client.base_url,
                "http://new-stt.test:8100",
            )
            self.assertEqual(reloaded.lm_client.model, "new-model")
            self.assertIn("http://new-stt.test:8100", reloaded_page.text)

    def test_saves_separate_commercial_subtitle_validator_settings(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            media_root = root / "media"
            media_root.mkdir()

            with TestClient(create_app(self.settings(root, media_root))) as client:
                page = client.get("/settings")
                saved = client.post(
                    "/settings/subtitle-validator",
                    data={
                        "validator_base_url": "https://validator.test/v1/",
                        "validator_token": "paid-secret",
                        "validator_model": "paid-model",
                    },
                    follow_redirects=False,
                )
                service = client.app.state.orchestrator
                refreshed = client.get("/settings?validator_saved=true")

            self.assertIn("상용 LLM 자막 검증", page.text)
            self.assertEqual(saved.status_code, 303)
            self.assertEqual(
                service.subtitle_validator_view(),
                {
                    "base_url": "https://validator.test/v1",
                    "token_configured": True,
                    "model": "paid-model",
                    "configured": True,
                },
            )
            self.assertIn("상용 LLM 검증 설정을 저장했습니다.", refreshed.text)
            self.assertNotIn("paid-secret", refreshed.text)

    def test_translation_manual_gate_starts_and_stops_explicitly(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            media_root = root / "media"
            media_root.mkdir()
            settings = replace(
                self.settings(root, media_root),
                lm_manual_start=True,
            )

            with patch(
                "stt_to_subtitle.orchestrator.list_openai_compatible_models",
                return_value=["model"],
            ) as models, TestClient(create_app(settings)) as client:
                page = client.get("/settings")
                started = client.post(
                    "/settings/translation/start",
                    follow_redirects=False,
                )
                service = client.app.state.orchestrator
                ready_state = service.lm_gate_state
                stopped = client.post(
                    "/settings/translation/stop",
                    follow_redirects=False,
                )
                persisted_gate = service.store.get_dependency_state(
                    "translation_lm"
                )

            self.assertIn("번역 시작/재개", page.text)
            self.assertEqual(started.status_code, 303)
            self.assertEqual(ready_state, "ready")
            self.assertEqual(stopped.status_code, 303)
            self.assertEqual(service.lm_gate_state, "offline")
            self.assertEqual(persisted_gate["state"], "offline")
            self.assertEqual(persisted_gate["reason_code"], "manual_stop")
            models.assert_called_once_with(
                "http://lm.test/v1",
                "",
                attempts=1,
            )

    def test_transcription_gate_requires_explicit_readiness_after_loss(
        self,
    ) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            media_root = root / "media"
            media_root.mkdir()

            with TestClient(
                create_app(self.settings(root, media_root))
            ) as client:
                service = client.app.state.orchestrator
                service._set_stt_gate(
                    "lost",
                    "connection refused",
                    reason_code="stt_unavailable",
                )
                page = client.get("/settings")
                service.stt_client.check_readiness = Mock(
                    return_value={"status": "ready"}
                )
                started = client.post(
                    "/settings/transcription/start",
                    follow_redirects=False,
                )

            self.assertIn("전사 연결 확인/재개", page.text)
            self.assertEqual(started.status_code, 303)
            self.assertEqual(
                started.headers["location"],
                "/settings?stt_started=true&stt_resumed=0",
            )
            self.assertEqual(service.stt_gate_state, "ready")
            service.stt_client.check_readiness.assert_called_once_with()

    def test_manages_path_display_rules_and_shortens_job_paths(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            media_root = root / "media"
            media_root.mkdir()

            with TestClient(
                create_app(self.settings(root, media_root))
            ) as client:
                service = client.app.state.orchestrator
                service.stop()
                service.store.create(
                    job_id="nested-job",
                    source_rel="av/japan/배우/ABC-001/ABC-001.mp4",
                    force_overwrite=False,
                    options={},
                )
                default_rule = service.path_display_rules[0]
                settings_page = client.get("/settings")
                shortened = client.get("/")
                updated = client.post(
                    f"/settings/path-display-rules/{default_rule.id}",
                    data={
                        "source_pattern": (
                            "av/{country}/{actress}/{content_id}/{filename}"
                        ),
                        "display_pattern": "{country}/{actress}/{filename}",
                    },
                    follow_redirects=False,
                )
                updated_dashboard = client.get("/")
                deleted = client.post(
                    f"/settings/path-display-rules/{default_rule.id}/delete",
                    follow_redirects=False,
                )
                unshortened = client.get("/")
                created = client.post(
                    "/settings/path-display-rules",
                    data={
                        "source_pattern": "{actress}/{content_id}/{filename}",
                        "display_pattern": "{actress}/{filename}",
                    },
                    follow_redirects=False,
                )

            self.assertIn("경로 표시 규칙", settings_page.text)
            self.assertIn(default_rule.source_pattern, settings_page.text)
            self.assertIn(
                'title="av/japan/배우/ABC-001.mp4"',
                shortened.text,
            )
            self.assertIn(
                '>av/japan/배우</span>',
                shortened.text,
            )
            self.assertEqual(updated.status_code, 303)
            self.assertIn(
                'title="japan/배우/ABC-001.mp4"',
                updated_dashboard.text,
            )
            self.assertEqual(deleted.status_code, 303)
            self.assertIn(
                'title="av/japan/배우/ABC-001/ABC-001.mp4"',
                unshortened.text,
            )
            self.assertEqual(created.status_code, 303)

    def test_audits_and_explicitly_cleans_unreferenced_artifacts(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            media_root = root / "media"
            media_root.mkdir()

            with TestClient(
                create_app(self.settings(root, media_root))
            ) as client:
                service = client.app.state.orchestrator
                service.stop()
                job = service.store.create(
                    job_id="retained-job",
                    source_rel="movie.mkv",
                    force_overwrite=False,
                    options={},
                )
                referenced = (
                    service.settings.jobs_dir / job.id / "transcript.json"
                )
                referenced.parent.mkdir(parents=True)
                referenced.write_text("retained", encoding="utf-8")
                service.store.update(
                    job.id,
                    status="transcription_completed",
                    transcript_path=str(referenced),
                )
                old_orphan = (
                    service.settings.jobs_dir / "deleted-job" / "old.json"
                )
                recent_orphan = (
                    service.settings.jobs_dir / "deleted-job" / "recent.json"
                )
                old_orphan.parent.mkdir(parents=True)
                old_orphan.write_text("old", encoding="utf-8")
                recent_orphan.write_text("recent", encoding="utf-8")
                os.utime(old_orphan, (0, 0))

                settings_page = client.get("/settings")
                audit_page = client.get(
                    "/settings?artifact_audit=true#artifact-retention"
                )
                cleanup_token = service.artifact_audit()["cleanup_token"]
                invalid = client.post(
                    "/settings/artifacts/cleanup",
                    data={
                        "minimum_age_days": "0",
                        "cleanup_token": cleanup_token,
                    },
                )
                cleaned = client.post(
                    "/settings/artifacts/cleanup",
                    data={
                        "minimum_age_days": "7",
                        "cleanup_token": cleanup_token,
                    },
                    follow_redirects=False,
                )
                stale_cleanup = client.post(
                    "/settings/artifacts/cleanup",
                    data={
                        "minimum_age_days": "7",
                        "cleanup_token": cleanup_token,
                    },
                )

            self.assertIn("산출물 보존", settings_page.text)
            self.assertNotIn("deleted-job/old.json", settings_page.text)
            self.assertIn("deleted-job/old.json", audit_page.text)
            self.assertIn("deleted-job/recent.json", audit_page.text)
            self.assertIn("DB 참조", audit_page.text)
            self.assertEqual(invalid.status_code, 400)
            self.assertEqual(cleaned.status_code, 303)
            self.assertEqual(stale_cleanup.status_code, 409)
            self.assertIn("artifact_cleanup_run=true", cleaned.headers["location"])
            self.assertIn("artifact_cleaned=1", cleaned.headers["location"])
            self.assertFalse(old_orphan.exists())
            self.assertTrue(recent_orphan.exists())
            self.assertTrue(referenced.exists())

    def test_manages_prompt_categories(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            media_root = root / "media"
            media_root.mkdir()

            with TestClient(
                create_app(self.settings(root, media_root))
            ) as client:
                page = client.get("/settings")
                created = client.post(
                    "/settings/prompt-categories",
                    data={
                        "name": "드라마",
                        "translation_prompt": "translate drama",
                        "review_prompt": "review drama",
                    },
                    follow_redirects=False,
                )
                service = client.app.state.orchestrator
                category = next(
                    item
                    for item in service.all_prompt_categories()
                    if item.name == "드라마"
                )
                updated = client.post(
                    f"/settings/prompt-categories/{category.id}",
                    data={
                        "name": "일본 드라마",
                        "translation_prompt": "translate drama v2",
                        "review_prompt": "review drama v2",
                    },
                    follow_redirects=False,
                )
                archived = client.post(
                    f"/settings/prompt-categories/{category.id}/archive",
                    follow_redirects=False,
                )
                dashboard = client.get("/media")
                restored = client.post(
                    f"/settings/prompt-categories/{category.id}/restore",
                    follow_redirects=False,
                )

            self.assertEqual(page.status_code, 200)
            self.assertIn("번역 프롬프트 카테고리", page.text)
            self.assertNotRegex(
                page.text,
                r'<details class="card settings-card prompt-category-card'
                r'[^\"]*"\s+open',
            )
            self.assertEqual(created.status_code, 303)
            self.assertEqual(updated.status_code, 303)
            self.assertEqual(archived.status_code, 303)
            self.assertNotIn(
                f'<option value="{category.id}">',
                dashboard.text,
            )
            self.assertEqual(restored.status_code, 303)

    def test_compares_and_selects_historical_prompt_revisions(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            media_root = root / "media"
            media_root.mkdir()
            (media_root / "movie.mkv").write_bytes(b"media")

            with TestClient(
                create_app(self.settings(root, media_root))
            ) as client:
                service = client.app.state.orchestrator
                service.stop()
                category = service.store.get_prompt_category("variety")
                service.store.update_prompt_category(
                    category.id,
                    name=category.name,
                    translation_prompt="current translation prompt",
                    review_prompt="current review prompt",
                )
                historical = service.store.list_prompt_revisions(
                    category.id
                )[0]
                selection = f"{category.id}@{historical['id']}"

                settings_page = client.get("/settings")
                media_page = client.get("/media")
                response = client.post(
                    "/jobs",
                    data={
                        "source_rels": "movie.mkv",
                        "backend": "hybrid",
                        "operation": "full",
                        "prompt_category_id": selection,
                    },
                    follow_redirects=False,
                )
                job = service.store.latest_jobs_by_source()["movie.mkv"]

            self.assertEqual(settings_page.status_code, 200)
            self.assertIn("이전 리비전 1개", settings_page.text)
            self.assertIn("current translation prompt", settings_page.text)
            self.assertIn(
                historical["translation_prompt"].splitlines()[0],
                settings_page.text,
            )
            self.assertIn("버라이어티 · v2 (현재)", media_page.text)
            self.assertIn("버라이어티 · v1", media_page.text)
            self.assertIn(f'value="{selection}"', media_page.text)
            self.assertEqual(response.status_code, 303)
            self.assertEqual(
                job.options["translation_prompt"]["revision_id"],
                historical["id"],
            )
            self.assertEqual(
                job.options["translation_prompt"]["translation_prompt"],
                historical["translation_prompt"],
            )

    def test_queries_openai_compatible_models_for_settings_list(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            media_root = root / "media"
            media_root.mkdir()

            with patch(
                "stt_to_subtitle.web_app.list_openai_compatible_models",
                return_value=["model-a", "model-b"],
            ) as list_models, TestClient(
                create_app(self.settings(root, media_root))
            ) as client:
                page = client.get("/settings")
                response = client.post(
                    "/settings/translation-models",
                    data={
                        "lm_base_url": "http://translation.test:1234/v1/",
                        "lm_token": "lookup-token",
                    },
                )

            self.assertEqual(response.status_code, 200)
            self.assertEqual(
                response.json(),
                {"models": ["model-a", "model-b"]},
            )
            list_models.assert_called_once_with(
                "http://translation.test:1234/v1",
                "lookup-token",
            )
            self.assertIn("OpenAI 호환 API 주소", page.text)
            self.assertIn('name="lm_model"', page.text)
            self.assertIn("모델 조회", page.text)
            self.assertIn("server-settings.js", page.text)

    def test_main_trusts_the_configured_reverse_proxy(self) -> None:
        with patch.dict(
            os.environ,
            {"WEB_FORWARDED_ALLOW_IPS": "*"},
            clear=False,
        ), patch("uvicorn.run") as run:
            main()

        self.assertTrue(run.call_args.kwargs["proxy_headers"])
        self.assertEqual(run.call_args.kwargs["forwarded_allow_ips"], "*")

    def test_recent_jobs_and_history_use_readable_responsive_layout(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            media_root = root / "media"
            media_root.mkdir()

            with TestClient(
                create_app(self.settings(root, media_root))
            ) as client:
                service = client.app.state.orchestrator
                service.stop()
                blocked = service.store.create(
                    job_id="blocked-job",
                    source_rel="show/movie.mkv",
                    force_overwrite=False,
                    options={},
                )
                service.store.add_event(
                    blocked.id,
                    "info",
                    "older history entry",
                )
                service.store.update(
                    blocked.id,
                    status="blocked",
                    blocked_stage="translation",
                    error="translation server unavailable",
                )
                service.store.add_event(
                    blocked.id,
                    "warning",
                    "newest history entry",
                )
                active = service.store.create(
                    job_id="active-job",
                    source_rel="another.mp4",
                    force_overwrite=False,
                    options={},
                )
                service.store.update(
                    active.id,
                    status="transcription_running",
                    chunks_created=12,
                    chunks_completed=7,
                    chunks_total_estimate=15,
                )

                dashboard = client.get("/")
                detail = client.get(f"/jobs/{blocked.id}")

            self.assertEqual(dashboard.status_code, 200)
            self.assertIn('class="ob-work-column"', dashboard.text)
            self.assertNotIn("<table", dashboard.text)
            self.assertIn("실행 중", dashboard.text)
            self.assertIn("전사", dashboard.text)
            self.assertIn("중단", dashboard.text)
            self.assertNotIn("확인 필요", dashboard.text)
            self.assertIn('class="ob-stages"', dashboard.text)
            self.assertIn("경과", dashboard.text)
            self.assertNotIn("남은 시간", dashboard.text)
            self.assertNotIn("추출된 WAV 재생 시간", dashboard.text)
            self.assertIn("7/≈15", dashboard.text)
            self.assertIn("movie.mkv", dashboard.text)
            self.assertIn("show", dashboard.text)

            self.assertEqual(detail.status_code, 200)
            self.assertIn("작업 히스토리", detail.text)
            self.assertIn("번역", detail.text)
            self.assertIn('class="latest-event"', detail.text)
            self.assertIn("주의", detail.text)
            self.assertLess(
                detail.text.index("newest history entry"),
                detail.text.index("older history entry"),
            )

    def test_first_run_is_configured_entirely_from_web_page(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            media_root = root / "media"
            media_root.mkdir()
            (media_root / "movie.mp4").write_bytes(b"media")
            settings = WebSettings(
                state_dir=root / "state",
                media_root=media_root,
                admin_password="",
                session_secret="",
                stt_base_url="",
                stt_token="",
                lm_base_url="",
                lm_token="",
                lm_model="",
            )

            with TestClient(create_app(settings)) as client:
                before = client.get("/media")
                saved = client.post(
                    "/settings",
                    data={
                        "stt_base_url": "http://stt.test:8100",
                        "lm_base_url": "http://lm.test:1234/v1",
                        "lm_model": "model",
                    },
                    follow_redirects=False,
                )
                after = client.get("/media")
                health = client.get("/healthz").json()

            self.assertIn("전사·번역 서버를 설정", before.text)
            self.assertIn('data-server-configured="false"', before.text)
            self.assertEqual(saved.status_code, 303)
            self.assertNotIn("전사·번역 서버를 설정", after.text)
            self.assertIn('data-server-configured="true"', after.text)
            self.assertTrue(health["remote_servers_configured"])

    def test_batch_submission_queues_all_selected_files(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            media_root = root / "media"
            media_root.mkdir()
            (media_root / "one.mkv").write_bytes(b"media")
            (media_root / "two.mp4").write_bytes(b"media")

            with TestClient(
                create_app(self.settings(root, media_root))
            ) as client:
                client.app.state.orchestrator.stop()
                response = client.post(
                    "/jobs",
                    data={
                        "source_rels": ["one.mkv", "two.mp4"],
                        "backend": "hybrid",
                        "duration_seconds": "0",
                        "noise_filter": ["false", "true"],
                        "prompt_category_id": "jav",
                    },
                    follow_redirects=False,
                )
                jobs = client.get("/api/jobs").json()

            self.assertEqual(response.status_code, 303)
            self.assertEqual(response.headers["location"], "/media?queued=2")
            self.assertEqual(
                {job["source_rel"] for job in jobs},
                {"one.mkv", "two.mp4"},
            )
            self.assertTrue(
                all(job["created_at"].endswith("+09:00") for job in jobs)
            )
            self.assertTrue(
                all(job["updated_at"].endswith("+09:00") for job in jobs)
            )
            self.assertTrue(
                all(
                    job["options"]["chunk_length_seconds"] == 15
                    for job in jobs
                )
            )
            self.assertTrue(
                all(job["options"]["noise_filter"] for job in jobs)
            )
            self.assertTrue(
                all(job["options"]["backend"] == "hybrid" for job in jobs)
            )
            self.assertTrue(
                all(
                    job["options"]["translation_prompt"]["category_id"]
                    == "jav"
                    for job in jobs
                )
            )

    def test_auto_backend_applies_jav_prompt_and_whisperjav_preset(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            media_root = root / "media"
            media_root.mkdir()
            (media_root / "jav.mp4").write_bytes(b"media")
            (media_root / "variety.mp4").write_bytes(b"media")

            with TestClient(
                create_app(self.settings(root, media_root))
            ) as client:
                service = client.app.state.orchestrator
                service.stop()
                jav_response = client.post(
                    "/jobs",
                    data={
                        "source_rels": "jav.mp4",
                        "backend": "auto",
                        "operation": "full",
                    },
                    follow_redirects=False,
                )
                variety_response = client.post(
                    "/jobs",
                    data={
                        "source_rels": "variety.mp4",
                        "backend": "hybrid",
                        "prompt_category_id": "variety",
                        "operation": "full",
                    },
                    follow_redirects=False,
                )
                jobs = {
                    job.source_rel: job
                    for job in service.store.list_jobs(limit=None)
                }

            self.assertEqual(jav_response.status_code, 303)
            self.assertEqual(variety_response.status_code, 303)
            self.assertEqual(jobs["jav.mp4"].options["backend"], "whisperjav")
            self.assertEqual(jobs["variety.mp4"].options["backend"], "hybrid")
            self.assertEqual(
                jobs["jav.mp4"].options["translation_prompt"]["category_id"],
                "jav",
            )
            self.assertEqual(
                jobs["variety.mp4"].options["translation_prompt"][
                    "category_id"
                ],
                "variety",
            )

    def test_multipart_card_queues_each_physical_file_as_a_job(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            media_root = root / "media"
            media_root.mkdir()
            (media_root / "movie-pt1.mkv").write_bytes(b"part-one")
            (media_root / "movie-pt2.mkv").write_bytes(b"part-two")

            with patch(
                "stt_to_subtitle.orchestrator.probe_media_duration",
                return_value=60.0,
            ), TestClient(
                create_app(self.settings(root, media_root))
            ) as client:
                client.app.state.orchestrator.stop()
                page = client.get("/media")
                response = client.post(
                    "/jobs",
                    data={
                        "source_groups": json.dumps(
                            ["movie-pt1.mkv", "movie-pt2.mkv"]
                        ),
                        "operation": "transcribe",
                    },
                    follow_redirects=False,
                )
                jobs = client.get("/api/jobs").json()

            self.assertEqual(page.status_code, 200)
            self.assertIn("MULTIPART · 2", page.text)
            self.assertIn("metadata-label multipart-badge", page.text)
            self.assertIn("subtitle-state multipart-badge", page.text)
            self.assertIn('name="source_groups"', page.text)
            self.assertNotIn("movie-pt*", page.text)
            self.assertIn(
                '<strong class="media-title" title="movie">movie</strong>',
                page.text,
            )
            self.assertEqual(response.status_code, 303)
            self.assertEqual(response.headers["location"], "/media?queued=2")
            self.assertEqual(
                {job["source_rel"] for job in jobs},
                {"movie-pt1.mkv", "movie-pt2.mkv"},
            )

    def test_folder_submission_recurses_and_reports_skipped_files(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            media_root = root / "media"
            nested = media_root / "Shows" / "Season 1"
            nested.mkdir(parents=True)
            (nested / "pending.mkv").write_bytes(b"media")
            (nested / "running.mkv").write_bytes(b"media")
            (nested / "completed.mkv").write_bytes(b"media")
            (nested / "subtitled.mkv").write_bytes(b"media")
            (nested / "subtitled.ko.srt").write_text(
                "subtitle",
                encoding="utf-8",
            )

            with TestClient(
                create_app(self.settings(root, media_root))
            ) as client:
                service = client.app.state.orchestrator
                service.stop()
                running = service.store.create(
                    job_id="running-job",
                    source_rel="Shows/Season 1/running.mkv",
                    force_overwrite=False,
                    options={},
                )
                service.store.update(
                    running.id,
                    status="transcription_running",
                )
                completed = service.store.create(
                    job_id="completed-job",
                    source_rel="Shows/Season 1/completed.mkv",
                    force_overwrite=False,
                    options={},
                )
                service.store.update(completed.id, status="completed")
                response = client.post(
                    "/jobs",
                    data={
                        "folder_rels": "Shows",
                        "backend": "hybrid",
                        "prompt_category_id": "variety",
                    },
                    follow_redirects=False,
                )
                jobs = service.store.list_jobs(limit=None)

            self.assertEqual(response.status_code, 303)
            self.assertEqual(
                response.headers["location"],
                "/media?queued=1&skipped=3",
            )
            created = next(
                job
                for job in jobs
                if job.id not in {"running-job", "completed-job"}
            )
            self.assertEqual(
                created.source_rel,
                "Shows/Season 1/pending.mkv",
            )
            self.assertEqual(created.prompt_category_name, "버라이어티")
            self.assertEqual(
                created.options["translation_prompt"]["review_rounds"],
                2,
            )
            self.assertEqual(created.options["backend"], "hybrid")

    def test_media_cards_show_each_files_latest_processing_stage(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            media_root = root / "media"
            media_root.mkdir()
            (media_root / "nested").mkdir()
            for name in (
                "pending.mkv",
                "audio.mkv",
                "transcribed.mkv",
                "running.mkv",
                "blocked.mkv",
                "completed.mkv",
                "subtitled.mkv",
            ):
                (media_root / name).write_bytes(b"media")
            (media_root / "subtitled.ko.srt").write_text(
                "subtitle",
                encoding="utf-8",
            )

            with TestClient(
                create_app(self.settings(root, media_root))
            ) as client:
                service = client.app.state.orchestrator
                service.stop()

                def add_job(
                    job_id: str,
                    source_rel: str,
                    status: str,
                    *,
                    blocked_stage: str | None = None,
                ) -> None:
                    job = service.store.create(
                        job_id=job_id,
                        source_rel=source_rel,
                        force_overwrite=False,
                        options={},
                    )
                    service.store.update(
                        job.id,
                        status=status,
                        blocked_stage=blocked_stage,
                    )

                add_job("audio-job", "audio.mkv", "audio_completed")
                add_job(
                    "transcribed-job",
                    "transcribed.mkv",
                    "transcription_completed",
                )
                add_job(
                    "running-job",
                    "running.mkv",
                    "translation_running",
                )
                add_job(
                    "blocked-job",
                    "blocked.mkv",
                    "blocked",
                    blocked_stage="translation",
                )
                add_job("completed-job", "completed.mkv", "completed")
                response = client.get("/media")

            self.assertEqual(response.status_code, 200)
            expected_stages = {
                "pending": "미처리",
                "audio_completed": "오디오 추출 완료",
                "transcription_completed": "전사 완료",
                "translation_running": "번역 중",
                "blocked": "중단 · 번역",
                "completed": "자막 생성 완료",
                "subtitle_present": "한국어 자막 있음",
            }
            for stage, label in expected_stages.items():
                self.assertRegex(
                    response.text,
                    rf'data-processing-stage="{stage}"[^>]*>\s*{label}',
                )
            self.assertIn("폴더 전체 선택", response.text)
            self.assertRegex(
                response.text,
                r'name="folder_rels"\s+type="checkbox"\s+'
                r'value="nested"\s+data-auto-select',
            )
            self.assertIn("이미 완료된 파일을 자동 제외", response.text)

    def test_completed_job_streams_video_range_and_webvtt(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            media_root = root / "media"
            media_root.mkdir()
            source = media_root / "movie.mp4"
            source.write_bytes(b"0123456789")
            subtitle = media_root / "movie.ko.srt"
            subtitle.write_text(
                "1\n"
                "00:00:01,000 --> 00:00:02,500\n"
                "처리 결과\n",
                encoding="utf-8",
            )

            with TestClient(
                create_app(self.settings(root, media_root))
            ) as client:
                service = client.app.state.orchestrator
                service.stop()
                job = service.create_job(
                    "movie.mp4",
                    force_overwrite=True,
                    options={},
                )
                service.store.update(
                    job.id,
                    status="completed",
                    srt_path=str(subtitle),
                )

                page = client.get(f"/jobs/{job.id}")
                video = client.get(
                    f"/jobs/{job.id}/video",
                    headers={"Range": "bytes=2-5"},
                )
                invalid_range = client.get(
                    f"/jobs/{job.id}/video",
                    headers={"Range": "bytes=99-"},
                )
                captions = client.get(f"/jobs/{job.id}/subtitles.vtt")
                vr_renderer = client.get("/static/vr180-player.js")
                player_script = client.get("/static/player.js")

            self.assertEqual(page.status_code, 200)
            self.assertEqual(
                page.headers["permissions-policy"],
                "xr-spatial-tracking=(self)",
            )
            self.assertIn("KST", page.text)
            self.assertIn('class="result-player"', page.text)
            self.assertIn('data-video-type="video/mp4"', page.text)
            self.assertIn("data-subtitle-src=", page.text)
            self.assertIn('data-player-mode="vr180"', page.text)
            self.assertIn("180° 미리보기", page.text)
            self.assertIn("data-vr180-canvas", page.text)
            self.assertIn("data-vr180-subtitles", page.text)
            self.assertNotIn("data-vr-eye", page.text)
            self.assertIn("data-vr-headset", page.text)
            self.assertIn("WebXR", page.text)
            self.assertIn("data-vr-volume", page.text)
            self.assertIn("Space: 재생/일시정지", page.text)
            self.assertIn("vr180-player.js", page.text)
            self.assertNotIn("전사·번역 결과를 화자별", page.text)
            self.assertNotIn("지원하지 않는 MIME 형식", page.text)
            self.assertNotIn("<source", page.text)
            self.assertEqual(video.status_code, 206)
            self.assertEqual(video.content, b"2345")
            self.assertEqual(video.headers["accept-ranges"], "bytes")
            self.assertEqual(
                video.headers["content-range"],
                "bytes 2-5/10",
            )
            self.assertEqual(invalid_range.status_code, 416)
            self.assertEqual(
                invalid_range.headers["content-range"],
                "bytes */10",
            )
            self.assertEqual(captions.status_code, 200)
            self.assertIn("text/vtt", captions.headers["content-type"])
            self.assertIn(
                "00:00:01.000 --> 00:00:02.500",
                captions.text,
            )
            self.assertIn("처리 결과", captions.text)
            self.assertEqual(vr_renderer.status_code, 200)
            self.assertIn(
                "window.createVR180Renderer",
                vr_renderer.text,
            )
            self.assertIn(
                'requestSession("immersive-vr"',
                vr_renderer.text,
            )
            self.assertIn(
                "frame.getViewerPose",
                vr_renderer.text,
            )
            self.assertIn(
                'view.eye === "right"',
                vr_renderer.text,
            )
            self.assertIn("XRWebGLLayer", vr_renderer.text)
            self.assertIn("setSubtitleLines", vr_renderer.text)
            self.assertNotIn("u_stereo_mode", vr_renderer.text)
            self.assertNotIn(
                'canvas.addEventListener("keydown"',
                vr_renderer.text,
            )
            self.assertIn(
                "window.isImmersiveVRSupported",
                player_script.text,
            )
            self.assertIn('event.code === "Space"', player_script.text)
            self.assertIn('event.key === "ArrowLeft"', player_script.text)
            self.assertIn('event.key === "ArrowUp"', player_script.text)

    def test_external_subtitle_is_default_playback_and_can_be_validated(
        self,
    ) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            media_root = root / "media"
            media_root.mkdir()
            source = media_root / "movie.mp4"
            source.write_bytes(b"media")
            external = media_root / "movie.srt"
            external.write_text(
                "1\n00:00:01,000 --> 00:00:03,000\n외부 한국어 자막\n",
                encoding="utf-8",
            )
            generated = media_root / "movie.ko.srt"
            generated.write_text(
                "1\n00:00:01,000 --> 00:00:03,000\n생성 한국어 자막\n",
                encoding="utf-8",
            )

            with TestClient(create_app(self.settings(root, media_root))) as client:
                service = client.app.state.orchestrator
                service.stop()
                job = service.create_job(
                    "movie.mp4",
                    force_overwrite=True,
                    options={},
                )
                service.store.update(
                    job.id,
                    status="completed",
                    srt_path=str(generated),
                )
                media_page = client.get("/media")
                page = client.get(f"/jobs/{job.id}")
                captions = client.get(f"/jobs/{job.id}/subtitles.vtt")
                media_captions = client.get(
                    "/media/subtitles.vtt",
                    params={"path": "movie.mp4"},
                )
                compared = client.post(
                    f"/jobs/{job.id}/validate-external-subtitle",
                    follow_redirects=False,
                )
                compared_page = client.get(f"/jobs/{job.id}")
                validator_saved = client.post(
                    "/settings/subtitle-validator",
                    data={
                        "validator_base_url": "https://validator.test/v1",
                        "validator_token": "paid-secret",
                        "validator_model": "paid-model",
                    },
                    follow_redirects=False,
                )
                paid_result = {
                    "severity": "review",
                    "severity_label": "검토 필요",
                    "summary": "표현 차이를 확인하세요.",
                    "findings": [
                        {
                            "reference_index": 1,
                            "category": "meaning",
                            "category_label": "의미 차이",
                            "message": "명사 표현이 다릅니다.",
                        }
                    ],
                }
                with patch(
                    "stt_to_subtitle.orchestrator.SubtitleValidationClient"
                ) as validator:
                    validator.return_value.validate.return_value = paid_result
                    paid = client.post(
                        f"/jobs/{job.id}/validate-external-subtitle/llm",
                        follow_redirects=False,
                    )
                validated_page = client.get(f"/jobs/{job.id}")

            self.assertIn("외부 자막", media_page.text)
            self.assertIn("외부 자막 비교", page.text)
            self.assertEqual(captions.status_code, 200)
            self.assertIn("외부 한국어 자막", captions.text)
            self.assertNotIn("생성 한국어 자막", captions.text)
            self.assertEqual(media_captions.status_code, 200)
            self.assertIn("외부 한국어 자막", media_captions.text)
            self.assertEqual(compared.status_code, 303)
            self.assertIn("시간 일치율", compared_page.text)
            self.assertEqual(validator_saved.status_code, 303)
            self.assertEqual(paid.status_code, 303)
            self.assertIn("상용 LLM · 검토 필요", validated_page.text)
            self.assertIn("표현 차이를 확인하세요.", validated_page.text)
            validator.return_value.validate.assert_called_once()

    def test_edits_source_named_json_and_shows_chunk_progress(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            media_root = root / "media"
            media_root.mkdir()
            source = media_root / "movie.mp4"
            source.write_bytes(b"media")

            with TestClient(
                create_app(self.settings(root, media_root))
            ) as client:
                service = client.app.state.orchestrator
                service.stop()
                job = service.create_job(
                    "movie.mp4",
                    force_overwrite=True,
                    options={},
                )
                artifact_dir = root / "state" / "jobs" / job.id
                artifact_dir.mkdir(parents=True, exist_ok=True)
                transcript = artifact_dir / "movie_translate.json"
                translation = artifact_dir / "movie_result_ko.json"
                transcript.write_text(
                    json.dumps(
                        {
                            "schema_version": 1,
                            "job_id": "remote-job",
                            "segments": [
                                {
                                    "id": "segment-000001",
                                    "start": 0,
                                    "end": 1,
                                    "speaker": "SPEAKER_00",
                                    "text": "こんにちは",
                                }
                            ],
                        }
                    ),
                    encoding="utf-8",
                )
                translation.write_text(
                    json.dumps(
                        {
                            "schema_version": 1,
                            "status": "completed",
                            "translations": [
                                {
                                    "id": "segment-000001",
                                    "text": "안녕하세요",
                                }
                            ],
                        }
                    ),
                    encoding="utf-8",
                )
                service.store.update(
                    job.id,
                    status="completed",
                    transcript_path=str(transcript),
                    translation_path=str(translation),
                    chunks_created=21,
                    chunks_completed=20,
                    chunk_progress_every=10,
                )

                page = client.get(f"/jobs/{job.id}")
                editor = client.get(
                    f"/jobs/{job.id}/artifacts/translation/edit"
                )
                invalid = client.post(
                    f"/jobs/{job.id}/artifacts/translation/edit",
                    data={"content": "{not-json"},
                )
                saved = client.post(
                    f"/jobs/{job.id}/artifacts/translation/edit",
                    data={
                        "content": json.dumps(
                            {
                                "schema_version": 1,
                                "status": "completed",
                                "translations": [
                                    {
                                        "id": "segment-000001",
                                        "text": "수정된 번역",
                                    }
                                ],
                            },
                            ensure_ascii=False,
                        )
                    },
                    follow_redirects=False,
                )
                captions = client.get(f"/jobs/{job.id}/subtitles.vtt")
                styled_subtitle = client.get(
                    f"/jobs/{job.id}/subtitle.ass"
                )
                refreshed_page = client.get(f"/jobs/{job.id}")
                generations = service.store.list_translation_generations(job.id)
                generation_download = client.get(
                    f"/jobs/{job.id}/translation-generations/"
                    f"{generations[-1]['id']}"
                )
                subtitle_generations = (
                    service.store.list_subtitle_generations(job.id)
                )
                subtitle_generation_download = client.get(
                    f"/jobs/{job.id}/subtitle-generations/"
                    f"{subtitle_generations[-1]['id']}.srt"
                )
                subtitle_republish = client.post(
                    f"/jobs/{job.id}/subtitle-generations/"
                    f"{subtitle_generations[-1]['id']}/publish",
                    follow_redirects=False,
                )
                completed_jobs = client.get("/jobs-fragment")
                restart = client.post(
                    f"/jobs/{job.id}/restart-translation",
                    data={"prompt_category_id": "variety"},
                    follow_redirects=False,
                )
                restarted_job = service.store.get(job.id)
                reset_translation = json.loads(
                    translation.read_text(encoding="utf-8")
                )

            self.assertIn("20", page.text)
            self.assertIn("21 전체", page.text)
            self.assertIn("1 남음", page.text)
            self.assertIn('class="job-stage-strip job-detail-stage-strip"', page.text)
            for stage_label in ("추출", "전사", "번역"):
                self.assertIn(
                    f'<span class="job-stage-label">{stage_label}</span>',
                    page.text,
                )
            self.assertIn('class="job-endpoint is-done"', page.text)
            self.assertIn("작업 완료", page.text)
            self.assertIn("한국어 결과 JSON 편집", page.text)
            self.assertEqual(editor.status_code, 200)
            self.assertIn("movie_result_ko.json", editor.text)
            self.assertIn("json-editor.js", editor.text)
            self.assertEqual(invalid.status_code, 400)
            self.assertIn("JSON syntax error", invalid.text)
            self.assertEqual(saved.status_code, 303)
            self.assertIn(
                "<c.speaker-1>수정된 번역</c>",
                captions.text,
            )
            self.assertNotIn("화자 1", captions.text)
            self.assertEqual(styled_subtitle.status_code, 200)
            self.assertIn("text/x-ssa", styled_subtitle.headers["content-type"])
            self.assertIn("[V4+ Styles]", styled_subtitle.text)
            self.assertIn("스타일 ASS 다운로드", refreshed_page.text)
            self.assertIn("번역부터 다시 시작", refreshed_page.text)
            self.assertIn("번역 이력", refreshed_page.text)
            self.assertIn("번역 버전 2 · 완료", refreshed_page.text)
            self.assertIn("직접 편집", refreshed_page.text)
            self.assertEqual(generation_download.status_code, 200)
            self.assertEqual(
                generation_download.json()["translations"][0]["text"],
                "수정된 번역",
            )
            self.assertIn("자막 이력", refreshed_page.text)
            self.assertIn("자막 버전 1 · 게시 중", refreshed_page.text)
            self.assertEqual(subtitle_generation_download.status_code, 200)
            self.assertIn("수정된 번역", subtitle_generation_download.text)
            self.assertEqual(subtitle_republish.status_code, 303)
            self.assertIn("번역 다시 시작", completed_jobs.text)
            self.assertIn(
                f'/jobs/{job.id}/restart-translation',
                completed_jobs.text,
            )
            self.assertIn(
                "수정된 번역",
                (media_root / "movie.ko.srt").read_text(encoding="utf-8"),
            )
            self.assertEqual(restart.status_code, 303)
            self.assertEqual(restarted_job.status, "transcribed")
            self.assertEqual(reset_translation["status"], "partial")
            self.assertEqual(reset_translation["translations"], [])
            self.assertTrue(transcript.is_file())

    def test_media_cards_show_job_state_and_link_to_latest_detail(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            media_root = root / "media"
            media_root.mkdir()
            for name in (
                "pending.mp4",
                "running.mp4",
                "done.mp4",
                "stopped.mp4",
                "bad.mp4",
            ):
                (media_root / name).write_bytes(b"media")
            done_subtitle = media_root / "done.ko.srt"
            done_subtitle.write_text("subtitle", encoding="utf-8")

            with TestClient(create_app(self.settings(root, media_root))) as client:
                service = client.app.state.orchestrator
                service.stop()
                running = service.store.create(
                    job_id="running-job",
                    source_rel="running.mp4",
                    force_overwrite=False,
                    options={},
                )
                service.store.update(running.id, status="translation_running")
                done = service.store.create(
                    job_id="done-job",
                    source_rel="done.mp4",
                    force_overwrite=True,
                    options={},
                )
                service.store.update(
                    done.id,
                    status="completed",
                    srt_path=str(done_subtitle),
                )
                blocked = service.store.create(
                    job_id="blocked-job",
                    source_rel="stopped.mp4",
                    force_overwrite=False,
                    options={},
                )
                service.store.update(
                    blocked.id,
                    status="blocked",
                    error="stopped",
                )
                failed = service.store.create(
                    job_id="failed-job",
                    source_rel="bad.mp4",
                    force_overwrite=False,
                    options={},
                )
                service.store.update(failed.id, status="failed", error="bad")

                page = client.get("/media")

            self.assertIn("미처리", page.text)
            self.assertIn("번역 중", page.text)
            self.assertIn("자막 생성 완료", page.text)
            self.assertIn("중단", page.text)
            self.assertIn("실패", page.text)
            self.assertIn('href="/jobs/running-job"', page.text)
            self.assertIn('href="/jobs/done-job"', page.text)
            self.assertIn('href="/jobs/blocked-job"', page.text)
            self.assertIn('href="/jobs/failed-job"', page.text)
            self.assertIn("subtitle-state is-blocked", page.text)
            self.assertIn("subtitle-state is-failed", page.text)
            self.assertIn('value="pending.mp4"', page.text)
            # 완료된 항목도 다시 번역하려면 개별 선택이 되어야 한다.
            self.assertIn('value="done.mp4"', page.text)
            # 다만 '전체 선택'에는 담기지 않는다.
            self.assertRegex(
                page.text,
                r'value="pending\.mp4"[^>]*\s+data-auto-select',
            )
            self.assertNotRegex(
                page.text,
                r'value="done\.mp4"[^>]*\s+data-auto-select',
            )

    def test_deletes_retriable_legacy_audio_and_missing_remote_job_records(
        self,
    ) -> None:
        missing_error = (
            "transcription status request failed: HTTP 404: "
            "{'detail': 'job not found'}"
        )
        with TemporaryDirectory() as directory:
            root = Path(directory)
            media_root = root / "media"
            media_root.mkdir()
            source = media_root / "movie.mp4"
            source.write_bytes(b"media")
            audio_source = media_root / "audio.mp4"
            audio_source.write_bytes(b"media")

            with TestClient(create_app(self.settings(root, media_root))) as client:
                service = client.app.state.orchestrator
                service.stop()
                missing = service.store.create(
                    job_id="missing-job",
                    source_rel=source.name,
                    force_overwrite=False,
                    options={},
                )
                service.store.update(
                    missing.id,
                    status="blocked",
                    blocked_stage="transcription",
                    error=missing_error,
                )
                other = service.store.create(
                    job_id="other-job",
                    source_rel=source.name,
                    force_overwrite=False,
                    options={},
                )
                service.store.update(
                    other.id,
                    status="blocked",
                    blocked_stage="transcription",
                    error="transcription server is unavailable",
                )
                failed = service.store.create(
                    job_id="failed-job",
                    source_rel=source.name,
                    force_overwrite=False,
                    options={},
                )
                service.store.update(
                    failed.id,
                    status="failed",
                    blocked_stage="translation",
                    error="translation server is unavailable",
                )
                active = service.store.create(
                    job_id="active-job",
                    source_rel=source.name,
                    force_overwrite=False,
                    options={},
                )
                invalid_return = service.store.create(
                    job_id="invalid-return-job",
                    source_rel=source.name,
                    force_overwrite=False,
                    options={},
                )
                service.store.update(
                    invalid_return.id,
                    status="blocked",
                    blocked_stage="translation",
                    error="translation failed",
                )
                audio = service.store.create(
                    job_id="audio-job",
                    source_rel=audio_source.name,
                    force_overwrite=False,
                    options={},
                    operation="extract",
                )
                audio_artifact = (
                    root / "state" / "jobs" / audio.id / "audio.wav"
                )
                audio_artifact.parent.mkdir(parents=True)
                audio_artifact.write_bytes(b"audio")
                service.store.update(
                    audio.id,
                    status="audio_completed",
                    audio_path=str(audio_artifact),
                )
                artifact = root / "state" / "jobs" / missing.id / "audio.wav"
                artifact.parent.mkdir(parents=True)
                artifact.write_bytes(b"audio")

                dashboard = client.get("/jobs-fragment")
                blocked_page = client.get(
                    "/jobs?status_group=blocked&jobs_page=1"
                )
                failed_page = client.get(
                    "/jobs?status_group=failed&jobs_page=1"
                )
                detail = client.get(
                    f"/jobs/{missing.id}?return_status_group=blocked"
                    "&return_jobs_page=2"
                )
                audio_detail = client.get(f"/jobs/{audio.id}")
                rejected = client.post(
                    f"/jobs/{active.id}/delete",
                    follow_redirects=False,
                )
                blocked_deleted = client.post(
                    f"/jobs/{other.id}/delete",
                    data={
                        "return_status_group": "blocked",
                        "return_jobs_page": "1",
                    },
                    follow_redirects=False,
                )
                failed_deleted = client.post(
                    f"/jobs/{failed.id}/delete",
                    data={
                        "return_status_group": "failed",
                        "return_jobs_page": "1",
                    },
                    follow_redirects=False,
                )
                deleted = client.post(
                    f"/jobs/{missing.id}/delete",
                    data={
                        "return_status_group": "blocked",
                        "return_jobs_page": "2",
                    },
                    follow_redirects=False,
                )
                audio_deleted = client.post(
                    f"/jobs/{audio.id}/delete",
                    data={
                        "return_stage_filter": "extraction",
                        "return_jobs_page": "3",
                    },
                    follow_redirects=False,
                )
                invalid_return_rejected = client.post(
                    f"/jobs/{invalid_return.id}/delete",
                    data={"return_status_group": "unknown"},
                    follow_redirects=False,
                )

                self.assertIsNotNone(service.store.get(active.id))
                self.assertIsNotNone(service.store.get(invalid_return.id))
                self.assertIsNone(service.store.get(other.id))
                self.assertIsNone(service.store.get(failed.id))
                self.assertIsNone(service.store.get(missing.id))
                self.assertIsNone(service.store.get(audio.id))

            self.assertIn(
                f'action="/jobs/{missing.id}/delete"',
                dashboard.text,
            )
            self.assertIn(
                f'action="/jobs/{other.id}/delete"',
                dashboard.text,
            )
            self.assertIn(
                f'action="/jobs/{failed.id}/delete"',
                dashboard.text,
            )
            self.assertNotIn(
                f'action="/jobs/{active.id}/delete"',
                dashboard.text,
            )
            self.assertIn(
                f'action="/jobs/{missing.id}/delete"',
                detail.text,
            )
            self.assertIn(
                'href="/jobs?status_group=blocked&amp;jobs_page=2"',
                detail.text,
            )
            self.assertIn(
                'name="return_status_group" value="blocked"',
                detail.text,
            )
            self.assertIn(
                'name="return_jobs_page" value="2"',
                detail.text,
            )
            self.assertIn(
                'name="return_status_group" value="blocked"',
                blocked_page.text,
            )
            self.assertIn(
                'name="return_status_group" value="failed"',
                failed_page.text,
            )
            self.assertIn(
                f'action="/jobs/{audio.id}/delete"',
                dashboard.text,
            )
            self.assertIn("기록 삭제", audio_detail.text)
            self.assertEqual(rejected.status_code, 400)
            self.assertEqual(blocked_deleted.status_code, 303)
            self.assertEqual(failed_deleted.status_code, 303)
            self.assertEqual(deleted.status_code, 303)
            self.assertEqual(audio_deleted.status_code, 303)
            self.assertEqual(invalid_return_rejected.status_code, 400)
            self.assertEqual(
                blocked_deleted.headers["location"],
                "/jobs?status_group=blocked&jobs_page=1",
            )
            self.assertEqual(
                failed_deleted.headers["location"],
                "/jobs?status_group=failed&jobs_page=1",
            )
            self.assertEqual(
                deleted.headers["location"],
                "/jobs?status_group=blocked&jobs_page=2",
            )
            self.assertEqual(
                audio_deleted.headers["location"],
                "/jobs?stage_filter=extraction&jobs_page=3",
            )
            self.assertTrue(source.is_file())
            self.assertTrue(artifact.is_file())
            self.assertTrue(audio_source.is_file())
            self.assertTrue(audio_artifact.is_file())

    def test_dashboard_actions_return_immediately_and_bulk_stop_jobs(
        self,
    ) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            media_root = root / "media"
            media_root.mkdir()
            (media_root / "series").mkdir()

            with TestClient(create_app(self.settings(root, media_root))) as client:
                service = client.app.state.orchestrator
                service.stop()
                single = service.store.create(
                    job_id="single",
                    source_rel="single.mkv",
                    force_overwrite=False,
                    options={},
                )
                service.store.update(single.id, status="transcribed")
                queued = service.store.create(
                    job_id="queued",
                    source_rel="queued.mkv",
                    force_overwrite=False,
                    options={},
                )
                running = service.store.create(
                    job_id="running",
                    source_rel="running.mkv",
                    force_overwrite=False,
                    options={},
                )
                service.store.update(
                    running.id,
                    status="transcription_running",
                )
                extraction = service.store.create(
                    job_id="extract",
                    source_rel="extract.mkv",
                    force_overwrite=False,
                    options={},
                    operation="extract",
                )

                fragment = client.get("/jobs-fragment?folder=series")
                single_pause = client.post(
                    f"/jobs/{single.id}/pause-translation",
                    data={"return_folder": "series"},
                    follow_redirects=False,
                )
                bulk_pause = client.post(
                    "/jobs/pause-all-translations",
                    data={"return_folder": "series"},
                    follow_redirects=False,
                )
                bulk_stop = client.post(
                    "/jobs/stop-all",
                    data={"return_folder": "series"},
                    follow_redirects=False,
                )
                refreshed = client.get("/jobs-fragment?folder=series")

                single = service.store.get(single.id)
                queued = service.store.get(queued.id)
                running = service.store.get(running.id)
                extraction = service.store.get(extraction.id)

            self.assertIn("전체 번역 중단 (3)", fragment.text)
            self.assertIn("전체 작업 중단 (4)", fragment.text)
            self.assertIn(
                'name="return_folder" value="series"',
                fragment.text,
            )
            self.assertEqual(single_pause.status_code, 303)
            self.assertEqual(
                single_pause.headers["location"],
                "/media?folder=series&translation_pause_requested=1",
            )
            self.assertEqual(bulk_pause.status_code, 303)
            self.assertEqual(
                bulk_pause.headers["location"],
                "/media?folder=series&translations_paused=2",
            )
            self.assertEqual(bulk_stop.status_code, 303)
            self.assertEqual(
                bulk_stop.headers["location"],
                "/media?folder=series&jobs_stopped=3",
            )
            self.assertEqual(single.status, "translation_paused")
            self.assertEqual(queued.status, "blocked")
            self.assertTrue(running.job_stop_requested)
            self.assertEqual(extraction.status, "blocked")
            self.assertIn("전체 작업 중단 요청됨", refreshed.text)

    def test_job_list_stops_selected_jobs_across_filtered_pages(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            media_root = root / "media"
            media_root.mkdir()

            with TestClient(create_app(self.settings(root, media_root))) as client:
                service = client.app.state.orchestrator
                service.stop()
                jobs = []
                for index in range(25):
                    job = service.store.create(
                        job_id=f"waiting-{index:02d}",
                        source_rel=f"waiting-{index:02d}.mkv",
                        force_overwrite=False,
                        options={},
                    )
                    service.store.update(job.id, status="transcribed")
                    jobs.append(job)

                page = client.get(
                    "/jobs?stage_filter=translation_waiting&jobs_page=1"
                )
                response = client.post(
                    "/jobs/stop-selected",
                    data={
                        "job_ids": [jobs[0].id, jobs[-1].id],
                        "return_stage_filter": "translation_waiting",
                        "return_jobs_page": "2",
                    },
                    follow_redirects=False,
                )
                selected = [
                    service.store.get(jobs[0].id),
                    service.store.get(jobs[-1].id),
                ]
                untouched = service.store.get(jobs[1].id)

            self.assertEqual(page.status_code, 200)
            self.assertEqual(page.text.count("data-stop-job-checkbox"), 20)
            self.assertEqual(page.text.count("data-stop-job-candidate"), 25)
            self.assertIn('action="/jobs/stop-selected"', page.text)
            self.assertIn("목록 전체 선택", page.text)
            self.assertEqual(response.status_code, 303)
            self.assertEqual(
                response.headers["location"],
                "/jobs?stage_filter=translation_waiting&jobs_page=1"
                "&jobs_stopped=2",
            )
            self.assertTrue(all(job.status == "blocked" for job in selected))
            self.assertEqual(untouched.status, "transcribed")

    def test_bulk_retry_restarts_all_blocked_and_failed_jobs(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            media_root = root / "media"
            media_root.mkdir()
            (media_root / "series").mkdir()

            with TestClient(create_app(self.settings(root, media_root))) as client:
                service = client.app.state.orchestrator
                service.stop()
                blocked = service.store.create(
                    job_id="blocked",
                    source_rel="blocked.mkv",
                    force_overwrite=False,
                    options={},
                )
                service.store.update(
                    blocked.id,
                    status="blocked",
                    blocked_stage="transcription",
                    error="stopped",
                )
                failed = service.store.create(
                    job_id="failed",
                    source_rel="failed.mkv",
                    force_overwrite=False,
                    options={},
                )
                service.store.update(
                    failed.id,
                    status="failed",
                    blocked_stage="translation",
                    error="failed",
                )
                queued = service.store.create(
                    job_id="queued",
                    source_rel="queued.mkv",
                    force_overwrite=False,
                    options={},
                )

                fragment = client.get("/jobs-fragment?folder=series")
                response = client.post(
                    "/jobs/retry-all",
                    data={"return_folder": "series"},
                    follow_redirects=False,
                )
                notice = client.get(response.headers["location"])
                refreshed = client.get("/jobs-fragment?folder=series")
                blocked = service.store.get(blocked.id)
                failed = service.store.get(failed.id)
                queued = service.store.get(queued.id)

            self.assertIn('action="/jobs/retry-all"', fragment.text)
            self.assertIn("전체 재시도 (2)", fragment.text)
            self.assertIn(
                'name="return_folder" value="series"',
                fragment.text,
            )
            self.assertEqual(response.status_code, 303)
            self.assertEqual(
                response.headers["location"],
                "/media?folder=series&jobs_retried=2",
            )
            self.assertIn("작업 2개를 재시도했습니다.", notice.text)
            self.assertIn("전체 재시도 (0)", refreshed.text)
            self.assertEqual(blocked.status, "queued")
            self.assertEqual(failed.status, "queued")
            self.assertEqual(queued.status, "queued")

    def test_filtered_job_list_retries_selected_jobs_across_pages(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            media_root = root / "media"
            media_root.mkdir()

            with TestClient(create_app(self.settings(root, media_root))) as client:
                service = client.app.state.orchestrator
                service.stop()
                blocked_jobs = []
                for index in range(25):
                    job = service.store.create(
                        job_id=f"blocked-{index:02d}",
                        source_rel=f"blocked-{index:02d}.mkv",
                        force_overwrite=False,
                        options={},
                    )
                    service.store.update(
                        job.id,
                        status="blocked",
                        blocked_stage="transcription",
                        error="stopped",
                    )
                    blocked_jobs.append(job)
                failed = service.store.create(
                    job_id="failed-not-selected",
                    source_rel="failed-not-selected.mkv",
                    force_overwrite=False,
                    options={},
                )
                service.store.update(failed.id, status="failed")

                blocked_page = client.get(
                    "/jobs?status_group=blocked&jobs_page=1"
                )
                failed_page = client.get("/jobs?status_group=failed")
                response = client.post(
                    "/jobs/retry-selected",
                    data={
                        "job_ids": [
                            blocked_jobs[0].id,
                            blocked_jobs[-1].id,
                        ],
                        "return_status_group": "blocked",
                        "return_jobs_page": "2",
                    },
                    follow_redirects=False,
                )
                notice = client.get(response.headers["location"])
                selected = [
                    service.store.get(blocked_jobs[0].id),
                    service.store.get(blocked_jobs[-1].id),
                ]
                untouched = service.store.get(blocked_jobs[1].id)
                failed = service.store.get(failed.id)
                global_response = client.post(
                    "/jobs/retry-all",
                    data={
                        "return_status_group": "blocked",
                        "return_jobs_page": "2",
                    },
                    follow_redirects=False,
                )
                global_notice = client.get(
                    global_response.headers["location"]
                )
                remaining_blocked = service.store.get(blocked_jobs[1].id)
                remaining_failed = service.store.get(failed.id)

            self.assertEqual(blocked_page.status_code, 200)
            self.assertEqual(
                blocked_page.text.count("data-retry-job-checkbox"),
                20,
            )
            self.assertEqual(
                blocked_page.text.count("data-retry-job-candidate"),
                25,
            )
            self.assertIn(
                'action="/jobs/retry-selected"',
                blocked_page.text,
            )
            self.assertIn("목록 전체 선택", blocked_page.text)
            self.assertIn("선택 재시도", blocked_page.text)
            self.assertIn("중단·실패 전체 재시도 (26)", blocked_page.text)
            self.assertEqual(
                failed_page.text.count("data-retry-job-checkbox"),
                1,
            )
            self.assertEqual(
                failed_page.text.count("data-retry-job-candidate"),
                1,
            )
            self.assertEqual(response.status_code, 303)
            self.assertEqual(
                response.headers["location"],
                "/jobs?status_group=blocked&jobs_page=1&jobs_retried=2",
            )
            self.assertIn(
                "작업 2개를 재시도했습니다.",
                notice.text,
            )
            self.assertTrue(all(job.status == "queued" for job in selected))
            self.assertEqual(untouched.status, "blocked")
            self.assertEqual(failed.status, "failed")
            self.assertEqual(global_response.status_code, 303)
            self.assertEqual(
                global_response.headers["location"],
                "/jobs?status_group=blocked&jobs_page=1&jobs_retried=24",
            )
            self.assertIn("작업 24개를 재시도했습니다.", global_notice.text)
            self.assertEqual(remaining_blocked.status, "queued")
            self.assertEqual(remaining_failed.status, "queued")

    def test_all_jobs_are_merged_and_paginated_by_creation_time(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            media_root = root / "media"
            media_root.mkdir()
            with TestClient(create_app(self.settings(root, media_root))) as client:
                service = client.app.state.orchestrator
                service.stop()
                for index in range(25):
                    active = service.store.create(
                        job_id=f"active-{index:02d}",
                        source_rel=f"active-{index:02d}.mp4",
                        force_overwrite=False,
                        options={},
                    )
                    service.store.update(active.id, status="transcription_running")
                    completed = service.store.create(
                        job_id=f"completed-{index:02d}",
                        source_rel=f"completed-{index:02d}.mp4",
                        force_overwrite=False,
                        options={},
                    )
                    service.store.update(completed.id, status="completed")

                first = client.get("/jobs-fragment?jobs_page=1")
                second = client.get("/jobs-fragment?jobs_page=2")

            self.assertEqual(first.text.count('class="recent-job-item'), 20)
            self.assertIn("active-24.mp4", first.text)
            self.assertIn("completed-24.mp4", first.text)
            self.assertNotIn("completed-00.mp4", first.text)
            self.assertIn("active-14.mp4", second.text)
            self.assertIn("completed-14.mp4", second.text)
            self.assertIn("jobs_page=2", first.text)

    def test_media_page_exposes_pipeline_buttons_and_queues_transcription(
        self,
    ) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            media_root = root / "media"
            media_root.mkdir()
            (media_root / "movie.mp4").write_bytes(b"media")
            with TestClient(
                create_app(self.settings(root, media_root))
            ) as client:
                service = client.app.state.orchestrator
                service.stop()
                dashboard = client.get("/media")
                response = client.post(
                    "/jobs",
                    data={
                        "source_rels": "movie.mp4",
                        "operation": "transcribe",
                    },
                    follow_redirects=False,
                )
                jobs = service.store.list_jobs()

            self.assertIn('name="operation" value="transcribe"', dashboard.text)
            self.assertIn('name="operation" value="compare"', dashboard.text)
            self.assertIn('name="operation" value="translate"', dashboard.text)
            self.assertIn('name="operation" value="full"', dashboard.text)
            self.assertIn(">전사</button>", dashboard.text)
            self.assertIn(">번역</button>", dashboard.text)
            self.assertIn(">전체</button>", dashboard.text)
            self.assertIn(
                "자동 (JAV 프롬프트 + WhisperJAV)",
                dashboard.text,
            )
            self.assertIn(
                'data-auto-prompt-category="jav" disabled',
                dashboard.text,
            )
            self.assertIn('value="jav" selected', dashboard.text)
            self.assertNotIn("오디오만 추출", dashboard.text)
            advanced_position = dashboard.text.index(
                '<details class="advanced-options wide">'
            )
            advanced_end = dashboard.text.index(
                "</details>",
                advanced_position,
            )
            prompt_position = dashboard.text.index("data-prompt-category")
            button_position = dashboard.text.index(
                'name="operation" value="transcribe"'
            )
            # 엔진·프롬프트 선택이 한 줄에 오고, 고급 옵션은 그 아래
            # 전체 폭으로, 실행 버튼은 마지막에 온다.
            self.assertLess(prompt_position, advanced_position)
            self.assertLess(advanced_end, button_position)
            self.assertEqual(response.status_code, 303)
            self.assertEqual(response.headers["location"], "/media?queued=1")
            self.assertEqual(jobs[0].operation, "transcribe")
            self.assertEqual(jobs[0].options["backend"], "whisperjav")
            self.assertNotIn("translation_prompt", jobs[0].options)

    def test_transcription_comparison_queues_four_engines_and_renders_results(
        self,
    ) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            media_root = root / "media"
            media_root.mkdir()
            (media_root / "movie.mp4").write_bytes(b"media")

            with TestClient(
                create_app(self.settings(root, media_root))
            ) as client:
                service = client.app.state.orchestrator
                service.stop()
                queued = client.post(
                    "/jobs",
                    data={
                        "source_rels": "movie.mp4",
                        "operation": "compare",
                    },
                    follow_redirects=False,
                )
                jobs = service.store.list_jobs(limit=None)
                comparison_id = str(jobs[0].options["comparison_id"])
                waiting = client.get(
                    f"/comparisons/{comparison_id}"
                )
                service.store.update(
                    jobs[0].id,
                    status="failed",
                    blocked_stage="transcription",
                    error="transcription failed",
                )
                failed_comparison = client.get(
                    f"/comparisons/{comparison_id}"
                )
                retry_response = client.post(
                    f"/comparisons/{comparison_id}/retry",
                    follow_redirects=False,
                )
                retry_notice = client.get(
                    retry_response.headers["location"]
                )
                retried_job = service.store.get(jobs[0].id)
                service.store.update(jobs[0].id, status="extracting")
                extracting = client.get(
                    f"/comparisons/{comparison_id}"
                )
                comparison_panel = client.get(
                    f"/comparisons/{comparison_id}/panel"
                )
                partial_comparison = None
                for index, job in enumerate(jobs):
                    backend = str(job.options["backend"])
                    audio = (
                        root
                        / "state"
                        / "jobs"
                        / job.id
                        / "audio.16k.wav"
                    )
                    audio.parent.mkdir(parents=True, exist_ok=True)
                    audio.write_bytes(b"wave")
                    transcript = (
                        root
                        / "state"
                        / "jobs"
                        / job.id
                        / "movie_translate.json"
                    )
                    transcript.parent.mkdir(parents=True, exist_ok=True)
                    transcript.write_text(
                        json.dumps(
                            {
                                "schema_version": 1,
                                "segments": [
                                    {
                                        "id": "segment-000001",
                                        "start": 0.0,
                                        "end": 1.0,
                                        "speaker": "SPEAKER_00",
                                        "text": f"{backend} 전사 결과",
                                    }
                                ],
                            },
                            ensure_ascii=False,
                        ),
                        encoding="utf-8",
                    )
                    service.store.update(
                        job.id,
                        status="transcription_completed",
                        audio_path=str(audio),
                        audio_sha256=f"audio-{job.id}",
                        transcript_path=str(transcript),
                    )
                    if index == 0:
                        partial_comparison = client.get(
                            f"/comparisons/{comparison_id}"
                        )
                comparison = client.get(
                    f"/comparisons/{comparison_id}"
                )
                history = client.get("/comparisons")
                history_fragment = client.get("/comparisons-fragment")
                invalid_rerun = client.post(
                    f"/comparisons/{comparison_id}/rerun",
                    data={
                        "kotoba_chunk_length_seconds": "0",
                        "whisperx_chunk_length_seconds": "42",
                    },
                )
                invalid_whisperx_rerun = client.post(
                    f"/comparisons/{comparison_id}/rerun",
                    data={
                        "kotoba_chunk_length_seconds": "21",
                        "whisperx_chunk_length_seconds": "31",
                    },
                )
                invalid_whisperjav_rerun = client.post(
                    f"/comparisons/{comparison_id}/rerun",
                    data={
                        "kotoba_chunk_length_seconds": "21",
                        "whisperx_chunk_length_seconds": "24",
                        "anime_max_group_duration_seconds": "0.1",
                    },
                )
                rerun_response = client.post(
                    f"/comparisons/{comparison_id}/rerun",
                    data={
                        "kotoba_chunk_length_seconds": "21",
                        "whisperx_chunk_length_seconds": "24",
                        "anime_max_group_duration_seconds": "2.7",
                        "qwen_max_group_duration_seconds": "4.2",
                    },
                    follow_redirects=False,
                )
                new_comparison_id = rerun_response.headers[
                    "location"
                ].split("/")[2].split("?")[0]
                rerun_notice = client.get(
                    rerun_response.headers["location"]
                )
                rerun_jobs = [
                    job
                    for job in service.store.list_jobs(limit=None)
                    if job.options.get("comparison_id")
                    == new_comparison_id
                ]
                original_after_rerun = client.get(
                    f"/comparisons/{comparison_id}"
                )
                history_after_rerun = client.get("/comparisons")
                detail = client.get(f"/jobs/{jobs[0].id}")
                selected_comparison_job = next(
                    job
                    for job in jobs
                    if job.options.get("backend") == "hybrid"
                )
                missing_translation_selection = client.post(
                    f"/comparisons/{comparison_id}/translate",
                    data={"prompt_category_id": "jav"},
                )
                duplicate_translation_selection = client.post(
                    f"/comparisons/{comparison_id}/translate",
                    data={
                        "job_ids": [jobs[0].id, jobs[1].id],
                        "prompt_category_id": "jav",
                    },
                )
                translation_response = client.post(
                    f"/comparisons/{comparison_id}/translate",
                    data={
                        "job_ids": selected_comparison_job.id,
                        "prompt_category_id": "jav",
                    },
                    follow_redirects=False,
                )
                translation_jobs = [
                    job
                    for job in service.store.list_jobs(limit=None)
                    if isinstance(
                        job.options.get("comparison_transcript_source"),
                        dict,
                    )
                ]
                preserved_comparison_job = service.store.get(
                    selected_comparison_job.id
                )
                translation_notice = client.get(
                    translation_response.headers["location"]
                )

            self.assertEqual(queued.status_code, 303)
            self.assertEqual(
                queued.headers["location"],
                f"/comparisons/{comparison_id}",
            )
            self.assertEqual(len(jobs), 4)
            self.assertEqual(
                {job.options["backend"] for job in jobs},
                {"whisperjav", "hybrid", "whisperx", "kotoba"},
            )
            self.assertEqual(
                {job.options["comparison_id"] for job in jobs},
                {comparison_id},
            )
            options_by_backend = {
                str(job.options["backend"]): job.options for job in jobs
            }
            self.assertIn("hybrid_rescue", options_by_backend["hybrid"])
            self.assertNotIn("hybrid_rescue", options_by_backend["whisperx"])
            self.assertNotIn("hybrid_rescue", options_by_backend["kotoba"])
            self.assertEqual(
                options_by_backend["whisperjav"]["whisperjav"],
                {
                    "recipe": "whisperjav-domain-ensemble-v1",
                    "anime_max_group_duration_seconds": 2.0,
                    "qwen_max_group_duration_seconds": 3.0,
                },
            )
            self.assertTrue(
                all(
                    job.options["comparison_schema_version"] == 2
                    for job in jobs
                )
            )
            self.assertEqual(
                options_by_backend["hybrid"]["hybrid_rescue"],
                {
                    "window_padding_sec": 5.0,
                    "max_word_duration_sec": 8.0,
                    "short_segment_duration_sec": 0.2,
                    "short_segment_cluster_window_sec": 5.0,
                    "short_segment_cluster_count": 3,
                    "speaker_debounce_sec": 0.1,
                    "kotoba_chunk_length_seconds": 15,
                    "whisperx_chunk_length_seconds": 30,
                    "rescue_scope": "windows",
                },
            )
            self.assertEqual(
                options_by_backend["whisperx"]["chunk_length_seconds"],
                30,
            )
            self.assertEqual(
                options_by_backend["kotoba"]["chunk_length_seconds"],
                15,
            )
            self.assertTrue(all(job.operation == "transcribe" for job in jobs))
            self.assertIn(
                f'data-update-url="/comparisons/{comparison_id}/panel"',
                waiting.text,
            )
            self.assertNotIn(
                "data-comparison-translation-form",
                waiting.text,
            )
            self.assertIn(
                f'action="/comparisons/{comparison_id}/rerun"',
                waiting.text,
            )
            self.assertIn(
                'name="kotoba_chunk_length_seconds"',
                waiting.text,
            )
            self.assertIn('value="15"', waiting.text)
            self.assertIn(
                'name="whisperx_chunk_length_seconds"',
                waiting.text,
            )
            self.assertIn('max="30"', waiting.text)
            self.assertIn('value="30"', waiting.text)
            self.assertIn(
                'name="anime_max_group_duration_seconds"', waiting.text
            )
            self.assertIn(
                'name="qwen_max_group_duration_seconds"', waiting.text
            )
            self.assertEqual(waiting.text.count("오디오 추출"), 1)
            self.assertEqual(waiting.text.count("전사 대기"), 4)
            self.assertIn("실패 작업 재시도 (1)", failed_comparison.text)
            self.assertEqual(retry_response.status_code, 303)
            self.assertEqual(
                retry_response.headers["location"],
                f"/comparisons/{comparison_id}?retried=1",
            )
            self.assertIn(
                "실패한 전사 작업 1개를 재시도했습니다.",
                retry_notice.text,
            )
            self.assertEqual(retried_job.status, "queued")
            self.assertIn(
                "comparison-audio-stage is-running",
                extracting.text,
            )
            self.assertEqual(extracting.text.count("오디오 추출"), 1)
            self.assertNotIn("오디오 추출 중", extracting.text)
            self.assertEqual(extracting.text.count("전사 대기"), 4)
            self.assertEqual(comparison_panel.status_code, 200)
            self.assertIn(
                "comparison-audio-stage is-running",
                comparison_panel.text,
            )
            self.assertIsNotNone(partial_comparison)
            self.assertIn(
                f'data-update-url="/comparisons/{comparison_id}/panel"',
                partial_comparison.text,
            )
            self.assertIn(
                f'action="/comparisons/{comparison_id}/translate"',
                partial_comparison.text,
            )
            self.assertIn(
                f'value="{jobs[0].id}"',
                partial_comparison.text,
            )
            self.assertIn("선택한 전사 결과로 번역", partial_comparison.text)
            self.assertNotIn(
                "완료된 결과 중 파일마다 사용할 엔진",
                partial_comparison.text,
            )
            self.assertEqual(comparison.status_code, 200)
            self.assertEqual(comparison.text.count("오디오 추출"), 1)
            self.assertIn("4 / 4개 전사 완료", comparison.text)
            self.assertIn("WhisperJAV", comparison.text)
            self.assertIn("하이브리드", comparison.text)
            self.assertIn("WhisperX", comparison.text)
            self.assertIn("Kotoba", comparison.text)
            self.assertIn("hybrid 전사 결과", comparison.text)
            self.assertIn("whisperx 전사 결과", comparison.text)
            self.assertIn("kotoba 전사 결과", comparison.text)
            self.assertIn("whisperjav 전사 결과", comparison.text)
            self.assertIn(
                f'action="/comparisons/{comparison_id}/translate"',
                comparison.text,
            )
            self.assertIn("선택한 전사 결과로 번역", comparison.text)
            self.assertIn('name="prompt_category_id"', comparison.text)
            for job in jobs:
                self.assertIn(f'value="{job.id}"', comparison.text)
            self.assertIn(
                'href="/comparisons" class="is-active"',
                comparison.text,
            )
            self.assertEqual(history.status_code, 200)
            self.assertIn("전사 비교 이력", history.text)
            self.assertIn(
                'href="/comparisons" class="is-active"',
                history.text,
            )
            self.assertNotIn(
                'href="/jobs" class="is-active"',
                history.text,
            )
            self.assertEqual(
                history.text.count('class="comparison-history-item'),
                1,
            )
            self.assertEqual(
                history.text.count('class="comparison-record-item'),
                1,
            )
            self.assertIn("movie.mp4", history.text)
            self.assertIn("완료 4", history.text)
            self.assertIn(
                f'href="/comparisons/{comparison_id}"',
                history.text,
            )
            self.assertEqual(history_fragment.status_code, 200)
            self.assertEqual(
                history_fragment.text.count(
                    'class="comparison-history-item'
                ),
                1,
            )
            self.assertEqual(
                history_fragment.text.count(
                    'class="comparison-record-item'
                ),
                1,
            )
            self.assertEqual(invalid_rerun.status_code, 400)
            self.assertIn(
                "Kotoba 청크는 1초 이상이어야 합니다.",
                invalid_rerun.text,
            )
            self.assertIn('value="0"', invalid_rerun.text)
            self.assertEqual(invalid_whisperx_rerun.status_code, 400)
            self.assertIn(
                "WhisperX 청크는 30초 이하여야 합니다.",
                invalid_whisperx_rerun.text,
            )
            self.assertEqual(invalid_whisperjav_rerun.status_code, 400)
            self.assertIn(
                "WhisperJAV 1차 그룹 길이: 0.5초 이상 30.0초 이하여야 합니다.",
                invalid_whisperjav_rerun.text,
            )
            self.assertEqual(rerun_response.status_code, 303)
            self.assertNotEqual(new_comparison_id, comparison_id)
            self.assertEqual(len(rerun_jobs), 4)
            self.assertTrue(
                all(job.status == "audio_ready" for job in rerun_jobs)
            )
            self.assertEqual(
                len({job.audio_path for job in rerun_jobs}),
                1,
            )
            self.assertTrue(
                all(
                    job.options["comparison_parent_id"] == comparison_id
                    for job in rerun_jobs
                )
            )
            self.assertTrue(
                all(
                    "comparison_audio_source_job_id" in job.options
                    for job in rerun_jobs
                )
            )
            rerun_options = {
                str(job.options["backend"]): job.options
                for job in rerun_jobs
            }
            self.assertEqual(
                rerun_options["hybrid"]["hybrid_rescue"][
                    "kotoba_chunk_length_seconds"
                ],
                21,
            )
            self.assertEqual(
                rerun_options["hybrid"]["hybrid_rescue"][
                    "whisperx_chunk_length_seconds"
                ],
                24,
            )
            self.assertEqual(
                rerun_options["kotoba"]["chunk_length_seconds"],
                21,
            )
            self.assertEqual(
                rerun_options["whisperx"]["chunk_length_seconds"],
                24,
            )
            self.assertEqual(
                rerun_options["whisperjav"]["whisperjav"][
                    "anime_max_group_duration_seconds"
                ],
                2.7,
            )
            self.assertEqual(
                rerun_options["whisperjav"]["whisperjav"][
                    "qwen_max_group_duration_seconds"
                ],
                4.2,
            )
            self.assertIn(
                "변경한 분할 설정으로 새 전사 비교를 시작했습니다.",
                rerun_notice.text,
            )
            self.assertIn(
                "기존 추출 오디오 1개를 재사용하며 전사부터 실행합니다.",
                rerun_notice.text,
            )
            self.assertIn(
                f'href="/comparisons/{comparison_id}"',
                rerun_notice.text,
            )
            self.assertIn("같은 미디어의 비교 기록", rerun_notice.text)
            self.assertIn("현재 기록", rerun_notice.text)
            self.assertNotIn("이전 결과", rerun_notice.text)
            self.assertIn(
                f'href="/comparisons/{new_comparison_id}"',
                original_after_rerun.text,
            )
            self.assertIn("같은 미디어의 비교 기록", original_after_rerun.text)
            self.assertNotIn("후속 실행", original_after_rerun.text)
            self.assertEqual(
                history_after_rerun.text.count(
                    'class="comparison-history-item'
                ),
                1,
            )
            self.assertEqual(
                history_after_rerun.text.count(
                    'class="comparison-record-item'
                ),
                2,
            )
            self.assertIn("비교 기록 2건", history_after_rerun.text)
            self.assertNotIn("재실행 · 이전", history_after_rerun.text)
            self.assertIn(
                f'/comparisons/{comparison_id}',
                detail.text,
            )
            self.assertEqual(translation_response.status_code, 303)
            self.assertEqual(missing_translation_selection.status_code, 400)
            self.assertIn(
                "번역에 사용할 전사 결과를 하나 이상 선택하세요.",
                missing_translation_selection.text,
            )
            self.assertEqual(duplicate_translation_selection.status_code, 400)
            self.assertIn(
                "파일마다 하나의 전사 결과만 선택하세요.",
                duplicate_translation_selection.text,
            )
            self.assertEqual(
                translation_response.headers["location"],
                "/jobs?translations_queued=1",
            )
            self.assertEqual(len(translation_jobs), 1)
            translated = translation_jobs[0]
            self.assertEqual(translated.status, "transcribed")
            self.assertEqual(translated.operation, "translate")
            self.assertTrue(translated.force_overwrite)
            self.assertEqual(
                translated.options["translation_prompt"]["category_id"],
                "jav",
            )
            self.assertNotIn("comparison_id", translated.options)
            self.assertEqual(
                translated.options["comparison_transcript_source"],
                {
                    "comparison_id": comparison_id,
                    "job_id": selected_comparison_job.id,
                    "backend": "hybrid",
                },
            )
            self.assertEqual(
                preserved_comparison_job.status,
                "transcription_completed",
            )
            self.assertNotEqual(
                translated.transcript_path,
                selected_comparison_job.transcript_path,
            )
            self.assertEqual(
                json.loads(
                    Path(translated.transcript_path).read_text(encoding="utf-8")
                )["job_id"],
                selected_comparison_job.id,
            )
            self.assertIn(
                "선택한 전사 작업 1개를 번역으로 전환했습니다.",
                translation_notice.text,
            )

    def test_transcription_comparison_accepts_completed_subtitled_media(
        self,
    ) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            media_root = root / "media"
            media_root.mkdir()
            (media_root / "movie.mp4").write_bytes(b"media")
            (media_root / "movie.ko.srt").write_text(
                "existing subtitle",
                encoding="utf-8",
            )

            with TestClient(
                create_app(self.settings(root, media_root))
            ) as client:
                service = client.app.state.orchestrator
                service.stop()
                completed = service.store.create(
                    job_id="completed",
                    source_rel="movie.mp4",
                    force_overwrite=False,
                    options={},
                    operation="full",
                )
                service.store.update(completed.id, status="completed")

                queued = client.post(
                    "/jobs",
                    data={
                        "source_rels": "movie.mp4",
                        "operation": "compare",
                    },
                    follow_redirects=False,
                )
                jobs = service.store.list_jobs(limit=None)

            comparison_jobs = [
                job for job in jobs if job.options.get("comparison_id")
            ]
            comparison_id = str(
                comparison_jobs[0].options["comparison_id"]
            )
            self.assertEqual(queued.status_code, 303)
            self.assertEqual(
                queued.headers["location"],
                f"/comparisons/{comparison_id}",
            )
            self.assertEqual(len(comparison_jobs), 4)
            self.assertTrue(
                all(not job.force_overwrite for job in comparison_jobs)
            )
            self.assertEqual(
                (media_root / "movie.ko.srt").read_text(encoding="utf-8"),
                "existing subtitle",
            )

    def test_comparison_retry_repairs_legacy_whisperx_chunk(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            media_root = root / "media"
            media_root.mkdir()
            (media_root / "movie.mp4").write_bytes(b"media")

            with TestClient(
                create_app(self.settings(root, media_root))
            ) as client:
                service = client.app.state.orchestrator
                service.stop()
                legacy = service.store.create(
                    job_id="legacy-comparison-whisperx",
                    source_rel="movie.mp4",
                    force_overwrite=False,
                    options={
                        "backend": "whisperx",
                        "chunk_length_seconds": 60,
                        "comparison_id": "legacy-comparison",
                    },
                    operation="transcribe",
                )
                service.store.update(
                    legacy.id,
                    status="blocked",
                    blocked_stage="transcription",
                    error="invalid input shape",
                )

                response = client.post(
                    "/comparisons/legacy-comparison/retry",
                    follow_redirects=False,
                )
                notice = client.get(response.headers["location"])
                retried = service.store.get(legacy.id)

            self.assertEqual(response.status_code, 303)
            self.assertEqual(
                response.headers["location"],
                "/comparisons/legacy-comparison?retried=1&adjusted=1",
            )
            self.assertEqual(retried.options["chunk_length_seconds"], 30)
            self.assertIn(
                "기존 WhisperX 청크 1개는 30초로 보정했습니다.",
                notice.text,
            )
            self.assertIn("<h1>전사 엔진 비교</h1>", notice.text)
            self.assertNotIn(
                "동일한 미디어를 다음 엔진으로 전사한 결과입니다",
                notice.text,
            )
            self.assertNotIn("<h3>WhisperJAV</h3>", notice.text)

    def test_job_list_selects_completed_transcripts_for_translation(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            media_root = root / "media"
            media_root.mkdir()
            for filename in ("first.mp4", "second.mp4", "waiting.mp4"):
                (media_root / filename).write_bytes(b"media")

            with TestClient(
                create_app(self.settings(root, media_root))
            ) as client:
                service = client.app.state.orchestrator
                service.stop()
                completed_jobs = []
                for index, filename in enumerate(
                    ("first.mp4", "second.mp4"),
                    start=1,
                ):
                    job = service.store.create(
                        job_id=f"transcribed-{index}",
                        source_rel=filename,
                        force_overwrite=False,
                        options={"backend": "hybrid"},
                        operation="transcribe",
                    )
                    artifact_dir = root / "state" / "jobs" / job.id
                    artifact_dir.mkdir(parents=True, exist_ok=True)
                    transcript = artifact_dir / f"{Path(filename).stem}_translate.json"
                    transcript.write_text(
                        json.dumps(
                            {
                                "schema_version": 1,
                                "job_id": f"remote-{index}",
                                "segments": [
                                    {
                                        "id": "segment-000001",
                                        "start": 0,
                                        "end": 1,
                                        "speaker": "SPEAKER_00",
                                        "text": f"원문 {index}",
                                    }
                                ],
                            },
                            ensure_ascii=False,
                        ),
                        encoding="utf-8",
                    )
                    service.store.update(
                        job.id,
                        status="transcription_completed",
                        transcript_path=str(transcript),
                    )
                    completed_jobs.append(service.store.get(job.id))
                waiting = service.store.create(
                    job_id="waiting",
                    source_rel="waiting.mp4",
                    force_overwrite=False,
                    options={},
                    operation="transcribe",
                )

                page = client.get("/jobs?status_group=completed")
                response = client.post(
                    "/jobs/translate-selected",
                    data={
                        "job_ids": [job.id for job in completed_jobs],
                        "prompt_category_id": "variety",
                        "return_status_group": "completed",
                        "return_jobs_page": "1",
                    },
                    follow_redirects=False,
                )
                all_jobs = service.store.list_jobs(limit=None)
                translated = [
                    service.store.get(job.id) for job in completed_jobs
                ]
                notice = client.get(response.headers["location"])
                stale_response = client.post(
                    "/jobs/translate-selected",
                    data={
                        "job_ids": completed_jobs[0].id,
                        "prompt_category_id": "variety",
                        "return_status_group": "completed",
                    },
                )

            self.assertEqual(page.status_code, 200)
            self.assertIn('action="/jobs/translate-selected"', page.text)
            self.assertEqual(
                page.text.count("data-translation-job-checkbox"),
                2,
            )
            for job in completed_jobs:
                self.assertIn(f'value="{job.id}"', page.text)
            self.assertNotIn(f'value="{waiting.id}"', page.text)
            self.assertIn("선택 번역 하기", page.text)
            self.assertIn("job-selection.js", page.text)
            self.assertEqual(response.status_code, 303)
            self.assertEqual(
                response.headers["location"],
                "/jobs?translations_queued=2",
            )
            self.assertEqual(len(all_jobs), 3)
            self.assertEqual(
                {job.id for job in translated},
                {job.id for job in completed_jobs},
            )
            self.assertTrue(all(job.status == "transcribed" for job in translated))
            self.assertTrue(
                all(job.operation == "full" for job in translated)
            )
            self.assertTrue(all(job.force_overwrite for job in translated))
            self.assertTrue(
                all(job.prompt_category_name == "버라이어티" for job in translated)
            )
            self.assertTrue(
                all(
                    job.transcript_path
                    and Path(job.transcript_path).is_file()
                    for job in translated
                )
            )
            self.assertIn(
                "선택한 전사 작업 2개를 번역으로 전환했습니다.",
                notice.text,
            )
            self.assertEqual(stale_response.status_code, 400)
            self.assertIn(
                "최신 전사 완료 작업만 번역할 수 있습니다.",
                stale_response.text,
            )

    def test_job_list_does_not_relabel_legacy_transcription_record(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            media_root = root / "media"
            media_root.mkdir()
            (media_root / "movie.mp4").write_bytes(b"media")

            with TestClient(
                create_app(self.settings(root, media_root))
            ) as client:
                service = client.app.state.orchestrator
                service.stop()
                transcription = service.store.create(
                    job_id="legacy-transcription",
                    source_rel="movie.mp4",
                    force_overwrite=False,
                    options={},
                    operation="transcribe",
                )
                service.store.update(
                    transcription.id,
                    status="transcription_completed",
                    transcript_path="/tmp/transcript.json",
                )
                translation = service.store.create(
                    job_id="legacy-translation",
                    source_rel="movie.mp4",
                    force_overwrite=True,
                    options={},
                    operation="translate",
                )
                service.store.update(translation.id, status="completed")

                page = client.get("/jobs?status_group=completed")

            self.assertEqual(page.status_code, 200)
            self.assertNotIn("번역 이행됨", page.text)
            self.assertIn("전사 완료", page.text)
            self.assertNotIn(
                'value="legacy-transcription"',
                page.text,
            )


if __name__ == "__main__":
    unittest.main()
