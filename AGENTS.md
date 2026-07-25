# Repository Guidelines

## Project Structure & Module Organization

Application code lives in `src/stt_to_subtitle/`. `audio.py` builds and runs FFmpeg extraction, `kotoba.py` loads the pinned Kotoba/Pyannote pipeline, `output.py` serializes transcripts, and `cli.py` coordinates the workflow. Tests under `tests/` mirror these responsibilities and use only short synthetic data or mocks. Container definitions are in `Dockerfile` and `compose.yaml`; dependency versions are locked in `requirements.txt`. Keep input media, generated WAV files, transcripts, model caches, and IDE metadata out of Git.

## Architecture & Data Flow

Maintain explicit boundaries between audio extraction, speech transcription, transcript normalization, translation, and subtitle rendering. The current test implementation stops after Japanese transcription and speaker diarization; do not add translation implicitly. Provider or model integrations should not leak credentials into output metadata. Docker on Apple Silicon currently runs this PyTorch pipeline on ARM64 CPU, not Metal/MPS.

## Build, Test, and Development Commands

- `make image` — build the `linux/arm64` Docker image for M1 systems.
- `make test` — run the standard-library unit test suite without downloading models.
- `make check` — compile Python sources and check changed files for whitespace errors.
- `docker compose config` — validate the Compose definition.
- `docker compose run --rm stt /data/sample.mkv --duration-seconds 300` — transcribe a five-minute test segment.

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
