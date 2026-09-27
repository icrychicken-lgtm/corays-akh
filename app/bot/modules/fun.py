"""Fun- & Info-Commands: 8ball, coinflip, dice, roll, choose, ship, rate, avatar, banner, userinfo, serverinfo, ping."""
from __future__ import annotations

import hashlib
import random
import re
import time

import discord
from discord import app_commands
from discord.ext import commands

from app.bot.ui import reply
from app.core.embeds import fmt_num, progress_bar, theme
from app.core.i18n import i18n
from app.core.timeutil import ts
from app.runtime import STARTED_AT


def _stable_percent(*parts: int | str) -> int:
    digest = hashlib.sha256("|".join(map(str, parts)).encode()).hexdigest()
    return int(digest[:8], 16) % 101


class Fun(commands.Cog):
    module = "fun"
    help_category = "fun"

    def __init__(self, bot: commands.Bot):
        self.bot = bot

    @app_commands.command(name="8ball", description="Stelle der magischen 8-Ball eine Frage")
    @app_commands.describe(question="Deine Frage")
    async def eightball(self, interaction: discord.Interaction, question: app_commands.Range[str, 3, 200]):
        _ = await i18n.for_guild(interaction.guild_id)
        th = await theme(interaction.guild)
        answers = _("fun.8ball_answers").split("|")
        e = th.embed("🎱 8-Ball", user=interaction.user)
        e.add_field(name=_("fun.question"), value=question, inline=False)
        e.add_field(name=_("fun.answer"), value=f"**{random.choice(answers)}**", inline=False)
        await reply(interaction, e, ephemeral=False)

    @app_commands.command(name="coinflip", description="Wirf eine Münze")
    async def coinflip(self, interaction: discord.Interaction):
        _ = await i18n.for_guild(interaction.guild_id)
        th = await theme(interaction.guild)
        side = random.choice([_("fun.heads"), _("fun.tails")])
        await reply(interaction, th.embed("🪙 Coinflip", _("fun.coin_result", side=side)), ephemeral=False)

    @app_commands.command(name="dice", description="Würfle einen oder mehrere Würfel")
    @app_commands.describe(count="Anzahl Würfel", sides="Seiten pro Würfel")
    async def dice(self, interaction: discord.Interaction, count: app_commands.Range[int, 1, 10] = 1,
                   sides: app_commands.Range[int, 2, 1000] = 6):
        _ = await i18n.for_guild(interaction.guild_id)
        th = await theme(interaction.guild)
        rolls = [random.randint(1, sides) for _i in range(count)]
        faces = " ".join(f"`{r}`" for r in rolls)
        await reply(interaction, th.embed("🎲 " + _("fun.dice_title"), _("fun.dice_result", rolls=faces, total=sum(rolls), count=count, sides=sides)), ephemeral=False)

    @app_commands.command(name="roll", description="Würfelnotation wie 2d20+3 oder Zufallszahl 1–100")
    @app_commands.describe(expression="z. B. 2d20+3, d6 oder 100")
    async def roll(self, interaction: discord.Interaction, expression: str = "100"):
        _ = await i18n.for_guild(interaction.guild_id)
        th = await theme(interaction.guild)
        expr = expression.replace(" ", "").lower()
        m = re.fullmatch(r"(\d{0,2})d(\d{1,4})([+-]\d{1,4})?", expr)
        if m:
            n, s, mod = int(m.group(1) or 1), int(m.group(2)), int(m.group(3) or 0)
            if not (1 <= n <= 50 and 2 <= s <= 1000):
                await reply(interaction, th.error(_("common.error"), _("fun.roll_invalid")))
                return
            rolls = [random.randint(1, s) for _i in range(n)]
            total = sum(rolls) + mod
            desc = _("fun.roll_result", expr=expr, rolls=", ".join(map(str, rolls)), mod=f"{mod:+d}" if mod else "", total=total)
        elif expr.isdigit() and 1 <= int(expr) <= 1_000_000_000:
            total = random.randint(1, int(expr))
            desc = _("fun.roll_simple", max=expr, total=total)
        else:
            await reply(interaction, th.error(_("common.error"), _("fun.roll_invalid")))
            return
        await reply(interaction, th.embed("🎲 Roll", desc), ephemeral=False)

    @app_commands.command(name="choose", description="Lass den Bot zwischen Optionen wählen")
    @app_commands.describe(options="Optionen, getrennt mit Komma oder |")
    async def choose(self, interaction: discord.Interaction, options: app_commands.Range[str, 3, 500]):
        _ = await i18n.for_guild(interaction.guild_id)
        th = await theme(interaction.guild)
        items = [o.strip() for o in re.split(r"[|,]", options) if o.strip()]
        if len(items) < 2:
            await reply(interaction, th.error(_("common.error"), _("fun.choose_min")))
            return
        await reply(interaction, th.embed("🤔 " + _("fun.choose_title"), _("fun.choose_result", choice=random.choice(items))), ephemeral=False)

    @app_commands.command(name="ship", description="Wie gut passen zwei User zusammen?")
    async def ship(self, interaction: discord.Interaction, user1: discord.Member, user2: discord.Member | None = None):
        _ = await i18n.for_guild(interaction.guild_id)
        th = await theme(interaction.guild)
        user2 = user2 or interaction.user  # type: ignore[assignment]
        a, b = sorted([user1.id, user2.id])
        pct = _stable_percent("ship", a, b)
        name = user1.display_name[: len(user1.display_name) // 2 + 1] + user2.display_name[len(user2.display_name) // 2:]
        verdict = _("fun.ship_high") if pct >= 75 else _("fun.ship_mid") if pct >= 40 else _("fun.ship_low")
        e = th.embed(f"💘 {name}", f"{user1.mention} × {user2.mention}\n\n{progress_bar(pct, 100)} **{pct}%**\n{verdict}")
        await reply(interaction, e, ephemeral=False)

    @app_commands.command(name="rate", description="Bewerte etwas auf einer Skala von 0 bis 10")
    async def rate(self, interaction: discord.Interaction, thing: app_commands.Range[str, 1, 100]):
        _ = await i18n.for_guild(interaction.guild_id)
        th = await theme(interaction.guild)
        score = _stable_percent("rate", thing.lower().strip()) / 10
        await reply(interaction, th.embed("⭐ Rating", _("fun.rate_result", thing=thing, score=f"{score:.1f}")), ephemeral=False)


class Info(commands.Cog):
    """Info-Commands gehören zum Kern und sind immer verfügbar."""
    module = "core"
    help_category = "community"

    def __init__(self, bot: commands.Bot):
        self.bot = bot

    @app_commands.command(name="avatar", description="Zeigt den Avatar eines Users")
    async def avatar(self, interaction: discord.Interaction, user: discord.User | None = None):
        await self._avatar(interaction, user or interaction.user)

    async def _avatar(self, interaction: discord.Interaction, user: discord.abc.User):
        th = await theme(interaction.guild)
        e = th.embed(f"🖼️ {user.display_name}", user=None)
        e.set_image(url=user.display_avatar.with_size(1024).url)
        view = discord.ui.View()
        view.add_item(discord.ui.Button(label="PNG", url=user.display_avatar.with_format("png").with_size(1024).url))
        await reply(interaction, e, ephemeral=False, view=view)

    @app_commands.command(name="banner", description="Zeigt das Profilbanner eines Users")
    async def banner(self, interaction: discord.Interaction, user: discord.User | None = None):
        _ = await i18n.for_guild(interaction.guild_id)
        th = await theme(interaction.guild)
        target = await self.bot.fetch_user((user or interaction.user).id)
        if not target.banner:
            await reply(interaction, th.warning(_("fun.no_banner_title"), _("fun.no_banner", user=target.mention)))
            return
        e = th.embed(f"🎨 {target.display_name}")
        e.set_image(url=target.banner.with_size(1024).url)
        await reply(interaction, e, ephemeral=False)

    @app_commands.command(name="userinfo", description="Informationen über einen User")
    @app_commands.guild_only()
    async def userinfo(self, interaction: discord.Interaction, user: discord.Member | None = None):
        await self._userinfo(interaction, user or interaction.user)  # type: ignore[arg-type]

    async def _userinfo(self, interaction: discord.Interaction, member: discord.Member):
        _ = await i18n.for_guild(interaction.guild_id)
        th = await theme(interaction.guild)
        e = th.embed(f"👤 {member.display_name}", user=member)
        e.set_thumbnail(url=member.display_avatar.url)
        e.add_field(name=_("fun.ui_user"), value=f"{member.mention}\n`{member.id}`", inline=True)
        e.add_field(name=_("fun.ui_created"), value=ts(member.created_at, "D") + "\n" + ts(member.created_at, "R"), inline=True)
        if member.joined_at:
            e.add_field(name=_("fun.ui_joined"), value=ts(member.joined_at, "D") + "\n" + ts(member.joined_at, "R"), inline=True)
        roles = [r.mention for r in reversed(member.roles) if not r.is_default()]
        e.add_field(name=_("fun.ui_roles", count=len(roles)), value=" ".join(roles[:20]) or "—", inline=False)
        flags = []
        if member.premium_since:
            flags.append("💜 Booster")
        if member.bot:
            flags.append("🤖 Bot")
        if member.id == member.guild.owner_id:
            flags.append("👑 Owner")
        if member.is_timed_out():
            flags.append("⏳ Timeout")
        if flags:
            e.add_field(name=_("fun.ui_flags"), value=" · ".join(flags), inline=False)
        await reply(interaction, e, ephemeral=False)

    @app_commands.command(name="serverinfo", description="Informationen über diesen Server")
    @app_commands.guild_only()
    async def serverinfo(self, interaction: discord.Interaction):
        _ = await i18n.for_guild(interaction.guild_id)
        g = interaction.guild
        th = await theme(g)
        e = th.embed(f"🏠 {g.name}", g.description)
        if g.icon:
            e.set_thumbnail(url=g.icon.url)
        if g.banner:
            e.set_image(url=g.banner.with_size(1024).url)
        bots = sum(1 for m in g.members if m.bot)
        e.add_field(name=_("fun.si_owner"), value=f"<@{g.owner_id}>", inline=True)
        e.add_field(name=_("fun.si_created"), value=ts(g.created_at, "D"), inline=True)
        e.add_field(name=_("fun.si_members"), value=f"{fmt_num(g.member_count or 0)} ({bots} Bots)", inline=True)
        e.add_field(name=_("fun.si_channels"), value=f"💬 {len(g.text_channels)} · 🔊 {len(g.voice_channels)} · 📁 {len(g.categories)}", inline=True)
        e.add_field(name=_("fun.si_roles"), value=str(len(g.roles) - 1), inline=True)
        e.add_field(name=_("fun.si_boosts"), value=f"Level {g.premium_tier} · {g.premium_subscription_count}", inline=True)
        e.add_field(name=_("fun.si_emojis"), value=f"{len(g.emojis)} Emojis · {len(g.stickers)} Sticker", inline=True)
        e.set_footer(text=f"ID {g.id}")
        await reply(interaction, e, ephemeral=False)

    @app_commands.command(name="ping", description="Latenz und Status des Bots")
    async def ping(self, interaction: discord.Interaction):
        _ = await i18n.for_guild(interaction.guild_id)
        th = await theme(interaction.guild)
        start = time.perf_counter()
        await interaction.response.defer(ephemeral=True)
        rtt = (time.perf_counter() - start) * 1000
        up = int(time.time() - STARTED_AT)
        d, rem = divmod(up, 86400)
        h, rem = divmod(rem, 3600)
        m = rem // 60
        e = th.embed("🏓 Pong!")
        e.add_field(name="Gateway", value=f"`{self.bot.latency * 1000:.0f} ms`", inline=True)
        e.add_field(name="API", value=f"`{rtt:.0f} ms`", inline=True)
        e.add_field(name="Uptime", value=f"`{d}d {h}h {m}m`", inline=True)
        await interaction.followup.send(embed=e, ephemeral=True)


async def setup(bot: commands.Bot):
    await bot.add_cog(Fun(bot))
    cog = Info(bot)
    await bot.add_cog(cog)

    @app_commands.context_menu(name="Avatar anzeigen")
    async def ctx_avatar(interaction: discord.Interaction, user: discord.User):
        await cog._avatar(interaction, user)

    @app_commands.context_menu(name="Benutzerinfo")
    @app_commands.guild_only()
    async def ctx_userinfo(interaction: discord.Interaction, user: discord.Member):
        await cog._userinfo(interaction, user)

    bot.tree.add_command(ctx_avatar)
    bot.tree.add_command(ctx_userinfo)
