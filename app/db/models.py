"""Alle Datenbank-Tabellen. Discord-IDs sind BIGINT, jede Guild-Tabelle trägt `guild_id` (Multi-Server)."""
from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import (
    JSON,
    BigInteger,
    Boolean,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, IdType, UTCDateTime, utcnow

TS = UTCDateTime()


def _id() -> Mapped[int]:
    return mapped_column(IdType, primary_key=True, autoincrement=True)


def _guild() -> Mapped[int]:
    return mapped_column(BigInteger, index=True)


def _created() -> Mapped[datetime]:
    return mapped_column(TS, default=utcnow, nullable=False)


# ───────────────────────── Kern ─────────────────────────
class Guild(Base):
    __tablename__ = "guilds"
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    name: Mapped[str] = mapped_column(String(100), default="")
    icon: Mapped[str | None] = mapped_column(String(255))
    owner_id: Mapped[int | None] = mapped_column(BigInteger)
    joined_at: Mapped[datetime] = _created()
    left_at: Mapped[datetime | None] = mapped_column(TS)
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    next_case: Mapped[int] = mapped_column(Integer, default=1)
    next_ticket: Mapped[int] = mapped_column(Integer, default=1)
    next_suggestion: Mapped[int] = mapped_column(Integer, default=1)


class GuildModule(Base):
    """Feature-Toggle + Einstellungen pro Modul und Server."""
    __tablename__ = "guild_modules"
    guild_id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    module: Mapped[str] = mapped_column(String(40), primary_key=True)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    settings: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    updated_at: Mapped[datetime] = mapped_column(TS, default=utcnow, onupdate=utcnow)


class BotState(Base):
    """Globaler Key/Value-Speicher (z. B. Maintenance-Mode)."""
    __tablename__ = "bot_state"
    key: Mapped[str] = mapped_column(String(64), primary_key=True)
    value: Mapped[Any] = mapped_column(JSON)


class DashboardSession(Base):
    __tablename__ = "dashboard_sessions"
    id: Mapped[str] = mapped_column(String(96), primary_key=True)
    user_id: Mapped[int] = mapped_column(BigInteger, index=True)
    username: Mapped[str] = mapped_column(String(100))
    avatar: Mapped[str | None] = mapped_column(String(255))
    access_token_enc: Mapped[str] = mapped_column(Text)
    csrf_token: Mapped[str] = mapped_column(String(64))
    ip: Mapped[str | None] = mapped_column(String(64))
    created_at: Mapped[datetime] = _created()
    last_seen: Mapped[datetime] = mapped_column(TS, default=utcnow)
    expires_at: Mapped[datetime] = mapped_column(TS)


class Integration(Base):
    """Pro Server hinterlegte API-Zugangsdaten – immer verschlüsselt."""
    __tablename__ = "integrations"
    guild_id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    provider: Mapped[str] = mapped_column(String(20), primary_key=True)
    data_encrypted: Mapped[str] = mapped_column(Text)
    updated_by: Mapped[int | None] = mapped_column(BigInteger)
    updated_at: Mapped[datetime] = mapped_column(TS, default=utcnow, onupdate=utcnow)


class CommandPermission(Base):
    __tablename__ = "command_permissions"
    guild_id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    command: Mapped[str] = mapped_column(String(64), primary_key=True)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    allowed_role_ids: Mapped[list] = mapped_column(JSON, default=list)
    denied_role_ids: Mapped[list] = mapped_column(JSON, default=list)
    allowed_user_ids: Mapped[list] = mapped_column(JSON, default=list)
    allowed_channel_ids: Mapped[list] = mapped_column(JSON, default=list)
    denied_channel_ids: Mapped[list] = mapped_column(JSON, default=list)


# ───────────────────────── Mitglieder ─────────────────────────
class Member(Base):
    """Server-spezifisches Profil: Level, Economy, Aktivität, AFK, Geburtstag (privat)."""
    __tablename__ = "members"
    guild_id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    user_id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    username: Mapped[str] = mapped_column(String(100), default="")
    display_name: Mapped[str] = mapped_column(String(100), default="")
    avatar: Mapped[str | None] = mapped_column(String(255))
    first_joined_at: Mapped[datetime | None] = mapped_column(TS)
    joined_at: Mapped[datetime | None] = mapped_column(TS)
    left_at: Mapped[datetime | None] = mapped_column(TS)
    in_guild: Mapped[bool] = mapped_column(Boolean, default=True)

    xp: Mapped[int] = mapped_column(BigInteger, default=0)
    level: Mapped[int] = mapped_column(Integer, default=0)
    last_xp_at: Mapped[datetime | None] = mapped_column(TS)
    messages: Mapped[int] = mapped_column(Integer, default=0)
    voice_minutes: Mapped[int] = mapped_column(Integer, default=0)
    reactions: Mapped[int] = mapped_column(Integer, default=0)
    stream_checkins: Mapped[int] = mapped_column(Integer, default=0)
    events_won: Mapped[int] = mapped_column(Integer, default=0)
    giveaways_won: Mapped[int] = mapped_column(Integer, default=0)
    invites: Mapped[int] = mapped_column(Integer, default=0)
    invited_by: Mapped[int | None] = mapped_column(BigInteger)

    coins: Mapped[int] = mapped_column(BigInteger, default=0)
    coins_earned: Mapped[int] = mapped_column(BigInteger, default=0)
    last_coin_msg_at: Mapped[datetime | None] = mapped_column(TS)
    daily_streak: Mapped[int] = mapped_column(Integer, default=0)
    daily_best_streak: Mapped[int] = mapped_column(Integer, default=0)
    daily_last: Mapped[datetime | None] = mapped_column(TS)
    weekly_streak: Mapped[int] = mapped_column(Integer, default=0)
    weekly_last: Mapped[datetime | None] = mapped_column(TS)
    work_last: Mapped[datetime | None] = mapped_column(TS)
    rob_last: Mapped[datetime | None] = mapped_column(TS)
    crime_last: Mapped[datetime | None] = mapped_column(TS)

    afk_message: Mapped[str | None] = mapped_column(String(200))
    afk_since: Mapped[datetime | None] = mapped_column(TS)
    birthday_day: Mapped[int | None] = mapped_column(Integer)
    birthday_month: Mapped[int | None] = mapped_column(Integer)
    birthday_year: Mapped[int | None] = mapped_column(Integer)
    birthday_last_celebrated: Mapped[int | None] = mapped_column(Integer)
    profile_banner: Mapped[str | None] = mapped_column(String(400))
    profile_bio: Mapped[str | None] = mapped_column(String(300))
    games: Mapped[dict] = mapped_column(JSON, default=dict)

    __table_args__ = (
        Index("ix_members_guild_xp", "guild_id", "xp"),
        Index("ix_members_guild_coins", "guild_id", "coins"),
    )


class UserNote(Base):
    """Interne Staff-Notizen zu einem User."""
    __tablename__ = "user_notes"
    id: Mapped[int] = _id()
    guild_id: Mapped[int] = _guild()
    user_id: Mapped[int] = mapped_column(BigInteger, index=True)
    author_id: Mapped[int] = mapped_column(BigInteger)
    author_name: Mapped[str] = mapped_column(String(100))
    content: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = _created()


class StaffNote(Base):
    """Staff-Board: interne Notizen zwischen Teammitgliedern."""
    __tablename__ = "staff_notes"
    id: Mapped[int] = _id()
    guild_id: Mapped[int] = _guild()
    author_id: Mapped[int] = mapped_column(BigInteger)
    author_name: Mapped[str] = mapped_column(String(100))
    content: Mapped[str] = mapped_column(Text)
    pinned: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = _created()


# ───────────────────────── Moderation ─────────────────────────
class ModCase(Base):
    __tablename__ = "mod_cases"
    id: Mapped[int] = _id()
    guild_id: Mapped[int] = _guild()
    case_number: Mapped[int] = mapped_column(Integer)
    action: Mapped[str] = mapped_column(String(20), index=True)
    user_id: Mapped[int] = mapped_column(BigInteger, index=True)
    user_name: Mapped[str] = mapped_column(String(100), default="")
    moderator_id: Mapped[int] = mapped_column(BigInteger)
    moderator_name: Mapped[str] = mapped_column(String(100), default="")
    reason: Mapped[str] = mapped_column(Text, default="")
    duration_seconds: Mapped[int | None] = mapped_column(Integer)
    expires_at: Mapped[datetime | None] = mapped_column(TS)
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    source: Mapped[str] = mapped_column(String(20), default="command")
    created_at: Mapped[datetime] = _created()
    updated_at: Mapped[datetime | None] = mapped_column(TS)
    __table_args__ = (UniqueConstraint("guild_id", "case_number"),)


# ───────────────────────── Tickets ─────────────────────────
class TicketCategory(Base):
    __tablename__ = "ticket_categories"
    id: Mapped[int] = _id()
    guild_id: Mapped[int] = _guild()
    label: Mapped[str] = mapped_column(String(80))
    emoji: Mapped[str] = mapped_column(String(64), default="🎫")
    description: Mapped[str] = mapped_column(String(100), default="")
    category_channel_id: Mapped[int | None] = mapped_column(BigInteger)
    staff_role_ids: Mapped[list] = mapped_column(JSON, default=list)
    modal_question: Mapped[str] = mapped_column(String(45), default="Was ist dein Anliegen?")
    questions: Mapped[list] = mapped_column(JSON, default=list)  # [{label, placeholder, style, required}] – max. 5
    welcome_message: Mapped[str] = mapped_column(Text, default="")
    position: Mapped[int] = mapped_column(Integer, default=0)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)


