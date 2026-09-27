"""API: Mitglieder & Profile, Moderation (Cases, Aktionen, Notes), Staff-System, Logs, Audit-Log, Raids, Fehler."""
from __future__ import annotations

from datetime import timedelta

import discord
from fastapi import APIRouter, Body, Depends, HTTPException, Query
from sqlalchemy import func, or_, select

from app.core.errors import UserError
from app.core.records import audit
from app.core.timeutil import parse_duration
from app.db.base import SessionLocal, session_scope, utcnow
from app.db.models import (Application, AuditEntry, Badge, ErrorLog, InventoryItem, LogEntry, Member, MemberAchievement, MemberBadge,
                           ModCase, RaidEvent, ShopItem, StaffNote, Ticket, UserNote)
from app.runtime import get_bot
from app.services import achievements, moderation as mod, progression
from app.services.members import ensure_member
from app.web.common import paginate, role_json, user_json
from app.web.deps import Ctx, admin, staff

router = APIRouter(prefix="/api/g/{guild_id}")


def _err(exc: UserError) -> HTTPException:
    from app.core.i18n import i18n
    return HTTPException(400, i18n.t("de", exc.key, **exc.kw))


# ───────────────────────── Mitglieder ─────────────────────────
@router.get("/members")
async def members(q: str = "", role: str = "", sort: str = "joined", page: int = 1, per_page: int = 25, ctx: Ctx = Depends(staff)):
    g = ctx.guild
    items = [m for m in g.members]
    if q:
        ql = q.lower()
        items = [m for m in items if ql in m.name.lower() or ql in m.display_name.lower() or q == str(m.id)]
    if role.isdigit():
        items = [m for m in items if m.get_role(int(role))]
    keyf = {"joined": lambda m: m.joined_at or utcnow(), "name": lambda m: m.display_name.lower(), "created": lambda m: m.created_at}.get(sort)
    items.sort(key=keyf or (lambda m: m.joined_at or utcnow()), reverse=sort in ("joined", "created"))
    off, lim = paginate(page, per_page)
    chunk = items[off:off + lim]
    async with SessionLocal() as db:
        rows = {r.user_id: r for r in (await db.execute(select(Member).where(Member.guild_id == g.id, Member.user_id.in_([m.id for m in chunk])))).scalars()}
    out = []
    for m in chunk:
        r = rows.get(m.id)
        out.append({**user_json(m), "joined_at": m.joined_at.isoformat() if m.joined_at else None, "created_at": m.created_at.isoformat(),
                    "top_role": m.top_role.name if not m.top_role.is_default() else None, "top_role_color": str(m.top_role.color) if m.top_role.color.value else None,
                    "timed_out": m.is_timed_out(), "level": r.level if r else 0, "xp": r.xp if r else 0, "coins": r.coins if r else 0,
                    "messages": r.messages if r else 0, "status": str(m.status)})
    return {"items": out, "total": len(items), "page": page, "per_page": lim}


