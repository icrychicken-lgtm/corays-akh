"""Gangs: Crews gründen, Mitglieder einladen (Annehmen/Ablehnen-Buttons), Ränge, Gang-Kasse und Rangliste.
Gang-Power = XP aller Mitglieder + Gang-Kasse."""
from __future__ import annotations

import re

import discord
from discord import app_commands
from discord.ext import commands
from sqlalchemy import func, select

from app.bot.ui import confirm, handle_exception, reply
from app.core.embeds import fmt_num, theme
from app.core.errors import UserError
from app.core.guild_config import config
from app.core.i18n import i18n
from app.core.records import log_event
from app.core.timeutil import ts
from app.db.base import SessionLocal, session_scope
from app.db.models import Gang, GangMember, Member
from app.services.members import ensure_member

RANK_ICON = {"leader": "👑", "officer": "⭐", "member": "•"}


async def gang_of(guild_id: int, user_id: int) -> tuple[Gang, GangMember] | None:
    async with SessionLocal() as db:
        gm = await db.get(GangMember, (guild_id, user_id))
        if gm is None:
            return None
        return await db.get(Gang, gm.gang_id), gm


async def gang_power(gang_id: int) -> tuple[int, int]:
    """→ (Power, Mitgliederzahl)"""
    async with SessionLocal() as db:
        g = await db.get(Gang, gang_id)
        xp, count = (await db.execute(select(func.coalesce(func.sum(Member.xp), 0), func.count(GangMember.user_id))
                                      .select_from(GangMember).join(Member, (Member.guild_id == GangMember.guild_id) & (Member.user_id == GangMember.user_id), isouter=True)
                                      .where(GangMember.gang_id == gang_id))).one()
    return int(xp) + (g.bank if g else 0), int(count)


class InviteButton(discord.ui.DynamicItem[discord.ui.Button], template=r"nova:gang:(?P<action>accept|decline):(?P<gang>\d+):(?P<user>\d+)"):
    def __init__(self, action: str, gang_id: int, user_id: int, label: str | None = None):
        style = discord.ButtonStyle.success if action == "accept" else discord.ButtonStyle.secondary
        super().__init__(discord.ui.Button(label=label, style=style, emoji="🤝" if action == "accept" else "✖️",
                                           custom_id=f"nova:gang:{action}:{gang_id}:{user_id}"))
        self.action, self.gang_id, self.user_id = action, gang_id, user_id

    @classmethod
    async def from_custom_id(cls, interaction, item, match: re.Match[str], /):
        return cls(match["action"], int(match["gang"]), int(match["user"]))

    async def callback(self, interaction: discord.Interaction):
        try:
            if interaction.user.id != self.user_id:
                raise UserError("gang.not_your_invite")
            cog: Gangs = interaction.client.get_cog("Gangs")  # type: ignore[assignment]
            _ = await i18n.for_guild(interaction.guild_id)
            th = await theme(interaction.guild)
            if self.action == "decline":
                await interaction.response.edit_message(embed=th.info(_("gang.invite_title"), _("gang.declined", user=interaction.user.mention)), view=None)
                return
            gang = await cog.join(interaction.guild, interaction.user, self.gang_id)  # type: ignore[arg-type]
            await interaction.response.edit_message(embed=th.success(_("gang.invite_title"), _("gang.joined", user=interaction.user.mention, gang=f"{gang.emoji} {gang.name}")), view=None)
        except Exception as exc:  # noqa: BLE001
            await handle_exception(interaction, exc, "gang_invite")


