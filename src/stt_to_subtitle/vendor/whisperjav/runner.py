"""In-process driver for the pinned two-pass WhisperJAV ensemble.

Replaces three upstream layers — ``pipelines/qwen_pipeline.py`` (1,254 lines),
``ensemble/pass_worker.py`` (1,855) and ``ensemble/orchestrator.py`` (896) —
with one function that runs both passes in the calling process.

What is gone relative to upstream, and why:

* **Process spawning.** Upstream ran each pass in a subprocess to dodge a
  ctranslate2 destructor crash. Our recipe never loads ctranslate2.
* **Phase 1 (ffmpeg extraction).** The caller already hands us a 16 kHz mono
  PCM16 WAV.
* **Scene detection per pass.** Both passes clustered the same audio with the
  same parameters; we do it once and share the scene WAVs.
* **Phase 3 (enhancement).** A no-op for ``enhancer="none"``.
* **Phase 4 (standalone VAD).** With ``aligner=None`` the only consumer of
  Phase-4 regions is ``HardeningConfig.speech_regions``, and the framer's own
  segmenter — built from identical kwargs — already supplies those as the
  orchestrator's priority-2 source. See VENDOR.md for the one residual
  difference between the two region sets.
* **Phase 9 (analytics JSON)** and the unused ``SRTPostProcessor``, whose mere
  construction pulled in ~3,500 lines of Whisper-era sanitizer code.

What is deliberately kept identical: the generator/cleaner/framer wiring, the
Phase 6-8 SRT path, and the merge. Those decide the output, and the point of
internalizing was to stop paying for the plumbing, not to change the result.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
import re
import time
from typing import Any, Callable

from stt_to_subtitle.vendor.whisperjav.ensemble.merge import MergeEngine
from stt_to_subtitle.vendor.whisperjav.presets import (
    LANGUAGE_CODE,
    MERGE_STRATEGY,
    PassConfig,
    SCENE_MAX_DURATION_SECONDS,
    SCENE_METHOD,
    SCENE_MIN_DURATION_SECONDS,
)
from stt_to_subtitle.vendor.whisperjav.utils.logger import logger

_TIMESTAMP_RE = re.compile(
    r"(?P<sh>\d{2}):(?P<sm>\d{2}):(?P<ss>\d{2})[,.](?P<sms>\d{3})"
    r"\s*-->\s*"
    r"(?P<eh>\d{2}):(?P<em>\d{2}):(?P<es>\d{2})[,.](?P<ems>\d{3})"
)


@dataclass(frozen=True)
class Cue:
    """One subtitle line with absolute timestamps."""

    start: float
    end: float
    text: str


@dataclass
class PassOutcome:
    """Result of a single ASR pass."""

    name: str
    status: str
    cues: list[Cue] = field(default_factory=list)
    srt_path: Path | None = None
    subtitle_count: int = 0
    filter_stats: dict[str, Any] = field(default_factory=dict)
    elapsed_seconds: float = 0.0
    error: str | None = None


@dataclass
class EnsembleResult:
    """Merged output of both passes."""

    cues: list[Cue]
    merged_srt_path: Path
    pass1: PassOutcome
    pass2: PassOutcome
    merge_stats: dict[str, Any]
    scene_count: int
    status: str
    stage_elapsed: dict[str, float]


def parse_srt_cues(path: Path) -> list[Cue]:
    """Read an SRT file into cues, tolerating both ``,`` and ``.`` decimals."""
    if not path.is_file():
        return []
    cues: list[Cue] = []
    for block in re.split(r"\r?\n\s*\r?\n", path.read_text(encoding="utf-8").strip()):
        lines = [line.rstrip() for line in block.splitlines()]
        index = next(
            (position for position, line in enumerate(lines) if "-->" in line),
            None,
        )
        if index is None:
            continue
        match = _TIMESTAMP_RE.fullmatch(lines[index].strip())
        if match is None:
            continue
        values = match.groupdict()

        def seconds(prefix: str) -> float:
            return (
                int(values[f"{prefix}h"]) * 3600
                + int(values[f"{prefix}m"]) * 60
                + int(values[f"{prefix}s"])
                + int(values[f"{prefix}ms"]) / 1000
            )

        text = "\n".join(lines[index + 1 :]).strip()
        start, end = seconds("s"), seconds("e")
        if text and end >= start:
            cues.append(Cue(start=start, end=end, text=text))
    return sorted(cues, key=lambda cue: (cue.start, cue.end, cue.text))


def detect_scenes(
    audio_path: Path,
    scenes_dir: Path,
    basename: str,
) -> list[tuple[Path, float, float, float]]:
    """Split the audio into semantic scenes once for both passes."""
    from stt_to_subtitle.vendor.whisperjav.modules.scene_detection_backends import (
        SceneDetectorFactory,
    )

    scenes_dir.mkdir(parents=True, exist_ok=True)
    # create() rather than safe_create(): the auditok fallback backend is not
    # vendored, so a silent downgrade would fail later and less clearly.
    detector = SceneDetectorFactory.create(
        SCENE_METHOD,
        min_duration=SCENE_MIN_DURATION_SECONDS,
        max_duration=SCENE_MAX_DURATION_SECONDS,
    )
    try:
        result = detector.detect_scenes(audio_path, scenes_dir, basename)
        scenes = result.to_legacy_tuples()
    finally:
        detector.cleanup()
    logger.info("scene detection produced %d scene(s)", len(scenes))
    return [(Path(path), start, end, duration) for path, start, end, duration in scenes]


def _build_subtitle_pipeline(config: PassConfig):
    """Compose the decoupled pipeline for one pass.

    Mirrors ``QwenPipeline._build_subtitle_pipeline`` for the two generator
    backends the recipe uses. ``aligner`` is always None because the recipe
    fixes ``timestamp_mode="vad_only"``, which also makes step-down retry
    unreachable (no alignment means no collapse to retry).
    """
    from stt_to_subtitle.vendor.whisperjav.modules.assembly_text_cleaner import (
        AssemblyCleanerConfig,
    )
    from stt_to_subtitle.vendor.whisperjav.modules.subtitle_pipeline.cleaners.factory import (
        TextCleanerFactory,
    )
    from stt_to_subtitle.vendor.whisperjav.modules.subtitle_pipeline.framers.factory import (
        TemporalFramerFactory,
    )
    from stt_to_subtitle.vendor.whisperjav.modules.subtitle_pipeline.generators.factory import (
        TextGeneratorFactory,
    )
    from stt_to_subtitle.vendor.whisperjav.modules.subtitle_pipeline.orchestrator import (
        DecoupledSubtitlePipeline,
    )
    from stt_to_subtitle.vendor.whisperjav.modules.subtitle_pipeline.types import (
        HardeningConfig,
        RegroupMode,
        TimestampMode,
    )

    framer = TemporalFramerFactory.create(
        "vad-grouped",
        segmenter_backend=config.segmenter_backend,
        max_group_duration_s=config.max_group_duration,
        chunk_threshold_s=config.chunk_threshold,
        segmenter_config=config.segmenter_kwargs(),
    )

    if config.generator_backend == "anime-whisper":
        generator = TextGeneratorFactory.create(
            "anime-whisper",
            model_id=config.model_id,
            device=config.device,
            dtype=config.dtype,
            no_repeat_ngram_size=config.no_repeat_ngram_size,
            max_new_tokens=config.max_new_tokens,
        )
        cleaner = TextCleanerFactory.create("anime-whisper")
    else:
        generator = TextGeneratorFactory.create(
            "qwen3",
            model_id=config.model_id,
            device=config.device,
            dtype=config.dtype,
            batch_size=config.batch_size,
            max_new_tokens=config.max_new_tokens,
            language=config.language,
            repetition_penalty=config.repetition_penalty,
            max_tokens_per_audio_second=config.max_tokens_per_audio_second,
            attn_implementation=config.attn_implementation,
        )
        cleaner = (
            TextCleanerFactory.create(
                "qwen3",
                config=AssemblyCleanerConfig(enabled=True),
                language=config.language,
            )
            if config.assembly_cleaner
            else TextCleanerFactory.create("passthrough")
        )

    return DecoupledSubtitlePipeline(
        framer=framer,
        generator=generator,
        cleaner=cleaner,
        aligner=None,
        hardening_config=HardeningConfig(
            timestamp_mode=TimestampMode.VAD_ONLY,
            regroup_mode=RegroupMode.OFF,
        ),
        language=config.language,
        context="",
        stepdown_config=None,
    )


def _apply_phase8_filters(
    srt_path: Path,
    config: PassConfig,
    subtitle_count: int,
) -> tuple[int, dict[str, Any]]:
    """Run the Phase-8 SRT filters in upstream's order, in place."""
    stats: dict[str, Any] = {}
    if subtitle_count <= 0:
        return subtitle_count, stats

    if config.anime_srt_filter:
        from stt_to_subtitle.vendor.whisperjav.modules.subtitle_pipeline.cleaners.anime_whisper import (
            AnimeWhisperCleaner,
        )

        anime = AnimeWhisperCleaner().filter_srt_file(srt_path)
        subtitle_count = anime["final_count"]
        stats["anime"] = anime

    if config.drop_nonverbal_lines and subtitle_count > 0:
        from stt_to_subtitle.vendor.whisperjav.modules.subtitle_pipeline.cleaners.nonverbal_line_filter import (
            NonverbalLineFilter,
        )

        nonverbal = NonverbalLineFilter().filter_srt_file(srt_path)
        subtitle_count = nonverbal["final_count"]
        stats["nonverbal"] = nonverbal

    if config.drop_nonlinguistic_utterances and subtitle_count > 0:
        from stt_to_subtitle.vendor.whisperjav.modules.subtitle_pipeline.cleaners.nonlinguistic_utterance_filter import (
            NonlinguisticUtteranceFilter,
        )

        nonlinguistic = NonlinguisticUtteranceFilter().filter_srt_file(srt_path)
        subtitle_count = nonlinguistic["final_count"]
        stats["nonlinguistic"] = nonlinguistic

    if config.cps_start_retimer and subtitle_count > 0:
        from stt_to_subtitle.vendor.whisperjav.modules.subtitle_pipeline.cleaners.cps_start_retimer import (
            CpsStartRetimer,
        )

        stats["cps_retimer"] = CpsStartRetimer(
            language=LANGUAGE_CODE
        ).retime_srt_file(srt_path)

    if config.resolve_scene_overlaps and subtitle_count > 0:
        from stt_to_subtitle.vendor.whisperjav.modules.subtitle_pipeline.cleaners.scene_overlap_resolver import (
            SceneOverlapResolver,
        )

        overlap = SceneOverlapResolver().resolve_srt_file(srt_path)
        subtitle_count = overlap["final_count"]
        stats["scene_overlap"] = overlap

    return subtitle_count, stats


