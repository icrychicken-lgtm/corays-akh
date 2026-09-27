"""Unit-Tests für Kernlogik, die ohne Discord-Verbindung prüfbar ist.

    pip install -r requirements-dev.txt
    pytest -q
"""
from __future__ import annotations

import asyncio
import json
import os
import re
import string
from pathlib import Path
from types import SimpleNamespace

os.environ.setdefault("DATABASE_URL", "sqlite+aiosqlite:///./data/test.db")
os.environ.setdefault("SECRET_KEY", "t" * 40)

import pytest  # noqa: E402

from app.core.schema import MODULES, F, ValidationContext, ValidationError, clean_value, validate_module  # noqa: E402
from app.core.timeutil import human_duration, parse_duration  # noqa: E402
from app.services.progression import level_from_xp, total_for_level, xp_to_next  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent


# ── Zeit ──
@pytest.mark.parametrize("text,expected", [("10m", 600), ("2h30m", 9000), ("1d", 86400), ("1w", 604800), ("45", 2700), ("90s", 90),
                                           ("1 std", 3600), ("", None), ("abc", None), ("5x", None)])
def test_parse_duration(text, expected):
    assert parse_duration(text) == expected


def test_human_duration():
    assert human_duration(3661) == "1h 1min"
    assert human_duration(None) == "—"


# ── Level-Formel ──
def test_levels_roundtrip():
    for lvl in (0, 1, 5, 25, 100):
        total = total_for_level(lvl)
        assert level_from_xp(total)[0] == lvl
        assert level_from_xp(total + xp_to_next(lvl) - 1)[0] == lvl
    assert xp_to_next(0) == 100


# ── Schema / Validierung ──
def _ctx():
    return ValidationContext({"1": "text", "2": "voice", "3": "category"}, ["10", "11"], ["5"])


def test_all_module_defaults_validate():
    """Jede Moduldefinition muss mit ihren eigenen Defaults gültig sein."""
    for spec in MODULES:
        clean = validate_module(spec, spec.defaults(), _ctx())
        assert set(clean) == {f.key for f in spec.fields}, spec.key


def test_channel_type_is_enforced():
    with pytest.raises(ValueError):
        clean_value(F("c", "channel", "c"), "2", _ctx())  # Voice statt Text
    assert clean_value(F("c", "voice", "c"), "2", _ctx()) == "2"
    with pytest.raises(ValueError):
        clean_value(F("r", "role", "r"), "999", _ctx())


def test_validation_errors_are_collected():
    spec = next(m for m in MODULES if m.key == "levels")
    with pytest.raises(ValidationError) as exc:
        validate_module(spec, {"xp_min": -5, "announce": "nope"}, _ctx())
    assert {"xp_min", "announce"} <= set(exc.value.errors)


def test_objlist_and_ids():
    f = F("rules", "objlist", "x", [], fields=[F("role", "role", "r"), F("n", "int", "n", 1, min=1, max=5)])
    assert clean_value(f, [{"role": "10", "n": "3"}], _ctx()) == [{"role": "10", "n": 3}]
    assert clean_value(F("u", "users", "u"), ["123", "123", ""], _ctx()) == ["123"]
    with pytest.raises(ValueError):
        clean_value(F("u", "users", "u"), ["abc"], _ctx())


# ── Übersetzungen ──
def _flatten(d, p=""):
    out = {}
    for k, v in d.items():
        out.update(_flatten(v, f"{p}{k}.")) if isinstance(v, dict) else out.__setitem__(f"{p}{k}", v)
    return out


def test_locales_have_same_keys_and_placeholders():
    de = _flatten(json.loads((ROOT / "app/locales/de.json").read_text(encoding="utf-8")))
    en = _flatten(json.loads((ROOT / "app/locales/en.json").read_text(encoding="utf-8")))
    assert set(de) == set(en)
    fmt = string.Formatter()
    for key in de:
        ph_de = {f for _, f, _, _ in fmt.parse(de[key]) if f}
        ph_en = {f for _, f, _, _ in fmt.parse(en[key]) if f}
        assert ph_de == ph_en, key


