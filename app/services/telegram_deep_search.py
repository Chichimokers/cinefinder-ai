"""Motor de búsqueda profunda y reanudable en Telegram (Telethon).

- Inventario de chats, grupos, supergrupos, canales y conversaciones archivadas.
- Recorrido paginado del historial accesible con punto de reanudación.
- Indexación de metadatos de archivos sin descargarlos.
- Búsqueda por variantes textuales y similitud visual de imágenes.
- Restricciones: FloodWait, concurrencia, límites por ejecución y autorización
  para publicar/indexar contenido en el canal de catálogo.
"""

from __future__ import annotations

import asyncio
import hashlib
import os
import re
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
from telethon.errors import FloodWaitError, RPCError
from telethon.tl.types import DocumentAttributeFilename, DocumentAttributeVideo

from app.database.models import (
    FileSearch,
    FileSearchResult,
    IndexedFile,
    IndexingJob,
    TelegramDialog,
    TelegramMediaReference,
    TelegramMessageIndex,
)
from app.search.image_similarity import dhash, similarity
from app.search.text_variants import generate_variants, normalize, score_match
from app.services.events import publish
from app.services.telegram_deep_scan_helper import ensure_telegram_ready
from app.telegram.client import get_telegram_client
from app.utils.logging import get_logger

logger = get_logger(__name__)

BATCH_SIZE = 100
MAX_MESSAGES_PER_DIALOG = int(os.environ.get("DEEP_SEARCH_MAX_MESSAGES", "500"))
MAX_DIALOGS_PER_RUN = int(os.environ.get("DEEP_SEARCH_MAX_DIALOGS", "200"))
MAX_RESULTS = 40
PUBLISH_BATCH_LIMIT = 20

VIDEO_EXT = {".mkv", ".mp4", ".avi", ".mov", ".webm", ".m4v", ".ts", ".flv"}
SUBTITLE_EXT = {".srt", ".ass", ".ssa", ".sub", ".vtt"}
ARCHIVE_EXT = {".zip", ".rar", ".7z", ".tar", ".gz"}
IMAGE_EXT = {".jpg", ".jpeg", ".png", ".webp", ".gif", ".bmp"}


async def _progress(db: AsyncSession, search: FileSearch, stage: str, **fields: Any) -> None:
    search.stage = stage
    for key, value in fields.items():
        if hasattr(search, key):
            setattr(search, key, value)
    await db.flush()
    await publish(search.id, {
        "type": "deep_progress",
        "message": stage,
        "data": {
            "stage": stage,
            "dialogs_done": search.dialogs_done,
            "dialogs_total": search.dialogs_total,
            "messages_scanned": search.messages_scanned,
            "files_found": search.files_found,
        },
    })


def _file_metadata(message: Any) -> dict[str, Any]:
    """Extrae metadatos reales del archivo sin descargarlo."""
    data: dict[str, Any] = {
        "has_media": False, "media_type": None, "file_name": None,
        "file_size": None, "mime_type": None, "duration": None,
        "width": None, "height": None,
    }
    media = message.media
    if media is None:
        return data
    data["has_media"] = True
    data["media_type"] = type(media).__name__
    try:
        file = message.file
        if file is not None:
            data["file_name"] = getattr(file, "name", None)
            data["file_size"] = int(getattr(file, "size", 0) or 0)
            data["mime_type"] = getattr(file, "mime_type", None)
    except Exception:  # noqa: BLE001
        pass
    document = getattr(message, "document", None)
    if document is not None:
        for attr in getattr(document, "attributes", []) or []:
            if isinstance(attr, DocumentAttributeFilename) and not data["file_name"]:
                data["file_name"] = attr.file_name
            if isinstance(attr, DocumentAttributeVideo):
                data["duration"] = getattr(attr, "duration", None)
                data["width"] = getattr(attr, "w", None)
                data["height"] = getattr(attr, "h", None)
    if data["mime_type"] is None and getattr(message, "photo", None) is not None:
        data["mime_type"] = "image/jpeg"
    return data


def _extension(name: str | None, mime: str | None) -> str:
    if name and "." in name:
        return "." + name.rsplit(".", 1)[-1].lower()
    if mime and "/" in mime:
        return "." + mime.split("/")[-1].lower()
    return ""


def classify_file(name: str | None, mime: str | None) -> tuple[str, str]:
    ext = _extension(name, mime)
    if ext in VIDEO_EXT or (mime or "").startswith("video/"):
        return "video", ext
    if ext in SUBTITLE_EXT:
        return "subtitle", ext
    if ext in ARCHIVE_EXT:
        return "archive", ext
    if ext in IMAGE_EXT or (mime or "").startswith("image/"):
        return "image", ext
    return "other", ext


