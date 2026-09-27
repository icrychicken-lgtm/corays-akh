"""Twitch-Chat anonym mitlesen (kein Token, kein HTTPS nötig):
- Kanal bestätigen: Der Kanalbesitzer schreibt einen Code in seinen eigenen Chat.
- Chat-Wächter: Nachrichten im Live-Chat auswerten (Statistik, Warnwörter für Mods)."""
from __future__ import annotations

import asyncio
import logging
import random
import secrets

log = logging.getLogger("nova.twitch_chat")
HOST, PORT = "irc.chat.twitch.tv", 6697


def new_code() -> str:
    return f"AKH-{secrets.randbelow(9000) + 1000}"


def _tags(raw: str) -> dict[str, str]:
    out: dict[str, str] = {}
    for part in raw.lstrip("@").split(";"):
        key, _, value = part.partition("=")
        out[key] = value
    return out


async def wait_for_code(login: str, broadcaster_id: str, code: str, timeout: float) -> bool:
    """True, sobald der Kanalbesitzer `code` in seinen eigenen Chat schreibt; False bei Timeout."""
    try:
        return await asyncio.wait_for(_listen(login.lower(), broadcaster_id, code.upper()), timeout)
    except asyncio.TimeoutError:
        return False


async def read_chat(login: str, on_message) -> None:
    """Liest den Chat von `login` anonym mit und ruft `await on_message(tags, text)` für jede Nachricht auf.
    Läuft, bis der Task abgebrochen wird oder `on_message` True zurückgibt; verbindet bei Abbrüchen neu."""
    login = login.lower()
    while True:
        try:
            reader, writer = await asyncio.open_connection(HOST, PORT, ssl=True)
        except OSError as exc:
            log.warning("Twitch-Chat nicht erreichbar: %s", exc)
            await asyncio.sleep(10)
            continue
        try:
            nick = f"justinfan{random.randint(10000, 99999)}"
            writer.write(f"CAP REQ :twitch.tv/tags\r\nPASS SCHMOOPIIE\r\nNICK {nick}\r\nJOIN #{login}\r\n".encode())
            await writer.drain()
            while line := (await reader.readline()).decode("utf-8", "ignore").rstrip("\r\n"):
                if line.startswith("PING"):
                    writer.write(line.replace("PING", "PONG", 1).encode() + b"\r\n")
                    await writer.drain()
                    continue
                if " PRIVMSG " not in line or not line.startswith("@"):
                    continue
                raw_tags, _, rest = line.partition(" ")
                text = rest.split(" :", 1)[1] if " :" in rest else ""
                if await on_message(_tags(raw_tags), text):
                    return
        except (OSError, ConnectionError) as exc:
            log.info("Twitch-Chat getrennt, verbinde neu: %s", exc)
        finally:
            writer.close()
        await asyncio.sleep(3)


async def _listen(login: str, broadcaster_id: str, code: str) -> bool:
    async def check(tags: dict[str, str], text: str) -> bool:
        return tags.get("user-id") == broadcaster_id and code in text.upper()

    await read_chat(login, check)
    return True
