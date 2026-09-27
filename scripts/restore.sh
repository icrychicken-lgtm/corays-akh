#!/usr/bin/env bash
# Stellt ein vollständiges PostgreSQL-Backup wieder her (Docker-Setup).
#   ./scripts/restore.sh backups/full_20260101-040000.dump
# ACHTUNG: überschreibt die komplette Datenbank. Der Bot wird dafür kurz gestoppt.
set -euo pipefail
cd "$(dirname "$0")/.."
FILE="${1:?Pfad zur .dump-Datei angeben}"
USER="${POSTGRES_USER:-nova}"
DB="${POSTGRES_DB:-nova}"

read -r -p "Datenbank '$DB' wirklich mit '$FILE' überschreiben? (ja/nein) " ANSWER
[ "$ANSWER" = "ja" ] || { echo "Abgebrochen."; exit 1; }

docker compose stop bot
docker compose exec -T db pg_restore -U "$USER" -d "$DB" --clean --if-exists --no-owner < "$FILE"
docker compose start bot
echo "✅ Wiederhergestellt aus $FILE"
