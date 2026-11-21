"""Control de acceso centralizado de CineFinder AI.

Reglas:
- El propietario (TELEGRAM_OWNER_ID) tiene acceso absoluto y no puede ser revocado.
- La autorización se resuelve por Telegram User ID real, nunca por username.
- El username se guarda solo como dato auxiliar; si cambia, se actualiza al resolverlo.
- Todo se valida en cada interacción (mensajes y callbacks).
"""

from __future__ import annotations

import re
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from telethon.errors import RPCError
from telethon.tl.types import User

from app.config import settings
from app.database.models import AppUser
from app.telegram.client import get_telegram_client
from app.utils.logging import get_logger

logger = get_logger(__name__)

UNAUTHORIZED_MESSAGE = (
    "⛔ No tienes autorización para utilizar CineFinder AI. Contacta con el administrador."
)


def is_owner_id(user_id: int | None) -> bool:
    return bool(user_id and settings.telegram_owner_id and int(user_id) == int(settings.telegram_owner_id))


async def resolve_user(
    db: AsyncSession, reference: str, replied_user_id: int | None = None
) -> dict[str, Any]:
    """Resuelve @username, ID numérico o un mensaje respondido a un User ID real.

    Nunca se fía de texto arbitrario: todo se verifica contra MTProto.
    Si el usuario responde a un mensaje ajeno, se usa el sender_id real de Telegram.
    """
    if replied_user_id:
        client = get_telegram_client()
        username, first_name = None, None
        if client is not None and client.is_connected():
            try:
                entity = await client.get_entity(int(replied_user_id))
                if isinstance(entity, User):
                    username = getattr(entity, "username", None)
                    first_name = getattr(entity, "first_name", None)
            except (RPCError, ValueError, TypeError):
                pass
        return {
            "ok": True,
            "telegram_id": int(replied_user_id),
            "username": username,
            "first_name": first_name,
            "source": "reply",
        }

    reference = (reference or "").strip()
    if not reference:
        return {"ok": False, "error": "Indica un @usuario, un ID numérico o responde a su mensaje."}

    clean = reference.lstrip("@").strip()

    if clean.lstrip("-").isdigit():
        # Un ID escrito a mano NO es prueba de identidad: se verifica en Telegram.
        client = get_telegram_client()
        if client is None or not client.is_connected():
            return {"ok": False, "error": "No se pudo verificar el ID: userbot desconectado."}
        try:
            entity = await client.get_entity(int(clean))
        except (RPCError, ValueError, TypeError):
            return {"ok": False, "error": f"No existe un usuario de Telegram con ID {clean}."}
        if not isinstance(entity, User):
            return {"ok": False, "error": "Ese ID no corresponde a un usuario."}
        result = await db.execute(select(AppUser).where(AppUser.telegram_id == int(entity.id)))
        row = result.scalar_one_or_none()
        return {
            "ok": True,
            "telegram_id": int(entity.id),
            "username": getattr(entity, "username", None) or (row.username if row else None),
            "first_name": getattr(entity, "first_name", None),
            "source": "id",
        }

    if not re.match(r"^[A-Za-z0-9_]{4,32}$", clean):
        return {"ok": False, "error": "Username de Telegram no válido."}

    client = get_telegram_client()
    if client is None or not client.is_connected():
        return {"ok": False, "error": "No se pudo resolver el username: userbot desconectado."}
    try:
        entity = await client.get_entity(f"@{clean}")
    except (RPCError, ValueError, TypeError) as exc:
        logger.warning("username_resolution_failed", extra={"error": type(exc).__name__})
        return {"ok": False, "error": f"No se pudo resolver @{clean} en Telegram."}
    if not isinstance(entity, User):
        return {"ok": False, "error": "@username no corresponde a un usuario."}

    return {
        "ok": True,
        "telegram_id": int(entity.id),
        "username": getattr(entity, "username", None) or clean,
        "first_name": getattr(entity, "first_name", None),
        "source": "username",
    }


async def check_access(db: AsyncSession, user_id: int | None) -> dict[str, Any]:
    """Valida acceso en cada interacción. Devuelve owner/authorized/allowed."""
    if is_owner_id(user_id):
        return {"allowed": True, "owner": True, "authorized": True, "user": None}
    if not user_id:
        return {"allowed": False, "owner": False, "authorized": False, "user": None}
    if db is None:
        return {"allowed": False, "owner": False, "authorized": False, "user": None}
    result = await db.execute(
        select(AppUser).where(AppUser.telegram_id == int(user_id))
    )
    user = result.scalar_one_or_none()
    allowed = bool(
        user and user.is_authorized and not user.is_banned and not user.is_owner
    )
    # Si el usuario está baneado o se revocó, se devuelve denegado inmediatamente.
    return {
        "allowed": allowed,
        "owner": False,
        "authorized": bool(allowed),
        "user": user,
    }


