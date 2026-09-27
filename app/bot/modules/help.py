"""Modernes /help-Menü mit Kategorien (Select) und Command-Suche (Autocomplete)."""
from __future__ import annotations

import discord
from discord import app_commands
from discord.ext import commands

from app.bot.bot import module_of
from app.bot.ui import BaseView, reply
from app.core.embeds import theme
from app.core.guild_config import config
from app.core.i18n import i18n

CATEGORIES = [
    ("moderation", "🛡️"), ("community", "💬"), ("economy", "💰"), ("music", "🎵"),
    ("tickets", "🎫"), ("fun", "🎲"), ("streamer", "📡"), ("admin", "⚙️"),
]


def _category(cmd: app_commands.Command | app_commands.Group) -> str:
    binding = getattr(cmd, "binding", None)
    if binding is None and isinstance(cmd, app_commands.Group):
        sub = next(iter(cmd.walk_commands()), None)
        binding = getattr(sub, "binding", None)
    return getattr(binding, "help_category", None) or (cmd.extras or {}).get("help_category") or "community"


def _flatten(cmd: app_commands.Command | app_commands.Group) -> list[app_commands.Command]:
    if isinstance(cmd, app_commands.Group):
        return [c for c in cmd.walk_commands() if isinstance(c, app_commands.Command)]
    return [cmd]


class HelpSelect(discord.ui.Select):
    def __init__(self, labels: dict[str, str], counts: dict[str, int]):
        options = [
            discord.SelectOption(label=labels[key], value=key, emoji=emoji, description=f"{counts.get(key, 0)} Commands")
            for key, emoji in CATEGORIES if counts.get(key)
        ]
        super().__init__(placeholder="📚 " + labels["_placeholder"], options=options, min_values=1, max_values=1)

    async def callback(self, interaction: discord.Interaction):
        view: HelpView = self.view  # type: ignore[assignment]
        await interaction.response.edit_message(embed=await view.cog.category_embed(interaction, self.values[0]), view=view)


class HelpView(BaseView):
    def __init__(self, cog: "Help", owner_id: int, labels: dict[str, str], counts: dict[str, int]):
        super().__init__(owner_id=owner_id, timeout=300)
        self.cog = cog
        self.add_item(HelpSelect(labels, counts))


class Help(commands.Cog):
    module = "core"
    help_category = "community"

    def __init__(self, bot: commands.Bot):
        self.bot = bot

    async def _visible(self, guild_id: int | None) -> list[app_commands.Command]:
        toggles = await config.all_toggles(guild_id) if guild_id else {}
        out = []
        for root in self.bot.tree.get_commands(type=discord.AppCommandType.chat_input):
            mod = module_of(root) if not isinstance(root, app_commands.Group) else module_of(next(iter(root.walk_commands()), None))
            if guild_id and mod != "core" and not toggles.get(mod, True):
                continue
            out.extend(_flatten(root))  # type: ignore[arg-type]
        return out

    async def category_embed(self, interaction: discord.Interaction, key: str) -> discord.Embed:
        _ = await i18n.for_guild(interaction.guild_id)
        th = await theme(interaction.guild)
        cmds = [c for c in await self._visible(interaction.guild_id) if _category(c.root_parent or c) == key]
        emoji = dict(CATEGORIES).get(key, "•")
        lines =[f"`/{c.qualified_name}` — {c.description}" for c in sorted(cmds, key=lambda c: c.qualified_name)]
        e = th.embed(f"{emoji}  {_('help.cat.' + key)}", "\n".join(lines)[:4000] or _("help.empty"), icon=False)
        return e

    @app_commands.command(name="help", description="Zeigt alle Commands und Kategorien")
    @app_commands.describe(command="Nach einem bestimmten Command suchen")
    async def help(self, interaction: discord.Interaction, command: str | None = None):
        _ = await i18n.for_guild(interaction.guild_id)
        th = await theme(interaction.guild)
        visible = await self._visible(interaction.guild_id)
        if command:
            match = next((c for c in visible if c.qualified_name == command.strip("/ ").lower()), None)
            if not match:
                await reply(interaction, th.error(_("common.error"), _("help.not_found", command=command)))
                return
            e = th.embed(f"/{match.qualified_name}", match.description, icon=False)
            params = [f"`{p.name}`{'' if p.required else ' *(optional)*'} — {p.description}" for p in match.parameters]
            if params:
                e.add_field(name=_("help.params"), value="\n".join(params)[:1024], inline=False)
            e.add_field(name=_("help.category"), value=_("help.cat." + _category(match.root_parent or match)), inline=True)
            await reply(interaction, e)
            return

        counts: dict[str, int] = {}
        for c in visible:
            k = _category(c.root_parent or c)
            counts[k] = counts.get(k, 0) + 1
        labels = {key: _("help.cat." + key) for key, _e in CATEGORIES} | {"_placeholder": _("help.placeholder")}
        e = th.embed(_("help.title", bot=self.bot.user.name if self.bot.user else "Nova"), _("help.intro"), icon=False)
        if self.bot.user:
            e.set_thumbnail(url=self.bot.user.display_avatar.url)
        overview = [f"{emoji} **{labels[key]}** · {counts[key]}" for key, emoji in CATEGORIES if counts.get(key)]
        e.add_field(name=_("help.categories"), value="\n".join(overview) or "—", inline=False)
        e.add_field(name=_("help.tip_title"), value=_("help.tip"), inline=False)
        view = HelpView(self, interaction.user.id, labels, counts)
        await reply(interaction, e, view=view)
        view.message = await interaction.original_response()

    @help.autocomplete("command")
    async def _ac(self, interaction: discord.Interaction, current: str):
        cur = current.lower().strip("/ ")
        names = sorted({c.qualified_name for c in await self._visible(interaction.guild_id)})
        return [app_commands.Choice(name=f"/{n}", value=n) for n in names if cur in n][:25]


async def setup(bot: commands.Bot):
    await bot.add_cog(Help(bot))
