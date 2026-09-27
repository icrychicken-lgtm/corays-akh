"""Startdatei für Bot-Hoster (Pterodactyl-Panels wie bot-hosting.net, SparkedHost, PebbleHost …).
Im Panel als Startdatei „main.py“ eintragen. Lokal geht auch: python main.py

Vor dem Start holt `updater.py` automatisch die neueste Version aus GitHub (wenn UPDATE_REPO + UPDATE_TOKEN gesetzt sind)."""
from updater import update

if __name__ == "__main__":
    update()
    from app.__main__ import entry

    entry()