def derive_title(file_name: str | None) -> str | None:
    if not file_name:
        return None
    name = re.sub(r"\.[a-zA-Z0-9]{2,4}$", "", file_name)
    name = re.sub(r"[._]+", " ", name)
    name = re.sub(r"\b(1080p|720p|2160p|4k|bluray|bdrip|webrip|x264|x265|h264|h265|aac|ac3)\b", " ", name, flags=re.I)
    return re.sub(r"\s+", " ", name).strip()[:200] or None


async def upsert_dialogs(db: AsyncSession) -> list[TelegramDialog]:
    """Descubre todos los diálogos accesibles, incluidos los archivados."""
    client = get_telegram_client()
    if client is None or not client.is_connected():
        raise RuntimeError("Userbot no conectado")
    seen: set[int] = set()
    rows: list[TelegramDialog] = []
    for archived in (False, True):
        try:
            async for dialog in client.iter_dialogs(limit=MAX_DIALOGS_PER_RUN, archived=archived):
                try:
                    dialog_id = int(dialog.id)
                except Exception:  # noqa: BLE001
                    continue
                if dialog_id in seen:
                    continue
                seen.add(dialog_id)
                entity = dialog.entity
                kind = type(entity).__name__ or "unknown"
                result = await db.execute(
                    select(TelegramDialog).where(TelegramDialog.dialog_id == dialog_id)
                )
                row = result.scalar_one_or_none()
                if row is None:
                    row = TelegramDialog(dialog_id=dialog_id, scan_status="pending")
                    db.add(row)
                row.title = getattr(dialog, "name", None) or getattr(entity, "title", None)
                row.username = getattr(entity, "username", None)
                row.dialog_type = kind
                row.is_archived = bool(archived)
                rows.append(row)
        except RPCError as exc:
            logger.warning("dialog_inventory_error", extra={"archived": archived, "error": type(exc).__name__})
    await db.flush()
    return rows[:MAX_DIALOGS_PER_RUN]


async def scan_dialog(
    db: AsyncSession,
    search: FileSearch,
    dialog: TelegramDialog,
    variants: list[str],
) -> int:
    """Recorre el historial en lotes desde el último mensaje conocido."""
    if search.cancel_requested:
        return 0
    client = get_telegram_client()
    if client is None or not client.is_connected():
        raise RuntimeError("Userbot no conectado")

    scanned = 0
    last_id = dialog.last_scanned_message_id or 0
    oldest_seen: int | None = None
    try:
        entity = await client.get_entity(dialog.dialog_id)
        if entity is None:
            raise RuntimeError("entidad no accesible")
        batch: list[Any] = []
        async for message in client.iter_messages(
            entity, limit=BATCH_SIZE, offset_id=last_id, wait_time=0.5
        ):
            batch.append(message)
            if len(batch) >= BATCH_SIZE:
                scanned += await _process_batch(db, search, dialog, batch, variants)
                oldest_seen = int(batch[-1].id)
                batch = []
                await _progress(db, search, "inspeccionando_historiales", messages_scanned=search.messages_scanned)
                if scanned >= MAX_MESSAGES_PER_DIALOG or search.cancel_requested:
                    break
        if batch and not search.cancel_requested and scanned < MAX_MESSAGES_PER_DIALOG:
            scanned += await _process_batch(db, search, dialog, batch, variants)
            oldest_seen = int(batch[-1].id)
        if oldest_seen is not None:
            dialog.last_scanned_message_id = oldest_seen
        dialog.messages_scanned += scanned
        dialog.last_scan_at = datetime.now(timezone.utc)
        dialog.scan_status = "partial" if scanned >= MAX_MESSAGES_PER_DIALOG else "complete"
        dialog.access_error = None
    except FloodWaitError as exc:
        wait = min(int(exc.seconds), 60)
        dialog.scan_status = "error"
        dialog.access_error = f"FloodWait {exc.seconds}s"
        await asyncio.sleep(wait)
    except RPCError as exc:
        dialog.scan_status = "error"
        dialog.access_error = type(exc).__name__
    await db.flush()
    return scanned


