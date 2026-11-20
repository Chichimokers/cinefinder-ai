"""Entrega de contenido autorizado.

Reglas estrictas:
- Solo fuentes marcadas como autorizadas en ``authorized_sources`` o con licencia
  explícita abierta verificable.
- Nunca se descarga contenido comercial sin autorización, se elude DRM o se
  ignoran controles de acceso.
- Los archivos temporales se eliminan tras la entrega.
"""

from __future__ import annotations

import os
import tempfile
from pathlib import Path
from typing import Any

import httpx
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database.models import AuthorizedSource, MediaAsset
from app.search.providers import search_internet_archive
from app.utils.logging import get_logger
from app.utils.security import sha256_file

logger = get_logger(__name__)

MAX_DOWNLOAD_BYTES = 200 * 1024 * 1024  # 200 MB: nunca vídeos grandes en disco efímero
OPEN_LICENSE_TOKENS = (
    "public domain", "cc0", "cc-by", "creative commons", "open access", "free license",
)


async def find_authorized_media(db: AsyncSession, query: str) -> dict:
    result = await db.execute(
        select(AuthorizedSource).where(AuthorizedSource.authorized.is_(True))
    )
    local = [
        {
            "id": s.id,
            "name": s.name,
            "url": s.url,
            "license": s.license,
            "type": s.source_type,
        }
        for s in result.scalars().all()
    ]
    archive = await search_internet_archive(query, max_results=5)
    external = [
        r for r in archive.get("results", [])
        if r.get("license") and any(t in str(r["license"]).lower() for t in OPEN_LICENSE_TOKENS)
    ]
    return {"authorized_sources": local, "external_candidates": external}


async def check_distribution_rights(
    db: AsyncSession, source_id: int | None = None, url: str | None = None
) -> dict:
    if source_id is not None:
        source = await db.get(AuthorizedSource, source_id)
        if source is None:
            return {"authorized": False, "reason": "Fuente no registrada"}
        if not source.authorized:
            return {"authorized": False, "reason": "Fuente no autorizada explícitamente"}
        return {"authorized": True, "license": source.license, "name": source.name}
    if url:
        result = await db.execute(
            select(AuthorizedSource).where(
                AuthorizedSource.url == url, AuthorizedSource.authorized.is_(True)
            )
        )
        source = result.scalar_one_or_none()
        if source is None:
            return {"authorized": False, "reason": "URL no registrada como fuente autorizada"}
        return {"authorized": True, "license": source.license, "name": source.name}
    return {"authorized": False, "reason": "No se aportó fuente"}


async def download_authorized_media(
    db: AsyncSession,
    url: str,
    source_id: int | None = None,
    expected_sha256: str | None = None,
) -> dict:
    rights = await check_distribution_rights(db, source_id=source_id, url=url)
    if not rights["authorized"]:
        return {"downloaded": False, "reason": rights["reason"]}

    temp_dir = Path(tempfile.gettempdir()) / "cinefinder"
    temp_dir.mkdir(parents=True, exist_ok=True)
    tmp_path = temp_dir / f"asset_{os.getpid()}_{len(list(temp_dir.iterdir()))}.bin"

    size = 0
    digest_sha = ""
    try:
        async with httpx.AsyncClient(timeout=60, follow_redirects=True) as client:
            async with client.stream("GET", url) as resp:
                if resp.status_code >= 400:
                    return {"downloaded": False, "reason": f"HTTP {resp.status_code}"}
                declared = resp.headers.get("content-length")
                if declared and int(declared) > MAX_DOWNLOAD_BYTES:
                    return {"downloaded": False, "reason": "Archivo demasiado grande"}
                import hashlib
                h = hashlib.sha256()
                with open(tmp_path, "wb") as fh:
                    async for chunk in resp.aiter_bytes(65536):
                        size += len(chunk)
                        if size > MAX_DOWNLOAD_BYTES:
                            fh.close()
                            tmp_path.unlink(missing_ok=True)
                            return {"downloaded": False, "reason": "Límite de tamaño excedido"}
                        h.update(chunk)
                        fh.write(chunk)
                digest_sha = h.hexdigest()
    except Exception as exc:  # noqa: BLE001
        tmp_path.unlink(missing_ok=True)
        return {"downloaded": False, "reason": f"{type(exc).__name__}"}

    if expected_sha256 and digest_sha != expected_sha256:
        tmp_path.unlink(missing_ok=True)
        return {"downloaded": False, "reason": "SHA-256 no coincide"}

    return {
        "downloaded": True,
        "path": str(tmp_path),
        "size_bytes": size,
        "sha256": digest_sha,
        "license": rights.get("license"),
    }


def verify_file_integrity(path: str, expected_sha256: str) -> dict:
    if not os.path.exists(path):
        return {"valid": False, "reason": "Archivo no encontrado"}
    actual = sha256_file(path)
    return {"valid": actual == expected_sha256, "sha256": actual}


async def register_asset(db: AsyncSession, movie_id: int, info: dict) -> MediaAsset:
    asset = MediaAsset(
        movie_id=movie_id,
        source_id=info.get("source_id"),
        sha256=info["sha256"],
        size_bytes=info.get("size_bytes", 0),
        storage_path=info.get("path"),
        mime_type=info.get("mime_type"),
        license=info.get("license"),
        status="available",
    )
    db.add(asset)
    await db.flush()
    return asset


def cleanup(path: str | None) -> None:
    if path and os.path.exists(path):
        try:
            os.remove(path)
        except OSError:
            pass


async def send_authorized_link(link: str, requester_id: str) -> dict:
    """Envía un enlace autorizado al solicitante mediante el userbot."""
    from app.telegram.client import get_telegram_client

    client = get_telegram_client()
    if not client or not client.is_connected():
        return {"sent": False, "reason": "Userbot no conectado"}
    await client.send_message(int(requester_id), f"Enlace autorizado: {link}")
    return {"sent": True}
