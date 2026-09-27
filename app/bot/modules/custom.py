"""Custom Slash-Commands (pro Server, im Dashboard erstellt) und Autoresponder (Trigger-Wörter)."""
from __future__ import annotations

import asyncio
import logging
import re
import time
from typing import Any

import discord
from discord import app_commands
from discord.ext import commands
from sqlalchemy import select, update

from app.bot.ui import fail, reply
from app.config import settings
from app.core.embeds import hex_to_color, theme
from app.core.guild_config import config
from app.core.i18n import fill
from app.core.records import capture_error
from app.db.base import SessionLocal, session_scope
from app.db.models import AutoResponder, CustomCommand
from app.services.notify import is_staff

log = logging.getLogger("nova.custom")
NAME_RE = re.compile(r"^[-_a-z0-9äöüß]{1,32}$")


def placeholders(member: discord.abc.User, guild: discord.Guild | None, channel: Any) -> dict[str, Any]:
    return {"user": member.mention, "username": getattr(member, "display_name", member.name), "server": guild.name if guild else "",
            "membercount": guild.member_count if guild else 0, "channel": getattr(channel, "mention", "")}


def build_buttons(buttons: list[dict]) -> discord.ui.View | None:
    items = [b for b in buttons or [] if str(b.get("url", "")).startswith(("http://", "https://"))][:5]
    if not items:
        return None
    v = discord.ui.View()
    for b in items:
        v.add_item(discord.ui.Button(label=(b.get("label") or "Link")[:80], url=b["url"], emoji=b.get("emoji") or None))
    return v


