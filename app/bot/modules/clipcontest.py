"""Clip-Contest: Wettbewerb über einen Zeitraum (z. B. 2 Monate). Die Community reicht bereits hochgeladene
TikTok-/YouTube-/Instagram-Clips per Link ein → Clip landet im Prüf-Channel, das Team nimmt an oder lehnt ab.
Aufrufe meldet der Einreicher per Screenshot (/clip aufrufe), gezählt wird erst nach Bestätigung durch das Team.
Preise: Platzierungen (meiste Aufrufe insgesamt) + Meilensteine pro Clip. Am Ende: Ergebnisse + Auszahlungsliste."""
from __future__ import annotations

import logging
import re
from collections import defaultdict
from datetime import timedelta

import discord
from discord import app_commands
from discord.ext import commands, tasks
from sqlalchemy import func, select

from app.bot.ui import Paginator, chunk, handle_exception, reply
from app.core.embeds import fmt_num, theme
from app.core.errors import UserError
from app.core.guild_config import config
from app.core.i18n import fill, i18n
from app.core.records import log_event
from app.core.timeutil import parse_duration, ts
from app.db.base import SessionLocal, session_scope, utcnow
from app.db.models import ClipContest, ClipSubmission
from app.services.notify import is_staff

log = logging.getLogger("nova.clipcontest")

PLATFORMS = {
    "tiktok": ("TikTok", "🎵", re.compile(r"^https?://(?:www\.|vm\.|vt\.|m\.)?tiktok\.com/\S+$", re.I)),
    "youtube": ("YouTube Shorts", "▶️", re.compile(r"^https?://(?:www\.|m\.)?(?:youtube\.com/shorts/|youtu\.be/)[\w-]{6,}\S*$", re.I)),
    "instagram": ("Instagram Reels", "📸", re.compile(r"^https?://(?:www\.)?instagram\.com/(?:reels?|p)/[\w-]+\S*$", re.I)),
}
STATUS = {"pending": ("🟡", "warning"), "approved": ("🟢", "success"), "denied": ("🔴", "error")}
MEDALS = {1: "🥇", 2: "🥈", 3: "🥉"}


def detect_platform(url: str) -> str | None:
    return next((key for key, (_n, _e, rx) in PLATFORMS.items() if rx.match(url)), None)


def normalize_url(url: str) -> str:
    """Tracking-Parameter weg, damit derselbe Clip nicht doppelt eingereicht werden kann."""
    return url.strip().split("?")[0].split("#")[0].rstrip("/")[:300]


def parse_views(text: str) -> int | None:
    """'1500000', '1.500.000', '1,5m', '1.5 mio', '150k' → int."""
    t = (text or "").strip().lower().replace(" ", "")
    m = re.fullmatch(r"(\d+(?:[.,]\d+)*)(k|m|mio)?", t)
    if not m:
        return None
    num, suffix = m.groups()
    if suffix:
        try:
            return int(float(num.replace(",", ".")) * (1_000 if suffix == "k" else 1_000_000))
        except ValueError:
            return None
    return int(re.sub(r"[.,]", "", num))


def money(amount: float, cfg) -> str:
    """3.0 → '3 €', 2.5 → '2,50 €'."""
    text = f"{amount:.0f}" if float(amount).is_integer() else f"{amount:.2f}".replace(".", ",")
    return f"{text} {cfg.get('currency') or '€'}"


def tiers(cfg) -> list[tuple[int, float]]:
    return sorted((int(t["views"]), float(t["amount"])) for t in cfg.get("pay_tiers") or [] if t.get("views") and t.get("amount"))


def clip_pay(views: int, cfg) -> float:
    """Geld für einen Clip: Betrag der höchsten erreichten Aufruf-Stufe."""
    return max((amount for need, amount in tiers(cfg) if views >= need), default=0.0)


def ranking(subs: list[ClipSubmission]) -> list[tuple[int, int, ClipSubmission]]:
    """[(user_id, Aufrufe gesamt, bester Clip)] – nur angenommene Clips, sortiert nach Aufrufen."""
    total: dict[int, int] = defaultdict(int)
    best: dict[int, ClipSubmission] = {}
    for s in subs:
        if s.status != "approved":
            continue
        total[s.user_id] += s.views
        if s.user_id not in best or s.views > best[s.user_id].views:
            best[s.user_id] = s
    return sorted(((u, v, best[u]) for u, v in total.items() if v > 0), key=lambda r: r[1], reverse=True)


