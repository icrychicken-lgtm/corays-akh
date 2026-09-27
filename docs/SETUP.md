# Setup-Guide – von null zum laufenden Bot

Diese Anleitung dauert etwa 20 Minuten. Am Ende läuft der Bot auf deinem Server und du verwaltest alles im Dashboard.

---

## 1. Discord-Application anlegen

1. Öffne <https://discord.com/developers/applications> → **New Application** → Namen vergeben (z. B. „Nova“).
2. **General Information:** Icon und Beschreibung nach Wunsch.
3. **Bot:**
   - **Reset Token** → Token kopieren (nur einmal sichtbar!) → später `DISCORD_TOKEN`.
   - **Privileged Gateway Intents:**
     - ✅ `SERVER MEMBERS INTENT` (Pflicht)
     - ✅ `MESSAGE CONTENT INTENT` (Pflicht – Automod, XP, Autoresponder)
     - ✅ `PRESENCE INTENT` (optional – Online-Zähler, Games im Profil)
4. **OAuth2:**
   - `CLIENT ID` → `DISCORD_CLIENT_ID`
   - **Reset Secret** → `DISCORD_CLIENT_SECRET`
   - **Redirects → Add Redirect:** `https://deine-domain.de/auth/callback`
     (lokal: `http://localhost:8080/auth/callback`)

## 2. Server vorbereiten

**Mit Docker (empfohlen):** Linux-Server/VPS mit Docker + Docker Compose Plugin.

```bash
git clone <repo-url> /opt/nova
cd /opt/nova
cp .env.example .env
python3 scripts/generate_keys.py
```

Die Ausgabe (`SECRET_KEY=…` und `ENCRYPTION_KEY=…`) in die `.env` übernehmen.

> ⚠️ Den `ENCRYPTION_KEY` sicher aufbewahren. Ohne ihn lassen sich gespeicherte API-Keys nicht mehr entschlüsseln.

## 3. `.env` ausfüllen

Mindestens:

```ini
DISCORD_TOKEN=…
DISCORD_CLIENT_ID=…
DISCORD_CLIENT_SECRET=…
OWNER_IDS=123456789012345678          # deine Discord-User-ID (Entwicklermodus → Rechtsklick → ID kopieren)
DASHBOARD_URL=https://dash.deine-domain.de
COOKIE_SECURE=true                    # bei HTTPS
SECRET_KEY=…
ENCRYPTION_KEY=…
POSTGRES_PASSWORD=ein-sicheres-passwort
```

`DATABASE_URL` und `REDIS_URL` setzt `docker-compose.yml` automatisch.

## 4. Starten

```bash
docker compose up -d --build
docker compose logs -f bot
```

In den Logs erscheint:
```
INFO  nova.bot  26 Module geladen: …
INFO  nova.bot  Eingeloggt als Nova#1234 – 0 Server
INFO  nova.bot  … globale Commands synchronisiert
```

## 5. HTTPS mit Caddy (Beispiel)

```caddyfile
dash.deine-domain.de {
    reverse_proxy localhost:8080
}
```
Caddy holt automatisch ein Zertifikat und reicht WebSockets durch. Bei Nginx `proxy_set_header Upgrade $http_upgrade; proxy_set_header Connection "upgrade";` für `/ws` setzen.

## 6. Bot einladen & erste Einrichtung

1. Öffne `DASHBOARD_URL` → **Mit Discord anmelden**.
2. Beim Server auf **＋ Bot einladen** klicken (Berechtigungen sind vorausgewählt).
3. In Discord: **Servereinstellungen → Rollen** → die Rolle „Nova“ **über** alle Rollen ziehen, die der Bot vergeben oder moderieren soll.
4. Im Dashboard den Server öffnen und der Reihe nach einrichten:

| Schritt | Wo im Dashboard |
|---|---|
| Sprache, Farben, Admin-/Staff-Rollen | Einstellungen → Allgemein |
| Module an/aus | Einstellungen → Module |
| Mod-Log-Channel, Eskalation | Moderation → Einstellungen |
| Log-Channels | Logs → Log-Einstellungen |
| Automod-Filter, Anti-Raid, Verifizierung | Automod |
| Ticket-Kategorien + Panel senden | Tickets → Kategorien / Ticket-Einstellungen |
| Bewerbungsfragen + Panel | Tickets → Bewerbungs-Einstellungen |
| Welcome, Boost, Geburtstage | Benachrichtigungen |
| Self-Role-Menüs | Rollen → Self-Role-Menüs → **Veröffentlichen** |
| Level-Belohnungen (5/10/25/50/100) | Level → Level-Belohnungen |
| Shop-Items, Quests | Economy |
| Streamer verbinden | Einstellungen → Integrationen (API-Keys), dann Streamer → Streamer verbinden |
| Custom Commands (`/socials` …) | Einstellungen → Custom Commands |

## 7. Streaming-Integrationen

| Plattform | Zugangsdaten | Eintragen als |
|---|---|---|
| **Twitch** | <https://dev.twitch.tv/console> → Register Application (Redirect egal, z. B. `http://localhost`), Kategorie „Chat Bot“ → Client ID + neues Secret | Integrationen → Twitch |
| **YouTube** | <https://console.cloud.google.com> → Projekt → „YouTube Data API v3“ aktivieren → Anmeldedaten → API-Schlüssel | Integrationen → YouTube |
| **Kick** | <https://kick.com/settings/developer> → App erstellen → Client ID + Secret | Integrationen → Kick |
| **Spotify** (Musik-Links) | <https://developer.spotify.com/dashboard> → App → Client ID + Secret | Integrationen → Spotify |

Keys können global in der `.env` stehen (gelten für alle Server) oder pro Server im Dashboard (verschlüsselt gespeichert).

Streamer hinzufügen: **Streamer → Streamer verbinden → ＋**
- Twitch: Login-Name (z. B. `montanablack88`)
- YouTube: Channel-ID (`UC…`) oder `@Handle`
- Kick: Slug aus der URL

Mit **Jetzt prüfen** testest du die Verbindung sofort. Fehler stehen in der Spalte „Fehler“.

## 8. Backups

- Automatisch alle `BACKUP_INTERVAL_HOURS` (Standard 24 h), Dateien in `./backups`.
- Manuell: Dashboard → Einstellungen → Backups (pro Server) oder Owner-Panel → DB-Backups (komplett).
- Per Cron auf dem Host: `0 4 * * * cd /opt/nova && ./scripts/backup.sh >> logs/backup.log 2>&1`
- Den Ordner `backups/` regelmäßig auf einen anderen Rechner/Speicher kopieren.

## 9. Updates

```bash
cd /opt/nova
git pull
docker compose up -d --build
```

Migrationen laufen beim Start automatisch. Vorher empfiehlt sich `./scripts/backup.sh`.

## 10. Checkliste bei Problemen

- [ ] Bot-Rolle ganz oben?
- [ ] Intents im Developer-Portal aktiv?
- [ ] Redirect-URL exakt `DASHBOARD_URL` + `/auth/callback`?
- [ ] `COOKIE_SECURE=true` nur mit HTTPS (sonst klappt der Login nicht)?
- [ ] Logs: `docker compose logs --tail=200 bot`
- [ ] Fehler-IDs: Dashboard → Einstellungen → Fehler
