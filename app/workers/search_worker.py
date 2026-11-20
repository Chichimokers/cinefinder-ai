"""Worker de búsquedas persistentes (recuperable tras reinicios)."""

from __future__ import annotations

import asyncio

from app.database.session import get_sessionmaker
from app.services.jobs import recover_stalled_jobs, set_status
from app.services.research import run_search_job
from app.utils.logging import get_logger

logger = get_logger(__name__)


async def process_pending_jobs(limit: int = 5) -> int:
    from app.services.jobs import pending_jobs

    processed = 0
    async with get_sessionmaker()() as db:
        await recover_stalled_jobs(db)
        jobs = await pending_jobs(db, limit=limit)
        await db.commit()
        for job in jobs:
            try:
                await set_status(db, job.id, "running")
                await run_search_job(
                    db, job.id, job.query or "", [], job.image_urls, job.requester_id
                )
                processed += 1
            except Exception as exc:  # noqa: BLE001
                logger.warning("worker_job_failed", extra={"error": type(exc).__name__})
                await set_status(db, job.id, "failed", error=type(exc).__name__)
                await db.commit()
    return processed


async def run_search_worker(interval_seconds: int = 10) -> None:
    while True:
        try:
            await process_pending_jobs()
        except Exception:  # noqa: BLE001
            logger.warning("worker_cycle_failed")
        await asyncio.sleep(interval_seconds)
