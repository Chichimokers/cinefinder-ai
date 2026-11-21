#!/usr/bin/env python3
"""Registra un chat/canal de Telegram como fuente autorizada para entrega.

Uso:
    py -3.12 scripts\authorize_source.py -1001234567890 --name "Mi canal"
    py -3.12 scripts\authorize_source.py -1001234567890 --revoke
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sqlalchemy import select

from app.database.models import AuthorizedSource
from app.database.session import db_session


async def main() -> int:
    parser = argparse.ArgumentParser(description="Fuentes autorizadas de Telegram")
    parser.add_argument("chat_id", type=int, help="ID numérico del chat/canal")
    parser.add_argument("--name", default=None, help="Nombre descriptivo")
    parser.add_argument("--license", default="Uso personal del propietario", help="Licencia o base de autorización")
    parser.add_argument("--revoke", action="store_true", help="Marcar como no autorizada")
    args = parser.parse_args()

    url = f"tg://chat?id={args.chat_id}"
    async with db_session() as db:
        result = await db.execute(
            select(AuthorizedSource).where(
                AuthorizedSource.source_type == "telegram",
                AuthorizedSource.url == url,
            )
        )
        source = result.scalar_one_or_none()
        if source is None:
            source = AuthorizedSource(
                name=args.name or f"Telegram {args.chat_id}",
                source_type="telegram",
                url=url,
            )
            db.add(source)
        if args.name:
            source.name = args.name
        source.license = args.license
        source.authorized = not args.revoke
        await db.flush()

    action = "desautorizada" if args.revoke else "autorizada"
    print(f"Fuente de Telegram {args.chat_id} {action} correctamente.")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
