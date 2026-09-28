"""Onboarding: /setup (vorhandene Channels/Rollen auswählen – erstellt nichts, leer = aus), /guide (verständliche Erklärungen), Begrüßung beim Server-Beitritt."""
from __future__ import annotations

import logging

import discord
from discord import app_commands
from discord.ext import commands
from sqlalchemy import func, select

from app.bot.ui import BaseView, reply
from app.config import settings
from app.core.embeds import fmt_num, theme
from app.core.errors import UserError
from app.core.guild_config import config
from app.core.i18n import i18n
from app.core.records import audit
from app.db.base import SessionLocal, session_scope
from app.db.models import TicketCategory
from app.services.notify import is_admin
from app.services.progression import total_for_level

log = logging.getLogger("nova.onboarding")




# /setup erstellt NICHTS – man wählt nur vorhandene Channels/Rollen aus. (modul, key, art, label-key)
SETUP_PAGES: list[list[tuple[str, str, str, str]]] = [
    [("streamer", "default_channel", "channel", "setup.pick_live"), ("streamer", "default_role", "role", "setup.pick_ping"),
     ("streamer", "clips_channel", "channel", "setup.pick_clips"), ("clipcontest", "review_channel", "channel", "setup.pick_review")],
    [("welcome", "channel", "channel", "setup.pick_welcome"), ("moderation", "log_channel", "channel", "setup.pick_modlog"),
     ("levels", "announce_channel", "channel", "setup.pick_levels"), ("suggestions", "channel", "forum_ok", "setup.pick_ideas")],
]
TEXT_TYPES = [discord.ChannelType.text, discord.ChannelType.news]
FORUM_OK_TYPES = [*TEXT_TYPES, discord.ChannelType.forum]  # Vorschläge dürfen auch in ein Forum


class _PickMixin:
    """Merkt sich die Auswahl (leer = Funktion aus); gespeichert wird erst mit „Speichern“."""
    setup_view: "SetupView"
    module: str
    key: str

    async def callback(self, interaction: discord.Interaction):
        self.setup_view.changes[(self.module, self.key)] = str(self.values[0].id) if self.values else None  # type: ignore[attr-defined]
        await interaction.response.defer()


class SetupChannelPick(_PickMixin, discord.ui.ChannelSelect):
    pass


class SetupRolePick(_PickMixin, discord.ui.RoleSelect):
    pass


def setup_pick(view: "SetupView", module: str, key: str, kind: str, placeholder: str, current, row: int) -> discord.ui.Item:
    kw = dict(placeholder=placeholder[:150], min_values=0, max_values=1, row=row, default_values=[current] if current else [])
    item = SetupRolePick(**kw) if kind == "role" else SetupChannelPick(channel_types=FORUM_OK_TYPES if kind == "forum_ok" else TEXT_TYPES, **kw)
    item.setup_view, item.module, item.key = view, module, key
    return item


