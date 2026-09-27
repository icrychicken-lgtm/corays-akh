"""Giveaways: Bedingungen (Rolle, Account-/Serveralter), Bonus-Entries (Rollen, Invites, Aktivität, Early Join),
mehrere Gewinner, Reroll, Gewinner-DM, Historie. Buttons: Teilnehmen · Teilnehmer · Restzeit."""
from __future__ import annotations

import asyncio
import logging
import random
import re
from datetime import timedelta
from typing import Any

import discord
from discord import app_commands
from discord.ext import commands, tasks
from sqlalchemy import func, select

from app.bot.ui import BaseView, Paginator, chunk, confirm, handle_exception, reply
from app.core.embeds import fmt_num, theme
from app.core.errors import UserError
from app.core.events import bus
from app.core.guild_config import config
from app.core.i18n import i18n
from app.core.metrics import metrics
from app.core.records import log_event
from app.core.timeutil import parse_duration, ts
from app.db.base import SessionLocal, session_scope, utcnow
from app.db.models import Giveaway, GiveawayEntry, Member
from app.services import achievements, progression
from app.services.members import ensure_member
from app.services.notify import is_staff, notify

log = logging.getLogger("nova.giveaways")


class GiveawayButton(discord.ui.DynamicItem[discord.ui.Button], template=r"nova:gw:(?P<id>\d+):(?P<action>join|list|time)"):
    STYLE = {"join": (discord.ButtonStyle.success, "🎉"), "list": (discord.ButtonStyle.secondary, "👥"), "time": (discord.ButtonStyle.secondary, "⏰")}

    def __init__(self, gid: int, action: str, label: str | None = None, disabled: bool = False):
        style, emoji = self.STYLE[action]
        super().__init__(discord.ui.Button(label=label, style=style, emoji=emoji, custom_id=f"nova:gw:{gid}:{action}", disabled=disabled))
        self.gid, self.action = gid, action

    @classmethod
    async def from_custom_id(cls, interaction, item, match: re.Match[str], /):
        return cls(int(match["id"]), match["action"])

    async def callback(self, interaction: discord.Interaction):
        cog: Giveaways = interaction.client.get_cog("Giveaways")  # type: ignore[assignment]
        try:
            if self.action == "join":
                await cog.toggle_join(interaction, self.gid)
            elif self.action == "list":
                await cog.show_participants(interaction, self.gid)
            else:
                await cog.show_time(interaction, self.gid)
        except Exception as exc:  # noqa: BLE001
            await handle_exception(interaction, exc, f"giveaway_{self.action}")


class LeaveView(BaseView):
    def __init__(self, cog: "Giveaways", gid: int, owner_id: int, label: str):
        super().__init__(owner_id=owner_id, timeout=60)
        self.cog, self.gid = cog, gid
        self.leave.label = label

    @discord.ui.button(style=discord.ButtonStyle.danger, emoji="🚪")
    async def leave(self, interaction: discord.Interaction, _b: discord.ui.Button):
        async with session_scope() as s:
            e = await s.get(GiveawayEntry, (self.gid, interaction.user.id))
            if e:
                await s.delete(e)
        _ = await i18n.for_guild(interaction.guild_id)
        th = await theme(interaction.guild)
        await interaction.response.edit_message(embed=th.info(_("gw.left_title"), _("gw.left")), view=None)
        self.cog.schedule_refresh(self.gid)


