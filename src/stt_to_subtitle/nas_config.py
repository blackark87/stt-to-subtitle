"""Configuration and media-root access for the NAS web service."""

from __future__ import annotations

from dataclasses import dataclass
import os
from pathlib import Path
from xml.etree import ElementTree

MEDIA_EXTENSIONS = {
    ".aac",
    ".avi",
    ".flac",
    ".m4a",
    ".mkv",
    ".mov",
    ".mp3",
    ".mp4",
    ".mpeg",
    ".mpg",
    ".ogg",
    ".opus",
    ".ts",
    ".wav",
    ".webm",
}
POSTER_EXTENSIONS = {".jpg", ".jpeg", ".png", ".webp"}
MAX_NFO_BYTES = 2 * 1024 * 1024
IGNORED_DIRECTORY_NAMES = {
    ".ds_store",
    ".snapshot",
    ".snapshots",
    "#recycle",
    "@eadir",
    "@recycle",
    "@sharesnap",
    "@tmp",
}
IGNORED_FILE_NAMES = {
    ".ds_store",
    "desktop.ini",
    "thumbs.db",
}


def _is_ignored_directory(name: str) -> bool:
    return name.casefold() in IGNORED_DIRECTORY_NAMES


def _is_ignored_file(name: str) -> bool:
    return name.casefold() in IGNORED_FILE_NAMES


