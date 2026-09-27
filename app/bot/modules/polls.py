"""Umfragen mit Live-Ergebnissen, Mehrfachauswahl, optional anonym, automatisches Ende."""
from __future__ import annotations

import asyncio
import re
from datetime import timedelta

import discord
from discord import app_commands
from discord.ext import commands, tasks
from sqlalchemy import func, select

from app.bot.ui import handle_exception, reply
from app.core.embeds import progress_bar, theme
from app.core.errors import UserError
from app.core.guild_config import config
from app.core.i18n import i18n
from app.core.timeutil import parse_duration, ts
from app.db.base import SessionLocal, session_scope, utcnow
from app.db.models import Poll, PollVote

LETTERS = ["🇦", "🇧", "🇨", "🇩", "🇪", "🇫", "🇬", "🇭", "🇮", "🇯"]


class PollButton(discord.ui.DynamicItem[discord.ui.Button], template=r"nova:poll:(?P<id>\d+):(?P<opt>\d+|res)"):
    def __init__(self, pid: int, opt: str, label: str | None = None, emoji: str | None = None, disabled: bool = False, row: int | None = None):
        style = discord.ButtonStyle.secondary if opt == "res" else discord.ButtonStyle.primary
        super().__init__(discord.ui.Button(label=label, emoji=emoji, style=style, custom_id=f"nova:poll:{pid}:{opt}", disabled=disabled, row=row))
        self.pid, self.opt = pid, opt

    @classmethod
    async def from_custom_id(cls, interaction, item, match: re.Match[str], /):
        return cls(int(match["id"]), match["opt"])

    async def callback(self, interaction: discord.Interaction):
        cog: Polls = interaction.client.get_cog("Polls")  # type: ignore[assignment]
        try:
            if self.opt == "res":
                await cog.show_voters(interaction, self.pid)
            else:
                await cog.vote(interaction, self.pid, int(self.opt))
        except Exception as exc:  # noqa: BLE001
            await handle_exception(interaction, exc, "poll")


