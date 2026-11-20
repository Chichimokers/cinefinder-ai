"""Cliente mínimo de la API de Render (administración local).

Endpoints verificados contra el OpenAPI oficial de Render:
- GET  /v1/services?name=...
- GET  /v1/services/{id}/env-vars
- PUT  /v1/services/{id}/env-vars/{key}   body: {"value": "..."}
- POST /v1/services/{id}/deploys
- GET  /v1/services/{id}/deploys/{deployId}/logs
"""

from __future__ import annotations

from typing import Any

import httpx

from app.config import settings

BASE = "https://api.render.com/v1"


def _headers() -> dict[str, str]:
    if not settings.render_api_key:
        raise RuntimeError("RENDER_API_KEY no configurada")
    return {
        "Authorization": f"Bearer {settings.render_api_key}",
        "Accept": "application/json",
        "Content-Type": "application/json",
    }


async def find_service(name: str = "cinefinder-api") -> dict | None:
    async with httpx.AsyncClient(timeout=30) as client:
        resp = await client.get(f"{BASE}/services", params={"name": name}, headers=_headers())
        if resp.status_code != 200:
            raise RuntimeError(f"Render devolvió HTTP {resp.status_code} al listar servicios")
        items = resp.json()
    if not items:
        return None
    # La respuesta es una lista de objetos {"service": {...}}.
    entry = items[0]
    return entry.get("service", entry)


async def list_env_vars(service_id: str) -> dict[str, Any]:
    """Devuelve {clave: {'value': str, 'id': ...}} SIN imprimir valores."""
    async with httpx.AsyncClient(timeout=30) as client:
        resp = await client.get(f"{BASE}/services/{service_id}/env-vars", headers=_headers())
        if resp.status_code != 200:
            raise RuntimeError(f"Render devolvió HTTP {resp.status_code} al listar variables")
        data = resp.json()
    result: dict[str, Any] = {}
    for item in data:
        env = item.get("envVar", item)
        key = env.get("key")
        if key:
            result[key] = env
    return result


async def set_env_var(service_id: str, key: str, value: str) -> bool:
    """Actualiza UNA variable, preservando el resto."""
    async with httpx.AsyncClient(timeout=30) as client:
        resp = await client.put(
            f"{BASE}/services/{service_id}/env-vars/{key}",
            headers=_headers(),
            json={"value": value},
        )
    return resp.status_code in (200, 201)


async def verify_env_var(service_id: str, key: str) -> bool:
    """Comprueba que la variable existe y tiene valor, sin mostrarlo."""
    async with httpx.AsyncClient(timeout=30) as client:
        resp = await client.get(
            f"{BASE}/services/{service_id}/env-vars/{key}", headers=_headers()
        )
    if resp.status_code != 200:
        return False
    env = resp.json().get("envVar", resp.json())
    return bool(env.get("value"))


async def trigger_deploy(service_id: str, clear_cache: bool = False) -> dict:
    async with httpx.AsyncClient(timeout=30) as client:
        resp = await client.post(
            f"{BASE}/services/{service_id}/deploys",
            headers=_headers(),
            json={"clearCache": "clear" if clear_cache else "do_not_clear"},
        )
        if resp.status_code not in (200, 201, 202):
            raise RuntimeError(f"Render devolvió HTTP {resp.status_code} al desplegar")
        return resp.json()


async def get_deploy(service_id: str, deploy_id: str) -> dict:
    async with httpx.AsyncClient(timeout=30) as client:
        resp = await client.get(
            f"{BASE}/services/{service_id}/deploys/{deploy_id}", headers=_headers()
        )
        if resp.status_code != 200:
            raise RuntimeError(f"Render devolvió HTTP {resp.status_code} al consultar deploy")
        return resp.json()


async def get_logs(service_id: str, deploy_id: str) -> str:
    async with httpx.AsyncClient(timeout=60) as client:
        resp = await client.get(
            f"{BASE}/services/{service_id}/deploys/{deploy_id}/logs", headers=_headers()
        )
        if resp.status_code != 200:
            return f"(logs no disponibles: HTTP {resp.status_code})"
        return resp.text
