"""Punto de entrada FastAPI de CineFinder AI."""

from __future__ import annotations

import asyncio
import os
from contextlib import asynccontextmanager
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from app.api.routes import router
from app.config import settings
from app.utils.logging import configure_logging, get_logger

logger = get_logger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    configure_logging()
    logger.info("cinefinder_starting", extra={"config": settings.public_dict()})

    # Migraciones automáticas controladas (no comandos generados por el modelo).
    # Se ejecutan en un subproceso: Alembic usa asyncio.run internamente y no
    # puede ejecutarse dentro del event loop de FastAPI.
    try:
        import subprocess
        import sys

        result = subprocess.run(
            [sys.executable, "-m", "alembic", "upgrade", "head"],
            cwd=str(ROOT), capture_output=True, text=True, timeout=180,
        )
        if result.returncode != 0:
            from app.utils.security import redact_message

            logger.warning("alembic_upgrade_failed", extra={"detail": redact_message(result.stderr[-300:])})
        else:
            logger.info("alembic_upgrade_ok")
    except Exception as exc:  # noqa: BLE001
        logger.warning("alembic_upgrade_failed", extra={"error": type(exc).__name__})

    try:
        from app.database.session import check_database, dispose_engine

        db_status = await check_database()
        if not db_status.get("ok"):
            logger.warning("database_not_ready_at_startup")
    except Exception as exc:  # noqa: BLE001
        logger.warning("startup_check_failed", extra={"error": type(exc).__name__})

    # Userbot opcional dentro del mismo proceso (ENABLE_TELEGRAM_WORKER=true).
    telegram_task: asyncio.Task | None = None
    if os.environ.get("ENABLE_TELEGRAM_WORKER", "").lower() in {"1", "true", "yes"}:
        try:
            from app.telegram.client import connect_telegram, disconnect_telegram, get_telegram_client
            from app.workers.telegram_worker import register_handlers

            ok, detail = await connect_telegram()
            if ok:
                client = get_telegram_client()
                if client is not None:
                    register_handlers(client)
                    telegram_task = asyncio.create_task(client.run_until_disconnected())
                    logger.info("telegram_worker_started_in_web_service")
            else:
                logger.warning("telegram_worker_not_started", extra={"detail": detail})
        except Exception as exc:  # noqa: BLE001
            logger.warning("telegram_worker_start_failed", extra={"error": type(exc).__name__})

    yield

    if telegram_task is not None:
        telegram_task.cancel()
        await asyncio.gather(telegram_task, return_exceptions=True)
        try:
            from app.telegram.client import disconnect_telegram

            await disconnect_telegram()
        except Exception:  # noqa: BLE001
            pass

    try:
        from app.database.session import dispose_engine

        await dispose_engine()
    except Exception:  # noqa: BLE001
        pass
    logger.info("cinefinder_stopped")


app = FastAPI(
    title="CineFinder AI API",
    description="API protegida del agente autónomo de investigación cinematográfica.",
    version="1.0.0",
    lifespan=lifespan,
    docs_url="/docs",
    redoc_url=None,
    openapi_url=None,  # se expone protegido en /api/v1/openapi.json
)

app.include_router(router)


@app.middleware("http")
async def rate_limit_middleware(request: Request, call_next):
    from app.api.deps import check_rate_limit

    path = request.url.path
    if path.startswith("/api/v1/") and path not in {
        "/api/v1/health", "/api/v1/ready",
    }:
        if not check_rate_limit(request):
            return JSONResponse(
                status_code=429,
                content={"detail": "Límite de peticiones por minuto excedido"},
            )
    return await call_next(request)


@app.exception_handler(Exception)
async def unhandled_exception_handler(request: Request, exc: Exception) -> JSONResponse:
    logger.error("unhandled_exception", extra={"error": type(exc).__name__, "path": request.url.path})
    return JSONResponse(
        status_code=500,
        content={"detail": "Error interno del servidor"},
    )
