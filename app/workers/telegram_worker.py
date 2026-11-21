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
from app.services.access_control import (
    UNAUTHORIZED_MESSAGE,
    authorize,
    check_access,
    is_owner_id,
    list_authorized,
    revoke,
    user_info,
)
from app.database.models import AppUser, AuthorizedSource, FileSearch, SystemSetting, TelegramMessage
from app.database.session import get_sessionmaker
from app.search.image_similarity import dhash
from app.services.jobs import create_job
from app.services.telegram_deep_search import create_file_search
from app.workers.deep_search_worker import run_deep_search_job
from app.services.research import run_search_job
from app.telegram.client import connect_telegram, get_telegram_client
from app.utils.logging import get_logger
from app.utils.security import hash_image

logger = get_logger(__name__)

COMMANDS = (
    "!busca", "!info", "!stats", "!ultimas", "!ayuda", "!estado",
    "!autorizar", "!desautorizar", "!autorizados", "!id",
    "!enviar", "!archivos", "!infoarchivo", "!enviararchivo", "!cancelar",
)
ADMIN_COMMANDS = {
    "/autorizar", "!autorizar", "/desautorizar", "!desautorizar",
    "/autorizados", "!autorizados", "/info", "!infoadmin", "/id", "!id",
}
MAX_MEDIA_OFFERS = 5
ALBUM_DEBOUNCE_SECONDS = 1.5

HELP = """🤖 CineFinder AI — Comandos
!busca [texto] — busca por texto e imágenes (responde con fotos)
!info CF-000001 — ficha de una película
!stats — estadísticas del catálogo
!ultimas — últimas incorporationes
!estado — estado del sistema
/autorizar @usuario — autoriza a un usuario (solo propietario)
/desautorizar @usuario — revoca la autorización (solo propietario)
/autorizados — lista de usuarios autorizados (solo propietario)
/info @usuario — información de autorización (solo propietario; también !infoadmin)
/id — muestra tu Telegram User ID
!enviar <n> — envía el archivo de Telegram elegido (solo fuentes autorizadas)
!archivos [texto] — búsqueda profunda en TODO tu historial de Telegram (admitime imagen)
!infoarchivo <n> — detalles de un resultado de búsqueda profunda
!enviararchivo <n> — solicita un archivo de la búsqueda profunda
!cancelar — cancela la búsqueda profunda en curso
!ayuda — este mensaje
"""


async def is_authorized(db: AsyncSession, user_id: int) -> bool:
    """Delegado al servicio central de acceso (validación en cada interacción)."""
    from app.services.access_control import is_authorized as _is_authorized

    return await _is_authorized(db, user_id)


async def handle_admin_command(message: Message, command: str, argument: str) -> bool:
    """Comandos administrativos exclusivos del propietario. Devuelve si se procesó."""
    if command not in ADMIN_COMMANDS:
        return False
    sender_id = getattr(message, "sender_id", None)
    if not is_owner_id(sender_id):
        await answer(message, "⛔ Este comando es exclusivo del propietario.")
        return True
    async with get_sessionmaker()() as db:
        if command in {"/autorizar", "!autorizar"}:
            result = await authorize(db, argument)
            await answer(message, result.get("message") or result.get("error", "Error"))
        elif command in {"/desautorizar", "!desautorizar"}:
            result = await revoke(db, argument)
            await answer(message, result.get("message") or result.get("error", "Error"))
        elif command in {"/autorizados", "!autorizados"}:
            await answer(message, await list_authorized(db))
        elif command in {"/info", "!infoadmin"}:
            await answer(message, await user_info(db, argument))
        elif command in {"/id", "!id"}:
            await answer(message, f"🆔 Tu Telegram User ID: {sender_id}")
    return True


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
    if result.get("telegram_media"):
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


async def _pending_key(chat_id: int, user_id: int | None = None) -> str:
    """Clave de sesión por usuario: impide cruzar resultados entre usuarios."""
    owner_key = user_id if is_owner_id(user_id) else (user_id or chat_id)
    return f"pending_media:{owner_key}"


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
        key = await _pending_key(message.chat_id, message.sender_id)
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
    """Entrega el archivo elegido solo a usuarios con acceso y fuente autorizada."""
    async with get_sessionmaker()() as db:
        access = await check_access(db, sender_id)
        if not access["allowed"]:
            await answer_safe(chat_id, UNAUTHORIZED_MESSAGE)
            return

    async with get_sessionmaker()() as db:
        key = await _pending_key(chat_id, sender_id)
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


