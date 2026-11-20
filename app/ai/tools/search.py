"""Herramientas de búsqueda cinematográfica y web."""

from __future__ import annotations

from app.ai.tools.base import Tool, ToolContext, registry
from app.search.providers import (
    search_internet_archive,
    search_movie_images,
    search_omdb,
    search_tmdb,
    search_web,
    search_wikidata,
    search_youtube,
)


async def _simple(ctx: ToolContext, func, key: str = "query", **extra):
    value = ctx.extra.get(key) or ctx.query
    return await func(value, **extra)


async def tmdb(ctx: ToolContext) -> dict:
    return await _simple(ctx, search_tmdb, year=ctx.extra.get("year"))


async def omdb(ctx: ToolContext) -> dict:
    return await _simple(ctx, search_omdb, year=ctx.extra.get("year"))


async def wikidata(ctx: ToolContext) -> dict:
    return await _simple(ctx, search_wikidata)


async def web(ctx: ToolContext) -> dict:
    return await _simple(ctx, search_web, max_results=ctx.extra.get("max_results", 5))


async def youtube(ctx: ToolContext) -> dict:
    return await _simple(ctx, search_youtube, max_results=ctx.extra.get("max_results", 5))


async def archive(ctx: ToolContext) -> dict:
    return await _simple(ctx, search_internet_archive, max_results=ctx.extra.get("max_results", 5))


async def images(ctx: ToolContext) -> dict:
    return await _simple(ctx, search_movie_images, year=ctx.extra.get("year"))


def register() -> None:
    definitions = [
        ("search_tmdb", "Busca películas en The Movie Database.", tmdb, {"year": "integer"}),
        ("search_omdb", "Busca películas en OMDb (requiere OMDB_API_KEY).", omdb, {"year": "integer"}),
        ("search_wikidata", "Busca películas en Wikidata mediante SPARQL.", wikidata, {}),
        ("search_movie_images", "Busca carteles e imágenes de una película.", images, {"year": "integer"}),
        ("search_web", "Busca en Internet (Tavily o Brave) para contrastar datos.", web, {"max_results": "integer"}),
        ("search_youtube", "Busca tráilers y vídeos en YouTube.", youtube, {"max_results": "integer"}),
        ("search_internet_archive", "Consulta metadatos públicos de Internet Archive.", archive, {"max_results": "integer"}),
    ]
    for name, description, handler, types in definitions:
        properties = {"query": {"type": "string", "description": "Texto a buscar"}}
        for key, typ in types.items():
            properties[key] = {"type": typ}
        registry.register(Tool(
            name=name,
            description=description,
            parameters={
                "type": "object",
                "properties": properties,
                "required": ["query"],
            },
            handler=handler,
        ))
