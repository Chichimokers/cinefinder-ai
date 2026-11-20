"""Cliente NVIDIA NIM compatible con la API de OpenAI.

Soporta:
- Chat completions con function calling.
- Imágenes (URLs o bytes en base64) para análisis multimodal.
- Control de tokens y reintentos.
"""

from __future__ import annotations

import base64
from dataclasses import dataclass, field
from typing import Any

from openai import AsyncOpenAI
from tenacity import retry, stop_after_attempt, wait_exponential

from app.config import settings
from app.utils.logging import get_logger

logger = get_logger(__name__)


@dataclass
class UsageStats:
    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0
    calls: int = 0

    def add(self, usage: Any) -> None:
        if usage is None:
            return
        self.prompt_tokens += getattr(usage, "prompt_tokens", 0) or 0
        self.completion_tokens += getattr(usage, "completion_tokens", 0) or 0
        self.total_tokens += getattr(usage, "total_tokens", 0) or 0
        self.calls += 1

    def as_dict(self) -> dict:
        return {
            "prompt_tokens": self.prompt_tokens,
            "completion_tokens": self.completion_tokens,
            "total_tokens": self.total_tokens,
            "calls": self.calls,
        }


@dataclass
class NIMResponse:
    content: str | None
    tool_calls: list[dict] = field(default_factory=list)
    usage: UsageStats = field(default_factory=UsageStats)
    finish_reason: str | None = None
    raw: dict | None = None


class NVIDIAClient:
    def __init__(self) -> None:
        self._client: AsyncOpenAI | None = None
        self.usage = UsageStats()

    @property
    def client(self) -> AsyncOpenAI:
        if self._client is None:
            if not settings.nvidia_api_key:
                raise RuntimeError("NVIDIA_API_KEY no configurada")
            self._client = AsyncOpenAI(
                api_key=settings.nvidia_api_key,
                base_url=settings.nvidia_base_url,
                timeout=settings.agent_timeout_seconds,
                max_retries=0,
            )
        return self._client

    @staticmethod
    def _image_content(images: list[bytes | str]) -> list[dict]:
        parts: list[dict] = []
        for image in images:
            if isinstance(image, bytes):
                b64 = base64.b64encode(image).decode("ascii")
                parts.append(
                    {
                        "type": "image_url",
                        "image_url": {"url": f"data:image/jpeg;base64,{b64}", "detail": "high"},
                    }
                )
            else:
                parts.append(
                    {"type": "image_url", "image_url": {"url": image, "detail": "high"}}
                )
        return parts

    @retry(stop=stop_after_attempt(3), wait=wait_exponential(multiplier=1, min=1, max=8))
    async def chat(
        self,
        messages: list[dict],
        tools: list[dict] | None = None,
        tool_choice: str | dict = "auto",
        temperature: float | None = None,
        max_tokens: int | None = None,
        track: bool = True,
    ) -> NIMResponse:
        kwargs: dict[str, Any] = {
            "model": settings.nvidia_model,
            "messages": messages,
            "temperature": settings.nvidia_temperature if temperature is None else temperature,
            "max_tokens": settings.nvidia_max_tokens if max_tokens is None else max_tokens,
        }
        if tools:
            kwargs["tools"] = tools
            kwargs["tool_choice"] = tool_choice

        response = await self.client.chat.completions.create(**kwargs)
        choice = response.choices[0]
        message = choice.message
        self.usage.add(response.usage)

        tool_calls: list[dict] = []
        for call in getattr(message, "tool_calls", None) or []:
            tool_calls.append(
                {
                    "id": call.id,
                    "type": "function",
                    "function": {
                        "name": call.function.name,
                        "arguments": call.function.arguments,
                    },
                }
            )

        raw: dict = {}
        try:
            raw = response.model_dump()
        except Exception:  # noqa: BLE001
            pass

        return NIMResponse(
            content=message.content,
            tool_calls=tool_calls,
            usage=self.usage if track else UsageStats(),
            finish_reason=choice.finish_reason,
            raw=raw,
        )

    async def analyze_images(
        self,
        images: list[bytes | str],
        instruction: str,
        context: str = "",
    ) -> NIMResponse:
        """Analiza una o varias imágenes y devuelve la interpretación del modelo."""
        parts: list[dict] = []
        if context:
            parts.append({"type": "text", "text": f"Contexto del usuario: {context}"})
        parts.append({"type": "text", "text": instruction})
        parts.extend(self._image_content(images))
        return await self.chat(
            messages=[
                {
                    "role": "system",
                    "content": (
                        "Eres un analista visual cinematográfico. Describe únicamente lo que "
                        "aparece en las imágenes: títulos visibles, rostros, carteles, escenas, "
                        "años, nombres y cualquier pista verificable. No inventes datos. "
                        "Responde en JSON con las claves: visual_clues (lista), "
                        "possible_titles (lista), text_found (texto literal), confidence (0-1)."
                    ),
                },
                {"role": "user", "content": parts},
            ],
            temperature=0.1,
            max_tokens=1200,
        )

    async def extract_text(self, image: bytes | str) -> str:
        response = await self.analyze_images(
            [image],
            "Extrae TODO el texto legible que aparezca en la imagen, tal cual. "
            "Devuelve solo el texto, sin comentarios.",
        )
        return response.content or ""

    async def probe(self) -> dict:
        """Comprueba autenticación, modelo y capacidades básicas."""
        try:
            result = await self.chat(
                messages=[{"role": "user", "content": "Responde únicamente: OK"}],
                max_tokens=16,
            )
            return {
                "ok": bool(result.content),
                "model": settings.nvidia_model,
                "supports_function_calling": True,  # se valida en el primer uso con tools
                "usage": result.usage.as_dict(),
            }
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}


_client: NVIDIAClient | None = None


def get_nvidia_client() -> NVIDIAClient:
    global _client
    if _client is None:
        _client = NVIDIAClient()
    return _client
