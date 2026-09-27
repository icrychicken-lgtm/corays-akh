"""Symmetrische Verschlüsselung (Fernet) für API-Keys und OAuth-Tokens."""
from __future__ import annotations

import base64
import hashlib
import json
import logging
from typing import Any

from cryptography.fernet import Fernet, InvalidToken

from app.config import settings

log = logging.getLogger("nova.crypto")


def _build_fernet() -> Fernet:
    key = settings.encryption_key.strip()
    if key:
        try:
            return Fernet(key.encode())
        except ValueError:
            log.warning("ENCRYPTION_KEY ist kein gültiger Fernet-Key – leite Key per SHA-256 ab.")
            return Fernet(base64.urlsafe_b64encode(hashlib.sha256(key.encode()).digest()))
    if not settings.secret_key:
        raise RuntimeError("ENCRYPTION_KEY oder SECRET_KEY muss gesetzt sein.")
    log.warning("ENCRYPTION_KEY fehlt – Schlüssel wird aus SECRET_KEY abgeleitet. Bitte eigenen Key setzen!")
    return Fernet(base64.urlsafe_b64encode(hashlib.sha256(("enc:" + settings.secret_key).encode()).digest()))


_fernet: Fernet | None = None


def fernet() -> Fernet:
    global _fernet
    if _fernet is None:
        _fernet = _build_fernet()
    return _fernet


def encrypt(value: str) -> str:
    return fernet().encrypt(value.encode()).decode()


def decrypt(token: str) -> str | None:
    try:
        return fernet().decrypt(token.encode()).decode()
    except (InvalidToken, ValueError):
        return None


def encrypt_json(data: dict[str, Any]) -> str:
    return encrypt(json.dumps(data))


def decrypt_json(token: str) -> dict[str, Any]:
    raw = decrypt(token)
    if not raw:
        return {}
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        return {}


def mask(value: str | None) -> str:
    """Nur die letzten 4 Zeichen zeigen – Secrets verlassen den Server nie im Klartext."""
    if not value:
        return ""
    return "•" * 8 + value[-4:] if len(value) > 6 else "•" * 8
