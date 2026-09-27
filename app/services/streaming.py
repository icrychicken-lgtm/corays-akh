"""Plattform-Clients für Twitch (Helix), YouTube (Data API v3 + RSS) und Kick (offizielle Public API).
Tokens werden gecacht, Requests gebündelt, um Rate-Limits und Quotas zu schonen."""
from __future__ import annotations

import logging
import time
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

import httpx

log = logging.getLogger("nova.streaming")
TIMEOUT = httpx.Timeout(12.0)


class PlatformError(Exception):
    pass


@dataclass
class StreamInfo:
    live: bool
    stream_id: str | None = None
    title: str = ""
    game: str = ""
    viewers: int = 0
    started_at: datetime | None = None
    thumbnail: str | None = None
    url: str = ""


@dataclass
class ChannelInfo:
    user_id: str
    display_name: str
    avatar: str | None = None
    followers: int | None = None
    subscribers: int | None = None


@dataclass
class VideoInfo:
    video_id: str
    title: str
    url: str
    thumbnail: str | None
    kind: str  # video | short | live | upcoming
    published: datetime | None = None
    viewers: int = 0
    started_at: datetime | None = None
    ended: bool = False


def _dt(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


# ───────────────────────── Twitch ─────────────────────────
class TwitchClient:
    def __init__(self) -> None:
        self._tokens: dict[str, tuple[str, float]] = {}

    async def _token(self, cid: str, secret: str) -> str:
        hit = self._tokens.get(cid)
        if hit and hit[1] > time.time():
            return hit[0]
        async with httpx.AsyncClient(timeout=TIMEOUT) as c:
            r = await c.post("https://id.twitch.tv/oauth2/token",
                             params={"client_id": cid, "client_secret": secret, "grant_type": "client_credentials"})
        if r.status_code != 200:
            raise PlatformError(f"Twitch-Auth fehlgeschlagen ({r.status_code})")
        data = r.json()
        self._tokens[cid] = (data["access_token"], time.time() + data.get("expires_in", 3600) - 120)
        return data["access_token"]

    async def _get(self, creds: dict[str, str], path: str, params: list[tuple[str, str]]) -> httpx.Response:
        if not creds.get("client_id") or not creds.get("client_secret"):
            raise PlatformError("Twitch-Zugangsdaten fehlen (Integrationen)")
        token = await self._token(creds["client_id"], creds["client_secret"])
        headers = {"Client-Id": creds["client_id"], "Authorization": f"Bearer {token}"}
        async with httpx.AsyncClient(timeout=TIMEOUT, headers=headers) as c:
            r = await c.get(f"https://api.twitch.tv/helix/{path}", params=params)
        if r.status_code == 401:
            self._tokens.pop(creds["client_id"], None)
        return r

    async def users(self, creds: dict[str, str], logins: list[str]) -> dict[str, ChannelInfo]:
        out: dict[str, ChannelInfo] = {}
        for i in range(0, len(logins), 100):
            r = await self._get(creds, "users", [("login", l.lower()) for l in logins[i:i + 100]])
            if r.status_code != 200:
                raise PlatformError(f"Twitch users: {r.status_code}")
            for u in r.json().get("data", []):
                out[u["login"].lower()] = ChannelInfo(u["id"], u["display_name"], u.get("profile_image_url"))
        return out

    async def streams(self, creds: dict[str, str], logins: list[str]) -> dict[str, StreamInfo]:
        out = {l.lower(): StreamInfo(live=False, url=f"https://twitch.tv/{l}") for l in logins}
        for i in range(0, len(logins), 100):
            r = await self._get(creds, "streams", [("user_login", l.lower()) for l in logins[i:i + 100]] + [("first", "100")])
            if r.status_code != 200:
                raise PlatformError(f"Twitch streams: {r.status_code}")
            for s in r.json().get("data", []):
                login = s["user_login"].lower()
                thumb = (s.get("thumbnail_url") or "").replace("{width}", "1280").replace("{height}", "720")
                out[login] = StreamInfo(True, s["id"], s.get("title", ""), s.get("game_name", ""), int(s.get("viewer_count", 0)),
                                        _dt(s.get("started_at")), f"{thumb}?t={int(time.time())}" if thumb else None, f"https://twitch.tv/{login}")
        return out

    async def clips(self, creds: dict[str, str], broadcaster_id: str, since: datetime) -> list[dict[str, Any]]:
        """Neue Clips seit `since` (funktioniert mit App-Zugangsdaten)."""
        r = await self._get(creds, "clips", [("broadcaster_id", broadcaster_id), ("first", "20"),
                                             ("started_at", since.strftime("%Y-%m-%dT%H:%M:%SZ"))])
        if r.status_code != 200:
            raise PlatformError(f"Twitch clips: {r.status_code}")
        return r.json().get("data", [])

    async def schedule(self, creds: dict[str, str], broadcaster_id: str) -> list[dict[str, Any]]:
        r = await self._get(creds, "schedule", [("broadcaster_id", broadcaster_id), ("first", "10")])
        if r.status_code == 404:
            return []  # Streamer hat keinen Plan hinterlegt
        if r.status_code != 200:
            raise PlatformError(f"Twitch schedule: {r.status_code}")
        segs = (r.json().get("data") or {}).get("segments") or []
        return [s for s in segs if not s.get("canceled_until")]

    async def latest_vod(self, creds: dict[str, str], user_id: str) -> dict[str, Any] | None:
        """Neuestes Archiv-Video (VOD) – funktioniert mit App-Zugangsdaten."""
        r = await self._get(creds, "videos", [("user_id", user_id), ("type", "archive"), ("first", "1")])
        if r.status_code != 200:
            return None
        data = r.json().get("data") or []
        return data[0] if data else None

    async def channel_info(self, creds: dict[str, str], user_id: str) -> dict[str, Any] | None:
        """Aktueller/letzter Titel und Kategorie eines Kanals (auch offline)."""
        r = await self._get(creds, "channels", [("broadcaster_id", user_id)])
        if r.status_code != 200:
            return None
        data = r.json().get("data") or []
        return data[0] if data else None

    async def followers(self, creds: dict[str, str], user_id: str) -> int | None:
        """Nur mit Broadcaster-/Moderator-User-Token vollständig verfügbar – mit App-Token liefert Twitch ggf. 401."""
        try:
            r = await self._get(creds, "channels/followers", [("broadcaster_id", user_id), ("first", "1")])
            if r.status_code == 200:
                return int(r.json().get("total", 0))
        except PlatformError:
            pass
        return None


# ───────────────────────── YouTube ─────────────────────────
class YouTubeClient:
    API = "https://www.googleapis.com/youtube/v3"

    async def _get(self, key: str, path: str, params: dict[str, str]) -> dict[str, Any]:
        if not key:
            raise PlatformError("YouTube-API-Key fehlt (Integrationen)")
        async with httpx.AsyncClient(timeout=TIMEOUT) as c:
            r = await c.get(f"{self.API}/{path}", params={**params, "key": key})
        if r.status_code != 200:
            raise PlatformError(f"YouTube {path}: {r.status_code} {r.text[:120]}")
        return r.json()

    async def channel(self, key: str, ident: str) -> ChannelInfo:
        params = {"part": "snippet,statistics"}
        if ident.startswith("UC") and len(ident) == 24:
            params["id"] = ident
        else:
            params["forHandle"] = ident if ident.startswith("@") else f"@{ident}"
        data = await self._get(key, "channels", params)
        items = data.get("items") or []
        if not items:
            raise PlatformError("YouTube-Kanal nicht gefunden")
        it = items[0]
        stats = it.get("statistics", {})
        subs = None if stats.get("hiddenSubscriberCount") else int(stats.get("subscriberCount", 0))
        thumb = (it["snippet"].get("thumbnails", {}).get("high") or it["snippet"].get("thumbnails", {}).get("default") or {}).get("url")
        return ChannelInfo(it["id"], it["snippet"]["title"], thumb, None, subs)

    async def recent_video_ids(self, channel_id: str, limit: int = 5) -> list[str]:
        """RSS-Feed – kostet keine API-Quota."""
        async with httpx.AsyncClient(timeout=TIMEOUT) as c:
            r = await c.get("https://www.youtube.com/feeds/videos.xml", params={"channel_id": channel_id})
        if r.status_code != 200:
            raise PlatformError(f"YouTube-RSS: {r.status_code}")
        ns = {"a": "http://www.w3.org/2005/Atom", "yt": "http://www.youtube.com/xml/schemas/2015"}
        root = ET.fromstring(r.text)
        return [e.findtext("yt:videoId", namespaces=ns) for e in root.findall("a:entry", ns)][:limit]

    async def videos(self, key: str, ids: list[str]) -> list[VideoInfo]:
        if not ids:
            return []
        data = await self._get(key, "videos", {"part": "snippet,liveStreamingDetails,contentDetails", "id": ",".join(ids)})
        out = []
        for it in data.get("items", []):
            sn = it["snippet"]
            live = it.get("liveStreamingDetails") or {}
            lbc = sn.get("liveBroadcastContent", "none")
            thumbs = sn.get("thumbnails", {})
            thumb = (thumbs.get("maxres") or thumbs.get("high") or thumbs.get("default") or {}).get("url")
            kind = "live" if lbc == "live" else "upcoming" if lbc == "upcoming" else "video"
            if kind == "video" and _iso_seconds(it.get("contentDetails", {}).get("duration", "")) <= 180 and not live:
                kind = "short" if await self._is_short(it["id"]) else "video"
            out.append(VideoInfo(it["id"], sn.get("title", ""), f"https://www.youtube.com/watch?v={it['id']}", thumb, kind,
                                 _dt(sn.get("publishedAt")), int(live.get("concurrentViewers", 0) or 0),
                                 _dt(live.get("actualStartTime")), bool(live.get("actualEndTime"))))
        return out

    async def _is_short(self, video_id: str) -> bool:
        try:
            async with httpx.AsyncClient(timeout=TIMEOUT, follow_redirects=False) as c:
                r = await c.head(f"https://www.youtube.com/shorts/{video_id}")
            return r.status_code == 200
        except httpx.HTTPError:
            return False


def _iso_seconds(value: str) -> int:
    import re

    m = re.fullmatch(r"P(?:(\d+)D)?T?(?:(\d+)H)?(?:(\d+)M)?(?:(\d+)S)?", value or "")
    if not m:
        return 10**9
    d, h, mi, s = (int(x or 0) for x in m.groups())
    return d * 86400 + h * 3600 + mi * 60 + s


# ───────────────────────── Kick ─────────────────────────
class KickClient:
    def __init__(self) -> None:
        self._tokens: dict[str, tuple[str, float]] = {}

    async def _token(self, cid: str, secret: str) -> str:
        hit = self._tokens.get(cid)
        if hit and hit[1] > time.time():
            return hit[0]
        async with httpx.AsyncClient(timeout=TIMEOUT) as c:
            r = await c.post("https://id.kick.com/oauth/token",
                             data={"grant_type": "client_credentials", "client_id": cid, "client_secret": secret})
        if r.status_code != 200:
            raise PlatformError(f"Kick-Auth fehlgeschlagen ({r.status_code})")
        data = r.json()
        self._tokens[cid] = (data["access_token"], time.time() + int(data.get("expires_in", 3600)) - 120)
        return data["access_token"]

    async def channel(self, creds: dict[str, str], slug: str) -> tuple[ChannelInfo, StreamInfo]:
        if not creds.get("client_id") or not creds.get("client_secret"):
            raise PlatformError("Kick-Zugangsdaten fehlen (Integrationen)")
        token = await self._token(creds["client_id"], creds["client_secret"])
        async with httpx.AsyncClient(timeout=TIMEOUT, headers={"Authorization": f"Bearer {token}"}) as c:
            r = await c.get("https://api.kick.com/public/v1/channels", params={"slug": slug.lower()})
            if r.status_code != 200:
                raise PlatformError(f"Kick channels: {r.status_code}")
            items = r.json().get("data") or []
            if not items:
                raise PlatformError("Kick-Kanal nicht gefunden")
            ch = items[0]
            avatar = None
            uid = str(ch.get("broadcaster_user_id") or "")
            if uid:
                u = await c.get("https://api.kick.com/public/v1/users", params={"id": uid})
                if u.status_code == 200 and u.json().get("data"):
                    avatar = u.json()["data"][0].get("profile_picture")
        stream = ch.get("stream") or {}
        info = ChannelInfo(uid, ch.get("slug", slug), avatar)
        live = bool(stream.get("is_live"))
        started = _dt(stream.get("start_time")) if live else None
        sid = f"{uid}-{int(started.timestamp())}" if started else None
        return info, StreamInfo(live, sid, ch.get("stream_title", ""), (ch.get("category") or {}).get("name", ""),
                                int(stream.get("viewer_count", 0) or 0), started, stream.get("thumbnail"), f"https://kick.com/{slug}")


twitch = TwitchClient()
youtube = YouTubeClient()
kick = KickClient()
