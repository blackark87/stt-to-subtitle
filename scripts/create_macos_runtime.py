#!/usr/bin/env python3
"""Export a self-contained macOS STT runtime outside the Git worktree."""

from __future__ import annotations

import argparse
from pathlib import Path
import shutil

RUNTIME_MARKER = "stt-to-subtitle macos runtime v1\n"


def _copy_runtime_files(repository_root: Path, target: Path) -> None:
    file_mappings = {
        repository_root / "pyproject.toml": target / "pyproject.toml",
        repository_root / "requirements-api.txt": (
            target / "requirements-api.txt"
        ),
        repository_root / "requirements-kotoba.txt": (
            target / "requirements-kotoba.txt"
        ),
        repository_root / ".env.macos.example": target / ".env.example",
        repository_root / "scripts" / "run-macos-stt.sh": (
            target / "scripts" / "run-macos-stt.sh"
        ),
        repository_root / "scripts" / "macos-runtime" / "setup.sh": (
            target / "setup.sh"
        ),
        repository_root / "scripts" / "macos-runtime" / "run.sh": (
            target / "run.sh"
        ),
        repository_root / "scripts" / "macos-runtime" / "README.md": (
            target / "README.md"
        ),
    }
    for source, destination in file_mappings.items():
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, destination)

    source_package = repository_root / "src" / "stt_to_subtitle"
    target_package = target / "src" / "stt_to_subtitle"
    if target_package.exists():
        shutil.rmtree(target_package)
    shutil.copytree(
        source_package,
        target_package,
        ignore=shutil.ignore_patterns("__pycache__", "*.pyc", "*.pyo"),
    )
    (target / ".stt-macos-runtime").write_text(
        RUNTIME_MARKER,
        encoding="utf-8",
    )


def create_runtime(
    target: Path,
    *,
    update: bool = False,
    repository_root: Path | None = None,
) -> Path:
    root = (
        repository_root or Path(__file__).resolve().parents[1]
    ).resolve()
    resolved_target = target.expanduser().resolve()

    if resolved_target == root or root in resolved_target.parents:
        raise ValueError("runtime target must be outside the Git repository")

    marker = resolved_target / ".stt-macos-runtime"
    if update:
        if not resolved_target.is_dir() or not marker.is_file():
            raise ValueError(
                "--update requires an existing exported macOS runtime"
            )
        if marker.read_text(encoding="utf-8") != RUNTIME_MARKER:
            raise ValueError("runtime marker is invalid or unsupported")
    elif resolved_target.exists() and any(resolved_target.iterdir()):
        raise ValueError(
            "runtime target already exists and is not empty; "
            "use --update only for a previously exported runtime"
        )
    else:
        resolved_target.mkdir(parents=True, exist_ok=True)

    _copy_runtime_files(root, resolved_target)
    return resolved_target


def main() -> None:
    parser = argparse.ArgumentParser(
        description=(
            "Copy the macOS STT application into a runtime directory "
            "outside the Git worktree."
        )
    )
    parser.add_argument(
        "target",
        type=Path,
        help="destination directory, for example ../stt-to-subtitle-python",
    )
    parser.add_argument(
        "--update",
        action="store_true",
        help=(
            "refresh application files in an exported runtime while "
            "preserving .env, .venv-macos, and var"
        ),
    )
    arguments = parser.parse_args()

    try:
        target = create_runtime(arguments.target, update=arguments.update)
    except (OSError, ValueError) as error:
        parser.error(str(error))

    action = "Updated" if arguments.update else "Created"
    print(f"{action} macOS STT runtime: {target}")
    print(f"Next: cd {target} && ./setup.sh")


if __name__ == "__main__":
    main()
