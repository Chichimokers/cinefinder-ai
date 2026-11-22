#!/usr/bin/env python3
"""Autenticación del userbot por QR (alternativa al código OTP).

Telegram entrega el código SOLO dentro de la app oficial (SentCodeTypeApp),
nunca por SMS. Si no ves el código, este método evita escribirlo: genera un
QR que se escanea desde la propia app de Telegram.

Pasos en el teléfono:
  Telegram → Configuración (☰) → Dispositivos → Vincular dispositivo → + → escanear QR

Uso: py -3.12 scripts/authenticate_qr.py
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from telethon import TelegramClient
from telethon.errors import SessionPasswordNeededError
from telethon.sessions import StringSession

from app.config import settings
from scripts.setup_credentials import write_env


async def _print_qr(url: str) -> None:
    try:
        import qrcode

        qr = qrcode.QRCode(border=2, box_size=6)
        qr.add_data(url)
        qr.make(fit=True)
        print("\n=== ESCANEA ESTE QR CON TU APP DE TELEGRAM ===\n")
        qr.print_ascii(invert=True)
    except ImportError:
        print("\nURL de vinculación (ábrela en la app de Telegram):")
        print(url)
    print("\nEn Telegram: ☰ → Dispositivos → Vincular dispositivo → + → escanear")


async def main() -> int:
    if not (settings.telegram_api_id and settings.telegram_api_hash):
        print("Faltan TELEGRAM_API_ID o TELEGRAM_API_HASH.")
        return 1

    client = TelegramClient(StringSession(), settings.telegram_api_id, settings.telegram_api_hash)
    try:
        await client.connect()
        if await client.is_user_authorized():
            print("Ya hay una sesión autorizada en memoria; no hace falta QR.")
            me = await client.get_me()
            return await _finish(client, me)

        print("Solicitando código QR a Telegram…")
        login = await client.qr_login()
        await _print_qr(login.url)

        print("\nEsperando a que escanees el QR (hasta 120 s)…")
        await login.wait(timeout=120)

        try:
            me = await client.get_me()
        except SessionPasswordNeededError:
            import getpass

            password = getpass.getpass("Contraseña de verificación en dos pasos (oculta): ").strip()
            await client.sign_in(password=password)
            me = await client.get_me()

        return await _finish(client, me)
    except Exception as exc:  # noqa: BLE001
        print(f"Autenticación por QR fallida: {type(exc).__name__}")
        print("Si Telegram indicó 'espera', vuelve a intentarlo en unos minutos.")
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
