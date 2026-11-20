"""Cliente Telegram (Telethon) con reconexión y detección de sesiones revocadas."""

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
_auth_failed = False
_reconnect_attempts = 0


def get_telegram_client() -> TelegramClient | None:
    """Devuelve el cliente configurado; None si no hay sesión disponible."""
    global _client
    if _auth_failed:
        return None
    if _client is None:
        if not (settings.telegram_api_id and settings.telegram_api_hash and settings.telegram_session):
            return None
        _client = TelegramClient(
            StringSession(settings.telegram_session),
            settings.telegram_api_id,
            settings.telegram_api_hash,
            connection_retries=3,
            retry_delay=2,
            auto_reconnect=True,
        )
    return _client


async def connect_telegram() -> tuple[bool, str]:
    """Conecta y valida la sesión. No reintenta autenticación si fue revocada."""
    global _auth_failed, _reconnect_attempts
    if _auth_failed:
        return False, "Sesión revocada: se requiere nueva autenticación local"

    client = get_telegram_client()
    if client is None:
        return False, "Faltan credenciales de Telegram o TELEGRAM_SESSION"

    try:
        if not client.is_connected():
            await client.connect()
        if not await client.is_user_authorized():
            _auth_failed = True
            return False, "Sesión no autorizada (revocada)"
        me = await client.get_me()
        expected = settings.telegram_owner_id
        if expected and me and me.id != expected:
            logger.warning("telegram_identity_mismatch")
            return False, "La cuenta autenticada no coincide con TELEGRAM_OWNER_ID"
        _reconnect_attempts = 0
        return True, f"Conectado como usuario {me.id if me else 'desconocido'}"
    except (RPCError, SessionPasswordNeededError, OSError) as exc:
        _reconnect_attempts += 1
        if _reconnect_attempts >= 3:
            _auth_failed = True
        return False, f"{type(exc).__name__} al conectar"


async def disconnect_telegram() -> None:
    global _client
    if _client and _client.is_connected():
        await _client.disconnect()
    _client = None


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
        ok, _ = await connect_telegram()
        if not ok:
            await asyncio.sleep(15)
            continue
        client = get_telegram_client()
        assert client is not None
        try:
            await client.run_until_disconnected()
        except Exception:  # noqa: BLE001
            logger.warning("telegram_disconnected_retrying")
        await asyncio.sleep(5)
