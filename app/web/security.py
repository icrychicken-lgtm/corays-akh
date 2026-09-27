"""Dashboard-Sicherheit: Sessions (serverseitig), CSRF, Origin-Prüfung, Rate-Limits, Security-Header."""
from __future__ import annotations

import secrets
import time
from datetime import timedelta
from urllib.parse import urlparse

from fastapi import HTTPException, Request
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import Response

from app.config import settings
from app.core.cache import cache
from app.db.base import SessionLocal, session_scope, utcnow
from app.db.models import DashboardSession

SESSION_COOKIE = "nova_session"
SESSION_TTL = timedelta(days=7)
SAFE_METHODS = {"GET", "HEAD", "OPTIONS"}


async def create_session(user: dict, access_token_enc: str, ip: str | None) -> DashboardSession:
    sid = secrets.token_urlsafe(48)
    async with session_scope() as db:
        s = DashboardSession(id=sid, user_id=int(user["id"]), username=user.get("global_name") or user["username"],
                             avatar=_avatar(user), access_token_enc=access_token_enc, csrf_token=secrets.token_urlsafe(32),
                             ip=(ip or "")[:64], expires_at=utcnow() + SESSION_TTL)
        db.add(s)
    return s


def _avatar(user: dict) -> str:
    if user.get("avatar"):
        ext = "gif" if user["avatar"].startswith("a_") else "png"
        return f"https://cdn.discordapp.com/avatars/{user['id']}/{user['avatar']}.{ext}?size=128"
    return f"https://cdn.discordapp.com/embed/avatars/{(int(user['id']) >> 22) % 6}.png"


async def load_session(request: Request) -> DashboardSession | None:
    sid = request.cookies.get(SESSION_COOKIE)
    if not sid or len(sid) > 96:
        return None
    cached = getattr(request.state, "nova_session", None)
    if cached is not None:
        return cached
    async with SessionLocal() as db:
        s = await db.get(DashboardSession, sid)
    if s is None or s.expires_at < utcnow():
        return None
    if (utcnow() - s.last_seen).total_seconds() > 300:
        async with session_scope() as db:
            row = await db.get(DashboardSession, sid)
            if row:
                row.last_seen = utcnow()
    request.state.nova_session = s
    return s


async def destroy_session(sid: str | None) -> None:
    if not sid:
        return
    async with session_scope() as db:
        s = await db.get(DashboardSession, sid)
        if s:
            await db.delete(s)


def set_session_cookie(response: Response, sid: str) -> None:
    response.set_cookie(SESSION_COOKIE, sid, max_age=int(SESSION_TTL.total_seconds()), httponly=True,
                        secure=settings.cookie_secure, samesite="lax", path="/")


def client_ip(request: Request) -> str:
    fwd = request.headers.get("x-forwarded-for")
    return (fwd.split(",")[0].strip() if fwd else (request.client.host if request.client else "?"))[:64]


async def check_csrf(request: Request, session: DashboardSession) -> None:
    if request.method in SAFE_METHODS:
        return
    token = request.headers.get("x-csrf-token", "")
    if not token or not secrets.compare_digest(token, session.csrf_token):
        raise HTTPException(403, "CSRF-Token ungültig")
    origin = request.headers.get("origin") or request.headers.get("referer")
    if origin:
        allowed = urlparse(settings.dashboard_url)
        o = urlparse(origin)
        if (o.scheme, o.netloc) != (allowed.scheme, allowed.netloc) and o.netloc != request.headers.get("host"):
            raise HTTPException(403, "Ungültige Herkunft")


async def rate_limit(key: str, limit: int, window: int) -> None:
    bucket = f"rl:{key}:{int(time.time() // window)}"
    count = await cache.incr(bucket, window + 1)
    if count > limit:
        raise HTTPException(429, "Zu viele Anfragen – bitte kurz warten.")


class SecurityHeaders(BaseHTTPMiddleware):
    CSP = ("default-src 'self'; script-src 'self' https://cdn.jsdelivr.net; style-src 'self' 'unsafe-inline' https://fonts.googleapis.com; "
           "font-src 'self' https://fonts.gstatic.com; img-src 'self' data: https:; connect-src 'self' ws: wss:; "
           "frame-ancestors 'none'; base-uri 'self'; form-action 'self' https://discord.com")

    async def dispatch(self, request: Request, call_next):
        # Grobes globales Rate-Limit pro IP
        if request.url.path.startswith(("/api", "/auth")):
            try:
                await rate_limit(f"ip:{client_ip(request)}", 300, 60)
            except HTTPException as exc:
                return Response(exc.detail, status_code=429)
        response = await call_next(request)
        if request.url.path.startswith("/static/"):
            # Immer revalidieren (ETag → 304): nach Updates lädt jeder Browser sofort die neuen JS/CSS-Dateien
            response.headers["Cache-Control"] = "no-cache"
        response.headers.setdefault("X-Content-Type-Options", "nosniff")
        response.headers.setdefault("X-Frame-Options", "DENY")
        response.headers.setdefault("Referrer-Policy", "strict-origin-when-cross-origin")
        response.headers.setdefault("Permissions-Policy", "camera=(), microphone=(), geolocation=()")
        if not request.url.path.startswith("/api/transcripts"):
            response.headers.setdefault("Content-Security-Policy", self.CSP)
        if settings.cookie_secure:
            response.headers.setdefault("Strict-Transport-Security", "max-age=31536000; includeSubDomains")
        return response
