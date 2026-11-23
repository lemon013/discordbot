"""Persistent role XP bonuses, fractional carry, and lightweight membership snapshots."""
import asyncio
from contextlib import closing
import json
import logging
import sqlite3

import discord
from discord import app_commands

log = logging.getLogger(__name__)


def member_roles(member):
    roles = getattr(member, 'roles', None)
    return [role.id for role in roles] if isinstance(roles, (list, tuple)) else None


class BonusStore:
    def __init__(self, path):
        self.path = path
        with closing(sqlite3.connect(path)) as db, db:
            db.executescript('''
                CREATE TABLE IF NOT EXISTS xp_bonus_roles (
                    guild_id INTEGER, role_id INTEGER, percent INTEGER NOT NULL,
                    PRIMARY KEY(guild_id,role_id));
                CREATE TABLE IF NOT EXISTS xp_bonus_members (
                    guild_id INTEGER, user_id INTEGER, roles TEXT NOT NULL,
                    PRIMARY KEY(guild_id,user_id));
                CREATE TABLE IF NOT EXISTS xp_bonus_carry (
                    guild_id INTEGER, user_id INTEGER, remainder INTEGER NOT NULL,
                    PRIMARY KEY(guild_id,user_id));
            ''')

    def set(self, guild_id, role_id, percent):
        if type(percent) is not int or not 5 <= percent <= 1000 or percent % 5:
            raise ValueError('加算率は5〜1000％の範囲で、5％刻みにしてください。')
        with closing(sqlite3.connect(self.path)) as db, db:
            db.execute('INSERT OR REPLACE INTO xp_bonus_roles VALUES(?,?,?)', (guild_id, role_id, percent))

    def remove(self, guild_id, role_id):
        with closing(sqlite3.connect(self.path)) as db, db:
            db.execute('DELETE FROM xp_bonus_roles WHERE guild_id=? AND role_id=?', (guild_id, role_id))

    def list(self, guild_id):
        with closing(sqlite3.connect(self.path)) as db:
            return db.execute('SELECT role_id,percent FROM xp_bonus_roles WHERE guild_id=? ORDER BY percent DESC,role_id', (guild_id,)).fetchall()

    @staticmethod
    def remember_in(db, guild_id, user_id, roles):
        if roles is not None:
            db.execute('INSERT OR REPLACE INTO xp_bonus_members VALUES(?,?,?)',
                (guild_id, user_id, json.dumps(roles)))

    def remember(self, guild_id, user_id, roles):
        with closing(sqlite3.connect(self.path)) as db, db:
            self.remember_in(db, guild_id, user_id, roles)


def apply_bonus(db, alias, guild_id, user_id, amount):
    if alias not in ('main', 'levels'):
        raise ValueError('Invalid database alias')
    rules = db.execute(f'SELECT role_id,percent FROM {alias}.xp_bonus_roles WHERE guild_id=?', (guild_id,)).fetchall()
    if not rules or amount <= 0:
        return amount
    row = db.execute(f'SELECT roles FROM {alias}.xp_bonus_members WHERE guild_id=? AND user_id=?', (guild_id, user_id)).fetchone()
    roles = set(json.loads(row[0])) if row else set()
    percent = max((percent for role, percent in rules if role in roles), default=0)
    if not percent:
        return amount
    row = db.execute(f'SELECT remainder FROM {alias}.xp_bonus_carry WHERE guild_id=? AND user_id=?', (guild_id, user_id)).fetchone()
    extra, remainder = divmod(amount * percent + (row[0] if row else 0), 100)
    db.execute(f'INSERT OR REPLACE INTO {alias}.xp_bonus_carry VALUES(?,?,?)', (guild_id, user_id, remainder))
    return amount + extra


async def remember_roles(bot, guild, user_id, member=None):
    levels = getattr(getattr(bot, 'leveling_service', None), 'store', None)
    store = getattr(levels, 'bonuses', None)
    if not isinstance(store, BonusStore) or guild is None:
        return
    if not await asyncio.to_thread(store.list, guild.id):
        return
    explicit = member is not None
    if member is None:
        get = getattr(guild, 'get_member', None)
        member = get(user_id) if callable(get) else None
    roles = member_roles(member)
    if not explicit and isinstance(guild, discord.Guild):
        try:
            roles = member_roles(await guild.fetch_member(user_id))
        except discord.HTTPException:
            log.warning('Could not refresh XP bonus roles (guild_id=%s, user_id=%s)', guild.id, user_id)
            roles = []
    await asyncio.to_thread(store.remember, guild.id, user_id, roles)
    return roles


def register_xp_bonus(bot, leveling):
    store = leveling.store.bonuses
    group = app_commands.Group(name='xpbonus', description='ロールごとの獲得XP加算率を設定・確認します', guild_only=True)

    async def edit(interaction, role, percent=None):
        await interaction.response.defer(ephemeral=True, thinking=True)
        if not interaction.user.guild_permissions.manage_guild:
            await interaction.edit_original_response(content='「サーバー管理」権限が必要です。')
            return
        if role.guild.id != interaction.guild_id:
            await interaction.edit_original_response(content='このサーバーのロールを選んでください。')
            return
        try:
            if percent is None:
                await asyncio.to_thread(store.remove, interaction.guild_id, role.id)
                text = f'{role.mention} のXP加算設定を削除しました。'
            else:
                await asyncio.to_thread(store.set, interaction.guild_id, role.id, percent)
                text = f'{role.mention} の獲得XPを **＋{percent}％** に設定しました。'
        except ValueError as exc:
            text = str(exc)
        await interaction.edit_original_response(content=text, allowed_mentions=discord.AllowedMentions.none())

    @group.command(name='set', description='ロールのXP加算率を5％刻みで設定します（管理者）')
    @app_commands.describe(role='対象ロール', percent='5〜1000％、5の倍数。例：20なら獲得XP＋20％')
    async def set_bonus(interaction: discord.Interaction, role: discord.Role, percent: app_commands.Range[int, 5, 1000]):
        await edit(interaction, role, percent)

    @group.command(name='remove', description='ロールのXP加算設定を削除します（管理者）')
    async def remove(interaction: discord.Interaction, role: discord.Role):
        await edit(interaction, role)

    @group.command(name='list', description='XPが加算されるロールと加算率の一覧を表示します')
    async def listing(interaction: discord.Interaction, page: app_commands.Range[int, 1, 1000] = 1):
        await interaction.response.defer(thinking=True)
        rows = await asyncio.to_thread(store.list, interaction.guild_id)
        pages = max(1, (len(rows) + 9) // 10)
        page = min(page, pages)
        lines = [f'<@&{role}>：**＋{percent}％**（{100 + percent}％獲得）' for role, percent in rows[(page - 1) * 10:page * 10]]
        embed = discord.Embed(title='ロールXPボーナス一覧', description='\n'.join(lines) or '設定されているロールはありません。', color=discord.Color.blue())
        embed.set_footer(text=f'{page}/{pages}ページ｜複数所持時は最大の加算率のみ適用。小数分は繰り越し。')
        await interaction.edit_original_response(embed=embed, allowed_mentions=discord.AllowedMentions.none())

    async def on_member_update(before, after):
        if member_roles(before) != member_roles(after):
            await asyncio.to_thread(store.remember, after.guild.id, after.id, member_roles(after))

    bot.add_listener(on_member_update, 'on_member_update')
    bot.tree.add_command(group)