class Ticket(Base):
    __tablename__ = "tickets"
    id: Mapped[int] = _id()
    guild_id: Mapped[int] = _guild()
    number: Mapped[int] = mapped_column(Integer)
    channel_id: Mapped[int | None] = mapped_column(BigInteger, index=True)
    category_id: Mapped[int | None] = mapped_column(ForeignKey("ticket_categories.id", ondelete="SET NULL"))
    category_label: Mapped[str] = mapped_column(String(80), default="")
    opener_id: Mapped[int] = mapped_column(BigInteger, index=True)
    opener_name: Mapped[str] = mapped_column(String(100), default="")
    subject: Mapped[str] = mapped_column(Text, default="")
    status: Mapped[str] = mapped_column(String(12), default="open", index=True)
    priority: Mapped[str] = mapped_column(String(10), default="normal")
    claimed_by: Mapped[int | None] = mapped_column(BigInteger)
    claimed_name: Mapped[str | None] = mapped_column(String(100))
    locked: Mapped[bool] = mapped_column(Boolean, default=False)
    added_user_ids: Mapped[list] = mapped_column(JSON, default=list)
    created_at: Mapped[datetime] = _created()
    first_response_at: Mapped[datetime | None] = mapped_column(TS)
    closed_at: Mapped[datetime | None] = mapped_column(TS)
    closed_by: Mapped[int | None] = mapped_column(BigInteger)
    closed_by_name: Mapped[str | None] = mapped_column(String(100))
    close_reason: Mapped[str | None] = mapped_column(Text)
    transcript_html: Mapped[str | None] = mapped_column(Text)
    transcript_count: Mapped[int] = mapped_column(Integer, default=0)
    __table_args__ = (UniqueConstraint("guild_id", "number"),)


