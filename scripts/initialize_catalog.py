#!/usr/bin/env python3
"""Inicialización del catálogo: tablas, propietario, fuentes autorizadas y canal privado.

Uso: py -3.12 scripts/initialize_catalog.py
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sqlalchemy import select

from app.config import settings
from app.database.models import AppUser, AuthorizedSource
from app.database.session import db_session, init_db
from app.telegram.client import ensure_catalog_channel


SEED_SOURCES = [
    {
        "name": "Wikimedia Commons",
        "source_type": "images",
        "url": "https://commons.wikimedia.org/",
        "license": "CC BY-SA / dominio público (según cada archivo)",
        "authorized": True,
        "notes": "Solo imágenes con licencia abierta; verificar cada archivo.",
    },
    {
        "name": "Internet Archive (metadata)",
        "source_type": "metadata",
        "url": "https://archive.org/advancedsearch.php",
        "license": "Metadatos públicos",
        "authorized": True,
        "notes": "Metadatos públicos; el contenido solo si la licencia lo permite.",
    },
    {
        "name": "The Movie Database",
        "source_type": "metadata",
        "url": "https://www.themoviedb.org/",
        "license": "Metadatos, uso con API Key",
        "authorized": True,
        "notes": "Metadatos y carteles; no distribución de películas.",
    },
]


async def main() -> int:
    await init_db()
    async with db_session() as db:
        owner = await db.execute(
            select(AppUser).where(AppUser.telegram_id == settings.telegram_owner_id)
        )
        if owner.scalar_one_or_none() is None:
            db.add(AppUser(
                telegram_id=settings.telegram_owner_id,
                is_owner=True,
                is_authorized=True,
            ))
            print("Propietario registrado en app_users.")

        for item in SEED_SOURCES:
            exists = await db.execute(
                select(AuthorizedSource).where(AuthorizedSource.name == item["name"])
            )
            if exists.scalar_one_or_none() is None:
                db.add(AuthorizedSource(**item))
        print("Fuentes autorizadas verificadas.")

    channel = await _ensure_channel()
    if channel.get("error"):
        print(f"Canal privado pendiente: {channel['error']}")
    else:
        print(f"Canal privado de índices: ID={channel.get('channel_id')} "
              f"({'creado' if channel.get('created') else 'existente'}). Valores no sensibles.")
    return 0


async def _ensure_channel() -> dict:
    async with db_session() as db:
        return await ensure_catalog_channel(db)


if __name__ == "__main__":
    result = asyncio.run(main())
    raise SystemExit(result)
