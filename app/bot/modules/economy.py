"""Economy: /balance, /daily, /weekly (Streaks), /work, /pay, /shop, /inventory, /coins (Admin). Keine Echtgeld-Transaktionen."""
from __future__ import annotations

import random
from datetime import timedelta

import discord
from discord import app_commands
from discord.ext import commands
from sqlalchemy import select

from app.bot.ui import BaseView, fail, guarded, reply
from app.core.embeds import fmt_num, theme
from app.core.errors import UserError
from app.core.guild_config import config
from app.core.i18n import i18n
from app.core.records import audit
from app.core.timeutil import as_utc, local_now, ts, tz
from app.db.base import SessionLocal, session_scope, utcnow
from app.db.models import InventoryItem, Member, MemberBadge, ShopItem
from app.services import achievements, progression
from app.services.members import ensure_member


async def _currency(guild_id: int) -> tuple[str, str]:
    cfg = await config.get(guild_id, "economy")
    return cfg.get("currency_emoji") or "🪙", cfg.get("currency_name") or "Coins"


class BuyConfirm(BaseView):
    def __init__(self, cog: "Economy", owner_id: int, item_id: int, labels: tuple[str, str]):
        super().__init__(owner_id=owner_id, timeout=60)
        self.cog, self.item_id = cog, item_id
        self.buy.label, self.cancel.label = labels

    @discord.ui.button(style=discord.ButtonStyle.success, emoji="🛒")
    async def buy(self, interaction: discord.Interaction, _b: discord.ui.Button):
        embed = await self.cog.purchase(interaction, self.item_id)
        await interaction.response.edit_message(embed=embed, view=None)
        self.stop()

    @discord.ui.button(style=discord.ButtonStyle.secondary)
    async def cancel(self, interaction: discord.Interaction, _b: discord.ui.Button):
        await interaction.response.edit_message(view=None)
        self.stop()


class ShopSelect(discord.ui.Select):
    def __init__(self, items: list[ShopItem], emoji: str, placeholder: str):
        options = [discord.SelectOption(label=i.name[:100], value=str(i.id), description=f"{fmt_num(i.price)} · {i.description}"[:100],
                                        emoji=i.emoji or None) for i in items[:25]]
        super().__init__(placeholder=placeholder, options=options)

    async def callback(self, interaction: discord.Interaction):
        view: ShopView = self.view  # type: ignore[assignment]
        _ = await i18n.for_guild(interaction.guild_id)
        th = await theme(interaction.guild)
        emoji, name = await _currency(interaction.guild_id)
        async with SessionLocal() as s:
            item = await s.get(ShopItem, int(self.values[0]))
            bal = (await s.get(Member, (interaction.guild_id, interaction.user.id)))
        if item is None or not item.enabled:
            await fail(interaction, "eco.item_gone")
            return
        balance = bal.coins if bal else 0
        e = th.embed(f"{item.emoji} {item.name}", item.description or None, icon=False)
        e.add_field(name=_("eco.price"), value=f"{emoji} **{fmt_num(item.price)}**", inline=True)
        e.add_field(name=_("eco.your_balance"), value=f"{emoji} {fmt_num(balance)}", inline=True)
        if item.stock >= 0:
            e.add_field(name=_("eco.stock"), value=str(item.stock), inline=True)
        if item.role_id:
            e.add_field(name=_("eco.grants_role"), value=f"<@&{item.role_id}>", inline=True)
        await interaction.response.send_message(
            embed=e, view=BuyConfirm(view.cog, interaction.user.id, item.id, (_("eco.buy"), _("common.cancel"))), ephemeral=True)


class ShopView(BaseView):
    def __init__(self, cog: "Economy", items: list[ShopItem], emoji: str, placeholder: str):
        super().__init__(timeout=300)
        self.cog = cog
        self.add_item(ShopSelect(items, emoji, placeholder))


