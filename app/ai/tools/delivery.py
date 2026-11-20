"""Herramientas de entrega de contenido autorizado."""

from __future__ import annotations

from app.ai.tools.base import Tool, ToolContext, registry
from app.delivery.service import (
    check_distribution_rights,
    cleanup,
    download_authorized_media,
    find_authorized_media,
    verify_file_integrity,
)
from app.telegram.client import get_telegram_client


async def find_media(ctx: ToolContext) -> dict:
    return await find_authorized_media(ctx.db, ctx.extra.get("query") or ctx.query)


async def rights(ctx: ToolContext) -> dict:
    return await check_distribution_rights(
        ctx.db, source_id=ctx.extra.get("source_id"), url=ctx.extra.get("url")
    )


async def download(ctx: ToolContext) -> dict:
    url = ctx.extra.get("url", "")
    if not url.startswith(("http://", "https://")):
        return {"downloaded": False, "reason": "URL no http(s)"}
    return await download_authorized_media(
        ctx.db,
        url,
        source_id=ctx.extra.get("source_id"),
        expected_sha256=ctx.extra.get("expected_sha256"),
    )


async def verify_integrity(ctx: ToolContext) -> dict:
    return verify_file_integrity(ctx.extra.get("path", ""), ctx.extra.get("sha256", ""))


async def send_file(ctx: ToolContext) -> dict:
    path = ctx.extra.get("path", "")
    requester = ctx.extra.get("requester_id") or ctx.requester_id
    if not requester or not str(requester).lstrip("-").isdigit():
        return {"sent": False, "reason": "Destinatario no válido"}
    info = verify_file_integrity(path, ctx.extra.get("sha256", ""))
    if not info["valid"]:
        return {"sent": False, "reason": "Integridad no verificada"}
    client = get_telegram_client()
    if client is None or not client.is_connected():
        return {"sent": False, "reason": "Userbot no conectado"}
    try:
        message = await client.send_file(int(requester), path, caption="Contenido autorizado")
        return {"sent": True, "message_id": message.id}
    finally:
        cleanup(path)


async def send_link(ctx: ToolContext) -> dict:
    from app.delivery.service import send_authorized_link

    requester = ctx.extra.get("requester_id") or ctx.requester_id
    if not requester:
        return {"sent": False, "reason": "Destinatario no válido"}
    return await send_authorized_link(ctx.extra.get("url", ""), str(requester))


def register() -> None:
    registry.register(Tool(
        name="find_authorized_media",
        description="Busca fuentes con autorización de distribución verificable.",
        parameters={
            "type": "object",
            "properties": {"query": {"type": "string"}},
            "required": ["query"],
        },
        handler=find_media,
    ))
    registry.register(Tool(
        name="check_distribution_rights",
        description="Comprueba si una fuente permite la distribución del contenido.",
        parameters={
            "type": "object",
            "properties": {"source_id": {"type": "integer"}, "url": {"type": "string"}},
            "required": [],
        },
        handler=rights,
    ))
    registry.register(Tool(
        name="download_authorized_media",
        description="Descarga un archivo de una fuente autorizada verificando SHA-256.",
        parameters={
            "type": "object",
            "properties": {
                "url": {"type": "string"},
                "source_id": {"type": "integer"},
                "expected_sha256": {"type": "string"},
            },
            "required": ["url"],
        },
        handler=download,
    ))
    registry.register(Tool(
        name="verify_file_integrity",
        description="Verifica el SHA-256 de un archivo temporal.",
        parameters={
            "type": "object",
            "properties": {"path": {"type": "string"}, "sha256": {"type": "string"}},
            "required": ["path", "sha256"],
        },
        handler=verify_integrity,
    ))
    registry.register(Tool(
        name="send_authorized_file",
        description="Entrega un archivo autorizado al solicitante por Telegram (borra el temporal).",
        parameters={
            "type": "object",
            "properties": {
                "path": {"type": "string"},
                "sha256": {"type": "string"},
                "requester_id": {"type": "string"},
            },
            "required": ["path", "sha256"],
        },
        handler=send_file,
        permission="admin",
    ))
    registry.register(Tool(
        name="send_authorized_link",
        description="Envía un enlace de fuente autorizada al solicitante.",
        parameters={
            "type": "object",
            "properties": {"url": {"type": "string"}, "requester_id": {"type": "string"}},
            "required": ["url"],
        },
        handler=send_link,
        permission="admin",
    ))
