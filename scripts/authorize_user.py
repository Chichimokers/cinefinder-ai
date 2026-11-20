#!/usr/bin/env python3
"""Autoriza o revoca a un usuario de Telegram en el catálogo.

Uso:
    py -3.12 scripts\authorize_user.py 123456789 --username nombre
    py -3.12 scripts\authorize_user.py 123456789 --revoke
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sqlalchemy import select

from app.database.models import AppUser
from app.database.session import db_session


async def main() -> int:
    parser = argparse.ArgumentParser(description="Gestiona usuarios autorizados de CineFinder AI")
    parser.add_argument("telegram_id", type=int, help="ID numérico de Telegram")
    parser.add_argument("--username", default=None, help="Usuario de Telegram (opcional)")
    parser.add_argument("--revoke", action="store_true", help="Revocar autorización")
    args = parser.parse_args()

    async with db_session() as db:
        result = await db.execute(
            select(AppUser).where(AppUser.telegram_id == args.telegram_id)
        )
        user = result.scalar_one_or_none()
        if user is None:
            user = AppUser(telegram_id=args.telegram_id)
            db.add(user)
        if args.username:
            user.username = args.username
        user.is_authorized = not args.revoke
        user.is_banned = False
        await db.flush()

    action = "desautorizado" if args.revoke else "autorizado"
    print(f"Usuario {args.telegram_id} {action} correctamente.")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