async def _process_batch(
    db: AsyncSession,
    search: FileSearch,
    dialog: TelegramDialog,
    messages: list[Any],
    variants: list[str],
) -> int:
    """Indexa metadatos de mensajes relevantes (con multimedia o coincidencia)."""
    processed = 0
    for message in messages:
        if search.cancel_requested:
            break
        processed += 1
        metadata = _file_metadata(message)
        text = message.text or ""
        caption = getattr(message, "message", "") or ""
        searchable = f"{text} {caption} {metadata['file_name'] or ''}"
        if not metadata["has_media"] and not any(
            normalize(v) and normalize(v) in normalize(searchable) for v in variants if len(v) > 3
        ):
            continue
        exists = await db.execute(
            select(TelegramMessageIndex.id).where(
                TelegramMessageIndex.dialog_id == dialog.dialog_id,
                TelegramMessageIndex.message_id == message.id,
            )
        )
        if exists.scalar_one_or_none() is not None:
            continue
        db.add(TelegramMessageIndex(
            dialog_id=dialog.dialog_id,
            message_id=int(message.id),
            date=message.date,
            text=text[:4000] or None,
            caption=caption[:2000] or None,
            sender_id=getattr(message, "sender_id", None),
            has_media=metadata["has_media"],
            media_type=metadata["media_type"],
            file_name=metadata["file_name"],
            file_size=metadata["file_size"],
            mime_type=metadata["mime_type"],
            duration=metadata["duration"],
            width=metadata["width"],
            height=metadata["height"],
            reply_to=getattr(message, "reply_to_msg_id", None),
        ))
        if metadata["has_media"] and (metadata["file_name"] or metadata["mime_type"]):
            await _register_indexed_file(db, dialog, message, metadata, variants)
    search.messages_scanned += processed
    await db.flush()
    return processed


async def _register_indexed_file(
    db: AsyncSession,
    dialog: TelegramDialog,
    message: Any,
    metadata: dict[str, Any],
    variants: list[str],
) -> IndexedFile | None:
    file_name = metadata["file_name"] or (
        f"foto_{message.id}.jpg" if metadata.get("mime_type", "").startswith("image/")
        else f"archivo_{message.id}"
    )
    size = metadata["file_size"] or 0
    category, ext = classify_file(file_name, metadata["mime_type"])
    searchable = f"{file_name} {getattr(message, 'message', '') or ''}"
    score, variant, matched = score_match(variants, searchable, file_name)
    # Se indexan todos los archivos multimedia para que futuras búsquedas puedan
    # encontrarlos sin reescanear; la puntuación se aplica al devolver resultados.
    key_source = f"{normalize(file_name)}|{size}|{ext}"
    file_key = hashlib.sha256(key_source.encode()).hexdigest()
    exists = await db.execute(select(IndexedFile).where(IndexedFile.file_key == file_key))
    row = exists.scalar_one_or_none()
    if row is not None:
        return row
    telegram_ref = f"tg://openmessage?chat_id={dialog.dialog_id}&message_id={message.id}"
    row = IndexedFile(
        file_key=file_key,
        dialog_id=dialog.dialog_id,
        message_id=int(message.id),
        original_name=file_name,
        title=derive_title(file_name),
        category=category,
        extension=ext,
        size_bytes=size,
        mime_type=metadata["mime_type"],
        telegram_ref=telegram_ref,
        indexing_status="indexed",
        confidence=round(score / 100.0, 3),
    )
    db.add(row)
    await db.flush()
    db.add(TelegramMediaReference(
        dialog_id=dialog.dialog_id,
        message_id=int(message.id),
        file_name=file_name,
        size_bytes=size,
        mime_type=metadata["mime_type"],
        telegram_ref=telegram_ref,
        indexed_file_id=row.id,
    ))
    return row


async def collect_results(db: AsyncSession, search: FileSearch) -> list[dict]:
    """Puntúa archivos indexados por variantes y, si hay imagen, por similitud visual."""
    conditions = []
    for variant in search.query_variants:
        key = normalize(variant)
        if len(key) >= 2:
            conditions.append(IndexedFile.original_name.ilike(f"%{key}%"))
            conditions.append(IndexedFile.title.ilike(f"%{key}%"))
    stmt = select(IndexedFile)
    if conditions:
        from sqlalchemy import or_
        stmt = stmt.where(or_(*conditions))
    rows = (await db.execute(stmt.limit(300))).scalars().all()

    original_hash = (search.image_features or {}).get("dhash") if search.image_features else None
    scored: list[dict] = []
    for row in rows:
        score, variant, matched = score_match(
            search.query_variants, row.original_name or "", row.original_name
        )
        visual_score = 0.0
        if original_hash and row.category == "image":
            try:
                client = get_telegram_client()
                if client and client.is_connected():
                    msgs = await client.get_messages(row.dialog_id, ids=[row.message_id])
                    if msgs and msgs[0].media:
                        thumb = await msgs[0].download_media(file=bytes, thumb=0)  # type: ignore[arg-type]
                        if isinstance(thumb, bytes):
                            visual_score = similarity(int(original_hash), dhash(thumb))
            except Exception:  # noqa: BLE001
                visual_score = 0.0
        final_score = round(max(score, visual_score), 1) if visual_score else score
        if final_score < 40:
            continue
        scored.append({
            "indexed_file_id": row.id,
            "score": final_score,
            "matched_variant": variant,
            "matched_text": matched,
            "file": {
                "id": row.id,
                "name": row.original_name,
                "title": row.title,
                "category": row.category,
                "extension": row.extension,
                "size_bytes": row.size_bytes,
                "mime_type": row.mime_type,
                "dialog_id": row.dialog_id,
                "message_id": row.message_id,
                "telegram_ref": row.telegram_ref,
                "confidence": row.confidence,
            },
        })
    scored.sort(key=lambda item: item["score"], reverse=True)
    scored = scored[:MAX_RESULTS]
    for item in scored:
        db.add(FileSearchResult(
            search_id=search.id,
            indexed_file_id=item["indexed_file_id"],
            score=item["score"],
            matched_variant=item.get("matched_variant"),
            matched_text=item.get("matched_text"),
        ))
    search.files_found = len(scored)
    await db.flush()
    return scored


