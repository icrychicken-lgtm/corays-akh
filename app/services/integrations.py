"""API-Zugangsdaten: pro Server verschlüsselt in der DB, Fallback auf globale ENV-Werte.
Secrets werden nie ans Frontend gegeben – nur maskiert."""
from __future__ import annotations

from typing import Any

from app.config import settings
from app.core.crypto import decrypt_json, encrypt_json, mask
from app.db.base import SessionLocal, session_scope
from app.db.models import Integration

PROVIDERS: dict[str, dict[str, Any]] = {
    "twitch": {"name": "Twitch", "fields": ["client_id", "client_secret"], "secret": ["client_secret"],
               "env": {"client_id": "twitch_client_id", "client_secret": "twitch_client_secret"},
               "help": "dev.twitch.tv/console → Application registrieren (Client Credentials)"},
    "youtube": {"name": "YouTube", "fields": ["api_key"], "secret": ["api_key"], "env": {"api_key": "youtube_api_key"},
                "help": "console.cloud.google.com → YouTube Data API v3 → API-Key"},
    "kick": {"name": "Kick", "fields": ["client_id", "client_secret"], "secret": ["client_secret"],
             "env": {"client_id": "kick_client_id", "client_secret": "kick_client_secret"},
             "help": "kick.com/settings/developer → App erstellen"},
    "spotify": {"name": "Spotify", "fields": ["client_id", "client_secret"], "secret": ["client_secret"],
                "env": {"client_id": "spotify_client_id", "client_secret": "spotify_client_secret"},
                "help": "developer.spotify.com/dashboard → App erstellen"},
}


async def get_credentials(guild_id: int | None, provider: str) -> dict[str, str]:
    spec = PROVIDERS[provider]
    data: dict[str, str] = {}
    if guild_id:
        async with SessionLocal() as s:
            row = await s.get(Integration, (guild_id, provider))
        if row:
            data = decrypt_json(row.data_encrypted)
    for key, env_attr in spec["env"].items():
        if not data.get(key):
            data[key] = getattr(settings, env_attr, "") or ""
    return data


async def save_credentials(guild_id: int, provider: str, values: dict[str, str], user_id: int) -> None:
    spec = PROVIDERS[provider]
    async with session_scope() as s:
        row = await s.get(Integration, (guild_id, provider))
        current = decrypt_json(row.data_encrypted) if row else {}
        for key in spec["fields"]:
            val = (values.get(key) or "").strip()
            if val == "__clear__":
                current.pop(key, None)
            elif val:
                current[key] = val[:300]
        if row:
            row.data_encrypted = encrypt_json(current)
            row.updated_by = user_id
        else:
            s.add(Integration(guild_id=guild_id, provider=provider, data_encrypted=encrypt_json(current), updated_by=user_id))


async def public_status(guild_id: int) -> list[dict[str, Any]]:
    out = []
    async with SessionLocal() as s:
        for key, spec in PROVIDERS.items():
            row = await s.get(Integration, (guild_id, key))
            stored = decrypt_json(row.data_encrypted) if row else {}
            fields = []
            for f in spec["fields"]:
                own = stored.get(f)
                env = getattr(settings, spec["env"][f], "")
                fields.append({"key": f, "configured": bool(own or env), "source": "server" if own else ("global" if env else None),
                               "preview": mask(own) if own and f in spec["secret"] else (own or "")})
            out.append({"provider": key, "name": spec["name"], "help": spec["help"], "fields": fields,
                        "configured": all(x["configured"] for x in fields)})
    return out
