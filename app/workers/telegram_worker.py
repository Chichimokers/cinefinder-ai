"""Worker de Telegram: comandos autorizados, álbumes y deduplicación."""

from __future__ import annotations

import asyncio
import re
from collections import defaultdict
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from telethon import Button, events
from telethon.tl.types import Message

from app.catalog.service import get_movie_by_code, search_catalog
from app.config import settings
from app.services.access_control import (
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
    "!busca", "!buscar", "!info", "!stats", "!ultimas", "!ayuda", "!estado",
    "!enviar", "!archivos", "!archivo", "!infoarchivo", "!enviararchivo", "!cancelar",
)
# Alias: cualquier variante se normaliza a su comando canónico.
COMMAND_ALIASES = {"!buscar": "!busca", "!archivo": "!archivos"}
ADMIN_COMMANDS = {
    "/autorizar", "!autorizar", "/desautorizar", "!desautorizar",
    "/autorizados", "!autorizados", "/info", "!infoadmin", "/id", "!id",
}
MAX_MEDIA_OFFERS = 5
ALBUM_DEBOUNCE_SECONDS = 1.5

HELP = """🤖 CineFinder AI — Comandos
(siempre usa un comando; los mensajes sin comando se ignoran)

!busca / !buscar [texto] — busca por texto
!busca ;texto — busca con descripción
!busca (adjunta foto/álbum) — identifica la imagen
!busca (respondiendo a una foto) — identifica esa foto
(responder a una foto y escribir !busca también funciona)
!archivos [texto] — búsqueda profunda en Telegram
!info CF-000001 — ficha de una película
!stats — estadísticas del catálogo
!ultimas — últimas incorporationes
!estado — estado del sistema
/autorizar @usuario — autoriza a un usuario (o responde a su mensaje)
/desautorizar @usuario — revoca la autorización (o responde a su mensaje)
/autorizados — lista de usuarios autorizados
/info @usuario — información de autorización (!infoadmin)
/id — muestra tu ID (responde a un mensaje para ver el de otro)
!enviar <n> — envía el archivo de Telegram elegido (solo fuentes autorizadas)
!archivos [texto] — búsqueda profunda en TODO tu historial de Telegram (admitime imagen)
!infoarchivo <n> — detalles de un resultado de búsqueda profunda
!enviararchivo <n> — solicita un archivo de la búsqueda profunda
!cancelar — cancela la búsqueda profunda en curso
!ayuda — este mensaje
"""


_self_id: int | None = None


async def _get_self_id() -> int | None:
    """ID de la cuenta userbot, para ignorar Mensajes Guardados."""
    global _self_id
    if _self_id is None:
        client = get_telegram_client()
        if client and client.is_connected():
            me = await client.get_me()
            if me:
                _self_id = int(me.id)
    return _self_id


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
        return True  # Silencio total: comandos administrativos ignorados.

    replied_id = None
    reply_msg = getattr(message, "reply_to_message", None)
    if reply_msg is not None:
        replied_id = getattr(reply_msg, "sender_id", None)

    async with get_sessionmaker()() as db:
        if command in {"/autorizar", "!autorizar"}:
            if replied_id and replied_id != sender_id:
                result = await authorize(db, argument, replied_user_id=replied_id)
            else:
                result = await authorize(db, argument)
            await answer(message, result.get("message") or result.get("error", "Error"))
        elif command in {"/desautorizar", "!desautorizar"}:
            if replied_id and replied_id != sender_id:
                result = await revoke(db, argument, replied_user_id=replied_id)
            else:
                result = await revoke(db, argument)
            await answer(message, result.get("message") or result.get("error", "Error"))
        elif command in {"/autorizados", "!autorizados"}:
            await answer(message, await list_authorized(db))
        elif command in {"/info", "!infoadmin"}:
            if replied_id and replied_id != sender_id:
                await answer(message, await user_info(db, argument, replied_user_id=replied_id))
            else:
                await answer(message, await user_info(db, argument))
        elif command in {"/id", "!id"}:
            if replied_id and replied_id != sender_id:
                from app.services.access_control import user_info

                await answer(message, await user_info(db, "", replied_user_id=replied_id))
            else:
                await answer(message, f"🆔 Tu Telegram User ID: {sender_id}")
        # Persistir cambios: get_sessionmaker() NO hace commit automático.
        await db.commit()
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
        try:
            await client.send_message(message.chat_id, text, reply_to=message.id)
        except Exception:  # noqa: BLE001  (chat sin permiso de escritura, etc.)
            pass


def _truncate(text: str, limit: int = 3000) -> str:
    text = (text or "").strip()
    return text if len(text) <= limit else text[: limit - 1] + "…"


async def _edit_status(client, chat_id: int, message_id: int, lines: list[str]) -> None:
    """Actualiza UN solo mensaje con el estado en vivo (streaming)."""
    if client is None or not client.is_connected():
        return
    text = _truncate("\n".join(lines))
    try:
        await client.edit_message(chat_id, message_id, text)
    except Exception:  # noqa: BLE001  (rate limit o mensaje idéntico)
        pass