class SetupView(BaseView):
    def __init__(self, cog: "Onboarding", owner_id: int, guild: discord.Guild, _):
        super().__init__(owner_id=owner_id, timeout=600)
        self.cog, self.guild, self._ = cog, guild, _
        self.page = 0
        self.changes: dict[tuple[str, str], str | None] = {}
        self.current: dict[tuple[str, str], int | None] = {}

    async def load(self) -> "SetupView":
        for page in SETUP_PAGES:
            for module, key, _kind, _label in page:
                self.current[(module, key)] = (await config.get(self.guild.id, module)).id(key)
        self.build()
        return self

    def _value(self, module: str, key: str, kind: str):
        raw = self.changes.get((module, key), self.current.get((module, key)))
        if not raw:
            return None
        obj = self.guild.get_role(int(raw)) if kind == "role" else self.guild.get_channel(int(raw))
        return obj

    def build(self) -> None:
        _ = self._
        self.clear_items()
        for row, (module, key, kind, label) in enumerate(SETUP_PAGES[self.page]):
            self.add_item(setup_pick(self, module, key, kind, _(label), self._value(module, key, kind), row))
        if self.page > 0:
            self.add_item(self._button(_("setup.back"), "◀️", discord.ButtonStyle.secondary, self.back))
        if self.page < len(SETUP_PAGES) - 1:
            self.add_item(self._button(_("setup.next"), "▶️", discord.ButtonStyle.primary, self.forward))
        self.add_item(self._button(_("setup.save"), "💾", discord.ButtonStyle.success, self.save))

    @staticmethod
    def _button(label: str, emoji: str, style: discord.ButtonStyle, cb) -> discord.ui.Button:
        b = discord.ui.Button(label=label, emoji=emoji, style=style, row=4)
        b.callback = cb
        return b

    def embed_for(self, th) -> discord.Embed:
        _ = self._
        return th.embed(_(f"setup.page{self.page + 1}_title"), _(f"setup.page{self.page + 1}_text"), icon=False) \
            .set_footer(text=_("setup.footer", page=self.page + 1, pages=len(SETUP_PAGES)))

    async def forward(self, interaction: discord.Interaction):
        self.page += 1
        self.build()
        await interaction.response.edit_message(embed=self.embed_for(await theme(self.guild)), view=self)

    async def back(self, interaction: discord.Interaction):
        self.page -= 1
        self.build()
        await interaction.response.edit_message(embed=self.embed_for(await theme(self.guild)), view=self)

    async def save(self, interaction: discord.Interaction):
        """Speichert ALLE Felder: Was leer ist, wird ausgeschaltet – nicht nur „nicht geändert“."""
        _ = self._
        await interaction.response.defer()
        final = {k: self.changes.get(k, str(v) if v else None) for k, v in self.current.items()}
        by_module: dict[str, dict[str, str | None]] = {}
        for (module, key), value in final.items():
            by_module.setdefault(module, {})[key] = value
        for module, values in by_module.items():
            cfg = dict(await config.get(self.guild.id, module))
            cfg.update(values)
            enabled = None
            if module == "levels":
                cfg["announce"] = "custom" if values["announce_channel"] else "off"  # kein Channel = keine Level-Up-Nachrichten
            elif module == "streamer":
                cfg["clips_enabled"] = cfg["clip_week"] = bool(values["clips_channel"])  # kein Clip-Channel = keine Clip-Posts
            elif module in ("welcome", "suggestions"):
                enabled = bool(values["channel"])  # kein Channel = Funktion aus
            await config.save(self.guild.id, module, settings=cfg, enabled=enabled)
        # Vorschläge laufen übers Formular: Knopf „Vorschlag einreichen“ in den (neu gewählten) Channel posten
        ideas = self.changes.get(("suggestions", "channel"))
        if ideas and ideas != str(self.current.get(("suggestions", "channel")) or ""):
            try:
                await self.cog.bot.get_cog("Suggestions").send_panel(self.guild)  # type: ignore[union-attr]
            except Exception as exc:  # noqa: BLE001
                log.info("Vorschlags-Panel nicht gesendet: %s", exc)
        await audit(self.guild.id, interaction.user.id, str(interaction.user), "setup.pick", f"{len(self.changes)} geändert", source="bot")
        lines = []
        for page in SETUP_PAGES:
            for module, key, kind, label in page:
                value = final[(module, key)]
                name = _(label.replace("pick_", "short_"))
                mention = (f"<@&{value}>" if kind == "role" else f"<#{value}>") if value else None
                lines.append(f"✅ {name}: {mention}" if mention else f"⛔ {name}: **{_('setup.off')}**")
        th = await theme(self.guild)
        await interaction.edit_original_response(embed=th.success(_("setup.saved_title"), "\n".join(lines) + "\n\n" + _("setup.saved")), view=None)
        self.stop()


class GuideSelect(discord.ui.Select):
    def __init__(self, cog: "Onboarding", labels: dict[str, str]):
        topics = [("start", "👋"), ("xp", "⭐"), ("coins", "💰"), ("gangs", "🏴"), ("streams", "📡"), ("tickets", "🎫")]
        super().__init__(placeholder=labels["placeholder"], options=[discord.SelectOption(label=labels[k], value=k, emoji=e) for k, e in topics])
        self.cog = cog

    async def callback(self, interaction: discord.Interaction):
        await interaction.response.edit_message(embed=await self.cog.guide_embed(interaction.guild, self.values[0]), view=self.view)


