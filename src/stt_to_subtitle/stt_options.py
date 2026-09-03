"""Shared transcription request options without model runtime imports."""

from __future__ import annotations

from dataclasses import dataclass
from math import isfinite
from typing import Any, Mapping


DEFAULT_CHUNK_LENGTH_SECONDS = 60
DEFAULT_NOISE_FILTER_TRIGGER_LEVEL = 7.0

WHISPERX_MAX_CHUNK_LENGTH_SECONDS = 30
WHISPERX_MIN_BATCH_SIZE = 1
WHISPERX_MAX_BATCH_SIZE = 64
DEFAULT_SUBTITLE_SEGMENTATION = {
    "split_on_speaker_change": True,
    "split_on_parent_change": False,
    "max_gap_sec": 0.8,
    "max_duration_sec": 8.0,
    "max_chars": 36,
    "prefer_punctuation_boundary": True,
    "punctuation_boundary_mode": "all",
}
HYBRID_STABLE_SUBTITLE_SEGMENTATION = {
    "split_on_speaker_change": False,
    "split_on_parent_change": True,
    "max_gap_sec": 1.5,
    "max_duration_sec": 8.0,
    "max_chars": 80,
    "prefer_punctuation_boundary": True,
    "punctuation_boundary_mode": "sentence",
}
PUNCTUATION_BOUNDARY_MODES = {"all", "sentence", "none"}

WHISPERJAV_RECIPE = "whisperjav-domain-ensemble-v1"
DEFAULT_ANIME_MAX_GROUP_SECONDS = 2.0
DEFAULT_QWEN_MAX_GROUP_SECONDS = 3.0
MIN_MAX_GROUP_SECONDS = 0.5
MAX_MAX_GROUP_SECONDS = 30.0

RESCUE_SCOPES = {"full", "windows"}


@dataclass(frozen=True)
class TranscriptionOptions:
    batch_size: int = 1
    chunk_length_seconds: int = DEFAULT_CHUNK_LENGTH_SECONDS
    num_speakers: int | None = None
    min_speakers: int | None = None
    max_speakers: int | None = None
    add_punctuation: bool = False
    noise_filter: bool = True
    noise_filter_trigger_level: float = DEFAULT_NOISE_FILTER_TRIGGER_LEVEL
    short_span_policy: str = "observe"
    threads: int | None = None

    def validate(self) -> None:
        if self.batch_size < 1:
            raise ValueError("batch_size must be at least 1")
        if self.chunk_length_seconds < 1:
            raise ValueError("chunk_length_seconds must be at least 1")
        if self.noise_filter_trigger_level <= 0:
            raise ValueError("noise_filter_trigger_level must be positive")
        if self.threads is not None and self.threads < 1:
            raise ValueError("threads must be at least 1")
        if self.short_span_policy != "observe":
            raise ValueError(
                "short_span_policy currently supports only non-destructive "
                "'observe' mode"
            )
        speaker_values = (
            self.num_speakers,
            self.min_speakers,
            self.max_speakers,
        )
        if any(value is not None and value < 1 for value in speaker_values):
            raise ValueError("speaker counts must be at least 1")
        if self.num_speakers is not None and (
            self.min_speakers is not None or self.max_speakers is not None
        ):
            raise ValueError(
                "num_speakers cannot be combined with min_speakers or max_speakers"
            )
        if (
            self.min_speakers is not None
            and self.max_speakers is not None
            and self.min_speakers > self.max_speakers
        ):
            raise ValueError("min_speakers cannot exceed max_speakers")


