"""Worker de Telegram: comandos autorizados, álbumes y deduplicación."""

from __future__ import annotations

import asyncio
from collections import defaultdict
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from telethon import Button, events
from telethon.tl.types import Message

from app.catalog.service import get_movie_by_code, search_catalog
from app.config import settings
from app.database.models import AppUser, AuthorizedSource, SystemSetting, TelegramMessage
from app.database.session import get_sessionmaker
from app.services.jobs import create_job
from app.services.research import run_search_job
from app.telegram.client import connect_telegram, get_telegram_client
from app.utils.logging import get_logger
from app.utils.security import hash_image

logger = get_logger(__name__)

COMMANDS = (
    "!busca", "!info", "!stats", "!ultimas", "!ayuda", "!estado",
    "!autorizar", "!desautorizar", "!enviar",
)
MAX_MEDIA_OFFERS = 5
ALBUM_DEBOUNCE_SECONDS = 1.5

HELP = """🤖 CineFinder AI — Comandos
!busca [texto] — busca por texto e imágenes (responde con fotos)
!info CF-000001 — ficha de una película
!stats — estadísticas del catálogo
!ultimas — últimas incorporationes
!estado — estado del sistema
!autorizar <ID> [usuario] — autoriza a otro usuario (solo propietario)
!desautorizar <ID> — revoca la autorización (solo propietario)
!enviar <n> — envía el archivo de Telegram elegido (solo fuentes autorizadas)
!ayuda — este mensaje
"""


async def is_authorized(db: AsyncSession, user_id: int) -> bool:
    if settings.telegram_owner_id and user_id == settings.telegram_owner_id:
        return True
    result = await db.execute(
        select(AppUser).where(
            AppUser.telegram_id == user_id,
            AppUser.is_authorized.is_(True),
            AppUser.is_banned.is_(False),
        )
    )
    return result.scalar_one_or_none() is not None


async def register_event(db: AsyncSession, message: Message, image_hashes: list[str]) -> bool:
    """Deduplica eventos. Devuelve False si ya fue procesado."""
    exists = await db.execute(
        select(TelegramMessage).where(
            TelegramMessage.chat_id == message.chat_id,
            TelegramMessage.message_id == message.id,
        )
    )
    if exists.scalar_one_or_none():
        return False
    db.add(TelegramMessage(
        chat_id=message.chat_id,
        message_id=message.id,
        sender_id=getattr(message, "sender_id", None),
        text=message.text,
        image_hashes=image_hashes,
        grouped_id=str(message.grouped_id) if message.grouped_id else None,
    ))
    await db.flush()
    return True


async def download_images(messages: list[Message]) -> list[bytes]:
    images: list[bytes] = []
    for message in messages:
        if not message.media:
            continue
        try:
            data = await message.download_media(file=bytes)
            if isinstance(data, bytes):
                images.append(data)
        except Exception:  # noqa: BLE001
            continue
    return images


async def answer(message: Message, text: str) -> None:
    client = get_telegram_client()
    if client and client.is_connected():
        await client.send_message(message.chat_id, text, reply_to=message.id)


async def handle_busca(message: Message, query: str, images: list[bytes]) -> None:
    await answer(message, "🔍 Iniciando investigación con todas las fuentes disponibles…")
    client = get_telegram_client()

    async def progress(event: dict) -> None:
        """Muestra en vivo el razonamiento y las herramientas del agente."""
        if client is None or not client.is_connected():
            return
        kind = event.get("type")
        step = event.get("step")
        text = ""
        if kind == "step":
            text = f"🧠 *Paso {step}* — {event.get('message', '')}"
        elif kind == "thinking":
            thought = (event.get("message") or "").strip()[:420]
            if thought:
                text = f"💭 *Razonamiento:* {thought}"
        elif kind == "tool_result":
            data = event.get("data") or {}
            tool = data.get("tool", "herramienta")
            state = "OK" if data.get("ok") else "sin datos"
            text = f"🔧 *{tool}* → {state}\n{(data.get('summary') or '')[:260]}"
        if text:
            try:
                await client.send_message(message.chat_id, text)
            except Exception:  # noqa: BLE001
                pass

    async with get_sessionmaker()() as db:
        job = await create_job(
            db,
            origin="telegram",
            requester_id=str(message.chat_id),
            query=query,
            image_hashes=[hash_image(i) for i in images],
        )
        await db.commit()
        result = await run_search_job(
            db, job.id, query, images,
            requester_id=str(message.chat_id),
            is_owner=message.sender_id == settings.telegram_owner_id,
            progress=progress,
        )
    await answer(message, result.get("formatted_answer") or "⚠️ Sin respuesta del agente.")
    if result.get("telegram_media") and message.sender_id == settings.telegram_owner_id:
        await offer_telegram_media(message, job.id, result["telegram_media"])


