"""Analytics-Zähler: werden im Speicher gesammelt und minütlich gebündelt in stat_buckets geschrieben."""
from __future__ import annotations

import logging
from collections import defaultdict
from datetime import datetime, timedelta, timezone

from sqlalchemy import func, select

from app.db.base import SessionLocal, dialect_insert, engine, session_scope
from app.db.models import StatBucket

log = logging.getLogger("nova.metrics")

# Gauges werden überschrieben (Maximum der Stunde), alle anderen Metriken aufsummiert.
GAUGES = {"members", "online", "voice_users", "stream_viewers"}


def hour_bucket(dt: datetime | None = None) -> datetime:
    dt = dt or datetime.now(timezone.utc)
    return dt.replace(minute=0, second=0, microsecond=0)


class Metrics:
    def __init__(self) -> None:
        self._counters: dict[tuple[int, datetime, str], float] = defaultdict(float)
        self._gauges: dict[tuple[int, datetime, str], float] = {}

    def incr(self, guild_id: int, metric: str, value: float = 1) -> None:
        self._counters[(guild_id, hour_bucket(), metric)] += value

    def gauge(self, guild_id: int, metric: str, value: float) -> None:
        key = (guild_id, hour_bucket(), metric)
        self._gauges[key] = max(self._gauges.get(key, 0), value)

    async def flush(self) -> None:
        if not self._counters and not self._gauges:
            return
        counters, gauges = self._counters, self._gauges
        self._counters, self._gauges = defaultdict(float), {}
        insert = dialect_insert()
        try:
            async with session_scope() as s:
                for (gid, bucket, metric), value in counters.items():
                    stmt = insert(StatBucket).values(guild_id=gid, bucket=bucket, metric=metric, value=value)
                    stmt = stmt.on_conflict_do_update(
                        index_elements=["guild_id", "bucket", "metric"],
                        set_={"value": StatBucket.value + stmt.excluded.value},
                    )
                    await s.execute(stmt)
                greatest = func.greatest if engine.dialect.name == "postgresql" else func.max
                for (gid, bucket, metric), value in gauges.items():
                    stmt = insert(StatBucket).values(guild_id=gid, bucket=bucket, metric=metric, value=value)
                    stmt = stmt.on_conflict_do_update(
                        index_elements=["guild_id", "bucket", "metric"],
                        set_={"value": greatest(StatBucket.value, stmt.excluded.value)},
                    )
                    await s.execute(stmt)
        except Exception:
            log.exception("Metrics-Flush fehlgeschlagen – Werte werden erneut versucht")
            for k, v in counters.items():
                self._counters[k] += v
            for k, v in gauges.items():
                self._gauges[k] = max(self._gauges.get(k, 0), v)


async def series(guild_id: int, metrics: list[str], range_key: str) -> dict:
    """Zeitreihe für Charts. 24h → stündlich, sonst täglich aggregiert."""
    now = datetime.now(timezone.utc)
    spans = {"24h": timedelta(hours=24), "7d": timedelta(days=7), "30d": timedelta(days=30), "90d": timedelta(days=90)}
    hourly = range_key == "24h"
    start = now - spans[range_key] if range_key in spans else None
    async with SessionLocal() as s:
        q = select(StatBucket.bucket, StatBucket.metric, StatBucket.value).where(
            StatBucket.guild_id == guild_id, StatBucket.metric.in_(metrics))
        if start is not None:
            q = q.where(StatBucket.bucket >= hour_bucket(start))
        rows = (await s.execute(q)).all()
        if start is None:
            first = min((r.bucket for r in rows), default=None)
            start = first.replace(tzinfo=timezone.utc) if first and first.tzinfo is None else (first or now - timedelta(days=30))

    labels: list[str] = []
    if hourly:
        cur = hour_bucket(start) + timedelta(hours=1)
        while cur <= now:
            labels.append(cur.strftime("%Y-%m-%dT%H:00"))
            cur += timedelta(hours=1)
    else:
        cur = start.date()
        while cur <= now.date():
            labels.append(cur.isoformat())
            cur += timedelta(days=1)
    index = {label: i for i, label in enumerate(labels)}
    data = {m: [0.0] * len(labels) for m in metrics}
    for bucket, metric, value in rows:
        b = bucket.replace(tzinfo=timezone.utc) if bucket.tzinfo is None else bucket
        label = b.strftime("%Y-%m-%dT%H:00") if hourly else b.date().isoformat()
        i = index.get(label)
        if i is None:
            continue
        if metric in GAUGES:
            data[metric][i] = max(data[metric][i], value)
        else:
            data[metric][i] += value
    # Gauges: Lücken mit letztem bekannten Wert füllen (z. B. Mitgliederzahl)
    for m in metrics:
        if m in GAUGES:
            last = 0.0
            for i, v in enumerate(data[m]):
                if v:
                    last = v
                else:
                    data[m][i] = last
    return {"labels": labels, "series": data, "hourly": hourly}


metrics = Metrics()
