"""Zentrale Konfiguration – ausschließlich aus Umgebungsvariablen / .env."""
from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

ROOT_DIR = Path(__file__).resolve().parent.parent


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=ROOT_DIR / ".env", env_file_encoding="utf-8", extra="ignore")

    # Discord
    discord_token: str = ""
    discord_client_id: str = ""
    discord_client_secret: str = ""
    owner_ids: str = ""
    dev_guild_id: str = ""
    enable_presence_intent: bool = True

    # Dashboard
    dashboard_url: str = "http://localhost:8080"
    web_host: str = "0.0.0.0"
    web_port: int = 8080
    server_port: int | None = None  # von Bot-Hostern (Pterodactyl) automatisch gesetzt – hat Vorrang
    cookie_secure: bool = False
    secret_key: str = ""
    encryption_key: str = ""

    # Storage
    database_url: str = "sqlite+aiosqlite:///./data/nova.db"
    redis_url: str = ""

    # Logging / backups
    log_level: str = "INFO"
    log_dir: str = "./logs"
    backup_dir: str = "./backups"
    backup_interval_hours: int = 24
    backup_keep: int = 14

    # Integrations (global defaults)
    twitch_client_id: str = ""
    twitch_client_secret: str = ""
    youtube_api_key: str = ""
    kick_client_id: str = ""
    kick_client_secret: str = ""
    spotify_client_id: str = ""
    spotify_client_secret: str = ""
    stream_poll_seconds: int = Field(default=90, ge=60)

    default_locale: str = "de"
    bot_name: str = "Nova"

    @field_validator("dashboard_url")
    @classmethod
    def _strip_slash(cls, v: str) -> str:
        return v.rstrip("/")

    @property
    def owner_id_set(self) -> set[int]:
        return {int(x) for x in self.owner_ids.replace(" ", "").split(",") if x.isdigit()}

    @property
    def port(self) -> int:
        return self.server_port or self.web_port

    @property
    def oauth_redirect_uri(self) -> str:
        return f"{self.dashboard_url}/auth/callback"

    @property
    def is_sqlite(self) -> bool:
        return self.database_url.startswith("sqlite")

    def path(self, value: str) -> Path:
        p = Path(value)
        return p if p.is_absolute() else (ROOT_DIR / p).resolve()


@lru_cache
def get_settings() -> Settings:
    return Settings()


settings = get_settings()
