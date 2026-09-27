# Nova – Community-Bot & Web-Dashboard für Streamer

Ein modularer Discord-Bot mit Web-Dashboard für Streamer-Communities. Moderation, Automod, Tickets, Giveaways, Level, Economy, Musik, Twitch/YouTube/Kick-Benachrichtigungen, Analytics und ein Owner-Panel – alles ohne Code über das Dashboard einstellbar.

- **Bot:** Python 3.12, discord.py 2.7 (Slash-Commands, Buttons, Select-Menüs, Modals, Kontextmenüs)
- **Dashboard:** FastAPI + Vanilla-JS-SPA (ohne Build-Schritt), Discord-OAuth2-Login, Live-Updates per WebSocket
- **Daten:** PostgreSQL (SQLAlchemy 2 async + Alembic-Migrationen), Redis optional (Cache/Rate-Limits)
- **Betrieb:** Docker Compose, Logging mit Rotation, Fehler-Monitoring, automatische Backups

> Bot und Dashboard laufen **in einem Prozess und einem Event-Loop**. Dashboard-Aktionen (Rolle erstellen, Ticket schließen, Giveaway starten, Musik steuern …) laufen direkt über die Bot-Verbindung, ohne zusätzliche IPC-Schicht.

---

## Inhalt
1. [Features](#features)
2. [Schnellstart mit Docker](#schnellstart-mit-docker)
3. [Installation ohne Docker](#installation-ohne-docker)
4. [Discord-Developer-Portal einrichten](#discord-developer-portal-einrichten)
5. [Konfiguration (.env)](#konfiguration-env)
6. [Dashboard & Berechtigungen](#dashboard--berechtigungen)
7. [Commands](#commands)
8. [Architektur](#architektur)
9. [Sicherheit](#sicherheit)
10. [Backups & Wiederherstellung](#backups--wiederherstellung)
11. [Updates & Migrationen](#updates--migrationen)
12. [Bekannte Grenzen](#bekannte-grenzen)
13. [Fehlerbehebung](#fehlerbehebung)

Eine Schritt-für-Schritt-Anleitung für die Ersteinrichtung steht in **[docs/SETUP.md](docs/SETUP.md)**.

---

## Features

| Bereich | Umfang |
|---|---|
| **Moderation** | `/warn /timeout /untimeout /kick /ban /unban /softban /purge /slowmode /lock /unlock /nick /warnlist /modhistory /case`, fortlaufende Case-IDs, Temp-Bans, automatische Eskalation (z. B. 3 Warns → Timeout), DM an User, Mod-Log, Kontextmenüs „Verwarnen“ und „Nachricht melden“. Moderationsaktionen aus der Discord-App werden ebenfalls als Case erfasst. |
| **Automod** | Spam, Flood, Caps, Duplikate, Discord-Invites (eigene erlaubt), unerlaubte/gesperrte Domains, Scam- & Lookalike-Links, IP- und Shortener-Links, Bad Words (Wildcards, Leetspeak-Normalisierung), Mention-Spam, Mass-Mentions, Emoji-Spam, neue Accounts. Aktionen je Filter: löschen, warnen, Timeout, Kick, Ban, Channel sperren, Rolle entfernen, Log. Whitelist für User, Rollen, Channels und Domains. |
| **Anti-Raid & Verifizierung** | Join-Wellen-Erkennung (z. B. 30 Joins/20 s) → Staff-Alarm, Verifizierungsstufe anheben, Einladungen pausieren, Channels sperren, neue User timeouten/kicken; automatisches Ende. Verifizierung per Button oder Bild-Captcha, Mindest-Accountalter. |
| **Tickets** | Panel mit Kategorien (Buttons oder Select), Modal „Was ist dein Anliegen?“, Claim, Close (mit Grund), Reopen, Rename, Add/Remove User, Lock, Transfer, Priorität, Staff-Notes, HTML-Transcripts (Discord + Dashboard), Messung der Erstantwortzeit. |
| **Bewerbungen** | Panel → Positionswahl → mehrseitiges Formular (beliebig viele Fragen), Review per Buttons oder Dashboard (offen / in Bearbeitung / angenommen / abgelehnt), automatische DM, Rolle bei Annahme, Cooldown. |
| **Giveaways** | Mehrere Gewinner, Mindestrolle, Account-/Serveralter, Zusatzbedingung, Bonus-Entries (Rollen, Invites, Aktivität, Early Join), Buttons 🎉/👥/⏰, gewichtete Ziehung, Reroll, Gewinner-DM, Coins für Gewinner, Historie. |
| **Level** | XP für Nachrichten (Cooldown gegen Spam), Voice (Anti-AFK: nur mit anderen, nicht stumm/taub), Reaktionen, Stream-Check-ins, Events. Multiplikator-Rollen, Belohnungsrollen (stapelnd oder exklusiv), `/level`, `/leaderboard`, `/xp`. |
| **Economy** | Eigene Währung, `/balance /daily /weekly /work /pay /shop /inventory`, Streaks mit 7- und 30-Tage-Bonus, Shop (Rollen, Farbrollen, Badges, kosmetische und Event-Items, Bestand/Limit). Keine Echtgeld-Transaktionen. |
| **Quests & Achievements** | Tägliche und wöchentliche Quests (Nachrichten, Voice-Minuten, Reaktionen, Stream-Check-ins, Events, Daily, Giveaways) mit XP/Coins/Rolle/Badge. 17 Achievements mit Fortschritt; konfigurierbare Badges (Owner, Staff, Booster, Gewinner, OG, Rolle, Level, manuell). |
| **Musik** | `/play /pause /resume /skip /stop /queue /volume /loop /shuffle /nowplaying /remove`, Skip-Vote, DJ-Rolle, Now-Playing-Panel mit Buttons, Spotify-Links (über die Spotify-API aufgelöst), Playlists, Auto-Leave, Steuerung im Dashboard. |
| **Streamer** | Twitch (Helix), YouTube (RSS + Data API, mit Short-Erkennung) und Kick (offizielle API). Live-Post 🔴 mit Titel, Kategorie, Link und Zuschauern, die live aktualisiert werden; Rollen-Ping, Live-Rolle für den Streamer, Channel-Umbenennung, Zusammenfassung nach Stream-Ende, Check-in-Button „Ich bin dabei“ (XP/Coins/Quests). Stats: aktuelle Zuschauer, Peak, Ø, Dauer, Streams, Follower/Subscriber-Wachstum (24h/7d/30d/90d). |
| **Community** | Welcome/Leave (Embed, Bild, Auto-Rollen, Auto-DM), Boost-Danke mit Belohnungen, Geburtstage (privat, Rolle für 24 h, Coins/XP), Suggestions mit Voting und Staff-Entscheidung, Umfragen (Live-Ergebnisse, Mehrfachauswahl, anonym), Reminders, Starboard, AFK, Server-Events mit RSVP, Countdown, Erinnerungen und Gewinnern, Join-to-Create-Voice mit Steuer-Panel, Stats-Channels, Profile mit Banner, Bio und Games, Self-Role-Menüs, Rollen-Sync-Regeln, Custom Slash-Commands, Autoresponder, Fun (`/8ball /coinflip /dice /roll /choose /ship /rate /avatar /banner /userinfo /serverinfo /ping`). |
| **Logging** | Nachrichten (Löschen/Bearbeiten/Bulk), Join/Leave, Ban/Kick/Timeout, Rollen, Channels, Voice, Nicknames, Mod-Aktionen, Tickets, Giveaways – Discord-Log-Channels plus durchsuchbar im Dashboard. |
| **Dashboard** | Übersicht mit Live-Stats & Charts, Serverinfo, Mitgliederprofile (Historie, Notizen, XP/Coins, Rollen, Badges), Moderation, Staff-System, Audit-Log, Tickets mit Transcript-Viewer, Giveaways, Level/Economy, Musik-Player, Streamer-Stats, Analytics (24h/7d/30d/90d/All), Rollen-Editor inkl. Berechtigungen, Benachrichtigungen, Suggestions & Polls, Events, Logs, Einstellungen (Feature-Toggles, Command-Berechtigungen, Integrationen, Backups, Fehler), globale Suche (Strg+K), Owner-Panel. |
| **Betrieb** | Multi-Server (eigene Einstellungen pro Server), Maintenance-Modus, Neustart, globale Ankündigungen, Fehler-Monitoring mit Error-ID und Stacktrace, automatische Backups, Deutsch/Englisch. |

---

## Schnellstart mit Docker

```bash
git clone <dein-repo> nova && cd nova
cp .env.example .env
python scripts/generate_keys.py      # SECRET_KEY + ENCRYPTION_KEY in die .env kopieren
# .env ausfüllen: DISCORD_TOKEN, DISCORD_CLIENT_ID, DISCORD_CLIENT_SECRET, OWNER_IDS, DASHBOARD_URL
docker compose up -d --build
docker compose logs -f bot
```

Das Dashboard ist danach unter `DASHBOARD_URL` erreichbar (Standard `http://localhost:8080`). Migrationen laufen beim Start automatisch.

> Für den Produktivbetrieb gehört ein Reverse-Proxy mit HTTPS davor (Caddy, Nginx, Traefik). Danach `COOKIE_SECURE=true` und `DASHBOARD_URL=https://…` setzen. Der Proxy muss WebSockets (`/ws`) durchreichen.

## Installation ohne Docker

Voraussetzungen: Python 3.12, PostgreSQL 14+, FFmpeg (für Musik), optional Redis.

```bash
python -m venv .venv
source .venv/bin/activate            # Windows: .venv\Scripts\activate
pip install -r requirements.txt
cp .env.example .env                 # ausfüllen
python -m app migrate                # optional – passiert auch automatisch beim Start
python -m app
```

Oder mit den Start-Skripten (inkl. automatischem Neustart aus dem Owner-Panel):
`./scripts/start.sh` (Linux/macOS) bzw. `.\scripts\start.ps1` (Windows).

Für schnelle lokale Tests funktioniert auch SQLite: `DATABASE_URL=sqlite+aiosqlite:///./data/nova.db`.

---

## Discord-Developer-Portal einrichten

1. <https://discord.com/developers/applications> → **New Application**
2. **Bot** → Token kopieren → `DISCORD_TOKEN`
3. **Bot → Privileged Gateway Intents:** `SERVER MEMBERS INTENT` und `MESSAGE CONTENT INTENT` aktivieren, optional `PRESENCE INTENT` (für Online-Zähler und „Games“ im Profil; sonst `ENABLE_PRESENCE_INTENT=false`)
4. **OAuth2:** Client ID und Client Secret → `DISCORD_CLIENT_ID` / `DISCORD_CLIENT_SECRET`
5. **OAuth2 → Redirects:** `<DASHBOARD_URL>/auth/callback` eintragen (z. B. `https://dash.example.com/auth/callback`)
6. Bot einladen: im Dashboard auf der Server-Auswahl oder über `<DASHBOARD_URL>/invite` – die nötigen Berechtigungen sind vorausgewählt
7. Die **Bot-Rolle** in den Server-Einstellungen möglichst weit oben platzieren – der Bot kann nur Rollen vergeben/moderieren, die unter seiner eigenen liegen.

## Konfiguration (.env)

Alle Secrets stehen ausschließlich in der `.env` (siehe [.env.example](.env.example)). Alles andere – Channels, Rollen, Texte, Farben, Module – wird **pro Server im Dashboard** eingestellt. Im Code stehen keine IDs.

| Variable | Beschreibung |
|---|---|
| `DISCORD_TOKEN`, `DISCORD_CLIENT_ID`, `DISCORD_CLIENT_SECRET` | Aus dem Developer-Portal |
| `OWNER_IDS` | Komma-separierte User-IDs mit Zugriff aufs Owner-Panel |
| `DASHBOARD_URL` | Öffentliche URL (für OAuth-Redirect & Origin-Prüfung) |
| `SECRET_KEY` | ≥ 32 zufällige Zeichen (signiert den OAuth-State) |
| `ENCRYPTION_KEY` | Fernet-Key zur Verschlüsselung von API-Keys und OAuth-Tokens |
| `DATABASE_URL` | `postgresql+asyncpg://…` (Produktion) oder `sqlite+aiosqlite:///…` (Tests) |
| `REDIS_URL` | Optional; ohne Redis wird ein In-Memory-Cache genutzt |
| `COOKIE_SECURE` | `true` hinter HTTPS |
| `BACKUP_INTERVAL_HOURS`, `BACKUP_KEEP` | Automatische Backups |
| `TWITCH_*`, `YOUTUBE_API_KEY`, `KICK_*`, `SPOTIFY_*` | Globale Standard-Zugangsdaten; pro Server im Dashboard überschreibbar |
| `STREAM_POLL_SECONDS` | Prüfintervall für Streams (min. 60) |
| `DEV_GUILD_ID` | Commands sofort in einer Test-Guild synchronisieren |

## Dashboard & Berechtigungen

- **Login** über Discord OAuth2 (Scopes `identify guilds`).
- **Admin**: Server-Owner, User mit „Server verwalten“ oder „Administrator“, sowie die in *Einstellungen → Allgemein → Dashboard-Admin-Rollen* hinterlegten Rollen → voller Zugriff.
- **Staff**: User mit Moderationsrechten oder den eingestellten *Staff-Rollen* → Übersicht, Mitglieder, Moderation, Tickets, Bewerbungen, Giveaways, Logs usw., aber keine Einstellungen.
- **Owner-Panel**: nur `OWNER_IDS`.
- Die Berechtigung wird **bei jeder API-Anfrage serverseitig** anhand der aktuellen Discord-Rollen geprüft. Rollen-Hierarchie und „nur Rechte vergeben, die man selbst hat“ werden auch im Dashboard durchgesetzt.

**Command-Berechtigungen** (*Einstellungen → Commands*): pro Command aktivieren/deaktivieren, erlaubte/gesperrte Rollen, zusätzliche User, erlaubte/gesperrte Channels. Admins umgehen Beschränkungen. Discords eigene Integrations-Berechtigungen gelten zusätzlich.

**Feature-Toggles** (*Einstellungen → Module*): jedes Modul einzeln 🟢/🔴. Deaktivierte Module reagieren auf keine Commands, Buttons oder Events.

## Commands

<details>
<summary>Alle Slash-Commands anzeigen</summary>

| Kategorie | Commands |
|---|---|
| Moderation | `/warn` `/timeout` `/untimeout` `/kick` `/ban` `/unban` `/softban` `/purge` `/slowmode` `/lock` `/unlock` `/nick` `/warnlist` `/modhistory` `/case` · Kontextmenü: *Verwarnen*, *Nachricht melden* |
| Tickets | `/ticket panel · close · reopen · claim · add · remove · rename · transfer · priority · lock · note · transcript` |
| Level | `/level` `/leaderboard` `/xp give · take · set` |
| Economy | `/balance` `/daily` `/weekly` `/work` `/pay` `/shop` `/inventory` `/coins give · take` `/quests` |
| Musik | `/play` `/pause` `/resume` `/skip` `/stop` `/queue` `/volume` `/loop` `/shuffle` `/nowplaying` `/remove` |
| Community | `/profile` `/profil banner · bio` `/achievements` `/suggest` `/poll` `/remind` `/reminders` `/afk` `/birthday set · remove · upcoming` `/events` `/streams` `/giveaway start · end · reroll · delete · list` |
| Fun & Info | `/8ball` `/coinflip` `/dice` `/roll` `/choose` `/ship` `/rate` `/avatar` `/banner` `/userinfo` `/serverinfo` `/ping` `/help` · Kontextmenü: *Avatar anzeigen*, *Benutzerinfo*, *Profil anzeigen* |
| Admin | `/verification-panel` · Panels, Custom Commands und alles Weitere im Dashboard |

</details>

## Architektur

```
app/
├── __main__.py          Start: Migrationen → Bot + Dashboard in einem Event-Loop
├── config.py            Konfiguration aus .env (pydantic-settings)
├── core/                schema.py (Modul-Definitionen = Toggles + Formulare + Validierung),
│                        guild_config (Settings-Cache), i18n, embeds (Design), metrics, events (Live-Bus),
│                        records (Audit/Logs/Fehler), crypto, cache, timeutil, errors
├── db/                  SQLAlchemy-Modelle (≈45 Tabellen), Engine/Session
├── services/            geteilte Logik für Bot & Dashboard: moderation, progression (XP/Coins/Quests),
│                        achievements, streaming (Twitch/YouTube/Kick), integrations, backups, notify
├── bot/                 NovaBot (globale Checks, Fehlerbehandlung), ui.py (Views, Confirm, Paginator)
│   └── modules/         ein Modul pro Datei, automatisch geladen (26 Module)
├── web/                 FastAPI: auth (OAuth2), security (Sessions/CSRF/Rate-Limits/Header), deps (Zugriff),
│   │                    routes_* (REST-API), collections (generisches CRUD), server (WebSocket, SPA)
│   └── static/          Dashboard-SPA: css/app.css, js/core.js, js/forms.js, js/app.js, js/pages/*.js
└── locales/             de.json, en.json – alle Bot-Texte zentral
migrations/              Alembic (automatisch beim Start)
scripts/                 start.sh/.ps1, backup.sh, restore.sh, generate_keys.py, check_i18n.py
```

**Neues Modul hinzufügen:** Datei in `app/bot/modules/` anlegen (Cog mit `module = "<key>"`), Einstellungen in `app/core/schema.py` als `ModuleSpec` beschreiben. Toggle, Formular und Validierung im Dashboard entstehen daraus automatisch. Texte in `app/locales/*.json` ergänzen und mit `python scripts/check_i18n.py` prüfen.

**Persistente Buttons** sind `DynamicItem`s mit Custom-IDs (`nova:tk:claim`, `nova:gw:<id>:join` …). Sie funktionieren auch nach einem Neustart.

## Sicherheit

- OAuth2 mit signiertem, zeitlich begrenztem `state`; serverseitige Sessions (HttpOnly, SameSite=Lax, Secure hinter HTTPS)
- CSRF-Schutz: Token im Header + Origin-Prüfung für alle schreibenden Anfragen
- Rate-Limits pro IP und pro User (Redis oder In-Memory)
- Berechtigungen bei jeder Anfrage serverseitig gegen die Live-Discord-Rollen geprüft; Rollen-Hierarchie wird eingehalten
- Eingaben serverseitig schemabasiert validiert; Channels/Rollen müssen auf dem Server existieren und den passenden Typ haben
- SQL ausschließlich über SQLAlchemy-Parameter (keine String-Konkatenation)
- XSS: das Frontend setzt Nutzerdaten nur als Textknoten; strikte Content-Security-Policy; Transcripts laufen in einer Sandbox
- API-Keys und OAuth-Tokens mit Fernet verschlüsselt gespeichert, im Dashboard nur maskiert angezeigt und nicht ins Audit-Log geschrieben
- Security-Header (CSP, X-Frame-Options, nosniff, Referrer-Policy, HSTS bei HTTPS)

## Backups & Wiederherstellung

- **Pro Server (Dashboard → Einstellungen → Backups):** Export aller Bot-Daten des Servers als komprimiertes JSON; Download; Wiederherstellung (vorher wird automatisch ein Sicherungs-Backup angelegt). Log-Verläufe sind nicht enthalten.
- **Automatisch:** alle `BACKUP_INTERVAL_HOURS` pro Server plus ein Vollbackup (`pg_dump`); ältere automatische Backups werden nach `BACKUP_KEEP` rotiert.
- **Vollbackup (Owner-Panel → DB-Backups)** oder per Skript: `./scripts/backup.sh`; Wiederherstellung: `./scripts/restore.sh backups/<datei>.dump`.

## Updates & Migrationen

```bash
git pull
docker compose up -d --build     # Migrationen laufen beim Start automatisch
```

Nach Modell-Änderungen eine neue Migration erzeugen:
```bash
alembic revision --autogenerate -m "beschreibung"
```

## Bekannte Grenzen

- **Twitch-Follower/Subscriber:** Die Twitch-API gibt diese Zahlen nur mit einem Broadcaster-User-Token heraus. Mit App-Zugangsdaten zeigt das Dashboard sie deshalb als „—“ an. Zuschauer, Peak, Ø und Streams funktionieren vollständig.
- **YouTube:** Neue Videos kommen quotafrei über den RSS-Feed; pro Prüfung wird 1 Einheit der Data-API verbraucht (Standard-Quota 10.000/Tag reicht für mehrere Kanäle bei 90 s Intervall). Die Subscriber-Zahl ist nur verfügbar, wenn der Kanal sie öffentlich zeigt.
- **Kick:** nutzt die offizielle Public API (App unter kick.com/settings/developer). Follower-Zahlen gibt diese API nicht heraus.
- **Musik:** Wiedergabe über yt-dlp/FFmpeg. Beachte die Nutzungsbedingungen der jeweiligen Plattform. Spotify-Links werden in Suchanfragen übersetzt; Spotify selbst streamt keine Audiodaten an Bots.
- Ohne **Presence-Intent** bleiben Online-Zähler und „Games“ im Profil leer.

## Fehlerbehebung

| Problem | Lösung |
|---|---|
| Slash-Commands erscheinen nicht | Globale Commands brauchen bis zu 1 h. Für Tests `DEV_GUILD_ID` setzen oder Owner-Panel → „Commands neu synchronisieren“. |
| Dashboard-Login schlägt fehl | Redirect `<DASHBOARD_URL>/auth/callback` exakt im Developer-Portal eintragen; `DASHBOARD_URL` ohne Slash am Ende. |
| „Die Rolle liegt über meiner Rolle“ | Bot-Rolle in den Server-Einstellungen nach oben ziehen. |
| Musik spielt nicht | FFmpeg installiert? (Docker: enthalten) · Bot hat „Verbinden/Sprechen“? |
| Stream wird nicht erkannt | Dashboard → Streamer → Spalte „Fehler“ und „Jetzt prüfen“; Zugangsdaten unter Integrationen. |
| Fehler-ID im Chat | Dashboard → Einstellungen → Fehler bzw. Owner-Panel → Fehler zeigt den Stacktrace. |
