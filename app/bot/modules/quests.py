"""Community-Quests: /quests zeigt tägliche und wöchentliche Aufgaben mit Fortschritt."""
from __future__ import annotations

import discord
from discord import app_commands
from discord.ext import commands
from sqlalchemy import select

from app.bot.ui import reply
from app.core.embeds import progress_bar, theme
from app.core.guild_config import config
from app.core.i18n import i18n
from app.core.timeutil import day_key, local_now, ts, week_key
from app.db.base import SessionLocal
from app.db.models import Quest, QuestProgress


@app_commands.guild_only()
class Quests(commands.Cog):
    module = "quests"
    help_category = "economy"

    def __init__(self, bot: commands.Bot):
        self.bot = bot

    @app_commands.command(name="quests", description="Deine täglichen und wöchentlichen Quests")
    async def quests(self, interaction: discord.Interaction):
        _ = await i18n.for_guild(interaction.guild_id)
        th = await theme(interaction.guild)
        gen = await config.get(interaction.guild_id, "general")
        eco = await config.get(interaction.guild_id, "economy")
        tzname = gen.get("timezone")
        keys = {"daily": day_key(tzname), "weekly": week_key(tzname)}
        async with SessionLocal() as s:
            quests = (await s.execute(select(Quest).where(Quest.guild_id == interaction.guild_id, Quest.enabled.is_(True))
                                      .order_by(Quest.period, Quest.id))).scalars().all()
            progress = {(p.quest_id, p.period_key): p for p in (await s.execute(select(QuestProgress).where(
                QuestProgress.guild_id == interaction.guild_id, QuestProgress.user_id == interaction.user.id,
                QuestProgress.period_key.in_(list(keys.values()))))).scalars()}
        if not quests:
            await reply(interaction, th.info(_("quests.title"), _("quests.empty")))
            return
        e = th.embed(_("quests.title"), user=interaction.user, icon=False)
        now = local_now(tzname)
        from datetime import timedelta
        day_reset = now.replace(hour=0, minute=0, second=0, microsecond=0) + timedelta(days=1)
        week_reset = day_reset + timedelta(days=(7 - now.weekday() - 1) % 7)
        for period in ("daily", "weekly"):
            items = [q for q in quests if q.period == period]
            if not items:
                continue
            lines = []
            for q in items:
                p = progress.get((q.id, keys[period]))
                cur = p.progress if p else 0
                done = bool(p and p.completed_at)
                rewards = []
                if q.reward_xp:
                    rewards.append(f"✨ {q.reward_xp} XP")
                if q.reward_coins:
                    rewards.append(f"{eco.get('currency_emoji')} {q.reward_coins}")
                if q.reward_role_id:
                    rewards.append(f"🎭 <@&{q.reward_role_id}>")
                status = "✅" if done else "▫️"
                lines.append(f"{status} **{q.name}**\n{progress_bar(cur, q.target, 12)} `{cur}/{q.target}` · {' · '.join(rewards) or '—'}")
            reset = day_reset if period == "daily" else week_reset
            e.add_field(name=f"{'📅' if period == 'daily' else '🗓️'} {_('quests.' + period)} · {_('quests.resets')} {ts(reset, 'R')}",
                        value="\n".join(lines)[:1024], inline=False)
        await reply(interaction, e)


async def setup(bot: commands.Bot):
    await bot.add_cog(Quests(bot))
