"""Fehler, die dem User als saubere (übersetzte) Meldung angezeigt werden."""
from __future__ import annotations

from typing import Any

from discord import app_commands


class UserError(Exception):
    def __init__(self, key: str, **kw: Any):
        super().__init__(key)
        self.key = key
        self.kw = kw


class NovaCheckFailure(app_commands.CheckFailure):
    def __init__(self, key: str, **kw: Any):
        super().__init__(key)
        self.key = key
        self.kw = kw
