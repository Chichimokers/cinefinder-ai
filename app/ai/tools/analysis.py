"""Herramientas de análisis visual con NVIDIA NIM."""

from __future__ import annotations

import json
import re

from app.ai.nvidia_client import get_nvidia_client
from app.ai.tools.base import Tool, ToolContext, registry

SYSTEM_CANDIDATES = """Eres un experto en identificación de películas.
A partir de las pistas visuales y el texto del usuario, devuelve SOLO un JSON con:
{"candidates": [{"title": string, "year": number|null, "reason": string, "confidence": number}],
 "search_queries": [string]}
No inventes títulos si no hay evidencia; en ese caso usa search_queries amplios.
"""


async def analyze_image(ctx: ToolContext) -> dict:
    images = ctx.extra.get("images") or ctx.images
    if not images:
        return {"error": "No se aportaron imágenes"}
    client = get_nvidia_client()
    response = await client.analyze_images(images[:1], "Analiza esta imagen.", ctx.query)
    return {"analysis": response.content, "usage": client.usage.as_dict()}


async def analyze_multiple_images(ctx: ToolContext) -> dict:
    images = ctx.extra.get("images") or ctx.images
    if not images:
        return {"error": "No se aportaron imágenes"}
    client = get_nvidia_client()
    response = await client.analyze_images(
        images, "Analiza este conjunto de imágenes buscando pistas comunes de una misma película.", ctx.query
    )
    return {"analysis": response.content, "image_count": len(images), "usage": client.usage.as_dict()}


async def extract_text_from_image(ctx: ToolContext) -> dict:
    images = ctx.extra.get("images") or ctx.images
    if not images:
        return {"error": "No se aportaron imágenes"}
    client = get_nvidia_client()
    text = await client.extract_text(images[0])
    return {"text": text, "usage": client.usage.as_dict()}


def _parse_json(text: str | None) -> dict:
    if not text:
        return {}
    cleaned = re.sub(r"^```(?:json)?\s*|\s*```$", "", text.strip())
    try:
        return json.loads(cleaned)
    except json.JSONDecodeError:
        match = re.search(r"\{.*\}", cleaned, re.DOTALL)
        if match:
            try:
                return json.loads(match.group(0))
            except json.JSONDecodeError:
                pass
    return {}


async def identify_movie_candidates(ctx: ToolContext) -> dict:
    clues = ctx.extra.get("clues") or ctx.extra.get("analysis") or ""
    client = get_nvidia_client()
    response = await client.chat(
        messages=[
            {"role": "system", "content": SYSTEM_CANDIDATES},
            {
                "role": "user",
                "content": f"Pistas visuales: {clues}\nTexto del usuario: {ctx.query}\n"
                           f"Máximo de candidatos: {ctx.extra.get('max_candidates', 10)}",
            },
        ],
        temperature=0.2,
    )
    parsed = _parse_json(response.content)
    candidates = parsed.get("candidates", [])[: ctx.extra.get("max_candidates", 10)]
    return {
        "candidates": candidates,
        "search_queries": parsed.get("search_queries", []),
        "usage": client.usage.as_dict(),
    }


def register() -> None:
    registry.register(Tool(
        name="analyze_image",
        description="Analiza una imagen para extraer pistas visuales de una película.",
        parameters={
            "type": "object",
            "properties": {"context": {"type": "string", "description": "Contexto adicional"}},
            "required": [],
        },
        handler=analyze_image,
    ))
    registry.register(Tool(
        name="analyze_multiple_images",
        description="Analiza varias imágenes (álbum) buscando pistas comunes.",
        parameters={"type": "object", "properties": {}, "required": []},
        handler=analyze_multiple_images,
    ))
    registry.register(Tool(
        name="extract_text_from_image",
        description="Extrae el texto literal visible en una imagen (cartel, captura).",
        parameters={"type": "object", "properties": {}, "required": []},
        handler=extract_text_from_image,
    ))
    registry.register(Tool(
        name="identify_movie_candidates",
        description="Genera candidatos de película a partir de pistas visuales y texto.",
        parameters={
            "type": "object",
            "properties": {
                "clues": {"type": "string", "description": "Pistas visuales extraídas"},
                "max_candidates": {"type": "integer", "description": "Máximo de candidatos"},
            },
            "required": ["clues"],
        },
        handler=identify_movie_candidates,
    ))
