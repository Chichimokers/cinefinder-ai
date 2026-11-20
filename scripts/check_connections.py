#!/usr/bin/env python3
"""Comprueba todas las conexiones externas sin imprimir secretos.

Uso: py -3.12 scripts/check_connections.py
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.config import settings  # noqa: E402
from app.database.session import check_database  # noqa: E402
from app.utils.security import redact  # noqa: E402


async def check_nvidia(client: httpx.AsyncClient) -> dict:
    if not settings.nvidia_api_key:
        return {"ok": False, "detail": "NVIDIA_API_KEY no configurada"}
    headers = {"Authorization": f"Bearer {settings.nvidia_api_key}"}
    try:
        resp = await client.get(
            f"{settings.nvidia_base_url}/models", headers=headers, timeout=20
        )
        if resp.status_code != 200:
            return {"ok": False, "detail": f"HTTP {resp.status_code} al listar modelos"}
        models = [m.get("id", "") for m in resp.json().get("data", [])]
        available = settings.nvidia_model in models
        detail = (
            f"modelo {settings.nvidia_model} disponible" if available
            else f"modelo {settings.nvidia_model} NO disponible; ejemplos: {', '.join(models[:5])}"
        )
        return {"ok": available, "detail": detail, "model_count": len(models)}
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "detail": f"{type(exc).__name__}: {exc}"}


async def check_render(client: httpx.AsyncClient) -> dict:
    if not settings.render_api_key:
        return {"ok": False, "detail": "RENDER_API_KEY no configurada (solo administración local)"}
    headers = {"Authorization": f"Bearer {settings.render_api_key}", "Accept": "application/json"}
    try:
        resp = await client.get("https://api.render.com/v1/owners", headers=headers, timeout=20)
        if resp.status_code != 200:
            return {"ok": False, "detail": f"HTTP {resp.status_code} (credencial inválida o sin permisos)"}
        owners = resp.json()
        return {"ok": True, "detail": f"autenticado; owners={len(owners)}", "owners": len(owners)}
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "detail": f"{type(exc).__name__}: {exc}"}


async def main() -> int:
    print("=== CineFinder AI - Comprobación de conexiones ===")
    print("(los valores de las credenciales NUNCA se muestran)\n")

    print("[1/4] PostgreSQL / Supabase ...")
    db = await check_database()
    print(f"      {'OK' if db['ok'] else 'FALLO'}: {db.get('detail') or db.get('postgres_version') or db.get('error')}"
          + (f" | SSL={db['ssl']}" if db.get("ssl") else ""))

    async with httpx.AsyncClient() as client:
        print("[2/4] NVIDIA NIM ...")
        nvidia = await check_nvidia(client)
        print(f"      {'OK' if nvidia['ok'] else 'FALLO'}: {nvidia['detail']}")

        print("[3/4] Render API (administración local) ...")
        render = await check_render(client)
        print(f"      {'OK' if render['ok'] else 'FALLO'}: {render['detail']}")

    print("[4/4] Telegram (configuración local) ...")
    telegram_ok = bool(settings.telegram_api_id and settings.telegram_api_hash and settings.telegram_phone)
    print(f"      {'OK' if telegram_ok else 'FALLO'}: API_ID/HASH/TELéfono "
          f"{'presentes' if telegram_ok else 'incompletos'} | OWNER_ID={redact(str(settings.telegram_owner_id))}")

    print("\nProveedores opcionales:", ", ".join(settings.configured_search_providers) or "ninguno")
    all_ok = db["ok"] and nvidia["ok"] and render["ok"] and telegram_ok
    print(f"\nRESULTADO GLOBAL: {'OK' if all_ok else 'REVISIONES PENDIENTES'}")
    return 0 if all_ok else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
