import asyncio
import json
import sys
from pathlib import Path

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.config import settings

BASE = "https://cinefinder-api.onrender.com"
HEADERS = {"Authorization": f"Bearer {settings.api_access_token}"}


async def main():
    async with httpx.AsyncClient(timeout=30) as client:
        for _ in range(30):
            try:
                r = await client.get(f"{BASE}/api/v1/health")
                if r.status_code == 200:
                    print("Health:", r.status_code)
                    break
            except Exception:
                pass
            await asyncio.sleep(8)
        else:
            print("Servidor sin respuesta")
            return 1

        r = await client.post(
            f"{BASE}/api/v1/search", headers={**HEADERS, "Content-Type": "application/json"},
            json={"query": "película de viaje espacial con cartel", "async": True},
        )
        print("Search:", r.status_code)
        job_id = r.json()["job_id"]

        print("--- eventos SSE ---")
        async with client.stream("GET", f"{BASE}/api/v1/jobs/{job_id}/events", headers=HEADERS) as stream:
            count = 0
            async for line in stream.aiter_lines():
                if line.startswith("data: "):
                    event = json.loads(line[6:])
                    print(event.get("type"), "|", str(event.get("message"))[:110])
                    count += 1
                    if event.get("type") == "final" or count > 40:
                        break

        job = (await client.get(f"{BASE}/api/v1/jobs/{job_id}", headers=HEADERS)).json()
        result = job.get("result") or {}
        print("--- resultado ---")
        print("status:", job.get("status"), "| tools:", len(result.get("tools_called", [])),
              "| trace:", len(result.get("trace", [])))
        print("formatted:", str(result.get("formatted_answer"))[:500].replace("\n", " | "))
    return 0


raise SystemExit(asyncio.run(main()))