def run_pass(
    config: PassConfig,
    scenes: list[tuple[Path, float, float, float]],
    work_dir: Path,
    *,
    debug_artifact_dir: Path | None = None,
) -> PassOutcome:
    """Transcribe every scene with one generator and stitch the result."""
    from stt_to_subtitle.vendor.whisperjav.modules.srt_stitching import SRTStitcher

    started = time.monotonic()
    pass_dir = work_dir / config.name
    scene_srt_dir = pass_dir / "scene_srts"
    scene_srt_dir.mkdir(parents=True, exist_ok=True)

    pipeline = _build_subtitle_pipeline(config)
    if debug_artifact_dir is not None:
        artifacts = debug_artifact_dir / config.name
        artifacts.mkdir(parents=True, exist_ok=True)
        pipeline.artifacts_dir = artifacts

    try:
        results = pipeline.process_scenes(
            scene_audio_paths=[scene[0] for scene in scenes],
            scene_durations=[scene[3] for scene in scenes],
            # None on purpose: the framer's regions become the hardening
            # source, which removes the second full VAD sweep per pass.
            scene_speech_regions=None,
        )
    finally:
        pipeline.cleanup()

    scene_srt_info: list[tuple[Path, float]] = []
    for index, (result, _diagnostics) in enumerate(results):
        if result is None or not result.segments:
            continue
        scene_srt_path = scene_srt_dir / f"scene_{index:04d}.srt"
        try:
            result.to_srt_vtt(
                str(scene_srt_path),
                word_level=False,
                segment_level=True,
                strip=True,
            )
        except Exception as error:  # noqa: BLE001 - one bad scene must not sink the pass
            logger.warning("scene %d SRT generation failed: %s", index, error)
            continue
        if scene_srt_path.is_file() and scene_srt_path.stat().st_size > 0:
            scene_srt_info.append((scene_srt_path, scenes[index][1]))

    stitched = pass_dir / "stitched.srt"
    if scene_srt_info:
        subtitle_count = SRTStitcher().stitch(scene_srt_info, stitched)
    else:
        stitched.write_text("", encoding="utf-8")
        subtitle_count = 0

    subtitle_count, filter_stats = _apply_phase8_filters(
        stitched, config, subtitle_count
    )

    return PassOutcome(
        name=config.name,
        status="completed",
        cues=parse_srt_cues(stitched),
        srt_path=stitched,
        subtitle_count=subtitle_count,
        filter_stats=filter_stats,
        elapsed_seconds=round(time.monotonic() - started, 3),
    )


