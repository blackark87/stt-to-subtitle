"""디자인 초안 미리보기 서버.

    ~/.pyenv/versions/3.12.13/bin/python -m uvicorn design.app:app --host 0.0.0.0 --port 8099 --reload

  /option-B   대시보드 목업 (Option B 계열, 검토·수정 반영본)
  /webgpu     배경 레이어 데모 (WebGPU, 실패 시 WebGL2 폴백)

src/ 는 건드리지 않는다. 데이터는 design/preview/fixtures.py 의 샘플이다.
"""

from __future__ import annotations

from pathlib import Path
import sys

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE / "preview"))

from fixtures import CONTEXT  # noqa: E402
from render import (  # noqa: E402
    approx_minutes,
    duration,
    filename,
    filesize,
    parent_path,
    sparkline,
)

app = FastAPI(title="stt-to-subtitle design draft")
app.mount("/static", StaticFiles(directory=HERE / "static"), name="static")

templates = Jinja2Templates(directory=HERE / "templates")
templates.env.filters.update(
    filesize=filesize,
    duration=duration,
    approx_minutes=approx_minutes,
    filename=filename,
    parent_path=parent_path,
    sparkline=sparkline,
)


@app.get("/", response_class=RedirectResponse)
def index() -> RedirectResponse:
    return RedirectResponse("/option-B")


@app.get("/option-B", response_class=HTMLResponse)
def option_b(request: Request) -> HTMLResponse:
    return templates.TemplateResponse(
        request, "dashboard.html", {**CONTEXT, "preview": False}
    )


@app.get("/webgpu", response_class=HTMLResponse)
def webgpu(request: Request) -> HTMLResponse:
    """파이프라인을 3D 씬으로 보여준다. 오브젝트가 곧 작업이다."""
    scene_data = {
        "slots": CONTEXT["pipeline_slots"],
        "queue": CONTEXT["queue"],
        "queue_rest": CONTEXT["queue_rest"],
        "stopped": CONTEXT["stopped_jobs"],
        "completed": CONTEXT["completed_jobs"],
        "completed_total": CONTEXT["job_stats"]["completed"],
        "gpu": CONTEXT["gpu"],
        # 상층 미디어 창고가 쓴다. media_tree 는 운영 사이트에서 직접 확인한
        # 실제 디렉터리 구조다 (fixtures.MEDIA_TREE 주석 참고).
        "actors": CONTEXT["actor_progress"],
        "media_tree": CONTEXT["media_tree"],
        "stopped_rest": CONTEXT["stopped_rest"],
    }
    return templates.TemplateResponse(
        request, "webgpu.html", {**CONTEXT, "scene_data": scene_data}
    )
