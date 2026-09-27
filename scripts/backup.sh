#!/usr/bin/env bash
# Vollständiges PostgreSQL-Backup (Docker-Setup). Behält die letzten N Dateien.
#   ./scripts/backup.sh            → backups/manual_YYYYmmdd-HHMMSS.dump
#   KEEP=30 ./scripts/backup.sh
# Für Cron:  0 4 * * * cd /opt/nova && ./scripts/backup.sh >> logs/backup.log 2>&1
set -euo pipefail
cd "$(dirname "$0")/.."
KEEP="${KEEP:-14}"
mkdir -p backups
STAMP="$(date +%Y%m%d-%H%M%S)"
FILE="backups/manual_${STAMP}.dump"
USER="${POSTGRES_USER:-nova}"
DB="${POSTGRES_DB:-nova}"

docker compose exec -T db pg_dump -U "$USER" -d "$DB" --format=custom > "$FILE"
echo "✅ Backup erstellt: $FILE ($(du -h "$FILE" | cut -f1))"

ls -1t backups/manual_*.dump 2>/dev/null | tail -n +"$((KEEP + 1))" | xargs -r rm --
