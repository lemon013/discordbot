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


def matching_roles(rules, roles):
    owned = set(roles)
    return [(role, percent) for role, percent in rules if role in owned]


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

    def for_roles(self, guild_id, roles):
        return matching_roles(self.list(guild_id), roles)

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
    percent = sum(percent for _, percent in matching_roles(rules, roles))
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
        embed.set_footer(text=f'{page}/{pages}ページ｜複数所持時は加算率を合計。小数分は繰り越し。')
        await interaction.edit_original_response(embed=embed, allowed_mentions=discord.AllowedMentions.none())

    async def on_member_update(before, after):
        if member_roles(before) != member_roles(after):
            await asyncio.to_thread(store.remember, after.guild.id, after.id, member_roles(after))

    bot.add_listener(on_member_update, 'on_member_update')
    bot.tree.add_command(group)


class RankBonusView(discord.ui.View):
    """Paginate the target member's role snapshot without additional API/DB polling."""
    def __init__(self, owner, base_embed, roles):
        super().__init__(timeout=180)
        self.owner, self.base_embed, self.roles = owner, base_embed, roles
        self.page, self.message = 0, None

    def embed(self):
        embed = self.base_embed.copy()
        total = sum(percent for _, percent in self.roles)
        embed.add_field(name='獲得XPボーナス合計', value=f'**＋{total}％**（通常の{100 + total}％獲得）', inline=False)
        lines = [f'<@&{role}>：＋{percent}％' for role, percent in self.roles[self.page * 10:(self.page + 1) * 10]]
        pages = max(1, (len(self.roles) + 9) // 10)
        embed.add_field(name=f'加算対象ロール（{self.page + 1}/{pages}ページ）',
            value='\n'.join(lines) or '対象ロールはありません。', inline=False)
        self.previous.disabled = self.page == 0
        self.next.disabled = self.page + 1 >= pages
        return embed

    async def interaction_check(self, interaction):
        if interaction.user.id == self.owner:
            return True
        await interaction.response.send_message('自分で /rank を実行して操作してください。', ephemeral=True)
        return False

    @discord.ui.button(label='前のロール', style=discord.ButtonStyle.secondary)
    async def previous(self, interaction, button):
        self.page = max(0, self.page - 1)
        await interaction.response.edit_message(embed=self.embed(), view=self, allowed_mentions=discord.AllowedMentions.none())

    @discord.ui.button(label='次のロール', style=discord.ButtonStyle.secondary)
    async def next(self, interaction, button):
        self.page = min(max(0, (len(self.roles) - 1) // 10), self.page + 1)
        await interaction.response.edit_message(embed=self.embed(), view=self, allowed_mentions=discord.AllowedMentions.none())

    async def on_timeout(self):
        for child in self.children:
            child.disabled = True
        if self.message:
            try:
                await self.message.edit(view=self)
            except discord.HTTPException:
                pass
