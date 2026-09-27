"""Logging: farbige Konsole + rotierende Log-Dateien."""
from __future__ import annotations

import logging
import sys
from logging.handlers import RotatingFileHandler

from app.config import settings

_COLORS = {"DEBUG": "\033[90m", "INFO": "\033[36m", "WARNING": "\033[33m", "ERROR": "\033[31m", "CRITICAL": "\033[41m"}


class _ConsoleFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        color = _COLORS.get(record.levelname, "")
        ts = self.formatTime(record, "%H:%M:%S")
        msg = f"\033[90m{ts}\033[0m {color}{record.levelname:<8}\033[0m \033[35m{record.name}\033[0m {record.getMessage()}"
        if record.exc_info:
            msg += "\n" + self.formatException(record.exc_info)
        return msg


def setup_logging() -> None:
    log_dir = settings.path(settings.log_dir)
    log_dir.mkdir(parents=True, exist_ok=True)
    level = getattr(logging, settings.log_level.upper(), logging.INFO)

    root = logging.getLogger()
    root.setLevel(level)
    root.handlers.clear()

    console = logging.StreamHandler(sys.stdout)
    console.setFormatter(_ConsoleFormatter())
    root.addHandler(console)

    file_handler = RotatingFileHandler(log_dir / "nova.log", maxBytes=10 * 1024 * 1024, backupCount=5, encoding="utf-8")
    file_handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)-8s %(name)s: %(message)s"))
    root.addHandler(file_handler)

    for noisy in ("discord.gateway", "discord.http", "httpx", "uvicorn.access", "sqlalchemy.engine"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
    # Windows-Proactor meldet von Clients getrennte Verbindungen als Fehler – reines Rauschen
    logging.getLogger("asyncio").addFilter(
        lambda r: not (r.exc_info and isinstance(r.exc_info[1], ConnectionResetError)))
