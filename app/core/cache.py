"""Cache-/Rate-Limit-Backend: Redis wenn konfiguriert, sonst In-Memory (Single-Process)."""
from __future__ import annotations

import json
import logging
import time
from typing import Any

from app.config import settings

log = logging.getLogger("nova.cache")


class MemoryBackend:
    def __init__(self) -> None:
        self._data: dict[str, tuple[float | None, Any]] = {}

    async def get(self, key: str) -> Any:
        item = self._data.get(key)
        if not item:
            return None
        exp, val = item
        if exp and exp < time.monotonic():
            self._data.pop(key, None)
            return None
        return val

    async def set(self, key: str, value: Any, ttl: float | None = None) -> None:
        self._data[key] = (time.monotonic() + ttl if ttl else None, value)
        if len(self._data) > 50_000:
            self._sweep()

    async def delete(self, key: str) -> None:
        self._data.pop(key, None)

    async def incr(self, key: str, ttl: float) -> int:
        cur = await self.get(key) or 0
        exp = self._data.get(key, (None, 0))[0] or time.monotonic() + ttl
        self._data[key] = (exp, cur + 1)
        return cur + 1

    def _sweep(self) -> None:
        now = time.monotonic()
        for k in [k for k, (e, _) in self._data.items() if e and e < now]:
            self._data.pop(k, None)

    async def ping(self) -> bool:
        return True

    name = "memory"


class RedisBackend:
    name = "redis"

    def __init__(self, url: str) -> None:
        import redis.asyncio as redis

        self._r = redis.from_url(url, decode_responses=True)

    async def get(self, key: str) -> Any:
        raw = await self._r.get(key)
        return json.loads(raw) if raw is not None else None

    async def set(self, key: str, value: Any, ttl: float | None = None) -> None:
        await self._r.set(key, json.dumps(value), ex=int(ttl) if ttl else None)

    async def delete(self, key: str) -> None:
        await self._r.delete(key)

    async def incr(self, key: str, ttl: float) -> int:
        pipe = self._r.pipeline()
        pipe.incr(key)
        pipe.expire(key, int(ttl), nx=True)
        val, _ = await pipe.execute()
        return int(val)

    async def ping(self) -> bool:
        try:
            return bool(await self._r.ping())
        except Exception:
            return False


def _make() -> MemoryBackend | RedisBackend:
    if settings.redis_url:
        try:
            return RedisBackend(settings.redis_url)
        except Exception as exc:  # pragma: no cover
            log.error("Redis nicht verfügbar (%s) – verwende In-Memory-Cache.", exc)
    return MemoryBackend()


cache = _make()
