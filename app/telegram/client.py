"""Clientes Telegram (Telethon) con reconexión y detección de sesiones revocadas.

Se usan DOS clientes independientes con la MISMA sesión:
- ``get_telegram_client``: para RPC puntuales (publicar, enviar, buscar).
- ``get_listener_client``: para el bucle de escucha de eventos (run_until_disconnected).

Telethon no permite compartir un único cliente entre un bucle de eventos
persistente y llamadas RPC concurrentes de forma fiable; por eso se separan.
"""

from __future__ import annotations

import asyncio
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from telethon import TelegramClient
from telethon.errors import RPCError, SessionPasswordNeededError
from telethon.sessions import StringSession
from telethon.tl.functions.channels import CreateChannelRequest

from app.config import settings
from app.database.models import SystemSetting, TelegramChannel
from app.utils.logging import get_logger

logger = get_logger(__name__)

_client: TelegramClient | None = None
# Un único cliente para escucha + RPC: dos clientes con la misma StringSession
# provocan AuthKeyDuplicatedError incluso desde la misma IP en algunos casos.
_listener: TelegramClient | None = None
_auth_failed = False
_reconnect_attempts = 0


def _make_client() -> TelegramClient | None:
    if not (settings.telegram_api_id and settings.telegram_api_hash and settings.telegram_session):
        return None
    return TelegramClient(
        StringSession(settings.telegram_session),
        settings.telegram_api_id,
        settings.telegram_api_hash,
        connection_retries=3,
        retry_delay=2,
        auto_reconnect=True,
    )


def get_telegram_client() -> TelegramClient | None:
    """Cliente para operaciones RPC puntuales."""
    global _client
    if _auth_failed:
        return None
    if _client is None:
        _client = _make_client()
    return _client


def get_listener_client() -> TelegramClient | None:
    """El listener usa el MISMO cliente único (evita duplicar la sesión)."""
    return get_telegram_client()


async def _authorize(client: TelegramClient) -> tuple[bool, str]:
    try:
        if not client.is_connected():
            await client.connect()
        if not await client.is_user_authorized():
            return False, "Sesión no autorizada (revocada)"
        me = await client.get_me()
        expected = settings.telegram_owner_id
        if expected and me and me.id != expected:
            logger.warning("telegram_identity_mismatch")
            return False, "La cuenta autenticada no coincide con TELEGRAM_OWNER_ID"
        return True, f"Conectado como usuario {me.id if me else 'desconocido'}"
    except (RPCError, SessionPasswordNeededError, OSError) as exc:
        return False, f"{type(exc).__name__} al conectar"


async def connect_telegram() -> tuple[bool, str]:
    """Conecta y valida el cliente de RPC. No reintenta si la sesión fue revocada."""
    global _auth_failed, _reconnect_attempts
    if _auth_failed:
        return False, "Sesión revocada: se requiere nueva autenticación local"
    client = get_telegram_client()
    if client is None:
        return False, "Faltan credenciales de Telegram o TELEGRAM_SESSION"
    ok, detail = await _authorize(client)
    if ok:
        _reconnect_attempts = 0
    else:
        _reconnect_attempts += 1
        if _reconnect_attempts >= 3 or "revocada" in detail:
            _auth_failed = True
        logger.warning("telegram_connect_failed", extra={"detail": detail})
    return ok, detail


async def connect_listener() -> tuple[bool, str]:
    """Conecta y valida el cliente de escucha de forma independiente."""
    global _auth_failed
    if _auth_failed:
        return False, "Sesión revocada"
    client = get_listener_client()
    if client is None:
        return False, "Faltan credenciales de Telegram o TELEGRAM_SESSION"
    ok, detail = await _authorize(client)
    if not ok:
        logger.warning("telegram_listener_connect_failed", extra={"detail": detail})
    return ok, detail


async def disconnect_telegram() -> None:
    global _client, _listener
    if _client and _client.is_connected():
        await _client.disconnect()
    _client = None
    _listener = None


async def ensure_catalog_channel(db: AsyncSession) -> dict:
    """Crea (si no existe) el canal privado 'CineFinder AI — Movie Index'."""
    result = await db.execute(
        select(SystemSetting).where(SystemSetting.key == "telegram_catalog_channel_id")
    )
    setting = result.scalar_one_or_none()
    channel_id = None
    if setting and isinstance(setting.value, dict):
        channel_id = setting.value.get("channel_id")
    if not channel_id and settings.telegram_catalog_channel_id:
        channel_id = settings.telegram_catalog_channel_id

    if channel_id:
        result = await db.execute(
            select(TelegramChannel).where(TelegramChannel.channel_id == int(channel_id))
        )
        channel = result.scalar_one_or_none()
        return {"created": False, "channel_id": int(channel_id), "title": channel.title if channel else None}

    ok, detail = await connect_telegram()
    if not ok:
        return {"created": False, "error": detail}
    client = get_telegram_client()
    assert client is not None

    response = await client(CreateChannelRequest(
        title="CineFinder AI — Movie Index",
        about="Catálogo privado de películas indexadas por CineFinder AI. No reenviar.",
        broadcast=True,
        megagroup=False,
    ))
    chat = response.chats[0]
    channel_id = int(chat.id)

    db.add(TelegramChannel(
        channel_id=channel_id,
        title=getattr(chat, "title", None),
        username=getattr(chat, "username", None),
        channel_type="channel",
        is_private=True,
        purpose="catalog",
    ))
    db.add(SystemSetting(
        key="telegram_catalog_channel_id",
        value={"channel_id": channel_id, "title": getattr(chat, "title", None)},
    ))
    await db.flush()
    return {"created": True, "channel_id": channel_id, "title": getattr(chat, "title", None)}


async def publish_to_catalog(
    db: AsyncSession, channel_id: int, caption: str, photo: bytes | str | None = None
) -> int:
    client = get_telegram_client()
    if client is None or not client.is_connected():
        raise RuntimeError("Userbot no conectado")
    if photo:
        message = await client.send_file(channel_id, photo, caption=caption)
    else:
        message = await client.send_message(channel_id, caption)
    return int(message.id)


async def reconnect_loop(stop_event: asyncio.Event) -> None:
    """Bucle de reconexión para el worker de Telegram."""
    while not stop_event.is_set():
        ok, _ = await connect_listener()
        if not ok:
            await asyncio.sleep(15)
            continue
        client = get_listener_client()
        assert client is not None
        try:
            await client.run_until_disconnected()
        except Exception:  # noqa: BLE001
            logger.warning("telegram_disconnected_retrying")
        await asyncio.sleep(5)