def test_fill_is_robust():
    from app.core.i18n import fill
    assert fill("Hi {user} auf {server}", user="@a") == "Hi @a auf {server}"
    assert fill("kaputt { {user}", user="@a") == "kaputt { @a"
    assert fill("", user="x") == ""


def test_all_used_keys_exist():
    import subprocess
    import sys
    r = subprocess.run([sys.executable, str(ROOT / "scripts/check_i18n.py")], capture_output=True, text=True)
    assert r.returncode == 0, r.stdout


# ── Automod ──
def _msg(content: str, mentions=0):
    return SimpleNamespace(content=content, raw_mentions=list(range(mentions)), raw_role_mentions=[],
                           guild=SimpleNamespace(id=1, vanity_url_code=None),
                           author=SimpleNamespace(id=2, created_at=__import__("datetime").datetime(2020, 1, 1, tzinfo=__import__("datetime").timezone.utc)),
                           channel=SimpleNamespace(id=3, permissions_for=lambda _a: SimpleNamespace(mention_everyone=False)))


@pytest.fixture
def automod():
    from app.bot.modules.automod import AutoMod
    return AutoMod(bot=SimpleNamespace())


def _cfg(**over):
    from app.core.guild_config import ModuleConfig
    spec = next(m for m in MODULES if m.key == "automod")
    d = spec.defaults()
    d.update(over)
    return ModuleConfig(d)


@pytest.mark.parametrize("content,rule", [
    ("free nitro here https://dlscord-gift.com/claim", "scam"),
    ("check https://bit.ly/abc", "scam"),
    ("join discord.gg/abcdef", "invites"),
    ("HALLO WIE GEHT ES EUCH ALLEN", "caps"),
    ("aaaaaaaaaaaaaaaaaaaaaaaaaaaaaa", "flood"),
    ("@everyone kommt alle", "mass_mentions"),
])
def test_automod_rules(automod, content, rule):
    cfg = _cfg(caps_enabled=True, invites_allow_own=False)
    hit = asyncio.run(automod.check(_msg(content), cfg))
    assert hit and hit[0] == rule


def test_automod_allows_clean_text(automod):
    assert asyncio.run(automod.check(_msg("Hallo zusammen, schöner Stream heute! https://twitch.tv/test"), _cfg())) is None


def test_automod_badwords_wildcard(automod):
    cfg = _cfg(badwords_enabled=True, badwords_list=["spast*"])
    assert asyncio.run(automod.check(_msg("du sp4stie"), cfg))[0] == "badwords"


def test_automod_mentions(automod):
    assert asyncio.run(automod.check(_msg("hi", mentions=10), _cfg()))[0] == "mentions"


# ── Crypto ──
def test_crypto_roundtrip_and_mask():
    from app.core.crypto import decrypt_json, encrypt_json, mask
    token = encrypt_json({"a": "secret"})
    assert "secret" not in token
    assert decrypt_json(token) == {"a": "secret"}
    assert mask("supersecretvalue").endswith("alue") and "super" not in mask("supersecretvalue")


# ── Scam-Muster dürfen offizielle Domains nicht treffen ──
def test_lookalike_regex_does_not_match_official():
    from app.bot.modules.automod import LOOKALIKE_RE, OFFICIAL
    for d in ("discord.com", "discord.gg", "steamcommunity.com"):
        assert d in OFFICIAL
    assert LOOKALIKE_RE.search("dlscord.gift")
    assert not re.search(LOOKALIKE_RE, "twitch.tv")


def test_split_pot_winners_share_losers_pool():
    from app.bot.modules.streamhub import split_pot
    bets = [(1, 0, 100), (2, 0, 300), (3, 1, 400)]
    assert split_pot(bets, 0) == {1: 200, 2: 600}
    assert split_pot(bets, 2) == {1: 100, 2: 300, 3: 400}  # niemand richtig → Einsatz zurück


def test_streak_days_counts_until_yesterday():
    from datetime import date, timedelta
    from app.bot.modules.streamhub import streak_days
    today = date(2026, 9, 27)
    days = {today - timedelta(days=i) for i in (1, 2, 3)}
    assert streak_days(days, today) == 3
    assert streak_days(days | {today}, today) == 4
    assert streak_days({today - timedelta(days=2)}, today) == 0
