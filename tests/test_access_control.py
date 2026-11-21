import asyncio

from app.config import settings
from app.services.access_control import UNAUTHORIZED_MESSAGE, check_access, is_owner_id, list_authorized, user_info


def test_owner_is_absolute():
    assert is_owner_id(settings.telegram_owner_id)
    assert not is_owner_id(settings.telegram_owner_id + 1)
    assert not is_owner_id(None)


def test_unauthorized_message_is_clear():
    assert "No tienes autorización" in UNAUTHORIZED_MESSAGE


def test_owner_access_without_db():
    # No se necesita DB: el propietario se valida por ID configurado.
    result = asyncio.run(check_access(None, settings.telegram_owner_id))
    assert result["allowed"] is True
    assert result["owner"] is True


def test_non_owner_without_db_is_denied():
    result = asyncio.run(check_access(None, 999999999))
    assert result["allowed"] is False


def test_username_validation():
    from app.services.access_control import resolve_user

    async def run():
        return await resolve_user(None, "bad name!")

    result = asyncio.run(run())
    assert result["ok"] is False


def test_numeric_id_without_telegram_is_not_trusted():
    """Un ID escrito a mano no debe autorizarse sin verificación MTProto."""
    from app.services.access_control import resolve_user

    async def run():
        return await resolve_user(None, "999999999")

    result = asyncio.run(run())
    assert result["ok"] is False
    assert "verificar" in result["error"].lower() or "userbot" in result["error"].lower()
