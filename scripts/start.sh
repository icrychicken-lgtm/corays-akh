#!/usr/bin/env bash
# Startet Bot + Dashboard ohne Docker. Neustart automatisch, wenn das Owner-Panel "Neustart" auslöst (Exit-Code 3).
set -euo pipefail
cd "$(dirname "$0")/.."

if [ ! -f .env ]; then
  echo "⚠️  .env fehlt – kopiere .env.example nach .env und fülle die Werte aus."
  exit 1
fi
if [ ! -d .venv ]; then
  python3 -m venv .venv
fi
# shellcheck disable=SC1091
source .venv/bin/activate
pip install -q -r requirements.txt

while true; do
  set +e
  python -m app
  code=$?
  set -e
  if [ "$code" -eq 3 ]; then
    echo "🔄 Neustart angefordert …"
    sleep 2
    continue
  fi
  echo "Beendet mit Code $code"
  exit "$code"
done
