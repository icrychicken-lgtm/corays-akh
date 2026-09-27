"""Verifizierung (Button/Captcha, Account-Age-Check) und Anti-Raid (Join-Wellen-Erkennung mit Lockdown)."""
from __future__ import annotations

import asyncio
import io
import logging
import random
import re
import string
import time
from collections import defaultdict, deque
from datetime import timedelta

import discord
from discord import app_commands
from discord.ext import commands
from PIL import Image, ImageDraw, ImageFilter, ImageFont

from app.bot.ui import handle_exception, reply
from app.core.embeds import theme
from app.core.errors import UserError
from app.core.events import bus
from app.core.guild_config import config
from app.core.i18n import i18n
from app.core.records import log_event
from app.db.base import session_scope, utcnow
from app.db.models import RaidEvent

log = logging.getLogger("nova.verify")
CAPTCHA_CHARS = "".join(c for c in string.ascii_uppercase + string.digits if c not in "O0I1L")


def make_captcha(code: str) -> io.BytesIO:
    img = Image.new("RGB", (320, 110), (17, 17, 27))
    draw = ImageDraw.Draw(img)
    for _i in range(8):
        draw.line([(random.randint(0, 320), random.randint(0, 110)) for _j in range(2)],
                  fill=(random.randint(60, 140), random.randint(60, 140), random.randint(120, 220)), width=2)
    try:
        font = ImageFont.load_default(size=52)
    except TypeError:  # sehr alte Pillow-Version
        font = ImageFont.load_default()
    x = 22
    for ch in code:
        layer = Image.new("RGBA", (60, 80), (0, 0, 0, 0))
        ImageDraw.Draw(layer).text((8, 4), ch, font=font, fill=(random.randint(170, 255), random.randint(140, 255), 255, 255))
        layer = layer.rotate(random.randint(-25, 25), resample=Image.BICUBIC, expand=False)
        img.paste(layer, (x, random.randint(8, 26)), layer)
        x += 46
    for _i in range(500):
        draw.point((random.randint(0, 319), random.randint(0, 109)), fill=(random.randint(80, 200),) * 3)
    img = img.filter(ImageFilter.SMOOTH)
    buf = io.BytesIO()
    img.save(buf, "PNG")
    buf.seek(0)
    return buf


class VerifyButton(discord.ui.DynamicItem[discord.ui.Button], template=r"nova:verify"):
    def __init__(self, label: str = "Verifizieren"):
        super().__init__(discord.ui.Button(label=label, style=discord.ButtonStyle.success, emoji="✅", custom_id="nova:verify"))

    @classmethod
    async def from_custom_id(cls, interaction: discord.Interaction, item: discord.ui.Button, match: re.Match[str], /):
        return cls()

    async def callback(self, interaction: discord.Interaction):
        cog: Verification = interaction.client.get_cog("Verification")  # type: ignore[assignment]
        try:
            await cog.start(interaction)
        except Exception as exc:  # noqa: BLE001
            await handle_exception(interaction, exc, "verify")


class CaptchaButton(discord.ui.DynamicItem[discord.ui.Button], template=r"nova:captcha"):
    def __init__(self, label: str = "Code eingeben"):
        super().__init__(discord.ui.Button(label=label, style=discord.ButtonStyle.primary, emoji="⌨️", custom_id="nova:captcha"))

    @classmethod
    async def from_custom_id(cls, interaction: discord.Interaction, item: discord.ui.Button, match: re.Match[str], /):
        return cls()

    async def callback(self, interaction: discord.Interaction):
        _ = await i18n.for_guild(interaction.guild_id)
        await interaction.response.send_modal(CaptchaModal(_("verify.modal_title"), _("verify.modal_label")))


