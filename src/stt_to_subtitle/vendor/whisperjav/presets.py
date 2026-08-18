"""Fully resolved parameters for the pinned two-pass WhisperJAV recipe.

Upstream reaches these values through ``config/v4`` (a pydantic + YAML
configuration manager, ~3,100 lines plus fifteen YAML documents) layered with
``ensemble/pass_worker.prepare_qwen_params`` and
``config/anime_whisper_vad.apply_anime_segmenter_defaults``. Our recipe is
fixed — one sensitivity, one segmenter, one generator per pass — so the whole
resolution collapses to the constants below.

The values were captured by running the upstream resolver on the exact recipe
``whisperjav_worker.build_whisperjav_command`` used to emit, and
``tests/test_whisperjav_vendor.py`` pins them against the recorded fixture at
``tests/golden/whisperjav/resolved_pass_params.json``.

Only ``max_group_duration`` is caller-tunable, mirroring the
``anime_max_group_duration_seconds`` / ``qwen_max_group_duration_seconds``
options that ``WhisperJAVOptions`` already exposes.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

# Scene detection is shared by both passes (upstream ran it once per pass).
SCENE_METHOD = "semantic"
SCENE_MIN_DURATION_SECONDS = 12
SCENE_MAX_DURATION_SECONDS = 48

LANGUAGE = "japanese"
LANGUAGE_CODE = "ja"

# WhisperSeg, "aggressive", with the anime-whisper table lifted on top.
# Source: config/v4/ecosystems/tools/whisperseg-speech-segmentation.yaml
# (spec < aggressive preset) then config/anime_whisper_vad.py aggressive row.
PASS1_SEGMENTER_CONFIG: dict[str, Any] = {
    "chunk_threshold_s": 1.0,
    "force_split_mode": "chop",
    "gap_merge_ms": 350,
    "grow_floor": 0.05,
    "max_group_duration_s": 5,
    "max_speech_duration_s": 4.0,
    "min_silence_duration_ms": 80,
    "min_speech_duration_ms": 80,
    "neg_threshold": 0.01,
    "segmentation_decoder": "offline",
    "speech_pad_ms": 400,
    "speech_start_threshold": 0.3,
    "split_smooth_ms": 120,
    "threshold": 0.15,
}

# TEN VAD, "balanced".
# Source: config/v4/ecosystems/tools/ten-speech-segmentation.yaml.
PASS2_SEGMENTER_CONFIG: dict[str, Any] = {
    "chunk_threshold_s": 1.0,
    "end_pad_ms": 200,
    "hop_size": 256,
    "max_group_duration_s": 6,
    "max_speech_duration_s": 5,
    "min_silence_duration_ms": 150,
    "min_speech_duration_ms": 150,
    "start_pad_ms": 0,
    "threshold": 0.32,
}


@dataclass(frozen=True)
class PassConfig:
    """One resolved ASR pass.

    ``segmenter_config`` holds the per-sensitivity VAD parameters.  The four
    scalars below it are injected on top of that dict when the segmenter is
    built, exactly as ``QwenPipeline`` does for both the Phase-4 segmenter and
    the framer's own segmenter — which is why one VAD instance can serve both.
    """

    name: str
    generator_backend: str
    model_id: str
    segmenter_backend: str
    segmenter_config: dict[str, Any]
    max_group_duration: float
    chunk_threshold: float
    start_pad_ms: int
    end_pad_ms: int
    assembly_cleaner: bool
    max_new_tokens: int
    language: str = LANGUAGE
    device: str = "auto"
    dtype: str = "auto"
    batch_size: int = 1
    repetition_penalty: float = 1.1
    max_tokens_per_audio_second: float = 20.0
    attn_implementation: str = "auto"
    no_repeat_ngram_size: int = 0
    # Phase-8 filters. anime_srt_filter is upstream's anime-whisper-only
    # ellipsis dropper; the other three run for every Qwen backend.
    anime_srt_filter: bool = False
    drop_nonverbal_lines: bool = True
    drop_nonlinguistic_utterances: bool = True
    cps_start_retimer: bool = True
    resolve_scene_overlaps: bool = True

    def segmenter_kwargs(self) -> dict[str, Any]:
        """Return the factory kwargs for this pass's speech segmenter.

        The scalars override anything of the same name inside
        ``segmenter_config`` — upstream injects them after copying the dict so
        the constructor value wins over the YAML/sensitivity value.
        """
        kwargs = dict(self.segmenter_config)
        kwargs["max_group_duration_s"] = self.max_group_duration
        kwargs["chunk_threshold_s"] = self.chunk_threshold
        kwargs["start_pad_ms"] = self.start_pad_ms
        kwargs["end_pad_ms"] = self.end_pad_ms
        return kwargs


def pass1_config(
    model_id: str,
    *,
    max_group_duration: float,
) -> PassConfig:
    """anime-whisper over WhisperSeg regions, "aggressive" sensitivity."""
    return PassConfig(
        name="pass1",
        generator_backend="anime-whisper",
        model_id=model_id,
        segmenter_backend="whisperseg",
        segmenter_config=dict(PASS1_SEGMENTER_CONFIG),
        max_group_duration=max_group_duration,
        # From the anime-whisper aggressive row, not the WhisperSeg YAML.
        chunk_threshold=0.2,
        start_pad_ms=0,
        end_pad_ms=30,
        # anime-whisper uses its own SRT cleaner, never the assembly cleaner.
        assembly_cleaner=False,
        # Whisper's max_target_positions is 448; upstream caps at 444 because
        # forwarding the Qwen default of 4096 crashes the decoder.
        max_new_tokens=444,
        anime_srt_filter=True,
    )


def pass2_config(
    model_id: str,
    *,
    max_group_duration: float,
) -> PassConfig:
    """Qwen3-ASR text-only over TEN VAD regions, "balanced" sensitivity."""
    return PassConfig(
        name="pass2",
        generator_backend="qwen3",
        model_id=model_id,
        segmenter_backend="ten",
        segmenter_config=dict(PASS2_SEGMENTER_CONFIG),
        max_group_duration=max_group_duration,
        # QwenPipeline's own defaults; the recipe overrides neither.
        chunk_threshold=0.3,
        start_pad_ms=100,
        end_pad_ms=100,
        assembly_cleaner=True,
        max_new_tokens=4096,
    )


MERGE_STRATEGY = "pass1_primary"
