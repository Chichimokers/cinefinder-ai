"""Herramientas de catálogo: búsqueda, alta, actualización y consulta."""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import selectinload

from app.ai.tools.base import Tool, ToolContext, registry
from app.catalog.service import (
    check_duplicate,
    create_movie,
    get_movie_by_code,
    get_movie_sources,
    search_catalog,
    update_movie,
)
from app.database.models import Movie


async def _find(ctx: ToolContext):
    code = ctx.extra.get("code")
    movie_id = ctx.extra.get("movie_id")
    if code:
        return await get_movie_by_code(ctx.db, str(code))
    if movie_id:
        return await ctx.db.get(Movie, int(movie_id))
    return None


async def tool_search_catalog(ctx: ToolContext) -> dict:
    results = await search_catalog(ctx.db, ctx.extra.get("query") or ctx.query, ctx.extra.get("limit", 10))
    return {"results": results, "count": len(results)}


async def tool_check_duplicate(ctx: ToolContext) -> dict:
    return await check_duplicate(
        ctx.db,
        ctx.extra.get("title", ""),
        ctx.extra.get("year"),
        ctx.extra.get("external_ids"),
    )


async def tool_save_movie(ctx: ToolContext) -> dict:
    data = ctx.extra
    dup = await check_duplicate(
        ctx.db, data.get("title", ""), data.get("year"), data.get("external_ids")
    )
    if dup["duplicate"]:
        return {"saved": False, "duplicate": True, "movie": dup["movie"], "reason": dup["reason"]}
    movie = await create_movie(
        ctx.db,
        data,
        sources=data.get("sources"),
        aliases=data.get("aliases"),
        images=data.get("images"),
    )
    await ctx.db.commit()
    return {"saved": True, "movie": {
        "id": movie.id, "code": movie.code, "title": movie.title,
        "year": movie.year, "confidence": movie.confidence,
    }}


async def tool_update_movie(ctx: ToolContext) -> dict:
    movie = await _find(ctx)
    if movie is None:
        return {"error": "Película no encontrada"}
    source = ctx.extra.get("changes") or ctx.extra
    changes = {
        k: v for k, v in source.items()
        if k not in {"code", "movie_id", "query", "job_id", "changes"}
    }
    await update_movie(ctx.db, movie, changes)
    await ctx.db.commit()
    return {"updated": True, "code": movie.code, "changes": list(changes)}


async def tool_get_details(ctx: ToolContext) -> dict:
    movie = await _find(ctx)
    if movie is None:
        code = ctx.extra.get("code") or ctx.query
        result = await ctx.db.execute(
            select(Movie).where(Movie.code == str(code).upper()).options(selectinload(Movie.sources))
        )
        movie = result.scalar_one_or_none()
    if movie is None:
        return {"error": "Película no encontrada"}
    return {"movie": {
        "id": movie.id, "code": movie.code, "title": movie.title,
        "original_title": movie.original_title, "year": movie.year,
        "genres": movie.genres, "synopsis": movie.synopsis,
        "poster_url": movie.poster_url, "confidence": movie.confidence,
        "verification_status": movie.verification_status,
    }}


async def tool_get_sources(ctx: ToolContext) -> dict:
    movie = await _find(ctx)
    if movie is None:
        return {"error": "Película no encontrada"}
    return {"sources": await get_movie_sources(ctx.db, movie.id)}


def register() -> None:
    registry.register(Tool(
        name="search_catalog",
        description="Busca películas ya registradas en el catálogo interno.",
        parameters={
            "type": "object",
            "properties": {
                "query": {"type": "string"},
                "limit": {"type": "integer"},
            },
            "required": ["query"],
        },
        handler=tool_search_catalog,
    ))
    registry.register(Tool(
        name="check_duplicate_movie",
        description="Comprueba si una película ya existe antes de guardarla.",
        parameters={
            "type": "object",
            "properties": {
                "title": {"type": "string"},
                "year": {"type": "integer"},
                "external_ids": {"type": "object"},
            },
            "required": ["title"],
        },
        handler=tool_check_duplicate,
    ))
    registry.register(Tool(
        name="save_movie",
        description="Guarda una película verificada en el catálogo (evita duplicados).",
        parameters={
            "type": "object",
            "properties": {
                "title": {"type": "string"},
                "original_title": {"type": "string"},
                "year": {"type": "integer"},
                "genres": {"type": "array", "items": {"type": "string"}},
                "synopsis": {"type": "string"},
                "poster_url": {"type": "string"},
                "confidence": {"type": "number"},
                "verification_status": {"type": "string"},
                "external_ids": {"type": "object"},
                "sources": {"type": "array", "items": {"type": "object"}},
                "aliases": {"type": "array", "items": {"type": "string"}},
                "images": {"type": "array", "items": {"type": "object"}},
            },
            "required": ["title"],
        },
        handler=tool_save_movie,
    ))
    registry.register(Tool(
        name="update_movie",
        description="Actualiza los datos de una película del catálogo (solo propietario).",
        parameters={
            "type": "object",
            "properties": {
                "code": {"type": "string"},
                "movie_id": {"type": "integer"},
                "changes": {"type": "object"},
            },
            "required": [],
        },
        handler=tool_update_movie,
        permission="admin",
    ))
    registry.register(Tool(
        name="get_movie_details",
        description="Obtiene la ficha completa de una película por código CF-xxxxxx.",
        parameters={
            "type": "object",
            "properties": {"code": {"type": "string"}},
            "required": ["code"],
        },
        handler=tool_get_details,
    ))
    registry.register(Tool(
        name="get_movie_sources",
        description="Devuelve las fuentes externas registradas de una película.",
        parameters={
            "type": "object",
            "properties": {"code": {"type": "string"}},
            "required": ["code"],
        },
        handler=tool_get_sources,
    ))