async def handle_info(message: Message, code: str) -> None:
    async with get_sessionmaker()() as db:
        movie = await get_movie_by_code(db, code)
    if movie is None:
        await answer(message, "❌ Código no encontrado.")
        return
    genres = ", ".join(movie.genres or [])
    await answer(
        message,
        f"🎬 {movie.code} — {movie.original_title}\n"
        f"Año: {movie.year or '?'}\nGéneros: {genres or '?'}\n"
        f"Confianza: {movie.confidence:.2f} | Verificación: {movie.verification_status}\n"
        f"{movie.synopsis or ''}"[:3500],
    )


async def handle_stats(message: Message) -> None:
    from sqlalchemy import func
    from app.database.models import Movie, SearchJob

    async with get_sessionmaker()() as db:
        movies = await db.execute(select(func.count()).select_from(Movie))
        jobs = await db.execute(select(func.count()).select_from(SearchJob))
    await answer(message, f"📊 Catálogo: {movies.scalar()} películas | Investigaciones: {jobs.scalar()}")


async def handle_ultimas(message: Message) -> None:
    from app.database.models import Movie

    async with get_sessionmaker()() as db:
        result = await db.execute(select(Movie).order_by(Movie.created_at.desc()).limit(5))
        movies = result.scalars().all()
    if not movies:
        await answer(message, "Aún no hay películas registradas.")
        return
    lines = [f"{m.code} — {m.original_title} ({m.year or '?'})" for m in movies]
    await answer(message, "🕒 Últimas incorporaciones:\n" + "\n".join(lines))


async def handle_estado(message: Message) -> None:
    ok, detail = await connect_telegram()
    nvidia_ok = bool(settings.nvidia_api_key and settings.nvidia_model)
    await answer(
        message,
        f"🟢 Estado: Telegram {'OK' if ok else 'ERROR'} | NVIDIA {'OK' if nvidia_ok else 'ERROR'}\n"
        f"Proveedores opcionales: {', '.join(settings.configured_search_providers) or 'ninguno'}",
    )


async def set_user_authorization(message: Message, argument: str, authorized: bool) -> None:
    if message.sender_id != settings.telegram_owner_id:
        await answer(message, "⛔ Solo el propietario puede autorizar usuarios.")
        return
    parts = argument.split()
    if not parts or not parts[0].lstrip("-").isdigit():
        await answer(message, "Uso: !autorizar <ID_numérico> [usuario]")
        return
    telegram_id = int(parts[0])
    username = parts[1] if len(parts) > 1 else None
    async with get_sessionmaker()() as db:
        result = await db.execute(
            select(AppUser).where(AppUser.telegram_id == telegram_id)
        )
        user = result.scalar_one_or_none()
        if user is None:
            user = AppUser(telegram_id=telegram_id, username=username)
            db.add(user)
        if username:
            user.username = username
        user.is_authorized = authorized
        user.is_owner = False
        await db.flush()
    action = "autorizado" if authorized else "desautorizado"
    await answer(message, f"✅ Usuario {telegram_id} {action}.")


def _telegram_link(chat_id: int, message_id: int) -> str:
    raw = str(abs(chat_id))
    if chat_id < 0 and raw.startswith("100"):
        return f"https://t.me/c/{raw[3:]}/{message_id}"
    return f"tg://openmessage?chat_id={chat_id}&message_id={message_id}"


async def _pending_key(chat_id: int) -> str:
    return f"pending_media:{chat_id}"


