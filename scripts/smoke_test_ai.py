#!/usr/bin/env python3
"""Prueba real contra NVIDIA NIM: visión + function calling.

No imprime secretos. Muestra solo respuestas truncadas.
"""

from __future__ import annotations

import asyncio
import io
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from PIL import Image, ImageDraw

from app.ai.nvidia_client import get_nvidia_client


def make_image() -> bytes:
    image = Image.new("RGB", (640, 360), "white")
    draw = ImageDraw.Draw(image)
    draw.rectangle((20, 20, 620, 340), outline="black", width=4)
    draw.text((60, 80), "CINEFINDER", fill="black")
    draw.text((60, 140), "TEST POSTER 2024", fill="black")
    draw.text((60, 200), "SCENE: SPACE ODYSSEY", fill="black")
    buffer = io.BytesIO()
    image.save(buffer, format="JPEG")
    return buffer.getvalue()


async def main() -> int:
    client = get_nvidia_client()
    image = make_image()

    print("[1/2] Analizando imagen real con el modelo configurado…")
    vision = await client.analyze_images([image], "Describe el texto visible en la imagen.")
    print("  contenido (recortado):", (vision.content or "")[:500].replace("\n", " "))

    print("[2/2] Probando function calling…")
    tools = [{
        "type": "function",
        "function": {
            "name": "extract_title",
            "description": "Extrae un título de texto dado",
            "parameters": {
                "type": "object",
                "properties": {"title": {"type": "string"}},
                "required": ["title"],
            },
        },
    }]
    response = await client.chat(
        messages=[{"role": "user", "content": "Extrae el título 'CINEFINDER' usando la herramienta."}],
        tools=tools,
        tool_choice="auto",
        max_tokens=128,
    )
    print("  tool_calls:", json.dumps(response.tool_calls, ensure_ascii=False)[:400])
    print("  finish_reason:", response.finish_reason)
    print("  uso total de tokens:", client.usage.total_tokens)

    ok_vision = bool(vision.content and vision.content.strip())
    ok_tools = bool(response.tool_calls)
    print(f"\nRESULTADO: vision={'OK' if ok_vision else 'FALLO'} | function_calling={'OK' if ok_tools else 'FALLO'}")
    return 0 if ok_vision and ok_tools else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
