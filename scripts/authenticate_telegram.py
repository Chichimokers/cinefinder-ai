#!/usr/bin/env python3
"""Autenticación local de Telegram (userbot) con Telethon.

- Usa el número configurado en TELEGRAM_PHONE (+5358126024 por defecto).
- Solicita el código y explica que Telegram lo entrega DENTRO de la app oficial.
- OTP y 2FA por entrada oculta; nunca se imprimen ni se guardan en texto plano.
- Si el ID autenticado difiere del propietario, ofrece actualizarlo.
- Guarda la StringSession en .env.local con permisos restringidos.

Uso:
    py -3.12 scripts/authenticate_telegram.py
    py -3.12 scripts/authenticate_telegram.py --force-sms   # (obsoleto, se ignora)
"""

from __future__ import annotations

import argparse
import asyncio
import getpass
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from telethon import TelegramClient
from telethon.errors import SessionPasswordNeededError
from telethon.sessions import StringSession

from app.config import settings
from scripts.setup_credentials import write_env


def _hidden_input(prompt: str) -> str:
    try:
        return getpass.getpass(prompt).strip()
    except (EOFError, KeyboardInterrupt):
        print("\nSe requiere un terminal local seguro para introducir este dato.")
        raise SystemExit(2)


async def main() -> int:
    parser = argparse.ArgumentParser(description="Autenticación local del userbot de Telegram")
    parser.add_argument(
        "--force-sms", action="store_true",
        help="Obsoleto: Telegram ya no entrega códigos por SMS, solo por app.",
    )
    parser.add_argument(
        "--phone", default=None,
        help="Número a usar (por defecto TELEGRAM_PHONE, actualmente +5358126024).",
    )
    args = parser.parse_args()

    if not (settings.telegram_api_id and settings.telegram_api_hash):
        print("Faltan TELEGRAM_API_ID o TELEGRAM_API_HASH.")
        return 1

    phone = (args.phone or settings.telegram_phone or "").strip()
    if not phone:
        phone = input("Teléfono (formato internacional, ej. +5358126024): ").strip()

    # Normalizar: si el usuario escribe solo los 8 dígitos locales, añadir +53.
    digits = "".join(c for c in phone if c.isdigit())
    if len(digits) == 8 and not phone.startswith("+"):
        phone = "+53" + digits
    elif not phone.startswith("+"):
        phone = "+" + digits
    print(f"Número a autenticar: {phone}")

    session = StringSession()
    client = TelegramClient(session, settings.telegram_api_id, settings.telegram_api_hash)

    try:
        await client.connect()
        if await client.is_user_authorized():
            print("La sesión nueva ya está autorizada.")
            me = await client.get_me()
            return await _finish(client, me, phone)

        print("\nSolicitando código a Telegram…")
        sent = await client.send_code_request(phone)
        kind = type(getattr(sent, "type", sent)).__name__
        print(f"Telegram entregó el código por: {kind}")
        print("\n⚠️  Telegram YA NO envía el código por SMS. El código de 5 dígitos")
        print("    aparece DENTRO de la app oficial de Telegram en el teléfono donde")
        print("    esté iniciada la cuenta " + phone + ":")
        print("    • abre la app de Telegram → chat de servicio «Telegram»")
        print("    • o Mensajes Guardados / notificaciones de esa cuenta")
        print("    • si tienes otra cuenta abierta en el PC, NO verás este código")
        print("    • espera 1-2 min; si pediste varios, solo sirve el último\n")

        code = _hidden_input("Código OTP de 5 dígitos (oculto; Enter para reenviar): ")
        if not code:
            sent = await client.send_code_request(phone)
            print(f"Código reenviado por {type(getattr(sent, 'type', sent)).__name__}.")
            code = _hidden_input("Código OTP (oculto): ")

        try:
            await client.sign_in(phone=phone, code=code)
        except SessionPasswordNeededError:
            password = _hidden_input("Contraseña de verificación en dos pasos (oculta): ")
            await client.sign_in(password=password)

        me = await client.get_me()
        return await _finish(client, me, phone)
    except Exception as exc:  # noqa: BLE001
        print(f"Autenticación fallida: {type(exc).__name__}. Revisa teléfono/OTP/2FA.")
        return 1
    finally:
        if client.is_connected():
            await client.disconnect()


async def _finish(client: TelegramClient, me, phone: str) -> int:
    if me is None:
        print("No se pudo obtener la identidad de la cuenta.")
        return 1

    owner_id = settings.telegram_owner_id
    if owner_id and me.id != owner_id:
        print(f"La cuenta autenticada tiene ID {me.id}, distinto de TELEGRAM_OWNER_ID={owner_id}.")
        answer = input("¿Establecer este ID como propietario? [s/n]: ").strip().lower()
        if answer in ("s", "si", "sí", "y", "yes"):
            owner_id = me.id
        else:
            print("Autenticación cancelada: el ID no coincide con el propietario.")
            return 1

    string_session = client.session.save()
    data = {"TELEGRAM_SESSION": string_session, "TELEGRAM_PHONE": phone}
    if owner_id:
        data["TELEGRAM_OWNER_ID"] = str(owner_id)
    write_env(data)

    # Validar la sesión recién guardada sin imprimir su contenido.
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
    print(f"Cuenta autenticada: ID {me.id} (número {phone})")
    return 0 if valid else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
