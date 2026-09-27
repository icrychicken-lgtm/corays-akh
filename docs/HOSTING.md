# Bot 24/7 online – Hosting

Auf deinem PC läuft der Bot nur, solange der PC an ist. Für dauerhaften Betrieb gibt es zwei Wege.

## Weg A: Discord-Bot-Hoster (am einfachsten)

Anbieter wie **bot-hosting.net**, **SparkedHost**, **PebbleHost (Bot Hosting)** oder **Cybrancee** bieten ein Web-Panel (meist „Pterodactyl“), in das du die Dateien hochlädst. Preise und Angebote ändern sich, vergleiche selbst. Achte beim Buchen auf:

| Muss | Warum |
|---|---|
| **Python 3.12** (Egg/Vorlage „Python“) | Der Bot ist in Python geschrieben |
| **mind. 512 MB RAM** (besser 1 GB) | 28 Module + Dashboard |
| **1 freier Port** (Allocation) | Für das Web-Dashboard |
| optional **FFmpeg** | Nur für Musik. Viele Bot-Hoster haben es nicht, dann funktioniert alles außer `/play`. |

### Schritt für Schritt (Pterodactyl-Panel)
1. Server mit der Vorlage **Python** erstellen, Version **3.12** wählen.
2. **Startup-Einstellungen:**
   - `App py file` / Startdatei: `main.py`
   - `Requirements file`: `requirements.txt` (die Pakete installiert das Panel beim Start automatisch)
3. **Dateien hochladen** (Tab *Files*): den kompletten Ordner `NovaCommunityBot` **ohne** `data/`, `logs/`, `backups/`, `__pycache__/`. Am einfachsten lokal zippen, hochladen und im Panel entpacken.
4. **`.env` anpassen** (im Panel-Editor):
   ```ini
   DATABASE_URL=sqlite+aiosqlite:///./data/nova.db
   DASHBOARD_URL=http://DEINE-SERVER-IP:DEIN-PORT
   ```
   IP und Port stehen im Panel unter *Network/Allocations*. Den Port musst du nicht eintragen, das Panel setzt ihn automatisch (`SERVER_PORT`).
5. **Discord-Developer-Portal → OAuth2 → Redirects** zusätzlich eintragen:
   `http://DEINE-SERVER-IP:DEIN-PORT/auth/callback`
6. Im Panel auf **Start** klicken. In der Konsole sollte stehen: `Eingeloggt als Corays Akh#0417`.
7. Den Bot **auf deinem PC stoppen**, sonst laufen zwei Instanzen mit demselben Token.

> Deine bisherigen Daten (Level, Einstellungen …) stecken in `data/nova.db`. Willst du sie mitnehmen, lade diese Datei mit hoch, **bevor** du den Bot dort das erste Mal startest.

## Weg B: Eigener VPS (mehr Leistung, alles inklusive)

Ein kleiner Linux-VPS (z. B. Hetzner Cloud, Netcup, Infomaniak), Ubuntu, ab ca. 2 GB RAM. Dort läuft alles mit Docker inklusive PostgreSQL, FFmpeg (Musik) und automatischen Neustarts:

```bash
git clone <repo> /opt/bot && cd /opt/bot
cp .env.example .env   # ausfüllen
docker compose up -d --build
```

Für eine eigene Adresse wie `https://dashboard.deinedomain.de` kommt ein Reverse-Proxy (z. B. Caddy) davor. Siehe `docs/SETUP.md` Abschnitt 5.

## Welcher Weg?

| | Bot-Hoster | VPS |
|---|---|---|
| Einrichtung | Web-Panel, kein Linux nötig | Linux-Grundlagen |
| Musik | oft nicht (kein FFmpeg) | ja |
| Dashboard-Adresse | `http://IP:Port` | eigene Domain mit HTTPS |
| Datenbank | SQLite | PostgreSQL |
