#!/usr/bin/env python3
"""Autenticación del userbot por QR con renovación automática del token.

Telegram caduca el token QR en ~30-60 s. Este script muestra el QR y lo
REGENERA automáticamente antes de que expire, para que puedas escanearlo
con calma desde la app oficial de Telegram.

En el teléfono:
  Telegram → ☰ → Dispositivos → Vincular dispositivo → + → escanear el QR

Uso: py -3.12 scripts/authenticate_qr.py
"""

from __future__ import annotations

import asyncio
import sys
from datetime import datetime, timezone

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from telethon import TelegramClient
from telethon.errors import SessionPasswordNeededError
from telethon.sessions import StringSession

from app.config import settings
from scripts.setup_credentials import write_env


def _print_qr(url: str, attempt: int) -> None:
    print("\n" + "=" * 60)
    print(f"QR #{attempt} — escanéalo AHORA con tu app de Telegram")
    print("=" * 60)
    try:
        import qrcode

        qr = qrcode.QRCode(border=2, box_size=6)
        qr.add_data(url)
        qr.make(fit=True)
        qr.print_ascii(invert=True)
    except ImportError:
        print("URL de vinculación (ábrela en la app de Telegram):")
        print(url)
    print("\nTelegram → ☰ → Dispositivos → Vincular dispositivo → + → escanear")
    print("(si expira, aparecerá uno nuevo automáticamente)")


async def _wait_scan(login, timeout: int = 25) -> bool:
    """Espera el escaneo con un timeout corto para poder renovar el QR."""
    try:
        await login.wait(timeout=timeout)
        return True
    except asyncio.TimeoutError:
        return False


async def main() -> int:
    if not (settings.telegram_api_id and settings.telegram_api_hash):
        print("Faltan TELEGRAM_API_ID o TELEGRAM_API_HASH.")
        return 1

    client = TelegramClient(StringSession(), settings.telegram_api_id, settings.telegram_api_hash)
    try:
        await client.connect()
        if await client.is_user_authorized():
            print("Ya hay una sesión autorizada en memoria.")
            return await _finish(client, await client.get_me())

        login = await client.qr_login()
        attempt = 0
        scanned = False

        # Renovar el QR en bucle hasta que se escanee (máx. 6 minutos).
        deadline = asyncio.get_event_loop().time() + 360
        while asyncio.get_event_loop().time() < deadline:
            attempt += 1
            _print_qr(login.url, attempt)
            expires = getattr(login, "expires", None)
            if expires:
                remaining = (expires - datetime.now(timezone.utc)).total_seconds()
                wait_for = max(5, min(int(remaining) - 3, 25))
            else:
                wait_for = 25
            print(f"\nEste QR caduca en ~{int(remaining) if expires else 30}s. Esperando escaneo…")
            scanned = await _wait_scan(login, timeout=wait_for)
            if scanned:
                break
            print("\nQR caducado o sin escanear; generando uno nuevo…")
            try:
                await login.recreate()
            except Exception:  # noqa: BLE001
                login = await client.qr_login()

        if not scanned:
            print("No se detectó ningún escaneo. Vuelve a ejecutar el script.")
            return 1

        try:
            me = await client.get_me()
        except SessionPasswordNeededError:
            import getpass

            password = getpass.getpass("Contraseña de verificación en dos pasos (oculta): ").strip()
            await client.sign_in(password=password)
            me = await client.get_me()

        return await _finish(client, me)
    except Exception as exc:  # noqa: BLE001
        print(f"Autenticación por QR fallida: {type(exc).__name__} — {str(exc)[:120]}")
        return 1
    finally:
        if client.is_connected():
            await client.disconnect()


async def _finish(client: TelegramClient, me) -> int:
    if me is None:
        print("No se pudo obtener la identidad de la cuenta.")
        return 1

    owner_id = settings.telegram_owner_id
    if owner_id and me.id != owner_id:
        print(f"La cuenta vinculada tiene ID {me.id}, distinto de TELEGRAM_OWNER_ID={owner_id}.")
        answer = input("¿Establecer este ID como propietario? [s/n]: ").strip().lower()
        if answer in ("s", "si", "sí", "y", "yes"):
            owner_id = me.id
        else:
            print("Cancelado: el ID no coincide con el propietario.")
            return 1

    string_session = client.session.save()
    data = {"TELEGRAM_SESSION": string_session}
    if owner_id:
        data["TELEGRAM_OWNER_ID"] = str(owner_id)
    write_env(data)

    check = TelegramClient(
        StringSession(string_session), settings.telegram_api_id, settings.telegram_api_hash
    )
    try:
        await check.connect()
        valid = await check.is_user_authorized()
    finally:
        if check.is_connected():
            await check.disconnect()

    print(f"Sesión generada y almacenada de forma segura. Válida: {valid}")
    print(f"Cuenta vinculada: ID {me.id}")
    return 0 if valid else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
