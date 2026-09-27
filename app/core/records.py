"""Schreibt Audit-Log, Event-Logs und Fehler in die DB und pusht sie live ans Dashboard."""
from __future__ import annotations

import logging
import secrets
import traceback
from typing import Any

from app.core.events import bus
from app.db.base import session_scope
from app.db.models import AuditEntry, ErrorLog, LogEntry

log = logging.getLogger("nova.records")


async def audit(guild_id: int | None, actor_id: int | None, actor_name: str, action: str, target: str = "",
                details: dict[str, Any] | None = None, source: str = "dashboard") -> None:
    try:
        async with session_scope() as s:
            entry = AuditEntry(guild_id=guild_id, actor_id=actor_id, actor_name=actor_name[:100], action=action[:60],
                               target=str(target)[:200], details=details or {}, source=source)
            s.add(entry)
            await s.flush()
            payload = entry.to_dict()
        bus.publish(guild_id, "audit", payload)
    except Exception:
        log.exception("Audit-Eintrag fehlgeschlagen")


async def log_event(guild_id: int, category: str, action: str, *, user: Any = None, target_id: int | None = None,
                    channel_id: int | None = None, content: str = "", details: dict[str, Any] | None = None) -> None:
    try:
        async with session_scope() as s:
            entry = LogEntry(
                guild_id=guild_id, category=category, action=action,
                user_id=getattr(user, "id", None), user_name=str(user)[:100] if user else None,
                target_id=target_id, channel_id=channel_id, content=(content or "")[:4000], details=details or {},
            )
            s.add(entry)
            await s.flush()
            payload = entry.to_dict()
        bus.publish(guild_id, "log", payload)
    except Exception:
        log.exception("Log-Eintrag fehlgeschlagen")


async def capture_error(exc: BaseException, *, guild_id: int | None = None, user_id: int | None = None,
                        command: str | None = None) -> str:
    err_id = secrets.token_hex(4).upper()
    tb = "".join(traceback.format_exception(type(exc), exc, exc.__traceback__))
    log.error("Fehler %s in %s: %s\n%s", err_id, command, exc, tb)
    try:
        async with session_scope() as s:
            s.add(ErrorLog(id=err_id, guild_id=guild_id, user_id=user_id, command=(command or "")[:100],
                           error_type=type(exc).__name__[:120], message=str(exc)[:4000], traceback=tb[-20000:]))
        bus.publish(guild_id, "error", {"id": err_id, "command": command, "type": type(exc).__name__})
    except Exception:
        log.exception("Fehler konnte nicht gespeichert werden")
    return err_id
