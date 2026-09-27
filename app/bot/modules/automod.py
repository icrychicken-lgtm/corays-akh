"""Automod: Spam, Flood, Caps, Duplikate, Invites, Links, Scam, Bad Words, Mentions, Emojis, neue Accounts."""
from __future__ import annotations

import asyncio
import hashlib
import logging
import re
import time
import unicodedata
from collections import defaultdict, deque
from datetime import timedelta
from urllib.parse import urlparse

import discord
from discord.ext import commands

from app.core.embeds import theme
from app.core.guild_config import ModuleConfig, config
from app.core.i18n import i18n
from app.core.metrics import metrics
from app.core.records import log_event
from app.db.base import utcnow
from app.services import moderation as mod

log = logging.getLogger("nova.automod")

URL_RE = re.compile(r"(?:https?://|www\.)[^\s<>\"']+", re.I)
BARE_DOMAIN_RE = re.compile(r"\b((?:[a-z0-9-]+\.)+(?:com|net|org|gg|ru|xyz|io|me|co|info|link|site|online|shop|app|ly|tk|ml|ga|cf|gq|click|top|gift|store))(/[^\s]*)?", re.I)
INVITE_RE = re.compile(r"(?:https?://)?(?:www\.)?(?:discord(?:app)?\.com/invite|discord\.gg|dsc\.gg|discord\.me|invite\.gg)/([\w-]{2,32})", re.I)
CUSTOM_EMOJI_RE = re.compile(r"<a?:\w{2,32}:\d{15,21}>")
REPEAT_CHARS_RE = re.compile(r"(.)\1{%d,}")

OFFICIAL = {"discord.com", "discord.gg", "discordapp.com", "discord.media", "discordapp.net", "discord.new", "discord.gift",
            "discordstatus.com", "steamcommunity.com", "steampowered.com", "store.steampowered.com", "twitch.tv", "youtube.com"}
SHORTENERS = {"bit.ly", "tinyurl.com", "cutt.ly", "shorturl.at", "rb.gy", "is.gd", "t.ly", "grabify.link", "iplogger.org",
              "iplogger.com", "2no.co", "yip.su", "bit.do", "blasze.tk", "ps3cfw.com", "linktr.ee.co"}
SCAM_KEYWORDS = ("free nitro", "nitro for free", "gratis nitro", "nitro gratis", "steam gift", "free discord nitro",
                 "claim your nitro", "airdrop", "free skins", "csgo skins giveaway", "you have been gifted",
                 "i'm leaving cs", "who wants my inventory", "@everyone free")
LOOKALIKE_RE = re.compile(r"(d[il1][s5]c[o0]r[dcl]|d[il1]sc[o0]rcl|dis[ck]ord|disc[o0]rd-?(nitro|gift|app)|steam[ck]omm?un?ity|stea[mr]n?c[o0]mm?unity|str?eamcommunity)", re.I)


def _norm(text: str) -> str:
    text = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode().lower()
    return text.translate(str.maketrans({"0": "o", "1": "i", "3": "e", "4": "a", "5": "s", "7": "t", "@": "a", "$": "s"}))


def _domains(content: str) -> list[str]:
    out = []
    for m in URL_RE.finditer(content):
        raw = m.group(0)
        host = urlparse(raw if raw.lower().startswith("http") else "http://" + raw).hostname or ""
        out.append(host.lower().removeprefix("www."))
    for m in BARE_DOMAIN_RE.finditer(content):
        out.append(m.group(1).lower().removeprefix("www."))
    return list(dict.fromkeys(d for d in out if d))


def _domain_match(domain: str, allowed: list[str]) -> bool:
    return any(domain == a or domain.endswith("." + a) for a in (x.lower().strip() for x in allowed) if a)


def _count_emojis(content: str) -> int:
    uni = sum(1 for ch in content if unicodedata.category(ch) == "So" or 0x1F000 <= ord(ch) <= 0x1FAFF)
    return uni + len(CUSTOM_EMOJI_RE.findall(content))