@router.get("/members/{user_id}")
async def member_profile(user_id: int, ctx: Ctx = Depends(staff)):
    g = ctx.guild
    bot = get_bot()
    member = g.get_member(user_id)
    user = member or await _fetch_user(bot, user_id)
    async with session_scope() as db:
        row = await ensure_member(db, g.id, member or user_id)
    async with SessionLocal() as db:
        cases = (await db.execute(select(ModCase).where(ModCase.guild_id == g.id, ModCase.user_id == user_id).order_by(ModCase.created_at.desc()))).scalars().all()
        notes = (await db.execute(select(UserNote).where(UserNote.guild_id == g.id, UserNote.user_id == user_id).order_by(UserNote.created_at.desc()))).scalars().all()
        tickets = (await db.execute(select(Ticket).where(Ticket.guild_id == g.id, Ticket.opener_id == user_id).order_by(Ticket.created_at.desc()).limit(20))).scalars().all()
        apps = (await db.execute(select(Application).where(Application.guild_id == g.id, Application.user_id == user_id).order_by(Application.created_at.desc()))).scalars().all()
        unlocked = set((await db.execute(select(MemberAchievement.key).where(MemberAchievement.guild_id == g.id, MemberAchievement.user_id == user_id))).scalars())
        inv = (await db.execute(select(InventoryItem, ShopItem).join(ShopItem, ShopItem.id == InventoryItem.item_id).where(
            InventoryItem.guild_id == g.id, InventoryItem.user_id == user_id))).all()
        higher = (await db.execute(select(func.count()).select_from(Member).where(Member.guild_id == g.id, Member.xp > row.xp, Member.in_guild.is_(True)))).scalar_one()
        all_badges = (await db.execute(select(Badge).where(Badge.guild_id == g.id))).scalars().all()
        manual = set((await db.execute(select(MemberBadge.badge_id).where(MemberBadge.guild_id == g.id, MemberBadge.user_id == user_id))).scalars())
    badges = await achievements.badges_for(g, row, member)
    level, into, need = progression.level_from_xp(row.xp)
    return {
        "user": user_json(user, user_id), "in_guild": member is not None,
        "joined_at": member.joined_at.isoformat() if member and member.joined_at else (row.joined_at.isoformat() if row.joined_at else None),
        "first_joined_at": row.first_joined_at.isoformat() if row.first_joined_at else None,
        "created_at": (user.created_at.isoformat() if user else None),
        "roles": [role_json(r, g.me) for r in reversed(member.roles) if not r.is_default()] if member else [],
        "timed_out_until": member.timed_out_until.isoformat() if member and member.is_timed_out() and member.timed_out_until else None,
        "premium_since": member.premium_since.isoformat() if member and member.premium_since else None,
        "stats": {"level": level, "xp": row.xp, "xp_into": into, "xp_need": need, "rank": higher + 1, "coins": row.coins,
                  "coins_earned": row.coins_earned, "messages": row.messages, "voice_minutes": row.voice_minutes, "reactions": row.reactions,
                  "invites": row.invites, "daily_streak": row.daily_streak, "best_streak": row.daily_best_streak,
                  "giveaways_won": row.giveaways_won, "events_won": row.events_won, "stream_checkins": row.stream_checkins},
        "warnings_active": sum(1 for c in cases if c.action == "warn" and c.active),
        "cases": [c.to_dict() for c in cases], "notes": [n.to_dict() for n in notes],
        "tickets": [{"id": t.id, "number": t.number, "subject": t.subject[:100], "status": t.status, "created_at": t.created_at.isoformat()} for t in tickets],
        "applications": [{"id": a.id, "position": a.position, "status": a.status, "created_at": a.created_at.isoformat()} for a in apps],
        "achievements": achievements.progress_for(row, unlocked),
        "badges": [{"id": b.id, "name": b.name, "emoji": b.emoji} for b in badges],
        "manual_badges": [str(b) for b in manual],
        "all_badges": [{"id": str(b.id), "name": b.name, "emoji": b.emoji, "auto_rule": b.auto_rule} for b in all_badges],
        "inventory": [{"name": it.name, "emoji": it.emoji, "quantity": i.quantity} for i, it in inv],
        "games": row.games or {}, "afk": row.afk_message if row.afk_since else None,
        "has_birthday": row.birthday_day is not None,  # Datum bleibt privat
        "invited_by": str(row.invited_by) if row.invited_by else None,
    }


async def _fetch_user(bot, user_id: int):
    try:
        return await bot.fetch_user(user_id)
    except discord.HTTPException:
        return None


@router.post("/members/{user_id}/notes")
async def add_note(user_id: int, body: dict = Body(...), ctx: Ctx = Depends(staff)):
    content = str(body.get("content", "")).strip()
    if not 1 <= len(content) <= 2000:
        raise HTTPException(422, "Notiz muss 1–2000 Zeichen haben")
    async with session_scope() as db:
        n = UserNote(guild_id=ctx.guild.id, user_id=user_id, author_id=ctx.user_id, author_name=ctx.actor_name, content=content)
        db.add(n)
        await db.flush()
        data = n.to_dict()
    await audit(ctx.guild.id, ctx.user_id, ctx.actor_name, "note.add", str(user_id))
    return data


@router.delete("/members/{user_id}/notes/{note_id}")
async def delete_note(user_id: int, note_id: int, ctx: Ctx = Depends(staff)):
    async with session_scope() as db:
        n = await db.get(UserNote, note_id)
        if not n or n.guild_id != ctx.guild.id or n.user_id != user_id:
            raise HTTPException(404, "Notiz nicht gefunden")
        if n.author_id != ctx.user_id and ctx.level != "admin":
            raise HTTPException(403, "Nur eigene Notizen löschen")
        await db.delete(n)
    return {"ok": True}