@dataclass(frozen=True)
class WhisperXSegmentationOptions:
    """Configurable word-level subtitle boundaries."""

    split_on_speaker_change: bool = True
    split_on_parent_change: bool = False
    max_gap_sec: float | None = None
    max_duration_sec: float | None = None
    max_chars: int | None = None
    prefer_punctuation_boundary: bool = True
    punctuation_boundary_mode: str = "all"

    @classmethod
    def from_options(
        cls,
        options: Mapping[str, Any],
        *,
        defaults: Mapping[str, Any] | None = None,
    ) -> WhisperXSegmentationOptions:
        provided = options.get("subtitle_segmentation", {})
        if not isinstance(provided, Mapping):
            raise ValueError("subtitle_segmentation must be a JSON object")
        raw = dict(provided)
        if defaults is not None:
            raw = {**defaults, **raw}
        allowed = {
            "split_on_speaker_change",
            "split_on_parent_change",
            "max_gap_sec",
            "max_duration_sec",
            "max_chars",
            "prefer_punctuation_boundary",
            "punctuation_boundary_mode",
        }
        unknown = set(raw) - allowed
        if unknown:
            raise ValueError(
                f"unsupported subtitle_segmentation options: {sorted(unknown)}"
            )

        def optional_float(name: str) -> float | None:
            value = raw.get(name)
            if value is None:
                return None
            converted = float(value)
            if converted <= 0:
                raise ValueError(f"{name} must be positive or null")
            return converted

        max_chars_value = raw.get("max_chars")
        max_chars = int(max_chars_value) if max_chars_value is not None else None
        if max_chars is not None and max_chars < 1:
            raise ValueError("max_chars must be positive or null")
        for name in (
            "split_on_speaker_change",
            "split_on_parent_change",
            "prefer_punctuation_boundary",
        ):
            if name in raw and not isinstance(raw[name], bool):
                raise ValueError(f"{name} must be a JSON boolean")
        prefer_punctuation = raw.get("prefer_punctuation_boundary", True)
        raw_punctuation_mode = provided.get("punctuation_boundary_mode")
        if raw_punctuation_mode is not None:
            punctuation_mode = str(raw_punctuation_mode).strip().lower()
        elif "prefer_punctuation_boundary" in provided:
            punctuation_mode = "all" if prefer_punctuation else "none"
        else:
            punctuation_mode = str(
                raw.get("punctuation_boundary_mode", "all")
            )
        if punctuation_mode not in PUNCTUATION_BOUNDARY_MODES:
            raise ValueError(
                "punctuation_boundary_mode must be one of "
                f"{sorted(PUNCTUATION_BOUNDARY_MODES)}"
            )
        if (
            "prefer_punctuation_boundary" in provided
            and "punctuation_boundary_mode" in provided
            and prefer_punctuation != (punctuation_mode != "none")
        ):
            raise ValueError(
                "prefer_punctuation_boundary conflicts with "
                "punctuation_boundary_mode"
            )
        return cls(
            split_on_speaker_change=raw.get("split_on_speaker_change", True),
            split_on_parent_change=raw.get("split_on_parent_change", False),
            max_gap_sec=optional_float("max_gap_sec"),
            max_duration_sec=optional_float("max_duration_sec"),
            max_chars=max_chars,
            prefer_punctuation_boundary=punctuation_mode != "none",
            punctuation_boundary_mode=punctuation_mode,
        )


@dataclass(frozen=True)
class WhisperJAVOptions:
    """Validated controls exposed by the stable WhisperJAV recipe."""

    recipe: str = WHISPERJAV_RECIPE
    anime_max_group_duration_seconds: float = DEFAULT_ANIME_MAX_GROUP_SECONDS
    qwen_max_group_duration_seconds: float = DEFAULT_QWEN_MAX_GROUP_SECONDS

    @classmethod
    def from_options(cls, options: Mapping[str, Any]) -> WhisperJAVOptions:
        raw = options.get("whisperjav", {})
        if not isinstance(raw, Mapping):
            raise ValueError("whisperjav must be a JSON object")
        allowed = {
            "recipe",
            "anime_max_group_duration_seconds",
            "qwen_max_group_duration_seconds",
        }
        unknown = set(raw) - allowed
        if unknown:
            raise ValueError(
                f"unsupported whisperjav options: {sorted(unknown)}"
            )
        recipe = str(raw.get("recipe", WHISPERJAV_RECIPE)).strip()
        if recipe != WHISPERJAV_RECIPE:
            raise ValueError(
                f"whisperjav recipe must be '{WHISPERJAV_RECIPE}'"
            )

        def group_seconds(name: str, default: float) -> float:
            try:
                value = float(raw.get(name, default))
            except (TypeError, ValueError) as error:
                raise ValueError(f"{name} must be a number") from error
            if not MIN_MAX_GROUP_SECONDS <= value <= MAX_MAX_GROUP_SECONDS:
                raise ValueError(
                    f"{name} must be between {MIN_MAX_GROUP_SECONDS} and "
                    f"{MAX_MAX_GROUP_SECONDS}"
                )
            return value

        return cls(
            recipe=recipe,
            anime_max_group_duration_seconds=group_seconds(
                "anime_max_group_duration_seconds",
                DEFAULT_ANIME_MAX_GROUP_SECONDS,
            ),
            qwen_max_group_duration_seconds=group_seconds(
                "qwen_max_group_duration_seconds",
                DEFAULT_QWEN_MAX_GROUP_SECONDS,
            ),
        )