def payouts(subs: list[ClipSubmission], cfg, _) -> dict[int, tuple[list[str], float]]:
    """user_id → (Aufschlüsselung je Clip, Summe)."""
    lines: dict[int, list[str]] = defaultdict(list)
    total: dict[int, float] = defaultdict(float)
    for s in sorted(subs, key=lambda s: s.id):
        if s.status == "approved" and (pay := clip_pay(s.views, cfg)):
            lines[s.user_id].append(_("cc.payout_clip", id=s.id, views=fmt_num(s.views), amount=money(pay, cfg), url=s.url))
            total[s.user_id] += pay
    return {uid: (lines[uid], total[uid]) for uid in lines}


def prize_text(cfg, _) -> str:
    rows = [_("cc.prize_tier", views=fmt_num(need), amount=money(amount, cfg)) for need, amount in tiers(cfg)]
    return "\n".join(rows) or "—"


# ───────────────────────── Buttons & Formulare ─────────────────────────
class ContestButton(discord.ui.DynamicItem[discord.ui.Button], template=r"nova:cc:(?P<action>submit|board)"):
    STYLE = {"submit": (discord.ButtonStyle.success, "🎬"), "board": (discord.ButtonStyle.secondary, "🏆")}

    def __init__(self, action: str, label: str | None = None, disabled: bool = False):
        style, emoji = self.STYLE[action]
        super().__init__(discord.ui.Button(label=label, style=style, emoji=emoji, custom_id=f"nova:cc:{action}", disabled=disabled))
        self.action = action

    @classmethod
    async def from_custom_id(cls, interaction, item, match: re.Match[str], /):
        return cls(match["action"])

    async def callback(self, interaction: discord.Interaction):
        cog: ClipContestCog = interaction.client.get_cog("ClipContest")  # type: ignore[assignment]
        try:
            if self.action == "submit":
                _ = await i18n.for_guild(interaction.guild_id)
                await interaction.response.send_modal(SubmitModal(_))
            else:
                await cog.show_board(interaction)
        except Exception as exc:  # noqa: BLE001
            await handle_exception(interaction, exc, f"clipcontest_{self.action}")


