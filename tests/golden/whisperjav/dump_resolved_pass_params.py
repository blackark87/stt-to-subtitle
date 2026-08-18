"""Dump the resolved WhisperJAV pass parameters for our fixed recipe."""
import json

from whisperjav.ensemble import pass_worker as pw

RECIPE = {
    "pass1": {
        "pipeline": "qwen",
        "sensitivity": "aggressive",
        "scene_detector": "semantic",
        "speech_segmenter": "whisperseg",
        "language": "japanese",
        "qwen_params": {
            "generator_backend": "anime-whisper",
            "model_id": "<ANIME_MODEL_PATH>",
            "max_group_duration": 2.0,
            "timestamp_mode": "vad_only",
            "use_aligner": False,
            "context": "",
        },
    },
    "pass2": {
        "pipeline": "qwen",
        "sensitivity": "balanced",
        "scene_detector": "semantic",
        "speech_segmenter": "ten",
        "language": "japanese",
        "qwen_params": {
            "generator_backend": "qwen3",
            "model_id": "<QWEN_MODEL_PATH>",
            "max_group_duration": 3.0,
            "timestamp_mode": "vad_only",
            "use_aligner": False,
            "context": "",
        },
    },
}

out = {"segmenter_params": sorted(pw.SEGMENTER_PARAMS)}
for name, cfg in RECIPE.items():
    defaults = pw.prepare_qwen_params(cfg)
    overrides = {}
    backend = cfg["speech_segmenter"]
    gen = defaults.get("qwen_generator_backend")
    if backend == "whisperseg":
        if gen == "anime-whisper":
            from whisperjav.config.anime_whisper_vad import (
                apply_anime_segmenter_defaults,
            )
            apply_anime_segmenter_defaults(overrides, cfg["sensitivity"])
        elif gen == "qwen3":
            overrides.setdefault("threshold", 0.25)
    segmenter_config = pw.resolve_qwen_sensitivity(
        backend, cfg["sensitivity"], overrides or None
    )
    out[name] = {
        "qwen_defaults": {
            k: v for k, v in sorted(defaults.items())
        },
        "user_segmenter_overrides": overrides,
        "segmenter_config": dict(sorted(segmenter_config.items())),
    }

print("@@@JSON@@@" + json.dumps(out, ensure_ascii=False, sort_keys=True))