@app_commands.guild_only()
class Economy(commands.Cog):
    module = "economy"
    help_category = "economy"

    def __init__(self, bot: commands.Bot):
        self.bot = bot

    @app_commands.command(name="balance", description="Zeigt deinen Kontostand")
    async def balance(self, interaction: discord.Interaction, user: discord.Member | None = None):
        member = user or interaction.user
        _ = await i18n.for_guild(interaction.guild_id)
        th = await theme(interaction.guild)
        emoji, name = await _currency(interaction.guild_id)
        async with session_scope() as s:
            row = await ensure_member(s, interaction.guild_id, member)
            coins, earned, streak = row.coins, row.coins_earned, row.daily_streak
        e = th.embed(_("eco.balance_title", user=member.display_name), user=member, icon=False)
        e.add_field(name=name, value=f"{emoji} **{fmt_num(coins)}**", inline=True)
        e.add_field(name=_("eco.earned_total"), value=f"{emoji} {fmt_num(earned)}", inline=True)
        e.add_field(name=_("eco.streak"), value=f"🔥 {streak}", inline=True)
        e.set_thumbnail(url=member.display_avatar.url)
        await reply(interaction, e, ephemeral=False)

    @app_commands.command(name="daily", description="Tägliche Belohnung abholen (mit Streak)")
    async def daily(self, interaction: discord.Interaction):
        _ = await i18n.for_guild(interaction.guild_id)
        th = await theme(interaction.guild)
        cfg = await config.get(interaction.guild_id, "economy")
        gen = await config.get(interaction.guild_id, "general")
        emoji, name = await _currency(interaction.guild_id)
        zone = tz(gen.get("timezone"))
        today = local_now(gen.get("timezone")).date()
        async with session_scope() as s:
            row = await ensure_member(s, interaction.guild_id, interaction.user)
            last = as_utc(row.daily_last).astimezone(zone).date() if row.daily_last else None
            if last == today:
                reset = (local_now(gen.get("timezone")).replace(hour=0, minute=0, second=0, microsecond=0) + timedelta(days=1))
                raise UserError("eco.daily_claimed", next=ts(reset, "R"))
            row.daily_streak = row.daily_streak + 1 if last == today - timedelta(days=1) else 1
            row.daily_best_streak = max(row.daily_best_streak, row.daily_streak)
            row.daily_last = utcnow()
            streak = row.daily_streak
            amount = int(cfg.get("daily_amount", 250)) + int(cfg.get("daily_streak_bonus", 25)) * min(streak - 1, 100)
            bonus = 0
            if streak % 30 == 0:
                bonus += int(cfg.get("streak_30_bonus", 0))
            elif streak % 7 == 0:
                bonus += int(cfg.get("streak_7_bonus", 0))
            snapshot = row
        balance = await progression.add_coins(interaction.guild_id, interaction.user, amount + bonus, reason="daily")
        await progression.quest_progress(self.bot, interaction.guild, interaction.user.id, "daily_claims", 1)
        await achievements.check(self.bot, interaction.guild, snapshot, interaction.user)
        e = th.success(_("eco.daily_title"), _("eco.daily_desc", amount=f"{emoji} **{fmt_num(amount)}**", streak=streak))
        if bonus:
            e.add_field(name=_("eco.streak_bonus"), value=f"🎉 +{emoji} **{fmt_num(bonus)}**", inline=False)
        e.add_field(name=_("eco.new_balance"), value=f"{emoji} {fmt_num(balance)}", inline=True)
        nxt = 7 - streak % 7 if streak % 30 != 0 else 7
        e.add_field(name=_("eco.next_bonus"), value=_("eco.days_left", days=nxt), inline=True)
        await reply(interaction, e, ephemeral=False)

    @app_commands.command(name="weekly", description="Wöchentliche Belohnung abholen")
    async def weekly(self, interaction: discord.Interaction):
        _ = await i18n.for_guild(interaction.guild_id)
        th = await theme(interaction.guild)
        cfg = await config.get(interaction.guild_id, "economy")
        gen = await config.get(interaction.guild_id, "general")
        emoji, _n = await _currency(interaction.guild_id)
        zone = tz(gen.get("timezone"))
        now_local = local_now(gen.get("timezone"))
        this_week = now_local.isocalendar()[:2]
        async with session_scope() as s:
            row = await ensure_member(s, interaction.guild_id, interaction.user)
            last = as_utc(row.weekly_last).astimezone(zone) if row.weekly_last else None
            if last and last.isocalendar()[:2] == this_week:
                days = 7 - now_local.weekday()
                reset = now_local.replace(hour=0, minute=0, second=0, microsecond=0) + timedelta(days=days)
                raise UserError("eco.weekly_claimed", next=ts(reset, "R"))
            prev_week = (now_local - timedelta(days=7)).isocalendar()[:2]
            row.weekly_streak = row.weekly_streak + 1 if last and last.isocalendar()[:2] == prev_week else 1
            row.weekly_last = utcnow()
            streak = row.weekly_streak
        amount = int(cfg.get("weekly_amount", 2000)) + int(cfg.get("weekly_amount", 2000)) * min(streak - 1, 10) // 10
        balance = await progression.add_coins(interaction.guild_id, interaction.user, amount, reason="weekly")
        e = th.success(_("eco.weekly_title"), _("eco.weekly_desc", amount=f"{emoji} **{fmt_num(amount)}**", streak=streak))
        e.add_field(name=_("eco.new_balance"), value=f"{emoji} {fmt_num(balance)}")
        await reply(interaction, e, ephemeral=False)

    @app_commands.command(name="work", description="Arbeite für ein paar Coins")
    async def work(self, interaction: discord.Interaction):
        _ = await i18n.for_guild(interaction.guild_id)
        th = await theme(interaction.guild)
        cfg = await config.get(interaction.guild_id, "economy")
        emoji, _n = await _currency(interaction.guild_id)
        cooldown = timedelta(minutes=int(cfg.get("work_cooldown", 60)))
        async with session_scope() as s:
            row = await ensure_member(s, interaction.guild_id, interaction.user)
            if row.work_last and utcnow() - as_utc(row.work_last) < cooldown:
                raise UserError("eco.work_cooldown", next=ts(as_utc(row.work_last) + cooldown, "R"))
            row.work_last = utcnow()
        lo, hi = sorted((int(cfg.get("work_min", 50)), int(cfg.get("work_max", 200))))
        amount = random.randint(lo, hi)
        balance = await progression.add_coins(interaction.guild_id, interaction.user, amount, reason="work")
        job = random.choice(_("eco.jobs").split("|"))
        e = th.success(_("eco.work_title"), _("eco.work_desc", job=job, amount=f"{emoji} **{fmt_num(amount)}**"))
        e.add_field(name=_("eco.new_balance"), value=f"{emoji} {fmt_num(balance)}")
        await reply(interaction, e, ephemeral=False)

    @app_commands.command(name="pay", description="Überweise Coins an einen anderen User")
    async def pay(self, interaction: discord.Interaction, user: discord.Member, amount: app_commands.Range[int, 1, 1_000_000_000]):
        if user.bot or user.id == interaction.user.id:
            raise UserError("eco.pay_invalid")
        cfg = await config.get(interaction.guild_id, "economy")
        if cfg.get("max_pay") and amount > cfg["max_pay"]:
            raise UserError("eco.pay_max", max=fmt_num(cfg["max_pay"]))
        _ = await i18n.for_guild(interaction.guild_id)
        th = await theme(interaction.guild)
        emoji, _n = await _currency(interaction.guild_id)
        async with session_scope() as s:
            sender = await ensure_member(s, interaction.guild_id, interaction.user)
            if sender.coins < amount:
                raise UserError("eco.insufficient", balance=fmt_num(sender.coins))
            receiver = await ensure_member(s, interaction.guild_id, user)
            sender.coins -= amount
            receiver.coins += amount
            new_bal = sender.coins
        await audit(interaction.guild_id, interaction.user.id, str(interaction.user), "eco.pay", str(user), {"amount": amount}, source="bot")
        e = th.success(_("eco.pay_title"), _("eco.pay_desc", amount=f"{emoji} **{fmt_num(amount)}**", user=user.mention))
        e.add_field(name=_("eco.new_balance"), value=f"{emoji} {fmt_num(new_bal)}")
        await reply(interaction, e, ephemeral=False)

    @app_commands.command(name="shop", description="Community-Shop durchsuchen und kaufen")
    async def shop(self, interaction: discord.Interaction):
        _ = await i18n.for_guild(interaction.guild_id)
        th = await theme(interaction.guild)
        emoji, name = await _currency(interaction.guild_id)
        async with SessionLocal() as s:
            items = (await s.execute(select(ShopItem).where(ShopItem.guild_id == interaction.guild_id, ShopItem.enabled.is_(True))
                                     .order_by(ShopItem.position, ShopItem.price))).scalars().all()
            me = await s.get(Member, (interaction.guild_id, interaction.user.id))
        if not items:
            await reply(interaction, th.info(_("eco.shop_title"), _("eco.shop_empty")))
            return
        kinds = {"role": "🎭", "color": "🎨", "badge": "🎖️", "cosmetic": "✨", "event": "🎉"}
        lines = []
        for i in items[:25]:
            stock = "" if i.stock < 0 else f" · {_('eco.stock')}: {i.stock}"
            lines.append(f"{i.emoji} **{i.name}** — {emoji} `{fmt_num(i.price)}` {kinds.get(i.kind, '')}{stock}\n╰ {i.description or '—'}")
        e = th.embed(_("eco.shop_title"), "\n".join(lines)[:4000], icon=False)
        e.set_footer(text=_("eco.your_balance") + f": {fmt_num(me.coins if me else 0)} {name}")
        await reply(interaction, e, view=ShopView(self, list(items), emoji, _("eco.shop_placeholder")), ephemeral=False)

    async def purchase(self, interaction: discord.Interaction, item_id: int) -> discord.Embed:
        _ = await i18n.for_guild(interaction.guild_id)
        th = await theme(interaction.guild)
        emoji, _n = await _currency(interaction.guild_id)
        guild = interaction.guild
        role = None
        async with session_scope() as s:
            item = await s.get(ShopItem, item_id)
            if item is None or not item.enabled or item.guild_id != guild.id:
                return th.error(_("common.error"), _("eco.item_gone"))
            if item.stock == 0:
                return th.error(_("common.error"), _("eco.sold_out"))
            inv = (await s.execute(select(InventoryItem).where(InventoryItem.guild_id == guild.id, InventoryItem.user_id == interaction.user.id,
                                                               InventoryItem.item_id == item.id))).scalar_one_or_none()
            if inv and item.max_per_user and inv.quantity >= item.max_per_user:
                return th.error(_("common.error"), _("eco.max_owned"))
            if item.role_id:
                role = guild.get_role(item.role_id)
                if role is None or role >= guild.me.top_role:
                    return th.error(_("common.error"), _("eco.role_unavailable"))
            row = await ensure_member(s, guild.id, interaction.user)
            if row.coins < item.price:
                return th.error(_("common.error"), _("eco.insufficient", balance=fmt_num(row.coins)))
            row.coins -= item.price
            if item.stock > 0:
                item.stock -= 1
            if inv:
                inv.quantity += 1
            else:
                s.add(InventoryItem(guild_id=guild.id, user_id=interaction.user.id, item_id=item.id, quantity=1))
            if item.kind == "badge" and item.badge_id and not await s.get(MemberBadge, (guild.id, interaction.user.id, item.badge_id)):
                s.add(MemberBadge(guild_id=guild.id, user_id=interaction.user.id, badge_id=item.badge_id))
            name, price, balance = item.name, item.price, row.coins
        if role is not None:
            try:
                await interaction.user.add_roles(role, reason=f"Shop-Kauf: {name}")
            except discord.HTTPException:
                pass
        await audit(guild.id, interaction.user.id, str(interaction.user), "eco.buy", name, {"price": price}, source="bot")
        e = th.success(_("eco.bought_title"), _("eco.bought_desc", item=name, price=f"{emoji} {fmt_num(price)}"))
        e.add_field(name=_("eco.new_balance"), value=f"{emoji} {fmt_num(balance)}")
        return e

    @app_commands.command(name="inventory", description="Zeigt deine gekauften Items")
    async def inventory(self, interaction: discord.Interaction, user: discord.Member | None = None):
        member = user or interaction.user
        _ = await i18n.for_guild(interaction.guild_id)
        th = await theme(interaction.guild)
        async with SessionLocal() as s:
            rows = (await s.execute(select(InventoryItem, ShopItem).join(ShopItem, ShopItem.id == InventoryItem.item_id).where(
                InventoryItem.guild_id == interaction.guild_id, InventoryItem.user_id == member.id))).all()
        if not rows:
            await reply(interaction, th.info(_("eco.inv_title", user=member.display_name), _("eco.inv_empty")))
            return
        lines = [f"{item.emoji} **{item.name}** × {inv.quantity} · {ts(inv.acquired_at, 'd')}" for inv, item in rows]
        e = th.embed(_("eco.inv_title", user=member.display_name), "\n".join(lines)[:4000], user=member, icon=False)
        await reply(interaction, e, ephemeral=False)

    # ── Hood-Economy ──
    @app_commands.command(name="rob", description="Versuch, jemandem Coins abzuziehen – riskant!")
    async def rob(self, interaction: discord.Interaction, user: discord.Member):
        cfg = await config.get(interaction.guild_id, "economy")
        if not cfg.get("rob_enabled", True):
            raise UserError("errors.command_disabled")
        if user.bot or user.id == interaction.user.id:
            raise UserError("eco.rob_invalid")
        _ = await i18n.for_guild(interaction.guild_id)
        th = await theme(interaction.guild)
        emoji, _n = await _currency(interaction.guild_id)
        cooldown = timedelta(minutes=int(cfg.get("rob_cooldown", 120)))
        async with session_scope() as s:
            me = await ensure_member(s, interaction.guild_id, interaction.user)
            if me.rob_last and utcnow() - as_utc(me.rob_last) < cooldown:
                raise UserError("eco.rob_cooldown", next=ts(as_utc(me.rob_last) + cooldown, "R"))
            victim = await ensure_member(s, interaction.guild_id, user)
            if victim.coins < int(cfg.get("rob_min_victim", 200)):
                raise UserError("eco.rob_poor", user=user.mention)
            me.rob_last = utcnow()
            if random.randint(1, 100) <= int(cfg.get("rob_success", 45)):
                loot = max(1, random.randint(1, max(1, victim.coins * int(cfg.get("rob_max_percent", 20)) // 100)))
                victim.coins -= loot
                me.coins += loot
                result = th.success(_("eco.rob_win_title"), _("eco.rob_win", user=user.mention, amount=f"{emoji} **{fmt_num(loot)}**"))
            else:
                fine = min(me.coins, max(50, me.coins // 10))
                me.coins -= fine
                victim.coins += fine
                result = th.error(_("eco.rob_fail_title"), _("eco.rob_fail", user=user.mention, amount=f"{emoji} **{fmt_num(fine)}**"))
        await reply(interaction, result, ephemeral=False)

    @app_commands.command(name="crime", description="Ein krummes Ding drehen – großer Gewinn oder erwischt")
    async def crime(self, interaction: discord.Interaction):
        cfg = await config.get(interaction.guild_id, "economy")
        if not cfg.get("crime_enabled", True):
            raise UserError("errors.command_disabled")
        _ = await i18n.for_guild(interaction.guild_id)
        th = await theme(interaction.guild)
        emoji, _n = await _currency(interaction.guild_id)
        cooldown = timedelta(minutes=int(cfg.get("crime_cooldown", 30)))
        async with session_scope() as s:
            me = await ensure_member(s, interaction.guild_id, interaction.user)
            if me.crime_last and utcnow() - as_utc(me.crime_last) < cooldown:
                raise UserError("eco.crime_cooldown", next=ts(as_utc(me.crime_last) + cooldown, "R"))
            me.crime_last = utcnow()
            caught = random.randint(1, 100) <= int(cfg.get("crime_fail", 35))
            lo, hi = sorted((int(cfg.get("crime_min", 150)), int(cfg.get("crime_max", 600))))
            amount = random.randint(lo, hi)
            if caught:
                amount = min(me.coins, amount // 2)
                me.coins -= amount
            else:
                me.coins += amount
                me.coins_earned += amount
            balance = me.coins
        if caught:
            e = th.error(_("eco.crime_fail_title"), _("eco.crime_fail", story=random.choice(_("eco.crime_fail_stories").split("|")), amount=f"{emoji} **{fmt_num(amount)}**"))
        else:
            e = th.success(_("eco.crime_win_title"), _("eco.crime_win", story=random.choice(_("eco.crime_stories").split("|")), amount=f"{emoji} **{fmt_num(amount)}**"))
        e.add_field(name=_("eco.new_balance"), value=f"{emoji} {fmt_num(balance)}")
        await reply(interaction, e, ephemeral=False)

    async def _take_bet(self, interaction: discord.Interaction, amount: int) -> None:
        cfg = await config.get(interaction.guild_id, "economy")
        if not cfg.get("gambling_enabled", True):
            raise UserError("errors.command_disabled")
        if cfg.get("max_bet") and amount > cfg["max_bet"]:
            raise UserError("eco.bet_max", max=fmt_num(cfg["max_bet"]))
        async with session_scope() as s:
            me = await ensure_member(s, interaction.guild_id, interaction.user)
            if me.coins < amount:
                raise UserError("eco.insufficient", balance=fmt_num(me.coins))
            me.coins -= amount

    @app_commands.command(name="slots", description="Einarmiger Bandit – 3 gleiche Symbole gewinnen")
    async def slots(self, interaction: discord.Interaction, bet: app_commands.Range[int, 10, 100_000_000]):
        await self._take_bet(interaction, bet)
        _ = await i18n.for_guild(interaction.guild_id)
        th = await theme(interaction.guild)
        emoji, _n = await _currency(interaction.guild_id)
        symbols = ["🍒", "🍋", "🔔", "💎", "7️⃣", "💰"]
        weights = [30, 25, 20, 12, 8, 5]
        reel = random.choices(symbols, weights=weights, k=3)
        multi = {"💰": 25, "7️⃣": 15, "💎": 10, "🔔": 6, "🍋": 4, "🍒": 3}
        if reel[0] == reel[1] == reel[2]:
            win = bet * multi[reel[0]]
        elif len(set(reel)) == 2:
            win = bet * 2 if reel.count("🍒") < 2 else int(bet * 1.5)
        else:
            win = 0
        balance = await progression.add_coins(interaction.guild_id, interaction.user, win, reason="slots") if win else None
        if balance is None:
            async with SessionLocal() as s:
                balance = (await s.get(Member, (interaction.guild_id, interaction.user.id))).coins
        board = f"**[ {' | '.join(reel)} ]**"
        e = (th.success if win else th.error)(_("eco.slots_title"),
                                              f"{board}\n\n" + (_("eco.slots_win", amount=f"{emoji} **{fmt_num(win)}**") if win else _("eco.slots_lose", amount=f"{emoji} {fmt_num(bet)}")))
        e.add_field(name=_("eco.new_balance"), value=f"{emoji} {fmt_num(balance)}")
        await reply(interaction, e, ephemeral=False)

    @app_commands.command(name="bet", description="Coinflip um Coins – doppelt oder nichts")
    @app_commands.choices(side=[app_commands.Choice(name="Kopf", value="heads"), app_commands.Choice(name="Zahl", value="tails")])
    async def bet(self, interaction: discord.Interaction, amount: app_commands.Range[int, 10, 100_000_000], side: app_commands.Choice[str]):
        await self._take_bet(interaction, amount)
        _ = await i18n.for_guild(interaction.guild_id)
        th = await theme(interaction.guild)
        emoji, _n = await _currency(interaction.guild_id)
        result = random.choice(["heads", "tails"])
        won = result == side.value
        balance = await progression.add_coins(interaction.guild_id, interaction.user, amount * 2, reason="bet") if won else None
        if balance is None:
            async with SessionLocal() as s:
                balance = (await s.get(Member, (interaction.guild_id, interaction.user.id))).coins
        side_name = _("fun.heads") if result == "heads" else _("fun.tails")
        e = (th.success if won else th.error)(_("eco.bet_title"), _("eco.bet_win" if won else "eco.bet_lose", side=side_name, amount=f"{emoji} **{fmt_num(amount * 2 if won else amount)}**"))
        e.add_field(name=_("eco.new_balance"), value=f"{emoji} {fmt_num(balance)}")
        await reply(interaction, e, ephemeral=False)

    coins = app_commands.Group(name="coins", description="Coins verwalten (Admin)", guild_only=True,
                               default_permissions=discord.Permissions(manage_guild=True))

    @coins.command(name="give", description="Coins gutschreiben")
    async def coins_give(self, interaction: discord.Interaction, user: discord.Member, amount: app_commands.Range[int, 1, 1_000_000_000]):
        await self._admin_coins(interaction, user, amount)

    @coins.command(name="take", description="Coins abziehen")
    async def coins_take(self, interaction: discord.Interaction, user: discord.Member, amount: app_commands.Range[int, 1, 1_000_000_000]):
        await self._admin_coins(interaction, user, -amount)

    async def _admin_coins(self, interaction: discord.Interaction, user: discord.Member, delta: int):
        bal = await progression.add_coins(interaction.guild_id, user, delta, reason="admin")
        await audit(interaction.guild_id, interaction.user.id, str(interaction.user), "eco.admin", str(user), {"delta": delta}, source="bot")
        _ = await i18n.for_guild(interaction.guild_id)
        th = await theme(interaction.guild)
        emoji, _n = await _currency(interaction.guild_id)
        await reply(interaction, th.success(_("common.success"), _("eco.admin_done", user=user.mention, balance=f"{emoji} {fmt_num(bal)}")))


async def setup(bot: commands.Bot):
    await bot.add_cog(Economy(bot))
