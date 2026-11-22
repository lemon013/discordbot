"""Persistent blackjack rounds using the existing jewel wallet and ledger."""
from __future__ import annotations

import asyncio
from contextlib import closing
import json
import logging
import secrets
import sqlite3
import time

import discord
from discord import app_commands
from discord.ext import tasks

from casino import CasinoError, MIN_BET, MAX_BET

log = logging.getLogger(__name__)
TURN_SECONDS = 120
SUITS = ('♠', '♥', '♦', '♣')
RANKS = ('A', '2', '3', '4', '5', '6', '7', '8', '9', '10', 'J', 'Q', 'K')
OUTCOMES = dict(blackjack='ブラックジャック！', win='あなたの勝ち！', push='引き分け',
                lose='ディーラーの勝ち', bust='バスト！', dealer_blackjack='ディーラーがブラックジャック')


def hand_value(cards):
    value = sum(11 if card % 13 == 0 else min(card % 13 + 1, 10) for card in cards)
    aces = sum(card % 13 == 0 for card in cards)
    while value > 21 and aces:
        value -= 10
        aces -= 1
    return value


def new_deck():
    cards = list(range(52))
    secrets.SystemRandom().shuffle(cards)
    return cards


def cards_text(cards):
    return '  '.join(f'{SUITS[card // 13]}{RANKS[card % 13]}' for card in cards)


