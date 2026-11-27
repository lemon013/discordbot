"""Guild birthday reminders. Store only month/day, never the year of birth."""
import asyncio
from contextlib import closing
from datetime import date, datetime, timedelta, timezone
import logging
import sqlite3

import discord
from discord import app_commands
from discord.ext import tasks

from storage import data_directory

JST = timezone(timedelta(hours=9))
LOG = logging.getLogger(__name__)
MENTIONS = discord.AllowedMentions.none()


def query(sql, values=(), fetch=False):
    path = data_directory() / 'birthdays.sqlite3'
    path.parent.mkdir(parents=True, exist_ok=True)
    with closing(sqlite3.connect(path)) as db, db:
        db.execute("""CREATE TABLE IF NOT EXISTS birthday (
            guild INTEGER, member INTEGER, month INTEGER, day INTEGER,
            dm INTEGER, PRIMARY KEY (guild, member))""")
        db.execute("CREATE TABLE IF NOT EXISTS destination (guild INTEGER PRIMARY KEY, channel INTEGER)")
        db.execute("""CREATE TABLE IF NOT EXISTS delivered (
            guild INTEGER, member INTEGER, day TEXT, route TEXT,
            PRIMARY KEY (guild, member, day, route))""")
        rows = db.execute(sql, values)
        return rows.fetchall() if fetch else rows.rowcount


class Birthdays:
    def __init__(self, bot):
        self.bot = bot

    async def on_ready(self):
        if not self.reminders.is_running():
            self.reminders.start()

    @tasks.loop(minutes=1, reconnect=True)
    async def reminders(self):
        today = datetime.now(JST).date()
        day_key = today.isoformat()
        try:
            rows = await asyncio.to_thread(query, """
                SELECT b.guild, b.member, b.dm, d.channel FROM birthday b
                LEFT JOIN destination d ON d.guild = b.guild
                WHERE b.month = ? AND b.day = ?""", (today.month, today.day), True)
            for guild_id, member_id, dm, channel_id in rows:
                guild = self.bot.get_guild(guild_id)
                if guild is None or guild.unavailable:
                    continue
                try:
                    member = guild.get_member(member_id) or await guild.fetch_member(member_id)
                except discord.HTTPException:
                    continue
                channel = guild.get_channel(channel_id) if channel_id else None
                message = f'🎂 {member.display_name}さん、お誕生日おめでとうございます！'
                for route, target in (('channel', channel), ('dm', member if dm else None)):
                    if target is None:
                        continue
                    key = (guild_id, member_id, day_key, route)
                    claimed = await asyncio.to_thread(
                        query, 'INSERT OR IGNORE INTO delivered VALUES (?, ?, ?, ?)', key)
                    if not claimed:
                        continue
                    try:
                        await target.send(message, allowed_mentions=MENTIONS)
                    except discord.HTTPException as error:
                        LOG.warning('Birthday delivery failed (%s, HTTP %s)', route, error.status)
                        if error.status not in (403, 404):
                            await asyncio.to_thread(
                                query, 'DELETE FROM delivered WHERE guild=? AND member=? AND day=? AND route=?', key)
        except sqlite3.Error:
            LOG.exception('Could not check birthday reminders')


def register_birthdays(bot):
    group = app_commands.Group(name='birthday', description='誕生日の登録と通知設定', )
    app_commands.guild_only(group)
    reminders = Birthdays(bot)
    bot.add_listener(reminders.on_ready, 'on_ready')

    @group.command(name='set', description='誕生日を登録する（登録すると設定済みのチャンネルでお祝いされます）')
    async def set_birthday(interaction: discord.Interaction, month: app_commands.Range[int, 1, 12],
                           day: app_commands.Range[int, 1, 31], dm: bool = False):
        try:
            date(2000, month, day)
        except ValueError:
            await interaction.response.send_message('存在しない日付です。', ephemeral=True)
            return
        await asyncio.to_thread(query, 'INSERT OR REPLACE INTO birthday VALUES (?, ?, ?, ?, ?)',
                                (interaction.guild_id, interaction.user.id, month, day, dm))
        await interaction.response.send_message(
            f'{month}月{day}日を登録しました。設定済みのお祝いチャンネルに通知します。本人へのDMは{"オン" if dm else "オフ"}です。'
            '2月29日はうるう年に通知します。', ephemeral=True)

    @group.command(name='clear', description='このサーバーの誕生日登録を削除する')
    async def clear_birthday(interaction: discord.Interaction):
        await asyncio.to_thread(query, 'DELETE FROM birthday WHERE guild=? AND member=?',
                                (interaction.guild_id, interaction.user.id))
        await interaction.response.send_message('誕生日登録を削除しました。', ephemeral=True)

    @group.command(name='channel', description='お祝いチャンネルを設定する（省略すると解除）')
    @app_commands.default_permissions(manage_guild=True)
    @app_commands.checks.has_permissions(manage_guild=True)
    async def set_channel(interaction: discord.Interaction, channel: discord.TextChannel | None = None):
        if channel is None:
            await asyncio.to_thread(query, 'DELETE FROM destination WHERE guild=?', (interaction.guild_id,))
        else:
            permissions = channel.permissions_for(interaction.guild.me)
            if not permissions.send_messages or not permissions.view_channel:
                await interaction.response.send_message('Botがそのチャンネルを閲覧・送信できるようにしてください。',
                                                        ephemeral=True)
                return
            await asyncio.to_thread(query, 'INSERT OR REPLACE INTO destination VALUES (?, ?)',
                                    (interaction.guild_id, channel.id))
        await interaction.response.send_message(
            f'通知先を {channel.mention} に設定しました。' if channel else 'チャンネル通知を解除しました。',
            ephemeral=True)

    bot.tree.add_command(group)
