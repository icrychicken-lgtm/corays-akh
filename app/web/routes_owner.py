"""Owner-Panel: alle Server, globale Stats, Fehler, API-/DB-Status, Maintenance, Neustart, Server verlassen, Ankündigungen."""
from __future__ import annotations

import asyncio
import logging
import os
import sys
import time

import httpx
from fastapi import APIRouter, Body, Depends, HTTPException, Query
from fastapi.responses import FileResponse
from sqlalchemy import func, or_, select, text

from app.config import settings
from app.core.cache import cache
from app.core.records import audit
from app.db.base import SessionLocal, engine, session_scope
from app.db.models import AuditEntry, Backup, ErrorLog, LogEntry, Member, Streamer
from app.runtime import get_bot
from app.services import backups
from app.services.integrations import get_credentials
from app.web.common import bot_status, paginate, user_json
from app.web.deps import require_owner

log = logging.getLogger("nova.owner")
router = APIRouter(prefix="/api/owner", dependencies=[Depends(require_owner)])


@router.get("/overview")
async def overview():
    bot = get_bot()
    t0 = time.perf_counter()
    db_ok, db_version = True, ""
    try:
        async with engine.connect() as conn:
            db_version = str((await conn.execute(text("SELECT sqlite_version()" if engine.dialect.name == "sqlite" else "SELECT version()"))).scalar())[:80]
    except Exception as exc:  # noqa: BLE001
        db_ok, db_version = False, str(exc)[:120]
    db_ms = round((time.perf_counter() - t0) * 1000, 1)
    async with SessionLocal() as db:
        errors_open = (await db.execute(select(func.count()).select_from(ErrorLog).where(ErrorLog.resolved.is_(False)))).scalar_one()
        profiles = (await db.execute(select(func.count()).select_from(Member))).scalar_one()
        streamers = (await db.execute(select(func.count()).select_from(Streamer))).scalar_one()
    shards = [{"id": sid, "latency": round(sh.latency * 1000) if sh.latency == sh.latency else None, "closed": sh.is_closed()}
              for sid, sh in bot.shards.items()] if hasattr(bot, "shards") else []
    return {"bot": bot_status(bot), "shards": shards, "database": {"ok": db_ok, "dialect": engine.dialect.name, "version": db_version, "ping_ms": db_ms},
            "cache": {"backend": cache.name, "ok": await cache.ping()}, "errors_open": errors_open, "profiles": profiles, "streamers": streamers,
            "python": sys.version.split()[0], "pid": os.getpid()}


@router.get("/api-status")
async def api_status():
    results = {}
    async with httpx.AsyncClient(timeout=6) as c:
        for name, url in {"discord": "https://discord.com/api/v10/gateway", "twitch": "https://id.twitch.tv/oauth2/keys",
                          "youtube": "https://www.youtube.com/feeds/videos.xml?channel_id=UC_x5XG1OV2P6uZZ5FSM9Ttw",
                          "kick": "https://api.kick.com/public/v1/categories?q=a", "spotify": "https://accounts.spotify.com/"}.items():
            t0 = time.perf_counter()
            try:
                r = await c.get(url)
                results[name] = {"reachable": r.status_code < 500, "status": r.status_code, "ms": round((time.perf_counter() - t0) * 1000)}
            except httpx.HTTPError as exc:
                results[name] = {"reachable": False, "error": type(exc).__name__}
    for provider in ("twitch", "youtube", "kick", "spotify"):
        creds = await get_credentials(None, provider)
        results[provider]["global_credentials"] = all(creds.values())
    return results


@router.get("/guilds")
async def guilds(q: str = ""):
    bot = get_bot()
    items = []
    for g in bot.guilds:
        if q and q.lower() not in g.name.lower() and q != str(g.id):
            continue
        items.append({"id": str(g.id), "name": g.name, "icon": g.icon.url if g.icon else None, "members": g.member_count,
                      "owner": user_json(g.owner, g.owner_id), "joined_at": g.me.joined_at.isoformat() if g.me.joined_at else None,
                      "shard": g.shard_id, "boosts": g.premium_subscription_count})
    return {"items": sorted(items, key=lambda x: -(x["members"] or 0))}


@router.post("/guilds/{guild_id}/leave")
async def leave_guild(guild_id: int, body: dict = Body(default={}), session=Depends(require_owner)):
    g = get_bot().get_guild(guild_id)
    if g is None:
        raise HTTPException(404, "Server nicht gefunden")
    if body.get("confirm") != g.name:
        raise HTTPException(422, "Zur Bestätigung den exakten Servernamen angeben")
    await audit(None, session.user_id, session.username, "owner.leave_guild", f"{g.name} ({g.id})")
    await g.leave()
    return {"ok": True}


@router.post("/maintenance")
async def maintenance(body: dict = Body(...), session=Depends(require_owner)):
    enabled = bool(body.get("enabled"))
    await get_bot().set_maintenance(enabled)
    await audit(None, session.user_id, session.username, "owner.maintenance", "on" if enabled else "off")
    return {"maintenance": enabled}


