import asyncio
import sys
from pathlib import Path

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.config import settings
from app.utils.security import redact_message
from scripts.deploy_render import get_owner_id


async def main():
    owner = await get_owner_id()
    async with httpx.AsyncClient(timeout=60) as client:
        resp = await client.get(
            "https://api.render.com/v1/logs",
            params={
                "ownerId": owner,
                "resource": ["srv-dc0bef3ncjis739bkhq0"],
                "type": ["app"],
                "direction": "backward",
                "limit": "120",
            },
            headers={
                "Authorization": f"Bearer {settings.render_api_key}",
                "Accept": "application/json",
            },
        )
        print("status:", resp.status_code)
        data = resp.json()
        logs = data.get("logs", data) if isinstance(data, dict) else data
        for entry in logs[-60:]:
            if isinstance(entry, dict):
                text = entry.get("text") or entry.get("message") or ""
            else:
                text = str(entry)
            if text.strip():
                print(redact_message(str(text))[:300])


asyncio.run(main())
