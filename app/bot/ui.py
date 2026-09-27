"""UI-Bausteine: Antworten, Fehler, Bestätigungsdialoge, Paginierung, abgesicherte Callbacks."""
from __future__ import annotations

import functools
import logging
from typing import Any, Awaitable, Callable, Sequence

import discord

from app.core.embeds import theme
from app.core.errors import UserError
from app.core.i18n import i18n
from app.core.records import capture_error

log = logging.getLogger("nova.ui")


async def reply(interaction: discord.Interaction, embed: discord.Embed | None = None, *, content: str | None = None,
                ephemeral: bool = True, view: discord.ui.View | None = None, file: discord.File | None = None) -> None:
    kwargs: dict[str, Any] = {"content": content, "embed": embed, "ephemeral": ephemeral}
    if view is not None:
        kwargs["view"] = view
    if file is not None:
        kwargs["file"] = file
    if interaction.response.is_done():
        await interaction.followup.send(**kwargs)
    else:
        await interaction.response.send_message(**kwargs)


async def ok(interaction: discord.Interaction, key: str, *, ephemeral: bool = True, title_key: str | None = None, **kw: Any) -> None:
    _ = await i18n.for_guild(interaction.guild_id)
    th = await theme(interaction.guild)
    await reply(interaction, th.success(_(title_key or "common.success"), _(key, **kw)), ephemeral=ephemeral)


async def fail(interaction: discord.Interaction, key: str, **kw: Any) -> None:
    _ = await i18n.for_guild(interaction.guild_id)
    th = await theme(interaction.guild)
    await reply(interaction, th.error(_("common.error"), _(key, **kw)), ephemeral=True)


async def handle_exception(interaction: discord.Interaction, exc: BaseException, where: str | None = None) -> None:
    """Einheitliche Fehlerausgabe: UserErrors freundlich, alles andere mit Error-ID."""
    try:
        if isinstance(exc, UserError):
            await fail(interaction, exc.key, **exc.kw)
            return
        if isinstance(exc, discord.Forbidden):
            await fail(interaction, "errors.bot_forbidden")
            return
        err_id = await capture_error(exc, guild_id=interaction.guild_id, user_id=interaction.user.id,
                                     command=where or getattr(interaction.command, "qualified_name", None))
        await fail(interaction, "errors.unexpected", id=err_id)
    except discord.HTTPException:
        pass
    except Exception:
        log.exception("Fehler beim Anzeigen eines Fehlers")


def guarded(func: Callable[..., Awaitable[Any]]):
    """Dekorator für Button/Select/Modal-Callbacks – Fehler brechen den Bot nie ab."""

    @functools.wraps(func)
    async def wrapper(self, interaction: discord.Interaction, *args, **kwargs):
        try:
            return await func(self, interaction, *args, **kwargs)
        except Exception as exc:  # noqa: BLE001
            await handle_exception(interaction, exc, where=f"{type(self).__name__}.{func.__name__}")

    return wrapper


class BaseView(discord.ui.View):
    """View mit Owner-Beschränkung und sauberem Fehler-Handling."""

    def __init__(self, *, owner_id: int | None = None, timeout: float | None = 180):
        super().__init__(timeout=timeout)
        self.owner_id = owner_id
        self.message: discord.Message | None = None

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if self.owner_id and interaction.user.id != self.owner_id:
            await fail(interaction, "errors.not_your_menu")
            return False
        return True

    async def on_error(self, interaction: discord.Interaction, error: Exception, item: discord.ui.Item) -> None:
        await handle_exception(interaction, error, where=f"{type(self).__name__}")

    async def on_timeout(self) -> None:
        for child in self.children:
            if hasattr(child, "disabled"):
                child.disabled = True
        if self.message:
            try:
                await self.message.edit(view=self)
            except discord.HTTPException:
                pass


class ConfirmView(BaseView):
    """Bestätigungsdialog für gefährliche Aktionen."""

    def __init__(self, owner_id: int, confirm_label: str, cancel_label: str, *, danger: bool = True):
        super().__init__(owner_id=owner_id, timeout=60)
        self.value: bool | None = None
        self.confirm.label = confirm_label
        self.confirm.style = discord.ButtonStyle.danger if danger else discord.ButtonStyle.success
        self.cancel.label = cancel_label

    @discord.ui.button(label="OK", emoji="✔️")
    async def confirm(self, interaction: discord.Interaction, _: discord.ui.Button):
        self.value = True
        await interaction.response.defer()
        self.stop()

    @discord.ui.button(label="Cancel", style=discord.ButtonStyle.secondary, emoji="✖️")
    async def cancel(self, interaction: discord.Interaction, _: discord.ui.Button):
        self.value = False
        await interaction.response.defer()
        self.stop()


async def confirm(interaction: discord.Interaction, title: str, description: str) -> bool:
    _ = await i18n.for_guild(interaction.guild_id)
    th = await theme(interaction.guild)
    view = ConfirmView(interaction.user.id, _("common.confirm"), _("common.cancel"))
    await reply(interaction, th.warning(title, description), view=view)
    await view.wait()
    try:
        await interaction.edit_original_response(view=None)
    except discord.HTTPException:
        pass
    return bool(view.value)


class Paginator(BaseView):
    def __init__(self, owner_id: int, pages: Sequence[discord.Embed]):
        super().__init__(owner_id=owner_id, timeout=300)
        self.pages = list(pages)
        self.index = 0
        self._sync()

    def _sync(self) -> None:
        self.prev.disabled = self.index == 0
        self.next.disabled = self.index >= len(self.pages) - 1
        self.counter.label = f"{self.index + 1} / {len(self.pages)}"

    @discord.ui.button(emoji="◀️", style=discord.ButtonStyle.secondary)
    async def prev(self, interaction: discord.Interaction, _: discord.ui.Button):
        self.index -= 1
        self._sync()
        await interaction.response.edit_message(embed=self.pages[self.index], view=self)

    @discord.ui.button(label="1 / 1", style=discord.ButtonStyle.secondary, disabled=True)
    async def counter(self, interaction: discord.Interaction, _: discord.ui.Button):
        await interaction.response.defer()

    @discord.ui.button(emoji="▶️", style=discord.ButtonStyle.secondary)
    async def next(self, interaction: discord.Interaction, _: discord.ui.Button):
        self.index += 1
        self._sync()
        await interaction.response.edit_message(embed=self.pages[self.index], view=self)

    async def send(self, interaction: discord.Interaction, ephemeral: bool = False) -> None:
        if len(self.pages) == 1:
            await reply(interaction, self.pages[0], ephemeral=ephemeral)
            return
        await reply(interaction, self.pages[0], view=self, ephemeral=ephemeral)
        self.message = await interaction.original_response()


def chunk(items: Sequence[Any], size: int) -> list[Sequence[Any]]:
    return [items[i:i + size] for i in range(0, len(items), size)] or [[]]