@router.post("/members/{user_id}/economy")
async def member_economy(user_id: int, body: dict = Body(...), ctx: Ctx = Depends(admin)):
    """XP / Coins setzen oder anpassen."""
    bot = get_bot()
    field, mode = body.get("field"), body.get("mode", "add")
    try:
        value = int(body.get("value"))
    except (TypeError, ValueError):
        raise HTTPException(422, "Ungültiger Wert")
    if field not in ("xp", "coins") or mode not in ("add", "set") or abs(value) > 1_000_000_000:
        raise HTTPException(422, "Ungültige Anfrage")
    member = ctx.guild.get_member(user_id)
    if field == "xp":
        if mode == "set":
            await progression.set_xp(bot, ctx.guild, user_id, max(0, value))
        else:
            await progression.add_xp(bot, ctx.guild, member or user_id, value, use_multiplier=False, announce=False)
    else:
        async with session_scope() as db:
            row = await ensure_member(db, ctx.guild.id, member or user_id)
            delta = value - row.coins if mode == "set" else value
        await progression.add_coins(ctx.guild.id, member or user_id, delta, reason="dashboard")
    await audit(ctx.guild.id, ctx.user_id, ctx.actor_name, f"{field}.{mode}", str(user_id), {"value": value})
    return {"ok": True}


@router.post("/members/{user_id}/badges")
async def member_badge(user_id: int, body: dict = Body(...), ctx: Ctx = Depends(admin)):
    bid = int(body.get("badge_id", 0))
    grant = bool(body.get("grant", True))
    async with session_scope() as db:
        b = await db.get(Badge, bid)
        if not b or b.guild_id != ctx.guild.id:
            raise HTTPException(404, "Badge nicht gefunden")
        existing = await db.get(MemberBadge, (ctx.guild.id, user_id, bid))
        if grant and not existing:
            db.add(MemberBadge(guild_id=ctx.guild.id, user_id=user_id, badge_id=bid, granted_by=ctx.user_id))
        elif not grant and existing:
            await db.delete(existing)
    await audit(ctx.guild.id, ctx.user_id, ctx.actor_name, "badge.grant" if grant else "badge.revoke", str(user_id), {"badge": bid})
    return {"ok": True}


@router.post("/members/{user_id}/roles")
async def member_roles(user_id: int, body: dict = Body(...), ctx: Ctx = Depends(admin)):
    member = ctx.guild.get_member(user_id)
    role = ctx.guild.get_role(int(body.get("role_id", 0)))
    if not member or not role:
        raise HTTPException(404, "Mitglied oder Rolle nicht gefunden")
    _check_role_editable(ctx, role)
    if body.get("add", True):
        await member.add_roles(role, reason=f"Dashboard: {ctx.actor_name}")
    else:
        await member.remove_roles(role, reason=f"Dashboard: {ctx.actor_name}")
    await audit(ctx.guild.id, ctx.user_id, ctx.actor_name, "member.roles", f"{member} {'+' if body.get('add', True) else '-'}{role.name}")
    return {"ok": True}


def _check_role_editable(ctx: Ctx, role: discord.Role) -> None:
    g = ctx.guild
    if role.is_default() or role.managed or role >= g.me.top_role:
        raise HTTPException(403, "Diese Rolle kann der Bot nicht verwalten (Hierarchie)")
    if not ctx.is_owner and ctx.member and ctx.member.id != g.owner_id and role >= ctx.member.top_role:
        raise HTTPException(403, "Die Rolle liegt über deiner höchsten Rolle")


# ───────────────────────── Moderation ─────────────────────────
@router.get("/cases")
async def cases(q: str = "", action: str = "", user_id: str = "", active: str = "", page: int = 1, per_page: int = 25, ctx: Ctx = Depends(staff)):
    off, lim = paginate(page, per_page)
    async with SessionLocal() as db:
        qry = select(ModCase).where(ModCase.guild_id == ctx.guild.id)
        if action:
            qry = qry.where(ModCase.action == action)
        if user_id.isdigit():
            qry = qry.where(ModCase.user_id == int(user_id))
        if active in ("1", "true"):
            qry = qry.where(ModCase.active.is_(True))
        if q:
            num = int(q.lstrip("#")) if q.lstrip("#").isdigit() and len(q) < 10 else -1
            qry = qry.where(or_(ModCase.reason.ilike(f"%{q}%"), ModCase.user_name.ilike(f"%{q}%"), ModCase.moderator_name.ilike(f"%{q}%"),
                                ModCase.case_number == num))
        total = (await db.execute(select(func.count()).select_from(qry.subquery()))).scalar_one()
        rows = (await db.execute(qry.order_by(ModCase.case_number.desc()).offset(off).limit(lim))).scalars().all()
    return {"items": [c.to_dict() for c in rows], "total": total, "page": page, "per_page": lim}