@router.post("/restart")
async def restart(session=Depends(require_owner)):
    """Beendet den Prozess sauber – Docker (restart: unless-stopped) bzw. start-Skript startet ihn neu."""
    await audit(None, session.user_id, session.username, "owner.restart", "")
    log.warning("Neustart angefordert von %s", session.username)

    async def later():
        await asyncio.sleep(1)
        bot = get_bot()
        await bot.close()
        os._exit(3)

    asyncio.create_task(later())
    return {"ok": True}


@router.post("/announce")
async def announce(body: dict = Body(...), session=Depends(require_owner)):
    title, message = str(body.get("title", "")).strip()[:200], str(body.get("message", "")).strip()[:3500]
    if not title or not message:
        raise HTTPException(422, "Titel und Nachricht erforderlich")
    cog = get_bot().get_cog("System")
    sent, failed = await cog.announce(title, message)
    await audit(None, session.user_id, session.username, "owner.announce", title, {"sent": sent, "failed": failed})
    return {"sent": sent, "failed": failed}


@router.post("/sync-commands")
async def sync_commands(session=Depends(require_owner)):
    await get_bot().sync_commands(force=True)
    await audit(None, session.user_id, session.username, "owner.sync_commands", "")
    return {"ok": True}


@router.get("/errors")
async def errors(resolved: bool = False, q: str = "", page: int = 1):
    off, lim = paginate(page, 50)
    async with SessionLocal() as db:
        qry = select(ErrorLog).where(ErrorLog.resolved.is_(resolved))
        if q:
            qry = qry.where(or_(ErrorLog.id == q.upper(), ErrorLog.command.ilike(f"%{q}%"), ErrorLog.message.ilike(f"%{q}%")))
        total = (await db.execute(select(func.count()).select_from(qry.subquery()))).scalar_one()
        rows = (await db.execute(qry.order_by(ErrorLog.created_at.desc()).offset(off).limit(lim))).scalars().all()
    bot = get_bot()
    return {"items": [r.to_dict() | {"guild_name": (bot.get_guild(r.guild_id).name if r.guild_id and bot.get_guild(r.guild_id) else None)} for r in rows],
            "total": total, "page": page}


@router.post("/errors/{error_id}/resolve")
async def resolve(error_id: str, body: dict = Body(default={}), session=Depends(require_owner)):
    async with session_scope() as db:
        e = await db.get(ErrorLog, error_id)
        if not e:
            raise HTTPException(404, "Fehler nicht gefunden")
        e.resolved = bool(body.get("resolved", True))
        e.resolved_by = session.user_id
    return {"ok": True}


@router.get("/logs")
async def global_logs(q: str = "", page: int = 1):
    off, lim = paginate(page, 50)
    async with SessionLocal() as db:
        qry = select(AuditEntry)
        if q:
            qry = qry.where(or_(AuditEntry.action.ilike(f"%{q}%"), AuditEntry.actor_name.ilike(f"%{q}%"), AuditEntry.target.ilike(f"%{q}%")))
        rows = (await db.execute(qry.order_by(AuditEntry.created_at.desc()).offset(off).limit(lim))).scalars().all()
        logs_q = select(LogEntry).order_by(LogEntry.created_at.desc()).limit(50)
        recent = (await db.execute(logs_q)).scalars().all() if page == 1 and not q else []
    bot = get_bot()
    name = lambda gid: bot.get_guild(gid).name if gid and bot.get_guild(gid) else None  # noqa: E731
    return {"audit": [r.to_dict() | {"guild_name": name(r.guild_id)} for r in rows],
            "events": [r.to_dict() | {"guild_name": name(r.guild_id)} for r in recent]}


@router.get("/commands")
async def commands_usage(days: int = Query(7, ge=1, le=90)):
    from datetime import timedelta
    from app.db.base import utcnow
    from app.db.models import StatBucket
    async with SessionLocal() as db:
        rows = (await db.execute(select(StatBucket.metric, func.sum(StatBucket.value)).where(
            StatBucket.metric.like("cmd:%"), StatBucket.bucket >= utcnow() - timedelta(days=days)).group_by(StatBucket.metric)
            .order_by(func.sum(StatBucket.value).desc()))).all()
    bot = get_bot()
    return {"usage": [{"name": m.removeprefix("cmd:"), "count": int(n)} for m, n in rows],
            "registered": sorted(c.name for c in bot.tree.get_commands())}


@router.get("/backups")
async def full_backups():
    async with SessionLocal() as db:
        rows = (await db.execute(select(Backup).where(Backup.kind == "full").order_by(Backup.created_at.desc()).limit(50))).scalars().all()
    return {"items": [b.to_dict() for b in rows]}


@router.post("/backups")
async def create_full_backup(session=Depends(require_owner)):
    try:
        b = await backups.full_backup(created_by=session.user_id)
    except RuntimeError as exc:
        raise HTTPException(500, str(exc))
    await audit(None, session.user_id, session.username, "owner.full_backup", b.filename)
    return b.to_dict()


@router.get("/backups/{backup_id}/download")
async def download_full(backup_id: int, session=Depends(require_owner)):
    async with SessionLocal() as db:
        b = await db.get(Backup, backup_id)
    if not b or b.kind != "full":
        raise HTTPException(404, "Backup nicht gefunden")
    await audit(None, session.user_id, session.username, "owner.backup_download", b.filename)
    return FileResponse(backups.backup_dir() / b.filename, filename=b.filename)
