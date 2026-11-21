"""Worker de búsquedas profundas en Telegram, con reanudación y notificación."""

from __future__ import annotations

import asyncio

from sqlalchemy import select

from app.database.models import FileSearch
from app.database.session import get_sessionmaker
from app.services.events import publish
from app.services.telegram_deep_search import recover_searches, run_file_search
from app.utils.logging import get_logger

logger = get_logger(__name__)


def format_results_message(result: dict) -> str:
    lines = ["🔎 *RESULTADOS DE BÚSQUEDA PROFUNDA*"]
    title = result.get("query") or "imagen enviada"
    lines.append(f"Consulta: {title}")
    lines.append(f"Archivos relacionados: {result.get('files_found', 0)}")
    lines.append(
        f"Conversaciones: {result.get('dialogs_done', 0)}/{result.get('dialogs_total', 0)} "
        f"· Mensajes revisados: {result.get('messages_scanned', 0)}"
    )
    for index, item in enumerate(result.get("results", [])[:8], start=1):
        size = item.get("size_bytes") or 0
        lines.append(
            f"{index}. {item.get('name') or item.get('title') or 'archivo'} "
            f"({size / 1024 / 1024:.1f} MB) — coincidencia {item.get('score', 0):.0f}%"
        )
    lines.append("\nResponde: !infoarchivo <n> para ver detalles, !enviararchivo <n> para solicitarlo, o !cancelar.")
    return "\n".join(lines)


async def run_deep_search_job(search_id: str) -> dict | None:
    sessionmaker = get_sessionmaker()
    async with sessionmaker() as db:
        result = await db.execute(select(FileSearch).where(FileSearch.id == search_id))
        search = result.scalar_one_or_none()
        if search is None or search.status in {"completed", "cancelled"}:
            return None
        search.status = "running"
        await db.commit()
        try:
            output = await run_file_search(db, search)
            output["query"] = search.query
            scope = search.scope or {}
            chat_id = scope.get("chat_id")
            await publish(search_id, {
                "type": "deep_results", "message": "Búsqueda finalizada", "data": output
            })
            if chat_id:
                from sqlalchemy import select

                from app.database.models import SystemSetting
                from app.telegram.client import get_telegram_client

                async with sessionmaker() as notify_db:
                    key = f"pending_files:{chat_id}"
                    res = await notify_db.execute(select(SystemSetting).where(SystemSetting.key == key))
                    setting = res.scalar_one_or_none()
                    value = {"search_id": search_id, "results": output.get("results", [])[:10]}
                    if setting is None:
                        notify_db.add(SystemSetting(key=key, value=value))
                    else:
                        setting.value = value
                    await notify_db.commit()

                client = get_telegram_client()
                if client and client.is_connected():
                    await client.send_message(int(chat_id), format_results_message(output))
            return output
        except Exception as exc:  # noqa: BLE001
            search.status = "failed"
            search.error = type(exc).__name__
            await db.commit()
            logger.warning("deep_search_failed", extra={"error": type(exc).__name__})
            return None


async def process_pending_deep_searches(limit: int = 2) -> int:
    sessionmaker = get_sessionmaker()
    async with sessionmaker() as db:
        await recover_searches(db)
        result = await db.execute(
            select(FileSearch).where(FileSearch.status == "queued").order_by(FileSearch.created_at).limit(limit)
        )
        searches = result.scalars().all()
        ids = [s.id for s in searches]
        await db.commit()
    for search_id in ids:
        asyncio.create_task(run_deep_search_job(search_id))
    return len(ids)
