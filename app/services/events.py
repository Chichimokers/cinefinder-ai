"""Bus de eventos en memoria para mostrar el razonamiento del agente en vivo."""

from __future__ import annotations

import asyncio
import json
from collections import defaultdict, deque
from typing import Any

_subscribers: defaultdict[str, list[asyncio.Queue]] = defaultdict(list)
_history: defaultdict[str, deque[dict]] = defaultdict(lambda: deque(maxlen=300))


async def publish(job_id: str, event: dict[str, Any]) -> None:
    entry = {
        "type": event.get("type", "info"),
        "step": event.get("step"),
        "message": event.get("message", ""),
        "data": event.get("data"),
    }
    _history[job_id].append(entry)
    for queue in list(_subscribers[job_id]):
        try:
            queue.put_nowait(entry)
        except asyncio.QueueFull:
            pass


def history(job_id: str) -> list[dict]:
    return list(_history[job_id])


def subscribe(job_id: str) -> asyncio.Queue:
    queue: asyncio.Queue = asyncio.Queue(maxsize=300)
    for event in history(job_id):
        try:
            queue.put_nowait(event)
        except asyncio.QueueFull:
            break
    _subscribers[job_id].append(queue)
    return queue


def unsubscribe(job_id: str, queue: asyncio.Queue) -> None:
    if queue in _subscribers[job_id]:
        _subscribers[job_id].remove(queue)


def sse(event: dict) -> str:
    return f"data: {json.dumps(event, ensure_ascii=False)}\n\n"
