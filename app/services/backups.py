"""Backups: pro Server als komprimiertes JSON (Export/Restore) + vollständige DB-Backups (pg_dump / SQLite-Kopie)."""
from __future__ import annotations

import asyncio
import gzip
import json
import logging
import os
import shutil
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from sqlalchemy import delete, insert, select, text

from app.config import settings
from app.db import models as m
from app.db.base import SessionLocal, UTCDateTime, engine, session_scope, utcnow

log = logging.getLogger("nova.backups")
VERSION = 1

# (Model, Filter-Art) – Reihenfolge = Einfüge-Reihenfolge (Eltern vor Kindern)
GUILD_TABLES: list[tuple[type, str]] = [
    (m.GuildModule, "guild"), (m.CommandPermission, "guild"), (m.Integration, "guild"), (m.Member, "guild"),
    (m.UserNote, "guild"), (m.StaffNote, "guild"), (m.ModCase, "guild"), (m.TicketCategory, "guild"), (m.Ticket, "guild"),
    (m.TicketNote, "ticket"), (m.Application, "guild"), (m.Giveaway, "guild"), (m.GiveawayEntry, "giveaway"),
    (m.LevelReward, "guild"), (m.ShopItem, "guild"), (m.InventoryItem, "guild"), (m.Quest, "guild"), (m.QuestProgress, "guild"),
    (m.Badge, "guild"), (m.MemberBadge, "guild"), (m.MemberAchievement, "guild"), (m.RoleMenu, "guild"),
    (m.Suggestion, "guild"), (m.SuggestionVote, "suggestion"), (m.Poll, "guild"), (m.PollVote, "poll"),
    (m.Reminder, "guild"), (m.CustomCommand, "guild"), (m.AutoResponder, "guild"), (m.StarboardEntry, "guild"),
    (m.ServerEvent, "guild"), (m.EventParticipant, "event"), (m.Streamer, "guild"), (m.StreamSession, "guild"),
    (m.StreamSnapshot, "streamer"), (m.StreamCheckin, "session"), (m.StatBucket, "guild"), (m.RaidEvent, "guild"),
    (m.Gang, "guild"), (m.GangMember, "guild"),
]
PARENT = {"ticket": (m.Ticket, "ticket_id"), "giveaway": (m.Giveaway, "giveaway_id"), "suggestion": (m.Suggestion, "suggestion_id"),
          "poll": (m.Poll, "poll_id"), "event": (m.ServerEvent, "event_id"), "streamer": (m.Streamer, "streamer_id"),
          "session": (m.StreamSession, "session_id")}


def backup_dir() -> Path:
    p = settings.path(settings.backup_dir)
    p.mkdir(parents=True, exist_ok=True)
    return p


def _filter(model: type, kind: str, guild_id: int):
    if kind == "guild":
        return model.guild_id == guild_id
    parent, fk = PARENT[kind]
    return getattr(model, fk).in_(select(parent.id).where(parent.guild_id == guild_id))


def _ser(v: Any) -> Any:
    if isinstance(v, datetime):
        return {"__dt__": v.astimezone(timezone.utc).isoformat()}
    return v


def _deser(v: Any) -> Any:
    if isinstance(v, dict) and "__dt__" in v:
        return datetime.fromisoformat(v["__dt__"])
    return v


