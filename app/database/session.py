"""Motor asíncrono y sesiones de base de datos."""

from __future__ import annotations

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
    session = get_sessionmaker()()
    try:
        yield session
    finally:
        await session.close()


@asynccontextmanager
async def db_session() -> AsyncGenerator[AsyncSession, None]:
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
    global _engine, _sessionmaker
    if _engine is not None:
        await _engine.dispose()
    _engine = None
    _sessionmaker = None