class BlackjackStore:
    def __init__(self, casino):
        self.casino = casino
        with closing(casino.connect()) as db, db:
            db.executescript('''
                CREATE TABLE IF NOT EXISTS blackjack_rounds (
                    round_id TEXT PRIMARY KEY,guild_id INTEGER NOT NULL,user_id INTEGER NOT NULL,
                    wager INTEGER NOT NULL,state TEXT NOT NULL,status TEXT NOT NULL,
                    revision INTEGER NOT NULL DEFAULT 0,expires_at REAL NOT NULL,created_at REAL NOT NULL,
                    channel_id INTEGER,message_id INTEGER);
                CREATE UNIQUE INDEX IF NOT EXISTS blackjack_one_active
                    ON blackjack_rounds(guild_id,user_id) WHERE status='active';
                CREATE TABLE IF NOT EXISTS blackjack_actions (
                    action_id TEXT PRIMARY KEY,round_id TEXT NOT NULL,created_at REAL NOT NULL);
            ''')

    @staticmethod
    def state(row):
        state = {**dict(row), **json.loads(row['state'])}
        if state.get('xp_reward'):
            state['xp_reward']['new'] = False
        return state

    @staticmethod
    def save(db, state):
        data = {key: state[key] for key in ('player', 'dealer', 'deck', 'outcome', 'payout')}
        data['xp_reward'] = state.get('xp_reward')
        db.execute('UPDATE blackjack_rounds SET state=?,status=?,revision=?,expires_at=? WHERE round_id=?',
                   (json.dumps(data), state['status'], state['revision'], state['expires_at'], state['round_id']))

    def settle(self, db, state, now, *, natural=False):
        if state['status'] != 'active':
            return state
        player = hand_value(state['player'])
        if natural:
            dealer = hand_value(state['dealer'])
            if player == 21 and dealer == 21:
                outcome, payout = 'push', state['wager']
            elif player == 21:
                outcome, payout = 'blackjack', state['wager'] * 5 // 2
            else:
                outcome, payout = 'dealer_blackjack', 0
        elif player > 21:
            outcome, payout = 'bust', 0
        else:
            # Stand on all 17s, including soft 17.
            while hand_value(state['dealer']) < 17:
                state['dealer'].append(state['deck'].pop())
            dealer = hand_value(state['dealer'])
            if dealer > 21 or player > dealer:
                outcome, payout = 'win', state['wager'] * 2
            elif player == dealer:
                outcome, payout = 'push', state['wager']
            else:
                outcome, payout = 'lose', 0
        state.update(status='done', outcome=outcome, payout=payout, revision=state['revision'] + 1)
        # Release the payout reservation before crediting, within the same transaction.
        self.save(db, state)
        wallet = self.casino.wallet(db, state['guild_id'], state['user_id'])
        result = dict(balance=wallet['balance'] + payout, wager=state['wager'], payout=payout, outcome=outcome)
        result['xp_reward'] = self.casino.award_game_xp(db, state['guild_id'], state['user_id'],
                                                     'blackjack:' + state['round_id'], payout > state['wager'])
        self.casino.finish(db, state['round_id'] + ':settle', state['guild_id'], state['user_id'],
                           'blackjack', wallet['balance'], result, now)
        state['balance'] = result['balance']
        state['xp_reward'] = result['xp_reward']
        self.save(db, state)
        return state

    def start(self, guild_id, user_id, round_id, wager=None, now=None):
        now = time.time() if now is None else now
        with closing(self.casino.connect()) as db, db:
            self.casino.prepare_game_db(db)
            db.execute('BEGIN IMMEDIATE')
            existing = db.execute('SELECT * FROM blackjack_rounds WHERE round_id=?', (round_id,)).fetchone()
            if existing:
                if (existing['guild_id'], existing['user_id']) != (guild_id, user_id):
                    raise CasinoError('このゲームは別のユーザーのものです。')
                state = self.state(existing)
                if state['status'] == 'active' and state['expires_at'] <= now:
                    state = self.settle(db, state, now)
                state['balance'] = self.casino.wallet(db, guild_id, user_id)['balance']
                return state
            active = db.execute("SELECT * FROM blackjack_rounds WHERE guild_id=? AND user_id=? AND status='active'", (guild_id, user_id)).fetchone()
            if active:
                state = self.state(active)
                if state['expires_at'] <= now:
                    state = self.settle(db, state, now)
                state['balance'] = self.casino.wallet(db, guild_id, user_id)['balance']
                return state  # Resume or settle the existing round, never charge a second bet.
            if not isinstance(wager, int) or not MIN_BET <= wager <= MAX_BET:
                raise CasinoError(f'新しく遊ぶには wager に{MIN_BET}〜{MAX_BET:,}ジュエルを指定してください。')
            wallet = self.casino.wallet(db, guild_id, user_id)
            if wallet['balance'] < wager:
                raise CasinoError(f"ジュエルが足りません。現在の残高：💎 {wallet['balance']:,} ジュエル")
            if wallet['last_play'] is not None and now - wallet['last_play'] < 3:
                raise CasinoError('ゲームは3秒ほど間隔をあけてください。')
            deck = new_deck()
            player, dealer = [deck.pop()], [deck.pop()]
            player.append(deck.pop())
            dealer.append(deck.pop())
            state = dict(round_id=round_id, guild_id=guild_id, user_id=user_id, wager=wager,
                         player=player, dealer=dealer, deck=deck, status='active', revision=0,
                         outcome=None, payout=0, expires_at=now + TURN_SECONDS)
            db.execute('INSERT INTO blackjack_rounds(round_id,guild_id,user_id,wager,state,status,expires_at,created_at) VALUES(?,?,?,?,?,?,?,?)',
                       (round_id, guild_id, user_id, wager, '{}', 'active', state['expires_at'], now))
            self.save(db, state)
            result = dict(balance=wallet['balance'] - wager, wager=wager)
            self.casino.finish(db, round_id + ':bet', guild_id, user_id, 'blackjack_bet', wallet['balance'], result, now)
            db.execute('UPDATE casino_wallets SET last_play=? WHERE guild_id=? AND user_id=?', (now, guild_id, user_id))
            state['balance'] = result['balance']
            if hand_value(player) == 21 or hand_value(dealer) == 21:
                state = self.settle(db, state, now, natural=True)
            return state

    def act(self, guild_id, user_id, round_id, action_id, action, revision, now=None):
        if action not in ('hit', 'stand'):
            raise CasinoError('ヒットまたはスタンドを選んでください。')
        now = time.time() if now is None else now
        with closing(self.casino.connect()) as db, db:
            self.casino.prepare_game_db(db)
            db.execute('BEGIN IMMEDIATE')
            row = db.execute('SELECT * FROM blackjack_rounds WHERE round_id=?', (round_id,)).fetchone()
            if row is None or (row['guild_id'], row['user_id']) != (guild_id, user_id):
                raise CasinoError('このゲームを操作できるのは開始した本人だけです。')
            state = self.state(row)
            previous = db.execute('SELECT round_id FROM blackjack_actions WHERE action_id=?', (action_id,)).fetchone()
            if previous:
                if previous['round_id'] != round_id:
                    raise CasinoError('この操作は処理済みです。')
            elif state['status'] == 'active':
                if state['expires_at'] <= now:
                    state = self.settle(db, state, now)
                elif state['revision'] != revision:
                    raise CasinoError('このボタンは操作済みです。最新のボードを使うか /casino blackjack で再表示してください。')
                else:
                    if action == 'hit':
                        state['player'].append(state['deck'].pop())
                    if action == 'stand' or hand_value(state['player']) >= 21:
                        state = self.settle(db, state, now)
                    else:
                        state.update(revision=state['revision'] + 1, expires_at=now + TURN_SECONDS)
                        self.save(db, state)
                db.execute('INSERT INTO blackjack_actions VALUES(?,?,?)', (action_id, round_id, now))
            state['balance'] = self.casino.wallet(db, guild_id, user_id)['balance']
            return state

    def bind_message(self, state, channel_id, message_id):
        with closing(self.casino.connect()) as db, db:
            db.execute('UPDATE blackjack_rounds SET channel_id=?,message_id=? WHERE round_id=? AND guild_id=? AND user_id=?',
                       (channel_id, message_id, state['round_id'], state['guild_id'], state['user_id']))

    def active(self):
        with closing(self.casino.connect()) as db:
            return [self.state(row) for row in db.execute("SELECT * FROM blackjack_rounds WHERE status='active'")]

    def expire(self, now=None):
        now = time.time() if now is None else now
        with closing(self.casino.connect()) as db, db:
            self.casino.prepare_game_db(db)
            db.execute('BEGIN IMMEDIATE')
            rows = db.execute("SELECT * FROM blackjack_rounds WHERE status='active' AND expires_at<=? LIMIT 100", (now,)).fetchall()
            return [self.settle(db, self.state(row), now) for row in rows]


