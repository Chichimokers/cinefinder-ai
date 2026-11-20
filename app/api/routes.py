"""Endpoints de la API v1."""

from __future__ import annotations

import base64
import binascii
from typing import Any

from fastapi import APIRouter, BackgroundTasks, Depends, File, Form, HTTPException, Request, UploadFile, status
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import BaseModel, Field
from sqlalchemy import func, select

from app.api.deps import require_auth
from app.database.models import AgentTask, Movie, SearchHistory, SearchJob
from app.database.session import check_database, get_sessionmaker
from app.services.jobs import create_job, get_job, job_to_dict, recover_stalled_jobs, set_status
from app.services.research import run_search_job
from app.utils.logging import get_logger

logger = get_logger(__name__)

router = APIRouter(prefix="/api/v1")


class SearchRequest(BaseModel):
    query: str = ""
    images: list[str] = Field(default_factory=list, description="Imágenes en base64")
    image_urls: list[str] = Field(default_factory=list)
    async_mode: bool = Field(default=True, alias="async")

    model_config = {"populate_by_name": True}


class SearchAccepted(BaseModel):
    job_id: str
    status: str
    status_url: str


class MovieOut(BaseModel):
    id: int
    code: str
    title: str
    original_title: str
    year: int | None
    genres: list
    synopsis: str | None
    poster_url: str | None
    confidence: float
    verification_status: str


def _decode_images(values: list[str]) -> list[bytes]:
    images = []
    for value in values:
        data = value.split(",", 1)[-1]
        try:
            images.append(base64.b64decode(data, validate=True))
        except (binascii.Error, ValueError):
            raise HTTPException(status_code=400, detail="Imagen base64 no válida")
        if len(images) >= 8:
            break
    return images


def _is_owner(requester: str | None) -> bool:
    from app.config import settings

    return bool(
        requester and str(requester).lstrip("-").isdigit()
        and int(requester) == settings.telegram_owner_id
    )


async def _process_background(
    job_id: str,
    query: str,
    images: list[bytes],
    image_urls: list[str],
    requester_id: str | None,
    is_owner: bool = False,
) -> None:
    try:
        async with get_sessionmaker()() as db:
            await run_search_job(db, job_id, query, images, image_urls, requester_id, is_owner)
    except Exception as exc:  # noqa: BLE001
        logger.warning("background_job_failed", extra={"error": type(exc).__name__})
        async with get_sessionmaker()() as db:
            await set_status(db, job_id, "failed", error=type(exc).__name__)
            await db.commit()


@router.get("/health")
async def health() -> dict:
    return {"status": "ok", "service": "cinefinder-api", "version": "1.0.0"}


@router.get("/ready")
async def ready() -> dict:
    db = await check_database()
    return {
        "ready": bool(db.get("ok")),
        "database": db,
        "model_configured": bool(__import__("app.config", fromlist=["settings"]).settings.nvidia_api_key),
    }


@router.post("/search")
async def search_json(
    payload: SearchRequest,
    background_tasks: BackgroundTasks,
    request: Request,
    _: str = Depends(require_auth),
) -> Any:
    images = _decode_images(payload.images)
    if not payload.query and not images and not payload.image_urls:
        raise HTTPException(status_code=400, detail="Se requiere texto o al menos una imagen")
    requester = request.headers.get("X-Requester-ID")
    owner = _is_owner(requester)
    async with get_sessionmaker()() as db:
        job = await create_job(
            db, query=payload.query, requester_id=requester,
            image_hashes=[], image_urls=payload.image_urls,
        )
        await db.commit()
    job_id = job.id
    if payload.async_mode:
        background_tasks.add_task(
            _process_background, job_id, payload.query, images, payload.image_urls, requester, owner
        )
        return JSONResponse(
            status_code=status.HTTP_202_ACCEPTED,
            content=SearchAccepted(
                job_id=job_id, status="queued", status_url=f"/api/v1/jobs/{job_id}"
            ).model_dump(),
        )
    result = await _process_and_return(job_id, payload.query, images, payload.image_urls, requester, owner)
    return JSONResponse(status_code=status.HTTP_200_OK, content={"job_id": job_id, **result})


async def _process_and_return(job_id, query, images, image_urls, requester, is_owner=False) -> dict:
    async with get_sessionmaker()() as db:
        return await run_search_job(db, job_id, query, images, image_urls, requester, is_owner)


