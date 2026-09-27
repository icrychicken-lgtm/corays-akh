"""API: Collections (CRUD), Tickets, Bewerbungen, Giveaways, Suggestions, Umfragen, Musik, Rollen, Streamer-Stats,
Panels, Leaderboards, Achievements, Integrationen, Command-Berechtigungen, Backups."""
from __future__ import annotations

import re
from datetime import timedelta
from typing import Any

import discord
from fastapi import APIRouter, Body, Depends, HTTPException, Query
from fastapi.responses import FileResponse, HTMLResponse
from sqlalchemy import func, or_, select

from app.core.errors import UserError
from app.core.guild_config import config
from app.core.i18n import i18n
from app.core.records import audit
from app.core.schema import ValidationError, clean_value
from app.core.timeutil import parse_duration
from app.db.base import SessionLocal, session_scope, utcnow
from app.db.models import (Application, Backup, CommandPermission, CustomCommand, EventParticipant, Giveaway, GiveawayEntry, Member,
                           MemberAchievement, Poll, PollVote, ServerEvent, Streamer, StreamSession, StreamSnapshot, Suggestion, Ticket,
                           TicketNote)
from app.runtime import get_bot
from app.services import achievements, backups
from app.services.integrations import PROVIDERS, public_status, save_credentials
from app.services.progression import invalidate_quests
from app.web.collections import COLLECTIONS, Collection, serialize, to_column
from app.web.common import paginate, role_json, user_json, validation_ctx
from app.web.deps import LEVELS, Ctx, admin, staff

router = APIRouter(prefix="/api/g/{guild_id}")


def _user_err(exc: UserError) -> HTTPException:
    return HTTPException(400, i18n.t("de", exc.key, **exc.kw).replace("**", ""))


def _cog(name: str):
    cog = get_bot().get_cog(name)
    if cog is None:
        raise HTTPException(503, f"Modul {name} ist nicht geladen")
    return cog


# ───────────────────────── Collections ─────────────────────────
def _coll(key: str, ctx: Ctx) -> Collection:
    c = COLLECTIONS.get(key)
    if c is None:
        raise HTTPException(404, "Unbekannte Collection")
    if LEVELS[ctx.level] < LEVELS[c.level]:
        raise HTTPException(403, "Keine Berechtigung")
    return c


@router.get("/c/{key}")
async def coll_list(key: str, ctx: Ctx = Depends(staff)):
    c = _coll(key, ctx)
    async with SessionLocal() as db:
        q = select(c.model).where(c.model.guild_id == ctx.guild.id)
        for o in c.order:
            q = q.order_by(getattr(c.model, o))
        rows = (await db.execute(q.limit(c.limit))).scalars().all()
    return {"collection": c.to_json(), "items": [serialize(c, r) for r in rows]}


async def _clean(c: Collection, ctx: Ctx, body: dict[str, Any]) -> dict[str, Any]:
    vctx = await validation_ctx(ctx.guild)
    data, errors = {}, {}
    for f in c.fields:
        try:
            data[f.key] = clean_value(f, body.get(f.key, f.default), vctx)
        except (ValueError, TypeError) as exc:
            errors[f.key] = str(exc)
    if errors:
        raise HTTPException(422, {"message": "Validierung fehlgeschlagen", "errors": errors})
    return data


async def _extra_validate(c: Collection, ctx: Ctx, data: dict[str, Any], obj_id: int | None) -> None:
    if c.key == "custom_commands":
        from app.bot.modules.custom import NAME_RE
        name = data["name"].lower().strip()
        data["name"] = name
        if not NAME_RE.match(name):
            raise HTTPException(422, {"message": "Ungültiger Name", "errors": {"name": "Nur a-z, 0-9, - und _ (max. 32)"}})
        if name in {cmd.name for cmd in get_bot().tree.get_commands()}:
            raise HTTPException(422, {"message": "Name reserviert", "errors": {"name": "Es gibt bereits einen Bot-Command mit diesem Namen"}})
        async with SessionLocal() as db:
            dup = (await db.execute(select(CustomCommand.id).where(CustomCommand.guild_id == ctx.guild.id, CustomCommand.name == name))).scalar_one_or_none()
        if dup and dup != obj_id:
            raise HTTPException(422, {"message": "Name vergeben", "errors": {"name": "Existiert bereits"}})
    if c.key == "autoresponders" and data["match_type"] == "regex":
        try:
            re.compile(data["trigger"])
        except re.error:
            raise HTTPException(422, {"message": "Regex ungültig", "errors": {"trigger": "Ungültiger regulärer Ausdruck"}})
    if c.key == "role_menus":
        if not any(o.get("role_id") for o in data["options"]):
            raise HTTPException(422, {"message": "Mindestens eine Rolle", "errors": {"options": "Mindestens eine Rolle hinzufügen"}})
        for o in data["options"]:
            role = ctx.guild.get_role(int(o["role_id"])) if o.get("role_id") else None
            if role and (role >= ctx.guild.me.top_role or role.managed):
                raise HTTPException(422, {"message": "Rolle nicht verwaltbar", "errors": {"options": f"{role.name} liegt über der Bot-Rolle"}})
    if c.key == "streamers":
        data["channel"] = data["channel"].strip().lstrip("@") if data["platform"] != "youtube" else data["channel"].strip()
        if not data["channel"]:
            raise HTTPException(422, {"message": "Kanal fehlt", "errors": {"channel": "Pflichtfeld"}})
    if c.key == "events" and obj_id is None and data["starts_at"] <= utcnow():
        raise HTTPException(422, {"message": "Datum in der Vergangenheit", "errors": {"starts_at": "Muss in der Zukunft liegen"}})


async def _after(c: Collection, ctx: Ctx) -> None:
    bot = get_bot()
    if c.key == "custom_commands":
        cc = bot.get_cog("CustomCommands")
        if cc:
            cc.schedule_sync(ctx.guild.id)
    elif c.key == "autoresponders":
        cc = bot.get_cog("CustomCommands")
        if cc:
            cc.invalidate_autoresponders(ctx.guild.id)
    elif c.key == "quests":
        invalidate_quests(ctx.guild.id)


