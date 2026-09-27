"""ticket questions – eigene Fragen pro Ticket-Kategorie

Revision ID: 1010607fb917
Revises: 4bfc440f0a64
Create Date: 2026-09-25 19:48:19.750111
"""
import json
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = '1010607fb917'
down_revision: Union[str, None] = '4bfc440f0a64'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    with op.batch_alter_table('ticket_categories', schema=None) as batch_op:
        batch_op.add_column(sa.Column('questions', sa.JSON(), nullable=False, server_default='[]'))

    # Bestehende Standard-Kategorien bekommen direkt passende Fragen
    from app.services.ticket_defaults import QUESTIONS_BY_LABEL
    conn = op.get_bind()
    value = "CAST(:q AS JSON)" if conn.dialect.name == "postgresql" else ":q"  # SQLite speichert JSON als Text
    rows = conn.execute(sa.text("SELECT id, label FROM ticket_categories")).fetchall()
    for row_id, label in rows:
        qs = QUESTIONS_BY_LABEL.get((label or "").lower())
        if qs:
            conn.execute(sa.text(f"UPDATE ticket_categories SET questions = {value} WHERE id = :id"), {"q": json.dumps(qs, ensure_ascii=False), "id": row_id})


def downgrade() -> None:
    with op.batch_alter_table('ticket_categories', schema=None) as batch_op:
        batch_op.drop_column('questions')