class TicketNote(Base):
    __tablename__ = "ticket_notes"
    id: Mapped[int] = _id()
    ticket_id: Mapped[int] = mapped_column(ForeignKey("tickets.id", ondelete="CASCADE"), index=True)
    author_id: Mapped[int] = mapped_column(BigInteger)
    author_name: Mapped[str] = mapped_column(String(100))
    content: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = _created()


# ───────────────────────── Bewerbungen ─────────────────────────
class Application(Base):
    __tablename__ = "applications"
    id: Mapped[int] = _id()
    guild_id: Mapped[int] = _guild()
    user_id: Mapped[int] = mapped_column(BigInteger, index=True)
    user_name: Mapped[str] = mapped_column(String(100), default="")
    position: Mapped[str] = mapped_column(String(100), default="")
    answers: Mapped[list] = mapped_column(JSON, default=list)
    status: Mapped[str] = mapped_column(String(12), default="open", index=True)
    reviewer_id: Mapped[int | None] = mapped_column(BigInteger)
    reviewer_name: Mapped[str | None] = mapped_column(String(100))
    decision_reason: Mapped[str | None] = mapped_column(Text)
    review_message_id: Mapped[int | None] = mapped_column(BigInteger)
    created_at: Mapped[datetime] = _created()
    updated_at: Mapped[datetime | None] = mapped_column(TS)


# ───────────────────────── Giveaways ─────────────────────────
class Giveaway(Base):
    __tablename__ = "giveaways"
    id: Mapped[int] = _id()
    guild_id: Mapped[int] = _guild()
    channel_id: Mapped[int] = mapped_column(BigInteger)
    message_id: Mapped[int | None] = mapped_column(BigInteger, index=True)
    prize: Mapped[str] = mapped_column(String(200))
    description: Mapped[str] = mapped_column(Text, default="")
    winners_count: Mapped[int] = mapped_column(Integer, default=1)
    host_id: Mapped[int] = mapped_column(BigInteger)
    host_name: Mapped[str] = mapped_column(String(100), default="")
    starts_at: Mapped[datetime] = _created()
    ends_at: Mapped[datetime] = mapped_column(TS, index=True)
    ended: Mapped[bool] = mapped_column(Boolean, default=False, index=True)
    requirements: Mapped[dict] = mapped_column(JSON, default=dict)
    bonus: Mapped[dict] = mapped_column(JSON, default=dict)
    winner_ids: Mapped[list] = mapped_column(JSON, default=list)
    image_url: Mapped[str | None] = mapped_column(String(400))
    created_at: Mapped[datetime] = _created()


class GiveawayEntry(Base):
    __tablename__ = "giveaway_entries"
    giveaway_id: Mapped[int] = mapped_column(ForeignKey("giveaways.id", ondelete="CASCADE"), primary_key=True)
    user_id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    entries: Mapped[int] = mapped_column(Integer, default=1)
    joined_at: Mapped[datetime] = _created()


# ───────────────────────── Level / Economy / Quests ─────────────────────────
class LevelReward(Base):
    __tablename__ = "level_rewards"
    id: Mapped[int] = _id()
    guild_id: Mapped[int] = _guild()
    level: Mapped[int] = mapped_column(Integer)
    role_id: Mapped[int] = mapped_column(BigInteger)
    coins: Mapped[int] = mapped_column(Integer, default=0)


