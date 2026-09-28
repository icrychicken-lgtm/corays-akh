"""clean live style: stellt gespeicherte Live-Post-Stile einmalig auf „clean“ um

Revision ID: 9e1f3b7c5d20
Revises: 7c2d41a9e0b3
Create Date: 2026-09-28 18:00:00.000000
"""
import json
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = '9e1f3b7c5d20'
down_revision: Union[str, None] = '7c2d41a9e0b3'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


GUILD_MODULES = sa.table("guild_modules", sa.column("guild_id", sa.BigInteger), sa.column("module", sa.String), sa.column("settings", sa.JSON))


def _set_style(old: set, new: str) -> None:
    """Über die typisierte Tabelle, damit JSON auf SQLite und Postgres korrekt gelesen/geschrieben wird."""
    conn = op.get_bind()
    rows = conn.execute(sa.select(GUILD_MODULES.c.guild_id, GUILD_MODULES.c.settings).where(GUILD_MODULES.c.module == "streamer")).fetchall()
    for guild_id, raw in rows:
        data = json.loads(raw) if isinstance(raw, str) else dict(raw or {})
        if data.get("style") in old:
            data["style"] = new
            conn.execute(GUILD_MODULES.update().where(GUILD_MODULES.c.guild_id == guild_id, GUILD_MODULES.c.module == "streamer")
                         .values(settings=data))


def upgrade() -> None:
    # Der Server-Betreiber hat sich für den Clean-Stil entschieden; „hood“ war nur der alte Standardwert.
    _set_style({"hood", None}, "clean")


def downgrade() -> None:
    _set_style({"clean"}, "hood")
