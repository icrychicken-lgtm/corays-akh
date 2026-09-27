"""Self-Roles über Buttons/Select-Menüs (im Dashboard erstellt) und Rollen-Synchronisationsregeln."""
from __future__ import annotations

import asyncio
import logging
import re

import discord
from discord import app_commands
from discord.ext import commands

from app.bot.ui import confirm, handle_exception, reply
from app.core.events import bus
from app.core.embeds import hex_to_color, theme
from app.core.errors import UserError
from app.core.guild_config import config
from app.core.i18n import i18n
from app.core.records import log_event
from app.db.base import SessionLocal, session_scope
from app.db.models import RoleMenu

log = logging.getLogger("nova.roles")


def _emoji(value: str | None) -> discord.PartialEmoji | str | None:
    if not value:
        return None
    value = value.strip()
    if value.startswith("<"):
        try:
            return discord.PartialEmoji.from_str(value)
        except ValueError:
            return None
    return value


async def _menu(menu_id: int, guild_id: int) -> RoleMenu:
    async with SessionLocal() as s:
        menu = await s.get(RoleMenu, menu_id)
    if menu is None or menu.guild_id != guild_id:
        raise UserError("roles.menu_gone")
    return menu


def _check_required(menu: RoleMenu, member: discord.Member) -> None:
    if menu.required_role_id and not any(r.id == menu.required_role_id for r in member.roles):
        raise UserError("roles.required", role=f"<@&{menu.required_role_id}>")


class RoleButton(discord.ui.DynamicItem[discord.ui.Button], template=r"nova:rm:(?P<menu>\d+):(?P<role>\d+)"):
    def __init__(self, menu_id: int, role_id: int, label: str | None = None, emoji=None):
        super().__init__(discord.ui.Button(label=label, emoji=emoji, style=discord.ButtonStyle.secondary,
                                           custom_id=f"nova:rm:{menu_id}:{role_id}"))
        self.menu_id, self.role_id = menu_id, role_id

    @classmethod
    async def from_custom_id(cls, interaction, item, match: re.Match[str], /):
        return cls(int(match["menu"]), int(match["role"]))

    async def callback(self, interaction: discord.Interaction):
        try:
            if not await config.enabled(interaction.guild_id, "roles"):
                raise UserError("errors.module_disabled")
            menu = await _menu(self.menu_id, interaction.guild_id)
            member: discord.Member = interaction.user  # type: ignore[assignment]
            _check_required(menu, member)
            role = interaction.guild.get_role(self.role_id)
            if role is None or role >= interaction.guild.me.top_role or not any(str(o.get("role_id")) == str(self.role_id) for o in menu.options):
                raise UserError("roles.role_unavailable")
            _ = await i18n.for_guild(interaction.guild_id)
            th = await theme(interaction.guild)
            if role in member.roles:
                await member.remove_roles(role, reason=f"Self-Role ({menu.name})")
                await reply(interaction, th.success(_("roles.removed_title"), _("roles.removed", role=role.mention)))
            else:
                menu_roles = {int(o["role_id"]) for o in menu.options if o.get("role_id")}
                owned = [r for r in member.roles if r.id in menu_roles]
                if menu.max_values and len(owned) >= menu.max_values:
                    if menu.max_values == 1:
                        await member.remove_roles(*owned, reason=f"Self-Role ({menu.name})")
                    else:
                        raise UserError("roles.max_reached", max=menu.max_values)
                await member.add_roles(role, reason=f"Self-Role ({menu.name})")
                await reply(interaction, th.success(_("roles.added_title"), _("roles.added", role=role.mention)))
            await log_event(interaction.guild_id, "role", "self_role", user=member, target_id=role.id, content=role.name)
        except Exception as exc:  # noqa: BLE001
            await handle_exception(interaction, exc, "role_button")