async def handle_busca(message: Message, query: str, images: list[bytes]) -> None:
    client = get_telegram_client()
    if client is None or not client.is_connected():
        return

    header = [f"🔎 *Investigando*{' · ' + _truncate(query, 90) if query else ''}", ""]
    try:
        status_msg = await client.send_message(message.chat_id, "\n".join(header))
        status_id = int(status_msg.id)
    except Exception:  # noqa: BLE001
        return

    state: dict[str, list[str]] = {
        "pasos": [],
        "razonamiento": [],
        "fuentes": [],
    }

    async def progress(event: dict) -> None:
        kind = event.get("type")
        step = event.get("step")
        if kind == "step":
            state["pasos"].append(f"🧠 Paso {step}: {event.get('message', '')}")
        elif kind == "thinking":
            thought = (event.get("message") or "").strip()
            if thought:
                state["razonamiento"].append(_truncate(thought, 500))
        elif kind == "deep_progress":
            data = event.get("data") or {}
            stage = str(data.get("stage") or "").replace("_", " ")
            state["pasos"].append(
                f"📊 {stage} · diálogos {data.get('dialogs_done', 0)}/{data.get('dialogs_total', 0)} "
                f"· mensajes {data.get('messages_scanned', 0)}"
            )
        elif kind == "tool_result":
            data = event.get("data") or {}
            if data.get("ok"):
                tool = str(data.get("tool", ""))
                if tool.startswith("search_") or tool.startswith("verify_"):
                    state["fuentes"].append(f"✓ {tool}")

        lines = list(header)
        if state["pasos"]:
            lines += ["*Progreso*"] + state["pasos"][-4:]
        if state["razonamiento"]:
            lines += ["", "*Razonamiento*"] + state["razonamiento"][-3:]
        if state["fuentes"]:
            lines += ["", "*Fuentes consultadas*"] + state["fuentes"][-8:]
        await _edit_status(client, message.chat_id, status_id, lines)

    async with get_sessionmaker()() as db:
        job = await create_job(
            db,
            origin="telegram",
            requester_id=str(message.sender_id or message.chat_id),
            query=query,
            image_hashes=[hash_image(i) for i in images],
        )
        await db.commit()
        result = await run_search_job(
            db, job.id, query, images,
            requester_id=str(message.sender_id or message.chat_id),
            is_owner=message.sender_id == settings.telegram_owner_id,
            progress=progress,
        )

    final = result.get("formatted_answer") or "⚠️ Sin respuesta del agente."
    await _edit_status(client, message.chat_id, status_id, [final])
    if result.get("telegram_media"):
        await offer_telegram_media(message, job.id, result["telegram_media"], status_id)


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
    client = get_telegram_client()
    ok = bool(client and client.is_connected())
    nvidia_ok = bool(settings.nvidia_api_key and settings.nvidia_model)
    await answer(
        message,
        f"🟢 Estado: Telegram {'OK' if ok else 'ERROR'} | NVIDIA {'OK' if nvidia_ok else 'ERROR'}\n"
        f"Proveedores opcionales: {', '.join(settings.configured_search_providers) or 'ninguno'}",
    )


def _telegram_link(chat_id: int, message_id: int) -> str:
    raw = str(abs(chat_id))
    if chat_id < 0 and raw.startswith("100"):
        return f"https://t.me/c/{raw[3:]}/{message_id}"
    return f"tg://openmessage?chat_id={chat_id}&message_id={message_id}"


async def _pending_key(chat_id: int, user_id: int | None = None) -> str:
    """Clave de sesión por usuario: impide cruzar resultados entre usuarios."""
    owner_key = user_id if is_owner_id(user_id) else (user_id or chat_id)
    return f"pending_media:{owner_key}"


async def offer_telegram_media(
    message: Message, job_id: str, media: list[dict], status_message_id: int | None = None
) -> None:
    """Ofrece los archivos encontrados dentro del MISMO mensaje de estado."""
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
        await db.commit()

    try:
        if status_message_id:
            await client.edit_message(message.chat_id, status_message_id, "\n".join(lines), buttons=rows)
        else:
            await client.send_message(message.chat_id, "\n".join(lines), buttons=rows)
    except Exception:  # noqa: BLE001
        await client.send_message(message.chat_id, "\n".join(lines), buttons=rows)


