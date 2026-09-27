"""Zentrale Moderationslogik – genutzt von Slash-Commands, Kontextmenüs, Automod, Anti-Raid und Dashboard."""
from __future__ import annotations

import logging
from datetime import timedelta
from typing import Any

import discord
from sqlalchemy import func, select

from app.core.embeds import theme
from app.core.errors import UserError
from app.core.events import bus
from app.core.guild_config import config
from app.core.i18n import i18n
from app.core.metrics import metrics
from app.core.records import audit, log_event
from app.core.timeutil import human_duration, ts
from app.db.base import session_scope, utcnow
from app.db.models import ModCase
from app.services.guilds import next_number

log = logging.getLogger("nova.moderation")

ACTION_ICONS = {
    "warn": "⚠️", "timeout": "⏳", "untimeout": "🔓", "kick": "👢", "ban": "🔨", "unban": "♻️",
    "softban": "🧹", "note": "📝", "unwarn": "↩️", "quarantine": "🔒", "unquarantine": "🔓",
}
MAX_TIMEOUT = 28 * 24 * 3600


class ModerationError(UserError):
    """Fehler mit Übersetzungs-Key für saubere Fehlermeldungen."""


def check_hierarchy(guild: discord.Guild, moderator: discord.abc.User, target: discord.abc.User) -> None:
    if target.id == moderator.id:
        raise ModerationError("mod.err_self")
    if target.id == guild.owner_id:
        raise ModerationError("mod.err_owner")
    if isinstance(target, discord.Member):
        me = guild.me
        if target.top_role >= me.top_role:
            raise ModerationError("mod.err_bot_hierarchy")
        if isinstance(moderator, discord.Member) and moderator.id != guild.owner_id and target.top_role >= moderator.top_role:
            raise ModerationError("mod.err_hierarchy")


async def create_case(guild: discord.Guild, action: str, target: discord.abc.User | int, moderator: discord.abc.User | None,
                      reason: str, duration: int | None = None, source: str = "command", active: bool = True) -> ModCase:
    target_id = target if isinstance(target, int) else target.id
    async with session_scope() as s:
        number = await next_number(s, guild.id, "next_case")
        case = ModCase(
            guild_id=guild.id, case_number=number, action=action, user_id=target_id,
            user_name=str(target)[:100] if not isinstance(target, int) else str(target_id),
            moderator_id=moderator.id if moderator else guild.me.id,
            moderator_name=str(moderator or guild.me)[:100], reason=reason or "", duration_seconds=duration,
            expires_at=utcnow() + timedelta(seconds=duration) if duration else None,
            active=active, source=source,
        )
        s.add(case)
        await s.flush()
        data = case.to_dict()
    metrics.incr(guild.id, "mod_actions")
    bus.publish(guild.id, "mod_action", data)
    return case


async def case_embed(guild: discord.Guild, case: ModCase, user: discord.abc.User | None = None) -> discord.Embed:
    _ = await i18n.for_guild(guild.id)
    lang = await i18n.lang(guild.id)
    th = await theme(guild)
    kind = {"warn": "warning", "ban": "error", "kick": "error", "softban": "error", "unban": "success", "untimeout": "success"}.get(case.action, "primary")
    e = th.embed(_("mod.case_title", number=case.case_number), kind=kind, icon=False)
    e.add_field(name=_("mod.f_user"), value=f"<@{case.user_id}>\n`{case.user_id}`", inline=True)
    e.add_field(name=_("mod.f_moderator"), value=f"<@{case.moderator_id}>", inline=True)
    e.add_field(name=_("mod.f_action"), value=f"{ACTION_ICONS.get(case.action, '•')} {_('mod.action.' + case.action)}", inline=True)
    e.add_field(name=_("mod.f_reason"), value=case.reason or _("mod.no_reason"), inline=False)
    if case.duration_seconds:
        e.add_field(name=_("mod.f_duration"), value=human_duration(case.duration_seconds, lang), inline=True)
        if case.expires_at:
            e.add_field(name=_("mod.f_expires"), value=ts(case.expires_at, "R"), inline=True)
    e.add_field(name=_("mod.f_date"), value=ts(case.created_at, "f"), inline=True)
    if user is not None:
        e.set_thumbnail(url=user.display_avatar.url)
    return e


async def send_modlog(guild: discord.Guild, embed: discord.Embed) -> None:
    cfg = await config.get(guild.id, "moderation")
    channel = guild.get_channel(cfg.id("log_channel") or 0)
    if channel is None:
        logcfg = await config.get(guild.id, "logging")
        if logcfg.enabled and "mod_action" in (logcfg.get("events") or []):
            channel = guild.get_channel(logcfg.id("default_channel") or 0)
    if isinstance(channel, (discord.TextChannel, discord.Thread)):
        try:
            await channel.send(embed=embed)
        except discord.HTTPException as exc:
            log.debug("Mod-Log nicht sendbar: %s", exc)