async def is_authorized(db: AsyncSession, user_id: int | None) -> bool:
    """Compatibilidad con el worker actual."""
    return (await check_access(db, user_id))["allowed"]


def _label(resolved: dict) -> str:
    username = resolved.get("username")
    return f"@{username}" if username else f"ID {resolved['telegram_id']}"


async def authorize(db: AsyncSession, reference: str, replied_user_id: int | None = None) -> dict[str, Any]:
    resolved = await resolve_user(db, reference, replied_user_id)
    if not resolved["ok"]:
        return resolved
    uid = resolved["telegram_id"]
    if is_owner_id(uid):
        return {"ok": False, "error": "El propietario ya tiene acceso absoluto."}

    result = await db.execute(select(AppUser).where(AppUser.telegram_id == uid))
    user = result.scalar_one_or_none()
    if user is None:
        user = AppUser(telegram_id=uid)
        db.add(user)
    user.username = resolved.get("username") or user.username
    user.first_name = resolved.get("first_name") or user.first_name
    user.is_authorized = True
    user.is_owner = False
    user.is_banned = False
    await db.flush()
    return {
        "ok": True,
        "telegram_id": uid,
        "username": user.username,
        "message": f"✅ Usuario autorizado: {_label(resolved)} (ID {uid}).",
    }


async def revoke(db: AsyncSession, reference: str, replied_user_id: int | None = None) -> dict[str, Any]:
    resolved = await resolve_user(db, reference, replied_user_id)
    if not resolved["ok"]:
        return resolved
    uid = resolved["telegram_id"]
    if is_owner_id(uid):
        return {"ok": False, "error": "El propietario no puede ser revocado."}
    result = await db.execute(select(AppUser).where(AppUser.telegram_id == uid))
    user = result.scalar_one_or_none()
    if user is None:
        return {"ok": False, "error": "Ese usuario no está registrado."}
    user.is_authorized = False
    user.is_banned = True
    await db.flush()
    return {"ok": True, "message": f"✅ Autorización revocada: {_label(resolved)} (ID {uid})."}


async def list_authorized(db: AsyncSession) -> str:
    result = await db.execute(
        select(AppUser).where(
            AppUser.is_authorized.is_(True), AppUser.is_banned.is_(False)
        ).order_by(AppUser.created_at)
    )
    users = result.scalars().all()
    lines = [f"👑 Propietario: {settings.telegram_owner_id}"]
    if not users:
        lines.append("No hay usuarios autorizados adicionales.")
    for user in users:
        if is_owner_id(user.telegram_id):
            continue
        lines.append(f"• @{user.username or 'sin username'} — ID {user.telegram_id}")
    return "\n".join(lines)


async def user_info(db: AsyncSession, reference: str, replied_user_id: int | None = None) -> str:
    resolved = await resolve_user(db, reference, replied_user_id)
    if not resolved["ok"]:
        return resolved["error"]
    uid = resolved["telegram_id"]
    if is_owner_id(uid):
        return f"👑 ID {uid} — propietario (acceso absoluto)."
    result = await db.execute(select(AppUser).where(AppUser.telegram_id == uid))
    user = result.scalar_one_or_none()
    if user is None:
        return f"ℹ️ ID {uid} — no registrado."
    state = "autorizado" if user.is_authorized and not user.is_banned else "sin autorización"
    return (
        f"ℹ️ @{user.username or 'sin username'} — ID {uid}\n"
        f"Estado: {state}\nRegistrado: {user.created_at.date() if user.created_at else '?'}"
    )


async def ensure_owner_record(db: AsyncSession) -> None:
    """Garantiza que el propietario exista como registro protegido."""
    if not settings.telegram_owner_id:
        return
    result = await db.execute(
        select(AppUser).where(AppUser.telegram_id == settings.telegram_owner_id)
    )
    owner = result.scalar_one_or_none()
    if owner is None:
        db.add(AppUser(
            telegram_id=settings.telegram_owner_id,
            is_owner=True,
            is_authorized=True,
        ))
    else:
        owner.is_owner = True
        owner.is_authorized = True
        owner.is_banned = False
    await db.flush()