class RoleSelect(discord.ui.DynamicItem[discord.ui.Select], template=r"nova:rms:(?P<menu>\d+)"):
    def __init__(self, menu_id: int, options: list[discord.SelectOption] | None = None, max_values: int = 1, placeholder: str | None = None):
        opts = options or [discord.SelectOption(label="…", value="0")]
        super().__init__(discord.ui.Select(custom_id=f"nova:rms:{menu_id}", options=opts, min_values=0,
                                           max_values=max(1, min(max_values or len(opts), len(opts))), placeholder=placeholder))
        self.menu_id = menu_id

    @classmethod
    async def from_custom_id(cls, interaction, item: discord.ui.Select, match: re.Match[str], /):
        inst = cls(int(match["menu"]))
        inst.item = item
        return inst

    async def callback(self, interaction: discord.Interaction):
        try:
            if not await config.enabled(interaction.guild_id, "roles"):
                raise UserError("errors.module_disabled")
            menu = await _menu(self.menu_id, interaction.guild_id)
            member: discord.Member = interaction.user  # type: ignore[assignment]
            _check_required(menu, member)
            guild = interaction.guild
            menu_roles = {int(o["role_id"]) for o in menu.options if o.get("role_id")}
            chosen = {int(v) for v in self.item.values if v.isdigit()} & menu_roles
            if menu.max_values and len(chosen) > menu.max_values:
                raise UserError("roles.max_reached", max=menu.max_values)
            add = [guild.get_role(r) for r in chosen if not member.get_role(r)]
            remove = [guild.get_role(r) for r in menu_roles - chosen if member.get_role(r)]
            add = [r for r in add if r and r < guild.me.top_role]
            remove = [r for r in remove if r and r < guild.me.top_role]
            if add:
                await member.add_roles(*add, reason=f"Self-Role ({menu.name})")
            if remove:
                await member.remove_roles(*remove, reason=f"Self-Role ({menu.name})")
            _ = await i18n.for_guild(interaction.guild_id)
            th = await theme(guild)
            lines = [f"➕ {r.mention}" for r in add] + [f"➖ {r.mention}" for r in remove]
            await reply(interaction, th.success(_("roles.updated_title"), "\n".join(lines) or _("roles.no_change")))
        except Exception as exc:  # noqa: BLE001
            await handle_exception(interaction, exc, "role_select")