def blackjack_embed(state):
    done = state['status'] == 'done'
    embed = discord.Embed(title='🃏 ブラックジャック',
        description=OUTCOMES[state['outcome']] if done else '「ヒット」で1枚引く／「スタンド」で勝負！',
        color=discord.Color.green() if done and state['payout'] > state['wager'] else discord.Color.blue())
    embed.add_field(name=f"あなたの手札 — {hand_value(state['player'])}", value=cards_text(state['player']), inline=False)
    dealer = cards_text(state['dealer']) if done else cards_text(state['dealer'][:1]) + '  🂠'
    embed.add_field(name=f"ディーラー — {hand_value(state['dealer'])}" if done else 'ディーラー（1枚非公開）', value=dealer, inline=False)
    embed.add_field(name='賭け金', value=f"💎 {state['wager']:,}")
    if done:
        embed.add_field(name='払い戻し（賭け金込み）', value=f"💎 {state['payout']:,}")
        embed.add_field(name='収支', value=f"💎 {state['payout'] - state['wager']:+,}")
    if 'balance' in state:
        embed.add_field(name='残高', value=f"💎 {state['balance']:,} ジュエル", inline=False)
    if not done:
        embed.add_field(name='自動スタンド', value=f"<t:{int(state['expires_at'])}:R>", inline=False)
    embed.set_footer(text='通常勝利2倍／最初の21は2.5倍／引き分け返却。2分操作がないと自動スタンド。')
    if state.get('xp_reward'):
        embed.add_field(name='経験値', value=f"+{state['xp_reward']['base_xp']} XP", inline=False)
    return embed


class BlackjackView(discord.ui.View):
    def __init__(self, service, state):
        super().__init__(timeout=None)
        self.service, self.state = service, state
        for action, label, style in [('hit', 'ヒット', discord.ButtonStyle.primary), ('stand', 'スタンド', discord.ButtonStyle.success)]:
            button = discord.ui.Button(label=label, style=style, custom_id=f"blackjack:{state['round_id']}:{action}")
            async def callback(interaction, action=action):
                await service.act(interaction, state, action)
            button.callback = callback
            self.add_item(button)

    async def interaction_check(self, interaction):
        if interaction.user.id != self.state['user_id'] or interaction.guild_id != self.state['guild_id']:
            await interaction.response.send_message('このゲームを操作できるのは開始した本人だけです。', ephemeral=True)
            return False
        return True


