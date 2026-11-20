#!/usr/bin/env python3
"""Configura y despliega CineFinder AI en Render de forma segura.

- NUNCA instala RENDER_API_KEY en el servicio de producción.
- Inspecciona las variables existentes y las actualiza UNA A UNA.
- Nunca muestra valores de secretos.

Uso: py -3.12 scripts/deploy_render.py
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.config import settings
from scripts import render_client
from scripts.setup_credentials import read_env

PRODUCTION_SECRETS = [
    "NVIDIA_API_KEY",
    "SUPABASE_DATABASE_URL",
    "TELEGRAM_API_ID",
    "TELEGRAM_API_HASH",
    "TELEGRAM_PHONE",
    "TELEGRAM_OWNER_ID",
    "TELEGRAM_SESSION",
    "TELEGRAM_CATALOG_CHANNEL_ID",
    "API_ACCESS_TOKEN",
    "SESSION_SECRET",
    "JOB_ENCRYPTION_KEY",
]

PRODUCTION_SETTINGS = [
    "NVIDIA_MODEL",
    "NVIDIA_BASE_URL",
    "AGENT_MAX_STEPS",
    "AGENT_MAX_PARALLEL_TOOLS",
    "AGENT_TIMEOUT_SECONDS",
    "AGENT_MAX_CANDIDATES",
    "RATE_LIMIT_PER_MINUTE",
]

FORBIDDEN = {"RENDER_API_KEY"}


@render_client.resilient()
async def get_owner_id() -> str | None:
    async with httpx.AsyncClient(timeout=30) as client:
        resp = await client.get(
            "https://api.render.com/v1/owners", headers={
                "Authorization": f"Bearer {settings.render_api_key}",
                "Accept": "application/json",
            },
        )
    if resp.status_code != 200:
        return None
    owners = resp.json()
    if not owners:
        return None
    owner = owners[0]
    return owner.get("owner", owner).get("id")


@render_client.resilient()
async def create_service(repo_url: str) -> dict | None:
    owner_id = await get_owner_id()
    if not owner_id:
        print("No se pudo obtener el ownerId de Render.")
        return None
    clean_repo = repo_url.strip()
    if clean_repo.endswith(".git"):
        clean_repo = clean_repo[:-4]
    async with httpx.AsyncClient(timeout=60) as client:
        resp = await client.post(
            "https://api.render.com/v1/services",
            headers={
                "Authorization": f"Bearer {settings.render_api_key}",
                "Accept": "application/json",
                "Content-Type": "application/json",
            },
            json={
                "type": "web_service",
                "name": "cinefinder-api",
                "ownerId": owner_id,
                "repo": clean_repo,
                "autoDeployTrigger": "commit",
                "envVars": [],
                "serviceDetails": {
                    "runtime": "python",
                    "plan": "free",
                    "region": "oregon",
                    "healthCheckPath": "/api/v1/health",
                    "envSpecificDetails": {
                        "buildCommand": "pip install -r requirements.txt",
                        "startCommand": "uvicorn app.main:app --host 0.0.0.0 --port $PORT",
                    },
                },
            },
        )
    if resp.status_code not in (200, 201):
        message = ""
        try:
            message = str(resp.json().get("message", ""))[:200]
        except Exception:  # noqa: BLE001
            pass
        print(f"No se pudo crear el servicio (HTTP {resp.status_code}). {message} "
              "Créalo desde render.yaml en el dashboard y vuelve a ejecutar.")
        return None
    data = resp.json()
    return data.get("service", data)


async def main() -> int:
    print("=== Despliegue de CineFinder AI en Render ===")
    service = await render_client.find_service()
    if service is None:
        repo_url = read_env().get("RENDER_REPO_URL", "")
        if not repo_url:
            print("No existe el servicio 'cinefinder-api' y no hay RENDER_REPO_URL configurada.")
            print("Para crearlo necesito la URL del repositorio GitHub que contendrá render.yaml.")
            print("Añádela con: py -3.12 scripts\\setup_credentials.py --key RENDER_REPO_URL")
            return 1
        service = await create_service(repo_url)
        if service is None:
            return 1
        print(f"Servicio creado: {service.get('name')} ({service.get('id')})")
    else:
        print(f"Servicio existente: {service.get('name')} ({service.get('id')})")

    service_id = service["id"]
    local = read_env()

    # 1) Inspeccionar variables existentes (sin mostrar valores).
    existing = await render_client.list_env_vars(service_id)
    print(f"Variables existentes inspeccionadas: {len(existing)} (valores no mostrados).")

    # 2) Actualizar individualmente, preservando las demás.
    to_configure = PRODUCTION_SECRETS + PRODUCTION_SETTINGS
    missing_locally = []
    for key in to_configure:
        if key in FORBIDDEN:
            continue
        value = local.get(key, "")
        if not value:
            missing_locally.append(key)
            continue
        ok = await render_client.set_env_var(service_id, key, value)
        print(f"  {key}: {'actualizada' if ok else 'ERROR'}")

    if "TELEGRAM_SESSION" in missing_locally:
        print("  TELEGRAM_SESSION pendiente: ejecuta authenticate_telegram.py y export_session.py.")
    if missing_locally:
        print(f"Variables sin valor local: {', '.join(missing_locally)}")

    # 3) Verificación sin mostrar valores.
    print("Verificando variables en Render…")
    verified = 0
    for key in PRODUCTION_SECRETS:
        if key in missing_locally:
            continue
        if await render_client.verify_env_var(service_id, key):
            verified += 1
    print(f"Verificadas correctamente: {verified}/{len(PRODUCTION_SECRETS) - missing_locally.count('TELEGRAM_SESSION')}")

    # 4) Desplegar y comprobar estado real.
    deploy = await render_client.trigger_deploy(service_id)
    deploy_id = deploy.get("id")
    print(f"Deploy solicitado: {deploy_id}")
    if deploy_id:
        info = await render_client.get_deploy(service_id, deploy_id)
        print(f"Estado inicial del deploy: {info.get('status')}")
        logs = await render_client.get_logs(service_id, deploy_id)
        print("Últimas líneas de log (redactadas):")
        from app.utils.security import redact_message
        print("\n".join(redact_message(line) for line in logs.splitlines()[-12:]))
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
