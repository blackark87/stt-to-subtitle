# Repository Guidelines

## Project Structure & Module Organization

Application code lives in `src/stt_to_subtitle/`. `audio.py` builds and runs FFmpeg extraction, `kotoba.py` loads the pinned Kotoba/Pyannote pipeline, `output.py` serializes transcripts, and `cli.py` coordinates the workflow. Tests under `tests/` mirror these responsibilities and use only short synthetic data or mocks. Container definitions are in `Dockerfile` and `compose.yaml`; dependency versions are locked in `requirements.txt`. Keep input media, generated WAV files, transcripts, model caches, and IDE metadata out of Git.

## Architecture & Data Flow

Maintain explicit boundaries between audio extraction, speech transcription, transcript normalization, translation, and subtitle rendering. The Mac transcription API runs natively with Whisper on MPS and Pyannote on CPU. Correct model-relative timestamps at the Mac transcript-normalization boundary; do not defer known model timestamp defects to translation. Subtitle rendering must preserve real cross-speaker overlap, replace overlapping lines from the same speaker, and emit both compatible SRT and styled ASS without changing translation IDs. The NAS Docker service orchestrates files, transcription, LM Studio translation, and subtitle rendering without running the ML models locally. Provider or model integrations should not leak credentials into output metadata.

## Build, Test, and Development Commands

- `make test` — run the standard-library unit test suite without downloading models.
- `make check` — compile Python sources and check changed files for whitespace errors.
- `docker compose --env-file .env.nas.example config` — validate the GHCR-only NAS Compose definition.

## Coding Style & Naming Conventions

Target Python 3.11, use four-space indentation, type annotations, and focused modules. Follow `snake_case` for functions and variables, `PascalCase` for classes, and uppercase names for constants. Keep heavyweight ML imports inside runtime functions so unit tests remain fast. Prefer small validation functions and actionable error messages.

## Testing Guidelines

Use `unittest`; name files `test_<module>.py` and methods after observable behavior. Mock model loading and network access. Cover FFmpeg command construction, invalid speaker options, timestamp offsets, Unicode serialization, and output naming. Real quality tests must use legally available, short fixtures and must not commit copyrighted media or sensitive transcripts.

## Security & Configuration

Pass `HF_TOKEN` only at runtime and never store it in source, images, logs, or committed environment files. Pin revisions whenever `trust_remote_code=True` is required. Preserve read-only input mounts and persistent model-cache volumes.

## Agent Communication

Always communicate in Korean with a professional, precise expert tone.

## Commit & Pull Request Guidelines

Use concise, imperative commit subjects, optionally prefixed with `feat:`, `fix:`, or `docs:`. Pull requests must describe the affected pipeline stage, validation results, operational constraints, and any output-format changes. Link relevant issues and include small, sanitized examples when useful.

Every new pull request must update the project version in `pyproject.toml`
exactly once and list both the version change and resulting GHCR tag in the PR
description. Follow Semantic Versioning:

- increment `MAJOR` for backward-incompatible API, configuration, storage, or
  output-format changes that require migration;
- increment `MINOR` for backward-compatible features;
- increment `PATCH` for backward-compatible fixes, documentation, dependency,
  CI, or operational changes.

Select the next unused version relative to the latest `main` and existing
release tags. Do not increment the version again for follow-up commits on the
same PR unless the PR's compatibility scope changes. The GHCR workflow must
publish `ghcr.io/blackark87/stt-to-subtitle:nas-X.Y.Z` from the
`pyproject.toml` version while retaining moving and immutable traceability tags
such as `nas-latest` and the full commit SHA.
