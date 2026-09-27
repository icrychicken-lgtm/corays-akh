"""Kern-API: Benutzer, Serverliste, Übersicht, Server-Info, Metadaten, Module/Settings, Analytics, Suche."""
from __future__ import annotations

from datetime import timedelta
from typing import Any

import discord
import httpx
from fastapi import APIRouter, Body, Depends, HTTPException, Query
from sqlalchemy import func, or_, select

from app.config import settings
from app.core.cache import cache
from app.core.crypto import decrypt
from app.core.guild_config import config
from app.core.metrics import series
from app.core.records import audit
from app.core.schema import MODULE_MAP, MODULES, ValidationError, validate_module
from app.db.base import SessionLocal, utcnow
from app.db.models import (Application, Badge, CustomCommand, DashboardSession, Giveaway, LogEntry, ModCase, StatBucket, Suggestion,
                           Ticket)
from app.runtime import get_bot
from app.web.common import bot_status, channel_json, role_json, sid, user_json, validation_ctx
from app.web.deps import Ctx, access_level, admin, require_session, staff

router = APIRouter(prefix="/api")


@router.get("/me")
async def me(session: DashboardSession = Depends(require_session)):
    bot = get_bot()
    return {"user": {"id": str(session.user_id), "name": session.username, "avatar": session.avatar},
            "csrf": session.csrf_token, "is_owner": session.user_id in settings.owner_id_set,
            "bot": user_json(bot.user) if bot.user else None, "bot_name": settings.bot_name}


@router.get("/status")
async def status(_s=Depends(require_session)):
    return bot_status(get_bot())


async def _oauth_guilds(session: DashboardSession) -> list[dict[str, Any]]:
    key = f"oauth_guilds:{session.user_id}"
    cached = await cache.get(key)
    if cached is not None:
        return cached
    token = decrypt(session.access_token_enc)
    if not token:
        return []
    async with httpx.AsyncClient(timeout=10) as c:
        r = await c.get("https://discord.com/api/v10/users/@me/guilds", headers={"Authorization": f"Bearer {token}"})
    data = r.json() if r.status_code == 200 else []
    await cache.set(key, data, ttl=120)
    return data


@router.get("/guilds")
async def guilds(session: DashboardSession = Depends(require_session)):
    bot = get_bot()
    oauth = await _oauth_guilds(session)
    oauth_ids = {g["id"] for g in oauth}
    is_owner = session.user_id in settings.owner_id_set
    manageable, invitable = [], []
    for g in bot.guilds:
        if str(g.id) not in oauth_ids and not is_owner:
            continue
        level, _m = await access_level(g, session.user_id)
        if level:
            manageable.append({"id": str(g.id), "name": g.name, "icon": g.icon.url if g.icon else None, "members": g.member_count, "level": level})
    bot_ids = {str(g.id) for g in bot.guilds}
    for g in oauth:
        perms = int(g.get("permissions", 0))
        if g["id"] not in bot_ids and (g.get("owner") or perms & 0x20 or perms & 0x8):
            icon = f"https://cdn.discordapp.com/icons/{g['id']}/{g['icon']}.png" if g.get("icon") else None
            invitable.append({"id": g["id"], "name": g["name"], "icon": icon})
    return {"manageable": sorted(manageable, key=lambda x: x["name"].lower()), "invitable": invitable[:50]}


@router.get("/g/{guild_id}/context")
async def context(ctx: Ctx = Depends(staff)):
    g = ctx.guild
    toggles = await config.all_toggles(g.id)
    gen = await config.get(g.id, "general")
    return {"guild": {"id": str(g.id), "name": g.name, "icon": g.icon.url if g.icon else None}, "level": ctx.level,
            "is_owner": ctx.is_owner, "modules": toggles, "accent": gen.get("color_primary"), "language": gen.get("language")}


@router.get("/g/{guild_id}/meta")
async def meta(ctx: Ctx = Depends(staff)):
    g = ctx.guild
    async with SessionLocal() as db:
        badges = (await db.execute(select(Badge).where(Badge.guild_id == g.id).order_by(Badge.position))).scalars().all()
    chans = sorted(g.channels, key=lambda c: (c.category.position if getattr(c, "category", None) else -1, c.position))
    return {
        "channels": [channel_json(c) for c in chans],
        "roles": [role_json(r, g.me) for r in sorted(g.roles, key=lambda r: -r.position)],
        "emojis": [{"id": str(e.id), "name": e.name, "url": e.url, "animated": e.animated} for e in g.emojis],
        "badges": [{"id": str(b.id), "name": b.name, "emoji": b.emoji} for b in badges],
    }


