"""FastAPI-App: Dashboard (SPA), REST-API, WebSocket für Live-Updates."""
from __future__ import annotations

import asyncio
import json
import logging
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request, WebSocket, WebSocketDisconnect
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from app.config import settings
from app.core.events import bus
from app.core.records import capture_error
from app.runtime import get_bot
from app.web import auth, routes_core, routes_features, routes_moderation, routes_owner, routes_twitch
from app.web.common import bot_status
from app.web.deps import access_level
from app.web.security import SecurityHeaders, load_session

log = logging.getLogger("nova.web")
STATIC = Path(__file__).resolve().parent / "static"


def create_app() -> FastAPI:
    app = FastAPI(title=f"{settings.bot_name} Dashboard", docs_url=None, redoc_url=None, openapi_url=None)
    app.add_middleware(SecurityHeaders)
    app.include_router(auth.router)
    app.include_router(routes_core.router)
    app.include_router(routes_moderation.router)
    app.include_router(routes_features.router)
    app.include_router(routes_owner.router)
    app.include_router(routes_twitch.router)
    app.mount("/static", StaticFiles(directory=STATIC), name="static")

    @app.exception_handler(RequestValidationError)
    async def _validation(_r: Request, exc: RequestValidationError):
        return JSONResponse({"detail": "Ungültige Eingabe", "errors": [e.get("msg") for e in exc.errors()][:5]}, status_code=422)

    @app.exception_handler(Exception)
    async def _unhandled(request: Request, exc: Exception):
        err_id = await capture_error(exc, command=f"web:{request.method} {request.url.path}")
        return JSONResponse({"detail": f"Interner Fehler (ID {err_id})"}, status_code=500)

    @app.get("/healthz")
    async def health():
        version = _version()
        try:
            bot = get_bot()
            return {"ok": True, "bot_ready": bot.is_ready(), "version": version}
        except RuntimeError:
            return {"ok": True, "bot_ready": False, "version": version}

    @app.websocket("/ws")
    async def ws(websocket: WebSocket, guild: str = "0"):
        session = await load_session(websocket)  # type: ignore[arg-type]
        origin = websocket.headers.get("origin", "")
        if session is None or (origin and not origin.startswith(settings.dashboard_url) and websocket.headers.get("host", "") not in origin):
            await websocket.close(code=4401)
            return
        gid = int(guild) if guild.isdigit() else 0
        bot = get_bot()
        if gid:
            g = bot.get_guild(gid)
            level = (await access_level(g, session.user_id))[0] if g else None
            if not level:
                await websocket.close(code=4403)
                return
        elif session.user_id not in settings.owner_id_set:
            await websocket.close(code=4403)
            return
        await websocket.accept()
        queue = bus.subscribe(gid)

        async def ticker():
            while True:
                await asyncio.sleep(5)
                payload = {"event": "status", "data": bot_status(bot)}
                if gid and (g := bot.get_guild(gid)):
                    import discord
                    payload["data"]["guild"] = {
                        "members": g.member_count, "online": sum(1 for m in g.members if m.status != discord.Status.offline) if settings.enable_presence_intent else None,
                        "voice_now": sum(len([m for m in vc.members if not m.bot]) for vc in g.voice_channels), "boosts": g.premium_subscription_count}
                await queue.put(payload)

        tick = asyncio.create_task(ticker())
        try:
            while True:
                msg = await queue.get()
                await websocket.send_text(json.dumps(msg, default=str))
        except (WebSocketDisconnect, RuntimeError):
            pass
        finally:
            tick.cancel()
            bus.unsubscribe(gid, queue)

    @app.get("/favicon.ico", include_in_schema=False)
    async def favicon():
        return FileResponse(STATIC / "img" / "logo.svg", media_type="image/svg+xml")

    @app.api_route("/{path:path}", methods=["GET", "HEAD"], include_in_schema=False)
    async def spa(path: str):
        if path.startswith(("api/", "static/", "auth/")):
            raise HTTPException(404)
        return FileResponse(STATIC / "index.html", headers={"Cache-Control": "no-cache"})

    return app


def _version() -> str:
    """Kurz-SHA der per Auto-Update geladenen Version (data/.update.json), sonst „lokal“."""
    import json
    from pathlib import Path

    try:
        return json.loads(Path("data/.update.json").read_text())["sha"][:7]
    except (OSError, ValueError, KeyError):
        return "lokal"
