"""„Mit Twitch verbinden“ für Medias: signierter Einmal-Link → Twitch-OAuth → Streamer-Eintrag wird automatisch angelegt."""
from __future__ import annotations

import logging
from urllib.parse import urlencode

import httpx
from itsdangerous import BadSignature, URLSafeTimedSerializer
from sqlalchemy import or_, select

from app.config import settings
from app.core.guild_config import config
from app.core.records import audit, log_event
from app.db.base import session_scope
from app.db.models import Streamer
from app.services.integrations import get_credentials

log = logging.getLogger("nova.twitch_link")
LINK_TTL = 15 * 60


def _signer(salt: str) -> URLSafeTimedSerializer:
    return URLSafeTimedSerializer(settings.secret_key or "insecure-dev-key", salt=salt)


def make_link(guild_id: int, user_id: int) -> str:
    token = _signer("twitch-link").dumps({"g": str(guild_id), "u": str(user_id)})
    return f"{settings.dashboard_url}/twitch/connect?{urlencode({'t': token})}"


def read_token(token: str, salt: str = "twitch-link") -> tuple[int, int] | None:
    try:
        data = _signer(salt).loads(token, max_age=LINK_TTL)
        return int(data["g"]), int(data["u"])
    except (BadSignature, KeyError, ValueError, TypeError):
        return None


def redirect_uri() -> str:
    return f"{settings.dashboard_url}/twitch/callback"


async def authorize_url(guild_id: int, user_id: int) -> str | None:
    creds = await get_credentials(guild_id, "twitch")
    if not creds.get("client_id"):
        return None
    state = _signer("twitch-state").dumps({"g": str(guild_id), "u": str(user_id)})
    return "https://id.twitch.tv/oauth2/authorize?" + urlencode({
        "client_id": creds["client_id"], "redirect_uri": redirect_uri(), "response_type": "code", "scope": "", "state": state, "force_verify": "true"})


async def exchange(guild_id: int, code: str) -> dict:
    """Code gegen User-Token tauschen, Twitch-Profil lesen, Token sofort wieder widerrufen (wird nicht gespeichert)."""
    creds = await get_credentials(guild_id, "twitch")
    async with httpx.AsyncClient(timeout=15) as c:
        tok = await c.post("https://id.twitch.tv/oauth2/token", data={
            "client_id": creds["client_id"], "client_secret": creds["client_secret"], "code": code,
            "grant_type": "authorization_code", "redirect_uri": redirect_uri()})
        if tok.status_code != 200:
            reason = tok.json().get("message", "") if tok.headers.get("content-type", "").startswith("application/json") else ""
            if "client credentials" in reason.lower() or "client secret" in reason.lower():
                raise RuntimeError("Client-ID und Client-Secret passen nicht zusammen – bitte in der .env / im Dashboard prüfen.")
            raise RuntimeError(f"Twitch-Anmeldung fehlgeschlagen ({tok.status_code}{': ' + reason if reason else ''})")
        access = tok.json()["access_token"]
        me = await c.get("https://api.twitch.tv/helix/users", headers={"Client-Id": creds["client_id"], "Authorization": f"Bearer {access}"})
        await c.post("https://id.twitch.tv/oauth2/revoke", data={"client_id": creds["client_id"], "token": access})
    if me.status_code != 200 or not me.json().get("data"):
        raise RuntimeError("Twitch-Profil konnte nicht geladen werden")
    return me.json()["data"][0]


async def link_streamer(guild_id: int, discord_user_id: int, discord_name: str, tw: dict) -> Streamer:
    """Legt den Streamer an bzw. aktualisiert ihn (ein Twitch-Kanal pro Media)."""
    cfg = await config.get(guild_id, "streamer")
    async with session_scope() as db:
        row = (await db.execute(select(Streamer).where(Streamer.guild_id == guild_id, Streamer.platform == "twitch", or_(
            Streamer.discord_user_id == discord_user_id, Streamer.channel == tw["login"])))).scalars().first()
        if row is None:
            row = Streamer(guild_id=guild_id, platform="twitch", channel=tw["login"], message_template="", rename_live="🔴 LIVE: {title}",
                           rename_offline="⚫ Offline", notify_videos=True, notify_shorts=False, seen_video_ids=[], total_streams=0, is_live=False)
            db.add(row)
        row.channel = tw["login"]
        row.platform_user_id = tw["id"]
        row.display_name = tw.get("display_name") or tw["login"]
        row.avatar_url = tw.get("profile_image_url")
        row.discord_user_id = discord_user_id
        row.live_role_id = cfg.id("media_live_role") or row.live_role_id
        row.enabled = True
        row.last_error = None
        await db.flush()
    await log_event(guild_id, "stream", "twitch_connect", content=f"{discord_name} → twitch.tv/{tw['login']}")
    await audit(guild_id, discord_user_id, discord_name, "twitch.connect", tw["login"], source="bot")
    return row


async def unlink(guild_id: int, discord_user_id: int) -> bool:
    async with session_scope() as db:
        rows = (await db.execute(select(Streamer).where(Streamer.guild_id == guild_id, Streamer.discord_user_id == discord_user_id,
                                                        Streamer.platform == "twitch"))).scalars().all()
        for r in rows:
            await db.delete(r)
    return bool(rows)
