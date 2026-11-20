"""Integraciones HTTP reales con fuentes cinematográficas.

Ninguna función inventa resultados: si la fuente no está configurada o falla,
devuelve una lista vacía con un motivo.
"""

from __future__ import annotations

import os
from typing import Any

import httpx

from app.config import settings
from app.utils.logging import get_logger

logger = get_logger(__name__)

USER_AGENT = "CineFinderAI/1.0 (research agent; contact: owner)"


def _empty(reason: str) -> dict:
    return {"results": [], "provider": None, "reason": reason}


async def search_tmdb(query: str, year: int | None = None) -> dict[str, Any]:
    if not settings.tmdb_api_key:
        return _empty("TMDB_API_KEY no configurada")
    params: dict[str, Any] = {
        "api_key": settings.tmdb_api_key,
        "query": query,
        "language": "es-ES",
        "include_adult": "false",
    }
    if year:
        params["year"] = year
    try:
        async with httpx.AsyncClient(timeout=20, headers={"User-Agent": USER_AGENT}) as client:
            resp = await client.get("https://api.themoviedb.org/3/search/movie", params=params)
            if resp.status_code != 200:
                return _empty(f"TMDB HTTP {resp.status_code}")
            data = resp.json()
        results = [
            {
                "provider": "tmdb",
                "external_id": str(item.get("id")),
                "title": item.get("title") or "",
                "original_title": item.get("original_title") or "",
                "year": (item.get("release_date") or "")[:4] or None,
                "overview": item.get("overview"),
                "poster_url": (
                    f"https://image.tmdb.org/t/p/w500{item['poster_path']}"
                    if item.get("poster_path") else None
                ),
                "rating": item.get("vote_average"),
            }
            for item in data.get("results", [])
        ]
        return {"results": results, "provider": "tmdb", "reason": None}
    except Exception as exc:  # noqa: BLE001
        return _empty(f"TMDB {type(exc).__name__}")


async def search_omdb(query: str, year: int | None = None) -> dict[str, Any]:
    api_key = os.environ.get("OMDB_API_KEY", "")
    if not api_key:
        return _empty("OMDB_API_KEY no configurada")
    params: dict[str, Any] = {"apikey": api_key, "t": query, "plot": "short"}
    if year:
        params["y"] = year
    try:
        async with httpx.AsyncClient(timeout=20, headers={"User-Agent": USER_AGENT}) as client:
            resp = await client.get("https://www.omdbapi.com/", params=params)
            data = resp.json()
        if data.get("Response") != "True":
            return _empty("OMDB sin resultados")
        return {
            "results": [{
                "provider": "omdb",
                "external_id": data.get("imdbID"),
                "title": data.get("Title"),
                "original_title": data.get("Title"),
                "year": data.get("Year"),
                "overview": data.get("Plot"),
                "poster_url": data.get("Poster") if data.get("Poster") != "N/A" else None,
                "genres": (data.get("Genre") or "").split(", "),
            }],
            "provider": "omdb",
            "reason": None,
        }
    except Exception as exc:  # noqa: BLE001
        return _empty(f"OMDB {type(exc).__name__}")


async def search_wikidata(query: str) -> dict[str, Any]:
    """SPARQL contra Wikidata: películas con etiqueta coincidente."""
    sparql = """
    SELECT ?item ?itemLabel ?imdb ?year ?image WHERE {
      ?item wdt:P31 wd:Q11424.
      ?item rdfs:label ?label.
      FILTER(LANG(?label) = "en" || LANG(?label) = "es")
      FILTER(CONTAINS(LCASE(STR(?label)), LCASE(?q)))
      OPTIONAL { ?item wdt:P345 ?imdb. }
      OPTIONAL { ?item wdt:P577 ?date. BIND(YEAR(?date) AS ?year) }
      OPTIONAL { ?item wdt:P18 ?image. }
      SERVICE wikibase:label { bd:serviceParam wikibase:language "es,en". }
    } LIMIT 20
    """
    try:
        async with httpx.AsyncClient(timeout=30, headers={"User-Agent": USER_AGENT}) as client:
            resp = await client.get(
                "https://query.wikidata.org/sparql",
                params={"query": sparql, "q": query, "format": "json"},
                headers={"Accept": "application/sparql-results+json"},
            )
            if resp.status_code != 200:
                return _empty(f"Wikidata HTTP {resp.status_code}")
            bindings = resp.json().get("results", {}).get("bindings", [])
        results = [
            {
                "provider": "wikidata",
                "external_id": b.get("item", {}).get("value", "").rsplit("/", 1)[-1],
                "title": b.get("itemLabel", {}).get("value", ""),
                "original_title": b.get("itemLabel", {}).get("value", ""),
                "year": b.get("year", {}).get("value"),
                "imdb_id": b.get("imdb", {}).get("value"),
                "poster_url": b.get("image", {}).get("value"),
            }
            for b in bindings
        ]
        return {"results": results, "provider": "wikidata", "reason": None}
    except Exception as exc:  # noqa: BLE001
        return _empty(f"Wikidata {type(exc).__name__}")


