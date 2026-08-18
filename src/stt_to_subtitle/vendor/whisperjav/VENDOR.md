# Vendored WhisperJAV

| | |
|---|---|
| Upstream | https://github.com/meizhong986/WhisperJAV |
| Commit | `a69a43244e14612ebd3a1eb417bdd0de6d494d0f` ("bump v1.9.0") |
| License | MIT — see `LICENSE`; third-party notices in `NOTICES` |
| Vendored on | 2026-08-18 |

## Why

The `whisperjav` pip package was previously installed into
`/opt/venvs/whisperjav` and invoked as `python -m whisperjav.main`, which
spawned a further subprocess per ASR pass. That put four nested interpreters on
the path of every job, each re-importing torch, transformers and stable-ts, and
made the two passes repeat work they could have shared. Vendoring the ~10k
lines our recipe actually executes lets `runner.py` drive both passes in one
process.

Only the modules the pinned recipe reaches were copied. The upstream GUI,
installer, translation, benchmark and BYOP trees, the nine unused pipelines,
the subtitle-sanitizer family, and every VAD / scene backend other than
WhisperSeg, TEN and semantic were left behind.

## Copied as-is

`modules/subtitle_pipeline/**` (orchestrator, hardening, reconstruction, types,
protocols, framers, generators, cleaners, aligners), `modules/qwen_asr.py`,
`modules/assembly_text_cleaner.py`, `modules/alignment_sentinel.py`,
`modules/japanese_postprocessor.py`, `modules/hallucination_remover.py`,
`modules/speech_segmentation/**` (whisperseg and ten backends),
`modules/scene_detection_backends/**` (semantic backend and adapter),
`vendor/semantic_audio_clustering.py`, `modules/audio_extraction.py`,
`modules/srt_stitching.py`, `utils/gpu_utils.py`, `ensemble/merge.py`,
`ensemble/utils.py`, `config/anime_whisper_vad.py`,
`config/sanitization_constants.py`, `data/hallucination_filters/**`.

Every `whisperjav.` import — statement or registry string — was rewritten to
`stt_to_subtitle.vendor.whisperjav.`.

## Local modifications

1. **`utils/logger.py` replaced.** Upstream installed its own stdout handler
   and set `propagate = False`. This worker's stdout is captured and surfaced
   only on failure, so those log lines were invisible in normal operation.
   Replaced with a four-line stdlib adapter exposing the same `logger` name and
   letting the host application own handlers and level.

2. **`modules/speech_segmentation/factory.py` — unknown parameters now raise.**
   `_sanitize_params` used to drop keys outside a backend's schema with a debug
   log, a sensible choice for GUI input but not for ours: our presets are code,
   so a typo silently changed VAD behaviour with no symptom. Unknown keys now
   raise `ValueError`.

3. **`modules/hallucination_remover.py` — bundled filter data only.** Upstream
   downloaded two JSON gists on every construction, cached them under
   `$HOME/.cache` and fell back to the bundled copies. That cache directory is
   not writable in our read-only container, so each pass-2 cleaner paid two
   network timeouts, and the phrase list varied with whatever the gist held
   that day. The vendored `data/hallucination_filters/` copies are now the only
   source, which makes the run reproducible and offline.

4. **Factory registries pruned.** Rows pointing at backends that were not
   vendored were removed so `available()` is truthful and an unsupported
   request fails immediately rather than deep inside an import.

## Behavioural difference to watch

`runner.py` drops upstream's Phase 4 (a standalone VAD sweep over every scene)
because, with `aligner=None`, its only consumer is
`HardeningConfig.speech_regions`, for which the orchestrator already accepts
the framer's own regions as a priority-2 source. The framer's segmenter is
built from identical kwargs, so the regions are the same **except** that the
framer reports only segments belonging to a group that survived its
`min_frame_duration_s = 0.1` filter, whereas Phase 4 reported every detected
segment. Sub-100 ms groups are therefore absent from the hardening input. The
golden regression in `tests/golden/whisperjav/` is what proves whether that
matters on real audio.

## Refreshing against upstream

Re-copy from a clean checkout of the pinned commit, re-apply the four
modifications above, then regenerate
`tests/golden/whisperjav/resolved_pass_params.json` with
`tests/golden/whisperjav/dump_resolved_pass_params.py` run inside a container
that still has the upstream package installed.
