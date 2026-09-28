"""clip contests

Revision ID: 7c2d41a9e0b3
Revises: e5b8657bbbf0
Create Date: 2026-09-28 12:00:00.000000
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = '7c2d41a9e0b3'
down_revision: Union[str, None] = 'e5b8657bbbf0'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table('clip_contests',
    sa.Column('id', sa.BigInteger().with_variant(sa.Integer(), 'sqlite'), autoincrement=True, nullable=False),
    sa.Column('guild_id', sa.BigInteger(), nullable=False),
    sa.Column('name', sa.String(length=100), nullable=False),
    sa.Column('channel_id', sa.BigInteger(), nullable=False),
    sa.Column('message_id', sa.BigInteger(), nullable=True),
    sa.Column('host_id', sa.BigInteger(), nullable=False),
    sa.Column('starts_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('ends_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('ended', sa.Boolean(), nullable=False),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_clip_contests'))
    )
    with op.batch_alter_table('clip_contests', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_clip_contests_guild_id'), ['guild_id'], unique=False)
        batch_op.create_index(batch_op.f('ix_clip_contests_ended'), ['ended'], unique=False)

    op.create_table('clip_submissions',
    sa.Column('id', sa.BigInteger().with_variant(sa.Integer(), 'sqlite'), autoincrement=True, nullable=False),
    sa.Column('contest_id', sa.BigInteger().with_variant(sa.Integer(), 'sqlite'), nullable=False),
    sa.Column('guild_id', sa.BigInteger(), nullable=False),
    sa.Column('user_id', sa.BigInteger(), nullable=False),
    sa.Column('user_name', sa.String(length=100), nullable=False),
    sa.Column('url', sa.String(length=300), nullable=False),
    sa.Column('platform', sa.String(length=12), nullable=False),
    sa.Column('status', sa.String(length=10), nullable=False),
    sa.Column('views', sa.BigInteger(), nullable=False),
    sa.Column('claimed_views', sa.BigInteger(), nullable=True),
    sa.Column('proof_url', sa.String(length=500), nullable=True),
    sa.Column('message_id', sa.BigInteger(), nullable=True),
    sa.Column('reviewer_id', sa.BigInteger(), nullable=True),
    sa.Column('paid', sa.Boolean(), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), nullable=True),
    sa.ForeignKeyConstraint(['contest_id'], ['clip_contests.id'], name=op.f('fk_clip_submissions_contest_id_clip_contests'), ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_clip_submissions')),
    sa.UniqueConstraint('contest_id', 'url', name=op.f('uq_clip_submissions_contest_id'))
    )
    with op.batch_alter_table('clip_submissions', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_clip_submissions_contest_id'), ['contest_id'], unique=False)
        batch_op.create_index(batch_op.f('ix_clip_submissions_guild_id'), ['guild_id'], unique=False)
        batch_op.create_index(batch_op.f('ix_clip_submissions_user_id'), ['user_id'], unique=False)


def downgrade() -> None:
    with op.batch_alter_table('clip_submissions', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_clip_submissions_user_id'))
        batch_op.drop_index(batch_op.f('ix_clip_submissions_guild_id'))
        batch_op.drop_index(batch_op.f('ix_clip_submissions_contest_id'))
    op.drop_table('clip_submissions')
    with op.batch_alter_table('clip_contests', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_clip_contests_ended'))
        batch_op.drop_index(batch_op.f('ix_clip_contests_guild_id'))
    op.drop_table('clip_contests')