@app_commands.guild_only()
class Giveaways(commands.Cog):
    module = "giveaways"
    help_category = "community"

    giveaway = app_commands.Group(name="giveaway", description="Giveaways verwalten", guild_only=True,
                                  default_permissions=discord.Permissions(manage_events=True))

    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self._refresh: dict[int, asyncio.Task] = {}
        self.end_loop.start()

    def cog_unload(self):
        self.end_loop.cancel()

    # ── Darstellung ──
    async def embed(self, guild: discord.Guild, g: Giveaway, entries: int | None = None, participants: int | None = None) -> discord.Embed:
        _ = await i18n.for_guild(guild.id)
        th = await theme(guild)
        if entries is None or participants is None:
            async with SessionLocal() as s:
                participants, entries = (await s.execute(select(func.count(), func.coalesce(func.sum(GiveawayEntry.entries), 0))
                                                         .where(GiveawayEntry.giveaway_id == g.id))).one()
        e = th.embed(f"🎁 {g.prize}", g.description or None, kind="success" if g.ended else "primary", icon=False)
        if g.ended:
            winners = " ".join(f"<@{w}>" for w in g.winner_ids) or _("gw.no_winners")
            e.add_field(name=_("gw.winners"), value=winners, inline=False)
            e.add_field(name=_("gw.ended"), value=ts(g.ends_at, "R"), inline=True)
        else:
            e.add_field(name=_("gw.ends"), value=f"{ts(g.ends_at, 'R')}\n{ts(g.ends_at, 'f')}", inline=True)
            e.add_field(name=_("gw.winner_count"), value=str(g.winners_count), inline=True)
        e.add_field(name=_("gw.host"), value=f"<@{g.host_id}>", inline=True)
        e.add_field(name=_("gw.participants"), value=f"👥 **{fmt_num(participants)}** · 🎟️ {fmt_num(entries)} Entries", inline=False)
        req = g.requirements or {}
        reqs = []
        if req.get("role_id"):
            reqs.append(_("gw.req_role", role=f"<@&{req['role_id']}>"))
        if req.get("account_days"):
            reqs.append(_("gw.req_account", days=req["account_days"]))
        if req.get("server_days"):
            reqs.append(_("gw.req_server", days=req["server_days"]))
        if req.get("text"):
            reqs.append(f"• {req['text']}")
        if reqs:
            e.add_field(name=_("gw.requirements"), value="\n".join(reqs)[:1024], inline=False)
        b = g.bonus or {}
        bonus = [f"• <@&{r['role']}> → +{r['entries']}" for r in b.get("role_entries") or [] if r.get("role")]
        if b.get("invite_entries"):
            bonus.append(_("gw.bonus_invites", n=b["invite_entries"]))
        if b.get("activity_messages") and b.get("activity_entries"):
            bonus.append(_("gw.bonus_activity", msgs=b["activity_messages"], n=b["activity_entries"]))
        if b.get("early_minutes") and b.get("early_entries"):
            bonus.append(_("gw.bonus_early", minutes=b["early_minutes"], n=b["early_entries"]))
        if bonus:
            e.add_field(name=_("gw.bonus"), value="\n".join(bonus)[:1024], inline=False)
        if g.image_url:
            e.set_image(url=g.image_url)
        e.set_footer(text=f"Giveaway #{g.id}", icon_url=th.icon_url)
        return e

    async def view(self, guild: discord.Guild, g: Giveaway) -> discord.ui.View:
        _ = await i18n.for_guild(guild.id)
        v = discord.ui.View(timeout=None)
        v.add_item(GiveawayButton(g.id, "join", _("gw.join"), disabled=g.ended))
        v.add_item(GiveawayButton(g.id, "list", _("gw.participants")))
        if not g.ended:
            v.add_item(GiveawayButton(g.id, "time", _("gw.time_left")))
        return v

    def schedule_refresh(self, gid: int) -> None:
        if gid in self._refresh and not self._refresh[gid].done():
            return

        async def _later():
            await asyncio.sleep(8)
            await self.refresh_message(gid)

        self._refresh[gid] = asyncio.create_task(_later())

    async def refresh_message(self, gid: int) -> None:
        async with SessionLocal() as s:
            g = await s.get(Giveaway, gid)
        if g is None or not g.message_id:
            return
        guild = self.bot.get_guild(g.guild_id)
        channel = guild.get_channel(g.channel_id) if guild else None
        if not isinstance(channel, discord.TextChannel):
            return
        try:
            msg = channel.get_partial_message(g.message_id)
            await msg.edit(embed=await self.embed(guild, g), view=await self.view(guild, g))
        except discord.HTTPException:
            pass

    # ── Erstellen (Command + Dashboard) ──
    async def create(self, guild: discord.Guild, channel: discord.TextChannel, host: discord.abc.User, prize: str, winners: int,
                     duration_seconds: int, *, description: str = "", requirements: dict[str, Any] | None = None,
                     bonus: dict[str, Any] | None = None, image_url: str | None = None) -> Giveaway:
        if duration_seconds < 30 or duration_seconds > 60 * 86400:
            raise UserError("gw.err_duration")
        cfg = await config.get(guild.id, "giveaways")
        defaults = {"role_entries": cfg.get("role_entries") or [], "invite_entries": cfg.get("invite_entries", 0),
                    "activity_messages": cfg.get("activity_messages", 0), "activity_entries": cfg.get("activity_entries", 0),
                    "early_minutes": cfg.get("early_minutes", 0), "early_entries": cfg.get("early_entries", 0)}
        bonus = {**defaults, **{k: v for k, v in (bonus or {}).items() if v not in (None, "")}}
        async with session_scope() as s:
            g = Giveaway(guild_id=guild.id, channel_id=channel.id, prize=prize[:200], description=description[:2000],
                         winners_count=max(1, min(winners, 50)), host_id=host.id, host_name=str(host),
                         ends_at=utcnow() + timedelta(seconds=duration_seconds), requirements=requirements or {}, bonus=bonus,
                         image_url=image_url or None, winner_ids=[])
            s.add(g)
            await s.flush()
        role_id = cfg.id("ping_role")
        msg = await channel.send(content=f"<@&{role_id}>" if role_id else None, embed=await self.embed(guild, g, 0, 0),
                                 view=await self.view(guild, g), allowed_mentions=discord.AllowedMentions(roles=True))
        async with session_scope() as s:
            row = await s.get(Giveaway, g.id)
            row.message_id = msg.id
            g = row
        metrics.incr(guild.id, "giveaways")
        bus.publish(guild.id, "giveaway", {"action": "start", "id": g.id, "prize": g.prize})
        await log_event(guild.id, "giveaway", "start", user=host, channel_id=channel.id, content=prize, details={"id": g.id})
        await notify(guild, "giveaway_start", prize=prize, ends=ts(g.ends_at, "R"), url=msg.jump_url, server=guild.name)
        return g

    @giveaway.command(name="start", description="Startet ein Giveaway")
    @app_commands.describe(prize="Preis", duration="Dauer, z. B. 1h, 2d", winners="Anzahl Gewinner", channel="Channel (Standard: aktueller)",
                           required_role="Mindestrolle", account_days="Mindestalter des Accounts (Tage)",
                           server_days="Mindestzeit auf dem Server (Tage)", extra="Zusätzliche Teilnahmebedingung (Text)",
                           description="Beschreibung", image="Bild-URL")
    async def start(self, interaction: discord.Interaction, prize: app_commands.Range[str, 1, 200], duration: str,
                    winners: app_commands.Range[int, 1, 50] = 1, channel: discord.TextChannel | None = None,
                    required_role: discord.Role | None = None, account_days: app_commands.Range[int, 0, 3650] = 0,
                    server_days: app_commands.Range[int, 0, 3650] = 0, extra: app_commands.Range[str, 0, 200] = "",
                    description: app_commands.Range[str, 0, 1000] = "", image: str | None = None):
        await self._require_manager(interaction)
        secs = parse_duration(duration)
        if not secs:
            raise UserError("gw.err_duration")
        cfg = await config.get(interaction.guild_id, "giveaways")
        target = channel or interaction.guild.get_channel(cfg.id("default_channel") or 0) or interaction.channel
        req = {"role_id": str(required_role.id) if required_role else None, "account_days": account_days, "server_days": server_days, "text": extra}
        await interaction.response.defer(ephemeral=True)
        g = await self.create(interaction.guild, target, interaction.user, prize, winners, secs, description=description,
                              requirements=req, image_url=image if image and image.startswith("http") else None)
        _ = await i18n.for_guild(interaction.guild_id)
        await interaction.followup.send(embed=(await theme(interaction.guild)).success(_("common.success"), _("gw.started", id=g.id, channel=target.mention)), ephemeral=True)

    async def _require_manager(self, interaction: discord.Interaction) -> None:
        cfg = await config.get(interaction.guild_id, "giveaways")
        member: discord.Member = interaction.user  # type: ignore[assignment]
        if member.guild_permissions.manage_events or member.guild_permissions.manage_guild:
            return
        if not await is_staff(member, cfg.ids("manager_roles")) and not ({r.id for r in member.roles} & set(cfg.ids("manager_roles"))):
            raise UserError("errors.no_permission")

    async def _get(self, guild_id: int, gid: int) -> Giveaway:
        async with SessionLocal() as s:
            g = await s.get(Giveaway, gid)
        if g is None or g.guild_id != guild_id:
            raise UserError("gw.not_found")
        return g

    async def _ac_id(self, interaction: discord.Interaction, current: str, ended: bool | None):
        async with SessionLocal() as s:
            q = select(Giveaway).where(Giveaway.guild_id == interaction.guild_id)
            if ended is not None:
                q = q.where(Giveaway.ended.is_(ended))
            rows = (await s.execute(q.order_by(Giveaway.id.desc()).limit(50))).scalars().all()
        return [app_commands.Choice(name=f"#{g.id} · {g.prize}"[:100], value=g.id) for g in rows if current.lower() in f"{g.id} {g.prize}".lower()][:25]

    @giveaway.command(name="end", description="Beendet ein Giveaway sofort")
    async def end_cmd(self, interaction: discord.Interaction, giveaway_id: int):
        await self._require_manager(interaction)
        await interaction.response.defer(ephemeral=True)
        g = await self.end(await self._get(interaction.guild_id, giveaway_id))
        _ = await i18n.for_guild(interaction.guild_id)
        await interaction.followup.send(embed=(await theme(interaction.guild)).success(_("common.success"), _("gw.ended_ok", count=len(g.winner_ids))), ephemeral=True)

    @end_cmd.autocomplete("giveaway_id")
    async def _end_ac(self, interaction: discord.Interaction, current: str):
        return await self._ac_id(interaction, current, False)

    @giveaway.command(name="reroll", description="Zieht neue Gewinner")
    async def reroll_cmd(self, interaction: discord.Interaction, giveaway_id: int, count: app_commands.Range[int, 1, 50] = 1):
        await self._require_manager(interaction)
        await interaction.response.defer(ephemeral=True)
        winners = await self.reroll(await self._get(interaction.guild_id, giveaway_id), count)
        _ = await i18n.for_guild(interaction.guild_id)
        await interaction.followup.send(embed=(await theme(interaction.guild)).success(_("common.success"), _("gw.rerolled", winners=" ".join(f"<@{w}>" for w in winners) or "—")), ephemeral=True)

    @reroll_cmd.autocomplete("giveaway_id")
    async def _reroll_ac(self, interaction: discord.Interaction, current: str):
        return await self._ac_id(interaction, current, True)

    @giveaway.command(name="delete", description="Löscht ein Giveaway")
    async def delete_cmd(self, interaction: discord.Interaction, giveaway_id: int):
        await self._require_manager(interaction)
        g = await self._get(interaction.guild_id, giveaway_id)
        _ = await i18n.for_guild(interaction.guild_id)
        if not await confirm(interaction, _("gw.delete_title"), _("gw.delete_confirm", prize=g.prize)):
            return
        await self.delete(g)
        await reply(interaction, (await theme(interaction.guild)).success(_("common.success"), _("common.done")))

    @delete_cmd.autocomplete("giveaway_id")
    async def _del_ac(self, interaction: discord.Interaction, current: str):
        return await self._ac_id(interaction, current, None)

    @giveaway.command(name="list", description="Aktive und vergangene Giveaways")
    async def list_cmd(self, interaction: discord.Interaction):
        _ = await i18n.for_guild(interaction.guild_id)
        th = await theme(interaction.guild)
        async with SessionLocal() as s:
            rows = (await s.execute(select(Giveaway).where(Giveaway.guild_id == interaction.guild_id)
                                    .order_by(Giveaway.ended, Giveaway.ends_at.desc()).limit(60))).scalars().all()
        if not rows:
            await reply(interaction, th.info(_("gw.list_title"), _("gw.list_empty")))
            return
        pages = []
        for group in chunk(rows, 8):
            lines = [f"{'🏁' if g.ended else '🟢'} **#{g.id} · {g.prize}** — {ts(g.ends_at, 'R')} · {g.winners_count}× 🏆" for g in group]
            pages.append(th.embed(_("gw.list_title"), "\n".join(lines), icon=False))
        await Paginator(interaction.user.id, pages).send(interaction, ephemeral=True)

    # ── Teilnahme ──
    async def compute_entries(self, g: Giveaway, member: discord.Member) -> int:
        """Prüft Bedingungen (UserError bei Verstoß) und berechnet die Anzahl Entries."""
        req = g.requirements or {}
        now = utcnow()
        if req.get("role_id") and not member.get_role(int(req["role_id"])):
            raise UserError("gw.req_fail_role", role=f"<@&{req['role_id']}>")
        if req.get("account_days") and now - member.created_at < timedelta(days=int(req["account_days"])):
            raise UserError("gw.req_fail_account", days=req["account_days"])
        if req.get("server_days") and member.joined_at and now - member.joined_at < timedelta(days=int(req["server_days"])):
            raise UserError("gw.req_fail_server", days=req["server_days"])
        b = g.bonus or {}
        entries = 1
        for r in b.get("role_entries") or []:
            if r.get("role") and member.get_role(int(r["role"])):
                entries += int(r.get("entries") or 0)
        async with SessionLocal() as s:
            row = await s.get(Member, (member.guild.id, member.id))
        if row:
            if b.get("invite_entries"):
                entries += row.invites * int(b["invite_entries"])
            if b.get("activity_messages") and b.get("activity_entries") and row.messages >= int(b["activity_messages"]):
                entries += int(b["activity_entries"])
        if b.get("early_minutes") and b.get("early_entries") and now - g.starts_at <= timedelta(minutes=int(b["early_minutes"])):
            entries += int(b["early_entries"])
        return min(entries, 1000)

    async def toggle_join(self, interaction: discord.Interaction, gid: int) -> None:
        g = await self._get(interaction.guild_id, gid)
        _ = await i18n.for_guild(interaction.guild_id)
        th = await theme(interaction.guild)
        if g.ended or g.ends_at <= utcnow():
            raise UserError("gw.already_ended")
        async with SessionLocal() as s:
            existing = await s.get(GiveawayEntry, (gid, interaction.user.id))
        if existing:
            await reply(interaction, th.info(_("gw.already_joined_title"), _("gw.already_joined", entries=existing.entries)),
                        view=LeaveView(self, gid, interaction.user.id, _("gw.leave")))
            return
        entries = await self.compute_entries(g, interaction.user)  # type: ignore[arg-type]
        async with session_scope() as s:
            if await s.get(GiveawayEntry, (gid, interaction.user.id)) is None:
                s.add(GiveawayEntry(giveaway_id=gid, user_id=interaction.user.id, entries=entries))
            await ensure_member(s, interaction.guild_id, interaction.user)
        await progression.quest_progress(self.bot, interaction.guild, interaction.user.id, "giveaways_joined", 1)
        await reply(interaction, th.success(_("gw.joined_title"), _("gw.joined", entries=entries, prize=g.prize)))
        self.schedule_refresh(gid)
        bus.publish(interaction.guild_id, "giveaway", {"action": "join", "id": gid})

    async def show_participants(self, interaction: discord.Interaction, gid: int) -> None:
        g = await self._get(interaction.guild_id, gid)
        _ = await i18n.for_guild(interaction.guild_id)
        th = await theme(interaction.guild)
        async with SessionLocal() as s:
            rows = (await s.execute(select(GiveawayEntry).where(GiveawayEntry.giveaway_id == gid)
                                    .order_by(GiveawayEntry.entries.desc(), GiveawayEntry.joined_at))).scalars().all()
        total = sum(r.entries for r in rows)
        mine = next((r.entries for r in rows if r.user_id == interaction.user.id), 0)
        if not rows:
            await reply(interaction, th.info(_("gw.participants"), _("gw.no_participants")))
            return
        pages = []
        for group in chunk(rows, 20):
            lines = [f"<@{r.user_id}> · 🎟️ {r.entries}" for r in group]
            e = th.embed(f"👥 {g.prize}", "\n".join(lines), icon=False)
            e.set_footer(text=_("gw.participants_footer", count=len(rows), entries=total, mine=mine,
                                chance=f"{(mine / total * 100) if total else 0:.1f}"))
            pages.append(e)
        await Paginator(interaction.user.id, pages).send(interaction, ephemeral=True)

    async def show_time(self, interaction: discord.Interaction, gid: int) -> None:
        g = await self._get(interaction.guild_id, gid)
        _ = await i18n.for_guild(interaction.guild_id)
        th = await theme(interaction.guild)
        await reply(interaction, th.info(_("gw.time_left"), _("gw.time_desc", rel=ts(g.ends_at, "R"), abs=ts(g.ends_at, "F"))))

    # ── Auslosung ──
    async def _draw(self, g: Giveaway, count: int, exclude: set[int]) -> list[int]:
        guild = self.bot.get_guild(g.guild_id)
        async with SessionLocal() as s:
            rows = (await s.execute(select(GiveawayEntry).where(GiveawayEntry.giveaway_id == g.id))).scalars().all()
        pool = [(r.user_id, r.entries) for r in rows if r.user_id not in exclude and guild and guild.get_member(r.user_id)]
        winners: list[int] = []
        rng = random.SystemRandom()
        while pool and len(winners) < count:
            total = sum(w for _u, w in pool)
            pick = rng.uniform(0, total)
            acc = 0.0
            for i, (uid, w) in enumerate(pool):
                acc += w
                if pick <= acc:
                    winners.append(uid)
                    pool.pop(i)
                    break
        return winners

    async def _reward(self, guild: discord.Guild, g: Giveaway, winners: list[int]) -> None:
        cfg = await config.get(guild.id, "giveaways")
        _ = await i18n.for_guild(guild.id)
        th = await theme(guild)
        for uid in winners:
            async with session_scope() as s:
                row = await ensure_member(s, guild.id, guild.get_member(uid) or uid)
                row.giveaways_won += 1
            await achievements.check(self.bot, guild, row, guild.get_member(uid))
            if cfg.get("winner_coins"):
                await progression.add_coins(guild.id, uid, int(cfg["winner_coins"]), reason="giveaway")
            if cfg.get("dm_winners", True):
                member = guild.get_member(uid)
                if member:
                    e = th.success(_("gw.dm_title"), _("gw.dm_desc", prize=g.prize, server=guild.name))
                    if g.message_id:
                        e.add_field(name="🔗", value=f"https://discord.com/channels/{guild.id}/{g.channel_id}/{g.message_id}")
                    try:
                        await member.send(embed=e)
                    except discord.HTTPException:
                        pass

    async def end(self, g: Giveaway) -> Giveaway:
        if g.ended:
            raise UserError("gw.already_ended")
        guild = self.bot.get_guild(g.guild_id)
        if guild is None:
            return g
        winners = await self._draw(g, g.winners_count, set())
        async with session_scope() as s:
            row = await s.get(Giveaway, g.id)
            row.ended = True
            row.winner_ids = [str(w) for w in winners]
            if row.ends_at > utcnow():
                row.ends_at = utcnow()
            g = row
        await self._reward(guild, g, winners)
        _ = await i18n.for_guild(guild.id)
        channel = guild.get_channel(g.channel_id)
        if isinstance(channel, discord.TextChannel):
            await self.refresh_message(g.id)
            th = await theme(guild)
            text = _("gw.winners_announce", winners=" ".join(f"<@{w}>" for w in winners), prize=g.prize) if winners else _("gw.no_valid_entries", prize=g.prize)
            try:
                ref = channel.get_partial_message(g.message_id) if g.message_id else None
                await channel.send(embed=th.success(_("gw.ended_title"), text), reference=ref,
                                   content=" ".join(f"<@{w}>" for w in winners) or None,
                                   allowed_mentions=discord.AllowedMentions(users=True))
            except discord.HTTPException:
                pass
        bus.publish(guild.id, "giveaway", {"action": "end", "id": g.id, "winners": [str(w) for w in winners]})
        await log_event(guild.id, "giveaway", "end", content=g.prize, details={"id": g.id, "winners": [str(w) for w in winners]})
        await notify(guild, "giveaway_end", prize=g.prize, winners=" ".join(f"<@{w}>" for w in winners) or "—", server=guild.name)
        return g

    async def reroll(self, g: Giveaway, count: int = 1) -> list[int]:
        if not g.ended:
            raise UserError("gw.not_ended")
        guild = self.bot.get_guild(g.guild_id)
        exclude = {int(w) for w in g.winner_ids}
        winners = await self._draw(g, count, exclude)
        if not winners:
            raise UserError("gw.no_reroll_candidates")
        async with session_scope() as s:
            row = await s.get(Giveaway, g.id)
            row.winner_ids = [*row.winner_ids, *[str(w) for w in winners]]
        await self._reward(guild, g, winners)
        _ = await i18n.for_guild(guild.id)
        channel = guild.get_channel(g.channel_id)
        if isinstance(channel, discord.TextChannel):
            th = await theme(guild)
            try:
                await channel.send(content=" ".join(f"<@{w}>" for w in winners),
                                   embed=th.success(_("gw.reroll_title"), _("gw.winners_announce", winners=" ".join(f"<@{w}>" for w in winners), prize=g.prize)),
                                   allowed_mentions=discord.AllowedMentions(users=True))
            except discord.HTTPException:
                pass
            await self.refresh_message(g.id)
        await log_event(guild.id, "giveaway", "reroll", content=g.prize, details={"id": g.id, "winners": [str(w) for w in winners]})
        return winners

    async def delete(self, g: Giveaway) -> None:
        guild = self.bot.get_guild(g.guild_id)
        if guild and g.message_id:
            ch = guild.get_channel(g.channel_id)
            if isinstance(ch, discord.TextChannel):
                try:
                    await ch.get_partial_message(g.message_id).delete()
                except discord.HTTPException:
                    pass
        async with session_scope() as s:
            row = await s.get(Giveaway, g.id)
            if row:
                await s.delete(row)
        bus.publish(g.guild_id, "giveaway", {"action": "delete", "id": g.id})

    @tasks.loop(seconds=15)
    async def end_loop(self):
        async with SessionLocal() as s:
            due = (await s.execute(select(Giveaway).where(Giveaway.ended.is_(False), Giveaway.ends_at <= utcnow()).limit(20))).scalars().all()
        for g in due:
            try:
                await self.end(g)
            except Exception:
                log.exception("Giveaway %s konnte nicht beendet werden", g.id)
                async with session_scope() as s:
                    row = await s.get(Giveaway, g.id)
                    if row:
                        row.ended = True

    @end_loop.before_loop
    async def _before(self):
        await self.bot.wait_until_ready()


async def setup(bot: commands.Bot):
    bot.add_dynamic_items(GiveawayButton)
    await bot.add_cog(Giveaways(bot))