@app_commands.guild_only()
class Gangs(commands.Cog):
    module = "gangs"
    help_category = "economy"

    gang = app_commands.Group(name="gang", description="Gangs & Crews", guild_only=True)

    def __init__(self, bot: commands.Bot):
        self.bot = bot

    async def _announce(self, guild: discord.Guild, text: str) -> None:
        cfg = await config.get(guild.id, "gangs")
        ch = guild.get_channel(cfg.id("announce_channel") or 0)
        if isinstance(ch, discord.TextChannel):
            th = await theme(guild)
            try:
                await ch.send(embed=th.embed(None, text, icon=False))
            except discord.HTTPException:
                pass

    async def _mine(self, interaction: discord.Interaction, ranks: tuple[str, ...] = ("leader", "officer", "member")) -> tuple[Gang, GangMember]:
        res = await gang_of(interaction.guild_id, interaction.user.id)
        if res is None:
            raise UserError("gang.not_in_gang")
        if res[1].rank not in ranks:
            raise UserError("gang.rank_required")
        return res

    async def join(self, guild: discord.Guild, member: discord.Member, gang_id: int) -> Gang:
        cfg = await config.get(guild.id, "gangs")
        async with session_scope() as db:
            g = await db.get(Gang, gang_id)
            if g is None or g.guild_id != guild.id:
                raise UserError("gang.not_found")
            if await db.get(GangMember, (guild.id, member.id)):
                raise UserError("gang.already_in_gang")
            count = (await db.execute(select(func.count()).select_from(GangMember).where(GangMember.gang_id == gang_id))).scalar_one()
            if count >= int(cfg.get("max_members", 15)):
                raise UserError("gang.full")
            await ensure_member(db, guild.id, member)
            db.add(GangMember(guild_id=guild.id, user_id=member.id, gang_id=gang_id, rank="member"))
        await log_event(guild.id, "gang", "join", user=member, content=g.name)
        _ = await i18n.for_guild(guild.id)
        await self._announce(guild, _("gang.news_join", user=member.mention, gang=f"{g.emoji} **{g.name}**"))
        return g

    # ── Commands ──
    @gang.command(name="create", description="Gründe deine eigene Gang")
    @app_commands.describe(name="Name der Gang", tag="Kürzel, z. B. 187 oder LGD", emoji="Symbol der Gang")
    async def create(self, interaction: discord.Interaction, name: app_commands.Range[str, 3, 32], tag: app_commands.Range[str, 1, 6], emoji: str = "🏴"):
        cfg = await config.get(interaction.guild_id, "gangs")
        eco = await config.get(interaction.guild_id, "economy")
        cost = int(cfg.get("create_cost", 5000))
        if await gang_of(interaction.guild_id, interaction.user.id):
            raise UserError("gang.already_in_gang")
        emoji = emoji.strip()[:64] or "🏴"
        async with session_scope() as db:
            if (await db.execute(select(Gang.id).where(Gang.guild_id == interaction.guild_id, func.lower(Gang.name) == name.lower()))).first():
                raise UserError("gang.name_taken")
            me = await ensure_member(db, interaction.guild_id, interaction.user)
            if me.coins < cost:
                raise UserError("gang.too_poor", cost=f"{eco.get('currency_emoji')} {fmt_num(cost)}")
            me.coins -= cost
            g = Gang(guild_id=interaction.guild_id, name=name.strip(), tag=tag.strip().upper(), emoji=emoji, leader_id=interaction.user.id, bank=0, wins=0)
            db.add(g)
            await db.flush()
            db.add(GangMember(guild_id=interaction.guild_id, user_id=interaction.user.id, gang_id=g.id, rank="leader"))
        _ = await i18n.for_guild(interaction.guild_id)
        th = await theme(interaction.guild)
        await log_event(interaction.guild_id, "gang", "create", user=interaction.user, content=g.name)
        await self._announce(interaction.guild, _("gang.news_create", user=interaction.user.mention, gang=f"{g.emoji} **{g.name}** [{g.tag}]"))
        e = th.success(_("gang.created_title"), _("gang.created", gang=f"{g.emoji} **{g.name}** `[{g.tag}]`", cost=f"{eco.get('currency_emoji')} {fmt_num(cost)}"))
        e.add_field(name=_("gang.next_steps"), value=_("gang.next_steps_text"), inline=False)
        await reply(interaction, e, ephemeral=False)

    @gang.command(name="invite", description="Lade jemanden in deine Gang ein")
    async def invite(self, interaction: discord.Interaction, user: discord.Member):
        g, _gm = await self._mine(interaction, ("leader", "officer"))
        if user.bot:
            raise UserError("gang.no_bots")
        if await gang_of(interaction.guild_id, user.id):
            raise UserError("gang.target_in_gang", user=user.mention)
        _ = await i18n.for_guild(interaction.guild_id)
        th = await theme(interaction.guild)
        e = th.embed(_("gang.invite_title"), _("gang.invite", user=user.mention, by=interaction.user.mention, gang=f"{g.emoji} **{g.name}** `[{g.tag}]`"), icon=False)
        v = discord.ui.View(timeout=None)
        v.add_item(InviteButton("accept", g.id, user.id, _("gang.accept")))
        v.add_item(InviteButton("decline", g.id, user.id, _("gang.decline")))
        await reply(interaction, e, content=user.mention, view=v, ephemeral=False)

    @gang.command(name="leave", description="Verlasse deine Gang")
    async def leave(self, interaction: discord.Interaction):
        g, gm = await self._mine(interaction)
        _ = await i18n.for_guild(interaction.guild_id)
        th = await theme(interaction.guild)
        if gm.rank == "leader":
            _power, count = await gang_power(g.id)
            if count > 1:
                raise UserError("gang.leader_cant_leave")
            return await self._disband(interaction, g)
        async with session_scope() as db:
            row = await db.get(GangMember, (interaction.guild_id, interaction.user.id))
            if row:
                await db.delete(row)
        await reply(interaction, th.info(_("gang.left_title"), _("gang.left", gang=f"{g.emoji} {g.name}")), ephemeral=False)

    @gang.command(name="kick", description="Wirf jemanden aus deiner Gang (Leader/Officer)")
    async def kick(self, interaction: discord.Interaction, user: discord.Member):
        g, gm = await self._mine(interaction, ("leader", "officer"))
        async with session_scope() as db:
            target = await db.get(GangMember, (interaction.guild_id, user.id))
            if target is None or target.gang_id != g.id:
                raise UserError("gang.target_not_member", user=user.mention)
            if target.rank == "leader" or (target.rank == "officer" and gm.rank != "leader"):
                raise UserError("gang.rank_required")
            await db.delete(target)
        _ = await i18n.for_guild(interaction.guild_id)
        await reply(interaction, (await theme(interaction.guild)).success(_("common.success"), _("gang.kicked", user=user.mention)), ephemeral=False)

    @gang.command(name="promote", description="Mitglied zum Officer machen bzw. zurückstufen (nur Leader)")
    async def promote(self, interaction: discord.Interaction, user: discord.Member):
        g, _gm = await self._mine(interaction, ("leader",))
        async with session_scope() as db:
            target = await db.get(GangMember, (interaction.guild_id, user.id))
            if target is None or target.gang_id != g.id or target.rank == "leader":
                raise UserError("gang.target_not_member", user=user.mention)
            target.rank = "member" if target.rank == "officer" else "officer"
            rank = target.rank
        _ = await i18n.for_guild(interaction.guild_id)
        await reply(interaction, (await theme(interaction.guild)).success(_("common.success"), _("gang.rank_set", user=user.mention, rank=_("gang.rank." + rank))), ephemeral=False)

    @gang.command(name="deposit", description="Coins in die Gang-Kasse einzahlen")
    async def deposit(self, interaction: discord.Interaction, amount: app_commands.Range[int, 1, 1_000_000_000]):
        g, _gm = await self._mine(interaction)
        eco = await config.get(interaction.guild_id, "economy")
        async with session_scope() as db:
            me = await ensure_member(db, interaction.guild_id, interaction.user)
            if me.coins < amount:
                raise UserError("eco.insufficient", balance=fmt_num(me.coins))
            me.coins -= amount
            row = await db.get(Gang, g.id)
            row.bank += amount
            gm = await db.get(GangMember, (interaction.guild_id, interaction.user.id))
            gm.contributed += amount
            bank = row.bank
        _ = await i18n.for_guild(interaction.guild_id)
        await reply(interaction, (await theme(interaction.guild)).success(_("gang.bank_title"),
                    _("gang.deposited", amount=f"{eco.get('currency_emoji')} {fmt_num(amount)}", bank=f"{eco.get('currency_emoji')} {fmt_num(bank)}")), ephemeral=False)

    @gang.command(name="withdraw", description="Coins aus der Gang-Kasse auszahlen (nur Leader)")
    async def withdraw(self, interaction: discord.Interaction, amount: app_commands.Range[int, 1, 1_000_000_000]):
        g, _gm = await self._mine(interaction, ("leader",))
        eco = await config.get(interaction.guild_id, "economy")
        async with session_scope() as db:
            row = await db.get(Gang, g.id)
            if row.bank < amount:
                raise UserError("gang.bank_too_low", bank=fmt_num(row.bank))
            row.bank -= amount
            me = await ensure_member(db, interaction.guild_id, interaction.user)
            me.coins += amount
            bank = row.bank
        await log_event(interaction.guild_id, "gang", "withdraw", user=interaction.user, content=f"{g.name}: {amount}")
        _ = await i18n.for_guild(interaction.guild_id)
        await reply(interaction, (await theme(interaction.guild)).success(_("gang.bank_title"),
                    _("gang.withdrawn", amount=f"{eco.get('currency_emoji')} {fmt_num(amount)}", bank=f"{eco.get('currency_emoji')} {fmt_num(bank)}")), ephemeral=False)

    @gang.command(name="info", description="Infos über eine Gang")
    async def info(self, interaction: discord.Interaction, name: str | None = None):
        _ = await i18n.for_guild(interaction.guild_id)
        th = await theme(interaction.guild)
        eco = await config.get(interaction.guild_id, "economy")
        async with SessionLocal() as db:
            if name:
                g = (await db.execute(select(Gang).where(Gang.guild_id == interaction.guild_id, func.lower(Gang.name) == name.lower()))).scalar_one_or_none()
            else:
                res = await gang_of(interaction.guild_id, interaction.user.id)
                g = res[0] if res else None
            if g is None:
                raise UserError("gang.not_found" if name else "gang.not_in_gang")
            members = (await db.execute(select(GangMember, Member).join(Member, (Member.guild_id == GangMember.guild_id) & (Member.user_id == GangMember.user_id), isouter=True)
                                        .where(GangMember.gang_id == g.id))).all()
        power, count = await gang_power(g.id)
        order = {"leader": 0, "officer": 1, "member": 2}
        members = sorted(members, key=lambda r: (order.get(r[0].rank, 3), -(r[1].xp if r[1] else 0)))
        lines = [f"{RANK_ICON[gm.rank]} <@{gm.user_id}> · Lv. {m.level if m else 0}" for gm, m in members[:25]]
        e = th.embed(f"{g.emoji} {g.name}  [{g.tag}]", g.description or None, icon=False)
        e.add_field(name=_("gang.f_power"), value=f"⚡ **{fmt_num(power)}**", inline=True)
        e.add_field(name=_("gang.f_bank"), value=f"{eco.get('currency_emoji')} {fmt_num(g.bank)}", inline=True)
        e.add_field(name=_("gang.f_members"), value=str(count), inline=True)
        e.add_field(name=_("gang.f_roster"), value="\n".join(lines) or "—", inline=False)
        e.set_footer(text=_("gang.founded", date=g.created_at.strftime("%d.%m.%Y")))
        await reply(interaction, e, ephemeral=False)

    @info.autocomplete("name")
    async def _info_ac(self, interaction: discord.Interaction, current: str):
        async with SessionLocal() as db:
            rows = (await db.execute(select(Gang).where(Gang.guild_id == interaction.guild_id).limit(50))).scalars().all()
        return [app_commands.Choice(name=f"{g.emoji} {g.name} [{g.tag}]"[:100], value=g.name) for g in rows if current.lower() in g.name.lower()][:25]

    @gang.command(name="top", description="Rangliste der mächtigsten Gangs")
    async def top(self, interaction: discord.Interaction):
        _ = await i18n.for_guild(interaction.guild_id)
        th = await theme(interaction.guild)
        async with SessionLocal() as db:
            gangs = (await db.execute(select(Gang).where(Gang.guild_id == interaction.guild_id))).scalars().all()
        if not gangs:
            await reply(interaction, th.info(_("gang.top_title"), _("gang.top_empty")), ephemeral=False)
            return
        ranked = []
        for g in gangs:
            power, count = await gang_power(g.id)
            ranked.append((power, count, g))
        ranked.sort(key=lambda x: -x[0])
        medals = {1: "🥇", 2: "🥈", 3: "🥉"}
        lines = [f"{medals.get(i, f'`#{i:>2}`')} {g.emoji} **{g.name}** `[{g.tag}]` — ⚡ {fmt_num(p)} · {c} 👥" for i, (p, c, g) in enumerate(ranked[:15], 1)]
        e = th.embed(_("gang.top_title"), "\n".join(lines), icon=False)
        e.set_footer(text=_("gang.power_explained"))
        await reply(interaction, e, ephemeral=False)

    @gang.command(name="disband", description="Gang auflösen (nur Leader)")
    async def disband(self, interaction: discord.Interaction):
        g, _gm = await self._mine(interaction, ("leader",))
        await self._disband(interaction, g)

    async def _disband(self, interaction: discord.Interaction, g: Gang) -> None:
        _ = await i18n.for_guild(interaction.guild_id)
        if not await confirm(interaction, _("gang.disband_title"), _("gang.disband_confirm", gang=f"{g.emoji} {g.name}", bank=fmt_num(g.bank))):
            return
        async with session_scope() as db:
            row = await db.get(Gang, g.id)
            if row:
                me = await ensure_member(db, interaction.guild_id, interaction.user)
                me.coins += row.bank  # Kasse geht an den Leader
                await db.delete(row)
        await log_event(interaction.guild_id, "gang", "disband", user=interaction.user, content=g.name)
        await self._announce(interaction.guild, _("gang.news_disband", gang=f"{g.emoji} **{g.name}**"))
        await reply(interaction, (await theme(interaction.guild)).info(_("gang.disband_title"), _("gang.disbanded", gang=g.name)), ephemeral=False)


async def setup(bot: commands.Bot):
    bot.add_dynamic_items(InviteButton)
    await bot.add_cog(Gangs(bot))
