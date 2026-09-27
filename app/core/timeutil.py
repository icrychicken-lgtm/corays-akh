"""Dauer-Parsing ("2h30m", "1d", "90s") und menschenlesbare Ausgabe."""
from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

_UNITS = {"s": 1, "sek": 1, "m": 60, "min": 60, "h": 3600, "std": 3600, "d": 86400, "t": 86400, "w": 604800}
_RE = re.compile(r"(\d+)\s*(sek|min|std|s|m|h|d|t|w)", re.I)


def parse_duration(text: str | None) -> int | None:
    """Gibt Sekunden zurück oder None, wenn nichts erkannt wurde."""
    if not text:
        return None
    text = text.strip().lower()
    if text.isdigit():
        return int(text) * 60  # reine Zahl = Minuten
    total, pos = 0, 0
    for m in _RE.finditer(text):
        if text[pos:m.start()].strip():
            return None
        total += int(m.group(1)) * _UNITS[m.group(2)]
        pos = m.end()
    if text[pos:].strip() or total <= 0:
        return None
    return total


def human_duration(seconds: int | float | None, lang: str = "de") -> str:
    if not seconds:
        return "—"
    seconds = int(seconds)
    names = {
        "de": (("T", 86400), ("h", 3600), ("min", 60), ("s", 1)),
        "en": (("d", 86400), ("h", 3600), ("m", 60), ("s", 1)),
    }[lang if lang in ("de", "en") else "de"]
    parts = []
    for name, size in names:
        if seconds >= size:
            val, seconds = divmod(seconds, size)
            parts.append(f"{val}{name}")
        if len(parts) == 2:
            break
    return " ".join(parts) or "0s"


def ts(dt: datetime, style: str = "R") -> str:
    """Discord-Timestamp (<t:…:R>) – wird im Client lokal formatiert."""
    return f"<t:{int(as_utc(dt).timestamp())}:{style}>"


def tz(name: str | None) -> ZoneInfo:
    try:
        return ZoneInfo(name or "Europe/Berlin")
    except (ZoneInfoNotFoundError, ValueError):
        return ZoneInfo("UTC")


def local_now(tz_name: str | None) -> datetime:
    return datetime.now(timezone.utc).astimezone(tz(tz_name))


def day_key(tz_name: str | None) -> str:
    return local_now(tz_name).date().isoformat()


def week_key(tz_name: str | None) -> str:
    y, w, _ = local_now(tz_name).isocalendar()
    return f"{y}-W{w:02d}"


def as_utc(dt: datetime | None) -> datetime | None:
    if dt is None:
        return None
    return dt.replace(tzinfo=timezone.utc) if dt.tzinfo is None else dt


def since(dt: datetime | None) -> timedelta:
    return datetime.now(timezone.utc) - (as_utc(dt) or datetime.now(timezone.utc))
