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


def format_professional_answer(
    parsed: dict, movie: dict | None, confidence: float, publication: dict | None, agent: AgentResult
) -> str:
    """Respuesta final profesional, legible en Telegram y en la API."""
    if movie:
        header = f"🎬 *{movie.get('code', '')} — {movie.get('title', '')}*"
        if movie.get("year"):
            header += f" ({movie['year']})"
        lines = [header]
    else:
        lines = ["🔎 *Resultado de la investigación*"]

    lines.append(f"📊 Confianza: *{confidence:.0%}*")
    status = parsed.get("verification_status") or ("verificado" if movie else "sin verificar")
    lines.append(f"✅ Estado: {status}")

    reasoning = parsed.get("reasoning") or parsed.get("raw")
    if reasoning:
        lines.append(f"\n🧠 *Análisis*\n{str(reasoning)[:900]}")

    sources = parsed.get("sources") or []
    if sources:
        lines.append("\n🔗 *Fuentes verificables*")
        for source in sources[:6]:
            provider = source.get("provider", "fuente")
            external = source.get("external_id") or ""
            url = source.get("url")
            label = f"• {provider}" + (f" · {external}" if external else "")
            lines.append(f"{label}: {url}" if url else label)

    alternatives = parsed.get("alternatives") or []
    if alternatives:
        lines.append("\n🔄 *Alternativas consideradas*")
        for alt in alternatives[:4]:
            lines.append(f"• {alt.get('title')}" + (f" ({alt.get('year')})" if alt.get("year") else ""))

    lines.append(f"\n🛠️ Herramientas: {', '.join(agent.tools_called[:12])}")
    lines.append(f"🧩 Pasos: {agent.steps} | Tokens: {agent.usage.get('total_tokens', 0)}")
    if publication:
        lines.append(f"📚 Publicado en el índice privado (mensaje {publication.get('message_id')}).")
    if parsed.get("need_review"):
        lines.append("\n⚠️ Se recomienda revisión manual antes de darla por definitiva.")
    return "\n".join(lines)


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
