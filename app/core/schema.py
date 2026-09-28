"""Deklarative Modul-Definitionen.

Jedes Modul beschreibt hier seine Einstellungen. Daraus entstehen automatisch:
  • die Feature-Toggles, • die Settings-Formulare im Dashboard, • die serverseitige Validierung.
Keine Discord-IDs im Code: alles kommt aus diesen (per Dashboard gepflegten) Einstellungen.
"""
from __future__ import annotations

import re
from dataclasses import asdict, dataclass, field
from typing import Any, Iterable

HEX_RE = re.compile(r"^#[0-9a-fA-F]{6}$")
NAME_TYPES_SINGLE = {"channel", "voice", "category", "role", "badge", "anychannel", "user"}
URL_RE = re.compile(r"^https?://[^\s<>\"']{3,400}$")


@dataclass
class F:
    key: str
    type: str
    label: str
    default: Any = None
    help: str = ""
    options: list[tuple[str, str]] | None = None
    min: float | None = None
    max: float | None = None
    max_len: int = 2000
    group: str = ""
    fields: list["F"] | None = None  # für objlist
    placeholder: str = ""
    advanced: bool = False  # im Dashboard hinter „Mehr Optionen“ (gesetzt über BASIC unten)

    def to_json(self) -> dict[str, Any]:
        d = asdict(self)
        if self.fields:
            d["fields"] = [f.to_json() for f in self.fields]
        if self.options:
            d["options"] = [{"value": v, "label": l} for v, l in self.options]
        return d


@dataclass
class ModuleSpec:
    key: str
    name: str
    icon: str
    description: str
    fields: list[F] = field(default_factory=list)
    toggleable: bool = True
    default_enabled: bool = True
    category: str = "community"

    def defaults(self) -> dict[str, Any]:
        return {f.key: _copy(f.default) for f in self.fields}

    def to_json(self) -> dict[str, Any]:
        return {
            "key": self.key, "name": self.name, "icon": self.icon, "description": self.description,
            "toggleable": self.toggleable, "category": self.category,
            "fields": [f.to_json() for f in self.fields],
        }


def _copy(v: Any) -> Any:
    if isinstance(v, list):
        return list(v)
    if isinstance(v, dict):
        return dict(v)
    return v


ACTIONS = [
    ("delete", "Nachricht löschen"), ("warn", "Verwarnen"), ("timeout", "Timeout"), ("kick", "Kick"),
    ("ban", "Ban"), ("lock", "Channel sperren"), ("remove_role", "Rolle entfernen"), ("log", "Nur Mod-Log"),
]


def _filter(key: str, label: str, extra: list[F], default_on: bool = True, actions: list[str] | None = None) -> list[F]:
    g = label
    return [
        F(f"{key}_enabled", "bool", f"{label} erkennen", default_on, group=g),
        *[_with_group(f, g) for f in extra],
        F(f"{key}_actions", "multiselect", "Aktionen", actions or ["delete", "log"], options=ACTIONS, group=g),
        F(f"{key}_timeout", "int", "Timeout-Dauer (Minuten)", 10, min=1, max=40320, group=g),
    ]


def _with_group(f: F, g: str) -> F:
    f.group = g
    return f


LANG = [("de", "Deutsch"), ("en", "English")]
EVENTS_LOG = [
    ("message_delete", "Nachricht gelöscht"), ("message_edit", "Nachricht bearbeitet"), ("bulk_delete", "Massenlöschung"),
    ("member_join", "Join"), ("member_leave", "Leave"), ("member_ban", "Ban"), ("member_unban", "Unban"),
    ("member_kick", "Kick"), ("member_timeout", "Timeout"), ("nickname", "Nickname-Änderung"),
    ("role_change", "Rollen eines Users"), ("role_update", "Rollen erstellt/bearbeitet/gelöscht"),
    ("channel_update", "Channels erstellt/bearbeitet/gelöscht"), ("voice", "Voice Join/Leave/Move"),
    ("mod_action", "Moderationsaktionen"), ("ticket", "Tickets"), ("giveaway", "Giveaways"),
]
NOTIFY_EVENTS = [
    ("video", "📺 Video veröffentlicht"), ("short", "📱 Short veröffentlicht"), ("giveaway_start", "🎁 Giveaway gestartet"),
    ("giveaway_end", "🏆 Giveaway beendet"), ("event_start", "🎉 Event gestartet"), ("boost", "💜 Server Boost"),
    ("member_join", "👋 User joined"), ("member_leave", "🚪 User left"),
]
NOTIFY_DEFAULTS = {
    "video": "📺 **{streamer}** hat ein neues Video veröffentlicht!\n**{title}**\n{url}",
    "short": "📱 Neuer Short von **{streamer}**: **{title}**\n{url}",
    "giveaway_start": "🎁 Neues Giveaway: **{prize}** – endet {ends}!\n{url}",
    "giveaway_end": "🏆 Giveaway **{prize}** beendet – Gewinner: {winners}",
    "event_start": "🎉 **{event}** startet jetzt!",
    "boost": "💜 {user} hat den Server geboostet!",
    "member_join": "👋 {user} ist beigetreten (Account erstellt {created}).",
    "member_leave": "🚪 **{username}** hat den Server verlassen.",
}


def _notify_fields() -> list[F]:
    out: list[F] = []
    for key, label in NOTIFY_EVENTS:
        out += [
            F(f"{key}_enabled", "bool", "Aktiv", key in ("video", "short", "giveaway_start", "giveaway_end", "event_start"), group=label),
            F(f"{key}_channel", "channel", "Channel", None, group=label),
            F(f"{key}_role", "role", "Rolle pingen", None, group=label),
            F(f"{key}_message", "text", "Nachricht", NOTIFY_DEFAULTS[key], max_len=1500, group=label),
        ]
    return out