@router.get("/g/{guild_id}/overview")
async def overview(ctx: Ctx = Depends(staff)):
    g = ctx.guild
    bot = get_bot()
    now = utcnow()
    today = now.replace(hour=0, minute=0, second=0, microsecond=0)
    async with SessionLocal() as db:
        open_tickets = (await db.execute(select(func.count()).select_from(Ticket).where(Ticket.guild_id == g.id, Ticket.status == "open"))).scalar_one()
        active_gw = (await db.execute(select(func.count()).select_from(Giveaway).where(Giveaway.guild_id == g.id, Giveaway.ended.is_(False)))).scalar_one()
        open_apps = (await db.execute(select(func.count()).select_from(Application).where(Application.guild_id == g.id, Application.status.in_(["open", "in_review"])))).scalar_one()
        cases = (await db.execute(select(ModCase).where(ModCase.guild_id == g.id).order_by(ModCase.created_at.desc()).limit(8))).scalars().all()
        todays = dict((await db.execute(select(StatBucket.metric, func.sum(StatBucket.value)).where(
            StatBucket.guild_id == g.id, StatBucket.bucket >= today, StatBucket.metric.in_(["messages", "joins", "leaves", "commands", "voice_minutes"]))
            .group_by(StatBucket.metric))).all())
        top_cmds = (await db.execute(select(StatBucket.metric, func.sum(StatBucket.value).label("n")).where(
            StatBucket.guild_id == g.id, StatBucket.bucket >= now - timedelta(days=7), StatBucket.metric.like("cmd:%"))
            .group_by(StatBucket.metric).order_by(func.sum(StatBucket.value).desc()).limit(8))).all()
    online = sum(1 for m in g.members if m.status != discord.Status.offline) if settings.enable_presence_intent else None
    charts = await series(g.id, ["messages", "joins", "leaves", "members"], "30d")
    return {
        "guild": {"id": str(g.id), "name": g.name, "icon": g.icon.url if g.icon else None, "banner": g.banner.url if g.banner else None},
        "stats": {
            "members": g.member_count, "online": online, "bots": sum(1 for m in g.members if m.bot),
            "channels": len(g.channels), "roles": len(g.roles) - 1, "boosts": g.premium_subscription_count, "boost_level": g.premium_tier,
            "open_tickets": open_tickets, "active_giveaways": active_gw, "open_applications": open_apps,
            "messages_today": int(todays.get("messages", 0)), "joins_today": int(todays.get("joins", 0)),
            "leaves_today": int(todays.get("leaves", 0)), "commands_today": int(todays.get("commands", 0)),
            "voice_minutes_today": int(todays.get("voice_minutes", 0)),
            "voice_now": sum(len([m for m in vc.members if not m.bot]) for vc in g.voice_channels),
        },
        "recent_cases": [c.to_dict() for c in cases],
        "top_commands": [{"name": m.removeprefix("cmd:"), "count": int(n)} for m, n in top_cmds],
        "charts": charts,
        "bot": bot_status(bot),
    }


@router.get("/g/{guild_id}/server")
async def server_info(ctx: Ctx = Depends(staff)):
    g = ctx.guild
    owner = g.owner or await get_bot().fetch_user(g.owner_id)
    return {
        "id": str(g.id), "name": g.name, "description": g.description, "icon": g.icon.url if g.icon else None,
        "banner": g.banner.url if g.banner else None, "splash": g.splash.url if g.splash else None,
        "created_at": g.created_at.isoformat(), "owner": user_json(owner, g.owner_id),
        "members": g.member_count, "humans": sum(1 for m in g.members if not m.bot), "bots": sum(1 for m in g.members if m.bot),
        "boost_level": g.premium_tier, "boosts": g.premium_subscription_count,
        "boosters": [user_json(m) for m in g.premium_subscribers[:50]],
        "text_channels": len(g.text_channels), "voice_channels": len(g.voice_channels), "categories": len(g.categories),
        "forums": len(g.forums), "stage_channels": len(g.stage_channels), "threads": len(g.threads),
        "roles": len(g.roles) - 1, "emojis": len(g.emojis), "emoji_limit": g.emoji_limit, "stickers": len(g.stickers),
        "sticker_limit": g.sticker_limit, "verification_level": str(g.verification_level), "features": sorted(g.features),
        "vanity": g.vanity_url_code, "locale": str(g.preferred_locale), "nsfw_level": str(g.nsfw_level),
        "mfa_level": str(g.mfa_level), "afk_channel": g.afk_channel.name if g.afk_channel else None,
        "bot_joined": g.me.joined_at.isoformat() if g.me.joined_at else None, "bot_permissions": str(g.me.guild_permissions.value),
        "bot_top_role": g.me.top_role.name, "emoji_list": [{"name": e.name, "url": e.url} for e in g.emojis[:60]],
        "sticker_list": [{"name": s.name, "url": s.url} for s in g.stickers[:30]],
    }


