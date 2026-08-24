"""초안 템플릿을 샘플 데이터로 렌더해 design/preview/out/ 에 정적 HTML 로 떨군다.

    python3 design/preview/render.py

앱을 띄우지 않고 브라우저로 열어 확인하기 위한 것이다. src/ 는 건드리지 않는다.
"""

from __future__ import annotations

from pathlib import Path
import shutil
import sys

from jinja2 import Environment, FileSystemLoader, select_autoescape

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
OUT = HERE / "out"

sys.path.insert(0, str(HERE))
from fixtures import CONTEXT, NEEDS_BACKEND  # noqa: E402

PAGES = {"dashboard.html": "dashboard.html"}


def filesize(value: float) -> str:
    step = 1024.0
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if abs(value) < step:
            return f"{value:.1f} {unit}" if unit != "B" else f"{value:.0f} B"
        value /= step
    return f"{value:.1f} PB"


def duration(seconds: float | None) -> str:
    if not seconds:
        return "—"
    seconds = int(seconds)
    hours, rest = divmod(seconds, 3600)
    minutes, secs = divmod(rest, 60)
    if hours:
        return f"{hours}:{minutes:02d}:{secs:02d}"
    return f"{minutes}:{secs:02d}"


def approx_minutes(seconds: float | None) -> str:
    if not seconds:
        return "—"
    minutes = max(1, round(seconds / 60))
    return f"약 {minutes}분"


def filename(path: str) -> str:
    return path.rsplit("/", 1)[-1]


def parent_path(path: str) -> str:
    return path.rsplit("/", 1)[0] if "/" in path else ""


def sparkline(values: list[int], width: int = 240, height: int = 36) -> str:
    """0–100 시계열을 polyline points 문자열로."""
    if not values:
        return ""
    span = max(1, len(values) - 1)
    return " ".join(
        f"{i * width / span:.1f},{height - 2 - (v / 100) * (height - 6):.1f}"
        for i, v in enumerate(values)
    )


def build() -> None:
    env = Environment(
        loader=FileSystemLoader(ROOT / "templates"),
        autoescape=select_autoescape(["html"]),
    )
    env.filters.update(
        filesize=filesize,
        duration=duration,
        approx_minutes=approx_minutes,
        filename=filename,
        parent_path=parent_path,
        sparkline=sparkline,
    )

    OUT.mkdir(parents=True, exist_ok=True)
    shutil.copy(ROOT / "static" / "app.css", OUT / "app.css")

    for template_name, out_name in PAGES.items():
        html = env.get_template(template_name).render(**CONTEXT, preview=True)
        (OUT / out_name).write_text(html, encoding="utf-8")
        print(f"wrote preview/out/{out_name}  ({len(html):,} bytes)")

    print("\n아직 백엔드가 만들어 주지 않는 값:")
    for key, why in NEEDS_BACKEND.items():
        print(f"  - {key}: {why}")


if __name__ == "__main__":
    build()
