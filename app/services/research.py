"""Flujo completo de investigación de una película."""

from __future__ import annotations

import json
import re
from datetime import date
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.ai.agent import AgentResult, run_agent
from app.ai.tools.base import ToolContext
from app.catalog.service import (
    check_duplicate,
    create_movie,
    get_movie_by_code,
    record_search_history,
)
from app.config import settings
from app.services.jobs import set_status
from app.telegram.client import ensure_catalog_channel, publish_to_catalog
from app.utils.logging import get_logger
from app.utils.security import hash_image

logger = get_logger(__name__)

MIN_CONFIDENCE_TO_SAVE = 0.55


def parse_agent_answer(text: str) -> dict:
    cleaned = re.sub(r"^```(?:json)?\s*|\s*```$", "", (text or "").strip())
    try:
        return json.loads(cleaned)
    except json.JSONDecodeError:
        match = re.search(r"\{.*\}", cleaned, re.DOTALL)
        if match:
            try:
                return json.loads(match.group(0))
            except json.JSONDecodeError:
                pass
    return {"need_review": True, "raw": (text or "")[:2000]}


def build_catalog_caption(movie: Any, sources: list[dict]) -> str:
    lines = [
        f"🎬 {movie.code}",
        f"Título original: {movie.original_title}",
    ]
    if movie.title != movie.original_title:
        lines.append(f"Título: {movie.title}")
    if movie.year:
        lines.append(f"Año: {movie.year}")
    if movie.genres:
        lines.append(f"Géneros: {', '.join(movie.genres)}")
    if movie.synopsis:
        synopsis = movie.synopsis[:400] + ("…" if len(movie.synopsis) > 400 else "")
        lines.append(f"Sinopsis: {synopsis}")
    if sources:
        refs = ", ".join(f"{s['provider']}:{s['external_id']}" for s in sources[:6])
        lines.append(f"Fuentes: {refs}")
    lines.extend([
        f"Alta: {date.today().isoformat()}",
        f"Confianza: {movie.confidence:.2f}",
        f"Verificación: {movie.verification_status}",
    ])
    return "\n".join(lines)


def _visual_summary(agent: AgentResult) -> list[str]:
    import json

    for name in ("analyze_image", "analyze_multiple_images"):
        for payload in agent.tool_results.get(name, []):
            raw = payload.get("analysis")
            if not raw:
                continue
            try:
                data = json.loads(raw)
            except Exception:  # noqa: BLE001
                data = {}
            clues = [str(c) for c in (data.get("visual_clues") or [])][:3]
            text_found = str(data.get("text_found") or "").strip()
            result = [f"Texto visible: {text_found[:90]}"] if text_found else []
            result += [f"Pista: {c[:90]}" for c in clues]
            if result:
                return result
    for payload in agent.tool_results.get("extract_text_from_image", []):
        text = str(payload.get("text") or "").strip()
        if text:
            return [f"Texto visible: {text[:120]}"]
    return []


def _telegram_scan_summary(agent: AgentResult) -> str:
    for payload in agent.tool_results.get("search_telegram_all_channels", []):
        scanned = payload.get("dialogs_scanned", 0)
        total = payload.get("total", 0)
        return f"Telegram completo: {scanned} chats/canales revisados → {total} coincidencias"
    for payload in agent.tool_results.get("search_telegram_authorized", []):
        total = len(payload.get("results", []))
        return f"Telegram (fuentes autorizadas) → {total} coincidencias"
    return "Telegram: búsqueda no disponible en esta sesión"