@router.post("/c/{key}")
async def coll_create(key: str, body: dict = Body(...), ctx: Ctx = Depends(staff)):
    c = _coll(key, ctx)
    data = await _clean(c, ctx, body)
    await _extra_validate(c, ctx, data, None)
    async with session_scope() as db:
        count = (await db.execute(select(func.count()).select_from(c.model).where(c.model.guild_id == ctx.guild.id))).scalar_one()
        if count >= c.limit:
            raise HTTPException(422, f"Maximal {c.limit} Einträge")
        obj = c.model(guild_id=ctx.guild.id, **{k: to_column(c.model, k, v) for k, v in data.items()})
        if key == "events":
            obj.created_by = ctx.user_id
        db.add(obj)
        await db.flush()
        out = serialize(c, obj)
    await _after(c, ctx)
    await audit(ctx.guild.id, ctx.user_id, ctx.actor_name, f"{key}.create", str(data.get("name") or data.get("label") or data.get("trigger") or out["id"]))
    return out


@router.put("/c/{key}/{obj_id}")
async def coll_update(key: str, obj_id: int, body: dict = Body(...), ctx: Ctx = Depends(staff)):
    c = _coll(key, ctx)
    data = await _clean(c, ctx, body)
    await _extra_validate(c, ctx, data, obj_id)
    async with session_scope() as db:
        obj = await db.get(c.model, obj_id)
        if obj is None or obj.guild_id != ctx.guild.id:
            raise HTTPException(404, "Eintrag nicht gefunden")
        if key == "streamers" and (obj.channel != data["channel"] or obj.platform != data["platform"]):
            obj.platform_user_id, obj.seen_video_ids, obj.is_live, obj.current_session_id = None, [], False, None
        for k, v in data.items():
            setattr(obj, k, to_column(c.model, k, v))
        out = serialize(c, obj)
    await _after(c, ctx)
    await audit(ctx.guild.id, ctx.user_id, ctx.actor_name, f"{key}.update", str(obj_id))
    return out


@router.delete("/c/{key}/{obj_id}")
async def coll_delete(key: str, obj_id: int, ctx: Ctx = Depends(staff)):
    c = _coll(key, ctx)
    async with session_scope() as db:
        obj = await db.get(c.model, obj_id)
        if obj is None or obj.guild_id != ctx.guild.id:
            raise HTTPException(404, "Eintrag nicht gefunden")
        await db.delete(obj)
    await _after(c, ctx)
    await audit(ctx.guild.id, ctx.user_id, ctx.actor_name, f"{key}.delete", str(obj_id))
    return {"ok": True}


@router.post("/c/{key}/{obj_id}/action/{action}")
async def coll_action(key: str, obj_id: int, action: str, body: dict = Body(default={}), ctx: Ctx = Depends(staff)):
    c = _coll(key, ctx)
    g = ctx.guild
    try:
        if key == "role_menus" and action == "publish":
            await _cog("Roles").publish(g, obj_id)
        elif key == "streamers" and action == "check":
            await _cog("StreamerCog").check_now(g, obj_id)
        elif key == "events" and action == "publish":
            await _cog("Events").publish(g, obj_id)
        elif key == "events" and action == "cancel":
            async with session_scope() as db:
                ev = await db.get(ServerEvent, obj_id)
                if not ev or ev.guild_id != g.id:
                    raise HTTPException(404, "Event nicht gefunden")
                ev.status = "cancelled"
            await _cog("Events").refresh(g, ev)
        elif key == "events" and action == "finish":
            winners = [int(x) for x in body.get("winner_ids") or [] if str(x).isdigit()][:50]
            await _cog("Events").finish(g, obj_id, winners, ctx.member)
        else:
            raise HTTPException(404, "Unbekannte Aktion")
    except UserError as exc:
        raise _user_err(exc)
    except discord.Forbidden:
        raise HTTPException(403, "Dem Bot fehlen Discord-Berechtigungen dafür")
    await audit(g.id, ctx.user_id, ctx.actor_name, f"{key}.{action}", str(obj_id))
    async with SessionLocal() as db:
        obj = await db.get(c.model, obj_id)
    return serialize(c, obj) if obj else {"ok": True}


@router.get("/events/{event_id}/participants")
async def event_participants(event_id: int, ctx: Ctx = Depends(staff)):
    async with SessionLocal() as db:
        ev = await db.get(ServerEvent, event_id)
        if not ev or ev.guild_id != ctx.guild.id:
            raise HTTPException(404, "Event nicht gefunden")
        rows = (await db.execute(select(EventParticipant).where(EventParticipant.event_id == event_id))).scalars().all()
    return {"items": [{**(user_json(ctx.guild.get_member(r.user_id), r.user_id) or {}), "winner": r.winner} for r in rows]}


# ───────────────────────── Panels ─────────────────────────
PANELS = {"tickets": "Tickets", "verification": "Verification", "applications": "Applications", "suggestions": "Suggestions", "media": "StreamerCog"}


@router.post("/panels/{kind}")
async def send_panel(kind: str, ctx: Ctx = Depends(admin)):
    if kind not in PANELS:
        raise HTTPException(404, "Unbekanntes Panel")
    try:
        msg = await _cog(PANELS[kind]).send_panel(ctx.guild)
    except UserError as exc:
        raise _user_err(exc)
    except discord.Forbidden:
        raise HTTPException(403, "Dem Bot fehlen Berechtigungen im Ziel-Channel")
    await audit(ctx.guild.id, ctx.user_id, ctx.actor_name, "panel.send", kind)
    return {"ok": True, "url": msg.jump_url}


@router.post("/stats-channels/create")
async def create_stats_channels(ctx: Ctx = Depends(admin)):
    try:
        data = await _cog("StatsChannels").create_channels(ctx.guild)
    except discord.Forbidden:
        raise HTTPException(403, "Dem Bot fehlt 'Kanäle verwalten'")
    await audit(ctx.guild.id, ctx.user_id, ctx.actor_name, "stats_channels.create", "")
    return {"settings": data}