class SubmitModal(discord.ui.Modal):
    def __init__(self, _):
        super().__init__(title=_("cc.modal_title")[:45])
        self.link = discord.ui.TextInput(label=_("cc.modal_link")[:45], placeholder="https://www.tiktok.com/@name/video/…", max_length=300)
        self.add_item(self.link)

    async def on_submit(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        await interaction.client.get_cog("ClipContest").submit(interaction, self.link.value)  # type: ignore[union-attr]

    async def on_error(self, interaction: discord.Interaction, error: Exception) -> None:
        await handle_exception(interaction, error, "clip_submit")


class ReviewButton(discord.ui.DynamicItem[discord.ui.Button], template=r"nova:cc:(?P<id>\d+):(?P<action>approve|deny|views|confirm)"):
    STYLE = {"approve": (discord.ButtonStyle.success, "✅"), "deny": (discord.ButtonStyle.danger, "⛔"),
             "views": (discord.ButtonStyle.secondary, "👁️"), "confirm": (discord.ButtonStyle.primary, "☑️")}

    def __init__(self, sid: int, action: str, label: str | None = None, disabled: bool = False):
        style, emoji = self.STYLE[action]
        super().__init__(discord.ui.Button(label=label, style=style, emoji=emoji, custom_id=f"nova:cc:{sid}:{action}", disabled=disabled))
        self.sid, self.action = sid, action

    @classmethod
    async def from_custom_id(cls, interaction, item, match: re.Match[str], /):
        return cls(int(match["id"]), match["action"])

    async def callback(self, interaction: discord.Interaction):
        cog: ClipContestCog = interaction.client.get_cog("ClipContest")  # type: ignore[assignment]
        try:
            await cog.require_staff(interaction)
            if self.action == "views":
                _ = await i18n.for_guild(interaction.guild_id)
                await interaction.response.send_modal(ViewsModal(_, self.sid))
                return
            await interaction.response.defer()
            await cog.review(interaction, self.sid, self.action)
        except Exception as exc:  # noqa: BLE001
            await handle_exception(interaction, exc, f"clip_review_{self.action}")


class ViewsModal(discord.ui.Modal):
    def __init__(self, _, sid: int):
        super().__init__(title=_("cc.views_modal")[:45])
        self.sid = sid
        self.views = discord.ui.TextInput(label=_("cc.views_label")[:45], placeholder="1500000 / 1,5m / 150k", max_length=20)
        self.add_item(self.views)

    async def on_submit(self, interaction: discord.Interaction):
        views = parse_views(self.views.value)
        if views is None:
            raise UserError("cc.bad_views")
        await interaction.response.defer()
        await interaction.client.get_cog("ClipContest").review(interaction, self.sid, "set", views)  # type: ignore[union-attr]

    async def on_error(self, interaction: discord.Interaction, error: Exception) -> None:
        await handle_exception(interaction, error, "clip_views")


# ───────────────────────── Cog ─────────────────────────
@app_commands.guild_only()
class ClipContestCog(commands.Cog, name="ClipContest"):
    module = "clipcontest"
    help_category = "streamer"

    clip = app_commands.Group(name="clip", description="Clip-Contest: Clips einreichen, Aufrufe melden, Rangliste", guild_only=True)
    contest = app_commands.Group(name="clipcontest", description="Clip-Contest verwalten", guild_only=True,
                                 default_permissions=discord.Permissions(manage_events=True))

    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.end_loop.start()

    def cog_unload(self):
        self.end_loop.cancel()

    # ── Helfer ──
    async def require_staff(self, interaction: discord.Interaction) -> None:
        cfg = await config.get(interaction.guild_id, "clipcontest")
        if not await is_staff(interaction.user, cfg.ids("staff_roles")):  # type: ignore[arg-type]
            raise UserError("errors.no_permission")

    async def active(self, guild_id: int) -> ClipContest | None:
        async with SessionLocal() as db:
            return (await db.execute(select(ClipContest).where(ClipContest.guild_id == guild_id, ClipContest.ended.is_(False))
                                     .order_by(ClipContest.id.desc()))).scalars().first()

    async def latest(self, guild_id: int) -> ClipContest:
        async with SessionLocal() as db:
            c = (await db.execute(select(ClipContest).where(ClipContest.guild_id == guild_id)
                                  .order_by(ClipContest.ended, ClipContest.id.desc()))).scalars().first()
        if c is None:
            raise UserError("cc.no_contest")
        return c

    async def review_channel(self, guild: discord.Guild, pick: discord.TextChannel | None = None) -> discord.TextChannel:
        """Prüf-Channel: der beim Start ausgewählte (wird gemerkt) oder der gespeicherte. Der Bot erstellt nie selbst einen."""
        cfg = await config.get(guild.id, "clipcontest")
        if pick is not None:
            await config.save(guild.id, "clipcontest", settings={**cfg, "review_channel": str(pick.id)})
            return pick
        ch = guild.get_channel(cfg.id("review_channel") or 0)
        if not isinstance(ch, discord.TextChannel):
            raise UserError("cc.no_review_channel")
        return ch

    async def submissions(self, contest_id: int) -> list[ClipSubmission]:
        async with SessionLocal() as db:
            return list((await db.execute(select(ClipSubmission).where(ClipSubmission.contest_id == contest_id))).scalars().all())

    # ── Ankündigung ──
    async def announce_embed(self, guild: discord.Guild, c: ClipContest) -> discord.Embed:
        _ = await i18n.for_guild(guild.id)
        cfg = await config.get(guild.id, "clipcontest")
        th = await theme(guild)
        platforms = " · ".join(f"{PLATFORMS[p][1]} {PLATFORMS[p][0]}" for p in cfg.get("platforms") or [] if p in PLATFORMS) or "—"
        e = th.embed(f"🎬 {c.name}", _("cc.announce_intro", streamer=cfg.get("streamer_name") or guild.name, end=ts(c.ends_at, "D")),
                     kind="success" if c.ended else "primary", icon=False)
        e.add_field(name=_("cc.how_title"), value=_("cc.how", platforms=platforms, max=cfg.get("max_per_user", 5)), inline=False)
        e.add_field(name=_("cc.rules_title"), value=fill(cfg.get("rules") or "—", streamer=cfg.get("streamer_name") or guild.name,
                                                         hashtag=cfg.get("hashtag") or "")[:1024], inline=False)
        e.add_field(name=_("cc.prizes_title"), value=(prize_text(cfg, _) + "\n\n" + _("cc.prize_note", max=cfg.get("max_per_user", 5)))[:1024],
                    inline=False)
        e.add_field(name=_("cc.ended") if c.ended else _("cc.ends"), value=f"{ts(c.ends_at, 'R')}\n{ts(c.ends_at, 'f')}", inline=True)
        e.set_footer(text=f"Clip-Contest #{c.id}", icon_url=th.icon_url)
        return e

    async def announce_view(self, guild: discord.Guild, c: ClipContest) -> discord.ui.View:
        _ = await i18n.for_guild(guild.id)
        v = discord.ui.View(timeout=None)
        v.add_item(ContestButton("submit", _("cc.btn_submit"), disabled=c.ended))
        v.add_item(ContestButton("board", _("cc.btn_board")))
        return v

    async def refresh_announcement(self, guild: discord.Guild, c: ClipContest) -> None:
        ch = guild.get_channel(c.channel_id)
        if isinstance(ch, discord.TextChannel) and c.message_id:
            try:
                await ch.get_partial_message(c.message_id).edit(embed=await self.announce_embed(guild, c), view=await self.announce_view(guild, c))
            except discord.HTTPException:
                pass

    # ── Einreichen ──
    async def submit(self, interaction: discord.Interaction, link: str) -> None:
        guild = interaction.guild
        _ = await i18n.for_guild(guild.id)
        if not await config.enabled(guild.id, "clipcontest"):
            raise UserError("errors.module_disabled")
        cfg = await config.get(guild.id, "clipcontest")
        c = await self.active(guild.id)
        if c is None or c.ends_at <= utcnow():
            raise UserError("cc.no_active")
        url = normalize_url(link)
        platform = detect_platform(url)
        allowed = cfg.get("platforms") or []
        if platform is None or platform not in allowed:
            raise UserError("cc.bad_link", platforms=", ".join(PLATFORMS[p][0] for p in allowed if p in PLATFORMS))
        review = await self.review_channel(guild)
        async with session_scope() as db:
            if (await db.execute(select(ClipSubmission.id).where(ClipSubmission.contest_id == c.id, ClipSubmission.url == url))).first():
                raise UserError("cc.duplicate")
            count = (await db.execute(select(func.count()).where(ClipSubmission.contest_id == c.id, ClipSubmission.user_id == interaction.user.id,
                                                                 ClipSubmission.status != "denied"))).scalar_one()
            if count >= int(cfg.get("max_per_user", 5)):
                raise UserError("cc.limit", max=cfg.get("max_per_user", 5))
            s = ClipSubmission(contest_id=c.id, guild_id=guild.id, user_id=interaction.user.id, user_name=str(interaction.user)[:100],
                               url=url, platform=platform, status="pending", views=0, paid=False)
            db.add(s)
            await db.flush()
        msg = await review.send(content=url, embed=await self.review_embed(guild, s), view=await self.review_view(guild, s))
        async with session_scope() as db:
            (await db.get(ClipSubmission, s.id)).message_id = msg.id
        await log_event(guild.id, "clipcontest", "submit", user=interaction.user, content=url, details={"id": s.id, "contest": c.id})
        await reply(interaction, (await theme(guild)).success(_("cc.submitted_title"), _("cc.submitted", id=s.id)))

    # ── Prüfen ──
    async def review_embed(self, guild: discord.Guild, s: ClipSubmission) -> discord.Embed:
        _ = await i18n.for_guild(guild.id)
        th = await theme(guild)
        icon, kind = STATUS.get(s.status, ("•", "primary"))
        name, emoji, _rx = PLATFORMS.get(s.platform, ("Link", "🔗", None))
        e = th.embed(_("cc.review_title", id=s.id), f"{emoji} **{name}** · <@{s.user_id}>\n{s.url}", kind=kind, icon=False)
        e.add_field(name=_("cc.status"), value=f"{icon} **{_('cc.st.' + s.status)}**", inline=True)
        e.add_field(name=_("cc.views_confirmed"), value=f"👁️ **{fmt_num(s.views)}**", inline=True)
        cfg = await config.get(guild.id, "clipcontest")
        e.add_field(name=_("cc.earned"), value=f"💶 **{money(clip_pay(s.views, cfg), cfg)}**", inline=True)
        if s.claimed_views is not None:
            proof = f"\n[{_('cc.proof')}]({s.proof_url})" if s.proof_url else ""
            e.add_field(name=_("cc.views_claimed"), value=f"⏳ **{fmt_num(s.claimed_views)}**{proof}", inline=True)
        if s.reviewer_id:
            e.add_field(name=_("cc.reviewer"), value=f"<@{s.reviewer_id}>", inline=True)
        return e

    async def review_view(self, guild: discord.Guild, s: ClipSubmission) -> discord.ui.View:
        _ = await i18n.for_guild(guild.id)
        v = discord.ui.View(timeout=None)
        v.add_item(ReviewButton(s.id, "approve", _("cc.btn_approve"), disabled=s.status == "approved"))
        v.add_item(ReviewButton(s.id, "deny", _("cc.btn_deny"), disabled=s.status == "denied"))
        v.add_item(ReviewButton(s.id, "views", _("cc.btn_views")))
        if s.claimed_views is not None:
            v.add_item(ReviewButton(s.id, "confirm", _("cc.btn_confirm", views=fmt_num(s.claimed_views))[:80]))
        return v

    async def refresh_review(self, guild: discord.Guild, s: ClipSubmission) -> None:
        cfg = await config.get(guild.id, "clipcontest")
        ch = guild.get_channel(cfg.id("review_channel") or 0)
        if isinstance(ch, discord.TextChannel) and s.message_id:
            try:
                await ch.get_partial_message(s.message_id).edit(embed=await self.review_embed(guild, s), view=await self.review_view(guild, s))
            except discord.HTTPException:
                pass

    async def review(self, interaction: discord.Interaction, sid: int, action: str, views: int | None = None) -> None:
        guild = interaction.guild
        async with session_scope() as db:
            s = await db.get(ClipSubmission, sid)
            if s is None or s.guild_id != guild.id:
                raise UserError("cc.not_found")
            if action == "approve":
                s.status = "approved"
            elif action == "deny":
                s.status = "denied"
            elif action == "confirm":
                if s.claimed_views is None:
                    raise UserError("cc.nothing_to_confirm")
                s.views, s.claimed_views = s.claimed_views, None
                s.status = "approved" if s.status == "pending" else s.status
            elif action == "set" and views is not None:
                s.views, s.claimed_views = views, None
            s.reviewer_id, s.updated_at = interaction.user.id, utcnow()
        await interaction.edit_original_response(embed=await self.review_embed(guild, s), view=await self.review_view(guild, s))
        await log_event(guild.id, "clipcontest", action, user=interaction.user, content=s.url, details={"id": s.id, "views": s.views})
        if action in ("approve", "deny"):
            member = guild.get_member(s.user_id)
            if member:
                _ = await i18n.for_guild(guild.id)
                try:
                    await member.send(embed=(await theme(guild)).embed(_("cc.dm_" + action + "_title"), _("cc.dm_" + action, url=s.url, server=guild.name),
                                                                       kind=STATUS[s.status][1], icon=False))
                except discord.HTTPException:
                    pass

    # ── Aufrufe melden (Einreicher) ──
    async def claim_views(self, interaction: discord.Interaction, sid: int, views: int, proof: discord.Attachment) -> None:
        guild = interaction.guild
        _ = await i18n.for_guild(guild.id)
        if not (proof.content_type or "").startswith("image/"):
            raise UserError("cc.proof_image")
        async with SessionLocal() as db:
            s = await db.get(ClipSubmission, sid)
            c = await db.get(ClipContest, s.contest_id) if s else None
        if s is None or s.guild_id != guild.id or s.user_id != interaction.user.id:
            raise UserError("cc.not_found")
        if s.status == "denied":
            raise UserError("cc.denied_clip")
        if c is None or c.ended:
            raise UserError("cc.contest_over")
        review = await self.review_channel(guild)
        # Screenshot neu hochladen – Discord-Anhangs-Links aus Slash-Commands laufen sonst ab
        content = _("cc.proof_msg", user=interaction.user.mention, id=s.id, views=fmt_num(views))
        try:
            ref = review.get_partial_message(s.message_id) if s.message_id else None
            proof_msg = await review.send(content=content, file=await proof.to_file(), reference=ref,
                                          allowed_mentions=discord.AllowedMentions.none())
        except discord.HTTPException:  # Prüf-Nachricht gelöscht → ohne Antwort-Verweis posten
            proof_msg = await review.send(content=content, file=await proof.to_file(), allowed_mentions=discord.AllowedMentions.none())
        async with session_scope() as db:
            row = await db.get(ClipSubmission, sid)
            row.claimed_views, row.proof_url, row.updated_at = views, proof_msg.jump_url, utcnow()
            s = row
        await self.refresh_review(guild, s)
        await reply(interaction, (await theme(guild)).success(_("cc.claimed_title"), _("cc.claimed", views=fmt_num(views))))

    # ── Rangliste ──
    async def show_board(self, interaction: discord.Interaction) -> None:
        guild = interaction.guild
        _ = await i18n.for_guild(guild.id)
        th = await theme(guild)
        c = await self.latest(guild.id)
        cfg = await config.get(guild.id, "clipcontest")
        subs = await self.submissions(c.id)
        rows = ranking(subs)
        if not rows:
            await reply(interaction, th.info(_("cc.board_title", name=c.name), _("cc.board_empty")))
            return
        earned = {uid: total for uid, (_l, total) in payouts(subs, cfg, _).items()}
        mine = next((i for i, r in enumerate(rows, start=1) if r[0] == interaction.user.id), None)
        pages = []
        for n, group in enumerate(chunk(rows, 10)):
            lines = [f"{MEDALS.get(i, f'`#{i}`')} <@{uid}> — 👁️ **{fmt_num(views)}** · 💶 {money(earned.get(uid, 0), cfg)}"
                     f" · [{_('cc.best_clip')}]({best.url})" for i, (uid, views, best) in enumerate(group, start=n * 10 + 1)]
            e = th.embed(_("cc.board_title", name=c.name), "\n".join(lines), icon=False)
            e.set_footer(text=_("cc.board_footer", place=mine or "—", end=c.ends_at.strftime("%d.%m.%Y")))
            pages.append(e)
        await Paginator(interaction.user.id, pages).send(interaction, ephemeral=True)

    # ── Ende & Auszahlung ──
    async def end(self, c: ClipContest) -> None:
        guild = self.bot.get_guild(c.guild_id)
        async with session_scope() as db:
            row = await db.get(ClipContest, c.id)
            row.ended = True
            if row.ends_at > utcnow():
                row.ends_at = utcnow()
            c = row
        if guild is None:
            return
        _ = await i18n.for_guild(guild.id)
        cfg = await config.get(guild.id, "clipcontest")
        th = await theme(guild)
        subs = await self.submissions(c.id)
        rows = ranking(subs)
        await self.refresh_announcement(guild, c)
        pay = payouts(subs, cfg, _)
        top = [f"{MEDALS.get(i, f'`#{i}`')} <@{uid}> — 👁️ **{fmt_num(views)}** · 💶 **{money(pay[uid][1] if uid in pay else 0, cfg)}**"
               for i, (uid, views, _b) in enumerate(rows[:10], start=1)]
        e = th.success(_("cc.results_title", name=c.name), "\n".join(top) or _("cc.board_empty"))
        e.add_field(name=_("cc.stats_title"), value=_("cc.stats", clips=sum(1 for s in subs if s.status == "approved"), people=len(rows),
                                                      views=fmt_num(sum(r[1] for r in rows)), paid=money(sum(t for _l, t in pay.values()), cfg)), inline=False)
        e.set_footer(text=_("cc.results_footer"), icon_url=th.icon_url)
        winners = set(pay)
        ch = guild.get_channel(c.channel_id)
        if isinstance(ch, discord.TextChannel):
            role_id = cfg.id("ping_role")
            mentions = " ".join(f"<@{u}>" for u in winners)
            try:
                await ch.send(content=(f"<@&{role_id}> " if role_id else "") + mentions or None, embed=e,
                              allowed_mentions=discord.AllowedMentions(users=True, roles=True))
            except discord.HTTPException:
                pass
        await log_event(guild.id, "clipcontest", "end", content=c.name, details={"id": c.id, "winners": [str(u) for u in winners]})

    @tasks.loop(seconds=60)
    async def end_loop(self):
        async with SessionLocal() as db:
            due = (await db.execute(select(ClipContest).where(ClipContest.ended.is_(False), ClipContest.ends_at <= utcnow()).limit(10))).scalars().all()
        for c in due:
            try:
                await self.end(c)
            except Exception:
                log.exception("Clip-Contest %s konnte nicht beendet werden", c.id)
                async with session_scope() as db:
                    row = await db.get(ClipContest, c.id)
                    if row:
                        row.ended = True

    @end_loop.before_loop
    async def _before(self):
        await self.bot.wait_until_ready()

    # ───────────── /clip ─────────────
    @clip.command(name="einreichen", description="Reiche deinen Clip (TikTok-, YouTube- oder Instagram-Link) für den Clip-Contest ein")
    @app_commands.describe(link="Link zu deinem bereits hochgeladenen Clip")
    async def submit_cmd(self, interaction: discord.Interaction, link: app_commands.Range[str, 10, 300]):
        await interaction.response.defer(ephemeral=True)
        await self.submit(interaction, link)

    @clip.command(name="aufrufe", description="Melde die aktuellen Aufrufe deines Clips (mit Screenshot als Beweis)")
    @app_commands.describe(clip="Dein eingereichter Clip", aufrufe="z. B. 150000, 1,5m oder 150k", beweis="Screenshot, auf dem die Aufrufe zu sehen sind")
    async def views_cmd(self, interaction: discord.Interaction, clip: int, aufrufe: app_commands.Range[str, 1, 20], beweis: discord.Attachment):
        views = parse_views(aufrufe)
        if views is None:
            raise UserError("cc.bad_views")
        await interaction.response.defer(ephemeral=True)
        await self.claim_views(interaction, clip, views, beweis)

    @views_cmd.autocomplete("clip")
    async def _views_ac(self, interaction: discord.Interaction, current: str):
        c = await self.active(interaction.guild_id)
        if c is None:
            return []
        async with SessionLocal() as db:
            rows = (await db.execute(select(ClipSubmission).where(ClipSubmission.contest_id == c.id, ClipSubmission.user_id == interaction.user.id,
                                                                  ClipSubmission.status != "denied").order_by(ClipSubmission.id.desc()))).scalars().all()
        return [app_commands.Choice(name=f"#{s.id} · {PLATFORMS[s.platform][0]} · {fmt_num(s.views)} · {s.url.split('/')[-1]}"[:100], value=s.id)
                for s in rows if current.lower() in f"{s.id} {s.url}".lower()][:25]

    @clip.command(name="rangliste", description="Rangliste des Clip-Contests")
    async def board_cmd(self, interaction: discord.Interaction):
        await self.show_board(interaction)

    @clip.command(name="meine", description="Deine eingereichten Clips und ihr Status")
    async def mine_cmd(self, interaction: discord.Interaction):
        _ = await i18n.for_guild(interaction.guild_id)
        th = await theme(interaction.guild)
        c = await self.latest(interaction.guild_id)
        subs = [s for s in await self.submissions(c.id) if s.user_id == interaction.user.id]
        if not subs:
            await reply(interaction, th.info(_("cc.mine_title"), _("cc.mine_empty")))
            return
        cfg = await config.get(interaction.guild_id, "clipcontest")
        lines = [f"{STATUS[s.status][0]} **#{s.id}** · {PLATFORMS[s.platform][1]} 👁️ {fmt_num(s.views)}"
                 + (f" (⏳ {fmt_num(s.claimed_views)})" if s.claimed_views is not None else "")
                 + (f" · 💶 {money(clip_pay(s.views, cfg), cfg)}" if s.status == "approved" else "") + f"\n{s.url}" for s in subs]
        mine = payouts(await self.submissions(c.id), cfg, _).get(interaction.user.id)
        e = th.embed(_("cc.mine_title"), "\n".join(lines)[:4000], icon=False)
        e.add_field(name=_("cc.mine_total"), value=f"💶 **{money(mine[1] if mine else 0, cfg)}**", inline=False)
        await reply(interaction, e)

    # ───────────── /clipcontest (Team) ─────────────
    @contest.command(name="start", description="Startet einen Clip-Contest und postet die Ankündigung")
    @app_commands.describe(name="Name, z. B. Coray Clip-Contest", dauer="Wie lange? z. B. 60d = 2 Monate, 30d = 1 Monat",
                           channel="Wo soll die Ankündigung hin? (leer = dieser Channel)",
                           pruefchannel="Wo prüft das Team die Clips? Am besten ein Channel, den nur das Team sieht (wird gemerkt)")
    async def start_cmd(self, interaction: discord.Interaction, name: app_commands.Range[str, 3, 100], dauer: str = "60d",
                        channel: discord.TextChannel | None = None, pruefchannel: discord.TextChannel | None = None):
        await self.require_staff(interaction)
        _ = await i18n.for_guild(interaction.guild_id)
        secs = parse_duration(dauer)
        if not secs or secs < 3600 or secs > 366 * 86400:
            raise UserError("cc.bad_duration")
        if await self.active(interaction.guild_id):
            raise UserError("cc.already_running")
        review = await self.review_channel(interaction.guild, pruefchannel)
        await interaction.response.defer(ephemeral=True)
        cfg = await config.get(interaction.guild_id, "clipcontest")
        target = channel or interaction.guild.get_channel(cfg.id("announce_channel") or 0) or interaction.channel
        async with session_scope() as db:
            c = ClipContest(guild_id=interaction.guild_id, name=name, channel_id=target.id, host_id=interaction.user.id,
                            ends_at=utcnow() + timedelta(seconds=secs), ended=False)
            db.add(c)
            await db.flush()
        role_id = cfg.id("ping_role")
        msg = await target.send(content=f"<@&{role_id}>" if role_id else None, embed=await self.announce_embed(interaction.guild, c),
                                view=await self.announce_view(interaction.guild, c), allowed_mentions=discord.AllowedMentions(roles=True))
        async with session_scope() as db:
            (await db.get(ClipContest, c.id)).message_id = msg.id
        await log_event(interaction.guild_id, "clipcontest", "start", user=interaction.user, content=name, details={"id": c.id})
        await reply(interaction, (await theme(interaction.guild)).success(_("cc.started_title"), _("cc.started", channel=target.mention, review=review.mention,
                                                                                                                  end=ts(c.ends_at, "f"))))

    @contest.command(name="stufen", description="Zeigt oder ändert, wie viel Geld es pro Aufrufe gibt")
    @app_commands.describe(neu="Leer = anzeigen. Ändern z. B.: 5k=2, 10k=4, 25k=8, 50k=15, 100k=25")
    async def tiers_cmd(self, interaction: discord.Interaction, neu: app_commands.Range[str, 1, 500] | None = None):
        await self.require_staff(interaction)
        _ = await i18n.for_guild(interaction.guild_id)
        th = await theme(interaction.guild)
        cfg = await config.get(interaction.guild_id, "clipcontest")
        if neu:
            parsed = []
            for part in re.split(r"[,;\n]+", neu):
                if not part.strip():
                    continue
                views, _sep, amount = part.partition("=")
                v = parse_views(views)
                try:
                    a = float(amount.strip().replace("€", "").replace(",", ".").strip())
                except ValueError:
                    a = None
                if not v or a is None or a < 0:
                    raise UserError("cc.bad_tiers", part=part.strip()[:50])
                parsed.append({"views": v, "amount": a})
            if not parsed:
                raise UserError("cc.bad_tiers", part=neu[:50])
            cfg = {**cfg, "pay_tiers": sorted(parsed, key=lambda t: t["views"])}
            await config.save(interaction.guild_id, "clipcontest", settings=cfg)
            cfg = await config.get(interaction.guild_id, "clipcontest")
            if c := await self.active(interaction.guild_id):
                await self.refresh_announcement(interaction.guild, c)
        title = _("cc.tiers_saved") if neu else _("cc.tiers_title")
        await reply(interaction, th.embed(title, prize_text(cfg, _) + "\n\n" + _("cc.tiers_help"), kind="success" if neu else "primary", icon=False))

    @contest.command(name="ende", description="Beendet den laufenden Clip-Contest sofort und postet die Ergebnisse")
    async def end_cmd(self, interaction: discord.Interaction):
        await self.require_staff(interaction)
        c = await self.active(interaction.guild_id)
        if c is None:
            raise UserError("cc.no_active")
        await interaction.response.defer(ephemeral=True)
        await self.end(c)
        _ = await i18n.for_guild(interaction.guild_id)
        await reply(interaction, (await theme(interaction.guild)).success(_("common.success"), _("common.done")))

    @contest.command(name="auszahlung", description="Wer bekommt was? Auszahlungsliste des letzten Clip-Contests")
    async def payout_cmd(self, interaction: discord.Interaction):
        await self.require_staff(interaction)
        _ = await i18n.for_guild(interaction.guild_id)
        th = await theme(interaction.guild)
        c = await self.latest(interaction.guild_id)
        subs = await self.submissions(c.id)
        cfg = await config.get(interaction.guild_id, "clipcontest")
        paid = {s.user_id for s in subs if s.paid}
        items = sorted(payouts(subs, cfg, _).items(), key=lambda kv: (kv[0] in paid, -kv[1][1]))
        if not items:
            await reply(interaction, th.info(_("cc.payout_title", name=c.name), _("cc.payout_empty")))
            return
        total = sum(t for _uid, (_l, t) in items)
        open_total = sum(t for uid, (_l, t) in items if uid not in paid)
        pages = []
        for group in chunk(items, 6):
            e = th.embed(_("cc.payout_title", name=c.name), _("cc.payout_sum", total=money(total, cfg), open=money(open_total, cfg)), icon=False)
            for uid, (lines, amount) in group:
                member = interaction.guild.get_member(uid)
                e.add_field(name=f"{'✅' if uid in paid else '💸'} {member.display_name if member else uid} — {money(amount, cfg)}",
                            value=(f"<@{uid}>\n" + "\n".join(lines))[:1024], inline=False)
            e.set_footer(text=_("cc.payout_footer"))
            pages.append(e)
        await Paginator(interaction.user.id, pages).send(interaction, ephemeral=True)

    @contest.command(name="bezahlt", description="Markiert einen Gewinner als ausgezahlt")
    async def paid_cmd(self, interaction: discord.Interaction, user: discord.Member):
        await self.require_staff(interaction)
        c = await self.latest(interaction.guild_id)
        async with session_scope() as db:
            rows = (await db.execute(select(ClipSubmission).where(ClipSubmission.contest_id == c.id, ClipSubmission.user_id == user.id))).scalars().all()
            if not rows:
                raise UserError("cc.not_found")
            for r in rows:
                r.paid = True
        await log_event(interaction.guild_id, "clipcontest", "paid", user=interaction.user, target_id=user.id, details={"contest": c.id})
        _ = await i18n.for_guild(interaction.guild_id)
        await reply(interaction, (await theme(interaction.guild)).success(_("common.success"), _("cc.paid", user=user.mention)))


async def setup(bot: commands.Bot):
    bot.add_dynamic_items(ContestButton, ReviewButton)
    await bot.add_cog(ClipContestCog(bot))
