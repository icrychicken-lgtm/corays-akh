"""Startpunkt: führt Migrationen aus und startet Bot + Dashboard im selben Event-Loop.

    python -m app            # Bot + Dashboard
    python -m app migrate    # nur Datenbank-Migrationen
"""
from __future__ import annotations

import asyncio
import logging
import signal
import sys

import uvicorn

from app import runtime
from app.config import ROOT_DIR, settings
from app.logging_setup import setup_logging

log = logging.getLogger("nova")


def run_migrations() -> None:
    from alembic import command
    from alembic.config import Config

    cfg = Config(str(ROOT_DIR / "alembic.ini"))
    cfg.set_main_option("script_location", str(ROOT_DIR / "migrations"))
    command.upgrade(cfg, "head")


def _check_config() -> None:
    problems = []
    if not settings.secret_key or len(settings.secret_key) < 32:
        problems.append("SECRET_KEY fehlt oder ist kürzer als 32 Zeichen")
    if not settings.encryption_key:
        log.warning("ENCRYPTION_KEY fehlt – wird aus SECRET_KEY abgeleitet. Für Produktion eigenen Fernet-Key setzen!")
    if not settings.discord_client_id or not settings.discord_client_secret:
        log.warning("DISCORD_CLIENT_ID/SECRET fehlen – Dashboard-Login ist deaktiviert.")
    if problems:
        for p in problems:
            log.critical(p)
        sys.exit(2)


async def main() -> None:
    setup_logging()
    _check_config()
    log.info("Starte %s – Datenbank: %s", settings.bot_name, settings.database_url.split("@")[-1].split("://")[0] if "@" in settings.database_url else settings.database_url.split(":")[0])
    await asyncio.to_thread(run_migrations)
    log.info("Datenbank-Migrationen aktuell")

    from app.bot.bot import NovaBot
    from app.web.server import create_app

    bot = NovaBot()
    runtime.bot = bot
    server = uvicorn.Server(uvicorn.Config(create_app(), host=settings.web_host, port=settings.port, log_level="warning",
                                           proxy_headers=True, forwarded_allow_ips="*", lifespan="off"))
    server.install_signal_handlers = lambda: None  # Signale selbst behandeln

    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, stop.set)
        except (NotImplementedError, RuntimeError):  # Windows
            signal.signal(sig, lambda *_: loop.call_soon_threadsafe(stop.set))

    tasks = [asyncio.create_task(server.serve(), name="web")]
    log.info("Dashboard läuft auf %s (lokal: http://%s:%s)", settings.dashboard_url, settings.web_host, settings.port)
    if settings.discord_token:
        tasks.append(asyncio.create_task(bot.start(settings.discord_token), name="bot"))
    else:
        log.error("DISCORD_TOKEN fehlt – nur das Dashboard wird gestartet.")
    tasks.append(asyncio.create_task(stop.wait(), name="stop"))

    done, _pending = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
    for t in done:
        if t.get_name() != "stop" and t.exception():
            log.critical("%s beendet mit Fehler", t.get_name(), exc_info=t.exception())
    log.info("Fahre herunter …")
    server.should_exit = True
    if not bot.is_closed():
        await bot.close()
    for t in tasks:
        t.cancel()
    from app.db.base import engine
    await engine.dispose()


def entry() -> None:
    if len(sys.argv) > 1 and sys.argv[1] == "migrate":
        setup_logging()
        run_migrations()
        print("Migrationen ausgeführt.")
    else:
        try:
            asyncio.run(main())
        except KeyboardInterrupt:
            pass


if __name__ == "__main__":
    entry()