# ───────────────────────── Tickets ─────────────────────────
@router.get("/tickets")
async def tickets(status: str = "", q: str = "", priority: str = "", page: int = 1, per_page: int = 25, ctx: Ctx = Depends(staff)):
    off, lim = paginate(page, per_page)
    async with SessionLocal() as db:
        qry = select(Ticket).where(Ticket.guild_id == ctx.guild.id)
        if status in ("open", "closed"):
            qry = qry.where(Ticket.status == status)
        if priority:
            qry = qry.where(Ticket.priority == priority)
        if q:
            num = int(q.lstrip("#")) if q.lstrip("#").isdigit() and len(q) < 8 else -1
            qry = qry.where(or_(Ticket.subject.ilike(f"%{q}%"), Ticket.opener_name.ilike(f"%{q}%"), Ticket.number == num))
        total = (await db.execute(select(func.count()).select_from(qry.subquery()))).scalar_one()
        rows = (await db.execute(qry.order_by(Ticket.status.desc(), Ticket.created_at.desc()).offset(off).limit(lim))).scalars().all()
        counts = dict((await db.execute(select(Ticket.status, func.count()).where(Ticket.guild_id == ctx.guild.id).group_by(Ticket.status))).all())
    return {"items": [t.to_dict(exclude={"transcript_html"}) | {"has_transcript": bool(t.transcript_html)} for t in rows],
            "total": total, "page": page, "per_page": lim, "counts": counts}


async def _ticket(ctx: Ctx, ticket_id: int) -> Ticket:
    async with SessionLocal() as db:
        t = await db.get(Ticket, ticket_id)
    if t is None or t.guild_id != ctx.guild.id:
        raise HTTPException(404, "Ticket nicht gefunden")
    return t


@router.post("/tickets/ensure-categories")
async def ensure_categories(body: dict = Body(default={}), ctx: Ctx = Depends(admin)):
    """Ohne Parameter: nur wenn keine Kategorien existieren. Mit {"missing": true}: fehlende Standard-Kategorien ergänzen."""
    from app.bot.modules.tickets import ensure_default_categories
    from app.db.models import TicketCategory
    from app.services.ticket_defaults import DEFAULT_CATEGORIES
    if not body.get("missing"):
        return {"created": await ensure_default_categories(ctx.guild.id)}
    async with session_scope() as db:
        rows = (await db.execute(select(TicketCategory).where(TicketCategory.guild_id == ctx.guild.id))).scalars().all()
        have = {r.label.lower(): r for r in rows}
        created = 0
        for i, (label, emoji, desc, questions) in enumerate(DEFAULT_CATEGORIES):
            row = have.get(label.lower())
            if row is None:
                db.add(TicketCategory(guild_id=ctx.guild.id, label=label, emoji=emoji, description=desc, staff_role_ids=[], questions=questions,
                                      modal_question="Was ist dein Anliegen?", welcome_message="", position=len(rows) + i, enabled=True))
                created += 1
            elif not row.questions:
                row.questions = questions  # vorhandene Kategorie ohne Fragen bekommt die passenden
    return {"created": created}


@router.get("/tickets/{ticket_id}")
async def ticket_detail(ticket_id: int, ctx: Ctx = Depends(staff)):
    t = await _ticket(ctx, ticket_id)
    async with SessionLocal() as db:
        notes = (await db.execute(select(TicketNote).where(TicketNote.ticket_id == t.id).order_by(TicketNote.created_at))).scalars().all()
    ch = ctx.guild.get_channel(t.channel_id or 0)
    return {**t.to_dict(exclude={"transcript_html"}), "has_transcript": bool(t.transcript_html), "notes": [n.to_dict() for n in notes],
            "opener": user_json(ctx.guild.get_member(t.opener_id), t.opener_id), "channel_url": ch.jump_url if ch else None,
            "added_users": [user_json(ctx.guild.get_member(int(u)), int(u)) for u in t.added_user_ids or []]}


@router.get("/tickets/{ticket_id}/transcript", response_class=HTMLResponse)
async def ticket_transcript(ticket_id: int, ctx: Ctx = Depends(staff)):
    t = await _ticket(ctx, ticket_id)
    html = t.transcript_html
    if not html:
        ch = ctx.guild.get_channel(t.channel_id or 0)
        if isinstance(ch, discord.TextChannel):
            from app.bot.modules.tickets import build_transcript
            html, _n = await build_transcript(ch, t, ctx.guild)
    if not html:
        raise HTTPException(404, "Kein Transcript vorhanden")
    return HTMLResponse(html, headers={"Content-Security-Policy": "default-src 'none'; img-src https: data:; style-src 'unsafe-inline'; sandbox allow-popups allow-popups-to-escape-sandbox",
                                       "X-Frame-Options": "SAMEORIGIN"})


@router.post("/tickets/{ticket_id}/action")
async def ticket_action(ticket_id: int, body: dict = Body(...), ctx: Ctx = Depends(staff)):
    t = await _ticket(ctx, ticket_id)
    cog = _cog("Tickets")
    action = body.get("action")
    actor = ctx.member or ctx.actor
    try:
        if action == "claim":
            await cog.claim(ctx.guild, t.id, actor)
        elif action == "close":
            await cog.close(ctx.guild, t.id, actor, str(body.get("reason", ""))[:500])
        elif action == "reopen":
            await cog.reopen(ctx.guild, t.id, actor)
        elif action == "lock":
            await cog.set_locked(ctx.guild, t.id, bool(body.get("locked", not t.locked)), actor)
        elif action == "priority":
            await cog.set_priority(ctx.guild, t.id, str(body.get("priority")), actor)
        elif action == "transfer":
            m = ctx.guild.get_member(int(body.get("user_id", 0)))
            if not m:
                raise HTTPException(404, "Mitglied nicht gefunden")
            await cog.transfer(ctx.guild, t.id, m, actor)
        elif action in ("add_user", "remove_user"):
            m = ctx.guild.get_member(int(body.get("user_id", 0)))
            if not m:
                raise HTTPException(404, "Mitglied nicht gefunden")
            await (cog.add_user if action == "add_user" else cog.remove_user)(ctx.guild, t.id, m, actor)
        elif action == "note":
            content = str(body.get("content", "")).strip()
            if not content:
                raise HTTPException(422, "Notiz leer")
            await cog.add_note(t.id, actor if ctx.member else discord.Object(ctx.user_id), content)
        elif action == "delete":
            if ctx.level != "admin":
                raise HTTPException(403, "Nur Admins")
            await cog.delete_channel(ctx.guild, t.id, actor)
        else:
            raise HTTPException(422, "Unbekannte Aktion")
    except UserError as exc:
        raise _user_err(exc)
    await audit(ctx.guild.id, ctx.user_id, ctx.actor_name, f"ticket.{action}", f"#{t.number}")
    return await ticket_detail(ticket_id, ctx)