class CustomCommands(commands.Cog, name="CustomCommands"):
    module = "custom_commands"
    help_category = "community"

    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self._sync_tasks: dict[int, asyncio.Task] = {}
        self._ar_cache: dict[int, tuple[float, list[dict]]] = {}
        self._ar_cd: dict[tuple[int, int], float] = {}

    # ── Custom Commands ──
    def reserved_names(self) -> set[str]:
        return {c.name for c in self.bot.tree.get_commands()}

    async def sync_guild(self, guild_id: int) -> int:
        guild = discord.Object(guild_id)
        async with SessionLocal() as db:
            rows = (await db.execute(select(CustomCommand).where(CustomCommand.guild_id == guild_id, CustomCommand.enabled.is_(True)))).scalars().all()
        enabled = await config.enabled(guild_id, "custom_commands")
        self.bot.tree.clear_commands(guild=guild)
        reserved = self.reserved_names()
        count = 0
        if enabled:
            for row in rows[:90]:
                if row.name in reserved or not NAME_RE.match(row.name):
                    continue
                self.bot.tree.add_command(self._make(row.id, row.name, row.description or "Custom Command"), guild=guild)
                count += 1
        try:
            await self.bot.tree.sync(guild=guild)
        except discord.HTTPException as exc:
            await capture_error(exc, guild_id=guild_id, command="custom.sync")
        return count

    def schedule_sync(self, guild_id: int) -> None:
        """Mehrere Änderungen im Dashboard bündeln → max. 1 Sync pro 5 s (Rate-Limits)."""
        task = self._sync_tasks.get(guild_id)
        if task and not task.done():
            task.cancel()

        async def later():
            await asyncio.sleep(5)
            await self.sync_guild(guild_id)

        self._sync_tasks[guild_id] = asyncio.create_task(later())

    async def sync_all(self) -> None:
        async with SessionLocal() as db:
            gids = set((await db.execute(select(CustomCommand.guild_id).distinct())).scalars())
        for gid in gids:
            if self.bot.get_guild(gid):
                await self.sync_guild(gid)
                await asyncio.sleep(1)

    def _make(self, cmd_id: int, name: str, description: str) -> app_commands.Command:
        cog = self

        async def callback(interaction: discord.Interaction):
            await cog.run(interaction, cmd_id)

        cmd = app_commands.Command(name=name, description=description[:100], callback=callback, extras={"module": "custom_commands", "help_category": "community"})
        cmd.guild_only = True
        return cmd

    async def run(self, interaction: discord.Interaction, cmd_id: int) -> None:
        async with session_scope() as db:
            row = await db.get(CustomCommand, cmd_id)
            if row is None or not row.enabled:
                await fail(interaction, "custom.gone")
                return
            data = row.to_dict()
            await db.execute(update(CustomCommand).where(CustomCommand.id == cmd_id).values(uses=CustomCommand.uses + 1))
        allowed = {int(r) for r in data.get("allowed_role_ids") or []}
        if allowed and isinstance(interaction.user, discord.Member) and not ({r.id for r in interaction.user.roles} & allowed) \
                and not interaction.user.guild_permissions.manage_guild:
            await fail(interaction, "errors.no_permission")
            return
        ph = placeholders(interaction.user, interaction.guild, interaction.channel)
        text = fill(data.get("content") or "", **ph)
        view = build_buttons(data.get("buttons") or [])
        mentions = discord.AllowedMentions(users=True, roles=True, everyone=False)
        if data.get("use_embed", True):
            th = await theme(interaction.guild)
            e = discord.Embed(title=fill(data.get("embed_title") or "", **ph) or None, description=text or None,
                              colour=hex_to_color(data.get("embed_color")) if data.get("embed_color") else th.color("primary"))
            if data.get("image_url"):
                e.set_image(url=data["image_url"])
            if data.get("thumbnail_url"):
                e.set_thumbnail(url=data["thumbnail_url"])
            e.set_footer(text=th.footer, icon_url=th.icon_url)
            await interaction.response.send_message(embed=e, view=view or discord.utils.MISSING, ephemeral=bool(data.get("ephemeral")), allowed_mentions=mentions)
        else:
            await interaction.response.send_message(text[:2000] or "…", view=view or discord.utils.MISSING, ephemeral=bool(data.get("ephemeral")),
                                                    allowed_mentions=mentions)

    # ── Autoresponder ──
    def invalidate_autoresponders(self, guild_id: int) -> None:
        self._ar_cache.pop(guild_id, None)

    async def _responders(self, guild_id: int) -> list[dict]:
        hit = self._ar_cache.get(guild_id)
        if hit and hit[0] > time.monotonic():
            return hit[1]
        async with SessionLocal() as db:
            rows = (await db.execute(select(AutoResponder).where(AutoResponder.guild_id == guild_id, AutoResponder.enabled.is_(True)))).scalars().all()
        data = []
        for r in rows:
            d = r.to_dict()
            d["id"] = r.id
            trig = r.trigger.lower()
            try:
                if r.match_type == "regex":
                    d["_re"] = re.compile(r.trigger, re.I)
                elif r.match_type == "word":
                    d["_re"] = re.compile(r"(?<!\w)" + re.escape(trig) + r"(?!\w)", re.I)
            except re.error:
                continue
            d["_trig"] = trig
            data.append(d)
        self._ar_cache[guild_id] = (time.monotonic() + 60, data)
        return data

    @staticmethod
    def _matches(d: dict, content: str) -> bool:
        low = content.lower()
        mt = d["match_type"]
        if mt == "exact":
            return low.strip() == d["_trig"]
        if mt == "startswith":
            return low.startswith(d["_trig"])
        if mt == "contains":
            return d["_trig"] in low
        pattern = d.get("_re")
        return bool(pattern and pattern.search(content[:2000]))

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message):
        if message.author.bot or message.guild is None or not message.content:
            return
        gid = message.guild.id
        if not await config.enabled(gid, "autoresponder"):
            return
        responders = await self._responders(gid)
        if not responders:
            return
        cfg = await config.get(gid, "autoresponder")
        if cfg.get("ignore_staff") and isinstance(message.author, discord.Member) and await is_staff(message.author):
            return
        for d in responders:
            channels = {int(c) for c in d.get("channel_ids") or []}
            if channels and message.channel.id not in channels and getattr(message.channel, "parent_id", None) not in channels:
                continue
            if not self._matches(d, message.content):
                continue
            key = (d["id"], message.channel.id)
            now = time.monotonic()
            if self._ar_cd.get(key, 0) > now:
                return
            self._ar_cd[key] = now + int(d.get("cooldown_seconds") or 0)
            text = fill(d["response"], **placeholders(message.author, message.guild, message.channel))[:2000]
            try:
                if d.get("use_embed"):
                    th = await theme(message.guild)
                    payload = {"embed": th.embed(None, text, icon=False)}
                else:
                    payload = {"content": text}
                if d.get("reply", True) and not d.get("delete_trigger"):
                    await message.reply(**payload, mention_author=False)
                else:
                    await message.channel.send(**payload)
                if d.get("delete_trigger"):
                    await message.delete()
            except discord.HTTPException:
                pass
            async with session_scope() as db:
                await db.execute(update(AutoResponder).where(AutoResponder.id == d["id"]).values(uses=AutoResponder.uses + 1))
            return


async def setup(bot: commands.Bot):
    await bot.add_cog(CustomCommands(bot))
