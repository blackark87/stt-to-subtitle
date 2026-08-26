"""Stable names and paths for editable web JSON artifacts."""

from __future__ import annotations

from pathlib import Path

ARTIFACT_FILENAMES = {
    "transcript": "{stem}_translate.json",
    "translation": "{stem}_result_ko.json",
}


def artifact_filename(source_rel: str, kind: str) -> str:
    try:
        template = ARTIFACT_FILENAMES[kind]
    except KeyError as error:
        raise ValueError(f"unsupported artifact kind: {kind}") from error
    return template.format(stem=Path(source_rel).stem)


def artifact_path(
    jobs_dir: Path,
    job_id: str,
    source_rel: str,
    kind: str,
) -> Path:
    """Keep equal media basenames isolated inside their job directories."""
    return jobs_dir / job_id / artifact_filename(source_rel, kind)
