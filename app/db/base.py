"""Datenbank-Engine, Session-Factory und Helfer (PostgreSQL produktiv, SQLite für lokale Tests)."""
from __future__ import annotations

from contextlib import asynccontextmanager
from datetime import datetime, timezone
from typing import Any, AsyncIterator

from sqlalchemy import BigInteger, DateTime, Integer, MetaData, event
from sqlalchemy.types import TypeDecorator
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import DeclarativeBase

from app.config import settings

# Autoincrement-PK, der in PostgreSQL BIGINT und in SQLite INTEGER (rowid) ist.
IdType = BigInteger().with_variant(Integer, "sqlite")

class UTCDateTime(TypeDecorator):
    """Speichert immer UTC und liefert immer zeitzonenbewusste Datetimes (auch unter SQLite)."""
    impl = DateTime(timezone=True)
    cache_ok = True

    def process_bind_param(self, value, dialect):
        if value is not None and value.tzinfo is not None:
            value = value.astimezone(timezone.utc)
            if dialect.name == "sqlite":
                value = value.replace(tzinfo=None)
        return value

    def process_result_value(self, value, dialect):
        if value is not None and value.tzinfo is None:
            value = value.replace(tzinfo=timezone.utc)
        return value


NAMING = {
    "ix": "ix_%(column_0_label)s",
    "uq": "uq_%(table_name)s_%(column_0_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}


class Base(DeclarativeBase):
    metadata = MetaData(naming_convention=NAMING)

    def to_dict(self, exclude: set[str] | None = None) -> dict[str, Any]:
        out: dict[str, Any] = {}
        for col in self.__table__.columns:
            if exclude and col.key in exclude:
                continue
            val = getattr(self, col.key)
            if isinstance(val, datetime):
                val = ensure_utc(val).isoformat()
            elif isinstance(val, int) and val > 2**53:
                val = str(val)  # Discord-Snowflakes sicher ans Frontend geben
            out[col.key] = val
        return out


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def ensure_utc(dt: datetime | None) -> datetime | None:
    if dt is None:
        return None
    return dt.replace(tzinfo=timezone.utc) if dt.tzinfo is None else dt


def _make_engine():
    url = settings.database_url
    kwargs: dict[str, Any] = {"pool_pre_ping": True}
    if url.startswith("sqlite"):
        settings.path("./data").mkdir(parents=True, exist_ok=True)
        kwargs = {"connect_args": {"timeout": 30}}
    else:
        kwargs.update(pool_size=10, max_overflow=20, pool_recycle=1800)
    eng = create_async_engine(url, **kwargs)
    if url.startswith("sqlite"):
        @event.listens_for(eng.sync_engine, "connect")
        def _pragma(dbapi_conn, _):  # pragma: no cover - sqlite only
            cur = dbapi_conn.cursor()
            cur.execute("PRAGMA journal_mode=WAL")
            cur.execute("PRAGMA foreign_keys=ON")
            cur.close()
    return eng


engine = _make_engine()
SessionLocal = async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)


@asynccontextmanager
async def session_scope() -> AsyncIterator[AsyncSession]:
    """Transaktionale Session: commit bei Erfolg, rollback bei Fehler."""
    async with SessionLocal() as session:
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise


def dialect_insert():
    """Dialekt-passendes INSERT mit ON CONFLICT (PostgreSQL + SQLite)."""
    if engine.dialect.name == "postgresql":
        from sqlalchemy.dialects.postgresql import insert
    else:
        from sqlalchemy.dialects.sqlite import insert
    return insert