MODULES: list[ModuleSpec] = [
    ModuleSpec("general", "Allgemein", "⚙️", "Sprache, Farben, Zugriff aufs Dashboard", toggleable=False, category="core", fields=[
        F("language", "select", "Sprache des Bots", "de", options=LANG),
        F("timezone", "str", "Zeitzone (IANA)", "Europe/Berlin", max_len=50, help="z. B. Europe/Berlin – für Geburtstage & Daily-Reset"),
        F("color_primary", "color", "Akzentfarbe", "#c8a45d", group="Design"),
        F("color_success", "color", "Erfolg", "#5fb37a", group="Design"),
        F("color_error", "color", "Fehler", "#d25a5a", group="Design"),
        F("color_warning", "color", "Warnung", "#d9a441", group="Design"),
        F("color_info", "color", "Info", "#7a8ba8", group="Design"),
        F("footer_text", "str", "Embed-Footer", "", max_len=100, group="Design", placeholder="Leer = Servername"),
        F("use_server_icon", "bool", "Server-Logo in Embeds nutzen", True, group="Design"),
        F("admin_roles", "roles", "Dashboard-Admin-Rollen", [], group="Zugriff", help="Zusätzlich zu Usern mit 'Server verwalten'"),
        F("staff_roles", "roles", "Staff-Rollen", [], group="Zugriff", help="Zugriff auf Moderation, Tickets, Bewerbungen im Dashboard"),
        F("announcement_channel", "channel", "Channel für Bot-Ankündigungen", None, group="Zugriff"),
    ]),
    ModuleSpec("moderation", "Moderation", "🛡️", "Cases, Verwarnungen, Timeouts, Bans", category="moderation", fields=[
        F("log_channel", "channel", "Mod-Log-Channel", None),
        F("dm_users", "bool", "User per DM über Maßnahmen informieren", True),
        F("require_reason", "bool", "Grund verpflichtend", False),
        F("default_timeout", "int", "Standard-Timeout (Minuten)", 30, min=1, max=40320),
        F("appeal_info", "str", "Hinweis für Einsprüche (in DMs)", "", max_len=300),
        F("warn_escalation", "objlist", "Automatische Eskalation bei Verwarnungen", [
            {"count": 3, "action": "timeout", "minutes": 60}, {"count": 5, "action": "kick", "minutes": 0},
        ], fields=[
            F("count", "int", "Ab Verwarnungen", 3, min=1, max=100),
            F("action", "select", "Aktion", "timeout", options=[("timeout", "Timeout"), ("kick", "Kick"), ("ban", "Ban")]),
            F("minutes", "int", "Dauer (Min., nur Timeout)", 60, min=0, max=40320),
        ]),
        F("reason_templates", "strlist", "Grund-Vorlagen (Vorschläge bei /warn, /timeout, /kick, /ban)", [
            "Spam", "Beleidigung", "Werbung ohne Erlaubnis", "NSFW-Inhalte", "Provokation / Trolling", "Drama in den Chat getragen",
            "Channel-Missbrauch", "Umgehung einer Strafe"], group="Mod-Tools"),
        F("duty_role", "role", "„Mod im Dienst“-Rolle (/mod dienst, wird bei Mod-Ruf & Reports gepingt)", None, group="Mod-Tools"),
        F("report_channel", "channel", "Channel für Reports (leer = Mod-Log)", None, group="Mod-Tools"),
        F("report_ping", "bool", "Bei Reports die Dienst-Rolle pingen", True, group="Mod-Tools"),
        F("modcall_channel", "channel", "Channel für /modruf (leer = Mod-Log)", None, group="Mod-Tools"),
        F("modcall_cooldown", "int", "/modruf Cooldown pro User (Minuten)", 10, min=1, max=1440, group="Mod-Tools"),
        F("watch_channel", "channel", "Watch-Channel (Nachrichten beobachteter User, leer = Mod-Log)", None, group="Mod-Tools"),
        F("quarantine_role", "role", "Quarantäne-Rolle (leer = wird automatisch erstellt)", None, group="Mod-Tools"),
        F("penalties", "objlist", "Strafenkatalog (/strafe)", [
            {"name": "Spam", "action": "timeout", "minutes": 10, "reason": "Spam"},
            {"name": "Beleidigung", "action": "timeout", "minutes": 60, "reason": "Beleidigung anderer Mitglieder"},
            {"name": "Werbung", "action": "warn", "minutes": 0, "reason": "Werbung ohne Erlaubnis"},
            {"name": "Hate / Rassismus", "action": "ban", "minutes": 0, "reason": "Hate / Rassismus – null Toleranz"},
            {"name": "NSFW", "action": "kick", "minutes": 0, "reason": "NSFW-Inhalte"},
            {"name": "Stream-Drama", "action": "warn", "minutes": 0, "reason": "Drama aus dem Stream in den Discord getragen"},
        ], help="Gleiches Vergehen = gleiche Strafe. Minuten nur bei Timeout bzw. temporärem Ban (0 = dauerhaft).", group="Strafenkatalog", fields=[
            F("name", "str", "Vergehen", "", max_len=80),
            F("action", "select", "Strafe", "warn", options=[("warn", "Verwarnung"), ("timeout", "Timeout"), ("kick", "Kick"), ("ban", "Ban")]),
            F("minutes", "int", "Minuten", 10, min=0, max=525600),
            F("reason", "str", "Grund-Text", "", max_len=300)]),
    ]),
    ModuleSpec("automod", "Automod", "🤖", "Spam, Links, Scam, Bad Words und mehr", category="moderation", fields=[
        F("exempt_roles", "roles", "Ausgenommene Rollen", [], group="Whitelist"),
        F("exempt_channels", "channels", "Ausgenommene Channels", [], group="Whitelist"),
        F("exempt_users", "users", "Ausgenommene User", [], group="Whitelist"),
        F("allowed_domains", "strlist", "Erlaubte Domains", ["twitch.tv", "youtube.com", "youtu.be", "tenor.com", "giphy.com"], group="Whitelist"),
        F("remove_role", "role", "Rolle für Aktion 'Rolle entfernen'", None, group="Whitelist"),
        F("lock_minutes", "int", "Dauer Channel-Sperre (Minuten)", 5, min=1, max=1440, group="Whitelist"),
        *_filter("spam", "Spam", [F("spam_messages", "int", "Max. Nachrichten", 6, min=2, max=50), F("spam_seconds", "int", "in Sekunden", 5, min=1, max=120)], actions=["delete", "timeout", "log"]),
        *_filter("flood", "Flood", [F("flood_lines", "int", "Max. Zeilen pro Nachricht", 15, min=3, max=100), F("flood_chars", "int", "Max. gleiche Zeichen in Folge", 20, min=5, max=500)]),
        *_filter("caps", "Caps", [F("caps_percent", "int", "Max. Großbuchstaben (%)", 75, min=10, max=100), F("caps_min_length", "int", "Ab Länge", 12, min=4, max=200)], default_on=False),
        *_filter("duplicates", "Wiederholte Nachrichten", [F("duplicates_count", "int", "Gleiche Nachricht max.", 3, min=2, max=20), F("duplicates_seconds", "int", "in Sekunden", 30, min=5, max=600)]),
        *_filter("invites", "Discord-Einladungen", [F("invites_allow_own", "bool", "Einladungen zu diesem Server erlauben", True)], actions=["delete", "warn", "log"]),
        *_filter("links", "Unerlaubte Links", [F("links_block_all", "bool", "Alle nicht erlaubten Domains blockieren", False), F("links_blocked_domains", "strlist", "Gesperrte Domains", [])], default_on=False),
        *_filter("scam", "Scam- & verdächtige Links", [], actions=["delete", "timeout", "log"]),
        *_filter("badwords", "Bad Words", [F("badwords_list", "strlist", "Wörter (* = Wildcard)", [])], default_on=False, actions=["delete", "warn", "log"]),
        *_filter("mentions", "Erwähnungs-Spam", [F("mentions_max", "int", "Max. Erwähnungen pro Nachricht", 6, min=2, max=50)], actions=["delete", "timeout", "log"]),
        *_filter("mass_mentions", "Mass Mentions (@everyone/@here)", [], actions=["delete", "warn", "log"]),
        *_filter("emoji", "Emoji-Spam", [F("emoji_max", "int", "Max. Emojis pro Nachricht", 12, min=3, max=100)], default_on=False),
        *_filter("new_accounts", "Account-Spam (neue Accounts)", [F("new_accounts_days", "int", "Account jünger als (Tage)", 3, min=1, max=90), F("new_accounts_block_links", "bool", "Links/Einladungen von neuen Accounts blockieren", True)], default_on=False),
    ]),
    ModuleSpec("logging", "Logging", "📝", "Nachrichten-, Member-, Rollen-, Voice- und Mod-Logs", category="moderation", fields=[
        F("default_channel", "channel", "Standard-Log-Channel", None, group="Channels"),
        F("message_channel", "channel", "Nachrichten-Logs", None, group="Channels"),
        F("member_channel", "channel", "Member-Logs", None, group="Channels"),
        F("server_channel", "channel", "Server-Logs (Rollen/Channels)", None, group="Channels"),
        F("voice_channel", "channel", "Voice-Logs", None, group="Channels"),
        F("events", "multiselect", "Geloggte Ereignisse", [k for k, _ in EVENTS_LOG], options=EVENTS_LOG, group="Ereignisse"),
        F("ignored_channels", "channels", "Ignorierte Channels", [], group="Ereignisse"),
        F("ignore_bots", "bool", "Bots ignorieren", True, group="Ereignisse"),
    ]),
    ModuleSpec("welcome", "Welcome & Leave", "👋", "Begrüßung, Auto-Rollen, Auto-DM, Abschied", fields=[
        F("channel", "channel", "Willkommens-Channel", None, group="Willkommen"),
        F("message", "text", "Text", "Willkommen {user} auf **{server}**! Du bist Mitglied **#{count}** 🎉", group="Willkommen",
          help="Platzhalter: {user} {username} {server} {count}"),
        F("use_embed", "bool", "Als Embed senden", True, group="Willkommen"),
        F("embed_title", "str", "Embed-Titel", "Willkommen!", max_len=256, group="Willkommen"),
        F("image_url", "url", "Bild-URL", "", group="Willkommen"),
        F("auto_roles", "roles", "Auto-Rollen für neue Mitglieder", [], group="Auto-Rollen"),
        F("bot_roles", "roles", "Auto-Rollen für neue Bots", [], group="Auto-Rollen"),
        F("dm_enabled", "bool", "Auto-DM senden", False, group="Auto-DM"),
        F("dm_message", "text", "DM-Text", "Hey {username}, willkommen auf {server}! Schau gerne in die Regeln 💜", group="Auto-DM"),
        F("leave_enabled", "bool", "Abschiedsnachricht", False, group="Leave"),
        F("leave_channel", "channel", "Leave-Channel", None, group="Leave"),
        F("leave_message", "text", "Leave-Text", "**{username}** hat den Server verlassen. 👋", group="Leave"),
    ]),
    ModuleSpec("boost", "Server Boosts", "💜", "Danke-Nachricht und Belohnungen für Booster", fields=[
        F("channel", "channel", "Channel", None),
        F("message", "text", "Nachricht", "💜 Danke {user} für den Server Boost!"),
        F("booster_role", "role", "Zusätzliche Booster-Rolle", None),
        F("reward_coins", "int", "Coins-Belohnung", 500, min=0, max=10_000_000),
        F("reward_xp", "int", "XP-Belohnung", 250, min=0, max=10_000_000),
        F("reward_badge", "badge", "Badge vergeben", None),
    ]),
    ModuleSpec("verification", "Verifizierung", "✅", "Button oder Captcha, Account-Age-Check", category="security", default_enabled=False, fields=[
        F("channel", "channel", "Verifizierungs-Channel", None),
        F("verified_role", "role", "Verified-Rolle", None),
        F("unverified_role", "role", "Unverified-Rolle (beim Join)", None),
        F("mode", "select", "Modus", "button", options=[("button", "Button"), ("captcha", "Captcha")]),
        F("min_account_days", "int", "Mindestalter Account (Tage, 0 = aus)", 0, min=0, max=365),
        F("kick_young", "bool", "Zu junge Accounts kicken statt ablehnen", False),
        F("panel_title", "str", "Panel-Titel", "Verifizierung", max_len=256),
        F("panel_text", "text", "Panel-Text", "Klicke auf **Verifizieren**, um Zugriff auf den Server zu erhalten."),
    ]),
    ModuleSpec("antiraid", "Anti-Raid", "🚨", "Erkennt Join-Wellen und reagiert automatisch", category="security", fields=[
        F("join_threshold", "int", "Joins", 30, min=3, max=500),
        F("join_seconds", "int", "innerhalb Sekunden", 20, min=5, max=600),
        F("actions", "multiselect", "Automatische Aktionen", ["alert", "verification", "pause_invites"], options=[
            ("alert", "Staff alarmieren"), ("verification", "Verifizierung erzwingen (Level hoch)"),
            ("pause_invites", "Einladungen pausieren (Join-Limit)"), ("lock", "Channels sperren"),
            ("timeout_new", "Neue User timeouten"), ("kick_new", "Neue User kicken"),
        ]),
        F("lock_channels", "channels", "Zu sperrende Channels", []),
        F("lockdown_minutes", "int", "Dauer Lockdown (Minuten)", 15, min=1, max=1440),
        F("alert_channel", "channel", "Alarm-Channel", None),
        F("alert_role", "role", "Alarm-Rolle pingen", None),
    ]),
    ModuleSpec("roles", "Rollen", "🎭", "Self-Roles, Rollen-Menüs, Sync-Regeln", fields=[
        F("sync_rules", "objlist", "Rollen-Synchronisation", [], help="Wer die Quell-Rolle hat, bekommt automatisch die Ziel-Rolle", fields=[
            F("source", "role", "Quell-Rolle", None), F("target", "role", "Ziel-Rolle", None),
            F("remove", "bool", "Ziel entfernen, wenn Quelle fehlt", True),
        ]),
    ]),
    ModuleSpec("tickets", "Tickets", "🎫", "Ticket-Panel, Kategorien, Transcripts", category="support", fields=[
        F("panel_channel", "channel", "Panel-Channel", None),
        F("panel_title", "str", "Panel-Titel", "Support", max_len=256),
        F("panel_text", "text", "Panel-Text", "Klick unten und wähle dein Thema."),
        F("panel_button", "str", "Text auf dem Button", "Ticket öffnen", max_len=80),
        F("panel_image", "url", "Bild im Panel (optional)", ""),
        F("category", "category", "Standard-Kategorie für Tickets", None),
        F("staff_roles", "roles", "Support-Rollen", []),
        F("transcript_channel", "channel", "Transcript-Channel", None),
        F("claimed_category", "category", "Kategorie für übernommene Tickets (leer = wird automatisch erstellt)", None),
        F("closed_category", "category", "Kategorie für geschlossene Tickets (leer = wird automatisch erstellt)", None),
        F("dm_transcript", "bool", "Transcript an User senden", True),
        F("max_open", "int", "Max. offene Tickets pro User", 2, min=1, max=10),
        F("name_format", "str", "Channel-Name", "{category}-{username}-{number}", max_len=90, help="z. B. entbannungsantrag-max-0012 · Platzhalter: {category} {username} {number}"),
        F("ping_staff", "bool", "Support-Rollen beim Öffnen pingen", True),
    ]),
    ModuleSpec("applications", "Bewerbungen", "📋", "Bewerbungsformulare mit Review im Dashboard", category="support", fields=[
        F("panel_channel", "channel", "Panel-Channel", None),
        F("panel_title", "str", "Panel-Titel", "📋 Bewirb dich im Team", max_len=256),
        F("panel_text", "text", "Panel-Text", "Du möchtest Teil unseres Teams werden? Klicke auf **Bewerben** und fülle das Formular aus."),
        F("review_channel", "channel", "Review-Channel (Staff)", None),
        F("reviewer_roles", "roles", "Reviewer-Rollen", []),
        F("positions", "strlist", "Positionen", ["Moderator", "Supporter", "Event-Team", "Editor"]),
        F("accept_role", "role", "Rolle bei Annahme", None),
        F("cooldown_days", "int", "Wartezeit zwischen Bewerbungen (Tage)", 14, min=0, max=365),
        F("accept_message", "text", "DM bei Annahme", "🎉 Glückwunsch! Deine Bewerbung als **{position}** auf **{server}** wurde angenommen."),
        F("reject_message", "text", "DM bei Ablehnung", "Deine Bewerbung als **{position}** auf **{server}** wurde leider abgelehnt."),
        F("questions", "objlist", "Fragen", [
            {"label": "Name", "style": "short", "required": True, "placeholder": "Wie heißt du?"},
            {"label": "Alter", "style": "short", "required": True, "placeholder": "z. B. 18"},
            {"label": "Discord", "style": "short", "required": True, "placeholder": "Dein Discord-Name"},
            {"label": "Erfahrung", "style": "paragraph", "required": True, "placeholder": "Welche Erfahrung bringst du mit?"},
            {"label": "Warum möchtest du ins Team?", "style": "paragraph", "required": True, "placeholder": ""},
            {"label": "Verfügbare Zeit", "style": "short", "required": True, "placeholder": "z. B. 10h/Woche"},
        ], fields=[
            F("label", "str", "Frage", "", max_len=45),
            F("style", "select", "Typ", "short", options=[("short", "Kurz"), ("paragraph", "Absatz")]),
            F("required", "bool", "Pflicht", True),
            F("placeholder", "str", "Platzhalter", "", max_len=100),
        ]),
    ]),
    ModuleSpec("giveaways", "Giveaways", "🎁", "Giveaways mit Bedingungen und Bonus-Entries", fields=[
        F("default_channel", "channel", "Standard-Channel", None),
        F("manager_roles", "roles", "Giveaway-Manager-Rollen", []),
        F("dm_winners", "bool", "Gewinner per DM benachrichtigen", True),
        F("ping_role", "role", "Rolle beim Start pingen", None),
        F("role_entries", "objlist", "Bonus-Entries durch Rollen", [], group="Bonus-Entries (Standard)", fields=[
            F("role", "role", "Rolle", None), F("entries", "int", "Zusätzliche Entries", 1, min=1, max=100),
        ]),
        F("invite_entries", "int", "Entries pro Einladung", 0, min=0, max=100, group="Bonus-Entries (Standard)"),
        F("activity_messages", "int", "Aktivität: ab Nachrichten", 0, min=0, max=1_000_000, group="Bonus-Entries (Standard)"),
        F("activity_entries", "int", "Aktivität: Entries", 0, min=0, max=100, group="Bonus-Entries (Standard)"),
        F("early_minutes", "int", "Early Join: in den ersten Minuten", 0, min=0, max=10080, group="Bonus-Entries (Standard)"),
        F("early_entries", "int", "Early Join: Entries", 0, min=0, max=100, group="Bonus-Entries (Standard)"),
        F("winner_coins", "int", "Coins für Gewinner", 0, min=0, max=10_000_000),
    ]),
    ModuleSpec("levels", "Levelsystem", "⭐", "XP für Nachrichten, Voice, Reaktionen, Streams", fields=[
        F("xp_min", "int", "XP pro Nachricht (min)", 15, min=0, max=1000, group="XP"),
        F("xp_max", "int", "XP pro Nachricht (max)", 25, min=0, max=1000, group="XP"),
        F("cooldown", "int", "Cooldown (Sekunden, Anti-Spam)", 60, min=0, max=3600, group="XP"),
        F("voice_xp", "int", "XP pro Voice-Minute", 5, min=0, max=1000, group="XP"),
        F("voice_needs_others", "bool", "Voice-XP nur mit anderen (Anti-AFK)", True, group="XP"),
        F("reaction_xp", "int", "XP pro Reaktion", 2, min=0, max=100, group="XP"),
        F("stream_xp", "int", "XP pro Stream-Check-in", 50, min=0, max=10000, group="XP"),
        F("multipliers", "objlist", "XP-Multiplikatoren", [], group="XP", fields=[
            F("role", "role", "Rolle", None), F("multiplier", "float", "Faktor", 1.5, min=0, max=10),
        ]),
        F("no_xp_channels", "channels", "Keine XP in Channels", [], group="XP"),
        F("no_xp_roles", "roles", "Keine XP für Rollen", [], group="XP"),
        F("announce", "select", "Level-Up-Nachricht", "off", options=[("channel", "Im selben Channel"), ("custom", "In festem Channel"), ("dm", "Per DM"), ("off", "Aus")], group="Level-Up"),
        F("announce_channel", "channel", "Fester Level-Up-Channel", None, group="Level-Up"),
        F("announce_message", "text", "Nachricht", "🎉 {user} du bist jetzt **Level {level}**!", group="Level-Up"),
        F("stack_rewards", "bool", "Belohnungsrollen behalten (stapeln)", True, group="Level-Up"),
    ]),
    ModuleSpec("economy", "Economy", "💰", "Community-Währung, Shop, Daily/Weekly", fields=[
        F("currency_name", "str", "Währungsname", "Coins", max_len=30),
        F("currency_emoji", "str", "Währungs-Emoji", "🪙", max_len=64),
        F("starting_balance", "int", "Startguthaben", 100, min=0, max=10_000_000),
        F("message_min", "int", "Coins pro Nachricht (min)", 1, min=0, max=10000, group="Verdienen"),
        F("message_max", "int", "Coins pro Nachricht (max)", 5, min=0, max=10000, group="Verdienen"),
        F("message_cooldown", "int", "Cooldown (Sekunden)", 60, min=0, max=3600, group="Verdienen"),
        F("voice_per_minute", "int", "Coins pro Voice-Minute", 1, min=0, max=1000, group="Verdienen"),
        F("stream_checkin", "int", "Coins pro Stream-Check-in", 100, min=0, max=100000, group="Verdienen"),
        F("work_min", "int", "/work min", 50, min=0, max=100000, group="Verdienen"),
        F("work_max", "int", "/work max", 200, min=0, max=100000, group="Verdienen"),
        F("work_cooldown", "int", "/work Cooldown (Minuten)", 60, min=1, max=10080, group="Verdienen"),
        F("daily_amount", "int", "Daily-Belohnung", 250, min=0, max=1_000_000, group="Daily & Weekly"),
        F("daily_streak_bonus", "int", "Bonus pro Streak-Tag", 25, min=0, max=100000, group="Daily & Weekly"),
        F("streak_7_bonus", "int", "7-Tage-Streak-Bonus", 1000, min=0, max=1_000_000, group="Daily & Weekly"),
        F("streak_30_bonus", "int", "30-Tage-Streak-Bonus", 7500, min=0, max=10_000_000, group="Daily & Weekly"),
        F("weekly_amount", "int", "Weekly-Belohnung", 2000, min=0, max=10_000_000, group="Daily & Weekly"),
        F("max_pay", "int", "Max. Betrag bei /pay (0 = unbegrenzt)", 0, min=0, max=100_000_000),
        F("rob_enabled", "bool", "/rob erlauben", True, group="Hood-Economy"),
        F("rob_cooldown", "int", "/rob Cooldown (Minuten)", 120, min=1, max=10080, group="Hood-Economy"),
        F("rob_success", "int", "/rob Erfolgschance (%)", 45, min=1, max=95, group="Hood-Economy"),
        F("rob_max_percent", "int", "/rob max. Beute (% vom Opfer)", 20, min=1, max=100, group="Hood-Economy"),
        F("rob_min_victim", "int", "Opfer braucht mindestens", 200, min=0, max=1_000_000, group="Hood-Economy"),
        F("crime_enabled", "bool", "/crime erlauben", True, group="Hood-Economy"),
        F("crime_cooldown", "int", "/crime Cooldown (Minuten)", 30, min=1, max=10080, group="Hood-Economy"),
        F("crime_min", "int", "/crime Gewinn min", 150, min=0, max=1_000_000, group="Hood-Economy"),
        F("crime_max", "int", "/crime Gewinn max", 600, min=0, max=1_000_000, group="Hood-Economy"),
        F("crime_fail", "int", "/crime Risiko erwischt zu werden (%)", 35, min=0, max=95, group="Hood-Economy"),
        F("gambling_enabled", "bool", "/slots und /bet erlauben", True, group="Hood-Economy"),
        F("max_bet", "int", "Max. Einsatz (0 = unbegrenzt)", 10000, min=0, max=100_000_000, group="Hood-Economy"),
    ]),
    ModuleSpec("quests", "Quests", "🗺️", "Tägliche und wöchentliche Community-Aufgaben", fields=[
        F("announce_channel", "channel", "Channel für abgeschlossene Quests", None),
        F("dm_on_complete", "bool", "DM bei Abschluss", True),
    ]),
    ModuleSpec("music", "Musik", "🎵", "Queue, Loop, Shuffle, Skip-Vote, Panel", fields=[
        F("dj_role", "role", "DJ-Rolle (darf alles)", None),
        F("default_volume", "int", "Standard-Lautstärke (%)", 60, min=1, max=150),
        F("max_queue", "int", "Max. Songs in Queue", 200, min=1, max=1000),
        F("max_minutes", "int", "Max. Songlänge (Minuten, 0 = egal)", 0, min=0, max=600),
        F("allow_playlists", "bool", "Playlists erlauben", True),
        F("skip_vote_percent", "int", "Skip-Vote benötigt (% der Zuhörer)", 50, min=1, max=100),
        F("idle_minutes", "int", "Automatisch verlassen nach Leerlauf (Minuten)", 3, min=1, max=60),
        F("allowed_channels", "channels", "Nur in diesen Text-Channels (leer = überall)", []),
    ]),
    ModuleSpec("streamer", "Streamer", "📡", "Twitch, YouTube, Kick – Live-Benachrichtigungen & Stats", category="streamer", fields=[
        F("style", "select", "Stil der Live-Posts", "hood", options=[("hood", "Hood – Straße, laut, Gang"), ("classic", "Classic"), ("clean", "Clean – minimal")],
          group="Live-Benachrichtigung"),
        F("default_channel", "channel", "Standard-Channel für Live-Posts", None, group="Live-Benachrichtigung"),
        F("default_role", "role", "Standard-Rolle für @Stream-Notification", None, group="Live-Benachrichtigung"),
        F("message", "text", "Eigene Live-Nachricht (leer = Text passend zum Stil)", "",
          help="Platzhalter: {streamer} {title} {game} {url} {role} {platform}", group="Live-Benachrichtigung"),
        F("update_embed", "bool", "Embed live aktualisieren (Zuschauer, Titel)", True, group="Live-Benachrichtigung"),
        F("show_viewers", "bool", "Zuschauerzahl anzeigen", True, group="Live-Benachrichtigung"),
        F("offline_edit", "bool", "Embed nach Stream-Ende zu Zusammenfassung umbauen", True, group="Live-Benachrichtigung"),
        F("checkin_enabled", "bool", "'Ich bin dabei'-Button (XP/Coins/Quests)", True, group="Live-Benachrichtigung"),
        F("media_role", "role", "Media-Rolle (nur diese darf sich mit Twitch verbinden)", None, group="Medias – Twitch verbinden"),
        F("media_panel_channel", "channel", "Channel für das „Mit Twitch verbinden“-Panel", None, group="Medias – Twitch verbinden"),
        F("media_live_role", "role", "Rolle, die ein Media bekommt, solange er live ist", None, group="Medias – Twitch verbinden"),
        F("clips_enabled", "bool", "Neue Twitch-Clips automatisch posten", True, group="Clips & Plan"),
        F("clips_channel", "channel", "Clip-Channel", None, group="Clips & Plan"),
        F("clips_min_views", "int", "Nur Clips ab Aufrufen", 0, min=0, max=100000, group="Clips & Plan"),
        F("golive_roles", "roles", "Wer darf /golive nutzen (leer = Admins)", [], group="Clips & Plan"),
        F("loyalty_roles", "objlist", "Stammzuschauer-Rollen", [
            {"checkins": 5, "role": None}, {"checkins": 25, "role": None}, {"checkins": 100, "role": None}],
          help="Rolle automatisch ab X Stream-Check-ins („Ich bin dabei“)", group="Stammzuschauer", fields=[
            F("checkins", "int", "Ab Check-ins", 5, min=1, max=100000), F("role", "role", "Rolle", None)]),
        F("live_board_channel", "channel", "Live-Board: Nachricht mit allen, die gerade live sind (leer = aus)", None, group="Live-Extras"),
        F("live_event", "bool", "Discord-Event „X ist live“ automatisch erstellen", True, group="Live-Extras"),
        F("game_change", "bool", "Spielwechsel im Stream melden", True, group="Live-Extras"),
        F("records", "bool", "Zuschauer-Rekorde, Stream-Meilensteine & Streaks feiern", True, group="Live-Extras"),
        F("hype_levels", "strlist", "Hype-Meldung ab so vielen Check-ins", ["10", "25", "50", "100"], group="Live-Extras"),
        F("vod_button", "bool", "Nach dem Stream „VOD ansehen“-Button anzeigen", True, group="Live-Extras"),
        F("remind_minutes", "int", "Erinnerung vor geplantem Stream (Minuten, 0 = aus)", 15, min=0, max=240,
          help="Nutzt den Twitch-Streamplan", group="Plan & Erinnerungen"),
        F("remind_ping", "bool", "Bei der Erinnerung die Stream-Rolle pingen", False, group="Plan & Erinnerungen"),
        F("weekly_plan_channel", "channel", "Wochenplan automatisch posten in (leer = aus)", None, group="Plan & Erinnerungen"),
        F("weekly_plan_day", "select", "Wochenplan posten am (ab 10 Uhr)", "0", options=[
            ("0", "Montag"), ("1", "Dienstag"), ("2", "Mittwoch"), ("3", "Donnerstag"), ("4", "Freitag"), ("5", "Samstag"), ("6", "Sonntag")],
          group="Plan & Erinnerungen"),
        F("clip_week", "bool", "Clip der Woche (Sonntag 18 Uhr im Clip-Channel)", True, group="Plan & Erinnerungen"),
        F("bets_enabled", "bool", "Stream-Wetten mit Coins (/wette)", True, group="Stream-Wetten"),
        F("bet_min", "int", "Mindesteinsatz", 10, min=1, max=100_000_000, group="Stream-Wetten"),
        F("bet_max", "int", "Höchsteinsatz", 10000, min=1, max=100_000_000, group="Stream-Wetten"),
        F("media_verified_role", "role", "Rolle nach erfolgreichem Twitch-Verbinden (z. B. Verifizierter Streamer)", None, group="Medias – Twitch verbinden"),
        F("media_weekly_dm", "bool", "Medias montags ihre Wochen-Statistik per DM schicken", True, group="Medias – Extras"),
        F("media_of_month", "bool", "Media des Monats küren (am 1. um 12 Uhr)", True, group="Medias – Extras"),
        F("media_month_channel", "channel", "Channel für Media des Monats (leer = Live-Channel)", None, group="Medias – Extras"),
        F("collab_channel", "channel", "Channel für /collab (leer = aktueller Channel)", None, group="Medias – Extras"),
        F("live_thread", "bool", "Stream-Talk-Thread unter jedem Live-Post", True, group="Live-Extras"),
        F("chat_stats", "bool", "Twitch-Chat-Statistik nach dem Stream (Nachrichten, Top-Chatter, Emotes)", True, group="Twitch-Chat-Wächter"),
        F("chat_watch_words", "strlist", "Warnwörter im Twitch-Chat – Mods bekommen sofort einen Alarm", [],
          help="z. B. Beleidigungen, Namen, Adressen, Links – Groß/Klein egal", group="Twitch-Chat-Wächter"),
        F("chat_watch_channel", "channel", "Alarm-Channel für den Chat-Wächter (leer = Mod-Log)", None, group="Twitch-Chat-Wächter"),
    ]),
    ModuleSpec("clipcontest", "Clip-Contest", "🎬", "Clip-Wettbewerb: TikTok/YouTube/Instagram-Clips einreichen, Aufrufe sammeln, Preise gewinnen",
               category="streamer", fields=[
        F("review_channel", "channel", "Channel, in den eingereichte Clips kommen (Team prüft dort)", None, group="Channels & Team"),
        F("announce_channel", "channel", "Standard-Channel für Ankündigung & Ergebnisse", None, group="Channels & Team"),
        F("staff_roles", "roles", "Wer darf Clips prüfen & Aufrufe bestätigen (leer = Mods)", [], group="Channels & Team"),
        F("ping_role", "role", "Rolle bei Start & Ende pingen", None, group="Channels & Team"),
        F("platforms", "multiselect", "Erlaubte Plattformen", ["tiktok"], options=[
            ("tiktok", "TikTok"), ("youtube", "YouTube Shorts"), ("instagram", "Instagram Reels")], group="Regeln"),
        F("max_per_user", "int", "Max. Clips pro Person", 5, min=1, max=100, group="Regeln"),
        F("rules", "text", "Regeln (erscheinen in der Ankündigung)",
          "• Nur Clips aus den Streams von **{streamer}**\n• Streamer im Video markieren + Hashtag **{hashtag}**\n"
          "• Clip muss **während** des Contests hochgeladen worden sein\n• Gekaufte Views / Bots = Disqualifikation\n"
          "• Aufrufe zählen erst, wenn das Team sie bestätigt hat", max_len=1500, group="Regeln"),
        F("streamer_name", "str", "Name des Streamers (für {streamer})", "Coray", max_len=60, group="Regeln"),
        F("hashtag", "str", "Pflicht-Hashtag (für {hashtag})", "#corayclips", max_len=60, group="Regeln"),
        F("currency", "str", "Währung", "€", max_len=10, group="Geld pro Aufrufe"),
        F("pay_tiers", "objlist", "Auszahlungs-Stufen pro Clip (höchste erreichte Stufe zählt)", [
            {"views": 5000, "amount": 2.0}, {"views": 10000, "amount": 4.0}, {"views": 25000, "amount": 8.0},
            {"views": 50000, "amount": 15.0}, {"views": 100000, "amount": 25.0}, {"views": 250000, "amount": 50.0},
            {"views": 500000, "amount": 80.0}, {"views": 1000000, "amount": 120.0}],
          help="Clip mit 30.000 Aufrufen → Stufe 25.000 → 8 €. Unter der ersten Stufe gibt es nichts.", group="Geld pro Aufrufe", fields=[
            F("views", "int", "Ab Aufrufen", 5000, min=1, max=10_000_000_000), F("amount", "float", "Betrag", 0, min=0, max=1_000_000)]),
    ]),
    ModuleSpec("gangs", "Gangs", "🏴", "Crews gründen, Mitglieder einladen, Gang-Kasse und Rangliste", fields=[
        F("create_cost", "int", "Kosten zum Gründen (Coins)", 5000, min=0, max=100_000_000),
        F("max_members", "int", "Max. Mitglieder pro Gang", 15, min=2, max=200),
        F("announce_channel", "channel", "Channel für Gang-News (leer = aus)", None),
    ]),
    ModuleSpec("notifications", "Benachrichtigungen", "🔔", "Routing für Videos, Shorts, Giveaways, Events, Boosts, Joins", category="streamer", fields=_notify_fields()),
    ModuleSpec("suggestions", "Suggestions", "💡", "Vorschläge mit Voting und Staff-Entscheidungen", fields=[
        F("channel", "channel", "Suggestion-Channel", None),
        F("staff_roles", "roles", "Staff-Rollen", []),
        F("create_thread", "bool", "Diskussions-Thread erstellen", True),
        F("anonymous", "bool", "Autor anonym anzeigen", False),
        F("dm_author", "bool", "Autor bei Statusänderung per DM informieren", True),
        F("panel_text", "text", "Panel-Text", "Hast du eine Idee für den Server? Klicke auf **Vorschlag einreichen**!"),
    ]),
    ModuleSpec("polls", "Umfragen", "📊", "Umfragen mit Live-Ergebnissen", fields=[
        F("creator_roles", "roles", "Wer darf Umfragen erstellen (leer = alle mit 'Nachrichten verwalten')", []),
        F("default_hours", "int", "Standarddauer (Stunden)", 24, min=1, max=720),
    ]),
    ModuleSpec("reminders", "Reminders", "⏰", "Persönliche Erinnerungen per DM", fields=[
        F("max_per_user", "int", "Max. aktive Reminder pro User", 25, min=1, max=100),
    ]),
    ModuleSpec("custom_commands", "Custom Commands", "🧩", "Eigene Slash-Commands aus dem Dashboard", fields=[]),
    ModuleSpec("autoresponder", "Autoresponder", "💬", "Automatische Antworten auf Trigger-Wörter", fields=[
        F("ignore_staff", "bool", "Staff ignorieren", False),
    ]),
    ModuleSpec("starboard", "Starboard", "⭐", "Beliebte Nachrichten hervorheben", default_enabled=False, fields=[
        F("channel", "channel", "Starboard-Channel", None),
        F("emoji", "str", "Emoji", "⭐", max_len=64),
        F("threshold", "int", "Mindestanzahl", 5, min=1, max=100),
        F("self_star", "bool", "Eigene Nachrichten zählen", False),
        F("allowed_roles", "roles", "Nur Reaktionen dieser Rollen zählen (leer = alle)", []),
        F("ignored_channels", "channels", "Ignorierte Channels", []),
    ]),
    ModuleSpec("afk", "AFK", "💤", "AFK-Status mit Auto-Antwort", fields=[
        F("nick_prefix", "bool", "[AFK] vor den Nickname setzen", True),
    ]),
    ModuleSpec("birthday", "Geburtstage", "🎂", "Geburtstagsgrüße (privat gespeichert)", fields=[
        F("channel", "channel", "Channel", None),
        F("message", "text", "Nachricht", "🎂 Happy Birthday {user}! Alles Gute von **{server}** 🎉"),
        F("role", "role", "Geburtstagsrolle (für 24h)", None),
        F("hour", "int", "Uhrzeit der Gratulation (0–23, Server-Zeitzone)", 9, min=0, max=23),
        F("reward_coins", "int", "Coins", 250, min=0, max=10_000_000),
        F("reward_xp", "int", "XP", 100, min=0, max=10_000_000),
    ]),
    ModuleSpec("events", "Events", "🎉", "Server-Events mit Countdown, RSVP und Erinnerungen", fields=[
        F("channel", "channel", "Event-Channel", None),
        F("ping_role", "role", "Rolle pingen", None),
        F("reminders", "strlist", "Erinnerungen (Minuten vorher)", ["1440", "60", "10"]),
        F("dm_participants", "bool", "Teilnehmer per DM erinnern", True),
    ]),
    ModuleSpec("tempvoice", "Temporäre Channels", "🔊", "Join-to-Create Voice-Channels", default_enabled=False, fields=[
        F("hub_channel", "voice", "Join-to-Create-Channel", None),
        F("category", "category", "Kategorie für neue Channels", None),
        F("name_template", "str", "Name", "🔊 {username}'s Room", max_len=90),
        F("default_limit", "int", "Standard-Limit (0 = unbegrenzt)", 0, min=0, max=99),
    ]),
    ModuleSpec("stats_channels", "Stats-Channels", "📈", "Automatische Zähler-Channels", default_enabled=False, fields=[
        F("members_channel", "voice", "Mitglieder-Channel", None), F("members_template", "str", "Vorlage", "👥 Mitglieder: {members}", max_len=90),
        F("online_channel", "voice", "Online-Channel", None), F("online_template", "str", "Vorlage", "🟢 Online: {online}", max_len=90),
        F("boosts_channel", "voice", "Boost-Channel", None), F("boosts_template", "str", "Vorlage", "🚀 Boosts: {boosts}", max_len=90),
        F("stream_channel", "voice", "Stream-Channel", None), F("stream_template", "str", "Vorlage", "🎥 Stream: {stream}", max_len=90),
    ]),
    ModuleSpec("profiles", "Profile & Achievements", "🏆", "Profile, Badges, Achievements", fields=[
        F("og_before", "str", "OG-Member: beigetreten vor (YYYY-MM-DD)", "", max_len=10),
        F("allow_banner", "bool", "User dürfen Profil-Banner setzen", True),
        F("track_games", "bool", "Gespielte Games im Profil anzeigen", True),
        F("achievement_channel", "channel", "Achievement-Ankündigungen (leer = aus)", None),
    ]),
    ModuleSpec("fun", "Fun", "🎲", "8ball, Würfel, Ship, Rate …", fields=[]),
]