@router.patch("/cases/{number}")
async def edit_case(number: int, body: dict = Body(...), ctx: Ctx = Depends(staff)):
    async with session_scope() as db:
        c = (await db.execute(select(ModCase).where(ModCase.guild_id == ctx.guild.id, ModCase.case_number == number))).scalar_one_or_none()
        if c is None:
            raise HTTPException(404, "Case nicht gefunden")
        if "reason" in body:
            c.reason = str(body["reason"])[:2000]
        if "active" in body:
            c.active = bool(body["active"])
        c.updated_at = utcnow()
        data = c.to_dict()
    await audit(ctx.guild.id, ctx.user_id, ctx.actor_name, "case.edit", f"#{number}", {k: body[k] for k in ("reason", "active") if k in body})
    return data


@router.post("/moderation/action")
async def mod_action(body: dict = Body(...), ctx: Ctx = Depends(staff)):
    action = body.get("action")
    if action not in ("warn", "timeout", "untimeout", "kick", "ban", "unban", "softban"):
        raise HTTPException(422, "Unbekannte Aktion")
    if ctx.member is None:
        raise HTTPException(403, "Du musst Mitglied des Servers sein, um Aktionen auszuführen")
    perms = ctx.member.guild_permissions
    needed = {"warn": perms.moderate_members, "timeout": perms.moderate_members, "untimeout": perms.moderate_members,
              "kick": perms.kick_members, "ban": perms.ban_members, "unban": perms.ban_members, "softban": perms.ban_members}[action]
    if not needed and not ctx.is_owner and ctx.level != "admin":
        raise HTTPException(403, "Dir fehlt die Discord-Berechtigung für diese Aktion")
    uid = str(body.get("user_id", ""))
    if not uid.isdigit():
        raise HTTPException(422, "Ungültige User-ID")
    target = ctx.guild.get_member(int(uid)) or await _fetch_user(get_bot(), int(uid))
    if target is None:
        raise HTTPException(404, "User nicht gefunden")
    duration = parse_duration(body.get("duration")) if body.get("duration") else None
    try:
        case = await mod.execute(ctx.guild, action, target, ctx.member, str(body.get("reason", ""))[:500], duration=duration,
                                 delete_days=int(body.get("delete_days") or 0), source="dashboard")
    except UserError as exc:
        raise _err(exc)
    return case.to_dict()


@router.get("/moderation/active")
async def active_punishments(ctx: Ctx = Depends(staff)):
    g = ctx.guild
    timeouts = [{**user_json(m), "until": m.timed_out_until.isoformat()} for m in g.members if m.is_timed_out() and m.timed_out_until]
    async with SessionLocal() as db:
        bans = (await db.execute(select(ModCase).where(ModCase.guild_id == g.id, ModCase.action == "ban", ModCase.active.is_(True))
                                 .order_by(ModCase.created_at.desc()).limit(100))).scalars().all()
        warns = (await db.execute(select(ModCase.user_id, ModCase.user_name, func.count()).where(
            ModCase.guild_id == g.id, ModCase.action == "warn", ModCase.active.is_(True)).group_by(ModCase.user_id, ModCase.user_name)
            .order_by(func.count().desc()).limit(50))).all()
    return {"timeouts": timeouts, "bans": [b.to_dict() for b in bans],
            "warnings": [{"user_id": str(u), "user_name": n, "count": c} for u, n, c in warns]}