class CaptchaModal(discord.ui.Modal):
    def __init__(self, title: str, label: str):
        super().__init__(title=title[:45])
        self.code = discord.ui.TextInput(label=label[:45], min_length=4, max_length=8)
        self.add_item(self.code)

    async def on_submit(self, interaction: discord.Interaction):
        cog: Verification = interaction.client.get_cog("Verification")  # type: ignore[assignment]
        await cog.check_captcha(interaction, self.code.value)

    async def on_error(self, interaction: discord.Interaction, error: Exception) -> None:
        await handle_exception(interaction, error, "captcha")


@app_commands.guild_only()
class Verification(commands.Cog):
    module = "verification"
    help_category = "admin"

    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.pending: dict[tuple[int, int], tuple[str, float, int]] = {}  # (guild,user) -> (code, expires, tries)

    async def send_panel(self, guild: discord.Guild) -> discord.Message:
        cfg = await config.get(guild.id, "verification")
        channel = guild.get_channel(cfg.id("channel") or 0)
        if not isinstance(channel, discord.TextChannel):
            raise UserError("verify.no_channel")
        _ = await i18n.for_guild(guild.id)
        th = await theme(guild)
        e = th.embed(cfg.get("panel_title"), cfg.get("panel_text"), icon=False)
        if guild.icon:
            e.set_thumbnail(url=guild.icon.url)
        view = discord.ui.View(timeout=None)
        view.add_item(VerifyButton(_("verify.button")))
        return await channel.send(embed=e, view=view)

    @app_commands.command(name="verification-panel", description="Sendet das Verifizierungs-Panel")
    @app_commands.default_permissions(manage_guild=True)
    async def panel(self, interaction: discord.Interaction):
        await self.send_panel(interaction.guild)
        _ = await i18n.for_guild(interaction.guild_id)
        th = await theme(interaction.guild)
        await reply(interaction, th.success(_("common.success"), _("verify.panel_sent")))

    async def start(self, interaction: discord.Interaction) -> None:
        guild, member = interaction.guild, interaction.user
        if not await config.enabled(guild.id, "verification"):
            raise UserError("errors.module_disabled")
        cfg = await config.get(guild.id, "verification")
        role = guild.get_role(cfg.id("verified_role") or 0)
        if role is None:
            raise UserError("verify.no_role")
        if role in member.roles:
            raise UserError("verify.already")
        min_days = int(cfg.get("min_account_days") or 0)
        if min_days and utcnow() - member.created_at < timedelta(days=min_days):
            await log_event(guild.id, "security", "verify_too_young", user=member)
            if cfg.get("kick_young"):
                try:
                    await member.send(embed=(await theme(guild)).error(guild.name, (await i18n.for_guild(guild.id))("verify.too_young", days=min_days)))
                except discord.HTTPException:
                    pass
                await member.kick(reason=f"Account jünger als {min_days} Tage")
                return
            raise UserError("verify.too_young", days=min_days)
        if cfg.get("mode") == "captcha":
            code = "".join(random.choice(CAPTCHA_CHARS) for _i in range(6))
            self.pending[(guild.id, member.id)] = (code, time.monotonic() + 300, 0)
            _ = await i18n.for_guild(guild.id)
            th = await theme(guild)
            e = th.info(_("verify.captcha_title"), _("verify.captcha_desc"))
            e.set_image(url="attachment://captcha.png")
            view = discord.ui.View(timeout=300)
            view.add_item(CaptchaButton(_("verify.enter_code")))
            await interaction.response.send_message(embed=e, file=discord.File(make_captcha(code), "captcha.png"), view=view, ephemeral=True)
            return
        await self.complete(interaction)

    async def check_captcha(self, interaction: discord.Interaction, value: str) -> None:
        key = (interaction.guild_id, interaction.user.id)
        entry = self.pending.get(key)
        if not entry or entry[1] < time.monotonic():
            self.pending.pop(key, None)
            raise UserError("verify.captcha_expired")
        code, exp, tries = entry
        if value.strip().upper() != code:
            tries += 1
            if tries >= 3:
                self.pending.pop(key, None)
                raise UserError("verify.captcha_failed")
            self.pending[key] = (code, exp, tries)
            raise UserError("verify.captcha_wrong", left=3 - tries)
        self.pending.pop(key, None)
        await self.complete(interaction)

    async def complete(self, interaction: discord.Interaction) -> None:
        guild, member = interaction.guild, interaction.user
        cfg = await config.get(guild.id, "verification")
        role = guild.get_role(cfg.id("verified_role") or 0)
        unverified = guild.get_role(cfg.id("unverified_role") or 0)
        if role is None or role >= guild.me.top_role:
            raise UserError("verify.no_role")
        await member.add_roles(role, reason="Verifiziert")
        if unverified and unverified in member.roles:
            await member.remove_roles(unverified, reason="Verifiziert")
        await log_event(guild.id, "security", "verified", user=member)
        _ = await i18n.for_guild(guild.id)
        th = await theme(guild)
        await reply(interaction, th.success(_("verify.done_title"), _("verify.done", server=guild.name)))

    @commands.Cog.listener()
    async def on_member_join(self, member: discord.Member):
        if member.bot or not await config.enabled(member.guild.id, "verification"):
            return
        cfg = await config.get(member.guild.id, "verification")
        role = member.guild.get_role(cfg.id("unverified_role") or 0)
        if role and role < member.guild.me.top_role:
            try:
                await member.add_roles(role, reason="Unverifiziert")
            except discord.HTTPException:
                pass


