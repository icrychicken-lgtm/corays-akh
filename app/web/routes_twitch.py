"""Öffentliche Seiten für „Mit Twitch verbinden“ (kein Dashboard-Login nötig – abgesichert über signierte, kurzlebige Links)."""
from __future__ import annotations

import html
import logging

import discord
from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from app.core.embeds import theme
from app.core.guild_config import config
from app.runtime import get_bot
from app.services import twitch_link
from app.web.security import client_ip, rate_limit

log = logging.getLogger("nova.twitch_web")
router = APIRouter()


def page(title: str, text: str, ok: bool = True) -> HTMLResponse:
    color = "#c8a45d" if ok else "#d25a5a"
    body = f"""<!doctype html><html lang="de"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>{html.escape(title)}</title><style>
body{{margin:0;min-height:100vh;display:grid;place-items:center;background:#0a0a0b;color:#ececec;font:15px/1.6 system-ui,-apple-system,Segoe UI,Roboto,sans-serif}}
.c{{width:min(420px,90vw);padding:36px 30px;background:#111113;border:1px solid #212125;border-radius:14px;text-align:center}}
.d{{width:46px;height:46px;border-radius:50%;margin:0 auto 16px;border:2px solid {color};display:grid;place-items:center;color:{color};font-size:22px}}
h1{{font-size:20px;margin:0 0 8px;font-weight:600}} p{{color:#a1a1a9;margin:0}}</style></head>
<body><div class="c"><div class="d">{"✓" if ok else "!"}</div><h1>{html.escape(title)}</h1><p>{html.escape(text)}</p></div></body></html>"""
    return HTMLResponse(body, status_code=200 if ok else 400)


async def _media_check(guild_id: int, user_id: int) -> tuple[discord.Guild | None, discord.Member | None, str | None]:
    guild = get_bot().get_guild(guild_id)
    if guild is None:
        return None, None, "Der Server wurde nicht gefunden."
    member = guild.get_member(user_id)
    if member is None:
        return guild, None, "Du bist nicht (mehr) auf dem Server."
    cfg = await config.get(guild_id, "streamer")
    role_id = cfg.id("media_role")
    if role_id and not member.get_role(role_id) and not member.guild_permissions.manage_guild:
        return guild, member, "Nur Mitglieder mit der Media-Rolle können sich verbinden."
    return guild, member, None


@router.get("/twitch/connect")
async def connect(request: Request, t: str = ""):
    await rate_limit(f"twitch:{client_ip(request)}", 20, 60)
    ids = twitch_link.read_token(t)
    if ids is None:
        return page("Link abgelaufen", "Klick in Discord nochmal auf „Mit Twitch verbinden“ – der Link ist nur 15 Minuten gültig.", ok=False)
    _g, _m, err = await _media_check(*ids)
    if err:
        return page("Nicht möglich", err, ok=False)
    url = await twitch_link.authorize_url(*ids)
    if url is None:
        return page("Twitch nicht eingerichtet", "Der Server-Admin muss zuerst Twitch-Zugangsdaten im Dashboard hinterlegen.", ok=False)
    return RedirectResponse(url, status_code=302)


@router.get("/twitch/callback")
async def callback(request: Request, code: str = "", state: str = "", error: str = ""):
    await rate_limit(f"twitch:{client_ip(request)}", 20, 60)
    if error or not code:
        return page("Abgebrochen", "Die Verbindung mit Twitch wurde abgebrochen.", ok=False)
    ids = twitch_link.read_token(state, salt="twitch-state")
    if ids is None:
        return page("Link abgelaufen", "Bitte starte die Verbindung in Discord nochmal.", ok=False)
    guild, member, err = await _media_check(*ids)
    if err:
        return page("Nicht möglich", err, ok=False)
    try:
        tw = await twitch_link.exchange(guild.id, code)
        s = await twitch_link.link_streamer(guild.id, member.id, str(member), tw)
    except RuntimeError as exc:
        log.warning("Twitch-Verbindung fehlgeschlagen: %s", exc)
        return page("Fehler", f"Die Verbindung mit Twitch hat nicht geklappt: {exc}", ok=False)
    except Exception as exc:  # noqa: BLE001
        log.warning("Twitch-Verbindung fehlgeschlagen: %s", exc)
        return page("Fehler", "Die Verbindung mit Twitch hat nicht geklappt. Bitte versuch es nochmal.", ok=False)
    try:
        th = await theme(guild)
        e = th.success("Twitch verbunden", f"**twitch.tv/{s.channel}** ist jetzt mit **{guild.name}** verbunden.\nSobald du live gehst, wird dein Stream automatisch angekündigt. 🔴")
        if s.avatar_url:
            e.set_thumbnail(url=s.avatar_url)
        await member.send(embed=e)
    except discord.HTTPException:
        pass
    return page("Verbunden!", f"twitch.tv/{tw['login']} ist jetzt mit {guild.name} verbunden. Du kannst dieses Fenster schließen.")