# ───────────────────────── Befehlsübersicht ─────────────────────────
@router.get("/g/{guild_id}/features")
async def features(ctx: Ctx = Depends(staff)):
    """Alle Befehle gruppiert nach Bereich – inkl. Info, ob das Modul aktiv ist."""
    import discord as _d
    from app.bot.bot import module_of
    from app.bot.modules.help import CATEGORIES, _category
    bot = get_bot()
    toggles = await config.all_toggles(ctx.guild.id)
    groups: dict[str, list] = {k: [] for k, _e in CATEGORIES}
    for root in bot.tree.get_commands():
        if isinstance(root, _d.app_commands.ContextMenu):
            cat = (root.extras or {}).get("help_category") or "community"
            mod = module_of(root)
            groups.setdefault(cat, []).append({"name": root.name, "description": "Rechtsklick → Apps", "context": True,
                                               "module": mod, "active": mod == "core" or toggles.get(mod, True)})
            continue
        cmds = [c for c in root.walk_commands() if isinstance(c, _d.app_commands.Command)] if isinstance(root, _d.app_commands.Group) else [root]
        for c in cmds:
            mod = module_of(c)
            groups.setdefault(_category(c.root_parent or c), []).append({
                "name": c.qualified_name, "description": c.description, "context": False, "module": mod,
                "active": mod == "core" or toggles.get(mod, True),
                "admin": bool(getattr(c.root_parent or c, "default_permissions", None)),
                "params": [{"name": p.name, "required": p.required, "description": p.description} for p in c.parameters]})
    labels = {"moderation": "Moderation", "community": "Community", "economy": "Economy & Gangs", "music": "Musik", "tickets": "Tickets & Support",
              "fun": "Fun", "streamer": "Streamer", "admin": "Admin & Einrichtung"}
    return {"groups": [{"key": k, "label": labels.get(k, k), "commands": sorted(v, key=lambda x: x["name"])} for k, v in groups.items() if v],
            "total": sum(len(v) for v in groups.values())}


# ───────────────────────── Module & Einstellungen ─────────────────────────
@router.get("/g/{guild_id}/modules")
async def modules(ctx: Ctx = Depends(admin)):
    out = []
    for spec in MODULES:
        cfg = await config.get(ctx.guild.id, spec.key)
        out.append({**spec.to_json(), "enabled": cfg.enabled, "settings": dict(cfg)})
    return out


@router.put("/g/{guild_id}/modules/{key}")
async def save_module(key: str, body: dict = Body(...), ctx: Ctx = Depends(admin)):
    spec = MODULE_MAP.get(key)
    if spec is None:
        raise HTTPException(404, "Unbekanntes Modul")
    enabled = body.get("enabled")
    clean = None
    if "settings" in body:
        try:
            clean = validate_module(spec, body["settings"] or {}, await validation_ctx(ctx.guild))
        except ValidationError as exc:
            raise HTTPException(422, {"message": "Validierung fehlgeschlagen", "errors": exc.errors})
    await config.save(ctx.guild.id, key, settings=clean, enabled=bool(enabled) if enabled is not None else None)
    await audit(ctx.guild.id, ctx.user_id, ctx.actor_name, "settings.update" if clean is not None else "module.toggle", spec.name,
                {"enabled": enabled} if enabled is not None else {})
    bot = get_bot()
    if key == "custom_commands" and enabled is not None:
        cc = bot.get_cog("CustomCommands")
        if cc:
            cc.schedule_sync(ctx.guild.id)
    cfg = await config.get(ctx.guild.id, key)
    return {"enabled": cfg.enabled, "settings": dict(cfg)}


