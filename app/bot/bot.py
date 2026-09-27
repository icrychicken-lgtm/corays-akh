"""NovaBot – discord.py-Client mit modularem Aufbau, globalen Checks und Fehlerbehandlung."""
from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import pkgutil
import time
from typing import Any

import discord
from discord import app_commands
from discord.ext import commands, tasks
from sqlalchemy import select

from app.config import settings
from app.core.embeds import theme
from app.core.errors import NovaCheckFailure, UserError
from app.core.events import bus
from app.core.guild_config import config
from app.core.i18n import i18n
from app.core.metrics import metrics
from app.core.records import capture_error
from app.db.base import SessionLocal, session_scope
from app.db.models import CommandPermission
from app.services.guilds import ensure_guild, mark_left
from app.services.notify import is_admin

log = logging.getLogger("nova.bot")


def module_of(command: app_commands.Command | app_commands.ContextMenu | app_commands.Group | None) -> str:
    if command is None:
        return "core"
    mod = (command.extras or {}).get("module") if hasattr(command, "extras") else None
    root = getattr(command, "root_parent", None)
    if not mod and root is not None:
        mod = (root.extras or {}).get("module")
    if mod:
        return mod
    binding = getattr(command, "binding", None)
    if binding is None and getattr(command, "root_parent", None) is not None:
        binding = getattr(command.root_parent, "binding", None) or getattr(command.root_parent, "module", None)
    return getattr(binding, "module", None) or "core"


class NovaTree(app_commands.CommandTree):
    """Globale Prüfung für jeden Slash-Command: Maintenance, Feature-Toggle, Command-Berechtigungen."""

    def __init__(self, client: "NovaBot"):
        super().__init__(client)
        self._perm_cache: dict[int, tuple[float, dict[str, dict[str, Any]]]] = {}

    def invalidate_permissions(self, guild_id: int) -> None:
        self._perm_cache.pop(guild_id, None)

    async def _perms(self, guild_id: int) -> dict[str, dict[str, Any]]:
        hit = self._perm_cache.get(guild_id)
        if hit and hit[0] > time.monotonic():
            return hit[1]
        async with SessionLocal() as s:
            rows = (await s.execute(select(CommandPermission).where(CommandPermission.guild_id == guild_id))).scalars().all()
        data = {r.command: {
            "enabled": r.enabled, "allowed_roles": {int(x) for x in r.allowed_role_ids or []},
            "denied_roles": {int(x) for x in r.denied_role_ids or []}, "allowed_users": {int(x) for x in r.allowed_user_ids or []},
            "allowed_channels": {int(x) for x in r.allowed_channel_ids or []}, "denied_channels": {int(x) for x in r.denied_channel_ids or []},
        } for r in rows}
        self._perm_cache[guild_id] = (time.monotonic() + 120, data)
        return data

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        bot: NovaBot = interaction.client  # type: ignore[assignment]
        is_owner = interaction.user.id in settings.owner_id_set
        if bot.maintenance and not is_owner:
            raise NovaCheckFailure("errors.maintenance")
        cmd = interaction.command
        if cmd is None or interaction.guild is None:
            return True
        module = module_of(cmd)
        if module != "core" and not await config.enabled(interaction.guild.id, module):
            raise NovaCheckFailure("errors.module_disabled")

        root = cmd.root_parent.name if getattr(cmd, "root_parent", None) else cmd.name
        perm = (await self._perms(interaction.guild.id)).get(root)
        if not perm:
            return True
        if not perm["enabled"]:
            raise NovaCheckFailure("errors.command_disabled")
        member = interaction.user
        if not isinstance(member, discord.Member) or is_owner or await is_admin(member):
            return True
        channel_id = interaction.channel_id or 0
        parent_id = getattr(interaction.channel, "parent_id", None) or 0
        if perm["denied_channels"] & {channel_id, parent_id}:
            raise NovaCheckFailure("errors.channel_not_allowed")
        if perm["allowed_channels"] and not perm["allowed_channels"] & {channel_id, parent_id}:
            raise NovaCheckFailure("errors.channel_not_allowed")
        role_ids = {r.id for r in member.roles}
        if perm["denied_roles"] & role_ids:
            raise NovaCheckFailure("errors.no_permission")
        if member.id in perm["allowed_users"]:
            return True
        if perm["allowed_roles"] and not perm["allowed_roles"] & role_ids:
            raise NovaCheckFailure("errors.no_permission")
        return True

    async def on_error(self, interaction: discord.Interaction, error: app_commands.AppCommandError) -> None:
        _ = await i18n.for_guild(interaction.guild_id)
        th = await theme(interaction.guild)
        original = getattr(error, "original", error)
        if isinstance(error, NovaCheckFailure):
            msg = _(error.key, **error.kw)
        elif isinstance(original, UserError):
            msg = _(original.key, **original.kw)
        elif isinstance(error, app_commands.MissingPermissions):
            msg = _("errors.missing_permissions", perms=", ".join(error.missing_permissions))
        elif isinstance(error, app_commands.BotMissingPermissions):
            msg = _("errors.bot_missing_permissions", perms=", ".join(error.missing_permissions))
        elif isinstance(error, app_commands.CommandOnCooldown):
            msg = _("errors.cooldown", seconds=f"{error.retry_after:.0f}")
        elif isinstance(error, app_commands.NoPrivateMessage):
            msg = _("errors.guild_only")
        elif isinstance(error, app_commands.TransformerError):
            msg = _("errors.invalid_input", value=str(error.value)[:100])
        elif isinstance(error, app_commands.CheckFailure):
            msg = _("errors.no_permission")
        elif isinstance(original, discord.Forbidden):
            msg = _("errors.bot_forbidden")
        else:
            err_id = await capture_error(original, guild_id=interaction.guild_id, user_id=interaction.user.id,
                                         command=getattr(interaction.command, "qualified_name", None))
            msg = _("errors.unexpected", id=err_id)
        embed = th.error(_("common.error"), msg)
        try:
            if interaction.response.is_done():
                await interaction.followup.send(embed=embed, ephemeral=True)
            else:
                await interaction.response.send_message(embed=embed, ephemeral=True)
        except discord.HTTPException:
            pass