def run_ensemble(
    audio_path: Path,
    *,
    pass1: PassConfig,
    pass2: PassConfig,
    work_dir: Path,
    debug_artifact_dir: Path | None = None,
    progress_callback: Callable[[str, int, int], None] | None = None,
) -> EnsembleResult:
    """Run both passes over shared scenes and merge them.

    A pass2 failure degrades to the pass1 result instead of failing the job,
    matching upstream's ensemble orchestrator.
    """
    work_dir.mkdir(parents=True, exist_ok=True)
    stage_elapsed: dict[str, float] = {}

    if progress_callback is not None:
        progress_callback("scene_detection", 1, 7)
    scene_started = time.monotonic()
    scenes = detect_scenes(audio_path, work_dir / "scenes", audio_path.stem)
    stage_elapsed["scene_detect"] = round(time.monotonic() - scene_started, 3)
    if not scenes:
        raise RuntimeError("scene detection produced no scenes")

    if progress_callback is not None:
        progress_callback("primary_transcription", 2, 7)
    first = run_pass(
        pass1, scenes, work_dir, debug_artifact_dir=debug_artifact_dir
    )
    stage_elapsed["pass1"] = first.elapsed_seconds

    try:
        if progress_callback is not None:
            progress_callback("secondary_transcription", 3, 7)
        second = run_pass(
            pass2, scenes, work_dir, debug_artifact_dir=debug_artifact_dir
        )
    except Exception as error:  # noqa: BLE001 - degraded is better than no output
        logger.warning("pass2 failed, degrading to the pass1 result: %s", error)
        second = PassOutcome(
            name=pass2.name,
            status="failed",
            error=f"{type(error).__name__}: {error}",
        )
    stage_elapsed["pass2"] = second.elapsed_seconds

    if progress_callback is not None:
        progress_callback("transcription_merge", 4, 7)
    merged = work_dir / "merged.srt"
    merge_started = time.monotonic()
    if second.status == "completed" and second.srt_path is not None:
        merge_stats = MergeEngine().merge(
            first.srt_path,
            second.srt_path,
            merged,
            strategy=MERGE_STRATEGY,
        )
        status = "completed"
    else:
        merged.write_text(
            first.srt_path.read_text(encoding="utf-8") if first.srt_path else "",
            encoding="utf-8",
        )
        merge_stats = {
            "pass1_count": first.subtitle_count,
            "pass2_count": 0,
            "merged_count": first.subtitle_count,
            "dedup_removed": 0,
            "strategy": MERGE_STRATEGY,
            "degraded": True,
        }
        status = "degraded"
    stage_elapsed["merge"] = round(time.monotonic() - merge_started, 3)

    return EnsembleResult(
        cues=parse_srt_cues(merged),
        merged_srt_path=merged,
        pass1=first,
        pass2=second,
        merge_stats=merge_stats,
        scene_count=len(scenes),
        status=status,
        stage_elapsed=stage_elapsed,
    )
