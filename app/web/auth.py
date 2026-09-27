"""Discord OAuth2 (Authorization Code Flow) mit State-Prüfung, Login/Logout und Bot-Einladung."""
from __future__ import annotations

import logging
import secrets
from urllib.parse import urlencode

import discord
import httpx
from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import JSONResponse, RedirectResponse
from itsdangerous import BadSignature, URLSafeTimedSerializer

from app.config import settings
from app.core.crypto import encrypt
from app.core.records import audit
from app.web.deps import require_session
from app.web.security import SESSION_COOKIE, client_ip, create_session, destroy_session, rate_limit, set_session_cookie

log = logging.getLogger("nova.auth")
router = APIRouter()
API = "https://discord.com/api/v10"
STATE_COOKIE = "nova_oauth"


def _signer() -> URLSafeTimedSerializer:
    return URLSafeTimedSerializer(settings.secret_key or "insecure-dev-key", salt="oauth-state")


def bot_permissions() -> int:
    p = discord.Permissions(
        manage_roles=True, manage_channels=True, kick_members=True, ban_members=True, moderate_members=True, manage_messages=True,
        manage_nicknames=True, view_audit_log=True, manage_guild=True, view_channel=True, send_messages=True, embed_links=True,
        attach_files=True, read_message_history=True, add_reactions=True, connect=True, speak=True, move_members=True,
        manage_threads=True, create_public_threads=True, send_messages_in_threads=True, use_external_emojis=True, manage_events=True,
        mention_everyone=True,
    )
    return p.value


@router.get("/auth/login")
async def login(request: Request):
    await rate_limit(f"login:{client_ip(request)}", 20, 60)
    if not settings.discord_client_id or not settings.discord_client_secret:
        raise HTTPException(500, "DISCORD_CLIENT_ID / DISCORD_CLIENT_SECRET fehlen in der .env")
    state = secrets.token_urlsafe(24)
    params = {"client_id": settings.discord_client_id, "redirect_uri": settings.oauth_redirect_uri, "response_type": "code",
              "scope": "identify guilds", "state": state, "prompt": "none"}
    resp = RedirectResponse(f"https://discord.com/oauth2/authorize?{urlencode(params)}", status_code=302)
    resp.set_cookie(STATE_COOKIE, _signer().dumps(state), max_age=600, httponly=True, secure=settings.cookie_secure, samesite="lax")
    return resp


@router.get("/auth/callback")
async def callback(request: Request, code: str | None = None, state: str | None = None, error: str | None = None):
    await rate_limit(f"login:{client_ip(request)}", 20, 60)
    if error or not code or not state:
        return RedirectResponse("/?login=cancelled")
    try:
        expected = _signer().loads(request.cookies.get(STATE_COOKIE, ""), max_age=600)
    except BadSignature:
        raise HTTPException(400, "Ungültiger OAuth-State – bitte erneut anmelden")
    if not secrets.compare_digest(expected, state):
        raise HTTPException(400, "OAuth-State stimmt nicht überein")
    async with httpx.AsyncClient(timeout=15) as c:
        tok = await c.post(f"{API}/oauth2/token", data={
            "client_id": settings.discord_client_id, "client_secret": settings.discord_client_secret, "grant_type": "authorization_code",
            "code": code, "redirect_uri": settings.oauth_redirect_uri}, headers={"Content-Type": "application/x-www-form-urlencoded"})
        if tok.status_code != 200:
            log.warning("OAuth-Token-Austausch fehlgeschlagen: %s %s", tok.status_code, tok.text[:200])
            raise HTTPException(400, "Anmeldung bei Discord fehlgeschlagen")
        access = tok.json()["access_token"]
        me = await c.get(f"{API}/users/@me", headers={"Authorization": f"Bearer {access}"})
        if me.status_code != 200:
            raise HTTPException(400, "Discord-Profil konnte nicht geladen werden")
    user = me.json()
    session = await create_session(user, encrypt(access), client_ip(request))
    await audit(None, int(user["id"]), user.get("username", "?"), "dashboard.login", client_ip(request), source="dashboard")
    resp = RedirectResponse("/", status_code=302)
    set_session_cookie(resp, session.id)
    resp.delete_cookie(STATE_COOKIE)
    return resp


@router.post("/auth/logout")
async def logout(request: Request, _s=Depends(require_session)):
    await destroy_session(request.cookies.get(SESSION_COOKIE))
    resp = JSONResponse({"ok": True})
    resp.delete_cookie(SESSION_COOKIE, path="/")
    return resp


@router.get("/invite")
async def invite(guild_id: str | None = None):
    params = {"client_id": settings.discord_client_id, "scope": "bot applications.commands", "permissions": str(bot_permissions())}
    if guild_id and guild_id.isdigit():
        params.update(guild_id=guild_id, disable_guild_select="true")
    return RedirectResponse(f"https://discord.com/oauth2/authorize?{urlencode(params)}")
