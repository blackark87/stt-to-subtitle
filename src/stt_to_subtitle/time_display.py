"""KST formatting for wall-clock timestamps and application logs."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
import logging


KST = timezone(timedelta(hours=9), name="KST")
LOG_FORMAT = "%(asctime)s %(levelname)s %(name)s %(message)s"
LOG_DATE_FORMAT = "%Y%m%d %H:%M:%S"


def format_kst_timestamp(value: float | int | str) -> str:
    return datetime.fromtimestamp(float(value), tz=KST).strftime(
        "%Y%m%d %H:%M:%S"
    )


def format_kst_iso(value: float | int | str) -> str:
    return datetime.fromtimestamp(float(value), tz=KST).isoformat(
        timespec="milliseconds"
    )


class KSTLogFormatter(logging.Formatter):
    def formatTime(
        self,
        record: logging.LogRecord,
        datefmt: str | None = None,
    ) -> str:
        timestamp = datetime.fromtimestamp(record.created, tz=KST)
        return timestamp.strftime(datefmt or LOG_DATE_FORMAT)


def configure_kst_logging(level: str) -> None:
    handler = logging.StreamHandler()
    handler.setFormatter(
        KSTLogFormatter(
            LOG_FORMAT,
            datefmt=LOG_DATE_FORMAT,
        )
    )
    logging.basicConfig(
        level=level,
        handlers=[handler],
        force=True,
    )