async def start_deep_search(message: Message, query: str, images: list[bytes]) -> None:
    features = None
    if images:
        try:
            features = {"dhash": dhash(images[0])}
        except Exception:  # noqa: BLE001
            features = None
    await answer(message, "🔎 Descubriendo conversaciones e inspeccionando historiales…")
    async with get_sessionmaker()() as db:
        search = await create_file_search(
            db,
            query=query or "imagen enviada",
            requester_id=str(message.chat_id),
            image_features=features,
        )
        search.scope = {"chat_id": message.chat_id, "origin": "telegram"}
        await db.commit()
        search_id = search.id
    asyncio.create_task(run_deep_search_job(search_id))
    await answer(message, f"✅ Búsqueda profunda encolada (`{search_id}`). Te avisaré al terminar.")


async def _get_pending_files(chat_id: int, user_id: int | None = None) -> tuple[str, list[dict]]:
    key = f"pending_files:{user_id or chat_id}"
    async with get_sessionmaker()() as db:
        result = await db.execute(
            select(SystemSetting).where(SystemSetting.key == key)
        )
        setting = result.scalar_one_or_none()
        if not setting or not setting.value:
            return "", []
        return str(setting.value.get("search_id", "")), list(setting.value.get("results", []))


async def show_file_info(message: Message, argument: str) -> None:
    _, results = await _get_pending_files(message.chat_id, message.sender_id)
    if not results or not argument.isdigit():
        await answer(message, "No hay resultados recientes. Usa !archivos primero.")
        return
    item = results[int(argument) - 1]
    size = (item.get("size_bytes") or 0) / 1024 / 1024
    await answer(
        message,
        f"📄 *{item.get('name') or item.get('title')}*\n"
        f"Tamaño: {size:.1f} MB\nCategoría: {item.get('category')}\n"
        f"Extensión: {item.get('extension')}\nOrigen: chat {item.get('dialog_id')} · mensaje {item.get('message_id')}\n"
        f"Coincidencia: {item.get('score', 0):.0f}%\nReferencia: {item.get('telegram_ref')}",
    )


async def deliver_indexed_file(message: Message, argument: str) -> None:
    async with get_sessionmaker()() as db:
        access = await check_access(db, message.sender_id)
    if not access["allowed"]:
        await answer(message, UNAUTHORIZED_MESSAGE)
        return
    _, results = await _get_pending_files(message.chat_id, message.sender_id)
    if not results or not argument.isdigit():
        await answer(message, "No hay resultados recientes. Usa !archivos primero.")
        return
    index = int(argument) - 1
    if index < 0 or index >= len(results):
        await answer(message, "Número no válido.")
        return
    item = results[index]
    dialog_id, message_id = int(item["dialog_id"]), int(item["message_id"])

    async with get_sessionmaker()() as db:
        source_result = await db.execute(
            select(AuthorizedSource).where(
                AuthorizedSource.source_type == "telegram",
                AuthorizedSource.authorized.is_(True),
                AuthorizedSource.url.contains(str(dialog_id)),
            )
        )
        source = source_result.scalar_one_or_none()

    link = f"tg://openmessage?chat_id={dialog_id}&message_id={message_id}"
    if source is None:
        await answer(
            message,
            "⚠️ Ese chat no está autorizado para entrega automática. "
            f"Enlace al original: {link}\n"
            f"Autorízalo con: py -3.12 scripts\\authorize_source.py {dialog_id}",
        )
        return

    client = get_telegram_client()
    if client is None or not client.is_connected():
        await answer(message, "Userbot no conectado.")
        return
    try:
        messages = await client.get_messages(dialog_id, ids=[message_id])
        if not messages or not messages[0].media:
            await answer(message, "El archivo ya no está disponible.")
            return
        # Reenvío directo: entrega exactamente el archivo seleccionado, sin descargarlo.
        await client.forward_messages(message.chat_id, message_id, dialog_id)
        await answer(message, f"✅ Archivo enviado: {item.get('name') or item.get('title')}")
    except Exception as exc:  # noqa: BLE001
        await answer(message, f"No se pudo enviar: {type(exc).__name__}. Enlace: {link}")


