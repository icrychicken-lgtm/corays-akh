"""Zentrale Übersetzungen. Alle Bot-Texte liegen in app/locales/<lang>.json – nichts hardcoden."""
from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any, Callable

from app.config import settings
from app.core.guild_config import config

log = logging.getLogger("nova.i18n")
LOCALE_DIR = Path(__file__).resolve().parent.parent / "locales"
SUPPORTED = ("de", "en")


class _SafeDict(dict):
    def __missing__(self, key: str) -> str:
        return "{" + key + "}"


class Translator:
    def __init__(self) -> None:
        self.strings: dict[str, dict[str, str]] = {}
        self.reload()

    def reload(self) -> None:
        for lang in SUPPORTED:
            path = LOCALE_DIR / f"{lang}.json"
            try:
                self.strings[lang] = _flatten(json.loads(path.read_text(encoding="utf-8")))
            except FileNotFoundError:
                self.strings[lang] = {}
                log.error("Locale-Datei fehlt: %s", path)

    def t(self, lang: str, key: str, **kw: Any) -> str:
        text = self.strings.get(lang, {}).get(key) or self.strings.get("de", {}).get(key)
        if text is None:
            log.debug("Fehlender Übersetzungs-Key: %s", key)
            return key
        return fill(text, **kw) if kw else text

    async def lang(self, guild_id: int | None) -> str:
        if not guild_id:
            return settings.default_locale
        cfg = await config.get(guild_id, "general")
        return cfg.get("language") or settings.default_locale

    async def for_guild(self, guild_id: int | None) -> Callable[..., str]:
        lang = await self.lang(guild_id)
        return lambda key, **kw: self.t(lang, key, **kw)


def _flatten(d: dict[str, Any], prefix: str = "") -> dict[str, str]:
    out: dict[str, str] = {}
    for k, v in d.items():
        key = f"{prefix}{k}"
        if isinstance(v, dict):
            out.update(_flatten(v, key + "."))
        else:
            out[key] = str(v)
    return out


def fill(template: str, **kw: Any) -> str:
    """Platzhalter in User-Vorlagen ersetzen ({user}, {server}, …) – unbekannte bleiben stehen.
    Ungültige Vorlagen (z. B. einzelne Klammern) werden unverändert zurückgegeben statt abzustürzen."""
    try:
        return (template or "").format_map(_SafeDict(kw))
    except (ValueError, IndexError, AttributeError, KeyError):
        out = template or ""
        for k, v in kw.items():
            out = out.replace("{" + k + "}", str(v))
        return out


i18n = Translator()