# ───────────────────────── Bewerbungen ─────────────────────────
@router.get("/applications")
async def applications(status: str = "", page: int = 1, per_page: int = 25, ctx: Ctx = Depends(staff)):
    off, lim = paginate(page, per_page)
    async with SessionLocal() as db:
        qry = select(Application).where(Application.guild_id == ctx.guild.id)
        if status:
            qry = qry.where(Application.status == status)
        total = (await db.execute(select(func.count()).select_from(qry.subquery()))).scalar_one()
        rows = (await db.execute(qry.order_by(Application.created_at.desc()).offset(off).limit(lim))).scalars().all()
        counts = dict((await db.execute(select(Application.status, func.count()).where(Application.guild_id == ctx.guild.id).group_by(Application.status))).all())
    return {"items": [a.to_dict() | {"user": user_json(ctx.guild.get_member(a.user_id), a.user_id)} for a in rows], "total": total, "counts": counts,
            "page": page, "per_page": lim}


@router.post("/applications/{app_id}/decide")
async def decide_application(app_id: int, body: dict = Body(...), ctx: Ctx = Depends(staff)):
    try:
        a = await _cog("Applications").decide(ctx.guild, app_id, str(body.get("status")), ctx.member or ctx.actor, str(body.get("reason", ""))[:1000])
    except UserError as exc:
        raise _user_err(exc)
    return a.to_dict()


# ───────────────────────── Giveaways ─────────────────────────
@router.get("/giveaways")
async def giveaways(ctx: Ctx = Depends(staff)):
    async with SessionLocal() as db:
        rows = (await db.execute(select(Giveaway).where(Giveaway.guild_id == ctx.guild.id).order_by(Giveaway.ended, Giveaway.ends_at.desc()).limit(200))).scalars().all()
        stats = dict((await db.execute(select(GiveawayEntry.giveaway_id, func.count()).where(
            GiveawayEntry.giveaway_id.in_([g.id for g in rows])).group_by(GiveawayEntry.giveaway_id))).all()) if rows else {}
    return {"items": [g.to_dict() | {"participants": stats.get(g.id, 0),
                                     "winners": [user_json(ctx.guild.get_member(int(w)), int(w)) for w in g.winner_ids]} for g in rows]}


@router.post("/giveaways")
async def create_giveaway(body: dict = Body(...), ctx: Ctx = Depends(staff)):
    cfg = await config.get(ctx.guild.id, "giveaways")
    ch = ctx.guild.get_channel(int(body.get("channel_id") or cfg.id("default_channel") or 0))
    if not isinstance(ch, discord.TextChannel):
        raise HTTPException(422, {"message": "Channel fehlt", "errors": {"channel_id": "Bitte Channel wählen"}})
    prize = str(body.get("prize", "")).strip()[:200]
    secs = parse_duration(str(body.get("duration", "")))
    if not prize or not secs:
        raise HTTPException(422, {"message": "Preis und Dauer erforderlich", "errors": {"prize": "" if prize else "Pflichtfeld", "duration": "" if secs else "z. B. 1h, 2d"}})
    req = {"role_id": str(body["required_role"]) if str(body.get("required_role") or "").isdigit() else None,
           "account_days": int(body.get("account_days") or 0), "server_days": int(body.get("server_days") or 0),
           "text": str(body.get("extra") or "")[:200]}
    bonus = {k: body[k] for k in ("invite_entries", "activity_messages", "activity_entries", "early_minutes", "early_entries") if str(body.get(k, "")).strip() != ""}
    bonus = {k: max(0, int(v)) for k, v in bonus.items()}
    if isinstance(body.get("role_entries"), list):
        bonus["role_entries"] = [{"role": str(r["role"]), "entries": max(1, min(int(r.get("entries") or 1), 100))}
                                 for r in body["role_entries"] if str(r.get("role", "")).isdigit()]
    try:
        g = await _cog("Giveaways").create(ctx.guild, ch, ctx.member or ctx.actor, prize, int(body.get("winners") or 1), secs,
                                           description=str(body.get("description") or "")[:2000], requirements=req, bonus=bonus,
                                           image_url=body.get("image_url") if str(body.get("image_url", "")).startswith("http") else None)
    except UserError as exc:
        raise _user_err(exc)
    await audit(ctx.guild.id, ctx.user_id, ctx.actor_name, "giveaway.create", prize)
    return g.to_dict()


@router.post("/giveaways/{gid}/{action}")
async def giveaway_action(gid: int, action: str, body: dict = Body(default={}), ctx: Ctx = Depends(staff)):
    cog = _cog("Giveaways")
    async with SessionLocal() as db:
        g = await db.get(Giveaway, gid)
    if g is None or g.guild_id != ctx.guild.id:
        raise HTTPException(404, "Giveaway nicht gefunden")
    try:
        if action == "end":
            await cog.end(g)
        elif action == "reroll":
            await cog.reroll(g, max(1, min(int(body.get("count") or 1), 50)))
        elif action == "delete":
            await cog.delete(g)
        else:
            raise HTTPException(404, "Unbekannte Aktion")
    except UserError as exc:
        raise _user_err(exc)
    await audit(ctx.guild.id, ctx.user_id, ctx.actor_name, f"giveaway.{action}", g.prize)
    return {"ok": True}


@router.get("/giveaways/{gid}/entries")
async def giveaway_entries(gid: int, ctx: Ctx = Depends(staff)):
    async with SessionLocal() as db:
        g = await db.get(Giveaway, gid)
        if g is None or g.guild_id != ctx.guild.id:
            raise HTTPException(404, "Giveaway nicht gefunden")
        rows = (await db.execute(select(GiveawayEntry).where(GiveawayEntry.giveaway_id == gid).order_by(GiveawayEntry.entries.desc()).limit(500))).scalars().all()
    return {"items": [{**(user_json(ctx.guild.get_member(r.user_id), r.user_id) or {}), "entries": r.entries, "joined_at": r.joined_at.isoformat()} for r in rows]}


# ───────────────────────── Suggestions & Umfragen ─────────────────────────
@router.get("/suggestions")
async def suggestions(status: str = "", q: str = "", ctx: Ctx = Depends(staff)):
    async with SessionLocal() as db:
        qry = select(Suggestion).where(Suggestion.guild_id == ctx.guild.id)
        if status:
            qry = qry.where(Suggestion.status == status)
        if q:
            qry = qry.where(Suggestion.content.ilike(f"%{q}%"))
        rows = (await db.execute(qry.order_by(Suggestion.created_at.desc()).limit(200))).scalars().all()
        counts = dict((await db.execute(select(Suggestion.status, func.count()).where(Suggestion.guild_id == ctx.guild.id).group_by(Suggestion.status))).all())
    return {"items": [s.to_dict() | {"user": user_json(ctx.guild.get_member(s.user_id), s.user_id),
                                     "url": f"https://discord.com/channels/{ctx.guild.id}/{s.channel_id}/{s.message_id}" if s.message_id else None} for s in rows],
            "counts": counts}


