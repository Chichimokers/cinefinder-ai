"""Infraestructura de herramientas con esquemas, permisos y validación."""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable

from sqlalchemy.ext.asyncio import AsyncSession

from app.utils.logging import get_logger

logger = get_logger(__name__)

Handler = Callable[["ToolContext"], Awaitable[Any]]


@dataclass
class ToolContext:
    db: AsyncSession
    job_id: str | None = None
    requester_id: str | None = None
    is_owner: bool = False
    images: list[bytes] = field(default_factory=list)
    image_urls: list[str] = field(default_factory=list)
    query: str = ""
    extra: dict[str, Any] = field(default_factory=dict)


@dataclass
class ToolResult:
    ok: bool
    data: Any = None
    error: str | None = None
    duration_ms: int = 0

    def as_dict(self) -> dict:
        out: dict[str, Any] = {"ok": self.ok, "duration_ms": self.duration_ms}
        if self.ok:
            out["data"] = self.data
        else:
            out["error"] = self.error
        return out


@dataclass
class Tool:
    name: str
    description: str
    parameters: dict[str, Any]
    handler: Handler
    permission: str = "user"  # user | admin | system

    def openai_schema(self) -> dict:
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": self.parameters,
            },
        }


class ToolRegistry:
    def __init__(self) -> None:
        self._tools: dict[str, Tool] = {}

    def register(self, tool: Tool) -> None:
        self._tools[tool.name] = tool

    def get(self, name: str) -> Tool | None:
        return self._tools.get(name)

    def schemas(self, is_owner: bool = False) -> list[dict]:
        allowed = {"user", "system"} | ({"admin"} if is_owner else set())
        return [
            t.openai_schema() for t in self._tools.values() if t.permission in allowed
        ]

    def names(self) -> list[str]:
        return sorted(self._tools)

    async def execute(self, name: str, arguments: dict, ctx: ToolContext) -> ToolResult:
        tool = self._tools.get(name)
        started = time.perf_counter()
        if tool is None:
            return ToolResult(False, error=f"Herramienta desconocida: {name}")
        if tool.permission == "admin" and not ctx.is_owner:
            return ToolResult(False, error=f"Permiso denegado para {name}")

        # Validación básica de tipos según el esquema.
        validation_error = self._validate(tool, arguments)
        if validation_error:
            return ToolResult(False, error=validation_error)

        try:
            captured: dict[str, Any] = dict(arguments)
            data = await tool.handler(
                ToolContext(
                    db=ctx.db,
                    job_id=ctx.job_id,
                    requester_id=ctx.requester_id,
                    is_owner=ctx.is_owner,
                    images=ctx.images,
                    image_urls=ctx.image_urls,
                    query=ctx.query,
                    extra={**ctx.extra, **captured},
                )
            )
            result = ToolResult(True, data=data)
        except Exception as exc:  # noqa: BLE001
            logger.warning("tool_error", extra={"tool": name, "error": type(exc).__name__})
            result = ToolResult(False, error=f"{type(exc).__name__}: {exc}")
        result.duration_ms = int((time.perf_counter() - started) * 1000)
        return result

    @staticmethod
    def _validate(tool: Tool, arguments: dict) -> str | None:
        schema = tool.parameters
        required = schema.get("required", [])
        for key in required:
            if key not in arguments or arguments[key] in (None, ""):
                return f"Falta el parámetro obligatorio '{key}' en {tool.name}"
        props = schema.get("properties", {})
        for key, value in arguments.items():
            spec = props.get(key)
            if not spec:
                continue
            expected = spec.get("type")
            if expected == "string" and not isinstance(value, str):
                return f"'{key}' debe ser string en {tool.name}"
            if expected == "integer" and not isinstance(value, int):
                return f"'{key}' debe ser integer en {tool.name}"
            if expected == "number" and not isinstance(value, (int, float)):
                return f"'{key}' debe ser number en {tool.name}"
            if expected == "array" and not isinstance(value, list):
                return f"'{key}' debe ser array en {tool.name}"
            if expected == "boolean" and not isinstance(value, bool):
                return f"'{key}' debe ser boolean en {tool.name}"
        return None


registry = ToolRegistry()