# ───────────────────────── Staff-System ─────────────────────────
@router.get("/staff")
async def staff_overview(days: int = 30, ctx: Ctx = Depends(staff)):
    g = ctx.guild
    since = utcnow() - timedelta(days=max(1, min(days, 365)))
    async with SessionLocal() as db:
        mod_rows = (await db.execute(select(ModCase.moderator_id, ModCase.moderator_name, ModCase.action, func.count()).where(
            ModCase.guild_id == g.id, ModCase.created_at >= since, ModCase.source.in_(["command", "dashboard", "discord"]))
            .group_by(ModCase.moderator_id, ModCase.moderator_name, ModCase.action))).all()
        tickets = (await db.execute(select(Ticket).where(Ticket.guild_id == g.id, Ticket.created_at >= since))).scalars().all()
        open_tickets = (await db.execute(select(func.count()).select_from(Ticket).where(Ticket.guild_id == g.id, Ticket.status == "open"))).scalar_one()
        notes = (await db.execute(select(StaffNote).where(StaffNote.guild_id == g.id).order_by(StaffNote.pinned.desc(), StaffNote.created_at.desc()).limit(100))).scalars().all()
        warns = (await db.execute(select(func.count()).select_from(ModCase).where(ModCase.guild_id == g.id, ModCase.action == "warn", ModCase.created_at >= since))).scalar_one()
    people: dict[int, dict] = {}

    def person(uid: int, name: str) -> dict:
        if uid not in people:
            m = g.get_member(uid)
            people[uid] = {"user": user_json(m, uid) or {"id": str(uid), "name": name}, "actions": {}, "mod_total": 0,
                           "tickets_claimed": 0, "tickets_closed": 0, "avg_response_min": None, "_resp": []}
        return people[uid]

    for uid, name, action, n in mod_rows:
        p = person(uid, name)
        p["actions"][action] = n
        p["mod_total"] += n
    closed_durations = []
    for t in tickets:
        if t.claimed_by:
            p = person(t.claimed_by, t.claimed_name or "")
            p["tickets_claimed"] += 1
            if t.first_response_at:
                p["_resp"].append((t.first_response_at - t.created_at).total_seconds() / 60)
        if t.closed_by and t.closed_by != t.opener_id:
            person(t.closed_by, t.closed_by_name or "")["tickets_closed"] += 1
        if t.closed_at:
            closed_durations.append((t.closed_at - t.created_at).total_seconds() / 60)
    for p in people.values():
        resp = p.pop("_resp")
        p["avg_response_min"] = round(sum(resp) / len(resp), 1) if resp else None
        p["activity"] = p["mod_total"] + p["tickets_claimed"] + p["tickets_closed"]
    from app.core.guild_config import config
    gen = await config.get(g.id, "general")
    staff_role_ids = set(gen.ids("staff_roles")) | set(gen.ids("admin_roles"))
    team = [m for m in g.members if not m.bot and ({r.id for r in m.roles} & staff_role_ids or m.guild_permissions.moderate_members)]
    return {
        "team": [{**user_json(m), "status": str(m.status), "top_role": m.top_role.name} for m in team[:100]],
        "people": sorted(people.values(), key=lambda p: -p["activity"]),
        "summary": {"open_tickets": open_tickets, "tickets_period": len(tickets), "closed_period": len(closed_durations),
                    "avg_ticket_minutes": round(sum(closed_durations) / len(closed_durations), 1) if closed_durations else None,
                    "mod_actions": sum(p["mod_total"] for p in people.values()), "warnings": warns,
                    "active_moderators": sum(1 for p in people.values() if p["mod_total"] > 0)},
        "notes": [n.to_dict() for n in notes],
    }


@router.post("/staff/notes")
async def add_staff_note(body: dict = Body(...), ctx: Ctx = Depends(staff)):
    content = str(body.get("content", "")).strip()
    if not 1 <= len(content) <= 2000:
        raise HTTPException(422, "Notiz muss 1–2000 Zeichen haben")
    async with session_scope() as db:
        n = StaffNote(guild_id=ctx.guild.id, author_id=ctx.user_id, author_name=ctx.actor_name, content=content, pinned=bool(body.get("pinned")))
        db.add(n)
        await db.flush()
        return n.to_dict()


@router.patch("/staff/notes/{note_id}")
async def pin_staff_note(note_id: int, body: dict = Body(...), ctx: Ctx = Depends(staff)):
    async with session_scope() as db:
        n = await db.get(StaffNote, note_id)
        if not n or n.guild_id != ctx.guild.id:
            raise HTTPException(404, "Notiz nicht gefunden")
        n.pinned = bool(body.get("pinned"))
        return n.to_dict()


@router.delete("/staff/notes/{note_id}")
async def delete_staff_note(note_id: int, ctx: Ctx = Depends(staff)):
    async with session_scope() as db:
        n = await db.get(StaffNote, note_id)
        if not n or n.guild_id != ctx.guild.id:
            raise HTTPException(404, "Notiz nicht gefunden")
        if n.author_id != ctx.user_id and ctx.level != "admin":
            raise HTTPException(403, "Nur eigene Notizen löschen")
        await db.delete(n)
    return {"ok": True}