@router.post("/suggestions/{sid}/status")
async def suggestion_status(sid: int, body: dict = Body(...), ctx: Ctx = Depends(staff)):
    try:
        s = await _cog("Suggestions").set_status(ctx.guild, sid, str(body.get("status")), ctx.member or ctx.actor, str(body.get("reason", ""))[:1000])
    except UserError as exc:
        raise _user_err(exc)
    return s.to_dict()


@router.get("/polls")
async def polls(ctx: Ctx = Depends(staff)):
    async with SessionLocal() as db:
        rows = (await db.execute(select(Poll).where(Poll.guild_id == ctx.guild.id).order_by(Poll.created_at.desc()).limit(50))).scalars().all()
        votes = (await db.execute(select(PollVote.poll_id, PollVote.option_index, func.count()).where(PollVote.poll_id.in_([p.id for p in rows]))
                                  .group_by(PollVote.poll_id, PollVote.option_index))).all() if rows else []
    tally: dict[int, dict[int, int]] = {}
    for pid, idx, n in votes:
        tally.setdefault(pid, {})[idx] = n
    return {"items": [p.to_dict() | {"results": [tally.get(p.id, {}).get(i, 0) for i in range(len(p.options))]} for p in rows]}


# ───────────────────────── Musik ─────────────────────────
@router.get("/music")
async def music_state(ctx: Ctx = Depends(staff)):
    cog = get_bot().get_cog("Music")
    p = cog.players.get(ctx.guild.id) if cog else None
    voice = [{"id": str(c.id), "name": c.name, "members": len(c.members)} for c in ctx.guild.voice_channels + list(ctx.guild.stage_channels)]
    return {"state": p.state() if p else {"connected": False, "queue": [], "current": None}, "voice_channels": voice}


@router.post("/music/control")
async def music_control(body: dict = Body(...), ctx: Ctx = Depends(staff)):
    cog = _cog("Music")
    action = str(body.get("action"))
    try:
        if action == "play":
            ch = ctx.guild.get_channel(int(body.get("channel_id") or 0))
            if not isinstance(ch, (discord.VoiceChannel, discord.StageChannel)):
                raise HTTPException(422, "Bitte einen Voice-Channel wählen")
            query = str(body.get("query", "")).strip()[:400]
            if not query:
                raise HTTPException(422, "Suchbegriff fehlt")
            p = cog.players.get(ctx.guild.id)
            tracks = await cog.enqueue(ctx.guild, ch, p.text_channel if p else None, query, ctx.user_id)
            await audit(ctx.guild.id, ctx.user_id, ctx.actor_name, "music.play", query)
            return {"added": len(tracks), "state": cog.players[ctx.guild.id].state()}
        if action not in ("pause", "resume", "toggle", "skip", "stop", "volume", "loop", "shuffle", "remove", "clear", "move"):
            raise HTTPException(422, "Unbekannte Aktion")
        return {"state": await cog.control(ctx.guild, action, body.get("value"))}
    except UserError as exc:
        raise _user_err(exc)


# ───────────────────────── Rollen-Verwaltung ─────────────────────────
PERMISSION_NAMES = [p for p, _v in discord.Permissions.all()]


@router.get("/roles")
async def roles(ctx: Ctx = Depends(staff)):
    g = ctx.guild
    return {"roles": [role_json(r, g.me) for r in sorted(g.roles, key=lambda r: -r.position)], "permissions": PERMISSION_NAMES,
            "bot_top_position": g.me.top_role.position,
            "my_top_position": ctx.member.top_role.position if ctx.member else 0, "is_guild_owner": bool(ctx.member and ctx.member.id == g.owner_id)}


def _role_payload(ctx: Ctx, body: dict) -> dict[str, Any]:
    out: dict[str, Any] = {}
    if "name" in body:
        name = str(body["name"]).strip()[:100]
        if not name:
            raise HTTPException(422, "Name fehlt")
        out["name"] = name
    if "color" in body:
        col = str(body["color"] or "#000000")
        if not re.fullmatch(r"#[0-9a-fA-F]{6}", col):
            raise HTTPException(422, "Ungültige Farbe")
        out["colour"] = discord.Colour(int(col[1:], 16))
    for k in ("hoist", "mentionable"):
        if k in body:
            out[k] = bool(body[k])
    if "permissions" in body:
        perms = discord.Permissions(int(str(body["permissions"])) if str(body["permissions"]).isdigit() else 0)
        mine = ctx.member.guild_permissions if ctx.member else discord.Permissions.none()
        if not (ctx.is_owner or (ctx.member and ctx.member.id == ctx.guild.owner_id) or mine.administrator):
            # Niemand darf Rechte vergeben, die er selbst nicht hat
            if perms.value & ~mine.value:
                raise HTTPException(403, "Du kannst keine Berechtigungen vergeben, die du selbst nicht hast")
        out["permissions"] = perms
    return out


@router.post("/roles")
async def create_role(body: dict = Body(...), ctx: Ctx = Depends(admin)):
    payload = _role_payload(ctx, {"name": body.get("name", "Neue Rolle"), **body})
    try:
        role = await ctx.guild.create_role(reason=f"Dashboard: {ctx.actor_name}", **payload)
    except discord.Forbidden:
        raise HTTPException(403, "Dem Bot fehlt 'Rollen verwalten'")
    await audit(ctx.guild.id, ctx.user_id, ctx.actor_name, "role.create", role.name)
    return role_json(role, ctx.guild.me)