# ───────────────────────── Einfach-Modus ─────────────────────────
# Pro Modul die wichtigsten Felder mit einem kurzen Erklärsatz. Alle anderen Felder dieser Module landen im
# Dashboard hinter „Mehr Optionen“. Module, die hier fehlen, sind klein genug und zeigen alles.
BASIC: dict[str, dict[str, str]] = {
    "general": {
        "language": "In welcher Sprache der Bot antwortet.",
        "admin_roles": "Wer mit dieser Rolle darf alles im Dashboard ändern.",
        "staff_roles": "Dein Team: darf moderieren, Tickets bearbeiten usw.",
        "color_primary": "Die Hauptfarbe der Bot-Nachrichten.",
    },
    "streamer": {
        "default_channel": "Hier postet der Bot, wenn du live gehst.",
        "default_role": "Diese Rolle wird beim Live-Post gepingt. Leer = kein Ping.",
        "style": "Wie die Live-Nachricht aussieht.",
        "message": "Eigener Text für den Live-Post. Leer lassen = fertiger Text passend zum Stil.",
        "clips_enabled": "Neue Twitch-Clips automatisch posten.",
        "clips_channel": "Hier landen die Twitch-Clips.",
        "checkin_enabled": "Zuschauer klicken „Ich bin dabei“ und bekommen XP & Coins.",
        "live_thread": "Unter jedem Live-Post ein Thread zum Quatschen.",
    },
    "clipcontest": {
        "review_channel": "Hier landen eingereichte Clips – dein Team klickt Annehmen/Ablehnen. Am besten ein Team-Channel.",
        "announce_channel": "Hier kommen Ankündigung und Ergebnisse hin.",
        "platforms": "Welche Links man einreichen darf.",
        "max_per_user": "Wie viele Clips eine Person einreichen darf.",
        "pay_tiers": "Wie viel Geld ein Clip ab wie vielen Aufrufen bringt. Die höchste erreichte Stufe zählt.",
        "streamer_name": "Dein Name – steht in der Ankündigung.",
        "hashtag": "Hashtag, den alle unter ihren Clip schreiben müssen.",
    },
    "notifications": {
        "video_enabled": "Posten, wenn ein neues YouTube-Video rauskommt.",
        "video_channel": "Hier kommen neue Videos hin.",
        "video_role": "Diese Rolle wird bei neuen Videos gepingt.",
        "short_enabled": "Posten, wenn ein neuer Short rauskommt.",
        "short_channel": "Hier kommen neue Shorts hin.",
        "short_role": "Diese Rolle wird bei neuen Shorts gepingt.",
    },
    "moderation": {
        "log_channel": "Hier sieht dein Team alle Warns, Timeouts und Bans.",
        "dm_users": "Bestrafte User bekommen eine DM mit dem Grund.",
        "default_timeout": "So lange dauert ein Timeout, wenn man keine Zeit angibt.",
    },
    "automod": {
        "spam_enabled": "Löscht, wenn jemand viele Nachrichten schnell hintereinander schickt.",
        "invites_enabled": "Löscht Einladungen zu anderen Discord-Servern.",
        "links_enabled": "Löscht Links, die nicht erlaubt sind.",
        "scam_enabled": "Löscht bekannte Scam-Links (Fake-Nitro usw.).",
        "badwords_enabled": "Löscht Nachrichten mit verbotenen Wörtern.",
        "badwords_list": "Die verbotenen Wörter – eins pro Zeile.",
        "mass_mentions_enabled": "Stoppt @everyone/@here von normalen Mitgliedern.",
        "exempt_roles": "Diese Rollen werden nie vom Automod erwischt (z. B. dein Team).",
    },
    "logging": {
        "default_channel": "Hier protokolliert der Bot, was auf dem Server passiert (gelöschte Nachrichten, Joins …).",
        "ignore_bots": "Nachrichten von Bots nicht protokollieren.",
    },
    "welcome": {
        "channel": "Hier werden neue Mitglieder begrüßt.",
        "message": "Der Begrüßungstext. {user} = die Person, {server} = Servername.",
        "auto_roles": "Diese Rollen bekommt jeder automatisch beim Beitreten.",
        "leave_enabled": "Nachricht, wenn jemand den Server verlässt.",
    },
    "tickets": {
        "panel_channel": "Hier steht der Knopf „Ticket öffnen“.",
        "category": "In diese Kategorie kommen neue Tickets.",
        "staff_roles": "Wer Tickets sehen und beantworten darf.",
        "transcript_channel": "Hier landet der Verlauf geschlossener Tickets.",
    },
    "applications": {
        "panel_channel": "Hier steht der Knopf „Bewerben“.",
        "review_channel": "Hier sieht dein Team die Bewerbungen.",
        "positions": "Wofür man sich bewerben kann – eins pro Zeile.",
        "accept_role": "Diese Rolle bekommt man bei Annahme.",
    },
    "giveaways": {
        "default_channel": "Hier werden Giveaways gepostet.",
        "ping_role": "Diese Rolle wird beim Start gepingt.",
        "dm_winners": "Gewinner bekommen eine DM.",
    },
    "levels": {
        "announce": "Ob und wo Level-Ups angekündigt werden. „Aus“ = keine Level-Up-Nachrichten.",
        "announce_channel": "Nur bei „In festem Channel“: hier kommen die Level-Ups hin.",
        "announce_message": "Text beim Level-Up. {user} = Person, {level} = neues Level.",
        "no_xp_channels": "In diesen Channels gibt es keine XP (z. B. Spam-Channel).",
    },
    "economy": {
        "currency_name": "Wie euer Geld heißt.",
        "currency_emoji": "Emoji für euer Geld.",
        "daily_amount": "So viel gibt es bei /daily.",
        "rob_enabled": "Mitglieder dürfen sich gegenseitig ausrauben (/rob).",
        "gambling_enabled": "Glücksspiel mit Coins erlauben (/slots, /bet).",
    },
    "music": {
        "dj_role": "Diese Rolle darf die Musik komplett steuern.",
        "allowed_channels": "Musik-Befehle nur in diesen Channels. Leer = überall.",
    },
    "suggestions": {
        "channel": "Hierhin kommen die Vorschläge (Channel oder Forum).",
        "staff_roles": "Wer Vorschläge annehmen oder ablehnen darf.",
    },
    "verification": {
        "channel": "Hier steht der Knopf „Verifizieren“.",
        "verified_role": "Diese Rolle bekommt man nach dem Verifizieren.",
        "mode": "Wie verifiziert wird.",
    },
    "antiraid": {
        "alert_channel": "Hier warnt der Bot, wenn plötzlich sehr viele Leute joinen.",
        "actions": "Was der Bot bei einem Raid automatisch macht.",
    },
}

