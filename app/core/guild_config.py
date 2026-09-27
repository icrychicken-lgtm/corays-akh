"""Einstellungen & Feature-Toggles pro Server (DB + In-Memory-Cache)."""
from __future__ import annotations

import asyncio
import logging
from typing import Any

from sqlalchemy import select

from app.core.schema import MODULE_MAP
from app.db.base import SessionLocal, session_scope
from app.db.models import BotState, GuildModule

log = logging.getLogger("nova.config")


class ModuleConfig(dict):
    """Dict mit Komfort-Zugriff: cfg.id('log_channel') → int | None, cfg.ids('roles') → list[int]."""

    enabled: bool = True

    def id(self, key: str) -> int | None:
        v = self.get(key)
        return int(v) if v not in (None, "") and str(v).isdigit() else None

    def ids(self, key: str) -> list[int]:
        return [int(x) for x in (self.get(key) or []) if str(x).isdigit()]


class GuildConfigService:
    def __init__(self) -> None:
        self._cache: dict[int, dict[str, tuple[bool, dict[str, Any]]]] = {}
        self._locks: dict[int, asyncio.Lock] = {}
        self._state: dict[str, Any] = {}

    async def _load(self, guild_id: int) -> dict[str, tuple[bool, dict[str, Any]]]:
        cached = self._cache.get(guild_id)
        if cached is not None:
            return cached
        lock = self._locks.setdefault(guild_id, asyncio.Lock())
        async with lock:
            if guild_id in self._cache:
                return self._cache[guild_id]
            async with SessionLocal() as s:
                rows = (await s.execute(select(GuildModule).where(GuildModule.guild_id == guild_id))).scalars().all()
            data = {r.module: (r.enabled, r.settings or {}) for r in rows}
            self._cache[guild_id] = data
            return data

    async def get(self, guild_id: int, module: str) -> ModuleConfig:
        spec = MODULE_MAP[module]
        data = await self._load(guild_id)
        enabled, stored = data.get(module, (spec.default_enabled, {}))
        merged = spec.defaults()
        merged.update({k: v for k, v in stored.items() if k in merged})
        cfg = ModuleConfig(merged)
        cfg.enabled = enabled if spec.toggleable else True
        return cfg

    async def enabled(self, guild_id: int, module: str) -> bool:
        spec = MODULE_MAP.get(module)
        if spec is None or not spec.toggleable:
            return True
        data = await self._load(guild_id)
        return data.get(module, (spec.default_enabled, {}))[0]

    async def all_toggles(self, guild_id: int) -> dict[str, bool]:
        data = await self._load(guild_id)
        return {k: (data.get(k, (s.default_enabled, {}))[0] if s.toggleable else True) for k, s in MODULE_MAP.items()}

    async def save(self, guild_id: int, module: str, *, settings: dict[str, Any] | None = None, enabled: bool | None = None) -> None:
        spec = MODULE_MAP[module]
        async with session_scope() as s:
            row = await s.get(GuildModule, (guild_id, module))
            if row is None:
                row = GuildModule(guild_id=guild_id, module=module, enabled=spec.default_enabled, settings={})
                s.add(row)
            if settings is not None:
                row.settings = settings
            if enabled is not None and spec.toggleable:
                row.enabled = enabled
        self._cache.pop(guild_id, None)

    def invalidate(self, guild_id: int) -> None:
        self._cache.pop(guild_id, None)

    # ── globaler Bot-Zustand ──
    async def state(self, key: str, default: Any = None) -> Any:
        if key in self._state:
            return self._state[key]
        async with SessionLocal() as s:
            row = await s.get(BotState, key)
        val = row.value if row else default
        self._state[key] = val
        return val

    async def set_state(self, key: str, value: Any) -> None:
        async with session_scope() as s:
            row = await s.get(BotState, key)
            if row:
                row.value = value
            else:
                s.add(BotState(key=key, value=value))
        self._state[key] = value


config = GuildConfigService()
