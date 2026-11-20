"""Punto de entrada FastAPI de CineFinder AI."""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager

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
    try:
        from app.database.session import check_database, dispose_engine

        db_status = await check_database()
        if not db_status.get("ok"):
            logger.warning("database_not_ready_at_startup")
    except Exception as exc:  # noqa: BLE001
        logger.warning("startup_check_failed", extra={"error": type(exc).__name__})

    # El userbot se conecta de forma diferida: Render Free puede no mantenerlo activo.
    yield

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