for _spec in MODULES:
    _basic = BASIC.get(_spec.key)
    if _basic:
        for _f in _spec.fields:
            if _f.key in _basic:
                _f.help = _f.help or _basic[_f.key]
            else:
                _f.advanced = True

MODULE_MAP: dict[str, ModuleSpec] = {m.key: m for m in MODULES}


# ───────────────────────── Validierung ─────────────────────────
class ValidationError(Exception):
    def __init__(self, errors: dict[str, str]):
        super().__init__("; ".join(f"{k}: {v}" for k, v in errors.items()))
        self.errors = errors


def _to_id(v: Any) -> str | None:
    if v in (None, "", 0, "0"):
        return None
    s = str(v).strip()
    if not s.isdigit() or len(s) > 20:
        raise ValueError("Ungültige ID")
    return s


def clean_value(f: F, v: Any, ctx: "ValidationContext | None") -> Any:
    t = f.type
    if t == "bool":
        return bool(v) if not isinstance(v, str) else v.lower() in ("1", "true", "on", "yes")
    if t in ("int", "float"):
        if v in (None, ""):
            return f.default
        num = int(float(v)) if t == "int" else float(v)
        if f.min is not None and num < f.min:
            raise ValueError(f"Mindestens {f.min:g}")
        if f.max is not None and num > f.max:
            raise ValueError(f"Höchstens {f.max:g}")
        return num
    if t in ("str", "text", "url"):
        s = "" if v is None else str(v)
        if len(s) > f.max_len:
            raise ValueError(f"Maximal {f.max_len} Zeichen")
        if t == "url" and s and not URL_RE.match(s):
            raise ValueError("Ungültige URL (http/https)")
        return s.strip() if t != "text" else s
    if t == "datetime":
        from datetime import datetime, timezone
        if not v:
            raise ValueError("Datum erforderlich")
        try:
            dt = datetime.fromisoformat(str(v).replace("Z", "+00:00"))
        except ValueError:
            raise ValueError("Ungültiges Datum")
        return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
    if t == "color_opt":
        if not v:
            return None
        if not HEX_RE.match(str(v)):
            raise ValueError("Farbe im Format #RRGGBB")
        return str(v).lower()
    if t == "user":
        return _to_id(v)
    if t == "color":
        s = str(v or f.default)
        if not HEX_RE.match(s):
            raise ValueError("Farbe im Format #RRGGBB")
        return s.lower()
    if t == "select":
        allowed = {o for o, _ in (f.options or [])}
        if v not in allowed:
            raise ValueError("Ungültige Auswahl")
        return v
    if t == "multiselect":
        allowed = {o for o, _ in (f.options or [])}
        vals = [x for x in (v or []) if x in allowed]
        return list(dict.fromkeys(vals))
    if t in ("channel", "voice", "category", "role", "badge", "anychannel"):
        sid = _to_id(v)
        if sid and ctx:
            ctx.check(t, sid)
        return sid
    if t in ("channels", "roles", "users"):
        out = []
        for x in (v or [])[:100]:
            sid = _to_id(x)
            if sid:
                if ctx and t != "users":
                    ctx.check(t[:-1], sid)
                out.append(sid)
        return list(dict.fromkeys(out))
    if t == "strlist":
        items = v if isinstance(v, list) else str(v or "").split("\n")
        return [str(x).strip()[:200] for x in items if str(x).strip()][:500]
    if t == "objlist":
        rows = []
        for row in (v or [])[:50]:
            if not isinstance(row, dict):
                continue
            rows.append({sf.key: clean_value(sf, row.get(sf.key, sf.default), ctx) for sf in (f.fields or [])})
        return rows
    raise ValueError(f"Unbekannter Feldtyp {t}")


