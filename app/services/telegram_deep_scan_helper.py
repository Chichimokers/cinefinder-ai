"""Utilidades de conectividad para el motor de búsqueda profunda."""

from __future__ import annotations

import asyncio

from app.telegram.client import connect_telegram, get_telegram_client


async def ensure_telegram_ready(retries: int = 12, delay: float = 2.0) -> bool:
    """Espera a que el userbot esté conectado (el lifespan puede ir detrás)."""
    for attempt in range(retries):
        client = get_telegram_client()
        if client is not None and client.is_connected():
            return True
        ok, _ = await connect_telegram()
        if ok:
            return True
        await asyncio.sleep(delay)
    return False
