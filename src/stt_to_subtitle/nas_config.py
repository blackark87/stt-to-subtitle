"""Configuration and media-root access for the NAS web service."""

from __future__ import annotations

from dataclasses import dataclass
import os
from pathlib import Path

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
        if not resolved.is_file():
            raise ValueError("media path is not a file")
        if resolved.suffix.lower() not in MEDIA_EXTENSIONS:
            raise ValueError("unsupported media file extension")
        return resolved

    def list_files(self) -> list[dict[str, object]]:
        if not self.root.is_dir():
            return []
        files: list[dict[str, object]] = []
        for directory, directory_names, file_names in os.walk(
            self.root,
            followlinks=False,
        ):
            directory_names[:] = [
                name
                for name in directory_names
                if not (Path(directory) / name).is_symlink()
            ]
            for name in file_names:
                path = Path(directory) / name
                if path.is_symlink() or path.suffix.lower() not in MEDIA_EXTENSIONS:
                    continue
                relative = path.relative_to(self.root).as_posix()
                subtitle = path.with_name(f"{path.stem}.ko.srt")
                files.append(
                    {
                        "path": relative,
                        "size": path.stat().st_size,
                        "has_subtitle": subtitle.is_file(),
                    }
                )
                if len(files) >= self.maximum_files:
                    return sorted(files, key=lambda item: str(item["path"]).lower())
        return sorted(files, key=lambda item: str(item["path"]).lower())
