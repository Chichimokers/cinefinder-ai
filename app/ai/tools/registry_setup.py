"""Registro central de todas las herramientas del agente."""

from __future__ import annotations

from app.ai.tools import analysis, catalog, delivery, search, telegram_tools, verification

_REGISTERED = False


def register_all() -> None:
    global _REGISTERED
    if _REGISTERED:
        return
    analysis.register()
    search.register()
    catalog.register()
    verification.register()
    telegram_tools.register()
    delivery.register()
    _REGISTERED = True


register_all()

from app.ai.tools.base import registry  # noqa: E402

__all__ = ["registry", "register_all"]
