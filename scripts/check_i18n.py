"""Prüft, ob alle im Code verwendeten Übersetzungs-Keys in app/locales/*.json existieren.

    python scripts/check_i18n.py          # Bericht
    python scripts/check_i18n.py --list   # alle gefundenen Keys ausgeben
"""
from __future__ import annotations

import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SRC = [ROOT / "app" / "bot", ROOT / "app" / "services", ROOT / "app" / "core"]
CALL = re.compile(r"""(?:\b_|UserError|NovaCheckFailure|ModerationError|fail|ok|i18n\.t\([^,]+,|raise_key)\(\s*(?:interaction,\s*)?["']([a-z0-9_]+(?:\.[a-z0-9_]+)+)["']""")
KEYWORD = re.compile(r"""(?:title_key|key)=["']([a-z0-9_]+(?:\.[a-z0-9_]+)+)["']""")
# dynamisch zusammengesetzte Keys: Präfix + bekannte Suffixe
DYNAMIC = {
    "mod.action.": ["warn", "timeout", "untimeout", "kick", "ban", "unban", "softban", "note", "unwarn"],
    "help.cat.": ["moderation", "community", "economy", "music", "tickets", "fun", "streamer", "admin"],
    "automod.rule.": ["spam", "flood", "caps", "duplicates", "invites", "links", "scam", "badwords", "mentions", "mass_mentions", "emoji", "new_accounts"],
    "tickets.prio.": ["low", "normal", "high", "urgent"],
    "apps.status.": ["open", "in_review", "accepted", "rejected"],
    "sg.status.": ["pending", "accepted", "denied", "considered"],
    "sg.btn.": ["accepted", "denied", "considered"],
    "ev.st.": ["scheduled", "live", "ended", "cancelled"],
    "quests.": ["daily", "weekly"],
    "tv.": ["rename", "limit", "lock", "kick", "block", "delete", "done_kick", "done_block"],
    "stream.new_": ["video", "short"],
    "stream.msg_": ["hood", "classic", "clean"],
    "gang.rank.": ["leader", "officer", "member"],
    **{f"guide.{t}.": ["title", "text"] for t in ("start", "xp", "coins", "gangs", "streams", "tickets")},
}
ACH = ["first_message", "messages_100", "messages_1000", "messages_10000", "level_10", "level_25", "level_50", "voice_10h", "voice_100h",
       "giveaway_winner", "event_winner", "streak_7", "streak_30", "stream_fan", "rich", "booster", "og_member"]


def flatten(d, prefix=""):
    out = {}
    for k, v in d.items():
        if isinstance(v, dict):
            out.update(flatten(v, f"{prefix}{k}."))
        else:
            out[f"{prefix}{k}"] = v
    return out


def used_keys() -> set[str]:
    keys: set[str] = set()
    for base in SRC:
        for f in base.rglob("*.py"):
            text = f.read_text(encoding="utf-8")
            keys.update(CALL.findall(text))
            keys.update(KEYWORD.findall(text))
    for prefix, items in DYNAMIC.items():
        keys.update(prefix + i for i in items)
    for a in ACH:
        keys.update({f"ach.{a}.name", f"ach.{a}.desc"})
    keys.update({"mod.history_title", "tickets.unlocked", "tickets.log_open", "tickets.log_close"})
    return {k for k in keys if not k.endswith(("_", "."))}


def main() -> int:
    keys = used_keys()
    if "--list" in sys.argv:
        print("\n".join(sorted(keys)))
        return 0
    ok = True
    for lang in ("de", "en"):
        path = ROOT / "app" / "locales" / f"{lang}.json"
        data = flatten(json.loads(path.read_text(encoding="utf-8"))) if path.exists() else {}
        missing = sorted(k for k in keys if k not in data)
        unused = sorted(k for k in data if k not in keys)
        print(f"[{lang}] {len(data)} Keys, {len(missing)} fehlen, {len(unused)} ungenutzt")
        for k in missing:
            print("   fehlt:", k)
        ok = ok and not missing
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
