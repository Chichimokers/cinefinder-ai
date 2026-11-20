"""Motor asíncrono y sesiones de base de datos."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager

from sqlalchemy import text
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from app.config import settings
from app.database.base import Base
from app.utils.logging import get_logger

logger = get_logger(__name__)

_engine: AsyncEngine | None = None
_sessionmaker: async_sessionmaker[AsyncSession] | None = None
_connectivity_ok = False


async def ensure_connectivity() -> None:
    """Reintenta la primera conexión ante fallos DNS transitorios del entorno."""
    global _connectivity_ok
    if _connectivity_ok:
        return
    last_error: Exception | None = None
    for attempt in range(6):
        try:
            async with get_engine().connect() as conn:
                await conn.execute(text("SELECT 1"))
            _connectivity_ok = True
            return
        except Exception as exc:  # noqa: BLE001
            last_error = exc
            await asyncio.sleep(1.5)
    raise RuntimeError(f"No se pudo conectar a PostgreSQL: {type(last_error).__name__}")


def get_engine() -> AsyncEngine:
    global _engine
    if _engine is None:
        if not settings.supabase_database_url:
            raise RuntimeError("SUPABASE_DATABASE_URL no configurada")
        _engine = create_async_engine(
            settings.supabase_database_url,
            echo=False,
            pool_size=settings.db_pool_max_size,
            max_overflow=0,
            pool_pre_ping=True,
            pool_recycle=1800,
            connect_args={"ssl": "require", "server_settings": {"application_name": "cinefinder"}},
        )
    return _engine


def get_sessionmaker() -> async_sessionmaker[AsyncSession]:
    global _sessionmaker
    if _sessionmaker is None:
        _sessionmaker = async_sessionmaker(
            bind=get_engine(),
            expire_on_commit=False,
            autoflush=False,
        )
    return _sessionmaker


async def get_db() -> AsyncGenerator[AsyncSession, None]:
    await ensure_connectivity()
    session = get_sessionmaker()()
    try:
        yield session
    finally:
        await session.close()


@asynccontextmanager
async def db_session() -> AsyncGenerator[AsyncSession, None]:
    await ensure_connectivity()
    session = get_sessionmaker()()
    try:
        yield session
        await session.commit()
    except Exception:
        await session.rollback()
        raise
    finally:
        await session.close()


async def check_database() -> dict:
    """Comprueba conexión y devuelve información segura (sin credenciales)."""
    try:
        await ensure_connectivity()
        engine = get_engine()
        async with engine.connect() as conn:
            result = await conn.execute(text("SELECT 1 AS ok, version() AS version"))
            row = result.mappings().one()
        return {
            "ok": True,
            "ssl": "require",
            "postgres_version": str(row["version"]).split(",")[0],
        }
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": type(exc).__name__}


async def init_db() -> None:
    """Crea tablas si no existen (útil en desarrollo; producción usa Alembic)."""
    engine = get_engine()
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    logger.info("Tablas verificadas/creadas correctamente")


async def dispose_engine() -> None:
    global _engine, _sessionmaker, _connectivity_ok
    if _engine is not None:
        await _engine.dispose()
    _engine = None
    _sessionmaker = None
    _connectivity_ok = False
