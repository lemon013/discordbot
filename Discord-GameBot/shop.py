"""Jewel shop with atomic XP purchases and signed Minecraft cosmetics sync."""
from __future__ import annotations

import asyncio
from contextlib import closing
import hashlib
import hmac
import io
import json
import logging
import os
from pathlib import Path
import sqlite3
import time

import discord
from discord import app_commands
from discord.ext import tasks

from casino import CasinoStore, CasinoError
from leveling import DEFAULTS, LevelStore, level_for

log = logging.getLogger(__name__)
CATALOG = json.loads(Path(__file__).with_name('shop_catalog.json').read_text(encoding='utf-8'))
ITEMS = {item['id']: item for item in CATALOG}
ATTACHMENT_NAME = 'shop-entitlements.json'


class ShopError(Exception):
    pass


def signed_snapshot(guild_id, active, secret, stamp=None):
    payload = json.dumps(dict(version=1, guild_id=str(guild_id), sent_at=stamp or int(time.time() * 1000), active=active),
                         ensure_ascii=False, separators=(',', ':'))
    signature = hmac.new(secret.encode(), payload.encode(), hashlib.sha256).hexdigest()
    raw = json.dumps(dict(payload=payload, signature=signature), ensure_ascii=False).encode()
    if len(raw) > 2_000_000:
        raise ShopError('ショップ同期データが大きすぎます。')
    return raw


class ShopStore:
    def __init__(self, casino=None, levels=None):
        self.casino = casino or CasinoStore()
        self.levels = levels or LevelStore()
        with closing(self.casino.connect()) as db, db:
            db.executescript('''
                CREATE TABLE IF NOT EXISTS shop_owned (
                    guild_id INTEGER NOT NULL,user_id INTEGER NOT NULL,item_id TEXT NOT NULL,
                    purchased_at REAL NOT NULL,PRIMARY KEY(guild_id,user_id,item_id));
                CREATE TABLE IF NOT EXISTS shop_active (
                    guild_id INTEGER NOT NULL,user_id INTEGER NOT NULL,record TEXT,welcome TEXT,
                    PRIMARY KEY(guild_id,user_id));
            ''')

    def profile(self, guild_id, user_id):
        with closing(self.casino.connect()) as db, db:
            db.execute('BEGIN IMMEDIATE')
            balance = self.casino.wallet(db, guild_id, user_id)['balance']
            owned = [row[0] for row in db.execute('SELECT item_id FROM shop_owned WHERE guild_id=? AND user_id=?', (guild_id, user_id))]
            row = db.execute('SELECT record,welcome FROM shop_active WHERE guild_id=? AND user_id=?', (guild_id, user_id)).fetchone()
            return dict(balance=balance, owned=owned, active=dict(row) if row else {})

    def snapshot(self, guild_id):
        with closing(self.casino.connect()) as db:
            return {str(row['user_id']): dict(record=row['record'], welcome=row['welcome'])
                    for row in db.execute('SELECT * FROM shop_active WHERE guild_id=?', (guild_id,))}

    def buy(self, guild_id, user_id, event_id, item_id):
        item = ITEMS.get(item_id)
        if item is None:
            raise ShopError('商品が見つかりません。/shop を開き直してください。')
        with closing(self.casino.connect()) as db, db:
            alias = 'main'
            if item['kind'] == 'xp' and self.casino.path.resolve() != self.levels.path.resolve():
                db.execute('ATTACH DATABASE ? AS levels', (str(self.levels.path.resolve()),))
                alias = 'levels'
                # Rollback journals make both the payment and XP commit atomically.
                if any(db.execute(f'PRAGMA {name}.journal_mode').fetchone()[0] not in ('delete', 'truncate', 'persist')
                       for name in ('main', 'levels')):
                    raise ShopError('XP購入には通常のSQLiteジャーナル設定が必要です。管理者にお知らせください。')
            db.execute('BEGIN IMMEDIATE')
            replay = self.casino.replay(db, event_id, guild_id, user_id, 'shop')
            if replay is not None:
                if replay['item_id'] != item_id:
                    raise ShopError('処理済みの商品と一致しません。')
                return replay
            wallet = self.casino.wallet(db, guild_id, user_id)
            owned = db.execute('SELECT 1 FROM shop_owned WHERE guild_id=? AND user_id=? AND item_id=?',
                               (guild_id, user_id, item_id)).fetchone()
            price = 0 if owned else item['price']
            if wallet['balance'] < price:
                raise ShopError(f"ジュエルが足りません。必要：💎 {price:,} / 残高：💎 {wallet['balance']:,}")
            result = dict(item_id=item_id, price=price, balance=wallet['balance'] - price, equipped=bool(owned))
            if item['kind'] == 'xp':
                cfg_row = db.execute(f'SELECT * FROM {alias}.level_config WHERE guild_id=?', (guild_id,)).fetchone()
                cfg = {**DEFAULTS, **(dict(cfg_row) if cfg_row else {})}
                db.execute(f'INSERT OR IGNORE INTO {alias}.level_users(guild_id,user_id) VALUES(?,?)', (guild_id, user_id))
                before = db.execute(f'SELECT xp FROM {alias}.level_users WHERE guild_id=? AND user_id=?', (guild_id, user_id)).fetchone()[0]
                if before > 9223372036854775807 - item['xp']:
                    raise ShopError('XPの上限を超えるため購入できません。')
                db.execute(f'UPDATE {alias}.level_users SET xp=xp+?,shop_xp=shop_xp+? WHERE guild_id=? AND user_id=?', (item['xp'], item['xp'], guild_id, user_id))
                result.update(xp=item['xp'], before=level_for(before, cfg['base_xp'], cfg['step_xp']),
                              after=level_for(before + item['xp'], cfg['base_xp'], cfg['step_xp']))
            else:
                db.execute('INSERT OR IGNORE INTO shop_owned VALUES(?,?,?,?)', (guild_id, user_id, item_id, time.time()))
                db.execute('INSERT OR IGNORE INTO shop_active(guild_id,user_id) VALUES(?,?)', (guild_id, user_id))
                column = 'record' if item['kind'] == 'record' else 'welcome'
                db.execute(f'UPDATE shop_active SET {column}=? WHERE guild_id=? AND user_id=?', (item_id, guild_id, user_id))
            return self.casino.finish(db, event_id, guild_id, user_id, 'shop', wallet['balance'], result, time.time())