class Roles(commands.Cog):
    module = "roles"
    help_category = "admin"

    role_group = app_commands.Group(name="role", description="Rollen vergeben oder wegnehmen – einzeln oder an alle", guild_only=True,
                                    default_permissions=discord.Permissions(manage_roles=True), extras={"module": "core"})

    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.jobs: dict[int, dict] = {}

    # ── Rollen-Rechte prüfen ──
    @staticmethod
    def check_manageable(guild: discord.Guild, actor: discord.abc.User, role: discord.Role) -> None:
        if role.is_default() or role.managed or role >= guild.me.top_role:
            raise UserError("roles.role_unavailable")
        if isinstance(actor, discord.Member) and actor.id != guild.owner_id and role >= actor.top_role:
            raise UserError("mod.err_hierarchy")

    # ── Massen-Vergabe (auch vom Dashboard genutzt) ──
    def start_mass(self, guild: discord.Guild, role: discord.Role, add: bool, target: str, actor: discord.abc.User,
                   only_role: discord.Role | None = None, on_progress=None) -> dict:
        if (job := self.jobs.get(guild.id)) and job["running"]:
            raise UserError("roles.mass_running")
        members = [m for m in guild.members if (target == "all" or (target == "bots") == m.bot)]
        if only_role is not None:
            members = [m for m in members if only_role in m.roles]
        # Nur wer die Rolle noch nicht hat (geben) bzw. wer sie hat (wegnehmen)
        members = [m for m in members if (role not in m.roles) == add]
        job = {"role": role.name, "role_id": str(role.id), "add": add, "total": len(members), "done": 0, "failed": 0, "running": True, "cancel": False,
               "actor": str(actor)}
        self.jobs[guild.id] = job

        async def run():
            reason = f"Massen-{'Vergabe' if add else 'Entzug'} durch {actor}"
            for i, m in enumerate(members, 1):
                if job["cancel"]:
                    break
                try:
                    await (m.add_roles(role, reason=reason) if add else m.remove_roles(role, reason=reason))
                    job["done"] += 1
                except discord.HTTPException:
                    job["failed"] += 1
                if on_progress and (i % 10 == 0 or i == len(members)):
                    await on_progress(job)
                bus.publish(guild.id, "mass_role", {k: v for k, v in job.items() if k != "cancel"})
            job["running"] = False
            if on_progress:
                await on_progress(job)
            bus.publish(guild.id, "mass_role", {k: v for k, v in job.items() if k != "cancel"})
            await log_event(guild.id, "role", "mass_add" if add else "mass_remove", user=actor, target_id=role.id,
                            content=f"{role.name}: {job['done']} / {job['total']}")

        asyncio.create_task(run())
        return job

    @role_group.command(name="give", description="Einem Mitglied eine Rolle geben")
    async def role_give(self, interaction: discord.Interaction, user: discord.Member, role: discord.Role):
        self.check_manageable(interaction.guild, interaction.user, role)
        await user.add_roles(role, reason=f"/role give durch {interaction.user}")
        _ = await i18n.for_guild(interaction.guild_id)
        await reply(interaction, (await theme(interaction.guild)).success(_("common.success"), _("roles.given", role=role.mention, user=user.mention)))

    @role_group.command(name="take", description="Einem Mitglied eine Rolle wegnehmen")
    async def role_take(self, interaction: discord.Interaction, user: discord.Member, role: discord.Role):
        self.check_manageable(interaction.guild, interaction.user, role)
        await user.remove_roles(role, reason=f"/role take durch {interaction.user}")
        _ = await i18n.for_guild(interaction.guild_id)
        await reply(interaction, (await theme(interaction.guild)).success(_("common.success"), _("roles.taken", role=role.mention, user=user.mention)))

    @role_group.command(name="all", description="Allen Mitgliedern auf einmal eine Rolle geben oder wegnehmen")
    @app_commands.describe(action="Geben oder wegnehmen", role="Welche Rolle", target="Wen betrifft es", only_with="Nur Mitglieder, die diese Rolle haben")
    @app_commands.choices(action=[app_commands.Choice(name="Geben", value="add"), app_commands.Choice(name="Wegnehmen", value="remove")],
                          target=[app_commands.Choice(name="Nur Menschen", value="humans"), app_commands.Choice(name="Alle (inkl. Bots)", value="all"),
                                  app_commands.Choice(name="Nur Bots", value="bots")])
    async def role_all(self, interaction: discord.Interaction, action: app_commands.Choice[str], role: discord.Role,
                       target: app_commands.Choice[str] | None = None, only_with: discord.Role | None = None):
        self.check_manageable(interaction.guild, interaction.user, role)
        _ = await i18n.for_guild(interaction.guild_id)
        th = await theme(interaction.guild)
        add = action.value == "add"
        tgt = target.value if target else "humans"
        affected = [m for m in interaction.guild.members if (tgt == "all" or (tgt == "bots") == m.bot)
                    and (only_with is None or only_with in m.roles) and ((role not in m.roles) if add else (role in m.roles))]
        if not affected:
            raise UserError("roles.mass_nothing")
        if not await confirm(interaction, _("roles.mass_confirm_title"),
                             _("roles.mass_confirm_add" if add else "roles.mass_confirm_remove", role=role.mention, count=len(affected))):
            return
        msg = await interaction.followup.send(embed=th.info(_("roles.mass_title"), _("roles.mass_progress", done=0, total=len(affected), failed=0)), wait=True)

        async def progress(job: dict):
            desc = _("roles.mass_progress", done=job["done"], total=job["total"], failed=job["failed"])
            e = (th.info if job["running"] else th.success)(_("roles.mass_title") if job["running"] else _("roles.mass_done"), desc)
            try:
                await msg.edit(embed=e)
            except discord.HTTPException:
                pass

        self.start_mass(interaction.guild, role, add, tgt, interaction.user, only_with, progress)

    @role_group.command(name="cancel", description="Laufende Massen-Vergabe abbrechen")
    async def role_cancel(self, interaction: discord.Interaction):
        job = self.jobs.get(interaction.guild_id)
        if not job or not job["running"]:
            raise UserError("roles.mass_none")
        job["cancel"] = True
        _ = await i18n.for_guild(interaction.guild_id)
        await reply(interaction, (await theme(interaction.guild)).warning(_("roles.mass_title"), _("roles.mass_cancelled")))

    async def build(self, guild: discord.Guild, menu: RoleMenu) -> tuple[discord.Embed, discord.ui.View]:
        th = await theme(guild)
        _ = await i18n.for_guild(guild.id)
        e = th.embed(menu.title or menu.name, menu.description or None, icon=False)
        if menu.color:
            e.colour = hex_to_color(menu.color)
        if menu.image_url:
            e.set_image(url=menu.image_url)
        lines = []
        view = discord.ui.View(timeout=None)
        valid = [o for o in menu.options if o.get("role_id") and guild.get_role(int(o["role_id"]))][:25]
        for o in valid:
            desc = f" — {o['description']}" if o.get("description") else ""
            lines.append(f"{o.get('emoji') or '•'} <@&{o['role_id']}>{desc}")
        if lines:
            e.add_field(name=_("roles.available"), value="\n".join(lines)[:1024], inline=False)
        if menu.required_role_id:
            e.add_field(name=_("roles.requirement"), value=f"<@&{menu.required_role_id}>", inline=False)
        if not valid:
            raise UserError("roles.menu_empty")
        if menu.style == "select":
            options = [discord.SelectOption(label=(o.get("label") or guild.get_role(int(o["role_id"])).name)[:100], value=str(o["role_id"]),
                                            description=(o.get("description") or None) and o["description"][:100], emoji=_emoji(o.get("emoji")))
                       for o in valid]
            view.add_item(RoleSelect(menu.id, options, menu.max_values, _("roles.select_placeholder")))
        else:
            for o in valid:
                role = guild.get_role(int(o["role_id"]))
                view.add_item(RoleButton(menu.id, role.id, (o.get("label") or role.name)[:80], _emoji(o.get("emoji"))))
        return e, view

    async def publish(self, guild: discord.Guild, menu_id: int) -> RoleMenu:
        """Postet das Menü neu bzw. aktualisiert die bestehende Nachricht."""
        async with session_scope() as s:
            menu = await s.get(RoleMenu, menu_id)
            if menu is None or menu.guild_id != guild.id:
                raise UserError("roles.menu_gone")
            channel = guild.get_channel(menu.channel_id or 0)
            if not isinstance(channel, discord.TextChannel):
                raise UserError("roles.no_channel")
            embed, view = await self.build(guild, menu)
            msg = None
            if menu.message_id:
                try:
                    msg = await channel.fetch_message(menu.message_id)
                    await msg.edit(embed=embed, view=view)
                except discord.NotFound:
                    msg = None
            if msg is None:
                msg = await channel.send(embed=embed, view=view)
                menu.message_id = msg.id
            return menu

    # ── Sync-Regeln ──
    async def apply_rules(self, member: discord.Member) -> None:
        cfg = await config.get(member.guild.id, "roles")
        me = member.guild.me
        role_ids = {r.id for r in member.roles}
        add, remove = [], []
        for rule in cfg.get("sync_rules") or []:
            src, tgt = rule.get("source"), rule.get("target")
            if not src or not tgt:
                continue
            target = member.guild.get_role(int(tgt))
            if target is None or target >= me.top_role:
                continue
            if int(src) in role_ids and target.id not in role_ids:
                add.append(target)
            elif int(src) not in role_ids and target.id in role_ids and rule.get("remove", True):
                remove.append(target)
        try:
            if add:
                await member.add_roles(*add, reason="Rollen-Sync")
            if remove:
                await member.remove_roles(*remove, reason="Rollen-Sync")
        except discord.HTTPException as exc:
            log.debug("Rollen-Sync fehlgeschlagen: %s", exc)

    async def sync_all(self, guild: discord.Guild) -> int:
        count = 0
        for m in guild.members:
            if not m.bot:
                await self.apply_rules(m)
                count += 1
        return count

    @commands.Cog.listener()
    async def on_member_update(self, before: discord.Member, after: discord.Member):
        if before.roles != after.roles and await config.enabled(after.guild.id, "roles"):
            await self.apply_rules(after)


async def setup(bot: commands.Bot):
    bot.add_dynamic_items(RoleButton, RoleSelect)
    await bot.add_cog(Roles(bot))
