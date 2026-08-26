"""Reference-aware auditing and cleanup for pipeline work artifacts."""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
from pathlib import Path
import time
from typing import Collection


@dataclass(frozen=True)
class ArtifactReference:
    """One persisted path that protects a work artifact from cleanup."""

    path: str
    kind: str
    record_id: str
    expected: bool = True


@dataclass(frozen=True)
class MissingArtifact:
    relative_path: str
    kind: str
    record_id: str


@dataclass(frozen=True)
class OrphanArtifact:
    path: Path
    relative_path: str
    size: int
    modified_at: float
    modified_at_ns: int
    device: int
    inode: int
    cleanup_eligible: bool


@dataclass(frozen=True)
class ArtifactAudit:
    root: Path
    total_files: int
    total_bytes: int
    referenced_files: int
    external_references: int
    missing_references: tuple[MissingArtifact, ...]
    orphan_files: tuple[OrphanArtifact, ...]
    skipped_symlinks: int
    minimum_age_days: int

    @property
    def orphan_bytes(self) -> int:
        return sum(artifact.size for artifact in self.orphan_files)

    @property
    def cleanup_eligible_files(self) -> tuple[OrphanArtifact, ...]:
        return tuple(
            artifact
            for artifact in self.orphan_files
            if artifact.cleanup_eligible
        )

    @property
    def cleanup_eligible_bytes(self) -> int:
        return sum(
            artifact.size for artifact in self.cleanup_eligible_files
        )

    @property
    def cleanup_token(self) -> str:
        entries = (
            (
                f"{artifact.relative_path}\0{artifact.size}\0"
                f"{artifact.modified_at_ns}\0{artifact.device}\0"
                f"{artifact.inode}"
            )
            for artifact in self.cleanup_eligible_files
        )
        return hashlib.sha256("\n".join(entries).encode("utf-8")).hexdigest()

    def to_view(self, *, sample_limit: int = 20) -> dict[str, object]:
        missing = self.missing_references[:sample_limit]
        orphans = self.orphan_files[:sample_limit]
        return {
            "root": str(self.root),
            "total_files": self.total_files,
            "total_bytes": self.total_bytes,
            "referenced_files": self.referenced_files,
            "external_references": self.external_references,
            "missing_count": len(self.missing_references),
            "missing_sample": missing,
            "orphan_count": len(self.orphan_files),
            "orphan_bytes": self.orphan_bytes,
            "orphan_sample": orphans,
            "cleanup_eligible_count": len(self.cleanup_eligible_files),
            "cleanup_eligible_bytes": self.cleanup_eligible_bytes,
            "cleanup_token": self.cleanup_token,
            "skipped_symlinks": self.skipped_symlinks,
            "minimum_age_days": self.minimum_age_days,
        }


@dataclass(frozen=True)
class ArtifactCleanup:
    removed_files: int
    removed_bytes: int
    failed_files: tuple[str, ...]


def audit_artifacts(
    root: Path,
    references: Collection[ArtifactReference],
    *,
    minimum_age_days: int,
    now: float | None = None,
) -> ArtifactAudit:
    """Classify files below ``root`` without following symbolic links."""
    if minimum_age_days < 1:
        raise ValueError("artifact cleanup age must be at least 1 day")
    audited_at = time.time() if now is None else now
    root = root.resolve()
    reference_by_path: dict[Path, ArtifactReference] = {}
    external_references = 0
    for reference in references:
        candidate = Path(reference.path)
        if not candidate.is_absolute():
            candidate = root / candidate
        resolved = candidate.resolve(strict=False)
        try:
            resolved.relative_to(root)
        except ValueError:
            external_references += 1
            continue
        current = reference_by_path.get(resolved)
        if current is None or (reference.expected and not current.expected):
            reference_by_path[resolved] = reference

    files_by_path: dict[Path, tuple[Path, int, float, int, int, int]] = {}
    skipped_symlinks = 0
    total_bytes = 0
    if root.is_dir():
        for candidate in root.rglob("*"):
            if candidate.is_symlink():
                skipped_symlinks += 1
                continue
            try:
                if not candidate.is_file():
                    continue
                stat = candidate.stat()
                resolved = candidate.resolve(strict=False)
                resolved.relative_to(root)
            except (FileNotFoundError, OSError, ValueError):
                continue
            files_by_path[resolved] = (
                candidate,
                stat.st_size,
                stat.st_mtime,
                stat.st_mtime_ns,
                stat.st_dev,
                stat.st_ino,
            )
            total_bytes += stat.st_size

    minimum_age_seconds = minimum_age_days * 24 * 60 * 60
    orphan_files = []
    for resolved, file_details in files_by_path.items():
        if resolved in reference_by_path:
            continue
        candidate, size, modified_at, modified_at_ns, device, inode = (
            file_details
        )
        orphan_files.append(
            OrphanArtifact(
                path=candidate,
                relative_path=candidate.relative_to(root).as_posix(),
                size=size,
                modified_at=modified_at,
                modified_at_ns=modified_at_ns,
                device=device,
                inode=inode,
                cleanup_eligible=(
                    audited_at - modified_at >= minimum_age_seconds
                ),
            )
        )
    orphan_files.sort(key=lambda artifact: artifact.relative_path.casefold())

    missing_references = []
    for path, reference in reference_by_path.items():
        if not reference.expected or path in files_by_path:
            continue
        missing_references.append(
            MissingArtifact(
                relative_path=path.relative_to(root).as_posix(),
                kind=reference.kind,
                record_id=reference.record_id,
            )
        )
    missing_references.sort(
        key=lambda artifact: artifact.relative_path.casefold()
    )

    return ArtifactAudit(
        root=root,
        total_files=len(files_by_path),
        total_bytes=total_bytes,
        referenced_files=sum(
            1 for path in reference_by_path if path in files_by_path
        ),
        external_references=external_references,
        missing_references=tuple(missing_references),
        orphan_files=tuple(orphan_files),
        skipped_symlinks=skipped_symlinks,
        minimum_age_days=minimum_age_days,
    )


def cleanup_orphan_artifacts(audit: ArtifactAudit) -> ArtifactCleanup:
    """Remove only files already classified as cleanup-eligible orphans."""
    removed_files = 0
    removed_bytes = 0
    failed_files = []
    root = audit.root.resolve()
    for artifact in audit.cleanup_eligible_files:
        path = artifact.path
        try:
            if path.is_symlink():
                continue
            resolved = path.resolve(strict=False)
            resolved.relative_to(root)
            stat = path.stat(follow_symlinks=False)
            unchanged = (
                stat.st_size == artifact.size
                and stat.st_mtime_ns == artifact.modified_at_ns
                and stat.st_dev == artifact.device
                and stat.st_ino == artifact.inode
            )
            if not path.is_file() or not unchanged:
                continue
            path.unlink()
        except (FileNotFoundError, OSError, ValueError):
            failed_files.append(artifact.relative_path)
            continue
        removed_files += 1
        removed_bytes += artifact.size
    return ArtifactCleanup(
        removed_files=removed_files,
        removed_bytes=removed_bytes,
        failed_files=tuple(failed_files),
    )