class ShopItem(Base):
    __tablename__ = "shop_items"
    id: Mapped[int] = _id()
    guild_id: Mapped[int] = _guild()
    name: Mapped[str] = mapped_column(String(80))
    description: Mapped[str] = mapped_column(String(200), default="")
    emoji: Mapped[str] = mapped_column(String(64), default="🛍️")
    price: Mapped[int] = mapped_column(Integer)
    kind: Mapped[str] = mapped_column(String(12), default="cosmetic")  # role | color | badge | cosmetic | event
    role_id: Mapped[int | None] = mapped_column(BigInteger)
    badge_id: Mapped[int | None] = mapped_column(Integer)
    stock: Mapped[int] = mapped_column(Integer, default=-1)
    max_per_user: Mapped[int] = mapped_column(Integer, default=1)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    position: Mapped[int] = mapped_column(Integer, default=0)


class InventoryItem(Base):
    __tablename__ = "inventory"
    id: Mapped[int] = _id()
    guild_id: Mapped[int] = _guild()
    user_id: Mapped[int] = mapped_column(BigInteger, index=True)
    item_id: Mapped[int] = mapped_column(ForeignKey("shop_items.id", ondelete="CASCADE"))
    quantity: Mapped[int] = mapped_column(Integer, default=1)
    acquired_at: Mapped[datetime] = _created()
    __table_args__ = (UniqueConstraint("guild_id", "user_id", "item_id"),)


class Quest(Base):
    __tablename__ = "quests"
    id: Mapped[int] = _id()
    guild_id: Mapped[int] = _guild()
    name: Mapped[str] = mapped_column(String(100))
    description: Mapped[str] = mapped_column(String(200), default="")
    period: Mapped[str] = mapped_column(String(10), default="daily")  # daily | weekly
    metric: Mapped[str] = mapped_column(String(20))
    target: Mapped[int] = mapped_column(Integer)
    reward_xp: Mapped[int] = mapped_column(Integer, default=0)
    reward_coins: Mapped[int] = mapped_column(Integer, default=0)
    reward_role_id: Mapped[int | None] = mapped_column(BigInteger)
    reward_badge_id: Mapped[int | None] = mapped_column(Integer)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)


class QuestProgress(Base):
    __tablename__ = "quest_progress"
    quest_id: Mapped[int] = mapped_column(ForeignKey("quests.id", ondelete="CASCADE"), primary_key=True)
    user_id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    period_key: Mapped[str] = mapped_column(String(12), primary_key=True)
    guild_id: Mapped[int] = _guild()
    progress: Mapped[int] = mapped_column(Integer, default=0)
    completed_at: Mapped[datetime | None] = mapped_column(TS)


class Badge(Base):
    __tablename__ = "badges"
    id: Mapped[int] = _id()
    guild_id: Mapped[int] = _guild()
    name: Mapped[str] = mapped_column(String(50))
    emoji: Mapped[str] = mapped_column(String(64), default="🏅")
    description: Mapped[str] = mapped_column(String(200), default="")
    auto_rule: Mapped[str] = mapped_column(String(20), default="manual")
    rule_value: Mapped[str | None] = mapped_column(String(64))
    position: Mapped[int] = mapped_column(Integer, default=0)


class MemberBadge(Base):
    __tablename__ = "member_badges"
    guild_id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    user_id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    badge_id: Mapped[int] = mapped_column(ForeignKey("badges.id", ondelete="CASCADE"), primary_key=True)
    granted_at: Mapped[datetime] = _created()
    granted_by: Mapped[int | None] = mapped_column(BigInteger)


class MemberAchievement(Base):
    __tablename__ = "member_achievements"
    guild_id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    user_id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    key: Mapped[str] = mapped_column(String(40), primary_key=True)
    unlocked_at: Mapped[datetime] = _created()


# ───────────────────────── Rollen ─────────────────────────
class RoleMenu(Base):
    __tablename__ = "role_menus"
    id: Mapped[int] = _id()
    guild_id: Mapped[int] = _guild()
    name: Mapped[str] = mapped_column(String(80))
    title: Mapped[str] = mapped_column(String(200), default="")
    description: Mapped[str] = mapped_column(Text, default="")
    style: Mapped[str] = mapped_column(String(10), default="buttons")  # buttons | select
    options: Mapped[list] = mapped_column(JSON, default=list)
    max_values: Mapped[int] = mapped_column(Integer, default=0)  # 0 = unbegrenzt
    required_role_id: Mapped[int | None] = mapped_column(BigInteger)
    color: Mapped[str | None] = mapped_column(String(9))
    image_url: Mapped[str | None] = mapped_column(String(400))
    channel_id: Mapped[int | None] = mapped_column(BigInteger)
    message_id: Mapped[int | None] = mapped_column(BigInteger)
    created_at: Mapped[datetime] = _created()


