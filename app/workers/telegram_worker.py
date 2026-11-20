"""Worker de Telegram: comandos autorizados, álbumes y deduplicación."""

from __future__ import annotations

import asyncio
from collections import defaultdict
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from telethon import events
from telethon.tl.types import Message

from app.catalog.service import get_movie_by_code, search_catalog
from app.config import settings
from app.database.models import AppUser, TelegramMessage
from app.database.session import get_sessionmaker
from app.services.jobs import create_job
from app.services.research import run_search_job
from app.telegram.client import connect_telegram, get_telegram_client
from app.utils.logging import get_logger
from app.utils.security import hash_image

logger = get_logger(__name__)

COMMANDS = ("!busca", "!info", "!stats", "!ultimas", "!ayuda", "!estado", "!autorizar", "!desautorizar")
ALBUM_DEBOUNCE_SECONDS = 1.5

HELP = """🤖 CineFinder AI — Comandos
!busca [texto] — busca por texto e imágenes (responde con fotos)
!info CF-000001 — ficha de una película
!stats — estadísticas del catálogo
!ultimas — últimas incorporationes
!estado — estado del sistema
!autorizar <ID> [usuario] — autoriza a otro usuario (solo propietario)
!desautorizar <ID> — revoca la autorización (solo propietario)
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
    await answer(message, "🔍 Iniciando investigación…")
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
        )
    movie = result.get("movie")
    if movie:
        text = (f"✅ {movie['code']} — {movie['title']} ({movie.get('year') or '?'})\n"
                f"Confianza: {result['confidence']:.2f}")
    else:
        text = "⚠️ No pude identificar la película con confianza. Revisión manual."
    await answer(message, text)


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


async def run_telegram_worker() -> None:
    ok, detail = await connect_telegram()
    if not ok:
        logger.warning("telegram_worker_not_connected", extra={"detail": detail})
        return
    client = get_telegram_client()
    assert client is not None
    register_handlers(client)
    await client.run_until_disconnected()
