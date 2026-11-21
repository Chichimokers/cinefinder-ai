"""Worker de búsquedas profundas en Telegram, con reanudación y notificación."""

from __future__ import annotations

import asyncio

from sqlalchemy import select

from app.database.models import SystemSetting

from app.database.models import FileSearch
from app.database.session import get_sessionmaker
from app.services.events import publish, subscribe, unsubscribe
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


async def _status_message_id(db, chat_id: int) -> int | None:
    from sqlalchemy import select

    from app.database.models import SystemSetting

    res = await db.execute(
        select(SystemSetting).where(SystemSetting.key == f"deep_status:{chat_id}")
    )
    setting = res.scalar_one_or_none()
    return int(setting.value.get("message_id")) if setting and setting.value else None


async def _set_status_message_id(db, chat_id: int, message_id: int) -> None:
    from sqlalchemy import select

    from app.database.models import SystemSetting

    key = f"deep_status:{chat_id}"
    res = await db.execute(select(SystemSetting).where(SystemSetting.key == key))
    setting = res.scalar_one_or_none()
    if setting is None:
        db.add(SystemSetting(key=key, value={"message_id": message_id}))
    else:
        setting.value = {"message_id": message_id}
    await db.flush()


def _progress_text(data: dict) -> str:
    stage = str(data.get("stage") or "").replace("_", " ")
    return (
        f"🔎 *Búsqueda profunda*\n"
        f"📊 {stage}\n"
        f"Diálogos: {data.get('dialogs_done', 0)}/{data.get('dialogs_total', 0)}\n"
        f"Mensajes revisados: {data.get('messages_scanned', 0)}\n"
        f"Archivos encontrados: {data.get('files_found', 0)}"
    )


async def run_deep_search_job(search_id: str) -> dict | None:
    sessionmaker = get_sessionmaker()
    async with sessionmaker() as db:
        result = await db.execute(select(FileSearch).where(FileSearch.id == search_id))
        search = result.scalar_one_or_none()
        if search is None or search.status in {"completed", "cancelled"}:
            return None
        search.status = "running"
        await db.commit()
        scope = search.scope or {}
        chat_id = scope.get("chat_id")
        status_id = None

        if chat_id:
            from app.telegram.client import get_telegram_client

            client = get_telegram_client()
            if client and client.is_connected():
                status_id = await _status_message_id(db, int(chat_id))
                try:
                    if status_id:
                        await client.edit_message(int(chat_id), status_id, _progress_text({"stage": "iniciando"}))
                    else:
                        msg = await client.send_message(
                            int(chat_id), _progress_text({"stage": "iniciando"})
                        )
                        status_id = int(msg.id)
                        await _set_status_message_id(db, int(chat_id), status_id)
                        await db.commit()
                except Exception:  # noqa: BLE001
                    status_id = None

        async def _live_progress(event: dict) -> None:
            nonlocal status_id
            if not chat_id or not status_id:
                return
            from app.telegram.client import get_telegram_client

            client = get_telegram_client()
            if client is None or not client.is_connected():
                return
            if event.get("type") == "deep_progress":
                try:
                    await client.edit_message(
                        int(chat_id), status_id, _progress_text(event.get("data") or {})
                    )
                except Exception:  # noqa: BLE001
                    pass

        queue = subscribe(search_id)

        async def _relay_events() -> None:
            while True:
                event = await queue.get()
                await _live_progress(event)
                if event.get("type") in ("deep_results", "final"):
                    break

        relay = asyncio.create_task(_relay_events())
        try:
            output = await run_file_search(db, search)
            output["query"] = search.query
            await publish(search_id, {
                "type": "deep_results", "message": "Búsqueda finalizada", "data": output
            })
            if chat_id:
                async with sessionmaker() as notify_db:
                    requester = search.requester_id or chat_id
                    key = f"pending_files:{requester}"
                    res = await notify_db.execute(select(SystemSetting).where(SystemSetting.key == key))
                    setting = res.scalar_one_or_none()
                    value = {"search_id": search_id, "results": output.get("results", [])[:10]}
                    if setting is None:
                        notify_db.add(SystemSetting(key=key, value=value))
                    else:
                        setting.value = value
                    await notify_db.commit()

                from app.telegram.client import get_telegram_client

                client = get_telegram_client()
                if client and client.is_connected() and status_id:
                    try:
                        await client.edit_message(int(chat_id), status_id, format_results_message(output))
                    except Exception:  # noqa: BLE001
                        await client.send_message(int(chat_id), format_results_message(output))
                elif client and client.is_connected():
                    await client.send_message(int(chat_id), format_results_message(output))
            return output
        except Exception as exc:  # noqa: BLE001
            search.status = "failed"
            search.error = f"{type(exc).__name__}: {str(exc)[:200]}"
            await db.commit()
            logger.warning("deep_search_failed", extra={"error": type(exc).__name__})
            return None
        finally:
            unsubscribe(search_id, queue)
            relay.cancel()


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
    async def _safe_run(search_id: str) -> None:
        try:
            await run_deep_search_job(search_id)
        except Exception as exc:  # noqa: BLE001
            logger.error("deep_search_task_crashed", extra={"error": type(exc).__name__})

    for search_id in ids:
        asyncio.create_task(_safe_run(search_id))
    return len(ids)