def format_professional_answer(
    parsed: dict, movie: dict | None, confidence: float, publication: dict | None, agent: AgentResult
) -> str:
    """Informe claro y escaneable: qué vi, dónde busqué, qué encontré y qué hacer."""
    identified = movie is not None
    if identified:
        title = f"{movie.get('code', '')} — {movie.get('title', '')}"
        if movie.get("year"):
            title += f" ({movie['year']})"
        lines = [f"🎬 RESULTADO: {title}", f"Confianza: {confidence:.0%}"]
    else:
        lines = ["🔎 RESULTADO: no pude identificar la película con seguridad",
                 f"Confianza: {confidence:.0%} · estado: {parsed.get('verification_status') or 'sin verificar'}"]

    visual = _visual_summary(agent)
    if visual:
        lines.append("\n📌 LO QUE VI EN LA IMAGEN")
        lines.extend(f"• {item}" for item in visual[:4])

    lines.append("\n🔎 DÓNDE BUSQUÉ")
    lines.append(f"• {_telegram_scan_summary(agent)}")
    lines.append("• Catálogo interno + bases de datos (TMDB, OMDb, Wikidata, web, YouTube, Internet Archive)")
    lines.append("• Imágenes y texto de la propia foto")

    candidates = parsed.get("alternatives") or agent.candidates[:4]
    if candidates:
        lines.append("\n🎯 CANDIDATOS / HALLAZGOS")
        for item in candidates[:4]:
            label = str(item.get("title") or item.get("chat") or "")
            year = item.get("year") or ""
            lines.append(f"• {label}" + (f" ({year})" if year else ""))
    else:
        lines.append("\n🎯 HALLAZGOS")
        lines.append("• Ninguna coincidencia verificable en las fuentes consultadas.")

    reasoning = str(parsed.get("reasoning") or parsed.get("raw") or "").strip()
    if reasoning:
        lines.append("\n🧠 POR QUÉ")
        lines.append(" ".join(reasoning.split())[:380])

    if not identified:
        lines.append("\n👉 SIGUIENTE PASO")
        lines.append("Envía otro ángulo/cartel más nítido o añade texto (año, actor, director). "
                     "También puedo repetir la búsqueda en Telegram con otra palabra clave de la imagen.")

    sources = parsed.get("sources") or []
    if sources:
        lines.append("\n🔗 FUENTES")
        for source in sources[:5]:
            provider = source.get("provider", "fuente")
            url = source.get("url")
            lines.append(f"• {provider}: {url}" if url else f"• {provider}")

    if publication:
        lines.append(f"\n📚 Publicado en el índice privado (mensaje {publication.get('message_id')}).")

    lines.append(f"\n🛠️ {len(agent.tools_called)} herramientas · {agent.steps} pasos · "
                 f"{agent.usage.get('total_tokens', 0)} tokens")
    return "\n".join(lines)


def extract_telegram_media(agent: AgentResult) -> list[dict]:
    """Extrae mensajes con archivos de Telegram para ofrecerlos al propietario."""
    found: list[dict] = []
    seen: set[tuple[int, int]] = set()
    for name in ("search_telegram_all_channels", "search_telegram_authorized"):
        for payload in agent.tool_results.get(name, []):
            for item in payload.get("results", []):
                chat_id = item.get("chat_id")
                message_id = item.get("message_id")
                if not item.get("has_media") or not chat_id or not message_id:
                    continue
                key = (int(chat_id), int(message_id))
                if key in seen:
                    continue
                seen.add(key)
                found.append({
                    "chat": item.get("chat"),
                    "chat_id": int(chat_id),
                    "message_id": int(message_id),
                    "date": item.get("date"),
                    "text": (item.get("text") or "")[:300],
                })
                if len(found) >= 8:
                    return found
    return found


