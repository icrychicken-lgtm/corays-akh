"""In-Process Event-Bus: Bot-Ereignisse → WebSocket-Clients des Dashboards (live)."""
from __future__ import annotations

import asyncio
import logging
from collections import defaultdict
from typing import Any

log = logging.getLogger("nova.events")


class EventBus:
    def __init__(self) -> None:
        self._subs: dict[int, set[asyncio.Queue]] = defaultdict(set)  # guild_id (0 = global/owner)

    def subscribe(self, guild_id: int) -> asyncio.Queue:
        q: asyncio.Queue = asyncio.Queue(maxsize=200)
        self._subs[guild_id].add(q)
        return q

    def unsubscribe(self, guild_id: int, q: asyncio.Queue) -> None:
        self._subs[guild_id].discard(q)
        if not self._subs[guild_id]:
            self._subs.pop(guild_id, None)

    def has_subscribers(self, guild_id: int) -> bool:
        return bool(self._subs.get(guild_id))

    def subscribed_guilds(self) -> list[int]:
        return [g for g, s in self._subs.items() if s and g]

    def publish(self, guild_id: int | None, event: str, data: Any = None) -> None:
        targets = list(self._subs.get(guild_id or 0, ()))
        if guild_id:
            targets += list(self._subs.get(0, ())) if event in ("error", "guild") else []
        for q in targets:
            try:
                q.put_nowait({"event": event, "guild_id": str(guild_id or 0), "data": data})
            except asyncio.QueueFull:
                pass  # langsamer Client – Event verwerfen statt blockieren


bus = EventBus()