# ───────────────────────── Logs / Audit / Raids / Fehler ─────────────────────────
@router.get("/logs")
async def logs(category: str = "", q: str = "", user_id: str = "", page: int = 1, per_page: int = 50, ctx: Ctx = Depends(staff)):
    off, lim = paginate(page, per_page)
    async with SessionLocal() as db:
        qry = select(LogEntry).where(LogEntry.guild_id == ctx.guild.id)
        if category:
            qry = qry.where(LogEntry.category == category)
        if user_id.isdigit():
            qry = qry.where(or_(LogEntry.user_id == int(user_id), LogEntry.target_id == int(user_id)))
        if q:
            qry = qry.where(or_(LogEntry.content.ilike(f"%{q}%"), LogEntry.user_name.ilike(f"%{q}%"), LogEntry.action.ilike(f"%{q}%")))
        total = (await db.execute(select(func.count()).select_from(qry.subquery()))).scalar_one()
        rows = (await db.execute(qry.order_by(LogEntry.created_at.desc()).offset(off).limit(lim))).scalars().all()
        cats = (await db.execute(select(LogEntry.category, func.count()).where(LogEntry.guild_id == ctx.guild.id).group_by(LogEntry.category))).all()
    return {"items": [r.to_dict() for r in rows], "total": total, "page": page, "per_page": lim, "categories": {c: n for c, n in cats}}


@router.get("/audit")
async def audit_log(q: str = "", page: int = 1, per_page: int = 50, ctx: Ctx = Depends(admin)):
    off, lim = paginate(page, per_page)
    async with SessionLocal() as db:
        qry = select(AuditEntry).where(AuditEntry.guild_id == ctx.guild.id)
        if q:
            qry = qry.where(or_(AuditEntry.action.ilike(f"%{q}%"), AuditEntry.actor_name.ilike(f"%{q}%"), AuditEntry.target.ilike(f"%{q}%")))
        total = (await db.execute(select(func.count()).select_from(qry.subquery()))).scalar_one()
        rows = (await db.execute(qry.order_by(AuditEntry.created_at.desc()).offset(off).limit(lim))).scalars().all()
    # Moderationsaktionen gehören ebenfalls ins Audit-Log
    async with SessionLocal() as db:
        cases = (await db.execute(select(ModCase).where(ModCase.guild_id == ctx.guild.id).order_by(ModCase.created_at.desc()).limit(lim))).scalars().all() if page == 1 and not q else []
    return {"items": [r.to_dict() for r in rows], "cases": [c.to_dict() for c in cases], "total": total, "page": page, "per_page": lim}


@router.get("/raids")
async def raids(ctx: Ctx = Depends(staff)):
    async with SessionLocal() as db:
        rows = (await db.execute(select(RaidEvent).where(RaidEvent.guild_id == ctx.guild.id).order_by(RaidEvent.started_at.desc()).limit(50))).scalars().all()
    cog = get_bot().get_cog("AntiRaid")
    return {"active": bool(cog and cog.in_lockdown(ctx.guild.id)), "items": [r.to_dict() for r in rows]}


@router.post("/raids/end")
async def end_raid(ctx: Ctx = Depends(admin)):
    cog = get_bot().get_cog("AntiRaid")
    ok = bool(cog and await cog.end(ctx.guild))
    await audit(ctx.guild.id, ctx.user_id, ctx.actor_name, "raid.end", "")
    return {"ok": ok}


@router.get("/errors")
async def guild_errors(resolved: bool = False, ctx: Ctx = Depends(admin)):
    async with SessionLocal() as db:
        rows = (await db.execute(select(ErrorLog).where(ErrorLog.guild_id == ctx.guild.id, ErrorLog.resolved.is_(resolved))
                                 .order_by(ErrorLog.created_at.desc()).limit(100))).scalars().all()
    return {"items": [r.to_dict() for r in rows]}


@router.post("/errors/{error_id}/resolve")
async def resolve_error(error_id: str, ctx: Ctx = Depends(admin)):
    async with session_scope() as db:
        e = await db.get(ErrorLog, error_id)
        if not e or e.guild_id != ctx.guild.id:
            raise HTTPException(404, "Fehler nicht gefunden")
        e.resolved, e.resolved_by = True, ctx.user_id
    return {"ok": True}