# ───────────────────────── Community ─────────────────────────
class Suggestion(Base):
    __tablename__ = "suggestions"
    id: Mapped[int] = _id()
    guild_id: Mapped[int] = _guild()
    number: Mapped[int] = mapped_column(Integer)
    user_id: Mapped[int] = mapped_column(BigInteger)
    user_name: Mapped[str] = mapped_column(String(100), default="")
    content: Mapped[str] = mapped_column(Text)
    status: Mapped[str] = mapped_column(String(12), default="pending", index=True)
    channel_id: Mapped[int | None] = mapped_column(BigInteger)
    message_id: Mapped[int | None] = mapped_column(BigInteger, index=True)
    upvotes: Mapped[int] = mapped_column(Integer, default=0)
    downvotes: Mapped[int] = mapped_column(Integer, default=0)
    staff_id: Mapped[int | None] = mapped_column(BigInteger)
    staff_name: Mapped[str | None] = mapped_column(String(100))
    staff_reason: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = _created()
    updated_at: Mapped[datetime | None] = mapped_column(TS)


class SuggestionVote(Base):
    __tablename__ = "suggestion_votes"
    suggestion_id: Mapped[int] = mapped_column(ForeignKey("suggestions.id", ondelete="CASCADE"), primary_key=True)
    user_id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    vote: Mapped[int] = mapped_column(Integer)


class Poll(Base):
    __tablename__ = "polls"
    id: Mapped[int] = _id()
    guild_id: Mapped[int] = _guild()
    channel_id: Mapped[int] = mapped_column(BigInteger)
    message_id: Mapped[int | None] = mapped_column(BigInteger, index=True)
    question: Mapped[str] = mapped_column(String(300))
    options: Mapped[list] = mapped_column(JSON)
    multiple: Mapped[bool] = mapped_column(Boolean, default=False)
    anonymous: Mapped[bool] = mapped_column(Boolean, default=False)
    author_id: Mapped[int] = mapped_column(BigInteger)
    ends_at: Mapped[datetime | None] = mapped_column(TS, index=True)
    ended: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = _created()


class PollVote(Base):
    __tablename__ = "poll_votes"
    poll_id: Mapped[int] = mapped_column(ForeignKey("polls.id", ondelete="CASCADE"), primary_key=True)
    user_id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    option_index: Mapped[int] = mapped_column(Integer, primary_key=True)


class Reminder(Base):
    __tablename__ = "reminders"
    id: Mapped[int] = _id()
    user_id: Mapped[int] = mapped_column(BigInteger, index=True)
    guild_id: Mapped[int | None] = mapped_column(BigInteger)
    channel_id: Mapped[int | None] = mapped_column(BigInteger)
    content: Mapped[str] = mapped_column(String(1000))
    remind_at: Mapped[datetime] = mapped_column(TS, index=True)
    done: Mapped[bool] = mapped_column(Boolean, default=False, index=True)
    created_at: Mapped[datetime] = _created()


class CustomCommand(Base):
    __tablename__ = "custom_commands"
    id: Mapped[int] = _id()
    guild_id: Mapped[int] = _guild()
    name: Mapped[str] = mapped_column(String(32))
    description: Mapped[str] = mapped_column(String(100), default="Custom Command")
    content: Mapped[str] = mapped_column(Text, default="")
    use_embed: Mapped[bool] = mapped_column(Boolean, default=True)
    embed_title: Mapped[str] = mapped_column(String(256), default="")
    embed_color: Mapped[str | None] = mapped_column(String(9))
    image_url: Mapped[str | None] = mapped_column(String(400))
    thumbnail_url: Mapped[str | None] = mapped_column(String(400))
    buttons: Mapped[list] = mapped_column(JSON, default=list)  # [{label, url, emoji}]
    ephemeral: Mapped[bool] = mapped_column(Boolean, default=False)
    allowed_role_ids: Mapped[list] = mapped_column(JSON, default=list)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    uses: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = _created()
    __table_args__ = (UniqueConstraint("guild_id", "name"),)


class AutoResponder(Base):
    __tablename__ = "autoresponders"
    id: Mapped[int] = _id()
    guild_id: Mapped[int] = _guild()
    trigger: Mapped[str] = mapped_column(String(200))
    match_type: Mapped[str] = mapped_column(String(12), default="word")  # word|contains|exact|startswith|regex
    response: Mapped[str] = mapped_column(Text)
    use_embed: Mapped[bool] = mapped_column(Boolean, default=False)
    reply: Mapped[bool] = mapped_column(Boolean, default=True)
    delete_trigger: Mapped[bool] = mapped_column(Boolean, default=False)
    cooldown_seconds: Mapped[int] = mapped_column(Integer, default=30)
    channel_ids: Mapped[list] = mapped_column(JSON, default=list)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    uses: Mapped[int] = mapped_column(Integer, default=0)


class StarboardEntry(Base):
    __tablename__ = "starboard_entries"
    message_id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    guild_id: Mapped[int] = _guild()
    channel_id: Mapped[int] = mapped_column(BigInteger)
    author_id: Mapped[int] = mapped_column(BigInteger)
    star_message_id: Mapped[int | None] = mapped_column(BigInteger)
    stars: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = _created()


