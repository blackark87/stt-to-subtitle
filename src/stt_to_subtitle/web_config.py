"""Configuration and media-root access for the web service."""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass
import math
import os
from pathlib import Path
import re
import subprocess
from urllib.parse import urlsplit
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
MULTIPART_STEM_PATTERN = re.compile(
    r"^(?P<base>.+)-pt(?P<part>.+)$",
    re.IGNORECASE,
)


def _is_ignored_directory(name: str) -> bool:
    return name.casefold() in IGNORED_DIRECTORY_NAMES


def _is_ignored_file(name: str) -> bool:
    return name.casefold() in IGNORED_FILE_NAMES


def _is_hidden_media_file(name: str) -> bool:
    return name.casefold().endswith("-trailer.mp4")


def _multipart_part_sort_key(value: str) -> tuple[tuple[int, object], ...]:
    return tuple(
        (0, int(token)) if token.isdigit() else (1, token.casefold())
        for token in re.split(r"(\d+)", value)
        if token
    )


def _nfo_actor_name(element: "ElementTree.Element") -> str:
    """Return an actor name from either a nested <name> or the element text."""
    for child in element:
        if child.tag.rsplit("}", 1)[-1].lower() == "name":
            return (child.text or "").strip()
    return (element.text or "").strip()


def group_multipart_media(
    media_files: Sequence[dict[str, object]],
) -> list[dict[str, object]]:
    """Collapse sibling ``*-pt*`` files into one display-only media item."""
    identities: dict[int, tuple[tuple[str, str], str, str]] = {}
    members_by_key: dict[tuple[str, str], list[dict[str, object]]] = {}
    for media in media_files:
        relative = Path(str(media.get("path", "")))
        match = MULTIPART_STEM_PATTERN.fullmatch(relative.stem)
        if match is None:
            continue
        base = match.group("base")
        part = match.group("part")
        parent = "" if relative.parent == Path(".") else relative.parent.as_posix()
        key = (parent.casefold(), base.casefold())
        identities[id(media)] = (key, base, part)
        members_by_key.setdefault(key, []).append(media)

    collapsed_keys = {
        key for key, members in members_by_key.items() if len(members) > 1
    }
    emitted: set[tuple[str, str]] = set()
    grouped: list[dict[str, object]] = []
    for media in media_files:
        identity = identities.get(id(media))
        if identity is None or identity[0] not in collapsed_keys:
            grouped.append(media)
            continue
        key, base, _part = identity
        if key in emitted:
            continue
        emitted.add(key)
        members = sorted(
            members_by_key[key],
            key=lambda item: _multipart_part_sort_key(
                identities[id(item)][2]
            ),
        )
        paths = [str(item["path"]) for item in members]
        parent = Path(paths[0]).parent
        display_name = base
        display_path = (
            display_name
            if parent == Path(".")
            else (parent / display_name).as_posix()
        )
        durations = [item.get("duration_seconds") for item in members]
        duration = (
            sum(float(value) for value in durations)
            if all(isinstance(value, (int, float)) for value in durations)
            else None
        )
        common_nfo_titles = {
            str(item.get("title", "")).casefold(): str(item.get("title", ""))
            for item in members
            if item.get("has_nfo") and str(item.get("title", "")).strip()
        }
        title = (
            next(iter(common_nfo_titles.values()))
            if len(common_nfo_titles) == 1
            else base
        )
        poster_path = next(
            (
                str(item["poster_path"])
                for item in members
                if item.get("poster_path")
            ),
            None,
        )
        grouped.append(
            {
                "path": display_path,
                "paths": paths,
                "parts": members,
                "name": display_name,
                "title": title,
                "size": sum(int(item.get("size", 0)) for item in members),
                "duration_seconds": duration,
                "has_subtitle": all(
                    bool(item.get("has_subtitle")) for item in members
                ),
                "has_nfo": any(bool(item.get("has_nfo")) for item in members),
                "poster_path": poster_path,
                "actors": list(
                    dict.fromkeys(
                        name
                        for item in members
                        for name in (item.get("actors") or ())
                    )
                ),
                "multipart": True,
                "part_count": len(members),
            }
        )
    return grouped


