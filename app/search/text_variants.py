"""Generación de variantes de búsqueda y cálculo de coincidencia real."""

from __future__ import annotations

import re
import unicodedata
from typing import Iterable


def normalize(value: str) -> str:
    """Minúsculas, sin acentos ni puntuación, espacios colapsados."""
    if not value:
        return ""
    decomposed = unicodedata.normalize("NFKD", value)
    ascii_text = "".join(c for c in decomposed if not unicodedata.combining(c))
    ascii_text = re.sub(r"[^\w\s]", " ", ascii_text.lower(), flags=re.UNICODE)
    return re.sub(r"\s+", " ", ascii_text).strip()


def generate_variants(
    query: str,
    year: int | str | None = None,
    season: int | None = None,
    episode: int | None = None,
    aliases: Iterable[str] | None = None,
) -> list[str]:
    """Devuelve variantes útiles para rastrear mensajes y nombres de archivo."""
    variants: list[str] = []
    base = (query or "").strip()
    if base:
        variants.extend([base, normalize(base)])
    for alias in aliases or []:
        if alias:
            variants.extend([str(alias).strip(), normalize(str(alias))])
    if year:
        y = str(year)
        for v in list(variants)[:4]:
            variants.append(f"{v} {y}")
    if season is not None:
        s = f"S{season:02d}"
        variants.append(s)
        if episode is not None:
            variants.append(f"{s}E{episode:02d}")
            variants.append(f"season {season} episode {episode}")
    # Deduplicar preservando orden.
    seen: set[str] = set()
    result: list[str] = []
    for v in variants:
        key = normalize(v)
        if key and len(key) >= 2 and key not in seen:
            seen.add(key)
            result.append(v)
    return result[:24]


def score_match(variants: list[str], text: str, file_name: str | None = None) -> tuple[float, str, str]:
    """Puntuación 0-100 basada en evidencia textual real."""
    haystacks = [normalize(text), normalize(file_name or "")]
    best_score = 0.0
    best_variant = ""
    best_text = ""
    for variant in variants:
        needle = normalize(variant)
        if not needle or len(needle) < 2:
            continue
        tokens = [t for t in needle.split() if len(t) > 2]
        for haystack in haystacks:
            if not haystack:
                continue
            if needle == haystack:
                score = 100.0
            elif needle in haystack:
                score = 88.0 + min(len(needle) / max(len(haystack), 1), 1.0) * 10.0
            elif tokens:
                hits = sum(1 for token in tokens if token in haystack)
                if hits == len(tokens):
                    score = 80.0
                elif hits:
                    score = 45.0 + (hits / len(tokens)) * 35.0
                else:
                    score = 0.0
            else:
                score = 0.0
            if file_name and needle in normalize(file_name) and score:
                score = min(100.0, score + 4.0)
            if score > best_score:
                best_score, best_variant, best_text = score, variant, (text or file_name or "")[:300]
    return round(best_score, 1), best_variant, best_text
