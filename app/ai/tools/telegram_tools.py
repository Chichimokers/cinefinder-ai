"""Herramienta de búsqueda en Telegram restringida a fuentes autorizadas."""

from __future__ import annotations

from sqlalchemy import select

from app.ai.tools.base import Tool, ToolContext, registry
from app.database.models import AppUser, AuthorizedSource
from app.telegram.client import get_telegram_client

MAX_MESSAGES_PER_CHAT = 50
# Límites estrictos para la búsqueda global en los diálogos del userbot.
MAX_DIALOGS_TO_SCAN = 120
MAX_MESSAGES_PER_DIALOG = 20
MAX_GLOBAL_RESULTS = 100


async def search_telegram_authorized(ctx: ToolContext) -> dict:
    """Busca solo en chats explícitamente autorizados (sin indexación masiva)."""
    result = await ctx.db.execute(
        select(AuthorizedSource).where(
            AuthorizedSource.source_type == "telegram",
            AuthorizedSource.authorized.is_(True),
        )
    )
    import re

    authorized_chats: list[int] = []
    for source in result.scalars().all():
        candidate = source.url or source.name or ""
        numbers = re.findall(r"-?\d{5,}", candidate)
        if numbers:
            authorized_chats.append(int(numbers[0]))

    user_result = await ctx.db.execute(
        select(AppUser).where(AppUser.is_authorized.is_(True), AppUser.is_banned.is_(False))
    )
    authorized_chats.extend(u.telegram_id for u in user_result.scalars().all())

    if ctx.requester_id and str(ctx.requester_id).lstrip("-").isdigit():
        rid = int(ctx.requester_id)
        if rid not in authorized_chats:
            authorized_chats.append(rid)

    if not authorized_chats:
        return {"results": [], "reason": "No hay chats autorizados para búsqueda"}

    client = get_telegram_client()
    if client is None or not client.is_connected():
        return {"results": [], "reason": "Userbot no conectado"}

    query = ctx.extra.get("query") or ctx.query
    found = []
    for chat_id in authorized_chats[:10]:
        try:
            count = 0
            async for message in client.iter_messages(chat_id, search=query or None, limit=MAX_MESSAGES_PER_CHAT):
                found.append({
                    "chat_id": chat_id,
                    "message_id": message.id,
                    "text": message.text,
                    "date": message.date.isoformat() if message.date else None,
                    "has_media": bool(message.media),
                })
                count += 1
                if count >= MAX_MESSAGES_PER_CHAT:
                    break
        except Exception:  # noqa: BLE001
            continue
    return {"results": found, "chats_searched": len(authorized_chats[:10]), "reason": None}


async def search_telegram_all_channels(ctx: ToolContext) -> dict:
    """Busca en TODOS los diálogos (chats/canales) accesibles al userbot.

    Solo para el propietario. Búsqueda bajo demanda, sin indexación persistente
    ni redistribución de contenido de terceros. Límites estrictos por diseño.
    """
    query = (ctx.extra.get("query") or ctx.query or "").strip()
    if not query:
        return {"results": [], "reason": "Se requiere un texto de búsqueda"}

    client = get_telegram_client()
    if client is None or not client.is_connected():
        return {"results": [], "reason": "Userbot no conectado"}

    results: list[dict] = []
    dialogs_seen = 0
    errors = 0
    try:
        async for dialog in client.iter_dialogs(limit=MAX_DIALOGS_TO_SCAN):
            if dialogs_seen >= MAX_DIALOGS_TO_SCAN or len(results) >= MAX_GLOBAL_RESULTS:
                break
            entity = dialog.entity
            kind = getattr(entity, "class_name", "") or type(entity).__name__
            # Saltar usuarios individuales: el foco son canales y grupos accesibles.
            if kind in ("User", "UserEmpty"):
                continue
            dialogs_seen += 1
            title = getattr(dialog, "name", None) or getattr(entity, "title", None) or "sin título"
            try:
                async for message in client.iter_messages(
                    entity, search=query, limit=MAX_MESSAGES_PER_DIALOG
                ):
                    results.append({
                        "chat": str(title)[:120],
                        "chat_id": int(dialog.id),
                        "message_id": message.id,
                        "date": message.date.isoformat() if message.date else None,
                        "text": (message.text or "")[:400],
                        "has_media": bool(message.media),
                    })
                    if len(results) >= MAX_GLOBAL_RESULTS:
                        break
            except Exception:  # noqa: BLE001
                errors += 1
                continue
    except Exception:  # noqa: BLE001
        errors += 1

    return {
        "results": results,
        "dialogs_scanned": dialogs_seen,
        "total": len(results),
        "limits": {
            "max_dialogs": MAX_DIALOGS_TO_SCAN,
            "max_per_dialog": MAX_MESSAGES_PER_DIALOG,
            "max_results": MAX_GLOBAL_RESULTS,
        },
        "chats_with_errors": errors,
        "reason": None,
    }


def register() -> None:
    registry.register(Tool(
        name="search_telegram_all_channels",
        description=(
            "Busca un texto en TODOS los chats y canales accesibles al userbot del propietario. "
            "Solo el propietario; bajo demanda, con límites y sin indexación."
        ),
        parameters={
            "type": "object",
            "properties": {"query": {"type": "string"}},
            "required": ["query"],
        },
        handler=search_telegram_all_channels,
        permission="admin",
    ))
    registry.register(Tool(
        name="search_telegram_authorized",
        description="Busca mensajes solo en chats de Telegram autorizados (máx. 50 por chat).",
        parameters={
            "type": "object",
            "properties": {"query": {"type": "string"}},
            "required": ["query"],
        },
        handler=search_telegram_authorized,
        permission="user",
    ))
