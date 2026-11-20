#!/usr/bin/env python3
"""Inicia el userbot de Telegram para probar los comandos (!busca, !info, ...).

Uso: py -3.12 scripts/run_telegram_worker.py
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.workers.telegram_worker import run_telegram_worker


if __name__ == "__main__":
    print("Iniciando userbot de Telegram. Ctrl+C para detener.")
    asyncio.run(run_telegram_worker())
