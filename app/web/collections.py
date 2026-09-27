"""Generische, schemabasierte CRUD-Collections fürs Dashboard (validiert serverseitig, gleiche Felddefinition wie das UI)."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable

from sqlalchemy import BigInteger, Integer

from app.core.schema import NAME_TYPES_SINGLE, F
from app.db import models as m
from app.services.progression import QUEST_METRICS

ID_SINGLE = NAME_TYPES_SINGLE


@dataclass
class Collection:
    key: str
    model: type
    title: str
    fields: list[F]
    order: list[str] = field(default_factory=lambda: ["id"])
    level: str = "admin"
    columns: list[str] = field(default_factory=list)  # Spalten für die Tabellenansicht
    actions: list[dict[str, str]] = field(default_factory=list)
    limit: int = 200
    after_save: Callable[..., Awaitable[None]] | None = None
    after_delete: Callable[..., Awaitable[None]] | None = None
    validate: Callable[..., Awaitable[None]] | None = None
    readonly_extra: list[str] = field(default_factory=list)

    def to_json(self) -> dict[str, Any]:
        return {"key": self.key, "title": self.title, "fields": [f.to_json() for f in self.fields], "columns": self.columns,
                "actions": self.actions, "level": self.level}


def serialize(col: Collection, obj: Any) -> dict[str, Any]:
    d = obj.to_dict()
    d["id"] = obj.id
    for f in col.fields:
        v = getattr(obj, f.key, None)
        if f.type in ID_SINGLE and v is not None:
            d[f.key] = str(v)
    for extra in col.readonly_extra:
        v = getattr(obj, extra, None)
        d[extra] = str(v) if isinstance(v, int) and v > 2**40 else v
    return d


def to_column(obj_model: type, key: str, value: Any) -> Any:
    col = obj_model.__table__.columns.get(key)
    if col is not None and isinstance(col.type, (BigInteger, Integer)) and isinstance(value, str) and value.isdigit():
        return int(value)
    return value


EMOJI = lambda default="": F("emoji", "str", "Emoji", default, max_len=64)  # noqa: E731

COLLECTIONS: dict[str, Collection] = {}


def register(c: Collection) -> Collection:
    COLLECTIONS[c.key] = c
    return c


register(Collection("ticket_categories", m.TicketCategory, "Ticket-Kategorien", [
    F("label", "str", "Name", "", max_len=80), EMOJI("🎫"), F("description", "str", "Beschreibung", "", max_len=100),
    F("category_channel_id", "category", "Discord-Kategorie", None), F("staff_role_ids", "roles", "Zusätzliche Support-Rollen", []),
    F("questions", "objlist", "Fragen beim Öffnen (max. 5)", [], help="Werden im Formular gestellt, bevor das Ticket erstellt wird", fields=[
        F("label", "str", "Frage", "", max_len=45), F("placeholder", "str", "Beispiel / Hinweis", "", max_len=100),
        F("style", "select", "Antwort", "short", options=[("short", "Kurz (eine Zeile)"), ("paragraph", "Lang (mehrere Zeilen)")]),
        F("required", "bool", "Pflicht", True)]),
    F("welcome_message", "text", "Text im Ticket (leer = Standard)", "", max_len=2000), F("position", "int", "Reihenfolge", 0, min=0, max=100),
    F("enabled", "bool", "Aktiv", True),
], order=["position", "id"], columns=["emoji", "label", "description", "enabled"]))

register(Collection("role_menus", m.RoleMenu, "Rollen-Menüs", [
    F("name", "str", "Interner Name", "", max_len=80), F("title", "str", "Titel", "", max_len=200),
    F("description", "text", "Beschreibung", "", max_len=2000),
    F("style", "select", "Darstellung", "buttons", options=[("buttons", "Buttons"), ("select", "Select-Menü")]),
    F("options", "objlist", "Rollen", [], fields=[F("role_id", "role", "Rolle", None), F("label", "str", "Label", "", max_len=80),
                                                   F("emoji", "str", "Emoji", "", max_len=64), F("description", "str", "Beschreibung", "", max_len=100)]),
    F("max_values", "int", "Max. gleichzeitig (0 = unbegrenzt, 1 = exklusiv)", 0, min=0, max=25),
    F("required_role_id", "role", "Benötigte Rolle", None), F("color", "color_opt", "Farbe", None), F("image_url", "url", "Bild-URL", ""),
    F("channel_id", "channel", "Channel", None),
], columns=["name", "style", "channel_id", "message_id"], actions=[{"key": "publish", "label": "Veröffentlichen / Aktualisieren", "icon": "send"}],
    readonly_extra=["message_id"]))

register(Collection("level_rewards", m.LevelReward, "Level-Belohnungen", [
    F("level", "int", "Level", 5, min=1, max=1000), F("role_id", "role", "Rolle", None), F("coins", "int", "Bonus-Coins", 0, min=0, max=10_000_000),
], order=["level"], columns=["level", "role_id", "coins"]))

register(Collection("shop_items", m.ShopItem, "Shop-Items", [
    F("name", "str", "Name", "", max_len=80), EMOJI("🛍️"), F("description", "str", "Beschreibung", "", max_len=200),
    F("price", "int", "Preis", 100, min=0, max=1_000_000_000),
    F("kind", "select", "Typ", "cosmetic", options=[("role", "Rolle"), ("color", "Farbrolle"), ("badge", "Badge"), ("cosmetic", "Kosmetisch"), ("event", "Event-Item")]),
    F("role_id", "role", "Rolle (bei Rolle/Farbe)", None), F("badge_id", "badge", "Badge (bei Badge)", None),
    F("stock", "int", "Bestand (-1 = unbegrenzt)", -1, min=-1, max=1_000_000), F("max_per_user", "int", "Max. pro User (0 = unbegrenzt)", 1, min=0, max=1000),
    F("position", "int", "Reihenfolge", 0, min=0, max=1000), F("enabled", "bool", "Aktiv", True),
], order=["position", "price"], columns=["emoji", "name", "kind", "price", "stock", "enabled"]))

register(Collection("quests", m.Quest, "Quests", [
    F("name", "str", "Name", "", max_len=100), F("description", "str", "Beschreibung", "", max_len=200),
    F("period", "select", "Zeitraum", "daily", options=[("daily", "Täglich"), ("weekly", "Wöchentlich")]),
    F("metric", "select", "Aufgabe", "messages", options=list(QUEST_METRICS.items())), F("target", "int", "Ziel", 50, min=1, max=1_000_000),
    F("reward_xp", "int", "XP", 100, min=0, max=10_000_000), F("reward_coins", "int", "Coins", 100, min=0, max=10_000_000),
    F("reward_role_id", "role", "Rolle", None), F("reward_badge_id", "badge", "Badge", None), F("enabled", "bool", "Aktiv", True),
], order=["period", "id"], columns=["name", "period", "metric", "target", "reward_xp", "reward_coins", "enabled"]))

register(Collection("badges", m.Badge, "Badges", [
    F("name", "str", "Name", "", max_len=50), EMOJI("🏅"), F("description", "str", "Beschreibung", "", max_len=200),
    F("auto_rule", "select", "Automatisch vergeben an", "manual", options=[
        ("manual", "Nur manuell / Shop / Quest"), ("owner", "Server-Owner"), ("staff", "Staff"), ("booster", "Booster"),
        ("giveaway_winner", "Giveaway-Gewinner"), ("og", "OG (beigetreten vor Datum)"), ("role", "Rolle"), ("level", "Ab Level")]),
    F("rule_value", "str", "Regel-Wert (Rollen-ID, Level oder YYYY-MM-DD)", "", max_len=64), F("position", "int", "Reihenfolge", 0, min=0, max=1000),
], order=["position", "id"], columns=["emoji", "name", "auto_rule", "rule_value"]))

register(Collection("custom_commands", m.CustomCommand, "Custom Commands", [
    F("name", "str", "Name (ohne /)", "", max_len=32, placeholder="socials"), F("description", "str", "Beschreibung", "Custom Command", max_len=100),
    F("content", "text", "Antwort", "", max_len=4000, help="Platzhalter: {user} {username} {server} {membercount} {channel} · Rollen-Mentions: <@&ID>"),
    F("use_embed", "bool", "Als Embed", True), F("embed_title", "str", "Embed-Titel", "", max_len=256), F("embed_color", "color_opt", "Embed-Farbe", None),
    F("image_url", "url", "Bild-URL", ""), F("thumbnail_url", "url", "Thumbnail-URL", ""),
    F("buttons", "objlist", "Link-Buttons", [], fields=[F("label", "str", "Label", "", max_len=80), F("url", "url", "URL", ""), F("emoji", "str", "Emoji", "", max_len=64)]),
    F("ephemeral", "bool", "Nur für Ausführenden sichtbar", False), F("allowed_role_ids", "roles", "Nur für Rollen (leer = alle)", []),
    F("enabled", "bool", "Aktiv", True),
], order=["name"], columns=["name", "description", "uses", "enabled"], readonly_extra=["uses"]))

register(Collection("autoresponders", m.AutoResponder, "Autoresponder", [
    F("trigger", "str", "Trigger", "", max_len=200),
    F("match_type", "select", "Erkennung", "word", options=[("word", "Ganzes Wort"), ("contains", "Enthält"), ("exact", "Exakt"), ("startswith", "Beginnt mit"), ("regex", "Regex")]),
    F("response", "text", "Antwort", "", max_len=2000, help="Platzhalter: {user} {username} {server} {channel}"),
    F("use_embed", "bool", "Als Embed", False), F("reply", "bool", "Als Antwort (Reply)", True), F("delete_trigger", "bool", "Trigger-Nachricht löschen", False),
    F("cooldown_seconds", "int", "Cooldown pro Channel (Sek.)", 30, min=0, max=86400), F("channel_ids", "channels", "Nur in Channels (leer = alle)", []),
    F("enabled", "bool", "Aktiv", True),
], columns=["trigger", "match_type", "uses", "enabled"], readonly_extra=["uses"]))

register(Collection("streamers", m.Streamer, "Streamer", [
    F("platform", "select", "Plattform", "twitch", options=[("twitch", "Twitch"), ("youtube", "YouTube"), ("kick", "Kick")]),
    F("channel", "str", "Kanal (Login / Slug / YouTube-Channel-ID oder @Handle)", "", max_len=100),
    F("announce_channel_id", "channel", "Ankündigungs-Channel (leer = Standard)", None), F("ping_role_id", "role", "Rolle pingen (leer = Standard)", None),
    F("message_template", "text", "Eigene Live-Nachricht (leer = Standard)", "", max_len=1500),
    F("discord_user_id", "user", "Discord-User des Streamers (für Live-Rolle)", None), F("live_role_id", "role", "Rolle während Live", None),
    F("rename_channel_id", "anychannel", "Channel umbenennen", None), F("rename_live", "str", "Name wenn live", "🔴 LIVE: {title}", max_len=100),
    F("rename_offline", "str", "Name wenn offline", "⚫ Offline", max_len=100),
    F("notify_videos", "bool", "Neue Videos melden (YouTube)", True), F("notify_shorts", "bool", "Neue Shorts melden (YouTube)", False),
    F("enabled", "bool", "Aktiv", True),
], columns=["platform", "display_name", "channel", "is_live", "last_checked", "last_error", "enabled"],
    actions=[{"key": "check", "label": "Jetzt prüfen", "icon": "refresh"}],
    readonly_extra=["display_name", "avatar_url", "is_live", "last_checked", "last_error", "followers", "subscribers", "total_streams"]))

register(Collection("events", m.ServerEvent, "Events", [
    F("name", "str", "Name", "", max_len=100), F("description", "text", "Beschreibung", "", max_len=3000),
    F("starts_at", "datetime", "Start", None), F("channel_id", "channel", "Channel (leer = Standard)", None),
    F("image_url", "url", "Bild-URL", ""), F("reward_coins", "int", "Coins für Gewinner", 0, min=0, max=10_000_000),
    F("reward_xp", "int", "XP für Gewinner", 0, min=0, max=10_000_000), F("reward_text", "str", "Belohnung (Text)", "", max_len=200),
], order=["starts_at"], columns=["name", "starts_at", "status"], readonly_extra=["status", "message_id"],
    actions=[{"key": "publish", "label": "Veröffentlichen", "icon": "send"}, {"key": "cancel", "label": "Absagen", "icon": "x"}]))