class ShopService:
    def __init__(self, bot, store=None):
        self.bot = bot
        self.store = store or ShopStore()
        self.sent = {}
        self.sync_lock = asyncio.Lock()

    async def sync(self, guild_id=None):
        channel_id = os.getenv('MC_SYNC_CHANNEL_ID', '')
        secret = os.getenv('MC_SYNC_SECRET', '')
        if not channel_id.isdecimal() or len(secret) < 32:
            return False
        async with self.sync_lock:
            channel = self.bot.get_channel(int(channel_id)) or await self.bot.fetch_channel(int(channel_id))
            if channel.guild is None or (guild_id is not None and channel.guild.id != guild_id):
                return False
            guild_id = channel.guild.id
            active = await asyncio.to_thread(self.store.snapshot, guild_id)
            digest = hashlib.sha256(json.dumps(active, sort_keys=True).encode()).hexdigest()
            previous_digest, previous_time = self.sent.get(guild_id, (None, 0))
            if previous_digest == digest and time.monotonic() - previous_time < 600:
                return True
            await channel.send(content='minecraft-shop-sync-v1',
                               file=discord.File(io.BytesIO(signed_snapshot(guild_id, active, secret)), filename=ATTACHMENT_NAME),
                               allowed_mentions=discord.AllowedMentions.none())
            self.sent[guild_id] = (digest, time.monotonic())
            return True

    @tasks.loop(seconds=60)
    async def sync_tick(self):
        try:
            await self.sync()
        except Exception:
            log.warning('Shop sync could not be sent; retrying on next tick')

    async def on_ready(self):
        if not self.sync_tick.is_running():
            self.sync_tick.start()

    async def show(self, interaction, *, replace=False):
        await interaction.response.defer(ephemeral=True, thinking=True)
        profile = await asyncio.to_thread(self.store.profile, interaction.guild_id, interaction.user.id)
        view = ShopView(self, interaction.user.id, profile)
        await interaction.edit_original_response(content=None, embed=self.board(profile), view=view,
                                                 allowed_mentions=discord.AllowedMentions.none())

    @staticmethod
    def board(profile):
        embed = discord.Embed(title='🛒 ジュエルショップ', description=f"残高：**💎 {profile['balance']:,} ジュエル**\n商品を1つ選んで内容を確認してください。", color=discord.Color.blue())
        for item in CATALOG:
            owned = item['id'] in profile['owned']
            equipped = item['id'] in profile['active'].values()
            suffix = '【装備中】' if equipped else '【購入済み・装備無料】' if owned else ''
            embed.add_field(name=f"{item['name']} — 💎 {item['price']:,} {suffix}", value=item['description'], inline=False)
        embed.set_footer(text='音・歓迎文は各1つ装備。Minecraft反映にはPCブリッジとPC Botでの/linkが必要です。')
        return embed

    async def purchase(self, interaction, item_id):
        await interaction.response.defer(ephemeral=True, thinking=True)
        try:
            result = await asyncio.to_thread(self.store.buy, interaction.guild_id, interaction.user.id, str(interaction.id), item_id)
        except (ShopError, CasinoError) as error:
            await interaction.edit_original_response(content=str(error), embed=None, view=None)
            return
        item = ITEMS[item_id]
        message = f"✅ {item['name']}を{'装備' if result['equipped'] else '購入'}しました！\n支払い：💎 {result['price']:,} / 残高：💎 {result['balance']:,}"
        profile = await asyncio.to_thread(self.store.profile, interaction.guild_id, interaction.user.id)
        await interaction.edit_original_response(content=message, embed=self.board(profile), view=ShopView(self, interaction.user.id, profile),
                                                 allowed_mentions=discord.AllowedMentions.none())
        if item['kind'] == 'xp':
            leveling = getattr(self.bot, 'leveling_service', None)
            if leveling:
                cfg = await asyncio.to_thread(leveling.store.config, interaction.guild_id)
                await leveling.notify(interaction.guild, [dict(user_id=interaction.user.id, before=result['before'], after=result['after'])], cfg, interaction.channel)
        else:
            try:
                synced = await self.sync(interaction.guild_id)
                if not synced:
                    await interaction.followup.send('Minecraftへの同期は未設定です。購入内容は保存済みです。PCブリッジの同期設定後に反映されます。', ephemeral=True)
            except Exception:
                log.warning('Shop purchase saved; Minecraft sync pending in guild %s', interaction.guild_id)
                await interaction.followup.send('購入内容は保存済みです。Minecraftへの同期は再試行します。', ephemeral=True)