async def offer_telegram_media(message: Message, job_id: str, media: list[dict]) -> None:
    """Pregunta al propietario cuál de los archivos encontrados desea recibir."""
    if not media:
        return
    client = get_telegram_client()
    if client is None or not client.is_connected():
        return
    offers = media[:MAX_MEDIA_OFFERS]
    lines = ["📎 *Archivos encontrados en tus canales de Telegram*",
             "¿Cuál quieres que te envíe? (responde con el número o pulsa el botón)"]
    for index, item in enumerate(offers, start=1):
        text = (item.get("text") or "").replace("\n", " ")[:90] or "sin descripción"
        lines.append(f"{index}. {item.get('chat', 'chat')} — {text}")
    buttons = [Button.inline(str(i), data=f"cfmedia:{job_id}:{i - 1}".encode())
               for i in range(1, len(offers) + 1)]
    rows = [buttons[i:i + 4] for i in range(0, len(buttons), 4)]
    rows.append([Button.inline("Cancelar", data=b"cfmedia:cancel")])

    async with get_sessionmaker()() as db:
        key = await _pending_key(message.chat_id)
        result = await db.execute(select(SystemSetting).where(SystemSetting.key == key))
        setting = result.scalar_one_or_none()
        value = {"job_id": job_id, "media": offers}
        if setting is None:
            db.add(SystemSetting(key=key, value=value))
        else:
            setting.value = value
        await db.flush()

    await client.send_message(message.chat_id, "\n".join(lines), buttons=rows)


async def deliver_selected_media(chat_id: int, sender_id: int, job_id: str | None, index: int) -> None:
    """Entrega el archivo elegido solo si la fuente está autorizada."""
    if sender_id != settings.telegram_owner_id:
        await answer_safe(chat_id, "⛔ Solo el propietario puede solicitar archivos.")
        return

    async with get_sessionmaker()() as db:
        key = await _pending_key(chat_id)
        result = await db.execute(select(SystemSetting).where(SystemSetting.key == key))
        setting = result.scalar_one_or_none()
        if setting is None or not setting.value:
            await answer_safe(chat_id, "No hay una selección pendiente. Haz otra búsqueda primero.")
            return
        value = setting.value
        if job_id and value.get("job_id") != job_id:
            await answer_safe(chat_id, "La selección corresponde a otra búsqueda.")
            return
        media = value.get("media") or []
        if index < 0 or index >= len(media):
            await answer_safe(chat_id, "Número de opción no válido.")
            return
        item = media[index]
        await db.delete(setting)
        await db.flush()

        source_result = await db.execute(
            select(AuthorizedSource).where(
                AuthorizedSource.source_type == "telegram",
                AuthorizedSource.authorized.is_(True),
                AuthorizedSource.url.contains(str(item["chat_id"])),
            )
        )
        source = source_result.scalar_one_or_none()

    link = _telegram_link(item["chat_id"], item["message_id"])
    if source is None:
        await answer_safe(
            chat_id,
            "⚠️ Ese chat no está registrado como fuente autorizada para entrega automática.\n"
            f"Te dejo el enlace al mensaje original: {link}\n"
            "Para autorizarlo: py -3.12 scripts\\authorize_source.py "
            f"{item['chat_id']} --name \"{item.get('chat', 'canal')}\"",
        )
        return

    client = get_telegram_client()
    if client is None or not client.is_connected():
        await answer_safe(chat_id, "Userbot no conectado.")
        return
    try:
        messages = await client.get_messages(item["chat_id"], ids=[item["message_id"]])
        if not messages or not messages[0].media:
            await answer_safe(chat_id, "El mensaje ya no contiene archivos.")
            return
        data = await messages[0].download_media(file=bytes)
        if not isinstance(data, bytes):
            await answer_safe(chat_id, "No se pudo descargar el archivo.")
            return
        import hashlib

        digest = hashlib.sha256(data).hexdigest()
        caption = f"📦 Archivo autorizado · fuente: {source.name}\nSHA-256: {digest}"
        await client.send_file(chat_id, data, caption=caption)
        await answer_safe(chat_id, f"✅ Enviado. Origen: {link}")
    except Exception as exc:  # noqa: BLE001
        await answer_safe(chat_id, f"No se pudo entregar: {type(exc).__name__}. Enlace: {link}")


