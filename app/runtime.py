"""Laufzeit-Referenzen, die Bot und Dashboard teilen (ein Prozess, ein Event-Loop)."""
from __future__ import annotations

import time
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from app.bot.bot import NovaBot

STARTED_AT = time.time()
bot: "NovaBot | None" = None


def get_bot() -> "NovaBot":
    if bot is None:
        raise RuntimeError("Bot ist noch nicht initialisiert")
    return bot
