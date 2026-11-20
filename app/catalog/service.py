"""Servicio de catálogo: alta, búsqueda, deduplicación y trazabilidad."""

from __future__ import annotations

import re
from typing import Any

from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database.models import (
    Movie,
    MovieAlias,
    MovieImage,
    MovieSource,
    SearchHistory,
)
from app.utils.logging import get_logger

logger = get_logger(__name__)


def normalize_title(value: str) -> str:
    value = re.sub(r"[^\w\s]", " ", value.lower(), flags=re.UNICODE)
    return re.sub(r"\s+", " ", value).strip()


async def next_movie_code(db: AsyncSession) -> str:
    result = await db.execute(select(Movie.code).order_by(Movie.code.desc()).limit(1))
    last = result.scalar_one_or_none()
    if not last:
        return "CF-000001"
    match = re.match(r"CF-(\d+)", last)
    number = int(match.group(1)) + 1 if match else 1
    return f"CF-{number:06d}"


async def find_by_external_id(
    db: AsyncSession, provider: str, external_id: str
) -> Movie | None:
    result = await db.execute(
        select(MovieSource).where(
            MovieSource.provider == provider, MovieSource.external_id == str(external_id)
        )
    )
    source = result.scalar_one_or_none()
    if source is None:
        return None
    return await db.get(Movie, source.movie_id)


async def search_catalog(db: AsyncSession, query: str, limit: int = 10) -> list[dict]:
    """Búsqueda textual en títulos y alias (case-insensitive)."""
    like = f"%{normalize_title(query)}%"
    result = await db.execute(
        select(Movie)
        .outerjoin(MovieAlias)
        .where(
            or_(
                Movie.title.ilike(like),
                Movie.original_title.ilike(like),
                MovieAlias.alias.ilike(like),
            )
        )
        .limit(limit)
    )
    movies = result.scalars().unique().all()
    return [_movie_dict(m) for m in movies]


async def check_duplicate(
    db: AsyncSession,
    title: str,
    year: int | None = None,
    external_ids: dict[str, str] | None = None,
) -> dict:
    """Comprueba duplicados por ID externo o por título+año normalizados."""
    for provider, ext_id in (external_ids or {}).items():
        movie = await find_by_external_id(db, provider, ext_id)
        if movie:
            return {"duplicate": True, "reason": f"external_id:{provider}:{ext_id}", "movie": _movie_dict(movie)}

    norm = normalize_title(title)
    result = await db.execute(
        select(Movie).where(
            or_(
                Movie.title.ilike(f"%{norm}%"),
                Movie.original_title.ilike(f"%{norm}%"),
            )
        )
    )
    for movie in result.scalars().all():
        if normalize_title(movie.title) == norm or normalize_title(movie.original_title) == norm:
            if year is None or movie.year is None or abs(movie.year - year) <= 1:
                return {"duplicate": True, "reason": "title_year", "movie": _movie_dict(movie)}
    return {"duplicate": False, "reason": None, "movie": None}


async def create_movie(
    db: AsyncSession,
    data: dict[str, Any],
    sources: list[dict] | None = None,
    aliases: list[str] | None = None,
    images: list[dict] | None = None,
) -> Movie:
    """Crea una película con sus relaciones en una sola transacción."""
    movie = Movie(
        code=await next_movie_code(db),
        title=data.get("title") or data.get("original_title") or "Sin título",
        original_title=data.get("original_title") or data.get("title") or "Sin título",
        year=data.get("year"),
        genres=data.get("genres") or [],
        synopsis=data.get("synopsis") or data.get("overview"),
        poster_url=data.get("poster_url"),
        backdrop_url=data.get("backdrop_url"),
        runtime=data.get("runtime"),
        rating=data.get("rating"),
        confidence=float(data.get("confidence") or 0.0),
        verification_status=data.get("verification_status") or "unverified",
    )
    db.add(movie)
    await db.flush()

    for alias in set(a for a in (aliases or []) if a):
        db.add(MovieAlias(movie_id=movie.id, alias=alias, source=data.get("alias_source")))

    for image in images or []:
        if image.get("url"):
            db.add(
                MovieImage(
                    movie_id=movie.id,
                    url=image["url"],
                    sha256=image.get("sha256"),
                    width=image.get("width"),
                    height=image.get("height"),
                    source=image.get("source"),
                )
            )

    for source in sources or []:
        if source.get("provider") and source.get("external_id"):
            db.add(
                MovieSource(
                    movie_id=movie.id,
                    provider=source["provider"],
                    external_id=str(source["external_id"]),
                    url=source.get("url"),
                    raw_json=source.get("raw_json"),
                    verified=bool(source.get("verified", False)),
                )
            )

    await db.flush()
    return movie


async def get_movie(db: AsyncSession, movie_id: int) -> Movie | None:
    return await db.get(Movie, movie_id)


async def get_movie_by_code(db: AsyncSession, code: str) -> Movie | None:
    result = await db.execute(select(Movie).where(Movie.code == code.upper()))
    return result.scalar_one_or_none()


async def get_movie_sources(db: AsyncSession, movie_id: int) -> list[dict]:
    result = await db.execute(
        select(MovieSource).where(MovieSource.movie_id == movie_id)
    )
    return [
        {
            "provider": s.provider,
            "external_id": s.external_id,
            "url": s.url,
            "verified": s.verified,
        }
        for s in result.scalars().all()
    ]


async def add_source(db: AsyncSession, movie_id: int, source: dict) -> MovieSource:
    obj = MovieSource(
        movie_id=movie_id,
        provider=source["provider"],
        external_id=str(source["external_id"]),
        url=source.get("url"),
        raw_json=source.get("raw_json"),
        verified=bool(source.get("verified", False)),
    )
    db.add(obj)
    await db.flush()
    return obj


async def update_movie(db: AsyncSession, movie: Movie, changes: dict) -> Movie:
    allowed = {
        "title", "original_title", "year", "genres", "synopsis", "poster_url",
        "backdrop_url", "runtime", "rating", "confidence", "verification_status", "status",
    }
    for key, value in (changes or {}).items():
        if key in allowed and value is not None:
            setattr(movie, key, value)
    await db.flush()
    return movie


async def record_search_history(
    db: AsyncSession,
    query: str | None,
    image_hashes: list[str],
    result_count: int,
    requester_id: str | None,
    job_id: str | None = None,
) -> None:
    db.add(
        SearchHistory(
            job_id=job_id,
            requester_id=requester_id,
            query=query,
            image_hashes=image_hashes,
            result_count=result_count,
        )
    )


def _movie_dict(movie: Movie) -> dict:
    return {
        "id": movie.id,
        "code": movie.code,
        "title": movie.title,
        "original_title": movie.original_title,
        "year": movie.year,
        "genres": movie.genres,
        "synopsis": movie.synopsis,
        "poster_url": movie.poster_url,
        "confidence": movie.confidence,
        "verification_status": movie.verification_status,
    }