async def export_guild(guild_id: int, *, created_by: int | None = None, auto: bool = False) -> m.Backup:
    data: dict[str, Any] = {"version": VERSION, "guild_id": str(guild_id), "created_at": utcnow().isoformat(), "tables": {}}
    async with SessionLocal() as db:
        g = await db.get(m.Guild, guild_id)
        if g:
            data["guild"] = {"next_case": g.next_case, "next_ticket": g.next_ticket, "next_suggestion": g.next_suggestion}
        for model, kind in GUILD_TABLES:
            rows = (await db.execute(select(model.__table__).where(_filter(model, kind, guild_id)))).mappings().all()
            data["tables"][model.__tablename__] = [{k: _ser(v) for k, v in r.items()} for r in rows]
    stamp = utcnow().strftime("%Y%m%d-%H%M%S")
    filename = f"guild_{guild_id}_{stamp}{'_auto' if auto else ''}.json.gz"
    path = backup_dir() / filename
    raw = json.dumps(data, default=str).encode()
    await asyncio.to_thread(path.write_bytes, gzip.compress(raw, 6))
    async with session_scope() as db:
        b = m.Backup(guild_id=guild_id, filename=filename, size=path.stat().st_size, kind="guild", auto=auto, created_by=created_by)
        db.add(b)
        await db.flush()
    log.info("Backup erstellt: %s (%d KB)", filename, path.stat().st_size // 1024)
    return b


async def restore_guild(guild_id: int, backup_id: int) -> dict[str, int]:
    async with SessionLocal() as db:
        b = await db.get(m.Backup, backup_id)
    if b is None or b.guild_id != guild_id or b.kind != "guild":
        raise ValueError("Backup nicht gefunden")
    path = backup_dir() / b.filename
    data = json.loads(gzip.decompress(await asyncio.to_thread(path.read_bytes)))
    if str(data.get("guild_id")) != str(guild_id):
        raise ValueError("Backup gehört zu einem anderen Server")
    counts: dict[str, int] = {}
    async with session_scope() as db:
        for model, kind in reversed(GUILD_TABLES):
            await db.execute(delete(model).where(_filter(model, kind, guild_id)).execution_options(synchronize_session=False))
        for model, _kind in GUILD_TABLES:
            rows = data["tables"].get(model.__tablename__) or []
            cols = {c.key: c for c in model.__table__.columns}
            clean = []
            for r in rows:
                row = {}
                for k, v in r.items():
                    if k not in cols:
                        continue
                    v = _deser(v)
                    if isinstance(cols[k].type, UTCDateTime) and isinstance(v, str):
                        v = datetime.fromisoformat(v)
                    row[k] = v
                clean.append(row)
            for i in range(0, len(clean), 500):
                await db.execute(insert(model.__table__), clean[i:i + 500])
            counts[model.__tablename__] = len(clean)
        g = await db.get(m.Guild, guild_id)
        if g and data.get("guild"):
            g.next_case = max(g.next_case, data["guild"]["next_case"])
            g.next_ticket = max(g.next_ticket, data["guild"]["next_ticket"])
            g.next_suggestion = max(g.next_suggestion, data["guild"]["next_suggestion"])
    await _fix_sequences()
    return counts


async def _fix_sequences() -> None:
    if engine.dialect.name != "postgresql":
        return
    async with engine.begin() as conn:
        for model, _k in GUILD_TABLES:
            col = model.__table__.columns.get("id")
            if col is None or not col.autoincrement or not col.primary_key:
                continue
            t = model.__tablename__
            await conn.execute(text(
                f"SELECT setval(pg_get_serial_sequence('{t}', 'id'), COALESCE((SELECT MAX(id) FROM {t}), 1), (SELECT MAX(id) IS NOT NULL FROM {t}))"))


async def full_backup(created_by: int | None = None, auto: bool = False) -> m.Backup:
    stamp = utcnow().strftime("%Y%m%d-%H%M%S")
    if engine.dialect.name == "postgresql":
        pg_dump = shutil.which("pg_dump")
        if not pg_dump:
            raise RuntimeError("pg_dump nicht gefunden (postgresql-client installieren)")
        filename = f"full_{stamp}.dump"
        url = settings.database_url.replace("+asyncpg", "")
        proc = await asyncio.create_subprocess_exec(pg_dump, "--format=custom", f"--file={backup_dir() / filename}", f"--dbname={url}",
                                                    stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
        _out, err = await proc.communicate()
        if proc.returncode != 0:
            raise RuntimeError(f"pg_dump fehlgeschlagen: {err.decode()[:300]}")
    else:
        filename = f"full_{stamp}.sqlite"
        src = settings.database_url.split("///", 1)[1]

        def copy():
            with sqlite3.connect(src) as s, sqlite3.connect(backup_dir() / filename) as d:
                s.backup(d)

        await asyncio.to_thread(copy)
    size = (backup_dir() / filename).stat().st_size
    async with session_scope() as db:
        b = m.Backup(guild_id=None, filename=filename, size=size, kind="full", auto=auto, created_by=created_by)
        db.add(b)
        await db.flush()
    return b


async def delete_backup(b: m.Backup) -> None:
    try:
        os.remove(backup_dir() / b.filename)
    except FileNotFoundError:
        pass
    async with session_scope() as db:
        row = await db.get(m.Backup, b.id)
        if row:
            await db.delete(row)


async def prune(guild_id: int | None, keep: int) -> None:
    async with SessionLocal() as db:
        rows = (await db.execute(select(m.Backup).where(m.Backup.guild_id.is_(None) if guild_id is None else m.Backup.guild_id == guild_id,
                                                        m.Backup.auto.is_(True)).order_by(m.Backup.created_at.desc()))).scalars().all()
    for b in rows[keep:]:
        await delete_backup(b)