class ServerEvent(Base):
    __tablename__ = "server_events"
    id: Mapped[int] = _id()
    guild_id: Mapped[int] = _guild()
    name: Mapped[str] = mapped_column(String(100))
    description: Mapped[str] = mapped_column(Text, default="")
    starts_at: Mapped[datetime] = mapped_column(TS, index=True)
    channel_id: Mapped[int | None] = mapped_column(BigInteger)
    message_id: Mapped[int | None] = mapped_column(BigInteger)
    image_url: Mapped[str | None] = mapped_column(String(400))
    reward_coins: Mapped[int] = mapped_column(Integer, default=0)
    reward_xp: Mapped[int] = mapped_column(Integer, default=0)
    reward_text: Mapped[str] = mapped_column(String(200), default="")
    reminders_sent: Mapped[list] = mapped_column(JSON, default=list)
    status: Mapped[str] = mapped_column(String(12), default="scheduled", index=True)
    created_by: Mapped[int | None] = mapped_column(BigInteger)
    created_at: Mapped[datetime] = _created()


class EventParticipant(Base):
    __tablename__ = "event_participants"
    event_id: Mapped[int] = mapped_column(ForeignKey("server_events.id", ondelete="CASCADE"), primary_key=True)
    user_id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    winner: Mapped[bool] = mapped_column(Boolean, default=False)
    joined_at: Mapped[datetime] = _created()


class TempChannel(Base):
    __tablename__ = "temp_channels"
    channel_id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    guild_id: Mapped[int] = _guild()
    owner_id: Mapped[int] = mapped_column(BigInteger)
    blocked_ids: Mapped[list] = mapped_column(JSON, default=list)
    created_at: Mapped[datetime] = _created()


# ───────────────────────── Streamer ─────────────────────────
class Streamer(Base):
    __tablename__ = "streamers"
    id: Mapped[int] = _id()
    guild_id: Mapped[int] = _guild()
    platform: Mapped[str] = mapped_column(String(10))  # twitch | youtube | kick
    channel: Mapped[str] = mapped_column(String(100))  # Login / Slug / YouTube-Channel-ID
    display_name: Mapped[str] = mapped_column(String(100), default="")
    platform_user_id: Mapped[str | None] = mapped_column(String(64))
    avatar_url: Mapped[str | None] = mapped_column(String(400))
    announce_channel_id: Mapped[int | None] = mapped_column(BigInteger)
    ping_role_id: Mapped[int | None] = mapped_column(BigInteger)
    message_template: Mapped[str] = mapped_column(Text, default="")
    discord_user_id: Mapped[int | None] = mapped_column(BigInteger)
    live_role_id: Mapped[int | None] = mapped_column(BigInteger)
    rename_channel_id: Mapped[int | None] = mapped_column(BigInteger)
    rename_live: Mapped[str] = mapped_column(String(100), default="🔴 LIVE: {title}")
    rename_offline: Mapped[str] = mapped_column(String(100), default="⚫ Offline")
    notify_videos: Mapped[bool] = mapped_column(Boolean, default=True)
    notify_shorts: Mapped[bool] = mapped_column(Boolean, default=False)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    is_live: Mapped[bool] = mapped_column(Boolean, default=False)
    current_session_id: Mapped[int | None] = mapped_column(Integer)
    live_message_id: Mapped[int | None] = mapped_column(BigInteger)
    live_channel_id: Mapped[int | None] = mapped_column(BigInteger)
    last_video_id: Mapped[str | None] = mapped_column(String(64))
    seen_video_ids: Mapped[list] = mapped_column(JSON, default=list)
    followers: Mapped[int | None] = mapped_column(Integer)
    subscribers: Mapped[int | None] = mapped_column(Integer)
    total_streams: Mapped[int] = mapped_column(Integer, default=0)
    last_clip_at: Mapped[datetime | None] = mapped_column(TS)
    last_checked: Mapped[datetime | None] = mapped_column(TS)
    last_error: Mapped[str | None] = mapped_column(String(300))
    created_at: Mapped[datetime] = _created()


class StreamSession(Base):
    __tablename__ = "stream_sessions"
    id: Mapped[int] = _id()
    guild_id: Mapped[int] = _guild()
    streamer_id: Mapped[int] = mapped_column(ForeignKey("streamers.id", ondelete="CASCADE"), index=True)
    platform_stream_id: Mapped[str | None] = mapped_column(String(64))
    title: Mapped[str] = mapped_column(String(300), default="")
    game: Mapped[str] = mapped_column(String(120), default="")
    started_at: Mapped[datetime] = _created()
    ended_at: Mapped[datetime | None] = mapped_column(TS)
    peak_viewers: Mapped[int] = mapped_column(Integer, default=0)
    viewer_sum: Mapped[int] = mapped_column(BigInteger, default=0)
    samples: Mapped[int] = mapped_column(Integer, default=0)
    checkins: Mapped[int] = mapped_column(Integer, default=0)

    @property
    def avg_viewers(self) -> int:
        return round(self.viewer_sum / self.samples) if self.samples else 0