@router.post("/search/multipart")
async def search_multipart(
    background_tasks: BackgroundTasks,
    request: Request,
    query: str = Form(""),
    images: list[UploadFile] = File(default=[]),
    async_mode: bool = Form(True, alias="async"),
    _: str = Depends(require_auth),
) -> Any:
    decoded: list[bytes] = []
    for upload in images[:8]:
        content = await upload.read()
        if content:
            decoded.append(content)
    if not query and not decoded:
        raise HTTPException(status_code=400, detail="Se requiere texto o imágenes")
    async with get_sessionmaker()() as db:
        job = await create_job(db, query=query, requester_id=request.headers.get("X-Requester-ID"))
        await db.commit()
    job_id = job.id
    requester = request.headers.get("X-Requester-ID")
    owner = _is_owner(requester)
    if async_mode:
        background_tasks.add_task(_process_background, job_id, query, decoded, [], requester, owner)
        return JSONResponse(
            status_code=status.HTTP_202_ACCEPTED,
            content=SearchAccepted(
                job_id=job_id, status="queued", status_url=f"/api/v1/jobs/{job_id}"
            ).model_dump(),
        )
    result = await _process_and_return(job_id, query, decoded, [], requester, owner)
    return JSONResponse(status_code=status.HTTP_200_OK, content={"job_id": job_id, **result})


@router.get("/jobs/{job_id}")
async def get_job_status(job_id: str, _: str = Depends(require_auth)) -> dict:
    async with get_sessionmaker()() as db:
        job = await get_job(db, job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="Job no encontrado")
    return await job_to_dict(job)


@router.get("/jobs/{job_id}/events")
async def job_events(job_id: str, _: str = Depends(require_auth)) -> StreamingResponse:
    """Streaming de eventos: pensamiento y herramientas en vivo (SSE)."""
    import asyncio

    from app.services.events import sse, subscribe, unsubscribe

    queue = subscribe(job_id)

    async def generator():
        try:
            while True:
                try:
                    event = await asyncio.wait_for(queue.get(), timeout=30)
                except TimeoutError:
                    yield sse({"type": "heartbeat", "message": "en curso"})
                    continue
                yield sse(event)
                if event.get("type") == "final":
                    break
        finally:
            unsubscribe(job_id, queue)

    return StreamingResponse(generator(), media_type="text/event-stream")


@router.get("/movies", response_model=list[MovieOut])
async def list_movies(
    skip: int = 0, limit: int = 50, q: str | None = None, _: str = Depends(require_auth)
) -> list[dict]:
    async with get_sessionmaker()() as db:
        stmt = select(Movie)
        if q:
            stmt = stmt.where(Movie.title.ilike(f"%{q}%") | Movie.original_title.ilike(f"%{q}%"))
        result = await db.execute(stmt.order_by(Movie.created_at.desc()).offset(skip).limit(min(limit, 100)))
        movies = result.scalars().all()
    return [_movie_out(m) for m in movies]


@router.get("/movies/{movie_id}", response_model=MovieOut)
async def get_movie(movie_id: str, _: str = Depends(require_auth)) -> dict:
    async with get_sessionmaker()() as db:
        movie = None
        if movie_id.upper().startswith("CF-"):
            result = await db.execute(select(Movie).where(Movie.code == movie_id.upper()))
            movie = result.scalar_one_or_none()
        elif movie_id.isdigit():
            movie = await db.get(Movie, int(movie_id))
        if movie is None:
            raise HTTPException(status_code=404, detail="Película no encontrada")
        return _movie_out(movie)


@router.get("/stats")
async def stats(_: str = Depends(require_auth)) -> dict:
    async with get_sessionmaker()() as db:
        movies = await db.execute(select(func.count()).select_from(Movie))
        jobs = await db.execute(select(func.count()).select_from(SearchJob))
        tasks = await db.execute(select(func.count()).select_from(AgentTask))
        by_status = await db.execute(
            select(SearchJob.status, func.count()).group_by(SearchJob.status)
        )
    return {
        "movies": movies.scalar(),
        "jobs": jobs.scalar(),
        "agent_tasks": tasks.scalar(),
        "jobs_by_status": {row[0]: row[1] for row in by_status.all()},
    }


@router.get("/search/history")
async def search_history(limit: int = 50, _: str = Depends(require_auth)) -> list[dict]:
    async with get_sessionmaker()() as db:
        result = await db.execute(
            select(SearchHistory).order_by(SearchHistory.created_at.desc()).limit(min(limit, 100))
        )
        rows = result.scalars().all()
    return [
        {
            "id": r.id, "query": r.query, "requester_id": r.requester_id,
            "result_count": r.result_count,
            "created_at": r.created_at.isoformat() if r.created_at else None,
        }
        for r in rows
    ]


@router.post("/admin/reindex")
async def admin_reindex(_: str = Depends(require_auth)) -> dict:
    async with get_sessionmaker()() as db:
        recovered = await recover_stalled_jobs(db)
        await db.commit()
    return {"recovered_jobs": recovered, "message": "Trabajos interrumpidos reencolados"}


@router.get("/openapi.json")
async def openapi_json(_: str = Depends(require_auth)) -> dict:
    from app.main import app

    return app.openapi()


def _movie_out(movie: Movie) -> dict:
    return {
        "id": movie.id,
        "code": movie.code,
        "title": movie.title,
        "original_title": movie.original_title,
        "year": movie.year,
        "genres": movie.genres or [],
        "synopsis": movie.synopsis,
        "poster_url": movie.poster_url,
        "confidence": movie.confidence,
        "verification_status": movie.verification_status,
    }