class NovaBot(commands.AutoShardedBot):
    def __init__(self) -> None:
        intents = discord.Intents.default()
        intents.members = True
        intents.message_content = True
        intents.voice_states = True
        intents.presences = settings.enable_presence_intent
        super().__init__(
            command_prefix=commands.when_mentioned,
            intents=intents,
            tree_cls=NovaTree,
            help_command=None,
            allowed_mentions=discord.AllowedMentions(everyone=False, roles=False, users=True, replied_user=False),
            chunk_guilds_at_startup=True,
            max_messages=5000,
        )
        self.maintenance: bool = False
        self.started_at = time.time()
        self.module_names: list[str] = []
        self._synced = False

    # ── Lifecycle ──
    async def setup_hook(self) -> None:
        self.maintenance = bool(await config.state("maintenance", False))
        from app.bot import modules

        for info in pkgutil.iter_modules(modules.__path__):
            name = f"app.bot.modules.{info.name}"
            try:
                await self.load_extension(name)
                self.module_names.append(info.name)
            except Exception:
                log.exception("Modul %s konnte nicht geladen werden", name)
        # Slash-Gruppen (/mod, /media, /ticket …) erben Modul und Hilfe-Kategorie ihres Cogs – sonst zählten sie als „core“
        for cog in self.cogs.values():
            for cmd in cog.__cog_app_commands__:
                if isinstance(cmd, app_commands.Group):
                    cmd.extras.setdefault("module", getattr(cog, "module", "core"))
                    cmd.extras.setdefault("help_category", getattr(cog, "help_category", None))
        log.info("%d Module geladen: %s", len(self.module_names), ", ".join(sorted(self.module_names)))
        self.flush_metrics.start()

    async def on_ready(self) -> None:
        log.info("Eingeloggt als %s (%s) – %d Server", self.user, self.user.id if self.user else "?", len(self.guilds))
        async with session_scope() as s:
            for g in self.guilds:
                await ensure_guild(s, g)
        if not self._synced:
            self._synced = True
            asyncio.create_task(self.sync_commands())
        await self.change_presence(activity=discord.Activity(type=discord.ActivityType.watching, name="/help"))

    async def sync_commands(self, force: bool = False) -> None:
        """Globale Commands nur synchronisieren, wenn sich ihre Definition geändert hat (schont Rate-Limits)."""
        try:
            payload = [c.to_dict(self.tree) for c in self.tree.get_commands()]
            digest = hashlib.sha256(json.dumps(payload, sort_keys=True, default=str).encode()).hexdigest()
            if settings.dev_guild_id.isdigit():
                await self.sync_dev_guild()
            if force or digest != await config.state("command_hash"):
                synced = await self.tree.sync()
                await config.set_state("command_hash", digest)
                log.info("%d globale Commands synchronisiert", len(synced))
            cc = self.get_cog("CustomCommands")
            if cc:
                await cc.sync_all()  # type: ignore[attr-defined]
        except Exception as exc:
            await capture_error(exc, command="sync_commands")

    async def sync_dev_guild(self) -> None:
        """Commands sofort in der Test-Guild verfügbar machen (scheitert still, solange der Bot dort noch nicht ist)."""
        if not self.get_guild(int(settings.dev_guild_id)):
            log.warning("DEV_GUILD_ID %s: Bot ist (noch) nicht auf diesem Server – bitte einladen", settings.dev_guild_id)
            return
        # Keine Kopie der globalen Commands mehr in die Guild – sonst erscheint jeder Befehl doppelt.
        # In der Guild liegen nur die Custom Commands; alte Kopien werden dabei entfernt.
        cc = self.get_cog("CustomCommands")
        try:
            if cc:
                await cc.sync_guild(int(settings.dev_guild_id))  # type: ignore[attr-defined]
            else:
                guild = discord.Object(int(settings.dev_guild_id))
                self.tree.clear_commands(guild=guild)
                await self.tree.sync(guild=guild)
            log.info("Guild-Commands bereinigt (keine Duplikate)")
        except discord.HTTPException as exc:
            log.warning("Dev-Guild-Sync fehlgeschlagen: %s", exc)

    async def on_guild_join(self, guild: discord.Guild) -> None:
        if settings.dev_guild_id == str(guild.id):
            await self.sync_dev_guild()
        async with session_scope() as s:
            await ensure_guild(s, guild)
        bus.publish(0, "guild", {"action": "join", "id": str(guild.id), "name": guild.name})
        log.info("Neuer Server: %s (%s)", guild.name, guild.id)

    async def on_guild_remove(self, guild: discord.Guild) -> None:
        await mark_left(guild.id)
        config.invalidate(guild.id)
        bus.publish(0, "guild", {"action": "leave", "id": str(guild.id), "name": guild.name})

    async def on_app_command_completion(self, interaction: discord.Interaction, command: app_commands.Command | app_commands.ContextMenu) -> None:
        if interaction.guild_id:
            metrics.incr(interaction.guild_id, "commands")
            metrics.incr(interaction.guild_id, f"cmd:{command.qualified_name.split(' ')[0]}")

    async def on_error(self, event_method: str, *args: Any, **kwargs: Any) -> None:
        import sys

        exc = sys.exc_info()[1]
        guild_id = None
        for a in args:
            g = getattr(a, "guild", None)
            if g is not None:
                guild_id = g.id
                break
        if exc:
            await capture_error(exc, guild_id=guild_id, command=f"event:{event_method}")

    async def set_maintenance(self, value: bool) -> None:
        self.maintenance = value
        await config.set_state("maintenance", value)
        status = discord.Status.dnd if value else discord.Status.online
        activity = discord.Game("🛠️ Wartungsmodus") if value else discord.Activity(type=discord.ActivityType.watching, name="/help")
        await self.change_presence(status=status, activity=activity)

    @tasks.loop(seconds=60)
    async def flush_metrics(self) -> None:
        for g in self.guilds:
            metrics.gauge(g.id, "members", g.member_count or len(g.members))
            if settings.enable_presence_intent:
                metrics.gauge(g.id, "online", sum(1 for m in g.members if m.status != discord.Status.offline))
            metrics.gauge(g.id, "voice_users", sum(len(vc.members) for vc in g.voice_channels))
        await metrics.flush()

    @flush_metrics.before_loop
    async def _before_flush(self) -> None:
        await self.wait_until_ready()

    async def close(self) -> None:
        try:
            await metrics.flush()
        finally:
            await super().close()
