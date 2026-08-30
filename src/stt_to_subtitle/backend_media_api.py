"""Media library and safe media asset API routes."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from fastapi import APIRouter, HTTPException, Query, Request, Response
from fastapi.responses import FileResponse, StreamingResponse

from .backend_common import public_value, service_from_request
from .media_display import decorate_media_listing, flatten_media_display_folders
from .media_preview import (
    guess_media_type,
    iter_file_range,
    parse_byte_range,
    read_subtitle_text,
    srt_to_webvtt,
)
from .subtitle_validation import parse_subtitle, render_webvtt
from .backend_config import group_multipart_media


router = APIRouter(prefix="/api/v1/media")


@router.get("")
def browse_media(
    request: Request,
    folder: str = "",
    q: str = "",
    actor: str = "",
    folder_sort: str = "name",
    file_sort: str = "filename",
    folder_offset: int = Query(default=0, ge=0),
    folder_limit: int | None = Query(default=None, ge=1, le=100),
) -> dict[str, Any]:
    service = service_from_request(request)
    library = service.library
    try:
        listing = (
            library.search_media(
                title_query=q,
                actor_query=actor,
                relative_directory=folder,
            )
            if q.strip() or actor.strip()
            else library.browse(folder)
        )
    except ValueError as error:
        raise HTTPException(status_code=400, detail=str(error)) from error
    listing = flatten_media_display_folders(
        library,
        listing,
        service.path_display_rules,
    )
    listing["files"] = group_multipart_media(listing.get("files", []))
    try:
        listing = decorate_media_listing(
            library,
            listing,
            service.path_display_rules,
            folder_sort=folder_sort,
            folder_offset=folder_offset,
            folder_limit=folder_limit,
            file_sort=file_sort,
        )
    except ValueError as error:
        raise HTTPException(status_code=400, detail=str(error)) from error
    return public_value(listing)


@router.get("/actors")
def actor_library(
    request: Request,
    folder: str = "av/japan",
) -> dict[str, Any]:
    entries = service_from_request(request).library.actor_library_entries(folder)
    return {"items": public_value(entries), "total": len(entries)}


@router.get("/actor-profile")
def actor_profile(request: Request, folder: str) -> dict[str, str | None]:
    try:
        path = service_from_request(request).library.actor_profile_for_directory(folder)
    except ValueError as error:
        raise HTTPException(status_code=404, detail="actor profile not found") from error
    return {"path": path}


@router.get("/posters/{poster_path:path}")
def poster(poster_path: str, request: Request) -> FileResponse:
    try:
        path = service_from_request(request).library.resolve_poster(poster_path)
    except ValueError as error:
        raise HTTPException(status_code=404, detail="poster not found") from error
    return FileResponse(path)


@router.get("/actors/{actor_path:path}")
def actor_image(actor_path: str, request: Request) -> FileResponse:
    try:
        path = service_from_request(request).library.resolve_actor_image(actor_path)
    except ValueError as error:
        raise HTTPException(status_code=404, detail="actor image not found") from error
    return FileResponse(path)


@router.api_route("/file", methods=["GET", "HEAD"])
def media_file(request: Request, path: str = Query(...)) -> Response:
    try:
        source = service_from_request(request).library.resolve_file(path)
    except ValueError as error:
        raise HTTPException(status_code=404, detail="media file not found") from error
    size = source.stat().st_size
    headers = {"Accept-Ranges": "bytes", "Cache-Control": "private, no-cache"}
    try:
        byte_range = parse_byte_range(request.headers.get("range"), size)
    except (OverflowError, ValueError):
        return Response(
            status_code=416,
            headers={**headers, "Content-Range": f"bytes */{size}"},
        )
    start, end = byte_range if byte_range is not None else (0, size - 1)
    status_code = 206 if byte_range is not None else 200
    headers["Content-Length"] = str(max(0, end - start + 1))
    if byte_range is not None:
        headers["Content-Range"] = f"bytes {start}-{end}/{size}"
    if request.method == "HEAD":
        return Response(
            status_code=status_code,
            media_type=guess_media_type(source.name),
            headers=headers,
        )
    return StreamingResponse(
        iter_file_range(source, start, end),
        status_code=status_code,
        media_type=guess_media_type(source.name),
        headers=headers,
    )


@router.get("/external-subtitles.vtt")
def external_subtitles(request: Request, path: str) -> Response:
    try:
        subtitles = service_from_request(request).library.external_subtitles(path)
        if not subtitles:
            raise FileNotFoundError("external subtitle not found")
        content = (
            srt_to_webvtt(read_subtitle_text(subtitles[0]))
            if subtitles[0].suffix.lower() == ".srt"
            else render_webvtt(parse_subtitle(subtitles[0]))
        )
    except (OSError, UnicodeError, ValueError) as error:
        raise HTTPException(status_code=404, detail="external subtitle not found") from error
    return Response(content, media_type="text/vtt")