class AntiRaid(commands.Cog):
    module = "antiraid"
    help_category = "admin"

    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.joins: dict[int, deque[tuple[float, int]]] = defaultdict(lambda: deque(maxlen=1000))
        self.lockdowns: dict[int, dict] = {}

    def in_lockdown(self, guild_id: int) -> bool:
        return guild_id in self.lockdowns

    @commands.Cog.listener()
    async def on_member_join(self, member: discord.Member):
        guild = member.guild
        if not await config.enabled(guild.id, "antiraid"):
            return
        cfg = await config.get(guild.id, "antiraid")
        now = time.monotonic()
        dq = self.joins[guild.id]
        dq.append((now, member.id))
        if guild.id in self.lockdowns:
            self.lockdowns[guild.id]["users"].append(member.id)
            await self._punish_new(guild, [member.id], cfg)
            return
        window = cfg.get("join_seconds", 20)
        recent = [uid for t, uid in dq if now - t <= window]
        if len(recent) >= cfg.get("join_threshold", 30):
            await self.trigger(guild, recent)

    async def _punish_new(self, guild: discord.Guild, user_ids: list[int], cfg) -> None:
        actions = cfg.get("actions") or []
        for uid in user_ids:
            m = guild.get_member(uid)
            if m is None or m.bot:
                continue
            try:
                if "kick_new" in actions:
                    await m.kick(reason="Anti-Raid")
                elif "timeout_new" in actions:
                    await m.timeout(timedelta(minutes=cfg.get("lockdown_minutes", 15)), reason="Anti-Raid")
            except discord.HTTPException:
                pass

    async def trigger(self, guild: discord.Guild, user_ids: list[int]) -> None:
        cfg = await config.get(guild.id, "antiraid")
        actions = cfg.get("actions") or []
        state: dict = {"users": list(user_ids), "actions": [], "prev_verification": None, "locked": []}
        self.lockdowns[guild.id] = state
        async with session_scope() as s:
            ev = RaidEvent(guild_id=guild.id, join_count=len(user_ids), actions=list(actions), user_ids=[str(u) for u in user_ids])
            s.add(ev)
            await s.flush()
            state["event_id"] = ev.id
        log.warning("RAID erkannt auf %s: %d Joins", guild.id, len(user_ids))
        reason = "Anti-Raid Lockdown"
        if "verification" in actions and guild.me.guild_permissions.manage_guild:
            try:
                state["prev_verification"] = guild.verification_level
                await guild.edit(verification_level=discord.VerificationLevel.high, reason=reason)
                state["actions"].append("verification")
            except discord.HTTPException:
                pass
        if "pause_invites" in actions and guild.me.guild_permissions.manage_guild:
            try:
                await guild.edit(invites_disabled=True, reason=reason)
                state["actions"].append("pause_invites")
            except discord.HTTPException:
                pass
        if "lock" in actions:
            mod_cog = self.bot.get_cog("Moderation")
            for cid in cfg.ids("lock_channels"):
                ch = guild.get_channel(cid)
                if isinstance(ch, discord.TextChannel) and mod_cog:
                    try:
                        await mod_cog.set_lock(ch, True, reason)  # type: ignore[attr-defined]
                        state["locked"].append(cid)
                    except discord.HTTPException:
                        pass
        await self._punish_new(guild, user_ids, cfg)
        bus.publish(guild.id, "raid", {"active": True, "joins": len(user_ids), "event_id": state.get("event_id")})
        await log_event(guild.id, "security", "raid_detected", content=f"{len(user_ids)} Joins", details={"actions": actions})
        if "alert" in actions:
            await self._alert(guild, cfg, True, len(user_ids))
        asyncio.create_task(self._auto_end(guild.id, cfg.get("lockdown_minutes", 15)))

    async def _auto_end(self, guild_id: int, minutes: int) -> None:
        await asyncio.sleep(minutes * 60)
        guild = self.bot.get_guild(guild_id)
        if guild and guild_id in self.lockdowns:
            await self.end(guild)

    async def end(self, guild: discord.Guild) -> bool:
        state = self.lockdowns.pop(guild.id, None)
        if state is None:
            return False
        reason = "Anti-Raid Lockdown beendet"
        try:
            if "verification" in state["actions"] and state["prev_verification"] is not None:
                await guild.edit(verification_level=state["prev_verification"], reason=reason)
            if "pause_invites" in state["actions"]:
                await guild.edit(invites_disabled=False, reason=reason)
        except discord.HTTPException:
            pass
        mod_cog = self.bot.get_cog("Moderation")
        for cid in state["locked"]:
            ch = guild.get_channel(cid)
            if isinstance(ch, discord.TextChannel) and mod_cog:
                try:
                    await mod_cog.set_lock(ch, False, reason)  # type: ignore[attr-defined]
                except discord.HTTPException:
                    pass
        async with session_scope() as s:
            ev = await s.get(RaidEvent, state.get("event_id"))
            if ev:
                ev.ended_at = utcnow()
                ev.resolved = True
                ev.user_ids = [str(u) for u in state["users"]]
                ev.join_count = len(state["users"])
        bus.publish(guild.id, "raid", {"active": False})
        cfg = await config.get(guild.id, "antiraid")
        if "alert" in (cfg.get("actions") or []):
            await self._alert(guild, cfg, False, len(state["users"]))
        return True

    async def _alert(self, guild: discord.Guild, cfg, started: bool, count: int) -> None:
        channel = guild.get_channel(cfg.id("alert_channel") or 0)
        if not isinstance(channel, discord.TextChannel):
            return
        _ = await i18n.for_guild(guild.id)
        th = await theme(guild)
        if started:
            e = th.error(_("raid.alert_title"), _("raid.alert_desc", count=count, minutes=cfg.get("lockdown_minutes", 15)))
        else:
            e = th.success(_("raid.end_title"), _("raid.end_desc", count=count))
        role_id = cfg.id("alert_role")
        try:
            await channel.send(content=f"<@&{role_id}>" if role_id and started else None, embed=e,
                               allowed_mentions=discord.AllowedMentions(roles=True))
        except discord.HTTPException:
            pass


async def setup(bot: commands.Bot):
    bot.add_dynamic_items(VerifyButton, CaptchaButton)
    await bot.add_cog(Verification(bot))
    await bot.add_cog(AntiRaid(bot))