async def run_search_job(
    db: AsyncSession,
    job_id: str,
    query: str,
    images_bytes: list[bytes],
    image_urls: list[str] | None = None,
    requester_id: str | None = None,
    is_owner: bool = False,
    progress=None,
) -> dict:
    """Ejecuta un job completo: agente → verificación → catálogo → publicación."""
    await set_status(db, job_id, "running")
    ctx = ToolContext(
        db=db,
        job_id=job_id,
        requester_id=requester_id,
        is_owner=is_owner,
        images=images_bytes,
        image_urls=image_urls or [],
        query=query,
    )

    agent: AgentResult = await run_agent(
        db, ctx, query=query, images=images_bytes, image_urls=image_urls or [], progress=progress
    )
    parsed = parse_agent_answer(agent.answer)
    movie_data = parsed.get("movie") or {}
    confidence = float(parsed.get("confidence") or movie_data.get("confidence") or 0.0)
    saved = None
    publication = None

    if movie_data.get("title") and confidence >= MIN_CONFIDENCE_TO_SAVE:
        external_ids = movie_data.get("external_ids") or {}
        sources = movie_data.get("sources") or []
        if isinstance(external_ids, dict):
            for provider, ext_id in external_ids.items():
                if ext_id:
                    sources.append({"provider": provider, "external_id": str(ext_id), "verified": True})
        dup = await check_duplicate(
            db, movie_data["title"], movie_data.get("year"), external_ids
        )
        if dup["duplicate"]:
            saved = dup["movie"]
        else:
            saved_movie = await create_movie(
                db,
                {
                    **movie_data,
                    "confidence": confidence,
                    "verification_status": parsed.get("verification_status") or "verified",
                },
                sources=sources,
                aliases=movie_data.get("aliases") or [],
                images=movie_data.get("images") or [],
            )
            saved = {
                "id": saved_movie.id,
                "code": saved_movie.code,
                "title": saved_movie.title,
                "year": saved_movie.year,
            }

    # Publicar en el canal privado de índices si hay película registrada.
    if saved and saved.get("code"):
        try:
            movie = await get_movie_by_code(db, saved["code"])
            if movie:
                channel = await ensure_catalog_channel(db)
                if channel.get("channel_id"):
                    from app.catalog.service import get_movie_sources
                    srcs = await get_movie_sources(db, movie.id)
                    message_id = await publish_to_catalog(
                        db, channel["channel_id"], build_catalog_caption(movie, srcs), movie.poster_url
                    )
                    from sqlalchemy import select
                    from app.database.models import TelegramCatalogPublication, TelegramChannel

                    ch_result = await db.execute(
                        select(TelegramChannel).where(TelegramChannel.channel_id == channel["channel_id"])
                    )
                    ch_row = ch_result.scalar_one_or_none()
                    if ch_row:
                        exists = await db.execute(
                            select(TelegramCatalogPublication).where(
                                TelegramCatalogPublication.movie_id == movie.id,
                                TelegramCatalogPublication.channel_id == ch_row.id,
                            )
                        )
                        if exists.scalar_one_or_none() is None:
                            db.add(TelegramCatalogPublication(
                                movie_id=movie.id, channel_id=ch_row.id, message_id=message_id
                            ))
                            await db.flush()
                    publication = {"channel_id": channel["channel_id"], "message_id": message_id}
        except Exception as exc:  # noqa: BLE001
            logger.warning("catalog_publication_failed", extra={"error": type(exc).__name__})

    await record_search_history(
        db, query, [hash_image(img) for img in images_bytes],
        len(agent.candidates), requester_id, job_id,
    )
    formatted = format_professional_answer(parsed, saved, confidence, publication, agent)
    telegram_media = extract_telegram_media(agent)
    result = {
        "job_id": job_id,
        "status": "completed",
        "movie": saved,
        "confidence": confidence,
        "candidates": agent.candidates[: settings.agent_max_candidates],
        "tools_called": agent.tools_called,
        "publication": publication,
        "agent_status": agent.status,
        "answer": parsed,
        "formatted_answer": formatted,
        "trace": agent.trace,
        "telegram_media": telegram_media,
        "usage": agent.usage,
    }
    await set_status(
        db, job_id, "completed",
        result_movie_id=saved.get("id") if saved else None,
        result_confidence=confidence,
        steps_done=agent.steps,
        result_json=result,
    )
    await db.commit()
    return result