class Onboarding(commands.Cog):
    module = "core"
    help_category = "admin"

    def __init__(self, bot: commands.Bot):
        self.bot = bot

    # ── Setup: nur auswählen, nie erstellen ──
    async def auto_setup(self, guild: discord.Guild, actor: discord.abc.User) -> list[str]:
        """Früher: Channels/Rollen automatisch erstellen. Abgeschafft – /setup in Discord wählt vorhandene aus."""
        raise UserError("setup.use_discord")

    @app_commands.command(name="setup", description="Bot einrichten: wähle deine vorhandenen Channels & Rollen aus (erstellt nichts)")
    @app_commands.guild_only()
    @app_commands.default_permissions(manage_guild=True)
    async def setup_cmd(self, interaction: discord.Interaction):
        if not await is_admin(interaction.user):  # type: ignore[arg-type]
            raise UserError("errors.no_permission")
        _ = await i18n.for_guild(interaction.guild_id)
        view = await SetupView(self, interaction.user.id, interaction.guild, _).load()
        await reply(interaction, view.embed_for(await theme(interaction.guild)), view=view)

    # ── Guide ──
    async def guide_embed(self, guild: discord.Guild, topic: str) -> discord.Embed:
        _ = await i18n.for_guild(guild.id)
        th = await theme(guild)
        lv = await config.get(guild.id, "levels")
        eco = await config.get(guild.id, "economy")
        gangs = await config.get(guild.id, "gangs")
        cur = f"{eco.get('currency_emoji')} {eco.get('currency_name')}"
        vals = dict(xp_min=lv.get("xp_min"), xp_max=lv.get("xp_max"), cooldown=lv.get("cooldown"), voice_xp=lv.get("voice_xp"),
                    reaction_xp=lv.get("reaction_xp"), stream_xp=lv.get("stream_xp"), lvl5=fmt_num(total_for_level(5)),
                    lvl10=fmt_num(total_for_level(10)), lvl25=fmt_num(total_for_level(25)), lvl50=fmt_num(total_for_level(50)),
                    currency=cur, daily=eco.get("daily_amount"), weekly=eco.get("weekly_amount"), streak7=eco.get("streak_7_bonus"),
                    streak30=eco.get("streak_30_bonus"), msg_min=eco.get("message_min"), msg_max=eco.get("message_max"),
                    gang_cost=fmt_num(gangs.get("create_cost", 5000)), gang_max=gangs.get("max_members", 15), bot=self.bot.user.name if self.bot.user else "Bot")
        e = th.embed(_(f"guide.{topic}.title"), _(f"guide.{topic}.text", **vals), icon=False)
        e.set_footer(text=_("guide.footer"))
        return e

    @app_commands.command(name="guide", description="Einfach erklärt: XP, Coins, Gangs, Streams, Tickets")
    @app_commands.guild_only()
    async def guide(self, interaction: discord.Interaction):
        _ = await i18n.for_guild(interaction.guild_id)
        view = BaseView(owner_id=interaction.user.id, timeout=600)
        labels = {k: _(f"guide.{k}.title") for k in ("start", "xp", "coins", "gangs", "streams", "tickets")} | {"placeholder": _("guide.placeholder")}
        view.add_item(GuideSelect(self, labels))
        await reply(interaction, await self.guide_embed(interaction.guild, "start"), view=view)

    # ── Begrüßung beim Hinzufügen ──
    @commands.Cog.listener()
    async def on_guild_join(self, guild: discord.Guild):
        _ = await i18n.for_guild(guild.id)
        th = await theme(guild)
        e = th.embed(_("setup.join_title", bot=self.bot.user.name if self.bot.user else "Bot"), _("setup.join_text", url=settings.dashboard_url), icon=False)
        if self.bot.user:
            e.set_thumbnail(url=self.bot.user.display_avatar.url)
        channel = guild.system_channel if guild.system_channel and guild.system_channel.permissions_for(guild.me).send_messages else \
            next((c for c in guild.text_channels if c.permissions_for(guild.me).send_messages), None)
        try:
            if channel:
                await channel.send(embed=e)
        except discord.HTTPException:
            pass


async def setup(bot: commands.Bot):
    await bot.add_cog(Onboarding(bot))