@router.patch("/roles/{role_id}")
async def edit_role(role_id: int, body: dict = Body(...), ctx: Ctx = Depends(admin)):
    from app.web.routes_moderation import _check_role_editable
    role = ctx.guild.get_role(role_id)
    if role is None:
        raise HTTPException(404, "Rolle nicht gefunden")
    _check_role_editable(ctx, role)
    payload = _role_payload(ctx, body)
    try:
        if payload:
            await role.edit(reason=f"Dashboard: {ctx.actor_name}", **payload)
        if "position" in body:
            pos = max(1, min(int(body["position"]), ctx.guild.me.top_role.position - 1))
            await role.edit(position=pos, reason=f"Dashboard: {ctx.actor_name}")
    except discord.Forbidden:
        raise HTTPException(403, "Dem Bot fehlen Berechtigungen (Rollen-Hierarchie)")
    await audit(ctx.guild.id, ctx.user_id, ctx.actor_name, "role.update", role.name, {k: str(v) for k, v in body.items()})
    return role_json(ctx.guild.get_role(role_id), ctx.guild.me)


@router.delete("/roles/{role_id}")
async def delete_role(role_id: int, ctx: Ctx = Depends(admin)):
    from app.web.routes_moderation import _check_role_editable
    role = ctx.guild.get_role(role_id)
    if role is None:
        raise HTTPException(404, "Rolle nicht gefunden")
    _check_role_editable(ctx, role)
    name = role.name
    await role.delete(reason=f"Dashboard: {ctx.actor_name}")
    await audit(ctx.guild.id, ctx.user_id, ctx.actor_name, "role.delete", name)
    return {"ok": True}


@router.post("/roles/{role_id}/mass")
async def mass_role(role_id: int, body: dict = Body(...), ctx: Ctx = Depends(admin)):
    role = ctx.guild.get_role(role_id)
    if role is None:
        raise HTTPException(404, "Rolle nicht gefunden")
    cog = _cog("Roles")
    only = ctx.guild.get_role(int(body["only_with"])) if str(body.get("only_with") or "").isdigit() else None
    target = body.get("target") if body.get("target") in ("all", "humans", "bots") else "humans"
    try:
        cog.check_manageable(ctx.guild, ctx.member or ctx.actor, role)
        job = cog.start_mass(ctx.guild, role, body.get("action") != "remove", target, ctx.member or ctx.actor, only)
    except UserError as exc:
        raise _user_err(exc)
    await audit(ctx.guild.id, ctx.user_id, ctx.actor_name, "role.mass_" + ("add" if job["add"] else "remove"), role.name, {"count": job["total"]})
    return {k: v for k, v in job.items() if k != "cancel"}


@router.get("/roles/mass")
async def mass_status(ctx: Ctx = Depends(staff)):
    job = _cog("Roles").jobs.get(ctx.guild.id)
    return {k: v for k, v in job.items() if k != "cancel"} if job else {"running": False}


@router.post("/roles/mass/cancel")
async def mass_cancel(ctx: Ctx = Depends(admin)):
    job = _cog("Roles").jobs.get(ctx.guild.id)
    if job:
        job["cancel"] = True
    return {"ok": True}


@router.post("/welcome/test")
async def welcome_test(ctx: Ctx = Depends(admin)):
    if ctx.member is None:
        raise HTTPException(403, "Du musst Mitglied des Servers sein")
    if not await _cog("Welcome").send_welcome(ctx.member):
        raise HTTPException(422, "Kein gültiger Willkommens-Channel eingestellt")
    return {"ok": True}


@router.post("/roles/sync")
async def sync_roles(ctx: Ctx = Depends(admin)):
    count = await _cog("Roles").sync_all(ctx.guild)
    await audit(ctx.guild.id, ctx.user_id, ctx.actor_name, "roles.sync", str(count))
    return {"checked": count}


# ───────────────────────── Streamer-Statistiken ─────────────────────────
RANGES = {"24h": timedelta(hours=24), "7d": timedelta(days=7), "30d": timedelta(days=30), "90d": timedelta(days=90)}


@router.get("/streamers/{streamer_id}/stats")
async def streamer_stats(streamer_id: int, range: str = Query("30d", pattern="^(24h|7d|30d|90d)$"), ctx: Ctx = Depends(staff)):
    since = utcnow() - RANGES[range]
    async with SessionLocal() as db:
        s = await db.get(Streamer, streamer_id)
        if s is None or s.guild_id != ctx.guild.id:
            raise HTTPException(404, "Streamer nicht gefunden")
        sessions = (await db.execute(select(StreamSession).where(StreamSession.streamer_id == streamer_id, StreamSession.started_at >= since)
                                     .order_by(StreamSession.started_at.desc()))).scalars().all()
        snaps = (await db.execute(select(StreamSnapshot).where(StreamSnapshot.streamer_id == streamer_id, StreamSnapshot.taken_at >= since)
                                  .order_by(StreamSnapshot.taken_at))).scalars().all()
        current = await db.get(StreamSession, s.current_session_id) if s.current_session_id else None
    viewers = [{"t": x.taken_at.isoformat(), "v": x.viewers} for x in snaps if x.live]
    growth = [{"t": x.taken_at.isoformat(), "followers": x.followers, "subscribers": x.subscribers} for x in snaps
              if not x.live and (x.followers is not None or x.subscribers is not None)]
    durations = [((x.ended_at or utcnow()) - x.started_at).total_seconds() for x in sessions]
    avg_list = [x.avg_viewers for x in sessions if x.samples]
    first_growth = next((g for g in growth if g["followers"] is not None or g["subscribers"] is not None), None)
    last_growth = growth[-1] if growth else None
    return {
        "streamer": s.to_dict(),
        "current": current.to_dict() | {"avg_viewers": current.avg_viewers, "live_viewers": viewers[-1]["v"] if viewers else 0} if current else None,
        "summary": {"streams": len(sessions), "peak": max((x.peak_viewers for x in sessions), default=0),
                    "avg_viewers": round(sum(avg_list) / len(avg_list)) if avg_list else 0,
                    "total_hours": round(sum(durations) / 3600, 1), "avg_duration_min": round(sum(durations) / len(durations) / 60) if durations else 0,
                    "followers": s.followers, "subscribers": s.subscribers,
                    "follower_growth": (last_growth["followers"] - first_growth["followers"]) if first_growth and last_growth and first_growth["followers"] is not None and last_growth["followers"] is not None else None,
                    "subscriber_growth": (last_growth["subscribers"] - first_growth["subscribers"]) if first_growth and last_growth and first_growth["subscribers"] is not None and last_growth["subscribers"] is not None else None,
                    "checkins": sum(x.checkins for x in sessions)},
        "sessions": [x.to_dict() | {"avg_viewers": x.avg_viewers} for x in sessions[:100]],
        "viewers": viewers[-2000:], "growth": growth[-500:],
    }


