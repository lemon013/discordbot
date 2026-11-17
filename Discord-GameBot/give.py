"""Persistent, one-claim-per-user jewel distribution boards."""
from __future__ import annotations

import asyncio
from contextlib import closing
from datetime import datetime, timezone
import logging
import re
import sqlite3
import time

import discord
from discord import app_commands
from discord.ext import tasks

from casino import CasinoError

OWNER_ID = 799820074990698496
log = logging.getLogger(__name__)
COLORS = {'赤': 0xE74C3C, '青': 0x3498DB, '黄': 0xF1C40F, '緑': 0x2ECC71,
          '水色': 0x1ABCFF, '紫': 0x9B59B6, '橙': 0xE67E22, 'ピンク': 0xFF69B4,
          '白': 0xFFFFFF, '黒': 0x000000, '金': 0xFFD700}


def duration_seconds(value):
    match = re.fullmatch(r'\s*(\d+)\s*(秒|分|時間|日|s|m|h|d)?\s*', value.lower())
    if not match:
        raise ValueError('時間は 60秒・30分・24時間・7日（60s・30m・24h・7d）で指定してください。数字のみは秒です。')
    seconds = int(match[1]) * {'秒': 1, 's': 1, '分': 60, 'm': 60, '時間': 3600,
                              'h': 3600, '日': 86400, 'd': 86400, None: 1}[match[2]]
    if not 1 <= seconds <= 366 * 86400:
        raise ValueError('配布時間は1秒～366日で指定してください。')
    return seconds


class GiveStore:
    def __init__(self, casino):
        self.casino = casino
        with closing(casino.connect()) as db, db:
            db.execute('''CREATE TABLE IF NOT EXISTS jewel_give_boards (
                board_id TEXT PRIMARY KEY, guild_id INTEGER NOT NULL,
                channel_id INTEGER NOT NULL, message_id INTEGER,
                amount INTEGER NOT NULL, label TEXT NOT NULL,
                expires_at REAL NOT NULL, disabled INTEGER NOT NULL DEFAULT 0)''')

    def create(self, board_id, guild_id, channel_id, amount, label, expires_at):
        with closing(self.casino.connect()) as db, db:
            db.execute('INSERT INTO jewel_give_boards(board_id,guild_id,channel_id,amount,label,expires_at) VALUES(?,?,?,?,?,?)',
                       (board_id, guild_id, channel_id, amount, label, expires_at))

    def attach(self, board_id, message_id):
        with closing(self.casino.connect()) as db, db:
            db.execute('UPDATE jewel_give_boards SET message_id=? WHERE board_id=?', (message_id, board_id))

    def pending(self):
        with closing(self.casino.connect()) as db:
            return [dict(r) for r in db.execute('SELECT * FROM jewel_give_boards WHERE disabled=0 AND message_id IS NOT NULL')]

    def disable(self, board_id):
        with closing(self.casino.connect()) as db, db:
            db.execute('UPDATE jewel_give_boards SET disabled=1 WHERE board_id=?', (board_id,))

    def claim(self, board_id, guild_id, message_id, user_id, now=None):
        now = time.time() if now is None else now
        with closing(self.casino.connect()) as db, db:
            db.execute('BEGIN IMMEDIATE')
            row = db.execute('SELECT * FROM jewel_give_boards WHERE board_id=?', (board_id,)).fetchone()
            if not row or (row['guild_id'], row['message_id']) != (guild_id, message_id):
                raise CasinoError('この配布ボードは利用できません。')
            if row['disabled'] or now >= row['expires_at']:
                raise CasinoError('このジュエル配布は終了しました。')
            event_id = f'give:{board_id}:{user_id}'
            if self.casino.replay(db, event_id, guild_id, user_id, 'give') is not None:
                raise CasinoError('このボードのジュエルは受取済みです（1人1回まで）。')
            wallet = self.casino.wallet(db, guild_id, user_id)
            result = dict(balance=wallet['balance'] + row['amount'], reward=row['amount'])
            return self.casino.finish(db, event_id, guild_id, user_id, 'give', wallet['balance'], result, now)


class GiveView(discord.ui.View):
    def __init__(self, service, board_id, label, disabled=False):
        super().__init__(timeout=None)
        button = discord.ui.Button(label=label, style=discord.ButtonStyle.primary,
                                   custom_id=f'jewel-give:{board_id}', disabled=disabled)

        async def callback(interaction):
            await service.claim(interaction, board_id)

        button.callback = callback
        self.add_item(button)


