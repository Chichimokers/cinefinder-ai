"""Dependencias de la API: autenticación, rate limiting y errores."""

from __future__ import annotations

import time
from collections import defaultdict, deque

from fastapi import HTTPException, Request, status

from app.config import settings
from app.utils.security import verify_bearer_token

_buckets: defaultdict[str, deque[float]] = defaultdict(deque)


async def require_auth(request: Request) -> str:
    if not verify_bearer_token(request.headers.get("Authorization")):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Token de acceso inválido o ausente",
            headers={"WWW-Authenticate": "Bearer"},
        )
    return "authenticated"


def check_rate_limit(request: Request) -> bool:
    """Ventana deslizante en memoria por IP. Devuelve False si excede el límite."""
    ip = request.client.host if request.client else "unknown"
    now = time.monotonic()
    bucket = _buckets[ip]
    window = 60.0
    while bucket and bucket[0] < now - window:
        bucket.popleft()
    if len(bucket) >= settings.rate_limit_per_minute:
        return False
    bucket.append(now)
    return True


def apply_rate_limit(request: Request) -> None:
    """Versión para uso dentro de endpoints."""
    if not check_rate_limit(request):
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail="Límite de peticiones por minuto excedido",
        )