class ValidationContext:
    """Prüft, ob referenzierte Channels/Rollen wirklich auf dem Server existieren."""

    def __init__(self, channels: dict[str, str], roles: Iterable[str], badges: Iterable[str] = ()):  # channel_id -> type
        self.channels = channels
        self.roles = set(roles)
        self.badges = set(badges)

    def check(self, t: str, sid: str) -> None:
        if t == "role" and sid not in self.roles:
            raise ValueError("Rolle existiert nicht")
        if t == "badge" and sid not in self.badges:
            raise ValueError("Badge existiert nicht")
        if t in ("channel", "voice", "category", "anychannel"):
            kind = self.channels.get(sid)
            if kind is None:
                raise ValueError("Channel existiert nicht")
            if t == "anychannel":
                return
            want = {"channel": ("text", "news", "forum"), "voice": ("voice", "stage_voice"), "category": ("category",)}[t]
            if kind not in want:
                raise ValueError("Falscher Channel-Typ")


def validate_module(spec: ModuleSpec, data: dict[str, Any], ctx: ValidationContext | None) -> dict[str, Any]:
    clean: dict[str, Any] = {}
    errors: dict[str, str] = {}
    for f in spec.fields:
        try:
            clean[f.key] = clean_value(f, data.get(f.key, f.default), ctx)
        except (ValueError, TypeError) as exc:
            errors[f.key] = str(exc)
    if errors:
        raise ValidationError(errors)
    return clean