def _env_bool(name: str, default: bool = False) -> bool:
    value = os.environ.get(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


@dataclass(frozen=True)
class NASSettings:
    state_dir: Path
    media_root: Path
    admin_password: str
    session_secret: str
    stt_base_url: str
    stt_token: str
    lm_base_url: str
    lm_token: str
    lm_model: str
    secure_cookie: bool = False
    maximum_listed_files: int = 5000
    stt_poll_interval: float = 5.0
    translation_batch_segments: int = 30
    translation_batch_characters: int = 6000

    @classmethod
    def from_env(cls) -> NASSettings:
        return cls(
            state_dir=Path(
                os.environ.get("NAS_STATE_DIR", "/var/lib/stt")
            ).expanduser(),
            media_root=Path(
                os.environ.get("MEDIA_ROOT", "/media")
            ).expanduser(),
            admin_password=os.environ.get("NAS_ADMIN_PASSWORD", ""),
            session_secret=os.environ.get("NAS_SESSION_SECRET", ""),
            stt_base_url=os.environ.get("STT_BASE_URL", "").strip(),
            stt_token=os.environ.get("STT_API_TOKEN", ""),
            lm_base_url=os.environ.get("LM_STUDIO_BASE_URL", "").strip(),
            lm_token=os.environ.get("LM_STUDIO_TOKEN", ""),
            lm_model=os.environ.get("LM_STUDIO_MODEL", "").strip(),
            secure_cookie=_env_bool("NAS_SECURE_COOKIE"),
            maximum_listed_files=int(
                os.environ.get("NAS_MAXIMUM_LISTED_FILES", "5000")
            ),
            stt_poll_interval=float(
                os.environ.get("STT_POLL_INTERVAL_SECONDS", "5")
            ),
            translation_batch_segments=int(
                os.environ.get("TRANSLATION_BATCH_SEGMENTS", "30")
            ),
            translation_batch_characters=int(
                os.environ.get("TRANSLATION_BATCH_CHARACTERS", "6000")
            ),
        )

    def validate(self) -> None:
        required = {
            "STT_BASE_URL": self.stt_base_url,
            "LM_STUDIO_BASE_URL": self.lm_base_url,
            "LM_STUDIO_MODEL": self.lm_model,
        }
        missing = [name for name, value in required.items() if not value.strip()]
        if missing:
            raise ValueError(f"required settings are missing: {', '.join(missing)}")
        if self.admin_password.strip() and not self.session_secret.strip():
            raise ValueError(
                "NAS_SESSION_SECRET is required when NAS_ADMIN_PASSWORD is set"
            )
        if self.session_secret and len(self.session_secret) < 32:
            raise ValueError("NAS_SESSION_SECRET must be at least 32 characters")
        if self.maximum_listed_files < 1:
            raise ValueError("NAS_MAXIMUM_LISTED_FILES must be positive")
        if self.stt_poll_interval <= 0:
            raise ValueError("STT_POLL_INTERVAL_SECONDS must be positive")
        if (
            self.translation_batch_segments < 1
            or self.translation_batch_characters < 1
        ):
            raise ValueError("translation batch limits must be positive")


class MediaLibrary:
    def __init__(self, root: Path, maximum_files: int = 5000) -> None:
        self.root = root.resolve()
        self.maximum_files = maximum_files

    def resolve_file(self, relative_path: str) -> Path:
        if not relative_path or Path(relative_path).is_absolute():
            raise ValueError("media path must be relative")
        try:
            resolved = (self.root / relative_path).resolve(strict=True)
        except OSError as error:
            raise ValueError("media file does not exist") from error
        if not resolved.is_relative_to(self.root):
            raise ValueError("media path escapes MEDIA_ROOT")
        resolved_relative = resolved.relative_to(self.root)
        if any(
            _is_ignored_directory(part)
            for part in resolved_relative.parts[:-1]
        ):
            raise ValueError("media path is inside an excluded metadata folder")
        if not resolved.is_file():
            raise ValueError("media path is not a file")
        if resolved.suffix.lower() not in MEDIA_EXTENSIONS:
            raise ValueError("unsupported media file extension")
        return resolved

    def resolve_poster(self, relative_path: str) -> Path:
        if not relative_path or Path(relative_path).is_absolute():
            raise ValueError("poster path must be relative")
        try:
            resolved = (self.root / relative_path).resolve(strict=True)
        except OSError as error:
            raise ValueError("poster file does not exist") from error
        if not resolved.is_relative_to(self.root):
            raise ValueError("poster path escapes MEDIA_ROOT")
        if (
            not resolved.is_file()
            or resolved.is_symlink()
            or resolved.suffix.lower() not in POSTER_EXTENSIONS
        ):
            raise ValueError("unsupported poster file")
        return resolved

    def resolve_directory(self, relative_path: str = "") -> Path:
        if Path(relative_path).is_absolute():
            raise ValueError("folder path must be relative")
        try:
            resolved = (self.root / relative_path).resolve(strict=True)
        except OSError as error:
            raise ValueError("media folder does not exist") from error
        if not resolved.is_relative_to(self.root):
            raise ValueError("folder path escapes MEDIA_ROOT")
        resolved_relative = resolved.relative_to(self.root)
        if any(
            _is_ignored_directory(part)
            for part in resolved_relative.parts
        ):
            raise ValueError("metadata folders are excluded")
        if not resolved.is_dir():
            raise ValueError("media folder is not a directory")
        return resolved

    def browse(self, relative_directory: str = "") -> dict[str, object]:
        if not self.root.is_dir():
            if relative_directory:
                raise ValueError("media folder does not exist")
            return {
                "current_folder": "",
                "parent_folder": None,
                "breadcrumbs": [{"name": "미디어 루트", "path": ""}],
                "folders": [],
                "files": [],
            }

        directory = self.resolve_directory(relative_directory)
        current_relative = directory.relative_to(self.root)
        current_folder = (
            "" if current_relative == Path(".") else current_relative.as_posix()
        )
        parent_relative = current_relative.parent
        parent_folder: str | None
        if current_relative == Path("."):
            parent_folder = None
        elif parent_relative == Path("."):
            parent_folder = ""
        else:
            parent_folder = parent_relative.as_posix()

        breadcrumbs: list[dict[str, str]] = [
            {"name": "미디어 루트", "path": ""}
        ]
        accumulated = Path()
        for part in current_relative.parts:
            if part == ".":
                continue
            accumulated /= part
            breadcrumbs.append(
                {"name": part, "path": accumulated.as_posix()}
            )

        folders: list[dict[str, object]] = []
        files: list[dict[str, object]] = []
        try:
            children = sorted(
                directory.iterdir(),
                key=lambda path: path.name.casefold(),
            )
        except OSError as error:
            raise ValueError("media folder cannot be read") from error
        for child in children:
            if child.is_symlink() or (
                child.is_dir() and _is_ignored_directory(child.name)
            ) or (
                child.is_file() and _is_ignored_file(child.name)
            ):
                continue
            if child.is_dir():
                folders.append(
                    {
                        "name": child.name,
                        "path": child.relative_to(self.root).as_posix(),
                    }
                )
            elif (
                child.is_file()
                and child.suffix.lower() in MEDIA_EXTENSIONS
                and len(files) < self.maximum_files
            ):
                files.append(self._describe_media(child))

        return {
            "current_folder": current_folder,
            "parent_folder": parent_folder,
            "breadcrumbs": breadcrumbs,
            "folders": folders,
            "files": files,
        }

    def _describe_media(self, path: Path) -> dict[str, object]:
        relative = path.relative_to(self.root).as_posix()
        subtitle = path.with_name(f"{path.stem}.ko.srt")
        nfo_path = self._find_nfo(path)
        title: str | None = None
        poster_path: str | None = None
        if nfo_path is not None:
            title, poster_references = self._read_nfo(nfo_path)
            poster = self._find_poster(path, nfo_path, poster_references)
            if poster is not None:
                poster_path = poster.relative_to(self.root).as_posix()
        relative_parent = path.relative_to(self.root).parent.as_posix()
        return {
            "path": relative,
            "name": path.name,
            "directory": "" if relative_parent == "." else relative_parent,
            "size": path.stat().st_size,
            "has_subtitle": subtitle.is_file(),
            "has_nfo": nfo_path is not None,
            "title": title or path.stem,
            "poster_path": poster_path,
        }

    def _find_nfo(self, source_path: Path) -> Path | None:
        for candidate in (
            source_path.with_suffix(".nfo"),
            source_path.parent / "movie.nfo",
        ):
            if candidate.is_file() and not candidate.is_symlink():
                return candidate
        return None

    def _read_nfo(self, nfo_path: Path) -> tuple[str | None, list[str]]:
        try:
            if nfo_path.stat().st_size > MAX_NFO_BYTES:
                return None, []
            root = ElementTree.parse(nfo_path).getroot()
        except (ElementTree.ParseError, OSError):
            return None, []

        title: str | None = None
        poster_references: list[str] = []
        for element in root.iter():
            tag = element.tag.rsplit("}", 1)[-1].lower()
            value = (element.text or "").strip()
            if not value:
                continue
            if tag == "title" and title is None:
                title = value
            elif tag == "poster":
                poster_references.append(value)
            elif (
                tag == "thumb"
                and element.attrib.get("aspect", "").strip().lower() == "poster"
            ):
                poster_references.append(value)
        return title, poster_references

    def _find_poster(
        self,
        source_path: Path,
        nfo_path: Path,
        poster_references: list[str],
    ) -> Path | None:
        for reference in poster_references:
            poster = self._resolve_local_poster(nfo_path.parent, reference)
            if poster is not None:
                return poster

        for stem in (
            f"{source_path.stem}-poster",
            source_path.stem,
            "poster",
            "folder",
            "cover",
        ):
            for extension in POSTER_EXTENSIONS:
                candidate = source_path.parent / f"{stem}{extension}"
                if candidate.is_file() and not candidate.is_symlink():
                    return candidate.resolve()
        return None

    def _resolve_local_poster(
        self,
        base_directory: Path,
        reference: str,
    ) -> Path | None:
        normalized = reference.strip().replace("\\", "/")
        if (
            not normalized
            or "://" in normalized
            or normalized.startswith(("data:", "/"))
        ):
            return None
        candidate = (base_directory / normalized).resolve()
        if not candidate.is_relative_to(self.root):
            return None
        if (
            candidate.is_file()
            and not candidate.is_symlink()
            and candidate.suffix.lower() in POSTER_EXTENSIONS
        ):
            return candidate
        return None
