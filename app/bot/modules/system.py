"""System-Aufgaben: automatische Backups, Datenbereinigung, globale Owner-Ankündigungen."""
from __future__ import annotations

import logging
from datetime import timedelta

import discord
from discord.ext import commands, tasks
from sqlalchemy import delete

from app.config import settings
from app.core.embeds import theme
from app.core.guild_config import config
from app.core.records import capture_error
from app.db.base import session_scope, utcnow
from app.db.models import DashboardSession, LogEntry, StreamSnapshot
from app.services import backups

log = logging.getLogger("nova.system")


class System(commands.Cog):
    module = "core"
    help_category = "admin"

    def __init__(self, bot: commands.Bot):
        self.bot = bot
        if settings.backup_interval_hours > 0:
            self.backup_loop.change_interval(hours=settings.backup_interval_hours)
            self.backup_loop.start()
        self.cleanup_loop.start()

    def cog_unload(self):
        self.backup_loop.cancel()
        self.cleanup_loop.cancel()

    @tasks.loop(hours=24)
    async def backup_loop(self):
        for g in self.bot.guilds:
            try:
                await backups.export_guild(g.id, auto=True)
                await backups.prune(g.id, settings.backup_keep)
            except Exception as exc:  # noqa: BLE001
                await capture_error(exc, guild_id=g.id, command="auto_backup")
        try:
            await backups.full_backup(auto=True)
            await backups.prune(None, max(3, settings.backup_keep // 2))
        except Exception as exc:  # noqa: BLE001
            log.warning("Vollbackup übersprungen: %s", exc)

    @backup_loop.before_loop
    async def _b(self):
        await self.bot.wait_until_ready()

    @tasks.loop(hours=6)
    async def cleanup_loop(self):
        now = utcnow()
        async with session_scope() as db:
            await db.execute(delete(DashboardSession).where(DashboardSession.expires_at < now))
            await db.execute(delete(LogEntry).where(LogEntry.created_at < now - timedelta(days=180)))
            await db.execute(delete(StreamSnapshot).where(StreamSnapshot.taken_at < now - timedelta(days=400), StreamSnapshot.live.is_(True)))

    @cleanup_loop.before_loop
    async def _c(self):
        await self.bot.wait_until_ready()

    async def announce(self, title: str, message: str) -> tuple[int, int]:
        """Globale Ankündigung an alle Server (Channel aus 'Allgemein → Bot-Ankündigungen', sonst System-Channel)."""
        sent = failed = 0
        for g in self.bot.guilds:
            cfg = await config.get(g.id, "general")
            ch = g.get_channel(cfg.id("announcement_channel") or 0) or g.system_channel
            if not isinstance(ch, discord.TextChannel) or not ch.permissions_for(g.me).send_messages:
                failed += 1
                continue
            th = await theme(g)
            try:
                await ch.send(embed=th.info(title, message))
                sent += 1
            except discord.HTTPException:
                failed += 1
        return sent, failed


async def setup(bot: commands.Bot):
    await bot.add_cog(System(bot))