async def run_file_search(db: AsyncSession, search: FileSearch) -> dict:
    """Orquesta una búsqueda profunda completa y devuelve los resultados."""
    search.status = "running"
    await _progress(db, search, "conectando_telegram")
    if not await ensure_telegram_ready():
        raise RuntimeError("Userbot no conectado: revisa TELEGRAM_SESSION en Render")
    await _progress(db, search, "descubriendo_conversaciones")
    dialogs = await upsert_dialogs(db)
    search.dialogs_total = len(dialogs)
    await _progress(db, search, "inspeccionando_historiales")

    for dialog in dialogs:
        if search.cancel_requested:
            break
        await scan_dialog(db, search, dialog, search.query_variants)
        search.dialogs_done += 1
        await _progress(db, search, "inspeccionando_historiales")

    await _progress(db, search, "analizando_archivos")
    # Backfill: mensajes ya indexados en ejecuciones anteriores que aún no tienen
    # su entrada en indexed_files (evita perder archivos tras una reanudación).
    pending = await db.execute(
        select(TelegramMessageIndex).where(
            TelegramMessageIndex.has_media.is_(True),
        ).limit(5000)
    )
    dialogs_by_id = {d.dialog_id: d for d in dialogs}
    for msg in pending.scalars().all():
        dialog = dialogs_by_id.get(msg.dialog_id)
        if dialog is None:
            dialog = TelegramDialog(dialog_id=msg.dialog_id, scan_status="complete")
        await _register_indexed_file(
            db, dialog, msg, {
                "file_name": msg.file_name,
                "file_size": msg.file_size,
                "mime_type": msg.mime_type,
                "has_media": True,
            }, search.query_variants,
        )
    await db.flush()

    results = await collect_results(db, search)

    await _progress(db, search, "indexando_coincidencias")
    job = IndexingJob(search_id=search.id, status="completed", files_indexed=len(results))
    db.add(job)

    search.status = "cancelled" if search.cancel_requested else "completed"
    search.completed_at = datetime.now(timezone.utc)
    await _progress(db, search, "listo", files_found=len(results))
    await db.commit()
    return {
        "search_id": search.id,
        "status": search.status,
        "results": [item["file"] | {"score": item["score"]} for item in results],
        "dialogs_total": search.dialogs_total,
        "dialogs_done": search.dialogs_done,
        "messages_scanned": search.messages_scanned,
        "files_found": len(results),
    }


async def create_file_search(
    db: AsyncSession,
    query: str,
    requester_id: str | None,
    image_hash: str | None = None,
    image_features: dict | None = None,
    aliases: list[str] | None = None,
    year: int | None = None,
) -> FileSearch:
    variants = generate_variants(query, year=year, aliases=aliases)
    search = FileSearch(
        status="queued",
        requester_id=requester_id,
        query=query,
        query_variants=variants,
        image_hash=image_hash,
        image_features=image_features,
    )
    db.add(search)
    await db.flush()
    return search


async def recover_searches(db: AsyncSession) -> int:
    """Reencola solo búsquedas antiguas interrumpidas por reinicios."""
    from datetime import timedelta

    cutoff = datetime.now(timezone.utc) - timedelta(minutes=30)
    result = await db.execute(
        select(FileSearch).where(
            FileSearch.status.in_(["queued", "running"]),
            FileSearch.created_at < cutoff,
        )
    )
    searches = result.scalars().all()
    for search in searches:
        search.status = "queued"
        search.stage = "recuperado"
    await db.flush()
    return len(searches)