# ───────────────────────── Leaderboards & Achievements ─────────────────────────
@router.get("/leaderboard")
async def leaderboard(by: str = Query("xp", pattern="^(xp|coins|voice_minutes|messages|invites)$"), page: int = 1, ctx: Ctx = Depends(staff)):
    off, lim = paginate(page, 25)
    col = getattr(Member, by)
    async with SessionLocal() as db:
        rows = (await db.execute(select(Member).where(Member.guild_id == ctx.guild.id, Member.in_guild.is_(True))
                                 .order_by(col.desc()).offset(off).limit(lim))).scalars().all()
        total = (await db.execute(select(func.count()).select_from(Member).where(Member.guild_id == ctx.guild.id, Member.in_guild.is_(True)))).scalar_one()
        sums = (await db.execute(select(func.sum(Member.coins), func.sum(Member.xp), func.count()).where(Member.guild_id == ctx.guild.id))).one()
    return {"items": [{"rank": off + i + 1, "user": user_json(ctx.guild.get_member(r.user_id), r.user_id) | {"name": r.display_name or r.username or str(r.user_id)},
                       "level": r.level, "xp": r.xp, "coins": r.coins, "voice_minutes": r.voice_minutes, "messages": r.messages, "invites": r.invites}
                      for i, r in enumerate(rows)], "total": total, "page": page,
            "economy": {"total_coins": int(sums[0] or 0), "total_xp": int(sums[1] or 0), "profiles": sums[2]}}


@router.get("/achievements")
async def achievement_stats(ctx: Ctx = Depends(staff)):
    async with SessionLocal() as db:
        counts = dict((await db.execute(select(MemberAchievement.key, func.count()).where(MemberAchievement.guild_id == ctx.guild.id)
                                        .group_by(MemberAchievement.key))).all())
        members = (await db.execute(select(func.count()).select_from(Member).where(Member.guild_id == ctx.guild.id))).scalar_one()
    lang = (await config.get(ctx.guild.id, "general")).get("language", "de")
    return {"members": members, "items": [{"key": a.key, "emoji": a.emoji, "target": a.target, "unlocked": counts.get(a.key, 0),
                                           "name": i18n.t(lang, f"ach.{a.key}.name"), "description": i18n.t(lang, f"ach.{a.key}.desc")}
                                          for a in achievements.ACHIEVEMENTS]}


# ───────────────────────── Integrationen ─────────────────────────
@router.get("/integrations")
async def integrations(ctx: Ctx = Depends(admin)):
    return {"items": await public_status(ctx.guild.id)}


@router.put("/integrations/{provider}")
async def set_integration(provider: str, body: dict = Body(...), ctx: Ctx = Depends(admin)):
    if provider not in PROVIDERS:
        raise HTTPException(404, "Unbekannte Integration")
    await save_credentials(ctx.guild.id, provider, {k: str(v) for k, v in body.items() if isinstance(v, (str, int))}, ctx.user_id)
    await audit(ctx.guild.id, ctx.user_id, ctx.actor_name, "integration.update", provider)  # Werte werden bewusst NICHT geloggt
    return {"items": await public_status(ctx.guild.id)}


# ───────────────────────── Command-Berechtigungen ─────────────────────────
@router.get("/commands")
async def commands_list(ctx: Ctx = Depends(admin)):
    from app.bot.bot import module_of
    bot = get_bot()
    async with SessionLocal() as db:
        perms = {p.command: p for p in (await db.execute(select(CommandPermission).where(CommandPermission.guild_id == ctx.guild.id))).scalars()}
    out = []
    for c in sorted(bot.tree.get_commands(), key=lambda c: c.name):
        p = perms.get(c.name)
        sub = [s.qualified_name for s in c.walk_commands()] if isinstance(c, discord.app_commands.Group) else []
        default = getattr(c, "default_permissions", None)
        out.append({"name": c.name, "description": getattr(c, "description", "") or "Kontextmenü", "module": module_of(c if not sub else next(c.walk_commands())),
                    "type": "context" if isinstance(c, discord.app_commands.ContextMenu) else "slash", "subcommands": sub,
                    "default_permissions": [n for n, v in default if v] if default else [],
                    "enabled": p.enabled if p else True, "allowed_role_ids": p.allowed_role_ids if p else [], "denied_role_ids": p.denied_role_ids if p else [],
                    "allowed_user_ids": p.allowed_user_ids if p else [], "allowed_channel_ids": p.allowed_channel_ids if p else [],
                    "denied_channel_ids": p.denied_channel_ids if p else []})
    return {"items": out}


@router.put("/commands/{name}")
async def set_command_permission(name: str, body: dict = Body(...), ctx: Ctx = Depends(admin)):
    bot = get_bot()
    if name not in {c.name for c in bot.tree.get_commands()}:
        raise HTTPException(404, "Unbekannter Command")
    vctx = await validation_ctx(ctx.guild)
    from app.core.schema import F

    def ids(key: str, t: str) -> list[str]:
        try:
            return clean_value(F(key, t, key, []), body.get(key) or [], vctx)
        except ValueError as exc:
            raise HTTPException(422, {"message": str(exc), "errors": {key: str(exc)}})

    async with session_scope() as db:
        p = await db.get(CommandPermission, (ctx.guild.id, name))
        if p is None:
            p = CommandPermission(guild_id=ctx.guild.id, command=name)
            db.add(p)
        p.enabled = bool(body.get("enabled", True))
        p.allowed_role_ids, p.denied_role_ids = ids("allowed_role_ids", "roles"), ids("denied_role_ids", "roles")
        p.allowed_user_ids = ids("allowed_user_ids", "users")
        p.allowed_channel_ids, p.denied_channel_ids = ids("allowed_channel_ids", "channels"), ids("denied_channel_ids", "channels")
    bot.tree.invalidate_permissions(ctx.guild.id)
    await audit(ctx.guild.id, ctx.user_id, ctx.actor_name, "command.permissions", f"/{name}")
    return {"ok": True}


# ───────────────────────── Setup & Gangs ─────────────────────────
@router.post("/setup/auto")
async def auto_setup(ctx: Ctx = Depends(admin)):
    if ctx.member is None:
        raise HTTPException(403, "Du musst Mitglied des Servers sein")
    try:
        lines = await _cog("Onboarding").auto_setup(ctx.guild, ctx.member)
    except UserError as exc:
        raise _user_err(exc)
    except discord.Forbidden:
        raise HTTPException(403, "Dem Bot fehlen Berechtigungen (Kanäle/Rollen verwalten)")
    config.invalidate(ctx.guild.id)
    return {"ok": True, "summary": [l.replace("**", "") for l in lines]}