async def cancel_deep_search(message: Message) -> None:
    search_id, _ = await _get_pending_files(message.chat_id, message.sender_id)
    if not search_id:
        await answer(message, "No hay una búsqueda profunda reciente.")
        return
    async with get_sessionmaker()() as db:
        result = await db.execute(select(FileSearch).where(FileSearch.id == search_id))
        search = result.scalar_one_or_none()
        if search:
            search.cancel_requested = True
            await db.commit()
    await answer(message, "🛑 Cancelación solicitada.")


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

    async def _flush_deep_album(group_id: str) -> None:
        await asyncio.sleep(ALBUM_DEBOUNCE_SECONDS)
        messages = albums.pop(group_id, [])
        album_tasks.pop(group_id, None)
        if not messages:
            return
        first = messages[0]
        images = await download_images(messages)
        text = " ".join((m.text or "") for m in messages if not (m.text or "").startswith("!"))
        await start_deep_search(first, text.strip(), images)

    @client.on(events.NewMessage())
    async def handler(event: events.NewMessage.Event) -> None:
        message = event.message
        if not message:
            return
        sender_id = getattr(message, "sender_id", None)

        # Validación de acceso en CADA interacción.
        async with get_sessionmaker()() as db:
            access = await check_access(db, sender_id)

        text = message.text or ""
        parts = text.strip().split(maxsplit=1)
        command = parts[0].lower() if parts else ""
        argument = parts[1] if len(parts) > 1 else ""

        # Comandos administrativos (solo propietario), incluso para usuarios sin autorizar.
        if command in ADMIN_COMMANDS:
            await handle_admin_command(message, command, argument)
            return

        if command == "/start" or command == "!start":
            await answer(message, "🤖 CineFinder AI. Usa !ayuda para ver los comandos.")
            return

        if not access["allowed"]:
            # Solo se responde en chats privados; en grupos se ignora para no molestar.
            if message.chat_id == sender_id:
                await answer(message, UNAUTHORIZED_MESSAGE)
            return

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
        elif command == "!archivos":
            if message.grouped_id:
                group_id = str(message.grouped_id)
                albums[group_id].append(message)
                if group_id in album_tasks:
                    album_tasks[group_id].cancel()
                album_tasks[group_id] = asyncio.create_task(_flush_deep_album(group_id))
                return
            async with get_sessionmaker()() as db:
                if not await register_event(db, message, []):
                    return
                await db.commit()
            await start_deep_search(message, argument, await download_images([message]))
        elif command == "!infoarchivo":
            await show_file_info(message, argument.strip())
        elif command == "!enviararchivo":
            await deliver_indexed_file(message, argument.strip())
        elif command == "!cancelar":
            await cancel_deep_search(message)

    @client.on(events.CallbackQuery(pattern=rb"^cfmedia:"))
    async def media_callback(event: events.CallbackQuery.Event) -> None:
        data = event.data.decode("utf-8", "replace")
        parts = data.split(":")
        target_chat = getattr(event, "chat_id", None) or event.sender_id
        await event.answer()
        async with get_sessionmaker()() as db:
            access = await check_access(db, event.sender_id)
        if not access["allowed"]:
            await answer_safe(target_chat, UNAUTHORIZED_MESSAGE)
            return
        if len(parts) >= 2 and parts[1] == "cancel":
            async with get_sessionmaker()() as db:
                key = await _pending_key(target_chat, event.sender_id)
                result = await db.execute(select(SystemSetting).where(SystemSetting.key == key))
                setting = result.scalar_one_or_none()
                if setting:
                    await db.delete(setting)
            await answer_safe(target_chat, "Selección cancelada.")
            return
        if len(parts) != 3 or not parts[2].isdigit():
            await answer_safe(target_chat, "Selección no válida.")
            return
        if parts[1] != "cancel":
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