class BlackjackService:
    def __init__(self, bot, store):
        self.bot, self.store = bot, store

    async def start(self, interaction, wager=None):
        await interaction.response.defer(thinking=True)
        try:
            state = await asyncio.to_thread(self.store.start, interaction.guild_id, interaction.user.id, str(interaction.id), wager)
            if state.get('xp_reward'):
                from solo_rewards import notify_solo
                await notify_solo(self.bot, interaction.guild, interaction.channel, state['xp_reward'])
            view = BlackjackView(self, state) if state['status'] == 'active' else None
            message = await interaction.edit_original_response(embed=blackjack_embed(state), view=view, allowed_mentions=discord.AllowedMentions.none())
            await asyncio.to_thread(self.store.bind_message, state, interaction.channel_id, message.id)
        except CasinoError as error:
            await interaction.edit_original_response(content=str(error))
        except (sqlite3.Error, OSError):
            log.exception('Blackjack start/save failed')
            await interaction.edit_original_response(content='保存処理に失敗しました。再実行前に /jewel history を確認してください。')

    async def act(self, interaction, previous, action):
        await interaction.response.defer()
        try:
            state = await asyncio.to_thread(self.store.act, interaction.guild_id, interaction.user.id,
                previous['round_id'], str(interaction.id), action, previous['revision'])
            if state.get('xp_reward'):
                from solo_rewards import notify_solo
                await notify_solo(self.bot, interaction.guild, interaction.channel, state['xp_reward'])
            view = BlackjackView(self, state) if state['status'] == 'active' else None
            await interaction.edit_original_response(embed=blackjack_embed(state), view=view, allowed_mentions=discord.AllowedMentions.none())
        except CasinoError as error:
            await interaction.followup.send(str(error), ephemeral=True)
        except (sqlite3.Error, OSError):
            log.exception('Blackjack action/save failed')
            await interaction.followup.send('処理結果を確認するため /casino blackjack で再表示してください。', ephemeral=True)

    @tasks.loop(seconds=15)
    async def expire_tick(self):
        try:
            states = await asyncio.to_thread(self.store.expire)
            for state in states:
                if state.get('xp_reward'):
                    from solo_rewards import notify_solo
                    guild = self.bot.get_guild(state['guild_id'])
                    channel = self.bot.get_channel(state.get('channel_id')) if state.get('channel_id') else None
                    await notify_solo(self.bot, guild, channel, state['xp_reward'])
                if not state.get('channel_id') or not state.get('message_id'):
                    continue
                try:
                    channel = self.bot.get_channel(state['channel_id']) or await self.bot.fetch_channel(state['channel_id'])
                    await channel.get_partial_message(state['message_id']).edit(embed=blackjack_embed(state), view=None)
                except discord.HTTPException:
                    log.warning('Blackjack settled, but result message could not be updated')
        except Exception:
            log.exception('Blackjack timeout settlement failed; will retry')

    async def on_ready(self):
        for state in await asyncio.to_thread(self.store.active):
            if state.get('message_id') and state['expires_at'] > time.time():
                self.bot.add_view(BlackjackView(self, state), message_id=state['message_id'])
        if not self.expire_tick.is_running():
            self.expire_tick.start()


def register_blackjack(bot, group, casino_store):
    service = BlackjackService(bot, BlackjackStore(casino_store))
    @group.command(name='blackjack', description='ブラックジャックで遊びます。賭け金省略で進行中のゲームを再表示')
    @app_commands.describe(wager='賭けるジュエル（10〜10000）。進行中なら省略可能')
    async def blackjack(interaction: discord.Interaction, wager: app_commands.Range[int, 10, 10000] | None = None):
        await service.start(interaction, wager)
    bot.add_listener(service.on_ready, 'on_ready')
    return service
