#!/usr/bin/env python3
"""Prueba integral del agente: tools reales + persistencia en PostgreSQL."""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.database.session import db_session
from app.services.jobs import create_job, get_job, job_to_dict
from app.services.research import run_search_job


async def main() -> int:
    async with db_session() as db:
        job = await create_job(
            db, origin="api", requester_id="smoke-test",
            query="Busca una película de ciencia ficción titulada CineFinder Test Poster",
        )
        await db.flush()
        job_id = job.id

    async with db_session() as db:
        result = await run_search_job(
            db, job_id,
            "Busca una película de ciencia ficción titulada CineFinder Test Poster",
            [], requester_id="smoke-test",
        )

    async with db_session() as db:
        job = await get_job(db, job_id)
        status = await job_to_dict(job)

    print("Estado del job:", status["status"], "| pasos:", status["steps_done"])
    print("Herramientas usadas:", ", ".join(result.get("tools_called", [])) or "(ninguna)")
    print("Confianza:", result.get("confidence"))
    print("Respuesta (recortada):", str(result.get("answer"))[:600].replace("\n", " "))
    print("Uso:", result.get("usage"))
    ok = status["status"] == "completed" and bool(result.get("tools_called"))
    print("RESULTADO:", "OK" if ok else "REVISAR")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