@dataclass(frozen=True)
class HybridRescueOptions:
    """Per-request thresholds and backend chunk lengths for hybrid STT."""

    window_padding_sec: float = 5.0
    max_word_duration_sec: float = 8.0
    short_segment_duration_sec: float = 0.2
    short_segment_cluster_window_sec: float = 5.0
    short_segment_cluster_count: int = 3
    speaker_debounce_sec: float = 0.1
    kotoba_chunk_length_seconds: int = 15
    whisperx_chunk_length_seconds: int = 30
    rescue_scope: str = "windows"
    stable_ts_regroup_enabled: bool = True

    @classmethod
    def from_options(cls, options: Mapping[str, Any]) -> HybridRescueOptions:
        raw = options.get("hybrid_rescue", {})
        if not isinstance(raw, Mapping):
            raise ValueError("hybrid_rescue must be a JSON object")
        allowed = {
            "window_padding_sec",
            "max_word_duration_sec",
            "short_segment_duration_sec",
            "short_segment_cluster_window_sec",
            "short_segment_cluster_count",
            "speaker_debounce_sec",
            "kotoba_chunk_length_seconds",
            "whisperx_chunk_length_seconds",
            "rescue_scope",
            "stable_ts_regroup_enabled",
        }
        unknown = set(raw) - allowed
        if unknown:
            raise ValueError(
                f"unsupported hybrid_rescue options: {sorted(unknown)}"
            )

        def positive_float(name: str, default: float) -> float:
            value = float(raw.get(name, default))
            if not isfinite(value) or value <= 0:
                raise ValueError(f"{name} must be positive")
            return value

        def positive_int(name: str, default: int, *, minimum: int = 1) -> int:
            value = int(raw.get(name, default))
            if value < minimum:
                raise ValueError(f"{name} must be at least {minimum}")
            return value

        defaults = cls()
        stable_ts_regroup_enabled = raw.get(
            "stable_ts_regroup_enabled",
            defaults.stable_ts_regroup_enabled,
        )
        if not isinstance(stable_ts_regroup_enabled, bool):
            raise ValueError(
                "stable_ts_regroup_enabled must be a JSON boolean"
            )
        normalized = cls(
            window_padding_sec=positive_float(
                "window_padding_sec", defaults.window_padding_sec
            ),
            max_word_duration_sec=positive_float(
                "max_word_duration_sec", defaults.max_word_duration_sec
            ),
            short_segment_duration_sec=positive_float(
                "short_segment_duration_sec",
                defaults.short_segment_duration_sec,
            ),
            short_segment_cluster_window_sec=positive_float(
                "short_segment_cluster_window_sec",
                defaults.short_segment_cluster_window_sec,
            ),
            short_segment_cluster_count=positive_int(
                "short_segment_cluster_count",
                defaults.short_segment_cluster_count,
                minimum=2,
            ),
            speaker_debounce_sec=positive_float(
                "speaker_debounce_sec", defaults.speaker_debounce_sec
            ),
            kotoba_chunk_length_seconds=positive_int(
                "kotoba_chunk_length_seconds",
                defaults.kotoba_chunk_length_seconds,
            ),
            whisperx_chunk_length_seconds=positive_int(
                "whisperx_chunk_length_seconds",
                defaults.whisperx_chunk_length_seconds,
            ),
            rescue_scope=str(raw.get("rescue_scope", defaults.rescue_scope)),
            stable_ts_regroup_enabled=stable_ts_regroup_enabled,
        )
        if normalized.rescue_scope not in RESCUE_SCOPES:
            raise ValueError(
                f"rescue_scope must be one of {sorted(RESCUE_SCOPES)}"
            )
        if (
            normalized.whisperx_chunk_length_seconds
            > WHISPERX_MAX_CHUNK_LENGTH_SECONDS
        ):
            raise ValueError(
                "whisperx_chunk_length_seconds must be at most "
                f"{WHISPERX_MAX_CHUNK_LENGTH_SECONDS}"
            )
        return normalized


@dataclass(frozen=True)
class OWSMAuditOptions:
    """Thresholds for finding clean WhisperX omissions with OWSM."""

    window_seconds: float = 30.0
    overlap_seconds: float = 5.0
    minimum_extra_characters: int = 20
    minimum_length_ratio: float = 1.35

    @classmethod
    def from_options(cls, options: Mapping[str, Any]) -> OWSMAuditOptions:
        raw = options.get("owsm_audit", {})
        if not isinstance(raw, Mapping):
            raise ValueError("owsm_audit must be a JSON object")
        allowed = {
            "window_seconds",
            "overlap_seconds",
            "minimum_extra_characters",
            "minimum_length_ratio",
        }
        unknown = set(raw) - allowed
        if unknown:
            raise ValueError(
                f"unsupported owsm_audit options: {sorted(unknown)}"
            )
        defaults = cls()
        window_seconds = float(
            raw.get("window_seconds", defaults.window_seconds)
        )
        overlap_seconds = float(
            raw.get("overlap_seconds", defaults.overlap_seconds)
        )
        minimum_extra_characters = int(
            raw.get(
                "minimum_extra_characters",
                defaults.minimum_extra_characters,
            )
        )
        minimum_length_ratio = float(
            raw.get("minimum_length_ratio", defaults.minimum_length_ratio)
        )
        if not isfinite(window_seconds) or window_seconds <= 0:
            raise ValueError("window_seconds must be positive")
        if (
            not isfinite(overlap_seconds)
            or overlap_seconds < 0
            or overlap_seconds >= window_seconds
        ):
            raise ValueError(
                "overlap_seconds must be non-negative and smaller than "
                "window_seconds"
            )
        if minimum_extra_characters < 1:
            raise ValueError("minimum_extra_characters must be positive")
        if not isfinite(minimum_length_ratio) or minimum_length_ratio <= 1:
            raise ValueError("minimum_length_ratio must be greater than 1")
        return cls(
            window_seconds=window_seconds,
            overlap_seconds=overlap_seconds,
            minimum_extra_characters=minimum_extra_characters,
            minimum_length_ratio=minimum_length_ratio,
        )
