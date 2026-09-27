"""Auto-Update beim Start: holt die neueste Version aus dem (privaten) GitHub-Repo und entpackt sie über den Code.

Nur Standardbibliothek – läuft, bevor irgendetwas aus `app` importiert wird.
Nie angefasst werden: .env, data/, logs/, backups/ (Token, Datenbank, Einstellungen bleiben auf dem Server).

Einrichtung in der .env:
    UPDATE_REPO=besitzer/repo
    UPDATE_TOKEN=github_pat_...      (Fine-grained Token, nur „Contents: Read-only“ für dieses Repo)
    UPDATE_BRANCH=main               (optional)
"""
from __future__ import annotations

import hashlib
import io
import json
import os
import shutil
import subprocess
import sys
import urllib.request
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent
STATE = ROOT / "data" / ".update.json"
PROTECTED = {".env", "data", "logs", "backups", ".git", ".venv", "venv"}
SYNC_DIRS = ("app", "migrations", "scripts", "docs")  # hier werden auch gelöschte Dateien entfernt


def _env() -> dict[str, str]:
    values = dict(os.environ)
    env_file = ROOT / ".env"
    if env_file.exists():
        for line in env_file.read_text(encoding="utf-8-sig").splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                key, _, value = line.partition("=")
                values.setdefault(key.strip(), value.strip().strip('"').strip("'"))
    return values


def _get(url: str, token: str, accept: str) -> bytes:
    req = urllib.request.Request(url, headers={"Authorization": f"Bearer {token}", "Accept": accept,
                                               "User-Agent": "corays-akh-updater", "X-GitHub-Api-Version": "2022-11-28"})
    with urllib.request.urlopen(req, timeout=60) as resp:
        return resp.read()


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest() if path.exists() else ""


def _protected(rel: Path) -> bool:
    return rel.parts[0] in PROTECTED or "__pycache__" in rel.parts


def update() -> None:
    env = _env()
    repo, token, branch = env.get("UPDATE_REPO", "").strip(), env.get("UPDATE_TOKEN", "").strip(), env.get("UPDATE_BRANCH", "main").strip() or "main"
    if not repo or not token:
        return
    try:
        latest = _get(f"https://api.github.com/repos/{repo}/commits/{branch}", token, "application/vnd.github.sha").decode().strip()
        state = json.loads(STATE.read_text()) if STATE.exists() else {}
        if state.get("sha") == latest:
            print(f"[Update] Aktuell ({latest[:7]})", flush=True)
            return
        print(f"[Update] Neue Version {latest[:7]} wird geladen …", flush=True)
        archive = zipfile.ZipFile(io.BytesIO(_get(f"https://api.github.com/repos/{repo}/zipball/{latest}", token, "application/vnd.github+json")))
        req_before = _sha(ROOT / "requirements.txt")
        incoming: set[Path] = set()
        for info in archive.infolist():
            parts = Path(info.filename).parts[1:]  # oberster Ordner „repo-sha/“ weg
            if not parts or info.is_dir():
                continue
            rel = Path(*parts)
            if _protected(rel):
                continue
            incoming.add(rel)
            target = ROOT / rel
            target.parent.mkdir(parents=True, exist_ok=True)
            with archive.open(info) as src, open(target, "wb") as dst:
                shutil.copyfileobj(src, dst)
        removed = 0
        for top in SYNC_DIRS:
            for path in (ROOT / top).rglob("*"):
                rel = path.relative_to(ROOT)
                if path.is_file() and not _protected(rel) and rel not in incoming and path.suffix != ".pyc":
                    path.unlink()
                    removed += 1
        if _sha(ROOT / "requirements.txt") != req_before:
            print("[Update] Neue Pakete werden installiert …", flush=True)
            subprocess.run([sys.executable, "-m", "pip", "install", "-q", "-r", str(ROOT / "requirements.txt")], check=False)
        STATE.parent.mkdir(parents=True, exist_ok=True)
        STATE.write_text(json.dumps({"sha": latest}))
        print(f"[Update] Fertig: {len(incoming)} Dateien aktualisiert, {removed} entfernt.", flush=True)
    except Exception as exc:  # noqa: BLE001 – ein fehlgeschlagenes Update darf den Start nie verhindern
        print(f"[Update] Übersprungen: {exc}", flush=True)


if __name__ == "__main__":
    update()
