"""Guild-local jewel wallets and atomic, replay-safe casino games."""
from __future__ import annotations

import asyncio
from contextlib import closing
from datetime import datetime, timezone, timedelta
import json
import logging
import os
from pathlib import Path
import secrets
import sqlite3
import time

import discord
from discord import app_commands
from storage import database_path, mark_initialized

log = logging.getLogger(__name__)
INITIAL_JEWELS = 0
DAILY_JEWELS = 500
MIN_BET, MAX_BET = 10, 10000
MAX_BALANCE = 10**15
SYMBOLS = ('💎', '⭐', '🔔', '🍒', '🍇', '🍋')
TRIPLE_MULTIPLIERS = (50, 20, 15, 10, 8, 5)
JST = timezone(timedelta(hours=9))
GAME_NAMES = dict(slot='スロット', coinflip='コイントス', dice='サイコロ')


class CasinoError(Exception):
    pass


def draw(game, wager, choice=None):
    if game == 'slot':
        reels = [secrets.randbelow(len(SYMBOLS)) for _ in range(3)]
        distinct = len(set(reels))
        multiplier = TRIPLE_MULTIPLIERS[reels[0]] if distinct == 1 else (1 if distinct == 2 else 0)
        return {'outcome': ' '.join(SYMBOLS[index] for index in reels), 'payout': wager * multiplier}
    if game == 'coinflip':
        face = secrets.randbelow(2)
        return {'outcome': ('表', '裏')[face], 'choice': ('表', '裏')[choice],
                'payout': wager * 19 // 10 if face == choice else 0}
    number = secrets.randbelow(6) + 1
    return {'outcome': str(number), 'choice': str(choice), 'payout': wager * 11 // 2 if number == choice else 0}


class CasinoStore:
    def __init__(self, path=None):
        self.path = Path(path) if path is not None else database_path('casino')
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with closing(self.connect()) as db, db:
            db.executescript('''
                CREATE TABLE IF NOT EXISTS casino_wallets (
                    guild_id INTEGER NOT NULL,user_id INTEGER NOT NULL,
                    balance INTEGER NOT NULL CHECK(balance>=0),last_daily TEXT,last_play REAL,
                    PRIMARY KEY(guild_id,user_id));
                CREATE TABLE IF NOT EXISTS casino_transactions (
                    event_id TEXT PRIMARY KEY,guild_id INTEGER NOT NULL,user_id INTEGER NOT NULL,
                    kind TEXT NOT NULL,delta INTEGER NOT NULL,balance_after INTEGER NOT NULL,
                    payload TEXT NOT NULL,created_at REAL NOT NULL);
                CREATE INDEX IF NOT EXISTS casino_history ON casino_transactions(guild_id,user_id,created_at DESC);
                CREATE INDEX IF NOT EXISTS casino_ranking ON casino_wallets(guild_id,balance DESC,user_id);
            ''')
        mark_initialized(self.path)

    def connect(self):
        db = sqlite3.connect(self.path, timeout=10)
        db.row_factory = sqlite3.Row
        return db

    @staticmethod
    def wallet(db, guild_id, user_id):
        db.execute('INSERT OR IGNORE INTO casino_wallets(guild_id,user_id,balance) VALUES(?,?,?)',
                   (guild_id, user_id, INITIAL_JEWELS))
        return dict(db.execute('SELECT * FROM casino_wallets WHERE guild_id=? AND user_id=?', (guild_id, user_id)).fetchone())

    @staticmethod
    def replay(db, event_id, guild_id, user_id, kind):
        row = db.execute('SELECT * FROM casino_transactions WHERE event_id=?', (event_id,)).fetchone()
        if not row:
            return None
        if (row['guild_id'], row['user_id'], row['kind']) != (guild_id, user_id, kind):
            raise CasinoError('この操作は既に処理されています。')
        return json.loads(row['payload'])

    @staticmethod
    def finish(db, event_id, guild_id, user_id, kind, old_balance, result, now):
        balance = result['balance']
        if not 0 <= balance <= MAX_BALANCE:
            raise CasinoError('ジュエルの保有上限を超えるため処理できません。')
        if db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='blackjack_rounds'").fetchone():
            reserved = sum(row[0] * 5 // 2 for row in db.execute(
                "SELECT wager FROM blackjack_rounds WHERE guild_id=? AND user_id=? AND status='active'", (guild_id, user_id)))
            if balance + reserved > MAX_BALANCE:
                raise CasinoError('ブラックジャックの払い戻し分を確保するため、保有上限付近では処理できません。')
        db.execute('UPDATE casino_wallets SET balance=? WHERE guild_id=? AND user_id=?', (balance, guild_id, user_id))
        db.execute('INSERT INTO casino_transactions VALUES(?,?,?,?,?,?,?,?)',
                   (event_id, guild_id, user_id, kind, balance - old_balance, balance,
                    json.dumps(result, ensure_ascii=False), now))
        return result

    def balance(self, guild_id, user_id):
        with closing(self.connect()) as db, db:
            db.execute('BEGIN IMMEDIATE')
            return self.wallet(db, guild_id, user_id)['balance']

    def reward_levels(self, guild_id, user_id, before, after):
        """Pay each newly reached level once, with history and balance atomic."""
        if not 0 <= before <= 100 or not 0 <= after <= 100:
            raise ValueError('Invalid level')
        with closing(self.connect()) as db, db:
            db.execute('BEGIN IMMEDIATE')
            db.execute('''CREATE TABLE IF NOT EXISTS casino_level_rewards (
                guild_id INTEGER NOT NULL, user_id INTEGER NOT NULL,
                highest_level INTEGER NOT NULL, PRIMARY KEY(guild_id,user_id))''')
            db.execute('INSERT OR IGNORE INTO casino_level_rewards VALUES(?,?,?)',
                       (guild_id, user_id, before))
            highest = db.execute('SELECT highest_level FROM casino_level_rewards WHERE guild_id=? AND user_id=?',
                                 (guild_id, user_id)).fetchone()[0]
            target = max(highest, before, after)
            reward = max(0, after - max(highest, before)) * 200
            if reward:
                wallet = self.wallet(db, guild_id, user_id)
                result = dict(balance=wallet['balance'] + reward, reward=reward, level=after)
                self.finish(db, f'level:{guild_id}:{user_id}:{after}', guild_id, user_id,
                            'level_reward', wallet['balance'], result, time.time())
            db.execute('UPDATE casino_level_rewards SET highest_level=? WHERE guild_id=? AND user_id=?',
                       (target, guild_id, user_id))
            return reward

    def daily(self, guild_id, user_id, event_id, now=None):
        now = time.time() if now is None else now
        day = datetime.fromtimestamp(now, JST).date().isoformat()
        with closing(self.connect()) as db, db:
            db.execute('BEGIN IMMEDIATE')
            previous = self.replay(db, event_id, guild_id, user_id, 'daily')
            if previous is not None:
                return previous
            wallet = self.wallet(db, guild_id, user_id)
            if wallet['last_daily'] and wallet['last_daily'] >= day:
                raise CasinoError('本日のデイリー報酬は受け取り済みです。日本時間0時にリセットされます。')
            result = dict(balance=wallet['balance'] + DAILY_JEWELS, reward=DAILY_JEWELS)
            db.execute('UPDATE casino_wallets SET last_daily=? WHERE guild_id=? AND user_id=?', (day, guild_id, user_id))
            return self.finish(db, event_id, guild_id, user_id, 'daily', wallet['balance'], result, now)

    def play(self, guild_id, user_id, event_id, game, wager, choice=None, now=None):
        if game not in GAME_NAMES or not isinstance(wager, int) or not MIN_BET <= wager <= MAX_BET:
            raise CasinoError(f'賭け金は{MIN_BET}〜{MAX_BET:,}ジュエルで指定してください。')
        if game == 'coinflip' and choice not in (0, 1):
            raise CasinoError('表または裏を選んでください。')
        if game == 'dice' and (not isinstance(choice, int) or not 1 <= choice <= 6):
            raise CasinoError('1〜6を選んでください。')
        now = time.time() if now is None else now
        with closing(self.connect()) as db, db:
            db.execute('BEGIN IMMEDIATE')
            previous = self.replay(db, event_id, guild_id, user_id, game)
            if previous is not None:
                return previous
            wallet = self.wallet(db, guild_id, user_id)
            if wallet['last_play'] is not None and now - wallet['last_play'] < 3:
                raise CasinoError('ゲームは3秒ほど間隔をあけてください。')
            if wallet['balance'] < wager:
                raise CasinoError(f"ジュエルが足りません。現在の残高：💎 {wallet['balance']:,} ジュエル")
            result = draw(game, wager, choice)
            result.update(wager=wager, game=game, balance=wallet['balance'] - wager + result['payout'])
            db.execute('UPDATE casino_wallets SET last_play=? WHERE guild_id=? AND user_id=?', (now, guild_id, user_id))
            return self.finish(db, event_id, guild_id, user_id, game, wallet['balance'], result, now)

    def edit(self, guild_id, user_id, event_id, mode, amount, actor_id, now=None):
        if mode not in ('add', 'subtract', 'set') or not isinstance(amount, int) or not 0 <= amount <= 10**9:
            raise CasinoError('編集方法と0〜10億の数値を指定してください。')
        now = time.time() if now is None else now
        with closing(self.connect()) as db, db:
            db.execute('BEGIN IMMEDIATE')
            previous = self.replay(db, event_id, guild_id, user_id, 'edit')
            if previous is not None:
                return previous
            wallet = self.wallet(db, guild_id, user_id)
            balance = {'add': wallet['balance'] + amount, 'subtract': max(0, wallet['balance'] - amount), 'set': amount}[mode]
            result = dict(balance=balance, old_balance=wallet['balance'], mode=mode, amount=amount, actor_id=actor_id)
            return self.finish(db, event_id, guild_id, user_id, 'edit', wallet['balance'], result, now)

    def gift(self, guild_id, sender_id, recipient_id, event_id, amount, now=None):
        if sender_id == recipient_id:
            raise CasinoError('自分自身にはジュエルを送れません。')
        if not isinstance(amount, int) or not 1 <= amount <= 10**9:
            raise CasinoError('送るジュエルは1〜10億で指定してください。')
        now = time.time() if now is None else now
        with closing(self.connect()) as db, db:
            db.execute('BEGIN IMMEDIATE')
            previous = self.replay(db, event_id, guild_id, sender_id, 'gift_send')
            if previous is not None:
                if previous['recipient_id'] != recipient_id or previous['amount'] != amount:
                    raise CasinoError('処理済みの送金内容と一致しません。')
                return previous
            sender = self.wallet(db, guild_id, sender_id)
            if sender['balance'] < amount:
                raise CasinoError(f"ジュエルが足りません。現在の残高：💎 {sender['balance']:,} ジュエル")
            recipient = self.wallet(db, guild_id, recipient_id)
            if recipient['balance'] + amount > MAX_BALANCE:
                raise CasinoError('相手のジュエル保有上限を超えるため送れません。')
            sent = dict(balance=sender['balance'] - amount, amount=amount, recipient_id=recipient_id)
            received = dict(balance=recipient['balance'] + amount, amount=amount, sender_id=sender_id)
            # Both balances and both history entries commit together or all roll back.
            self.finish(db, event_id, guild_id, sender_id, 'gift_send', sender['balance'], sent, now)
            self.finish(db, event_id + ':received', guild_id, recipient_id, 'gift_receive', recipient['balance'], received, now)
            return sent

    def leaderboard(self, guild_id):
        with closing(self.connect()) as db:
            return [dict(row) for row in db.execute(
                'SELECT user_id,balance FROM casino_wallets WHERE guild_id=? ORDER BY balance DESC,user_id LIMIT 10', (guild_id,))]

    def history(self, guild_id, user_id):
        with closing(self.connect()) as db:
            return [dict(row) for row in db.execute(
                'SELECT kind,delta,balance_after,created_at FROM casino_transactions WHERE guild_id=? AND user_id=? ORDER BY created_at DESC,rowid DESC LIMIT 10',
                (guild_id, user_id))]


class Casino:
    def __init__(self, bot, store=None):
        self.bot = bot
        self.store = store or CasinoStore()

    async def execute(self, interaction, action, **values):
        await interaction.response.defer(thinking=True, ephemeral=action not in (*GAME_NAMES, 'leaderboard', 'gift'))
        if interaction.guild is None:
            await interaction.edit_original_response(content='このコマンドはサーバー内で使用してください。')
            return
        member = values.pop('member', None) or interaction.user
        if member.bot:
            await interaction.edit_original_response(content='Botはジュエルを利用できません。')
            return
        guild_id, user_id, event_id = interaction.guild_id, member.id, str(interaction.id)
        try:
            if action in GAME_NAMES:
                result = await asyncio.to_thread(self.store.play, guild_id, user_id, event_id, action, **values)
                net = result['payout'] - result['wager']
                embed = discord.Embed(title=f'🎰 {GAME_NAMES[action]}', description=result['outcome'],
                                      color=discord.Color.green() if net > 0 else discord.Color.red() if net < 0 else discord.Color.gold())
                if 'choice' in result:
                    embed.add_field(name='あなたの予想', value=result['choice'])
                embed.add_field(name='賭け金', value=f"💎 {result['wager']:,}")
                embed.add_field(name='払い戻し（賭け金込み）', value=f"💎 {result['payout']:,}")
                embed.add_field(name='収支', value=f'💎 {net:+,}')
                embed.add_field(name='残高', value=f"💎 {result['balance']:,} ジュエル", inline=False)
                await interaction.edit_original_response(embed=embed, allowed_mentions=discord.AllowedMentions.none())
                return
            if action == 'balance':
                balance = await asyncio.to_thread(self.store.balance, guild_id, user_id)
                text = f"<@{user_id}> の残高：**💎 {balance:,} ジュエル**"
            elif action == 'daily':
                result = await asyncio.to_thread(self.store.daily, guild_id, user_id, event_id)
                text = f"🎁 デイリー報酬 **💎 {result['reward']:,} ジュエル** を受け取りました！\n残高：💎 {result['balance']:,} ジュエル"
            elif action == 'gift':
                result = await asyncio.to_thread(self.store.gift, guild_id, interaction.user.id, member.id,
                                                 event_id, values['amount'])
                text = f"🎁 <@{interaction.user.id}> が <@{member.id}> に **💎 {result['amount']:,} ジュエル** を送りました！"
            elif action == 'edit':
                if not interaction.user.guild_permissions.manage_guild:
                    raise CasinoError('この操作には「サーバー管理」権限が必要です。')
                result = await asyncio.to_thread(self.store.edit, guild_id, user_id, event_id, actor_id=interaction.user.id, **values)
                text = f"<@{user_id}> のジュエルを編集しました。\n💎 {result['old_balance']:,} → {result['balance']:,} ジュエル"
            elif action == 'leaderboard':
                rows = await asyncio.to_thread(self.store.leaderboard, guild_id)
                text = '**💎 ジュエルランキング**\n' + ('\n'.join(
                    f"{index}位 <@{row['user_id']}> — 💎 {row['balance']:,}" for index, row in enumerate(rows, 1)) or 'まだデータがありません。')
            elif action == 'history':
                rows = await asyncio.to_thread(self.store.history, guild_id, user_id)
                text = '**💎 最近のジュエル履歴（最大10件）**\n' + ('\n'.join(
                    f"<t:{int(row['created_at'])}:f> {GAME_NAMES.get(row['kind'], {'level_reward': 'レベルアップ報酬', 'daily': 'デイリー', 'edit': '管理者編集', 'gift_send': '送金', 'gift_receive': '受取', 'shop': 'ショップ', 'blackjack_bet': 'ブラックジャック掛け金', 'blackjack': 'ブラックジャック払い戻し'}.get(row['kind'], row['kind']))}：💎 {row['delta']:+,} / 残高 {row['balance_after']:,}"
                    for row in rows) or 'まだ履歴がありません。')
            else:
                text = ('**🎰 カジノ / 💎ジュエル**\n初期残高：0ジュエル。デイリー：500ジュエル（日本時間0時更新）。\n'
                        '`/jewel balance` 残高 / `/jewel daily` デイリー / `/jewel history` 履歴\n'
                        '`/jewel leaderboard` ランキング / `/jewel edit` 管理者編集 / `/gift` ジュエルを送る\n'
                        '`/casino slot`：3つ揃い 💎50倍 / ⭐20倍 / 🔔15倍 / 🍒10倍 / 🍇8倍 / 🍋5倍。2つ揃いは1倍。\n'
                        '`/casino coinflip`：表・裏を当てると1.9倍。\n'
                        '`/casino dice`：1〜6を当てると5.5倍。\n'
                        '`/casino blackjack`：ヒット・スタンドで21を目指す。通常勝利2倍、最初の21は2.5倍、引き分けは返却。\n'
                        '賭け金：10〜10,000。倍率は賭け金を含む払い戻しで、小数点以下切り捨て。外れは0。\n'
                        'ジュエルはこのサーバー内のゲーム用通貨です。')
            await interaction.edit_original_response(content=text, allowed_mentions=discord.AllowedMentions.none())
        except CasinoError as error:
            await interaction.edit_original_response(content=str(error), allowed_mentions=discord.AllowedMentions.none())
        except (sqlite3.Error, OSError):
            log.exception('Casino storage error in guild %s', guild_id)
            await interaction.edit_original_response(content='ジュエルの保存に失敗しました。管理者にお知らせください。')


def register_casino(bot):
    service = Casino(bot)
    casino = app_commands.Group(name='casino', description='💎ジュエルで遊ぶカジノ', guild_only=True)
    jewel = app_commands.Group(name='jewel', description='💎ジュエルの残高・報酬', guild_only=True)

    @casino.command(name='help', description='カジノの遊び方・配当を表示します')
    async def help_command(interaction: discord.Interaction):
        await service.execute(interaction, 'help')

    @casino.command(name='slot', description='スロットで遊びます')
    @app_commands.describe(wager='賭けるジュエル（10〜10000）')
    async def slot(interaction: discord.Interaction, wager: app_commands.Range[int, 10, 10000]):
        await service.execute(interaction, 'slot', wager=wager)

    @casino.command(name='coinflip', description='コインの表・裏を予想します')
    @app_commands.choices(side=[app_commands.Choice(name='表', value=0), app_commands.Choice(name='裏', value=1)])
    @app_commands.describe(wager='賭けるジュエル（10〜10000）', side='予想する面')
    async def coinflip(interaction: discord.Interaction, wager: app_commands.Range[int, 10, 10000], side: int):
        await service.execute(interaction, 'coinflip', wager=wager, choice=side)

    @casino.command(name='dice', description='サイコロの出目を予想します')
    @app_commands.describe(wager='賭けるジュエル（10〜10000）', number='予想する出目（1〜6）')
    async def dice(interaction: discord.Interaction, wager: app_commands.Range[int, 10, 10000], number: app_commands.Range[int, 1, 6]):
        await service.execute(interaction, 'dice', wager=wager, choice=number)

    @jewel.command(name='balance', description='ジュエル残高を確認します')
    async def balance(interaction: discord.Interaction, member: discord.Member | None = None):
        await service.execute(interaction, 'balance', member=member)

    @jewel.command(name='daily', description='1日1回、500ジュエルを受け取ります')
    async def daily(interaction: discord.Interaction):
        await service.execute(interaction, 'daily')

    @jewel.command(name='history', description='自分の最近のジュエル履歴を確認します')
    async def history(interaction: discord.Interaction):
        await service.execute(interaction, 'history')

    @jewel.command(name='leaderboard', description='ジュエル残高ランキングを表示します')
    async def leaderboard(interaction: discord.Interaction):
        await service.execute(interaction, 'leaderboard')

    @jewel.command(name='edit', description='ジュエルを加算・減算・指定します（サーバー管理者）')
    @app_commands.default_permissions(manage_guild=True)
    @app_commands.choices(mode=[app_commands.Choice(name='加算', value='add'), app_commands.Choice(name='減算', value='subtract'), app_commands.Choice(name='指定', value='set')])
    async def edit(interaction: discord.Interaction, mode: str, amount: app_commands.Range[int, 0, 1000000000], member: discord.Member | None = None):
        await service.execute(interaction, 'edit', mode=mode, amount=amount, member=member)

    @bot.tree.command(name='gift', description='相手に💎ジュエルを送ります')
    @app_commands.guild_only()
    @app_commands.describe(member='送り先のメンバー', amount='送るジュエル（1〜10億）')
    async def gift(interaction: discord.Interaction, member: discord.Member, amount: app_commands.Range[int, 1, 1000000000]):
        await service.execute(interaction, 'gift', member=member, amount=amount)

    from blackjack import register_blackjack
    register_blackjack(bot, casino, service.store)
    from give import register_give
    register_give(bot, service.store, jewel)
    bot.tree.add_command(casino)
    bot.tree.add_command(jewel)
    return service