async def answer_safe(chat_id: int, text: str) -> None:
    client = get_telegram_client()
    if client and client.is_connected():
        await client.send_message(chat_id, text)


async def process_group(images: list[bytes], texts: list[str], message: Message) -> None:
    query = " ".join(t for t in texts if t and not t.startswith("!")).strip()
    await handle_busca(message, query, images)


def register_handlers(client: Any) -> None:
    albums: dict[str, list[Message]] = defaultdict(list)
    album_tasks: dict[str, asyncio.Task] = {}

    async def flush_album(group_id: str) -> None:
        await asyncio.sleep(ALBUM_DEBOUNCE_SECONDS)
        messages = albums.pop(group_id, [])
        album_tasks.pop(group_id, None)
        if not messages:
            return
        async with get_sessionmaker()() as db:
            first = messages[0]
            if not await register_event(db, first, []):
                return
            await db.commit()
        images = await download_images(messages)
        texts = [m.text or "" for m in messages]
        await process_group(images, texts, first)

    @client.on(events.NewMessage())
    async def handler(event: events.NewMessage.Event) -> None:
        message = event.message
        if not message:
            return
        sender_id = getattr(message, "sender_id", None)
        if sender_id is None or sender_id == settings.telegram_owner_id:
            pass  # mensajes guardados / propio dueño: permitidos
        async with get_sessionmaker()() as db:
            if not await is_authorized(db, sender_id or 0):
                return

        text = message.text or ""
        parts = text.strip().split(maxsplit=1)
        command = parts[0].lower() if parts else ""
        argument = parts[1] if len(parts) > 1 else ""

        if command not in COMMANDS:
            return

        if command == "!busca":
            if message.grouped_id:
                group_id = str(message.grouped_id)
                albums[group_id].append(message)
                if group_id in album_tasks:
                    album_tasks[group_id].cancel()
                album_tasks[group_id] = asyncio.create_task(flush_album(group_id))
                return
            async with get_sessionmaker()() as db:
                if not await register_event(db, message, []):
                    return
                await db.commit()
            images = await download_images([message])
            await handle_busca(message, argument, images)
        elif command == "!info":
            await handle_info(message, argument.strip())
        elif command == "!stats":
            await handle_stats(message)
        elif command == "!ultimas":
            await handle_ultimas(message)
        elif command == "!ayuda":
            await answer(message, HELP)
        elif command == "!estado":
            await handle_estado(message)
        elif command == "!autorizar":
            await set_user_authorization(message, argument, True)
        elif command == "!desautorizar":
            await set_user_authorization(message, argument, False)
        elif command == "!enviar":
            parts = argument.split()
            if not parts or not parts[0].isdigit():
                await answer(message, "Uso: !enviar <número de opción>")
            else:
                await deliver_selected_media(
                    message.chat_id, sender_id or 0, None, int(parts[0]) - 1
                )

    @client.on(events.CallbackQuery(pattern=rb"^cfmedia:"))
    async def media_callback(event: events.CallbackQuery.Event) -> None:
        data = event.data.decode("utf-8", "replace")
        parts = data.split(":")
        target_chat = getattr(event, "chat_id", None) or event.sender_id
        await event.answer()
        if len(parts) >= 2 and parts[1] == "cancel":
            async with get_sessionmaker()() as db:
                key = await _pending_key(target_chat)
                result = await db.execute(select(SystemSetting).where(SystemSetting.key == key))
                setting = result.scalar_one_or_none()
                if setting:
                    await db.delete(setting)
            await answer_safe(target_chat, "Selección cancelada.")
            return
        if len(parts) != 3 or not parts[2].isdigit():
            await answer_safe(target_chat, "Selección no válida.")
            return
        await deliver_selected_media(target_chat, event.sender_id, parts[1], int(parts[2]))


async def run_telegram_worker() -> None:
    ok, detail = await connect_telegram()
    if not ok:
        logger.warning("telegram_worker_not_connected", extra={"detail": detail})
        return
    client = get_telegram_client()
    assert client is not None
    register_handlers(client)
    await client.run_until_disconnected()