@app_commands.guild_only()
class Polls(commands.Cog):
    module = "polls"
    help_category = "community"

    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self._pending: dict[int, asyncio.Task] = {}
        self.end_loop.start()

    def cog_unload(self):
        self.end_loop.cancel()

    async def counts(self, pid: int) -> tuple[dict[int, int], int]:
        async with SessionLocal() as db:
            rows = (await db.execute(select(PollVote.option_index, func.count()).where(PollVote.poll_id == pid).group_by(PollVote.option_index))).all()
            voters = (await db.execute(select(func.count(func.distinct(PollVote.user_id))).where(PollVote.poll_id == pid))).scalar_one()
        return {i: c for i, c in rows}, voters

    async def embed(self, guild: discord.Guild, p: Poll) -> discord.Embed:
        _ = await i18n.for_guild(guild.id)
        th = await theme(guild)
        counts, voters = await self.counts(p.id)
        total = sum(counts.values())
        best = max(counts.values(), default=0)
        lines = []
        for i, opt in enumerate(p.options):
            c = counts.get(i, 0)
            pct = c / total * 100 if total else 0
            crown = " 👑" if p.ended and c == best and c > 0 else ""
            lines.append(f"{LETTERS[i]} **{opt}**{crown}\n{progress_bar(c, total or 1, 16)} `{pct:4.1f}%` · {c}")
        e = th.embed(f"📊 {p.question}", "\n\n".join(lines), kind="success" if p.ended else "primary", icon=False)
        info = [f"👥 {voters} {_('poll.voters')}"]
        if p.multiple:
            info.append(_("poll.multiple"))
        if p.anonymous:
            info.append("🕶️ " + _("poll.anonymous"))
        if p.ends_at:
            info.append((_("poll.ended") if p.ended else _("poll.ends")) + " " + ts(p.ends_at, "R"))
        e.add_field(name="​", value=" · ".join(info), inline=False)
        e.set_footer(text=f"Poll #{p.id}", icon_url=th.icon_url)
        return e

    def view(self, p: Poll, results_label: str) -> discord.ui.View:
        v = discord.ui.View(timeout=None)
        for i, opt in enumerate(p.options):
            v.add_item(PollButton(p.id, str(i), opt[:70], LETTERS[i], disabled=p.ended, row=i // 5))
        v.add_item(PollButton(p.id, "res", results_label, "📋", row=2))
        return v

    @app_commands.command(name="poll", description="Erstellt eine Umfrage")
    @app_commands.describe(question="Frage", options="Antworten, getrennt mit |  (2–10)", duration="Dauer, z. B. 30m, 1d (leer = Standard)",
                           multiple="Mehrere Antworten erlauben", anonymous="Stimmen anonym")
    async def poll(self, interaction: discord.Interaction, question: app_commands.Range[str, 3, 300], options: str,
                   duration: str = "", multiple: bool = False, anonymous: bool = False):
        cfg = await config.get(interaction.guild_id, "polls")
        member: discord.Member = interaction.user  # type: ignore[assignment]
        roles = set(cfg.ids("creator_roles"))
        if roles and not ({r.id for r in member.roles} & roles) and not member.guild_permissions.manage_guild:
            raise UserError("errors.no_permission")
        if not roles and not member.guild_permissions.manage_messages:
            raise UserError("errors.no_permission")
        opts = [o.strip()[:80] for o in options.split("|") if o.strip()]
        if not 2 <= len(opts) <= 10:
            raise UserError("poll.err_options")
        secs = parse_duration(duration) if duration else cfg.get("default_hours", 24) * 3600
        if not secs or secs > 60 * 86400:
            raise UserError("poll.err_duration")
        async with session_scope() as db:
            p = Poll(guild_id=interaction.guild_id, channel_id=interaction.channel_id, question=question, options=opts, multiple=multiple,
                     anonymous=anonymous, author_id=interaction.user.id, ends_at=utcnow() + timedelta(seconds=secs), ended=False)
            db.add(p)
            await db.flush()
        _ = await i18n.for_guild(interaction.guild_id)
        await interaction.response.send_message(embed=await self.embed(interaction.guild, p), view=self.view(p, _("poll.results")))
        msg = await interaction.original_response()
        async with session_scope() as db:
            (await db.get(Poll, p.id)).message_id = msg.id

    async def vote(self, interaction: discord.Interaction, pid: int, idx: int) -> None:
        _ = await i18n.for_guild(interaction.guild_id)
        async with session_scope() as db:
            p = await db.get(Poll, pid)
            if p is None or p.ended:
                raise UserError("poll.closed")
            if not 0 <= idx < len(p.options):
                raise UserError("errors.invalid_input", value=idx)
            mine = (await db.execute(select(PollVote).where(PollVote.poll_id == pid, PollVote.user_id == interaction.user.id))).scalars().all()
            existing = next((v for v in mine if v.option_index == idx), None)
            if existing:
                await db.delete(existing)
                msg = _("poll.vote_removed", option=p.options[idx])
            else:
                if not p.multiple:
                    for v in mine:
                        await db.delete(v)
                db.add(PollVote(poll_id=pid, user_id=interaction.user.id, option_index=idx))
                msg = _("poll.voted", option=p.options[idx])
        await interaction.response.send_message(msg, ephemeral=True)
        self._schedule(p)

    def _schedule(self, p: Poll) -> None:
        if p.id in self._pending and not self._pending[p.id].done():
            return

        async def later():
            await asyncio.sleep(3)
            await self.refresh(p.id)

        self._pending[p.id] = asyncio.create_task(later())

    async def refresh(self, pid: int) -> None:
        async with SessionLocal() as db:
            p = await db.get(Poll, pid)
        guild = self.bot.get_guild(p.guild_id) if p else None
        ch = guild.get_channel(p.channel_id) if guild else None
        if not p or not p.message_id or not isinstance(ch, (discord.TextChannel, discord.Thread)):
            return
        _ = await i18n.for_guild(guild.id)
        try:
            await ch.get_partial_message(p.message_id).edit(embed=await self.embed(guild, p), view=self.view(p, _("poll.results")))
        except discord.HTTPException:
            pass

    async def show_voters(self, interaction: discord.Interaction, pid: int) -> None:
        async with SessionLocal() as db:
            p = await db.get(Poll, pid)
            votes = (await db.execute(select(PollVote).where(PollVote.poll_id == pid))).scalars().all()
        if p is None:
            raise UserError("poll.closed")
        _ = await i18n.for_guild(interaction.guild_id)
        th = await theme(interaction.guild)
        e = th.embed(f"📋 {p.question}", icon=False)
        for i, opt in enumerate(p.options):
            ids = [v.user_id for v in votes if v.option_index == i]
            value = f"{len(ids)} {_('poll.votes')}" if p.anonymous else (" ".join(f"<@{u}>" for u in ids[:40]) or "—")
            e.add_field(name=f"{LETTERS[i]} {opt}", value=value[:1024], inline=False)
        await reply(interaction, e)

    @tasks.loop(seconds=30)
    async def end_loop(self):
        async with session_scope() as db:
            due = (await db.execute(select(Poll).where(Poll.ended.is_(False), Poll.ends_at <= utcnow()).limit(20))).scalars().all()
            for p in due:
                p.ended = True
            ids = [p.id for p in due]
        for pid in ids:
            await self.refresh(pid)

    @end_loop.before_loop
    async def _before(self):
        await self.bot.wait_until_ready()


async def setup(bot: commands.Bot):
    bot.add_dynamic_items(PollButton)
    await bot.add_cog(Polls(bot))
