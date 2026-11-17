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
COLORS = {'��': 0xE74C3C, '��': 0x3498DB, '��': 0xF1C40F, '��': 0x2ECC71,
          '���F': 0x1ABCFF, '��': 0x9B59B6, '��': 0xE67E22, '�s���N': 0xFF69B4,
          '��': 0xFFFFFF, '��': 0x000000, '��': 0xFFD700}


def duration_seconds(value):
    match = re.fullmatch(r'\s*(\d+)\s*(�b|��|����|��|s|m|h|d)?\s*', value.lower())
    if not match:
        raise ValueError('���Ԃ� 60�b�E30���E24���ԁE7���i60s�E30m�E24h�E7d�j�Ŏw�肵�Ă��������B�����݂͕̂b�ł��B')
    seconds = int(match[1]) * {'�b': 1, 's': 1, '��': 60, 'm': 60, '����': 3600,
                              'h': 3600, '��': 86400, 'd': 86400, None: 1}[match[2]]
    if not 1 <= seconds <= 366 * 86400:
        raise ValueError('�z�z���Ԃ�1�b�`366���Ŏw�肵�Ă��������B')
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
                raise CasinoError('���̔z�z�{�[�h�͗��p�ł��܂���B')
            if row['disabled'] or now >= row['expires_at']:
                raise CasinoError('���̃W���G���z�z�͏I�����܂����B')
            event_id = f'give:{board_id}:{user_id}'
            if self.casino.replay(db, event_id, guild_id, user_id, 'give') is not None:
                raise CasinoError('���̃{�[�h�̃W���G���͎��ς݂ł��i1�l1��܂Łj�B')
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
            await interaction.followup.send('�T�[�o�[�̃����o�[�̂ݎ󂯎��܂��B', ephemeral=True)
            return
        try:
            result = await asyncio.to_thread(self.store.claim, board_id, interaction.guild_id,
                                             interaction.message.id, interaction.user.id)
            text = f"?? {result['reward']:,}�W���G�����󂯎��܂����I\n�c���F{result['balance']:,}�W���G��"
        except CasinoError as error:
            text = str(error)
        except (sqlite3.Error, OSError):
            log.exception('Give claim failed')
            text = '��挋�ʂ��m�F�ł��܂���ł����B������x�����Ă��������B�d�����ĕt�^����邱�Ƃ͂���܂���B'
        await interaction.followup.send(text, ephemeral=True)

    async def create(self, interaction, title, description, button, amount, color, duration):
        if interaction.user.id != OWNER_ID:
            await interaction.response.send_message('??���Ȃ��ɂ͕K�v�Ȍ���������܂���B', ephemeral=True)
            return
        if interaction.guild_id is None:
            await interaction.response.send_message('�T�[�o�[���Ŏg�p���Ă��������B', ephemeral=True)
            return
        try:
            seconds = duration_seconds(duration)
            color_value = COLORS.get(color)
            if color_value is None:
                raise ValueError('�F�� ' + '�E'.join(COLORS) + ' ����I��ł��������B')
            description = description.replace('\\n', '\n')
            if not 1 <= len(title) <= 256 or not 1 <= len(description) <= 4096 or not 1 <= len(button) <= 80:
                raise ValueError('�^�C�g����1�`256�����A������1�`4096�����A�{�^������1�`80�����Ŏw�肵�Ă��������B')
            if not 1 <= amount <= 10**9:
                raise ValueError('�z�z����W���G����1�`10���Ŏw�肵�Ă��������B')
        except ValueError as error:
            await interaction.response.send_message(str(error), ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True, thinking=True)
        board_id = str(interaction.id)
        expires_at = time.time() + seconds
        embed = discord.Embed(title=title, description=description, color=color_value,
                              timestamp=datetime.fromtimestamp(expires_at, timezone.utc))
        embed.add_field(name='�z�z�W���G��', value=f'?? {amount:,}�W���G���i1�l1��j')
        embed.add_field(name='�z�z�I��', value=f'<t:{int(expires_at)}:F>\n<t:{int(expires_at)}:R>')
        embed.set_footer(text='�z�z�I������')
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
            await interaction.followup.send('�z�z�{�[�h�����J�ł��܂���ł����B�`�����l�������ƕۑ�����m�F���Ă��������B', ephemeral=True)
            return
        await interaction.followup.send(f'�z�z�{�[�h���쐬���܂����B{message.jump_url}', ephemeral=True)

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

    @jewel.command(name='give', description='�Ǘ��Ґ�p�F1�l1��󂯎���W���G���z�z�{�[�h���쐬')
    @app_commands.rename(title='�^�C�g��', description='����', button='�{�^����', amount='�W���G��',
                         color='�F', duration='�L������')
    @app_commands.describe(title='���ߍ��݃{�[�h�̃^�C�g��', description='�����i\\n�ŉ��s�j',
                           button='���{�^���̖��O', amount='1�l������̔z�z�W���G��',
                           color='���ߍ��݂̐F', duration='�{�^���𖳌��ɂ���܂ł̎��ԁF30���E24���ԁE7���Ȃ�')
    @app_commands.choices(color=[app_commands.Choice(name=k, value=k) for k in COLORS])
    async def give(interaction: discord.Interaction, title: str, description: str, button: str,
                   amount: app_commands.Range[int, 1, 1000000000], color: str, duration: str):
        await service.create(interaction, title, description, button, amount, color, duration)

    bot.add_listener(service.on_ready, 'on_ready')
    return service
