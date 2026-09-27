"""Standard-Ticket-Kategorien mit passenden Fragen je Thema (max. 5 pro Kategorie – Discord-Limit für Formulare)."""
from __future__ import annotations


def q(label: str, placeholder: str = "", style: str = "short", required: bool = True) -> dict:
    return {"label": label, "placeholder": placeholder, "style": style, "required": required}


DEFAULT_CATEGORIES: list[tuple[str, str, str, list[dict]]] = [
    ("Support", "🛠️", "Hilfe & Fragen", [
        q("Wobei brauchst du Hilfe?", "Beschreib kurz dein Problem", "paragraph")]),
    ("Bewerbung", "📋", "Ins Team", [
        q("Wie alt bist du?", "z. B. 17"),
        q("Für welche Position?", "z. B. Moderator, Supporter, Editor"),
        q("Hast du schon Erfahrung?", "Wo und wie lange?", "paragraph"),
        q("Warum sollten wir dich nehmen?", "", "paragraph"),
        q("Wie viel Zeit hast du pro Woche?", "z. B. 10 Stunden")]),
    ("Entbannungsantrag", "🔓", "Ban aufheben", [
        q("Dein gebannter Account", "Name oder User-ID"),
        q("Warum wurdest du gebannt?", "", "paragraph"),
        q("Warum sollen wir dich entbannen?", "", "paragraph")]),
    ("Partnerschaft", "🤝", "Kooperation", [
        q("Name deines Servers / Projekts"),
        q("Einladungslink oder Website", "https://…"),
        q("Wie viele Mitglieder / Follower?", "z. B. 1.200"),
        q("Was stellst du dir vor?", "", "paragraph")]),
    ("Beschwerde", "⚠️", "Etwas melden", [
        q("Über wen oder was?", "Name, User-ID oder Channel"),
        q("Was ist passiert?", "", "paragraph"),
        q("Beweise", "Links zu Screenshots (optional)", "short", False)]),
    ("Creator", "🎥", "Für Creator", [
        q("Link zu deinem Kanal", "https://twitch.tv/…"),
        q("Plattform & Follower", "z. B. Twitch, 3.000"),
        q("Worum geht's?", "", "paragraph")]),
    ("Gewinnspiel", "🎁", "Gewinne & Giveaways", [
        q("Welches Giveaway?", "Name oder Preis"),
        q("Dein Anliegen", "z. B. Gewinn abholen", "paragraph")]),
    ("Technische Probleme", "💻", "Bot & Technik", [
        q("Was funktioniert nicht?", "", "paragraph"),
        q("Was hast du schon versucht?", "(optional)", "paragraph", False)]),
    ("Allgemeine Anfrage", "💬", "Alles andere", [
        q("Worum geht es?", "", "paragraph")]),
]

QUESTIONS_BY_LABEL = {label.lower(): qs for label, _e, _d, qs in DEFAULT_CATEGORIES}