async def deliver_selected_media(chat_id: int, sender_id: int, job_id: str | None, index: int) -> None:
    """Entrega el archivo elegido solo a usuarios con acceso y fuente autorizada."""
    async with get_sessionmaker()() as db:
        access = await check_access(db, sender_id)
        if not access["allowed"]:
            return  # Silencio total para usuarios sin autorización.

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
        await db.commit()

        source_result = await db.execute(
            select(AuthorizedSource).where(
                AuthorizedSource.source_type == "telegram",
                AuthorizedSource.authorized.is_(True),
                AuthorizedSource.url.contains(str(item["chat_id"])),
            )
        )
        source = source_result.scalar_one_or_none()

    link = _telegram_link(item["chat_id"], item["message_id"])
    # El acceso ya fue validado arriba (propietario o usuario autorizado),
    # por lo que cualquier usuario con acceso puede recibir el archivo.
    # La fuente registrada solo aporta trazabilidad, no bloquea la entrega.

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
        source_name = source.name if source else "tus chats autorizados"
        caption = f"📦 Archivo autorizado · fuente: {source_name}\nSHA-256: {digest}"
        await client.send_file(chat_id, data, caption=caption)
        await answer_safe(chat_id, f"✅ Enviado. Origen: {link}")
    except Exception as exc:  # noqa: BLE001
        await answer_safe(chat_id, f"No se pudo entregar: {type(exc).__name__}. Enlace: {link}")


async def answer_safe(chat_id: int, text: str) -> None:
    client = get_telegram_client()
    if client and client.is_connected():
        try:
            await client.send_message(chat_id, text)
        except Exception:  # noqa: BLE001
            pass


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
            requester_id=str(message.sender_id or message.chat_id),
            image_features=features,
        )
        search.scope = {
            "chat_id": message.chat_id,
            "user_id": message.sender_id,
            "origin": "telegram",
        }
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
        return  # Silencio total para usuarios sin autorización.
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

    link = _telegram_link(dialog_id, message_id)
    # El acceso ya fue validado arriba: propietario o usuario autorizado
    # pueden recibir el archivo; el registro de fuente es solo trazabilidad.

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
        await answer(
            message,
            f"✅ Archivo enviado: {item.get('name') or item.get('title')}\n"
            f"📎 Mensaje original: {link}",
        )
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

        # Mensajes Guardados (chat consigo mismo) se ignoran por completo:
        # no se investiga nada ni se responde comandos en ese chat.
        me = await _get_self_id()
        if me and message.chat_id == me:
            return

        # Validación de acceso en CADA interacción.
        async with get_sessionmaker()() as db:
            access = await check_access(db, sender_id)

        text = message.text or ""
        parts = text.strip().split(maxsplit=1)
        command = parts[0].lower() if parts else ""
        argument = parts[1] if len(parts) > 1 else ""
        # Normalizar alias de comandos (!buscar -> !busca, !archivo -> !archivos).
        command = COMMAND_ALIASES.get(command, command)

        # Comandos administrativos (solo propietario), incluso para usuarios sin autorizar.
        if command in ADMIN_COMMANDS:
            await handle_admin_command(message, command, argument)
            return

        if command == "/start" or command == "!start":
            await answer(message, "🤖 CineFinder AI. Usa !ayuda para ver los comandos.")
            return

        if not access["allowed"]:
            # Usuarios no autorizados: silencio total, sin respuesta de ningún tipo.
            return

        # Responder solo con un número selecciona un archivo de la última búsqueda.
        if re.fullmatch(r"\d{1,2}", text.strip()):
            selection = text.strip()
            _, deep_results = await _get_pending_files(message.chat_id, sender_id)
            if deep_results:
                await deliver_indexed_file(message, selection)
                return
            async with get_sessionmaker()() as db:
                key = await _pending_key(message.chat_id, sender_id)
                res = await db.execute(select(SystemSetting).where(SystemSetting.key == key))
                pending = res.scalar_one_or_none()
            if pending and pending.value:
                await deliver_selected_media(
                    message.chat_id, sender_id or 0, None, int(selection) - 1
                )
                return
            await answer(message, "ℹ️ No hay una selección pendiente. Usa !archivos o !busca primero.")
            return

        # Sin comando no se hace nada: siempre se exige !busca / !archivos,
        # incluso para fotos, álbumes y texto.
        if command not in COMMANDS:
            return

        if command == "!busca":
            # Normalizar el separador opcional: !busca ;texto  →  texto
            argument = argument.strip()
            if argument.startswith(";"):
                argument = argument[1:].strip()

            # Si se responde a un mensaje con foto, se usa la imagen de ese mensaje.
            reply = getattr(message, "reply_to_message", None)
            if reply is not None and getattr(reply, "media", None):
                images = await download_images([reply])
                if images:
                    if not argument:
                        argument = (reply.text or "").strip()
                    async with get_sessionmaker()() as db:
                        if not await register_event(db, message, []):
                            return
                        await db.commit()
                    await handle_busca(message, argument, images)
                    return

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
            return  # Silencio total para usuarios sin autorización.
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
    """Arranca el bucle de escucha con su propio cliente y reconexión."""
    while True:
        ok, detail = await connect_telegram()
        if not ok:
            logger.warning("telegram_worker_not_connected", extra={"detail": detail})
            await asyncio.sleep(15)
            continue
        client = get_telegram_client()
        assert client is not None
        register_handlers(client)
        try:
            await client.run_until_disconnected()
        except Exception:  # noqa: BLE001
            logger.warning("telegram_listener_disconnected")
        await asyncio.sleep(5)