class StreamSnapshot(Base):
    __tablename__ = "stream_snapshots"
    id: Mapped[int] = _id()
    streamer_id: Mapped[int] = mapped_column(ForeignKey("streamers.id", ondelete="CASCADE"))
    taken_at: Mapped[datetime] = mapped_column(TS, default=utcnow)
    live: Mapped[bool] = mapped_column(Boolean, default=False)
    viewers: Mapped[int] = mapped_column(Integer, default=0)
    followers: Mapped[int | None] = mapped_column(Integer)
    subscribers: Mapped[int | None] = mapped_column(Integer)
    __table_args__ = (Index("ix_stream_snapshots_streamer_time", "streamer_id", "taken_at"),)


class StreamCheckin(Base):
    __tablename__ = "stream_checkins"
    session_id: Mapped[int] = mapped_column(ForeignKey("stream_sessions.id", ondelete="CASCADE"), primary_key=True)
    user_id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    created_at: Mapped[datetime] = _created()


class StreamAlert(Base):
    """Persönlicher Stream-Alarm: DM, sobald ein bestimmter Streamer live geht."""
    __tablename__ = "stream_alerts"
    user_id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    streamer_id: Mapped[int] = mapped_column(ForeignKey("streamers.id", ondelete="CASCADE"), primary_key=True)
    guild_id: Mapped[int] = _guild()
    created_at: Mapped[datetime] = _created()


class Prediction(Base):
    """Stream-Wette: Zuschauer setzen Coins auf eine Antwort, Gewinner teilen den Pot."""
    __tablename__ = "predictions"
    id: Mapped[int] = _id()
    guild_id: Mapped[int] = _guild()
    channel_id: Mapped[int] = mapped_column(BigInteger)
    message_id: Mapped[int | None] = mapped_column(BigInteger)
    creator_id: Mapped[int] = mapped_column(BigInteger)
    question: Mapped[str] = mapped_column(String(200))
    options: Mapped[list] = mapped_column(JSON, default=list)
    status: Mapped[str] = mapped_column(String(10), default="open")  # open | locked | done | cancelled
    closes_at: Mapped[datetime] = mapped_column(TS)
    winner: Mapped[int | None] = mapped_column(Integer)
    created_at: Mapped[datetime] = _created()


class PredictionBet(Base):
    __tablename__ = "prediction_bets"
    prediction_id: Mapped[int] = mapped_column(ForeignKey("predictions.id", ondelete="CASCADE"), primary_key=True)
    user_id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    option: Mapped[int] = mapped_column(Integer)
    amount: Mapped[int] = mapped_column(BigInteger)


# ───────────────────────── Gangs ─────────────────────────
class Gang(Base):
    __tablename__ = "gangs"
    id: Mapped[int] = _id()
    guild_id: Mapped[int] = _guild()
    name: Mapped[str] = mapped_column(String(32))
    tag: Mapped[str] = mapped_column(String(6), default="")
    emoji: Mapped[str] = mapped_column(String(64), default="🏴")
    description: Mapped[str] = mapped_column(String(200), default="")
    leader_id: Mapped[int] = mapped_column(BigInteger)
    bank: Mapped[int] = mapped_column(BigInteger, default=0)
    wins: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = _created()
    __table_args__ = (UniqueConstraint("guild_id", "name"),)


class GangMember(Base):
    __tablename__ = "gang_members"
    guild_id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    user_id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    gang_id: Mapped[int] = mapped_column(ForeignKey("gangs.id", ondelete="CASCADE"), index=True)
    rank: Mapped[str] = mapped_column(String(10), default="member")  # leader | officer | member
    contributed: Mapped[int] = mapped_column(BigInteger, default=0)
    joined_at: Mapped[datetime] = _created()


# ───────────────────────── Clip-Contest ─────────────────────────
class ClipContest(Base):
    """Clip-Wettbewerb über einen Zeitraum: Community reicht TikTok/YouTube/Instagram-Clips ein, Aufrufe entscheiden."""
    __tablename__ = "clip_contests"
    id: Mapped[int] = _id()
    guild_id: Mapped[int] = _guild()
    name: Mapped[str] = mapped_column(String(100))
    channel_id: Mapped[int] = mapped_column(BigInteger)           # Ankündigung + Einreichen-Button
    message_id: Mapped[int | None] = mapped_column(BigInteger)
    host_id: Mapped[int] = mapped_column(BigInteger)
    starts_at: Mapped[datetime] = _created()
    ends_at: Mapped[datetime] = mapped_column(TS)
    ended: Mapped[bool] = mapped_column(Boolean, default=False, index=True)


