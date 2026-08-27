# Repository Guidelines

## Project Structure & Module Organization

Application code lives in `src/stt_to_subtitle/`. `audio.py` builds and runs FFmpeg extraction, `kotoba.py` loads the pinned Kotoba/Pyannote pipeline, `output.py` serializes transcripts, and `cli.py` coordinates the workflow. Tests under `tests/` mirror these responsibilities and use only short synthetic data or mocks. `Dockerfile.web` builds the static Nginx edge, `Dockerfile.backend` builds the orchestration API, `Dockerfile.runtime` builds the CUDA transcription service, and `Dockerfile.stt-runtime` prepares the reusable ML base image. `compose.yaml` runs all three services as one project. Dependency versions are locked in the `requirements*.txt` files. Keep input media, generated WAV files, transcripts, model caches, and IDE metadata out of Git.

## Architecture & Data Flow

Maintain explicit boundaries between static web serving, backend orchestration, audio extraction, speech transcription, transcript normalization, translation, and subtitle rendering. The Compose project runs Nginx, the FastAPI backend, and the CUDA transcription runtime as separate containers; only the runtime receives GPU devices and model caches. The Runtime image isolates Kotoba, WhisperX, and WhisperJAV in separate Python environments. The host STT API can run with Whisper on MPS and Pyannote on CPU. Correct model-relative timestamps at the transcript-normalization boundary; do not defer known model timestamp defects to translation. Subtitle rendering must preserve real cross-speaker overlap, replace overlapping lines from the same speaker, and emit both compatible SRT and styled ASS without changing translation IDs. Provider or model integrations should not leak credentials into output metadata.

## Build, Test, and Development Commands

- `make test` — run the standard-library unit test suite without downloading models.
- `make check` — compile Python sources and check changed files for whitespace errors.
- `./scripts/compose.sh --env-file .env.compose.example config` — validate Compose with the current non-root UID/GID.

## Coding Style & Naming Conventions

Target Python 3.11, use four-space indentation, type annotations, and focused modules. Follow `snake_case` for functions and variables, `PascalCase` for classes, and uppercase names for constants. Keep heavyweight ML imports inside runtime functions so unit tests remain fast. Prefer small validation functions and actionable error messages.

## Testing Guidelines

Use `unittest`; name files `test_<module>.py` and methods after observable behavior. Mock model loading and network access. Cover FFmpeg command construction, invalid speaker options, timestamp offsets, Unicode serialization, and output naming. Real quality tests must use legally available, short fixtures and must not commit copyrighted media or sensitive transcripts.

Do not rerun the full test suite unless application runtime code or substantive
test logic changed after the last complete run, the previous run was incomplete
or failed, or the user explicitly requests it. Dependency manifests, Dockerfiles,
Compose files, environment examples, workflows, documentation, version metadata,
branch moves, and commit/push/PR work do not by themselves justify another full
run. Validate those changes with the smallest direct check, such as dependency
resolution, an image build, a runtime import, Compose config rendering, or a
focused test. Reuse the latest successful full-suite result and never rerun the
full suite speculatively.

## Documentation Relevance

Keep repository documentation and pull requests limited to code behavior,
public configuration contracts, reproducible constraints, and verification
evidence. Omit deployment-local facts such as host addresses, machine roles,
hardware capacity, and where a test happened unless they materially change a
supported requirement or are necessary to reproduce a verified limitation.

## Security & Configuration

Pass `HF_TOKEN` only at runtime and never store it in source, images, logs, or committed environment files. Pin revisions whenever `trust_remote_code=True` is required. Preserve persistent state and model-cache volumes. Do not put credentials into Compose image build arguments.

## Agent Communication

Always communicate in Korean with a professional, precise expert tone.

## Agent Execution Environment

Agents work in a CLI-only environment for this repository. The Browser skill
and an interactive browser backend are unavailable, so do not invoke Browser
automation or claim browser-based visual validation. Validate web UI changes
with template and route tests, JavaScript syntax checks, static inspection, and
other CLI-accessible checks. Revisit this restriction only when the user
explicitly provides a browser-enabled environment.

## Commit & Pull Request Guidelines

Use concise, imperative commit subjects, optionally prefixed with `feat:`, `fix:`, or `docs:`. Pull requests must describe the affected pipeline stage, validation results, operational constraints, local image changes, and any output-format changes. Link relevant issues and include small, sanitized examples when useful.

Every new pull request must update the project version in `pyproject.toml`
exactly once and list the version change in the PR description. Follow Semantic
Versioning:

- increment `MAJOR` for backward-incompatible API, configuration, storage, or
  output-format changes that require migration;
- increment `MINOR` for backward-compatible features;
- increment `PATCH` for backward-compatible fixes, documentation, dependency,
  CI, or operational changes.

Select the next unused version relative to the latest `main` and existing
release tags. Do not increment the version again for follow-up commits on the
same PR unless the PR's compatibility scope changes.