async def search_web(query: str, max_results: int = 5) -> dict[str, Any]:
    """Búsqueda web con Tavily o Brave (el que esté configurado)."""
    if settings.tavily_api_key:
        try:
            async with httpx.AsyncClient(timeout=25, headers={"User-Agent": USER_AGENT}) as client:
                resp = await client.post(
                    "https://api.tavily.com/search",
                    json={
                        "api_key": settings.tavily_api_key,
                        "query": query,
                        "max_results": max_results,
                        "search_depth": "basic",
                    },
                )
                if resp.status_code != 200:
                    return _empty(f"Tavily HTTP {resp.status_code}")
                data = resp.json()
            results = [
                {
                    "provider": "tavily",
                    "title": r.get("title"),
                    "url": r.get("url"),
                    "content": r.get("content"),
                    "score": r.get("score"),
                }
                for r in data.get("results", [])
            ]
            return {"results": results, "provider": "tavily", "reason": None}
        except Exception as exc:  # noqa: BLE001
            return _empty(f"Tavily {type(exc).__name__}")

    if settings.brave_search_api_key:
        try:
            async with httpx.AsyncClient(timeout=25, headers={"User-Agent": USER_AGENT}) as client:
                resp = await client.get(
                    "https://api.search.brave.com/res/v1/web/search",
                    params={"q": query, "count": max_results},
                    headers={
                        "X-Subscription-Token": settings.brave_search_api_key,
                        "Accept": "application/json",
                    },
                )
                if resp.status_code != 200:
                    return _empty(f"Brave HTTP {resp.status_code}")
                data = resp.json()
            results = [
                {
                    "provider": "brave",
                    "title": r.get("title"),
                    "url": r.get("url"),
                    "content": r.get("description"),
                }
                for r in data.get("web", {}).get("results", [])
            ]
            return {"results": results, "provider": "brave", "reason": None}
        except Exception as exc:  # noqa: BLE001
            return _empty(f"Brave {type(exc).__name__}")

    return _empty("Ni TAVILY_API_KEY ni BRAVE_SEARCH_API_KEY configuradas")


async def search_youtube(query: str, max_results: int = 5) -> dict[str, Any]:
    if not settings.youtube_data_api_key:
        return _empty("YOUTUBE_DATA_API_KEY no configurada")
    try:
        async with httpx.AsyncClient(timeout=20, headers={"User-Agent": USER_AGENT}) as client:
            resp = await client.get(
                "https://www.googleapis.com/youtube/v3/search",
                params={
                    "part": "snippet",
                    "q": f"{query} trailer",
                    "type": "video",
                    "maxResults": max_results,
                    "key": settings.youtube_data_api_key,
                },
            )
            if resp.status_code != 200:
                return _empty(f"YouTube HTTP {resp.status_code}")
            data = resp.json()
        results = [
            {
                "provider": "youtube",
                "video_id": item.get("id", {}).get("videoId"),
                "title": item.get("snippet", {}).get("title"),
                "url": f"https://www.youtube.com/watch?v={item.get('id', {}).get('videoId')}",
                "description": item.get("snippet", {}).get("description"),
            }
            for item in data.get("items", [])
        ]
        return {"results": results, "provider": "youtube", "reason": None}
    except Exception as exc:  # noqa: BLE001
        return _empty(f"YouTube {type(exc).__name__}")


async def search_internet_archive(query: str, max_results: int = 5) -> dict[str, Any]:
    """Solo metadatos públicos; nunca descarga contenido."""
    try:
        async with httpx.AsyncClient(timeout=25, headers={"User-Agent": USER_AGENT}) as client:
            resp = await client.get(
                "https://archive.org/advancedsearch.php",
                params={
                    "q": f'title:({query}) AND mediatype:movies',
                    "fl[]": ["identifier", "title", "year", "licenseurl"],
                    "rows": max_results,
                    "output": "json",
                },
            )
            if resp.status_code != 200:
                return _empty(f"Internet Archive HTTP {resp.status_code}")
            docs = resp.json().get("response", {}).get("docs", [])
        results = [
            {
                "provider": "internet_archive",
                "external_id": d.get("identifier"),
                "title": d.get("title"),
                "year": d.get("year"),
                "license": d.get("licenseurl"),
                "url": f"https://archive.org/details/{d.get('identifier')}",
            }
            for d in docs
        ]
        return {"results": results, "provider": "internet_archive", "reason": None}
    except Exception as exc:  # noqa: BLE001
        return _empty(f"Internet Archive {type(exc).__name__}")


async def search_movie_images(query: str, year: int | None = None) -> dict[str, Any]:
    """Carteles/fotogramas desde TMDB (si hay clave) o Wikimedia Commons."""
    tmdb = await search_tmdb(query, year)
    if tmdb["results"]:
        images = [
            {"url": r["poster_url"], "source": "tmdb", "title": r["title"]}
            for r in tmdb["results"] if r.get("poster_url")
        ]
        return {"results": images, "provider": "tmdb", "reason": None}

    try:
        async with httpx.AsyncClient(timeout=25, headers={"User-Agent": USER_AGENT}) as client:
            resp = await client.get(
                "https://commons.wikimedia.org/w/api.php",
                params={
                    "action": "query",
                    "generator": "search",
                    "gsrsearch": f"{query} film poster",
                    "gsrnamespace": 6,
                    "gsrlimit": 8,
                    "prop": "imageinfo",
                    "iiprop": "url",
                    "format": "json",
                },
            )
            if resp.status_code != 200:
                return _empty(f"Wikimedia HTTP {resp.status_code}")
            pages = resp.json().get("query", {}).get("pages", {})
        images = [
            {
                "url": p.get("imageinfo", [{}])[0].get("url"),
                "source": "wikimedia",
                "title": p.get("title"),
            }
            for p in pages.values()
            if p.get("imageinfo")
        ]
        return {"results": images, "provider": "wikimedia", "reason": None}
    except Exception as exc:  # noqa: BLE001
        return _empty(f"Wikimedia {type(exc).__name__}")
