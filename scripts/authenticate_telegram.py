#!/usr/bin/env python3
"""Autenticación local de Telegram (userbot) con Telethon.

- Pide teléfono, OTP y 2FA por entrada oculta cuando es posible.
- Nunca escribe OTP, contraseñas ni la sesión en logs.
- Valida la identidad contra TELEGRAM_OWNER_ID.
- Guarda el StringSession en .env.local.

Uso: py -3.12 scripts/authenticate_telegram.py
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
        help="Fuerza el envío del código por SMS en lugar de la app de Telegram",
    )
    args = parser.parse_args()

    if not (settings.telegram_api_id and settings.telegram_api_hash):
        print("Faltan TELEGRAM_API_ID o TELEGRAM_API_HASH.")
        return 1

    phone = settings.telegram_phone or input("Teléfono (formato internacional): ").strip()
    session = StringSession()
    client = TelegramClient(session, settings.telegram_api_id, settings.telegram_api_hash)

    try:
        await client.connect()
        if await client.is_user_authorized():
            print("La sesión guardada ya está autorizada; no es necesario autenticar de nuevo.")
            me = await client.get_me()
            if settings.telegram_owner_id and me.id != settings.telegram_owner_id:
                print("La cuenta autenticada no coincide con TELEGRAM_OWNER_ID.")
                return 1
            return 0

        await client.send_code_request(phone, force_sms=args.force_sms)
        print("Código solicitado. Si no llegó por SMS, BUSCA en la app de Telegram:")
        print("  • el chat de servicio «Telegram» (mensaje de inicio de sesión)")
        print("  • Mensajes Guardados / notificaciones de la cuenta +5358126024")
        print("  • espera 1-2 minutos; si solicitaste varios códigos, solo sirve el último.")
        code = _hidden_input("Código OTP recibido en Telegram (oculto; Enter para reenviar): ")
        if not code:
            # Reenviar forzando SMS para el caso de que la app no muestre el código.
            await client.send_code_request(phone, force_sms=True)
            print("Código reenviado por SMS. Introdúcelo ahora.")
            code = _hidden_input("Código OTP (oculto): ")
        try:
            await client.sign_in(phone=phone, code=code)
        except SessionPasswordNeededError:
            password = _hidden_input("Contraseña de verificación en dos pasos (oculta): ")
            await client.sign_in(password=password)

        me = await client.get_me()
        if me is None:
            print("No se pudo obtener la identidad de la cuenta.")
            return 1

        # Al usar un número distinto, el Telegram User ID cambia. Si el ID
        # autenticado no coincide con el configurado, se ofrece actualizarlo.
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
        data = {"TELEGRAM_SESSION": string_session}
        if owner_id:
            data["TELEGRAM_OWNER_ID"] = str(owner_id)
        write_env(data)
        await client.disconnect()

        # Validación de la sesión guardada (sin imprimir su contenido).
        check = TelegramClient(
            StringSession(string_session), settings.telegram_api_id, settings.telegram_api_hash
        )
        await check.connect()
        valid = await check.is_user_authorized()
        await check.disconnect()
        print(f"Sesión generada y almacenada de forma segura. Válida: {valid}")
        print(f"Cuenta autenticada: ID {me.id} (número {phone})")
        return 0 if valid else 1
    except Exception as exc:  # noqa: BLE001
        print(f"Autenticación fallida: {type(exc).__name__}. Revisa teléfono/OTP/2FA.")
        return 1
    finally:
        if client.is_connected():
            await client.disconnect()


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
