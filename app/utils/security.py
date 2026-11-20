"""Funciones de seguridad: credenciales, cifrado, hashes y redacción."""

from __future__ import annotations

import base64
import hashlib
import hmac
from typing import Any

from cryptography.fernet import Fernet, InvalidToken

from app.config import settings


def verify_bearer_token(authorization: str | None) -> bool:
    """Comparación en tiempo constante contra API_ACCESS_TOKEN."""
    if not authorization:
        return False
    parts = authorization.split(" ", 1)
    if len(parts) != 2 or parts[0].lower() != "bearer":
        return False
    expected = settings.api_access_token
    if not expected:
        return False
    return hmac.compare_digest(parts[1].strip(), expected)


def _fernet() -> Fernet:
    key = settings.job_encryption_key
    if not key:
        raise RuntimeError("JOB_ENCRYPTION_KEY no configurada")
    raw = key.encode()
    try:
        return Fernet(raw)
    except Exception:
        padded = base64.urlsafe_b64encode(
            hashlib.sha256(raw).digest()
        )
        return Fernet(padded)


def encrypt_payload(data: str) -> str:
    """Cifra un payload sensible (p. ej. parámetros de un job)."""
    return _fernet().encrypt(data.encode()).decode()


def decrypt_payload(token: str) -> str:
    try:
        return _fernet().decrypt(token.encode()).decode()
    except InvalidToken as exc:
        raise ValueError("Payload cifrado inválido o manipulado") from exc


def sha256_bytes(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


def sha256_file(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def hash_image(image_bytes: bytes) -> str:
    """Hash perceptual simple (SHA-256 de bytes normalizados)."""
    return sha256_bytes(image_bytes)


def redact(value: str | None) -> str:
    """Devuelve una representación segura de un secreto para diagnósticos."""
    if not value:
        return "<no configurado>"
    return f"***{value[-4:]}" if len(value) > 4 else "***"


SECRET_ENV_NAMES = (
    "RENDER_API_KEY",
    "SUPABASE_DATABASE_URL",
    "NVIDIA_API_KEY",
    "TELEGRAM_API_HASH",
    "TELEGRAM_SESSION",
    "TMDB_API_KEY",
    "TAVILY_API_KEY",
    "BRAVE_SEARCH_API_KEY",
    "YOUTUBE_DATA_API_KEY",
    "API_ACCESS_TOKEN",
    "SESSION_SECRET",
    "JOB_ENCRYPTION_KEY",
)


def secret_values() -> list[str]:
    """Valores reales de secretos, solo para el filtro de redacción de logs."""
    import os

    values: list[str] = []
    for name in SECRET_ENV_NAMES:
        val = os.environ.get(name, "")
        if not val and name == "SUPABASE_DATABASE_URL":
            val = settings.supabase_database_url.replace("postgresql+asyncpg://", "postgresql://")
        if val and len(val) >= 6:
            values.append(val)
    return values


def redact_message(message: str) -> str:
    for secret in secret_values():
        message = message.replace(secret, "***REDACTED***")
    # Contraseñas embebidas en URLs tipo postgresql://user:pass@host
    import re

    message = re.sub(
        r"(postgresql(?:\+asyncpg)?://[^:/@\s]+:)[^@\s/]+(@)",
        r"\1***REDACTED***\2",
        message,
    )
    # Patrones genéricos: key=..., token: ..., "password": "..."
    message = re.sub(
        r"(?i)\b(api[_-]?key|access[_-]?token|token|password|secret|session|authorization|key)"
        r"([\"']?\s*[:=]\s*[\"']?)([^\s\"',}&]+)",
        r"\1\2***REDACTED***",
        message,
    )
    return message


def safe_json(data: Any) -> Any:
    """Prepara datos para logging eliminando claves sensibles."""
    sensitive = {"password", "api_key", "token", "authorization", "secret", "session"}
    if isinstance(data, dict):
        return {
            k: ("***REDACTED***" if k.lower() in sensitive else safe_json(v))
            for k, v in data.items()
        }
    if isinstance(data, (list, tuple)):
        return [safe_json(v) for v in data]
    return data