async def _dm(guild: discord.Guild, user: discord.abc.User, action: str, reason: str, duration: int | None) -> None:
    cfg = await config.get(guild.id, "moderation")
    if not cfg.get("dm_users", True) or user.bot:
        return
    _ = await i18n.for_guild(guild.id)
    lang = await i18n.lang(guild.id)
    th = await theme(guild)
    e = th.warning(_("mod.dm_title", server=guild.name), _("mod.dm_desc", action=_("mod.action." + action)))
    e.add_field(name=_("mod.f_reason"), value=reason or _("mod.no_reason"), inline=False)
    if duration:
        e.add_field(name=_("mod.f_duration"), value=human_duration(duration, lang))
    if cfg.get("appeal_info"):
        e.add_field(name=_("mod.appeal"), value=cfg["appeal_info"][:1000], inline=False)
    if guild.icon:
        e.set_thumbnail(url=guild.icon.url)
    try:
        await user.send(embed=e)
    except discord.HTTPException:
        pass


async def execute(guild: discord.Guild, action: str, target: discord.abc.User, moderator: discord.abc.User, reason: str = "",
                  *, duration: int | None = None, delete_days: int = 0, source: str = "command", escalate: bool = True) -> ModCase:
    """Führt eine Moderationsaktion aus, legt einen Case an, loggt und informiert."""
    cfg = await config.get(guild.id, "moderation")
    if cfg.get("require_reason") and not reason and source in ("command", "dashboard"):
        raise ModerationError("mod.err_reason_required")
    audit_reason = f"{moderator} | {reason or '-'}"[:512]
    member = target if isinstance(target, discord.Member) else guild.get_member(target.id)

    if action not in ("unban",) and member is not None:
        check_hierarchy(guild, moderator, member)
    if action in ("timeout", "untimeout", "warn", "kick") and member is None:
        raise ModerationError("mod.err_not_member")

    try:
        if action == "timeout":
            duration = min(max(duration or cfg.get("default_timeout", 30) * 60, 60), MAX_TIMEOUT)
            await member.timeout(timedelta(seconds=duration), reason=audit_reason)
            await _dm(guild, member, action, reason, duration)
        elif action == "untimeout":
            await member.timeout(None, reason=audit_reason)
        elif action == "warn":
            await _dm(guild, member, action, reason, None)
        elif action == "kick":
            await _dm(guild, member, action, reason, None)
            await member.kick(reason=audit_reason)
        elif action == "ban":
            await _dm(guild, target, action, reason, duration)
            await guild.ban(target, reason=audit_reason, delete_message_seconds=min(delete_days, 7) * 86400)
        elif action == "softban":
            await _dm(guild, target, action, reason, None)
            await guild.ban(target, reason=audit_reason, delete_message_seconds=max(1, min(delete_days or 1, 7)) * 86400)
            await guild.unban(target, reason="Softban")
        elif action == "unban":
            await guild.unban(target, reason=audit_reason)
        else:
            raise ModerationError("mod.err_unknown_action")
    except discord.Forbidden:
        raise ModerationError("mod.err_forbidden")
    except discord.NotFound:
        raise ModerationError("mod.err_not_found")

    case = await create_case(guild, action, target, moderator, reason, duration if action in ("timeout", "ban") else None,
                             source=source, active=action in ("warn", "timeout", "ban"))
    if action == "untimeout":
        await _deactivate(guild.id, target.id, "timeout")
    if action == "unban":
        await _deactivate(guild.id, target.id, "ban")

    await send_modlog(guild, await case_embed(guild, case, target))
    await log_event(guild.id, "moderation", action, user=moderator, target_id=target.id,
                    content=reason, details={"case": case.case_number, "target": str(target), "duration": duration})
    if source == "dashboard":
        await audit(guild.id, moderator.id, str(moderator), f"mod.{action}", f"{target} (Case #{case.case_number})", {"reason": reason})

    if action == "warn" and escalate:
        await _escalate(guild, member, moderator)
    return case


async def _deactivate(guild_id: int, user_id: int, action: str) -> None:
    async with session_scope() as s:
        rows = (await s.execute(select(ModCase).where(ModCase.guild_id == guild_id, ModCase.user_id == user_id,
                                                      ModCase.action == action, ModCase.active.is_(True)))).scalars().all()
        for r in rows:
            r.active = False
            r.updated_at = utcnow()


async def active_warns(guild_id: int, user_id: int) -> int:
    async with session_scope() as s:
        return (await s.execute(select(func.count()).select_from(ModCase).where(
            ModCase.guild_id == guild_id, ModCase.user_id == user_id, ModCase.action == "warn", ModCase.active.is_(True)))).scalar_one()


async def _escalate(guild: discord.Guild, member: discord.Member, moderator: discord.abc.User) -> None:
    cfg = await config.get(guild.id, "moderation")
    rules = sorted(cfg.get("warn_escalation") or [], key=lambda r: r.get("count", 0))
    if not rules:
        return
    count = await active_warns(guild.id, member.id)
    rule = next((r for r in rules if r.get("count") == count), None)
    if not rule:
        return
    _ = await i18n.for_guild(guild.id)
    reason = _("mod.escalation_reason", count=count)
    try:
        await execute(guild, rule["action"], member, guild.me, reason,
                      duration=(rule.get("minutes") or 60) * 60 if rule["action"] == "timeout" else None,
                      source="escalation", escalate=False)
    except ModerationError as exc:
        log.info("Eskalation für %s nicht möglich: %s", member, exc.key)