class GiveService:
    def __init__(self, bot, casino):
        self.bot = bot
        self.store = GiveStore(casino)

    async def claim(self, interaction, board_id):
        await interaction.response.defer(ephemeral=True, thinking=True)
        if interaction.guild_id is None or interaction.user.bot:
            await interaction.followup.send('サーバーのメンバーのみ受け取れます。', ephemeral=True)
            return
        try:
            result = await asyncio.to_thread(self.store.claim, board_id, interaction.guild_id,
                                             interaction.message.id, interaction.user.id)
            text = f"💎 {result['reward']:,}ジュエルを受け取りました！\n残高：{result['balance']:,}ジュエル"
        except CasinoError as error:
            text = str(error)
        except (sqlite3.Error, OSError):
            log.exception('Give claim failed')
            text = '受取結果を確認できませんでした。もう一度押してください。重複して付与されることはありません。'
        await interaction.followup.send(text, ephemeral=True)

    async def create(self, interaction, title, description, button, amount, color, duration):
        if interaction.user.id != OWNER_ID:
            await interaction.response.send_message('⚠️あなたには必要な権限がありません。', ephemeral=True)
            return
        if interaction.guild_id is None:
            await interaction.response.send_message('サーバー内で使用してください。', ephemeral=True)
            return
        try:
            seconds = duration_seconds(duration)
            color_value = COLORS.get(color)
            if color_value is None:
                raise ValueError('色は ' + '・'.join(COLORS) + ' から選んでください。')
            description = description.replace('\\n', '\n')
            if not 1 <= len(title) <= 256 or not 1 <= len(description) <= 4096 or not 1 <= len(button) <= 80:
                raise ValueError('タイトルは1～256文字、説明は1～4096文字、ボタン名は1～80文字で指定してください。')
            if not 1 <= amount <= 10**9:
                raise ValueError('配布するジュエルは1～10億で指定してください。')
        except ValueError as error:
            await interaction.response.send_message(str(error), ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True, thinking=True)
        board_id = str(interaction.id)
        expires_at = time.time() + seconds
        embed = discord.Embed(title=title, description=description, color=color_value,
                              timestamp=datetime.fromtimestamp(expires_at, timezone.utc))
        embed.add_field(name='配布ジュエル', value=f'💎 {amount:,}ジュエル（1人1回）')
        embed.add_field(name='配布終了', value=f'<t:{int(expires_at)}:F>\n<t:{int(expires_at)}:R>')
        embed.set_footer(text='配布終了日時')
        try:
            await asyncio.to_thread(self.store.create, board_id, interaction.guild_id, interaction.channel_id,
                                    amount, button, expires_at)
            # Attach the message ID before enabling claims; failed publication cannot pay out.
            message = await interaction.channel.send(embed=embed, view=GiveView(self, board_id, button, True),
                                                     allowed_mentions=discord.AllowedMentions.none())
            await asyncio.to_thread(self.store.attach, board_id, message.id)
            await message.edit(view=GiveView(self, board_id, button))
        except (discord.HTTPException, sqlite3.Error, OSError):
            log.exception('Give board publication failed')
            await interaction.followup.send('配布ボードを公開できませんでした。チャンネル権限と保存先を確認してください。', ephemeral=True)
            return
        await interaction.followup.send(f'配布ボードを作成しました。{message.jump_url}', ephemeral=True)

    async def on_ready(self):
        for row in await asyncio.to_thread(self.store.pending):
            self.bot.add_view(GiveView(self, row['board_id'], row['label']), message_id=row['message_id'])
        if not self.expire_tick.is_running():
            self.expire_tick.start()

    @tasks.loop(seconds=5)
    async def expire_tick(self):
        try:
            for row in await asyncio.to_thread(self.store.pending):
                if time.time() < row['expires_at']:
                    continue
                try:
                    channel = self.bot.get_channel(row['channel_id']) or await self.bot.fetch_channel(row['channel_id'])
                    await channel.get_partial_message(row['message_id']).edit(
                        view=GiveView(self, row['board_id'], row['label'], True))
                except discord.NotFound:
                    pass
                except discord.HTTPException:
                    log.warning('Could not disable expired give board %s; will retry', row['board_id'])
                    continue
                await asyncio.to_thread(self.store.disable, row['board_id'])
        except Exception:
            log.exception('Give expiry check failed; will retry')


def register_give(bot, casino, jewel):
    service = GiveService(bot, casino)

    @jewel.command(name='give', description='管理者専用：1人1回受け取れるジュエル配布ボードを作成')
    @app_commands.rename(title='タイトル', description='説明', button='ボタン名', amount='ジュエル',
                         color='色', duration='有効時間')
    @app_commands.describe(title='埋め込みボードのタイトル', description='説明（\\nで改行）',
                           button='受取ボタンの名前', amount='1人あたりの配布ジュエル',
                           color='埋め込みの色', duration='ボタンを無効にするまでの時間：30分・24時間・7日など')
    @app_commands.choices(color=[app_commands.Choice(name=k, value=k) for k in COLORS])
    async def give(interaction: discord.Interaction, title: str, description: str, button: str,
                   amount: app_commands.Range[int, 1, 1000000000], color: str, duration: str):
        await service.create(interaction, title, description, button, amount, color, duration)

    bot.add_listener(service.on_ready, 'on_ready')
    return service