def probe_media_duration(path: Path) -> float | None:
    """Return container duration without decoding media content."""
    try:
        result = subprocess.run(
            [
                "ffprobe",
                "-v",
                "error",
                "-show_entries",
                "format=duration",
                "-of",
                "default=noprint_wrappers=1:nokey=1",
                str(path),
            ],
            check=False,
            capture_output=True,
            text=True,
            timeout=10,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if result.returncode != 0:
        return None
    try:
        duration = float(result.stdout.strip())
    except ValueError:
        return None
    if not math.isfinite(duration) or duration <= 0:
        return None
    return duration


def _env_bool(name: str, default: bool = False) -> bool:
    value = os.environ.get(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def normalize_server_url(value: str, setting: str) -> str:
    normalized = value.strip().rstrip("/")
    parsed = urlsplit(normalized)
    if (
        parsed.scheme not in {"http", "https"}
        or not parsed.netloc
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
    ):
        raise ValueError(
            f"{setting} must be an http(s) URL without credentials, "
            "query parameters, or fragments"
        )
    return normalized


def _first_configured_env(*names: str) -> str:
    for name in names:
        value = os.environ.get(name, "")
        if value.strip():
            return value
    return ""


@dataclass(frozen=True)
class RemoteServerSettings:
    stt_base_url: str
    stt_token: str
    lm_base_url: str
    lm_token: str
    lm_model: str
    translation_workers: int = 1

    @property
    def is_complete(self) -> bool:
        return all(
            value.strip()
            for value in (
                self.stt_base_url,
                self.lm_base_url,
                self.lm_model,
            )
        )

    def normalized(self) -> RemoteServerSettings:
        missing = [
            name
            for name, value in (
                ("STT_BASE_URL", self.stt_base_url),
                ("OPENAI_COMPATIBLE_BASE_URL", self.lm_base_url),
                ("OPENAI_COMPATIBLE_MODEL", self.lm_model),
            )
            if not value.strip()
        ]
        if missing:
            raise ValueError(
                f"required server settings are missing: {', '.join(missing)}"
            )
        if not 1 <= self.translation_workers <= 8:
            raise ValueError("TRANSLATION_WORKERS must be between 1 and 8")
        return RemoteServerSettings(
            stt_base_url=normalize_server_url(
                self.stt_base_url,
                "STT_BASE_URL",
            ),
            stt_token=self.stt_token,
            lm_base_url=normalize_server_url(
                self.lm_base_url,
                "OPENAI_COMPATIBLE_BASE_URL",
            ),
            lm_token=self.lm_token,
            lm_model=self.lm_model.strip(),
            translation_workers=self.translation_workers,
        )


@dataclass(frozen=True)
class WebSettings:
    state_dir: Path
    media_root: Path
    admin_password: str
    session_secret: str
    stt_base_url: str
    stt_token: str
    lm_base_url: str
    lm_token: str
    lm_model: str
    gpu_prometheus_url: str = ""
    gpu_prometheus_token: str = ""
    gpu_metrics_refresh_seconds: float = 10.0
    gpu_metrics_timeout_seconds: float = 3.0
    secure_cookie: bool = False
    maximum_listed_files: int = 5000
    translation_batch_segments: int = 30
    translation_batch_characters: int = 6000

    @classmethod
    def from_env(cls) -> WebSettings:
        return cls(
            state_dir=Path(
                os.environ.get("WEB_STATE_DIR", "/var/lib/stt")
            ).expanduser(),
            media_root=Path(
                os.environ.get("MEDIA_ROOT", "/media")
            ).expanduser(),
            admin_password=os.environ.get("WEB_ADMIN_PASSWORD", ""),
            session_secret=os.environ.get("WEB_SESSION_SECRET", ""),
            stt_base_url=os.environ.get("STT_BASE_URL", "").strip(),
            stt_token=os.environ.get("STT_API_TOKEN", ""),
            lm_base_url=_first_configured_env(
                "OPENAI_COMPATIBLE_BASE_URL",
                "LM_STUDIO_BASE_URL",
            ).strip(),
            lm_token=_first_configured_env(
                "OPENAI_COMPATIBLE_TOKEN",
                "LM_STUDIO_TOKEN",
            ),
            lm_model=_first_configured_env(
                "OPENAI_COMPATIBLE_MODEL",
                "LM_STUDIO_MODEL",
            ).strip(),
            gpu_prometheus_url=os.environ.get(
                "GPU_PROMETHEUS_URL",
                "",
            ).strip().rstrip("/"),
            gpu_prometheus_token=os.environ.get(
                "GPU_PROMETHEUS_TOKEN",
                "",
            ),
            gpu_metrics_refresh_seconds=float(
                os.environ.get("GPU_METRICS_REFRESH_SECONDS", "10")
            ),
            gpu_metrics_timeout_seconds=float(
                os.environ.get("GPU_METRICS_TIMEOUT_SECONDS", "3")
            ),
            secure_cookie=_env_bool("WEB_SECURE_COOKIE"),
            maximum_listed_files=int(
                os.environ.get("WEB_MAXIMUM_LISTED_FILES", "5000")
            ),
            translation_batch_segments=int(
                os.environ.get("TRANSLATION_BATCH_SEGMENTS", "30")
            ),
            translation_batch_characters=int(
                os.environ.get("TRANSLATION_BATCH_CHARACTERS", "6000")
            ),
        )

    def validate(self) -> None:
        if self.admin_password.strip() and not self.session_secret.strip():
            raise ValueError(
                "WEB_SESSION_SECRET is required when WEB_ADMIN_PASSWORD is set"
            )
        if self.session_secret and len(self.session_secret) < 32:
            raise ValueError("WEB_SESSION_SECRET must be at least 32 characters")
        if self.maximum_listed_files < 1:
            raise ValueError("WEB_MAXIMUM_LISTED_FILES must be positive")
        if self.gpu_prometheus_url:
            normalize_server_url(
                self.gpu_prometheus_url,
                "GPU_PROMETHEUS_URL",
            )
        if self.gpu_metrics_refresh_seconds <= 0:
            raise ValueError("GPU_METRICS_REFRESH_SECONDS must be positive")
        if self.gpu_metrics_timeout_seconds <= 0:
            raise ValueError("GPU_METRICS_TIMEOUT_SECONDS must be positive")
        if (
            self.translation_batch_segments < 1
            or self.translation_batch_characters < 1
        ):
            raise ValueError("translation batch limits must be positive")

    def remote_servers(self) -> RemoteServerSettings:
        return RemoteServerSettings(
            stt_base_url=self.stt_base_url,
            stt_token=self.stt_token,
            lm_base_url=self.lm_base_url,
            lm_token=self.lm_token,
            lm_model=self.lm_model,
            translation_workers=1,
        )


class MediaLibrary:
    def __init__(
        self,
        root: Path,
        maximum_files: int = 5000,
        *,
        duration_probe: Callable[[Path], float | None] | None = None,
    ) -> None:
        self.root = root.resolve()
        self.maximum_files = maximum_files
        self._duration_probe = duration_probe
        self._duration_cache: dict[
            Path,
            tuple[int, int, float | None],
        ] = {}

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
                child.is_file()
                and (
                    _is_ignored_file(child.name)
                    or _is_hidden_media_file(child.name)
                )
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

    def search_by_title(
        self,
        query: str,
        relative_directory: str = "",
    ) -> dict[str, object]:
        """Find media by display title below the selected directory."""
        normalized_query = query.strip()
        if not normalized_query:
            return self.browse(relative_directory)
        if len(normalized_query) > 200:
            raise ValueError("검색어는 200자 이하로 입력하세요.")
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
        location = self._directory_location(directory)
        matching_paths: list[Path] = []
        pending = [directory]
        folded_query = normalized_query.casefold()
        while pending and len(matching_paths) < self.maximum_files:
            current = pending.pop()
            try:
                children = sorted(
                    current.iterdir(),
                    key=lambda path: path.name.casefold(),
                    reverse=True,
                )
            except OSError as error:
                raise ValueError("media folder cannot be read") from error
            for child in children:
                if child.is_symlink():
                    continue
                if child.is_dir():
                    if not _is_ignored_directory(child.name):
                        pending.append(child)
                    continue
                if (
                    not child.is_file()
                    or child.suffix.lower() not in MEDIA_EXTENSIONS
                    or _is_ignored_file(child.name)
                    or _is_hidden_media_file(child.name)
                ):
                    continue
                if folded_query not in self._media_title(child).casefold():
                    continue
                matching_paths.append(child)
                if len(matching_paths) >= self.maximum_files:
                    break

        matching_paths.sort(
            key=lambda path: path.relative_to(self.root).as_posix().casefold()
        )
        return {
            **location,
            "folders": [],
            "files": [self._describe_media(path) for path in matching_paths],
        }

    def list_media_recursive(
        self,
        relative_directories: Sequence[str],
    ) -> list[str]:
        """Return supported media below selected folders without following links."""
        selected: set[str] = set()
        pending: list[Path] = []
        for relative in dict.fromkeys(relative_directories):
            raw_relative = Path(relative)
            candidate = self.root
            for part in raw_relative.parts:
                candidate /= part
                if candidate.is_symlink():
                    raise ValueError("symbolic-link folders cannot be selected")
            pending.append(self.resolve_directory(relative))
        visited: set[Path] = set()
        while pending:
            directory = pending.pop()
            if directory in visited:
                continue
            visited.add(directory)
            try:
                children = sorted(
                    directory.iterdir(),
                    key=lambda path: path.name.casefold(),
                    reverse=True,
                )
            except OSError as error:
                raise ValueError("media folder cannot be read") from error
            for child in children:
                if child.is_symlink():
                    continue
                if child.is_dir():
                    if not _is_ignored_directory(child.name):
                        pending.append(child)
                    continue
                if (
                    not child.is_file()
                    or child.suffix.lower() not in MEDIA_EXTENSIONS
                    or _is_ignored_file(child.name)
                    or _is_hidden_media_file(child.name)
                ):
                    continue
                selected.add(child.relative_to(self.root).as_posix())
                if len(selected) > self.maximum_files:
                    raise ValueError(
                        "한 번에 등록할 수 있는 파일 수를 초과했습니다."
                    )
        return sorted(selected, key=str.casefold)

    def _describe_media(self, path: Path) -> dict[str, object]:
        relative = path.relative_to(self.root).as_posix()
        file_stat = path.stat()
        srt_subtitle = path.with_name(f"{path.stem}.ko.srt")
        ass_subtitle = path.with_name(f"{path.stem}.ko.ass")
        nfo_path = self._find_nfo(path)
        title: str | None = None
        poster_path: str | None = None
        actors: list[str] = []
        if nfo_path is not None:
            title, poster_references, actors = self._read_nfo(nfo_path)
            poster = self._find_poster(path, nfo_path, poster_references)
            if poster is not None:
                poster_path = poster.relative_to(self.root).as_posix()
        return {
            "path": relative,
            "name": path.name,
            "size": file_stat.st_size,
            "duration_seconds": self._media_duration(path, file_stat),
            "has_subtitle": srt_subtitle.is_file() or ass_subtitle.is_file(),
            "has_nfo": nfo_path is not None,
            "title": title or path.stem,
            "poster_path": poster_path,
            "actors": actors,
        }

    def _media_title(self, path: Path) -> str:
        nfo_path = self._find_nfo(path)
        if nfo_path is not None:
            title, _, _ = self._read_nfo(nfo_path)
            if title:
                return title
        return path.stem

    def _directory_location(self, directory: Path) -> dict[str, object]:
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
        return {
            "current_folder": current_folder,
            "parent_folder": parent_folder,
            "breadcrumbs": breadcrumbs,
        }

    def _media_duration(
        self,
        path: Path,
        file_stat: os.stat_result,
    ) -> float | None:
        if self._duration_probe is None:
            return None
        cached = self._duration_cache.get(path)
        cache_key = (file_stat.st_size, file_stat.st_mtime_ns)
        if cached is not None and cached[:2] == cache_key:
            return cached[2]
        duration = self._duration_probe(path)
        self._duration_cache[path] = (*cache_key, duration)
        return duration

    def _find_nfo(self, source_path: Path) -> Path | None:
        candidates = [source_path.with_suffix(".nfo")]
        multipart = MULTIPART_STEM_PATTERN.fullmatch(source_path.stem)
        if multipart is not None:
            candidates.append(
                source_path.with_name(f"{multipart.group('base')}.nfo")
            )
        candidates.append(source_path.parent / "movie.nfo")
        for candidate in candidates:
            if candidate.is_file() and not candidate.is_symlink():
                return candidate
        return None

    def _read_nfo(
        self,
        nfo_path: Path,
    ) -> tuple[str | None, list[str], list[str]]:
        try:
            if nfo_path.stat().st_size > MAX_NFO_BYTES:
                return None, [], []
            root = ElementTree.parse(nfo_path).getroot()
        except (ElementTree.ParseError, OSError):
            return None, [], []

        title: str | None = None
        poster_references: list[str] = []
        actors: list[str] = []
        for element in root.iter():
            tag = element.tag.rsplit("}", 1)[-1].lower()
            if tag == "actor":
                name = _nfo_actor_name(element)
                if name and name not in actors:
                    actors.append(name)
                continue
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
        return title, poster_references, actors

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