# ───────────────────────── Analytics ─────────────────────────
ANALYTICS_METRICS = ["members", "joins", "leaves", "messages", "voice_minutes", "xp", "coins", "tickets_opened", "tickets_closed",
                     "mod_actions", "automod", "giveaways", "streams", "stream_viewers", "commands", "reactions", "online", "boosts",
                     "stream_checkins"]


@router.get("/g/{guild_id}/analytics")
async def analytics(range: str = Query("7d", pattern="^(24h|7d|30d|90d|all)$"), metrics: str = "", ctx: Ctx = Depends(staff)):
    wanted = [m for m in metrics.split(",") if m in ANALYTICS_METRICS] or ANALYTICS_METRICS
    data = await series(ctx.guild.id, wanted, range)
    totals = {m: (max(v) if m in ("members", "online", "stream_viewers") else sum(v)) for m, v in data["series"].items()}
    return {**data, "totals": totals}


# ───────────────────────── Globale Suche ─────────────────────────
@router.get("/g/{guild_id}/search")
async def search(q: str = Query(..., min_length=1, max_length=100), ctx: Ctx = Depends(staff)):
    g = ctx.guild
    ql = q.lower().strip()
    results: dict[str, list] = {"users": [], "tickets": [], "cases": [], "suggestions": [], "giveaways": [], "commands": [], "logs": []}
    for m in g.members:
        if ql in m.name.lower() or ql in m.display_name.lower() or ql == str(m.id):
            results["users"].append(user_json(m))
            if len(results["users"]) >= 8:
                break
    like = f"%{q}%"
    num = int(q.lstrip("#")) if q.lstrip("#").isdigit() and len(q) < 10 else None
    async with SessionLocal() as db:
        tq = select(Ticket).where(Ticket.guild_id == g.id, or_(Ticket.subject.ilike(like), Ticket.opener_name.ilike(like),
                                                               Ticket.number == num if num else False)).limit(6)
        results["tickets"] = [{"id": t.id, "number": t.number, "subject": t.subject[:80], "status": t.status, "opener": t.opener_name}
                              for t in (await db.execute(tq)).scalars()]
        cq = select(ModCase).where(ModCase.guild_id == g.id, or_(ModCase.reason.ilike(like), ModCase.user_name.ilike(like),
                                                                 ModCase.case_number == num if num else False, ModCase.user_id == int(q) if q.isdigit() and len(q) > 15 else False)).limit(6)
        results["cases"] = [{"number": c.case_number, "action": c.action, "user": c.user_name, "reason": c.reason[:80]} for c in (await db.execute(cq)).scalars()]
        sq = select(Suggestion).where(Suggestion.guild_id == g.id, or_(Suggestion.content.ilike(like), Suggestion.number == num if num else False)).limit(6)
        results["suggestions"] = [{"id": s.id, "number": s.number, "content": s.content[:80], "status": s.status} for s in (await db.execute(sq)).scalars()]
        gq = select(Giveaway).where(Giveaway.guild_id == g.id, or_(Giveaway.prize.ilike(like), Giveaway.id == num if num else False)).limit(6)
        results["giveaways"] = [{"id": x.id, "prize": x.prize, "ended": x.ended} for x in (await db.execute(gq)).scalars()]
        ccq = select(CustomCommand).where(CustomCommand.guild_id == g.id, CustomCommand.name.ilike(like)).limit(5)
        results["commands"] = [{"name": c.name, "custom": True, "id": c.id} for c in (await db.execute(ccq)).scalars()]
        lq = select(LogEntry).where(LogEntry.guild_id == g.id, or_(LogEntry.content.ilike(like), LogEntry.user_name.ilike(like))).order_by(LogEntry.created_at.desc()).limit(6)
        results["logs"] = [{"id": l.id, "category": l.category, "action": l.action, "user": l.user_name, "content": l.content[:100],
                            "created_at": l.created_at.isoformat()} for l in (await db.execute(lq)).scalars()]
    for c in get_bot().tree.get_commands():
        if ql in c.name.lower():
            results["commands"].append({"name": c.name, "custom": False, "description": getattr(c, "description", "")})
    results["commands"] = results["commands"][:8]
    return results