@router.get("/setup/status")
async def setup_status(ctx: Ctx = Depends(staff)):
    checks = {
        "welcome": bool((await config.get(ctx.guild.id, "welcome")).get("channel")),
        "streamer": bool((await config.get(ctx.guild.id, "streamer")).get("default_channel")),
        "tickets": bool((await config.get(ctx.guild.id, "tickets")).get("panel_channel")),
        "logs": bool((await config.get(ctx.guild.id, "moderation")).get("log_channel")),
    }
    async with SessionLocal() as db:
        checks["streamer_connected"] = bool((await db.execute(select(Streamer.id).where(Streamer.guild_id == ctx.guild.id).limit(1))).first())
    me = ctx.guild.me
    checks["bot_role_top"] = me.top_role.position >= max((r.position for r in ctx.guild.roles if not r.managed), default=0)
    return checks


@router.get("/levels/info")
async def levels_info(ctx: Ctx = Depends(staff)):
    """Verständliche Zusammenfassung, wie XP funktionieren (für die Level-Seite)."""
    from app.db.models import LevelReward
    from app.services.progression import total_for_level
    lv = await config.get(ctx.guild.id, "levels")
    async with SessionLocal() as db:
        rewards = (await db.execute(select(LevelReward).where(LevelReward.guild_id == ctx.guild.id).order_by(LevelReward.level))).scalars().all()
    role = lambda rid: ctx.guild.get_role(rid)  # noqa: E731
    return {"xp_min": lv.get("xp_min"), "xp_max": lv.get("xp_max"), "cooldown": lv.get("cooldown"), "voice_xp": lv.get("voice_xp"),
            "reaction_xp": lv.get("reaction_xp"), "stream_xp": lv.get("stream_xp"), "voice_needs_others": lv.get("voice_needs_others"),
            "table": [{"level": l, "xp": total_for_level(l)} for l in (1, 5, 10, 15, 25, 50, 75, 100)],
            "rewards": [{"level": r.level, "role": role(r.role_id).name if role(r.role_id) else "gelöschte Rolle",
                         "color": str(role(r.role_id).color) if role(r.role_id) and role(r.role_id).color.value else None} for r in rewards]}


@router.get("/gangs")
async def gangs(ctx: Ctx = Depends(staff)):
    from app.bot.modules.gangs import gang_power
    from app.db.models import Gang
    async with SessionLocal() as db:
        rows = (await db.execute(select(Gang).where(Gang.guild_id == ctx.guild.id))).scalars().all()
    out = []
    for g in rows:
        power, count = await gang_power(g.id)
        out.append(g.to_dict() | {"power": power, "members": count, "leader": user_json(ctx.guild.get_member(g.leader_id), g.leader_id)})
    return {"items": sorted(out, key=lambda x: -x["power"])}


@router.delete("/gangs/{gang_id}")
async def delete_gang(gang_id: int, ctx: Ctx = Depends(admin)):
    from app.db.models import Gang
    async with session_scope() as db:
        g = await db.get(Gang, gang_id)
        if g is None or g.guild_id != ctx.guild.id:
            raise HTTPException(404, "Gang nicht gefunden")
        name = g.name
        await db.delete(g)
    await audit(ctx.guild.id, ctx.user_id, ctx.actor_name, "gang.delete", name)
    return {"ok": True}


# ───────────────────────── Backups ─────────────────────────
@router.get("/backups")
async def list_backups(ctx: Ctx = Depends(admin)):
    async with SessionLocal() as db:
        rows = (await db.execute(select(Backup).where(Backup.guild_id == ctx.guild.id).order_by(Backup.created_at.desc()).limit(100))).scalars().all()
    return {"items": [b.to_dict() for b in rows]}


@router.post("/backups")
async def create_backup(ctx: Ctx = Depends(admin)):
    b = await backups.export_guild(ctx.guild.id, created_by=ctx.user_id)
    await audit(ctx.guild.id, ctx.user_id, ctx.actor_name, "backup.create", b.filename)
    return b.to_dict()


async def _backup(ctx: Ctx, backup_id: int) -> Backup:
    async with SessionLocal() as db:
        b = await db.get(Backup, backup_id)
    if b is None or b.guild_id != ctx.guild.id:
        raise HTTPException(404, "Backup nicht gefunden")
    return b


@router.get("/backups/{backup_id}/download")
async def download_backup(backup_id: int, ctx: Ctx = Depends(admin)):
    b = await _backup(ctx, backup_id)
    path = backups.backup_dir() / b.filename
    if not path.exists():
        raise HTTPException(404, "Datei fehlt")
    await audit(ctx.guild.id, ctx.user_id, ctx.actor_name, "backup.download", b.filename)
    return FileResponse(path, filename=b.filename, media_type="application/gzip")


@router.post("/backups/{backup_id}/restore")
async def restore_backup(backup_id: int, body: dict = Body(default={}), ctx: Ctx = Depends(admin)):
    if body.get("confirm") != "RESTORE":
        raise HTTPException(422, "Bestätigung fehlt")
    b = await _backup(ctx, backup_id)
    # Sicherheitsnetz: vor jedem Restore automatisch den aktuellen Stand sichern
    await backups.export_guild(ctx.guild.id, created_by=ctx.user_id)
    try:
        counts = await backups.restore_guild(ctx.guild.id, b.id)
    except (ValueError, FileNotFoundError) as exc:
        raise HTTPException(400, str(exc))
    config.invalidate(ctx.guild.id)
    get_bot().tree.invalidate_permissions(ctx.guild.id)
    invalidate_quests(ctx.guild.id)
    await audit(ctx.guild.id, ctx.user_id, ctx.actor_name, "backup.restore", b.filename, {"rows": sum(counts.values())})
    return {"ok": True, "rows": counts}


@router.delete("/backups/{backup_id}")
async def delete_backup(backup_id: int, ctx: Ctx = Depends(admin)):
    b = await _backup(ctx, backup_id)
    await backups.delete_backup(b)
    await audit(ctx.guild.id, ctx.user_id, ctx.actor_name, "backup.delete", b.filename)
    return {"ok": True}
