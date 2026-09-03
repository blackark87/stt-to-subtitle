#!/usr/bin/env python3
"""Stage the minimal Python package required by one container service."""

from __future__ import annotations

import argparse
import ast
from pathlib import Path
import shutil


ENTRYPOINTS = {
    "backend": "backend_api",
    "runtime": "runtime_api",
}
EXTRA_ROOTS = {
    "backend": {"migration_check", "storage_migration"},
    # Workers launched via ``python -m`` are not visible to the static import
    # closure rooted at runtime_api, so they must be staged explicitly.
    "runtime": {
        "kotoba_worker",
        "owsm_audit_worker",
        "speaker_worker",
        "stable_ts_worker",
    },
}
FORBIDDEN_MODULES = {
    "backend": {
        "web_app",
        "runtime_api",
        "kotoba",
        "hybrid_stt",
        "whisperx_worker",
        "whisperjav_worker",
        "speaker_worker",
    },
    "runtime": {
        "web_app",
        "backend_api",
        "backend_common",
        "backend_contracts",
        "backend_jobs_api",
        "backend_media_api",
        "backend_settings_api",
        "orchestrator",
        "job_store",
    },
}


def module_files(source: Path) -> dict[str, Path]:
    return {
        path.relative_to(source).with_suffix("").as_posix().replace("/", "."): path
        for path in source.rglob("*.py")
        if "__pycache__" not in path.parts
    }


def relative_imports(module: str, path: Path, modules: set[str]) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    package = module.split(".")[:-1]
    imported: set[str] = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.ImportFrom) or node.level < 1:
            continue
        base = package[: len(package) - node.level + 1]
        if node.module:
            base.extend(node.module.split("."))
        candidate = ".".join(base)
        if candidate in modules:
            imported.add(candidate)
        for alias in node.names:
            child = ".".join([*base, alias.name])
            if child in modules:
                imported.add(child)
    return imported


def dependency_closure(source: Path, entrypoints: set[str]) -> set[str]:
    files = module_files(source)
    available = set(files)
    pending = list(entrypoints)
    selected: set[str] = set()
    while pending:
        module = pending.pop()
        if module in selected:
            continue
        path = files.get(module)
        if path is None:
            raise ValueError(f"missing local module: {module}")
        selected.add(module)
        pending.extend(relative_imports(module, path, available) - selected)
    return selected


def copy_module(source: Path, destination: Path, module: str) -> None:
    relative = Path(*module.split(".")).with_suffix(".py")
    target = destination / relative
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(source / relative, target)
    parent = relative.parent
    while parent != Path("."):
        package_init = source / parent / "__init__.py"
        if package_init.is_file():
            init_target = destination / parent / "__init__.py"
            init_target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(package_init, init_target)
        parent = parent.parent


def stage(service: str, source: Path, destination: Path) -> set[str]:
    selected = dependency_closure(
        source,
        {ENTRYPOINTS[service], *EXTRA_ROOTS[service]},
    )
    forbidden = FORBIDDEN_MODULES[service] & selected
    if forbidden:
        raise ValueError(
            f"{service} dependency boundary includes forbidden modules: "
            f"{sorted(forbidden)}"
        )
    if destination.exists():
        shutil.rmtree(destination)
    destination.mkdir(parents=True)
    shutil.copy2(source / "__init__.py", destination / "__init__.py")
    for module in sorted(selected):
        copy_module(source, destination, module)
    if service == "runtime":
        vendor = source / "vendor" / "whisperjav"
        shutil.copytree(
            vendor,
            destination / "vendor" / "whisperjav",
            dirs_exist_ok=True,
            ignore=shutil.ignore_patterns("__pycache__", "*.pyc"),
        )
    return selected


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("service", choices=sorted(ENTRYPOINTS))
    parser.add_argument("source", type=Path)
    parser.add_argument("destination", type=Path)
    arguments = parser.parse_args()
    selected = stage(
        arguments.service,
        arguments.source.resolve(),
        arguments.destination.resolve(),
    )
    print(f"staged {arguments.service}: {len(selected)} modules")


if __name__ == "__main__":
    main()
