"""Backend configuration and media-library access."""

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

from .subtitle_validation import discover_external_subtitles

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
    ".actors",
    ".ds_store",
    ".snapshot",
    ".snapshots",
    "#recycle",
    "@eadir",
    "@recycle",
    "@sharesnap",
    "@tmp",
    "extrafanart",
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
            str(item.get("nfo_title", "")).casefold(): str(
                item.get("nfo_title", "")
            )
            for item in members
            if item.get("has_nfo")
            and str(item.get("nfo_title", "")).strip()
        }
        nfo_title = (
            next(iter(common_nfo_titles.values()))
            if len(common_nfo_titles) == 1
            else None
        )
        title = nfo_title or base
        created_values = [
            float(value)
            for item in members
            if isinstance((value := item.get("created_at")), (int, float))
        ]
        modified_values = [
            float(value)
            for item in members
            if isinstance((value := item.get("modified_at")), (int, float))
        ]
        nfo_release_date = next(
            (
                str(value)
                for item in members
                if (value := item.get("nfo_release_date"))
            ),
            None,
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
                "nfo_title": nfo_title,
                "nfo_release_date": nfo_release_date,
                "size": sum(int(item.get("size", 0)) for item in members),
                "created_at": min(created_values) if created_values else None,
                "modified_at": max(modified_values) if modified_values else None,
                "duration_seconds": duration,
                "has_subtitle": all(
                    bool(item.get("has_subtitle")) for item in members
                ),
                "has_external_subtitle": all(
                    bool(item.get("has_external_subtitle")) for item in members
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


def _enabled_env(name: str, default: str) -> bool:
    return os.environ.get(name, default).strip().lower() in {
        "1",
        "true",
        "yes",
        "on",
    }


def _host_list_env(name: str) -> tuple[str, ...]:
    hosts: list[str] = []
    for item in os.environ.get(name, "").split(","):
        host = item.strip().casefold().rstrip(".")
        if host and host not in hosts:
            hosts.append(host)
    return tuple(hosts)


@dataclass(frozen=True)
class RemoteServerSettings:
    stt_base_url: str
    stt_token: str

    @property
    def stt_is_complete(self) -> bool:
        return bool(self.stt_base_url.strip())

    @property
    def is_complete(self) -> bool:
        return self.stt_is_complete

    def normalized(self) -> RemoteServerSettings:
        if not self.stt_base_url.strip():
            raise ValueError("required server setting is missing: STT_BASE_URL")
        return RemoteServerSettings(
            stt_base_url=normalize_server_url(
                self.stt_base_url,
                "STT_BASE_URL",
            ),
            stt_token=self.stt_token,
        )


@dataclass(frozen=True)
class SubtitleValidatorSettings:
    provider: str = "openai_compatible"
    base_url: str = ""
    token: str = ""
    model: str = ""
    region: str = ""

    @property
    def is_complete(self) -> bool:
        try:
            self.normalized()
        except ValueError:
            return False
        return True

    def normalized(self) -> SubtitleValidatorSettings:
        provider = self.provider.strip().lower()
        region = self.region.strip().lower()
        if provider not in {"openrouter", "bedrock", "openai_compatible"}:
            raise ValueError("지원하지 않는 상용 LLM 제공자입니다.")
        required = [("검증 모델", self.model)]
        if provider == "openrouter":
            required.append(("OpenRouter API 키", self.token))
        elif provider == "bedrock":
            required.extend(
                (
                    ("Amazon Bedrock 리전", self.region),
                    ("Amazon Bedrock API 키", self.token),
                )
            )
        else:
            required.append(("OpenAI 호환 API 주소", self.base_url))
        missing = [name for name, value in required if not value.strip()]
        if missing:
            raise ValueError(f"필수 검증 설정이 없습니다: {', '.join(missing)}")
        if provider == "bedrock" and not re.fullmatch(
            r"[a-z]{2}(?:-gov)?-[a-z]+-\d+",
            region,
        ):
            raise ValueError("Amazon Bedrock 리전 형식이 올바르지 않습니다.")
        return SubtitleValidatorSettings(
            provider=provider,
            base_url=(
                "https://openrouter.ai/api/v1"
                if provider == "openrouter"
                else ""
                if provider == "bedrock"
                else normalize_server_url(
                    self.base_url,
                    "SUBTITLE_VALIDATOR_BASE_URL",
                )
            ),
            token=self.token,
            model=self.model.strip(),
            region=region if provider == "bedrock" else "",
        )


@dataclass(frozen=True)
class BackendSettings:
    state_dir: Path
    media_root: Path
    stt_base_url: str
    stt_token: str
    gpu_prometheus_url: str = ""
    gpu_prometheus_token: str = ""
    gpu_metrics_refresh_seconds: float = 10.0
    gpu_metrics_timeout_seconds: float = 3.0
    maximum_listed_files: int = 5000
    translation_batch_segments: int = 30
    translation_batch_characters: int = 6000
    audio_workers: int = 1
    work_dir: Path | None = None
    translation_state_dir: Path | None = None
    translation_builtin_name: str = "기본 서버"
    translation_builtin_base_url: str = ""
    translation_builtin_token: str = ""
    translation_builtin_capacity: int = 1
    translation_builtin_draft_enabled: bool = True
    translation_builtin_review_enabled: bool = False
    translation_builtin_draft_batch_preferred: bool = False
    translation_builtin_review_batch_preferred: bool = False
    translation_connect_timeout_seconds: float = 10.0
    translation_read_timeout_seconds: float = 600.0
    translation_stt_hard_breaker_hosts: tuple[str, ...] = ()
    translation_stt_hard_breaker_timeout_seconds: float = 600.0

    @classmethod
    def from_env(cls) -> BackendSettings:
        return cls(
            state_dir=Path(
                os.environ.get("BACKEND_STATE_DIR", "/var/lib/stt")
            ).expanduser(),
            media_root=Path(
                os.environ.get("MEDIA_ROOT", "/media")
            ).expanduser(),
            stt_base_url=os.environ.get("STT_BASE_URL", "").strip(),
            stt_token=os.environ.get("STT_API_TOKEN", ""),
            work_dir=(
                Path(os.environ["BACKEND_WORK_DIR"]).expanduser()
                if os.environ.get("BACKEND_WORK_DIR", "").strip()
                else None
            ),
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
            maximum_listed_files=int(
                os.environ.get("BACKEND_MAXIMUM_LISTED_FILES", "5000")
            ),
            translation_batch_segments=int(
                os.environ.get("TRANSLATION_BATCH_SEGMENTS", "30")
            ),
            translation_batch_characters=int(
                os.environ.get("TRANSLATION_BATCH_CHARACTERS", "6000")
            ),
            audio_workers=int(
                os.environ.get("BACKEND_AUDIO_WORKERS", "1")
            ),
            translation_state_dir=(
                Path(os.environ["TRANSLATION_STATE_DIR"]).expanduser()
                if os.environ.get("TRANSLATION_STATE_DIR", "").strip()
                else None
            ),
            translation_builtin_name=os.environ.get(
                "TRANSLATION_BUILTIN_NAME",
                "기본 서버",
            ).strip(),
            translation_builtin_base_url=os.environ.get(
                "TRANSLATION_BUILTIN_BASE_URL",
                "",
            ).strip(),
            translation_builtin_token=os.environ.get(
                "TRANSLATION_BUILTIN_TOKEN",
                "",
            ),
            translation_builtin_capacity=int(
                os.environ.get("TRANSLATION_BUILTIN_CAPACITY", "1")
            ),
            translation_builtin_draft_enabled=_enabled_env(
                "TRANSLATION_BUILTIN_DRAFT_ENABLED",
                "true",
            ),
            translation_builtin_review_enabled=_enabled_env(
                "TRANSLATION_BUILTIN_REVIEW_ENABLED",
                "false",
            ),
            translation_builtin_draft_batch_preferred=_enabled_env(
                "TRANSLATION_BUILTIN_DRAFT_BATCH_PREFERRED",
                "false",
            ),
            translation_builtin_review_batch_preferred=_enabled_env(
                "TRANSLATION_BUILTIN_REVIEW_BATCH_PREFERRED",
                "false",
            ),
            translation_connect_timeout_seconds=float(
                os.environ.get("TRANSLATION_CONNECT_TIMEOUT_SECONDS", "10")
            ),
            translation_read_timeout_seconds=float(
                os.environ.get("TRANSLATION_READ_TIMEOUT_SECONDS", "600")
            ),
            translation_stt_hard_breaker_hosts=_host_list_env(
                "TRANSLATION_STT_HARD_BREAKER_HOSTS"
            ),
            translation_stt_hard_breaker_timeout_seconds=float(
                os.environ.get(
                    "TRANSLATION_STT_HARD_BREAKER_TIMEOUT_SECONDS",
                    "600",
                )
            ),
        )

    @property
    def jobs_dir(self) -> Path:
        return self.work_dir or self.state_dir / "jobs"

    @property
    def translation_dir(self) -> Path:
        return self.translation_state_dir or self.state_dir / "translation"

    def validate(self) -> None:
        if self.maximum_listed_files < 1:
            raise ValueError("BACKEND_MAXIMUM_LISTED_FILES must be positive")
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
        if self.audio_workers < 1:
            raise ValueError("BACKEND_AUDIO_WORKERS must be at least 1")
        if not self.translation_builtin_name.strip():
            raise ValueError("TRANSLATION_BUILTIN_NAME is required")
        if not 1 <= self.translation_builtin_capacity <= 8:
            raise ValueError("TRANSLATION_BUILTIN_CAPACITY must be 1..8")
        if self.translation_builtin_base_url:
            normalize_server_url(
                self.translation_builtin_base_url,
                "TRANSLATION_BUILTIN_BASE_URL",
            )
        if (
            self.translation_connect_timeout_seconds <= 0
            or self.translation_read_timeout_seconds <= 0
        ):
            raise ValueError("translation timeouts must be positive")
        if self.translation_stt_hard_breaker_timeout_seconds <= 0:
            raise ValueError(
                "TRANSLATION_STT_HARD_BREAKER_TIMEOUT_SECONDS must be positive"
            )
        for host in self.translation_stt_hard_breaker_hosts:
            if (
                not host
                or "://" in host
                or any(character in host for character in "/?#@")
            ):
                raise ValueError(
                    "TRANSLATION_STT_HARD_BREAKER_HOSTS must contain "
                    "comma-separated host names or IP addresses"
                )

    def remote_servers(self) -> RemoteServerSettings:
        return RemoteServerSettings(
            stt_base_url=self.stt_base_url,
            stt_token=self.stt_token,
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

    def resolve_actor_image(self, relative_path: str) -> Path:
        if not relative_path or Path(relative_path).is_absolute():
            raise ValueError("actor image path must be relative")
        candidate = self.root / relative_path
        try:
            resolved = candidate.resolve(strict=True)
        except OSError as error:
            raise ValueError("actor image does not exist") from error
        if not resolved.is_relative_to(self.root):
            raise ValueError("actor image path escapes MEDIA_ROOT")
        relative = resolved.relative_to(self.root)
        if not any(part.casefold() == ".actors" for part in relative.parts[:-1]):
            raise ValueError("actor image is not inside an .actors folder")
        if (
            candidate.is_symlink()
            or not resolved.is_file()
            or resolved.suffix.lower() not in POSTER_EXTENSIONS
        ):
            raise ValueError("unsupported actor image")
        return resolved

    def actor_profile_for_directory(
        self,
        relative_directory: str,
    ) -> str | None:
        directory = self.resolve_directory(relative_directory)
        profile = self._find_actor_profile(directory, directory.name)
        if profile is None:
            return None
        return profile.relative_to(self.root).as_posix()

    def external_subtitles(self, relative_media_path: str) -> tuple[Path, ...]:
        """Return safe plain sidecars for one media file."""
        media_path = self.resolve_file(relative_media_path)
        return discover_external_subtitles(media_path)

    def actor_library_entries(
        self,
        relative_directory: str = "av/japan",
    ) -> list[dict[str, object]]:
        """Return actor folders and their media/subtitle state."""
        collection = self._casefold_directory(relative_directory)
        if collection is None:
            return []
        try:
            actor_directories = sorted(
                (
                    entry
                    for entry in os.scandir(collection)
                    if entry.is_dir(follow_symlinks=False)
                    and not _is_ignored_directory(entry.name)
                ),
                key=lambda entry: entry.name.casefold(),
            )
        except OSError:
            return []

        entries: list[dict[str, object]] = []
        for actor_entry in actor_directories:
            actor_directory = Path(actor_entry.path)
            media = self._progress_media_entries(actor_directory)
            if not media:
                continue
            profile = self._find_actor_profile(
                actor_directory,
                actor_entry.name,
            )
            entries.append(
                {
                    "name": actor_entry.name,
                    "path": actor_directory.relative_to(self.root).as_posix(),
                    "image_path": (
                        profile.relative_to(self.root).as_posix()
                        if profile is not None
                        else None
                    ),
                    "media": media,
                }
            )
        return entries

    def collection_library_entry(
        self,
        relative_directory: str,
        *,
        name: str,
    ) -> dict[str, object] | None:
        """Return progress media for a non-actor library collection."""
        collection = self._casefold_directory(relative_directory)
        if collection is None:
            return None
        media = self._progress_media_entries(collection)
        if not media:
            return None
        return {
            "name": name,
            "path": collection.relative_to(self.root).as_posix(),
            "image_path": None,
            "media": media,
        }

    def _progress_media_entries(
        self,
        root: Path,
    ) -> list[dict[str, object]]:
        media: list[dict[str, object]] = []
        pending = [root]
        while pending:
            directory = pending.pop()
            try:
                children = list(os.scandir(directory))
            except OSError:
                continue
            names = {child.name.casefold() for child in children}
            for child in children:
                if child.is_dir(follow_symlinks=False):
                    if not _is_ignored_directory(child.name):
                        pending.append(Path(child.path))
                    continue
                if (
                    not child.is_file(follow_symlinks=False)
                    or Path(child.name).suffix.lower() not in MEDIA_EXTENSIONS
                    or _is_ignored_file(child.name)
                    or _is_hidden_media_file(child.name)
                ):
                    continue
                source = Path(child.path)
                external_subtitles = discover_external_subtitles(source)
                media.append(
                    {
                        "path": source.relative_to(self.root).as_posix(),
                        "has_subtitle": (
                            f"{source.stem}.ko.srt".casefold() in names
                            or f"{source.stem}.ko.ass".casefold() in names
                        ),
                        "has_external_subtitle": bool(external_subtitles),
                        "external_subtitle_formats": [
                            path.suffix.lower().lstrip(".")
                            for path in external_subtitles
                        ],
                    }
                )
        media.sort(key=lambda item: str(item["path"]).casefold())
        return media

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
                try:
                    modified_at = child.stat().st_mtime
                except OSError:
                    modified_at = None
                folders.append(
                    {
                        "name": child.name,
                        "path": child.relative_to(self.root).as_posix(),
                        "modified_at": modified_at,
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
        return self.search_media(
            title_query=query,
            relative_directory=relative_directory,
        )

    def search_media(
        self,
        *,
        title_query: str = "",
        actor_query: str = "",
        relative_directory: str = "",
    ) -> dict[str, object]:
        """Find media by title and NFO actor below the selected directory."""
        normalized_title = title_query.strip()
        normalized_actor = actor_query.strip()
        if not normalized_title and not normalized_actor:
            return self.browse(relative_directory)
        if len(normalized_title) > 200:
            raise ValueError("검색어는 200자 이하로 입력하세요.")
        if len(normalized_actor) > 200:
            raise ValueError("배우 필터는 200자 이하로 입력하세요.")
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
        matching_files: list[dict[str, object]] = []
        pending = [directory]
        folded_title = normalized_title.casefold()
        folded_actor = normalized_actor.casefold()
        while pending and len(matching_files) < self.maximum_files:
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
                if (
                    folded_title
                    and folded_title not in self._media_title(child).casefold()
                ):
                    continue
                media = self._describe_media(child)
                if folded_actor and not any(
                    folded_actor in str(actor).casefold()
                    for actor in media.get("actors", ())
                ):
                    continue
                matching_files.append(media)
                if len(matching_files) >= self.maximum_files:
                    break

        matching_files.sort(
            key=lambda media: str(media["path"]).casefold()
        )
        return {
            **location,
            "folders": [],
            "files": matching_files,
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
        external_subtitles = discover_external_subtitles(path)
        nfo_path = self._find_nfo(path)
        title: str | None = None
        release_date: str | None = None
        poster_path: str | None = None
        actors: list[str] = []
        if nfo_path is not None:
            title, poster_references, actors, release_date = self._read_nfo(
                nfo_path
            )
            poster = self._find_poster(path, nfo_path, poster_references)
            if poster is not None:
                poster_path = poster.relative_to(self.root).as_posix()
        return {
            "path": relative,
            "name": path.name,
            "size": file_stat.st_size,
            "created_at": float(
                getattr(file_stat, "st_birthtime", file_stat.st_ctime)
            ),
            "modified_at": float(file_stat.st_mtime),
            "duration_seconds": self._media_duration(path, file_stat),
            "has_subtitle": srt_subtitle.is_file() or ass_subtitle.is_file(),
            "has_external_subtitle": bool(external_subtitles),
            "external_subtitle_formats": [
                subtitle.suffix.lower().lstrip(".")
                for subtitle in external_subtitles
            ],
            "external_subtitle_path": (
                external_subtitles[0].relative_to(self.root).as_posix()
                if external_subtitles
                else None
            ),
            "has_nfo": nfo_path is not None,
            "title": title or path.stem,
            "nfo_title": title,
            "nfo_release_date": release_date,
            "poster_path": poster_path,
            "actors": actors,
        }

    def _media_title(self, path: Path) -> str:
        nfo_path = self._find_nfo(path)
        if nfo_path is not None:
            title, _, _, _ = self._read_nfo(nfo_path)
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
    ) -> tuple[str | None, list[str], list[str], str | None]:
        try:
            if nfo_path.stat().st_size > MAX_NFO_BYTES:
                return None, [], [], None
            root = ElementTree.parse(nfo_path).getroot()
        except (ElementTree.ParseError, OSError):
            return None, [], [], None

        title: str | None = None
        poster_references: list[str] = []
        actors: list[str] = []
        release_dates: dict[str, str] = {}
        for element in root.iter():
            tag = element.tag.rsplit("}", 1)[-1].lower()
            normalized_tag = re.sub(r"[^a-z]", "", tag)
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
            elif normalized_tag in {
                "releasedate",
                "premiered",
            }:
                release_dates.setdefault(
                    normalized_tag,
                    value,
                )
            elif tag == "poster":
                poster_references.append(value)
            elif (
                tag == "thumb"
                and element.attrib.get("aspect", "").strip().lower() == "poster"
            ):
                poster_references.append(value)
        release_date = release_dates.get("releasedate") or release_dates.get(
            "premiered"
        )
        return title, poster_references, actors, release_date

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

    def _casefold_directory(self, relative_directory: str) -> Path | None:
        current = self.root
        for part in Path(relative_directory).parts:
            if part in {"", "."}:
                continue
            try:
                matches = sorted(
                    (
                        entry
                        for entry in os.scandir(current)
                        if entry.name.casefold() == part.casefold()
                        and entry.is_dir(follow_symlinks=False)
                    ),
                    key=lambda entry: entry.name,
                )
            except OSError:
                return None
            if not matches:
                return None
            current = Path(matches[0].path)
        return current

    def _find_actor_profile(
        self,
        actor_directory: Path,
        actor_name: str,
    ) -> Path | None:
        metadata_directories = [actor_directory / ".actors"]
        try:
            children = list(os.scandir(actor_directory))
        except OSError:
            children = []
        metadata_directories.extend(
            Path(child.path) / ".actors"
            for child in children
            if child.is_dir(follow_symlinks=False)
            and not _is_ignored_directory(child.name)
        )
        folded_name = actor_name.casefold()
        for metadata_directory in metadata_directories:
            if metadata_directory.is_symlink():
                continue
            try:
                images = sorted(
                    (
                        entry
                        for entry in os.scandir(metadata_directory)
                        if entry.is_file(follow_symlinks=False)
                        and Path(entry.name).suffix.lower()
                        in POSTER_EXTENSIONS
                    ),
                    key=lambda entry: entry.name.casefold(),
                )
            except OSError:
                continue
            for image in images:
                if Path(image.name).stem.casefold() == folded_name:
                    return Path(image.path).resolve()
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
