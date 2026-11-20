"""Gestión de trabajos asíncronos persistentes en PostgreSQL."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.database.models import SearchJob
from app.utils.security import encrypt_payload


async def create_job(
    db: AsyncSession,
    origin: str = "api",
    requester_id: str | None = None,
    query: str | None = None,
    image_hashes: list[str] | None = None,
    image_urls: list[str] | None = None,
    payload: dict | None = None,
) -> SearchJob:
    job = SearchJob(
        status="queued",
        origin=origin,
        requester_id=requester_id,
        query=query,
        image_hashes=image_hashes or [],
        image_urls=image_urls or [],
        encrypted_payload=encrypt_payload(payload_json) if (payload_json := _dump(payload)) else None,
    )
    db.add(job)
    await db.flush()
    return job


def _dump(payload: dict | None) -> str | None:
    if not payload:
        return None
    import json
    return json.dumps(payload, ensure_ascii=False)


async def get_job(db: AsyncSession, job_id: str) -> SearchJob | None:
    return await db.get(SearchJob, job_id)


async def job_to_dict(job: SearchJob) -> dict:
    return {
        "job_id": job.id,
        "status": job.status,
        "origin": job.origin,
        "query": job.query,
        "image_hashes": job.image_hashes,
        "created_at": job.created_at.isoformat() if job.created_at else None,
        "updated_at": job.updated_at.isoformat() if job.updated_at else None,
        "completed_at": job.completed_at.isoformat() if job.completed_at else None,
        "result_movie_id": job.result_movie_id,
        "result_confidence": job.result_confidence,
        "result": job.result_json,
        "steps_done": job.steps_done,
        "error": job.error,
    }


async def set_status(db: AsyncSession, job_id: str, status: str, **fields: Any) -> None:
    values: dict[str, Any] = {"status": status}
    if status in {"completed", "failed", "cancelled"}:
        values["completed_at"] = datetime.now(timezone.utc)
    values.update(fields)
    await db.execute(update(SearchJob).where(SearchJob.id == job_id).values(**values))
    await db.flush()


async def recover_stalled_jobs(db: AsyncSession, minutes: int = 30) -> int:
    """Reencola trabajos interrumpidos por reinicios del hosting."""
    cutoff = datetime.now(timezone.utc) - timedelta(minutes=minutes)
    result = await db.execute(
        update(SearchJob)
        .where(SearchJob.status.in_(["queued", "running"]), SearchJob.created_at < cutoff)
        .values(status="queued", error="Recuperado tras reinicio")
    )
    await db.flush()
    return result.rowcount or 0


async def pending_jobs(db: AsyncSession, limit: int = 10) -> list[SearchJob]:
    result = await db.execute(
        select(SearchJob).where(SearchJob.status == "queued").order_by(SearchJob.created_at).limit(limit)
    )
    return list(result.scalars().all())
