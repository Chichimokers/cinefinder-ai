#!/usr/bin/env python3
"""Valida la sesión local y la exporta a Render.

Exporta TELEGRAM_SESSION, TELEGRAM_PHONE y TELEGRAM_OWNER_ID para que la
cuenta vinculada (número +5358126024 por defecto) quede coherente en producción.

Uso: py -3.12 scripts/export_session.py
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from telethon import TelegramClient
from telethon.sessions import StringSession

from app.config import settings
from scripts import render_client
from scripts.setup_credentials import read_env


async def validate_local_session() -> tuple[bool, str | None]:
    session_string = read_env().get("TELEGRAM_SESSION") or settings.telegram_session
    if not session_string:
        return False, None
    client = TelegramClient(
        StringSession(session_string), settings.telegram_api_id, settings.telegram_api_hash
    )
    try:
        await client.connect()
        if not await client.is_user_authorized():
            return False, session_string
        me = await client.get_me()
        if settings.telegram_owner_id and me.id != settings.telegram_owner_id:
            return False, session_string
        return True, session_string
    except Exception:  # noqa: BLE001
        return False, session_string
    finally:
        if client.is_connected():
            await client.disconnect()


async def main() -> int:
    print("Validando sesión local de Telegram…")
    valid, session_string = await validate_local_session()
    if not valid or not session_string:
        print("La sesión local no es válida. Ejecuta authenticate_telegram.py primero.")
        return 1
    print("Sesión local válida.")

    service = await render_client.find_service()
    if service is None:
        print("No se encontró el servicio 'cinefinder-api' en Render.")
        return 1
    service_id = service.get("id")
    print(f"Servicio Render identificado: {service.get('name')} ({service_id}).")

    existing = await render_client.list_env_vars(service_id)
    print(f"Variables existentes preservadas: {len(existing)} (valores no mostrados).")

    local = read_env()
    values = {
        "TELEGRAM_SESSION": session_string,
        "TELEGRAM_PHONE": local.get("TELEGRAM_PHONE", settings.telegram_phone),
        "TELEGRAM_OWNER_ID": local.get("TELEGRAM_OWNER_ID", str(settings.telegram_owner_id)),
    }
    all_ok = True
    for key, value in values.items():
        if not value:
            continue
        ok = await render_client.set_env_var(service_id, key, str(value))
        verified = await render_client.verify_env_var(service_id, key)
        print(f"{key}: exportada={'sí' if ok else 'NO'} | verificada={'sí' if verified else 'NO'}")
        all_ok = all_ok and ok and verified

    print("IMPORTANTE: desconecta la sesión local antes de iniciar el worker en Render;")
    print("Telegram no permite la misma sesión conectada dos veces simultáneamente.")
    return 0 if all_ok else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