class AutoMod(commands.Cog):
    module = "automod"
    help_category = "moderation"

    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.msg_times: dict[tuple[int, int], deque[float]] = defaultdict(lambda: deque(maxlen=60))
        self.msg_hashes: dict[tuple[int, int], deque[tuple[str, float]]] = defaultdict(lambda: deque(maxlen=30))
        self.invite_cache: dict[str, int | None] = {}
        self.badword_cache: dict[int, tuple[tuple[str, ...], list[re.Pattern]]] = {}
        self.cooldown: dict[tuple[int, int], float] = {}

    # ── Hilfen ──
    async def _exempt(self, message: discord.Message, cfg: ModuleConfig) -> bool:
        member = message.author
        if not isinstance(member, discord.Member):
            return True
        if member.guild_permissions.manage_guild or member.guild_permissions.administrator:
            return True
        if str(member.id) in (cfg.get("exempt_users") or []):
            return True
        if {r.id for r in member.roles} & set(cfg.ids("exempt_roles")):
            return True
        chan_ids = {message.channel.id, getattr(message.channel, "category_id", None), getattr(message.channel, "parent_id", None)}
        return bool(chan_ids & set(cfg.ids("exempt_channels")))

    def _badword_patterns(self, guild_id: int, words: list[str]) -> list[re.Pattern]:
        key = tuple(words)
        hit = self.badword_cache.get(guild_id)
        if hit and hit[0] == key:
            return hit[1]
        pats = []
        for w in words:
            w = _norm(w.strip())
            if not w:
                continue
            body = ".*?".join(re.escape(p) for p in w.split("*"))
            prefix = "" if w.startswith("*") else r"\b"
            suffix = "" if w.endswith("*") else r"\b"
            pats.append(re.compile(prefix + body + suffix, re.I))
        self.badword_cache[guild_id] = (key, pats)
        return pats

    async def _is_own_invite(self, guild: discord.Guild, code: str) -> bool:
        if code in self.invite_cache:
            return self.invite_cache[code] == guild.id
        if guild.vanity_url_code and code.lower() == guild.vanity_url_code.lower():
            return True
        try:
            inv = await self.bot.fetch_invite(code, with_counts=False)
            gid = inv.guild.id if inv.guild else None
        except discord.HTTPException:
            gid = None
        self.invite_cache[code] = gid
        if len(self.invite_cache) > 5000:
            self.invite_cache.clear()
        return gid == guild.id

    # ── Filter ──
    async def check(self, message: discord.Message, cfg: ModuleConfig, *, edited: bool = False) -> tuple[str, str] | None:
        content = message.content or ""
        low = content.lower()
        norm = _norm(content)
        key = (message.guild.id, message.author.id)
        now = time.monotonic()
        domains = _domains(content)

        if cfg.get("scam_enabled"):
            for d in domains:
                if d in OFFICIAL or _domain_match(d, cfg.get("allowed_domains") or []):
                    continue
                if LOOKALIKE_RE.search(d) or d.startswith("xn--") or ".xn--" in d or d in SHORTENERS or re.fullmatch(r"[\d.]+", d):
                    return "scam", d
            if domains and any(k in norm for k in SCAM_KEYWORDS):
                return "scam", "keyword"

        invites = INVITE_RE.findall(content)
        if cfg.get("invites_enabled") and invites:
            for code in invites:
                if cfg.get("invites_allow_own") and await self._is_own_invite(message.guild, code):
                    continue
                return "invites", code

        if cfg.get("links_enabled") and domains:
            blocked = cfg.get("links_blocked_domains") or []
            allowed = cfg.get("allowed_domains") or []
            for d in domains:
                if _domain_match(d, blocked):
                    return "links", d
                if cfg.get("links_block_all") and not _domain_match(d, allowed) and not INVITE_RE.search(d):
                    return "links", d

        if cfg.get("badwords_enabled") and cfg.get("badwords_list"):
            for p in self._badword_patterns(message.guild.id, cfg["badwords_list"]):
                if p.search(norm):
                    return "badwords", p.pattern[:40]

        if cfg.get("mass_mentions_enabled") and ("@everyone" in content or "@here" in content):
            if not message.channel.permissions_for(message.author).mention_everyone:
                return "mass_mentions", "@everyone/@here"

        mention_count = len(set(message.raw_mentions)) + len(set(message.raw_role_mentions))
        if cfg.get("mentions_enabled") and mention_count > cfg.get("mentions_max", 6):
            return "mentions", str(mention_count)

        if cfg.get("emoji_enabled") and _count_emojis(content) > cfg.get("emoji_max", 12):
            return "emoji", str(_count_emojis(content))

        letters = [c for c in content if c.isalpha()]
        if cfg.get("caps_enabled") and len(letters) >= cfg.get("caps_min_length", 12):
            ratio = sum(1 for c in letters if c.isupper()) * 100 / len(letters)
            if ratio > cfg.get("caps_percent", 75):
                return "caps", f"{ratio:.0f}%"

        if cfg.get("flood_enabled"):
            if content.count("\n") + 1 > cfg.get("flood_lines", 15):
                return "flood", "lines"
            n = max(4, int(cfg.get("flood_chars", 20)) - 1)
            if re.search(r"(.)\1{%d,}" % n, content):
                return "flood", "chars"

        if cfg.get("new_accounts_enabled") and (domains or invites):
            age = utcnow() - message.author.created_at
            if age < timedelta(days=cfg.get("new_accounts_days", 3)) and cfg.get("new_accounts_block_links", True):
                return "new_accounts", f"{age.days}d"

        if not edited:
            if cfg.get("duplicates_enabled") and len(content) >= 3:
                h = hashlib.md5(low.strip().encode()).hexdigest()
                window = cfg.get("duplicates_seconds", 30)
                dq = self.msg_hashes[key]
                dq.append((h, now))
                same = sum(1 for hh, t in dq if hh == h and now - t <= window)
                if same >= cfg.get("duplicates_count", 3):
                    return "duplicates", str(same)
            if cfg.get("spam_enabled"):
                dq2 = self.msg_times[key]
                dq2.append(now)
                window = cfg.get("spam_seconds", 5)
                recent = sum(1 for t in dq2 if now - t <= window)
                if recent > cfg.get("spam_messages", 6):
                    return "spam", str(recent)
        return None

    # ── Aktionen ──
    async def punish(self, message: discord.Message, cfg: ModuleConfig, rule: str, detail: str) -> None:
        guild = message.guild
        member = message.author
        actions = cfg.get(f"{rule}_actions") or ["delete", "log"]
        _ = await i18n.for_guild(guild.id)
        reason = _("automod.reason", rule=_("automod.rule." + rule))
        metrics.incr(guild.id, "automod")

        if "delete" in actions:
            try:
                await message.delete()
            except discord.HTTPException:
                pass
            if rule in ("spam", "duplicates"):
                await self._cleanup_recent(message)

        # Strafaktionen max. 1× pro 10 s pro User (verhindert Doppelstrafen bei Spam-Wellen)
        ck = (guild.id, member.id)
        if self.cooldown.get(ck, 0) > time.monotonic():
            return
        self.cooldown[ck] = time.monotonic() + 10

        for action in ("warn", "timeout", "kick", "ban"):
            if action in actions:
                try:
                    await mod.execute(guild, action, member, guild.me, reason,
                                      duration=cfg.get(f"{rule}_timeout", 10) * 60 if action == "timeout" else None, source="automod")
                except mod.ModerationError as exc:
                    log.debug("Automod-Aktion %s nicht möglich: %s", action, exc.key)
                break_after = action in ("kick", "ban")
                if break_after:
                    break
        if "remove_role" in actions and cfg.id("remove_role"):
            role = guild.get_role(cfg.id("remove_role"))
            if role and role in member.roles and role < guild.me.top_role:
                try:
                    await member.remove_roles(role, reason=reason)
                except discord.HTTPException:
                    pass
        if "lock" in actions and isinstance(message.channel, discord.TextChannel):
            asyncio.create_task(self._temp_lock(message.channel, cfg.get("lock_minutes", 5), reason))

        await log_event(guild.id, "automod", rule, user=member, channel_id=message.channel.id,
                        content=(message.content or "")[:1500], details={"detail": detail, "actions": actions})
        if "log" in actions or len(actions) > 1:
            th = await theme(guild)
            e = th.warning(_("automod.log_title", rule=_("automod.rule." + rule)), (message.content or "*—*")[:1500], user=member)
            e.add_field(name=_("automod.f_user"), value=f"{member.mention} `{member.id}`", inline=True)
            e.add_field(name=_("automod.f_channel"), value=message.channel.mention, inline=True)
            e.add_field(name=_("automod.f_actions"), value=", ".join(actions), inline=True)
            e.add_field(name=_("automod.f_detail"), value=f"`{detail[:100]}`", inline=True)
            await mod.send_modlog(guild, e)

        if "delete" in actions and rule not in ("spam", "duplicates"):
            try:
                th = await theme(guild)
                await message.channel.send(content=member.mention, embed=th.warning(_("automod.notice_title"), _("automod.notice", rule=_("automod.rule." + rule))),
                                           delete_after=6, allowed_mentions=discord.AllowedMentions(users=True))
            except discord.HTTPException:
                pass

    async def _cleanup_recent(self, message: discord.Message) -> None:
        try:
            cutoff = utcnow() - timedelta(seconds=30)
            await message.channel.purge(limit=30, after=cutoff, check=lambda m: m.author.id == message.author.id, bulk=True)
        except discord.HTTPException:
            pass

    async def _temp_lock(self, channel: discord.TextChannel, minutes: int, reason: str) -> None:
        cog = self.bot.get_cog("Moderation")
        if cog is None:
            return
        try:
            await cog.set_lock(channel, True, reason)  # type: ignore[attr-defined]
            await asyncio.sleep(minutes * 60)
            await cog.set_lock(channel, False, "Automod: Sperre abgelaufen")  # type: ignore[attr-defined]
        except discord.HTTPException as exc:
            log.warning("Temp-Lock fehlgeschlagen: %s", exc)

    # ── Events ──
    @commands.Cog.listener()
    async def on_message(self, message: discord.Message):
        if message.author.bot or message.guild is None or not message.content:
            return
        if not await config.enabled(message.guild.id, "automod"):
            return
        cfg = await config.get(message.guild.id, "automod")
        if await self._exempt(message, cfg):
            return
        hit = await self.check(message, cfg)
        if hit:
            await self.punish(message, cfg, *hit)

    @commands.Cog.listener()
    async def on_message_edit(self, before: discord.Message, after: discord.Message):
        if after.author.bot or after.guild is None or before.content == after.content or not after.content:
            return
        if not await config.enabled(after.guild.id, "automod"):
            return
        cfg = await config.get(after.guild.id, "automod")
        if await self._exempt(after, cfg):
            return
        hit = await self.check(after, cfg, edited=True)
        if hit:
            await self.punish(after, cfg, *hit)


async def setup(bot: commands.Bot):
    await bot.add_cog(AutoMod(bot))