class ClipSubmission(Base):
    __tablename__ = "clip_submissions"
    id: Mapped[int] = _id()
    contest_id: Mapped[int] = mapped_column(ForeignKey("clip_contests.id", ondelete="CASCADE"), index=True)
    guild_id: Mapped[int] = _guild()
    user_id: Mapped[int] = mapped_column(BigInteger, index=True)
    user_name: Mapped[str] = mapped_column(String(100), default="")
    url: Mapped[str] = mapped_column(String(300))
    platform: Mapped[str] = mapped_column(String(12))              # tiktok | youtube | instagram
    status: Mapped[str] = mapped_column(String(10), default="pending")  # pending | approved | denied
    views: Mapped[int] = mapped_column(BigInteger, default=0)      # vom Team bestätigt – nur diese zählen
    claimed_views: Mapped[int | None] = mapped_column(BigInteger)  # vom Einreicher gemeldet, wartet auf Bestätigung
    proof_url: Mapped[str | None] = mapped_column(String(500))
    message_id: Mapped[int | None] = mapped_column(BigInteger)
    reviewer_id: Mapped[int | None] = mapped_column(BigInteger)
    paid: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = _created()
    updated_at: Mapped[datetime | None] = mapped_column(TS)
    __table_args__ = (UniqueConstraint("contest_id", "url"),)


# ───────────────────────── Logs / Analytics / Betrieb ─────────────────────────
class LogEntry(Base):
    __tablename__ = "log_entries"
    id: Mapped[int] = _id()
    guild_id: Mapped[int] = _guild()
    category: Mapped[str] = mapped_column(String(20), index=True)
    action: Mapped[str] = mapped_column(String(40))
    user_id: Mapped[int | None] = mapped_column(BigInteger, index=True)
    user_name: Mapped[str | None] = mapped_column(String(100))
    target_id: Mapped[int | None] = mapped_column(BigInteger)
    channel_id: Mapped[int | None] = mapped_column(BigInteger)
    content: Mapped[str] = mapped_column(Text, default="")
    details: Mapped[dict] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(TS, default=utcnow)
    __table_args__ = (Index("ix_log_entries_guild_time", "guild_id", "created_at"),)


class AuditEntry(Base):
    __tablename__ = "audit_log"
    id: Mapped[int] = _id()
    guild_id: Mapped[int | None] = mapped_column(BigInteger, index=True)
    actor_id: Mapped[int | None] = mapped_column(BigInteger)
    actor_name: Mapped[str] = mapped_column(String(100), default="")
    source: Mapped[str] = mapped_column(String(12), default="dashboard")
    action: Mapped[str] = mapped_column(String(60))
    target: Mapped[str] = mapped_column(String(200), default="")
    details: Mapped[dict] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(TS, default=utcnow, index=True)


class ErrorLog(Base):
    __tablename__ = "error_logs"
    id: Mapped[str] = mapped_column(String(12), primary_key=True)
    guild_id: Mapped[int | None] = mapped_column(BigInteger, index=True)
    user_id: Mapped[int | None] = mapped_column(BigInteger)
    command: Mapped[str | None] = mapped_column(String(100))
    error_type: Mapped[str] = mapped_column(String(120))
    message: Mapped[str] = mapped_column(Text)
    traceback: Mapped[str] = mapped_column(Text)
    resolved: Mapped[bool] = mapped_column(Boolean, default=False, index=True)
    resolved_by: Mapped[int | None] = mapped_column(BigInteger)
    created_at: Mapped[datetime] = mapped_column(TS, default=utcnow, index=True)


class StatBucket(Base):
    """Stündliche Zähler für Analytics (messages, joins, leaves, voice, xp, coins, …)."""
    __tablename__ = "stat_buckets"
    guild_id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    bucket: Mapped[datetime] = mapped_column(TS, primary_key=True)
    metric: Mapped[str] = mapped_column(String(48), primary_key=True)
    value: Mapped[float] = mapped_column(Float, default=0)
    __table_args__ = (Index("ix_stat_buckets_metric_time", "guild_id", "metric", "bucket"),)


class RaidEvent(Base):
    __tablename__ = "raid_events"
    id: Mapped[int] = _id()
    guild_id: Mapped[int] = _guild()
    started_at: Mapped[datetime] = _created()
    ended_at: Mapped[datetime | None] = mapped_column(TS)
    join_count: Mapped[int] = mapped_column(Integer, default=0)
    actions: Mapped[list] = mapped_column(JSON, default=list)
    user_ids: Mapped[list] = mapped_column(JSON, default=list)
    resolved: Mapped[bool] = mapped_column(Boolean, default=False)


class Backup(Base):
    __tablename__ = "backups"
    id: Mapped[int] = _id()
    guild_id: Mapped[int | None] = mapped_column(BigInteger, index=True)
    filename: Mapped[str] = mapped_column(String(200))
    size: Mapped[int] = mapped_column(BigInteger, default=0)
    kind: Mapped[str] = mapped_column(String(12), default="guild")  # guild | full
    auto: Mapped[bool] = mapped_column(Boolean, default=False)
    created_by: Mapped[int | None] = mapped_column(BigInteger)
    created_at: Mapped[datetime] = _created()
