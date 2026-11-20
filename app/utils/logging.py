"""Configuración de logging con redacción automática de secretos."""

from __future__ import annotations

import logging
import os
import sys

from app.utils.security import redact_message


class RedactingFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        try:
            record.msg = redact_message(str(record.msg))
            if record.args:
                record.args = tuple(
                    redact_message(str(a)) if isinstance(a, str) else a for a in record.args
                )
        except Exception:
            pass
        return True


def configure_logging(level: str | None = None) -> None:
    log_level = (level or os.environ.get("LOG_LEVEL", "INFO")).upper()
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(
        logging.Formatter(
            "%(asctime)s | %(levelname)-8s | %(name)s | %(message)s",
            datefmt="%Y-%m-%dT%H:%M:%S%z",
        )
    )
    handler.addFilter(RedactingFilter())

    root = logging.getLogger()
    root.handlers.clear()
    root.addHandler(handler)
    root.setLevel(getattr(logging, log_level, logging.INFO))

    for noisy in ("httpx", "telethon", "asyncpg", "sqlalchemy.engine"):
        logging.getLogger(noisy).setLevel(logging.WARNING)


def get_logger(name: str) -> logging.Logger:
    return logging.getLogger(name)
