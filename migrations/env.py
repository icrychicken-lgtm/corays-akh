"""Alembic-Umgebung (async). Liest DATABASE_URL aus der App-Konfiguration."""
from __future__ import annotations

import asyncio

from alembic import context
from sqlalchemy import pool
from sqlalchemy.ext.asyncio import create_async_engine

from app.config import settings
from app.db import models  # noqa: F401 – registriert alle Tabellen
from app.db.base import Base, UTCDateTime

target_metadata = Base.metadata


def render_item(type_, obj, autogen_context):
    """UTCDateTime wird als normales DateTime(timezone=True) in Migrationen geschrieben."""
    if type_ == "type" and isinstance(obj, UTCDateTime):
        return "sa.DateTime(timezone=True)"
    return False


def run_migrations_offline() -> None:
    context.configure(url=settings.database_url, target_metadata=target_metadata, literal_binds=True, render_item=render_item,
                      render_as_batch=True)
    with context.begin_transaction():
        context.run_migrations()


def _do_run(connection) -> None:
    context.configure(connection=connection, target_metadata=target_metadata, render_item=render_item,
                      render_as_batch=connection.dialect.name == "sqlite", compare_type=True)
    with context.begin_transaction():
        context.run_migrations()


async def run_migrations_online() -> None:
    if settings.is_sqlite:
        settings.path("./data").mkdir(parents=True, exist_ok=True)
    engine = create_async_engine(settings.database_url, poolclass=pool.NullPool)
    async with engine.connect() as conn:
        await conn.run_sync(_do_run)
    await engine.dispose()


if context.is_offline_mode():
    run_migrations_offline()
else:
    asyncio.run(run_migrations_online())