class OwnedView(discord.ui.View):
    def __init__(self, service, owner):
        super().__init__(timeout=180)
        self.service, self.owner = service, owner

    async def interaction_check(self, interaction):
        if interaction.user.id != self.owner or interaction.guild is None:
            await interaction.response.send_message('このショップを操作できるのは開いた本人だけです。/shop で自分のショップを開いてください。', ephemeral=True)
            return False
        return True

    async def on_error(self, interaction, error, item):
        log.error('Shop interaction failed: %s', type(error).__name__)
        text = 'ショップの処理に失敗しました。再購入前に /jewel history と /rank を確認してください。'
        if interaction.response.is_done():
            await interaction.followup.send(text, ephemeral=True)
        else:
            await interaction.response.send_message(text, ephemeral=True)


class ShopView(OwnedView):
    def __init__(self, service, owner, profile):
        super().__init__(service, owner)
        select = discord.ui.Select(placeholder='商品を1つ選択', min_values=1, max_values=1,
            options=[discord.SelectOption(label=item['name'], value=item['id'],
                description='購入済み：無料で装備' if item['id'] in profile['owned'] else f"💎 {item['price']:,} ジュエル") for item in CATALOG])
        async def choose(interaction):
            await interaction.response.defer(ephemeral=True, thinking=True)
            item = ITEMS[select.values[0]]
            current = await asyncio.to_thread(service.store.profile, interaction.guild_id, interaction.user.id)
            owned = item['id'] in current['owned']
            embed = discord.Embed(title=item['name'], description=item['description'], color=discord.Color.blue())
            embed.add_field(name='価格', value='購入済み：装備無料' if owned else f"💎 {item['price']:,} ジュエル")
            if item.get('sound'):
                embed.add_field(name='効果音', value=f"`{item['sound']}`")
            await interaction.edit_original_response(embed=embed, view=ConfirmView(service, owner, item['id'], owned))
        select.callback = choose
        self.add_item(select)


class ConfirmView(OwnedView):
    def __init__(self, service, owner, item_id, owned):
        super().__init__(service, owner)
        self.used = False
        button = discord.ui.Button(label='装備する（無料）' if owned else '購入する', style=discord.ButtonStyle.success)
        async def purchase(interaction):
            if self.used:
                await interaction.response.send_message('この購入操作は処理済みです。商品一覧から選び直してください。', ephemeral=True)
                return
            self.used = True
            await service.purchase(interaction, item_id)
        button.callback = purchase
        self.add_item(button)

    @discord.ui.button(label='商品一覧に戻る', style=discord.ButtonStyle.secondary)
    async def back(self, interaction, button):
        await self.service.show(interaction)


def register_shop(bot):
    service = ShopService(bot)
    @bot.tree.command(name='shop', description='💎ジュエルでXP・Minecraft参加演出を購入します')
    @app_commands.guild_only()
    async def shop(interaction: discord.Interaction):
        if interaction.guild is None:
            await interaction.response.send_message('サーバー内で使用してください。', ephemeral=True)
            return
        await service.show(interaction)
    bot.add_listener(service.on_ready, 'on_ready')
    return service
