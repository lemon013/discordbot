"""Persistent, per-guild text/voice XP without joining or recording voice calls."""
from __future__ import annotations

import asyncio
from contextlib import closing
from math import isqrt
import logging
import os
import random
from pathlib import Path
import sqlite3
import time

import discord
from discord import app_commands
from discord.ext import tasks

log = logging.getLogger(__name__)
MAX_LEVEL = 100
REGULAR_LEVEL = 30
REGULAR_ROLE_ID = 1370764628354666688
REGULAR_MESSAGE = '🎉常連ロールをゲットしました！おめでとうございます！'
DEFAULTS = dict(enabled=1, text_xp_min=5, text_xp_max=15, cooldown=60, voice_xp=10,
                base_xp=300, step_xp=50, notifications=1, notify_channel=None)


def threshold(level, base=300, step=50):
    return base * level + step * level * (level - 1) // 2


def level_for(xp, base=300, step=50):
    if step == 0:
        return min(MAX_LEVEL, xp // base)
    b = 2 * base - step
    return min(MAX_LEVEL, (isqrt(b * b + 8 * step * xp) - b) // (2 * step))


class LevelStore:
    """Short SQLite transactions. Call blocking methods through asyncio.to_thread."""
    def __init__(self, path=None):
        self.path = Path(path or os.getenv('LEVEL_DB_PATH') or Path(__file__).with_name('levels.sqlite3'))
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with closing(self.connect()) as db, db:
            db.executescript('''
                CREATE TABLE IF NOT EXISTS level_config (
                    guild_id INTEGER PRIMARY KEY, enabled INTEGER NOT NULL DEFAULT 1,
                    text_xp INTEGER NOT NULL DEFAULT 15,
                    text_xp_min INTEGER NOT NULL DEFAULT 5, text_xp_max INTEGER NOT NULL DEFAULT 15,
                    cooldown INTEGER NOT NULL DEFAULT 60,
                    voice_xp INTEGER NOT NULL DEFAULT 10, base_xp INTEGER NOT NULL DEFAULT 300,
                    step_xp INTEGER NOT NULL DEFAULT 50, notifications INTEGER NOT NULL DEFAULT 1,
                    notify_channel INTEGER);
                CREATE TABLE IF NOT EXISTS level_users (
                    guild_id INTEGER NOT NULL, user_id INTEGER NOT NULL,
                    xp INTEGER NOT NULL DEFAULT 0 CHECK(xp >= 0),
                    text_xp INTEGER NOT NULL DEFAULT 0, voice_xp INTEGER NOT NULL DEFAULT 0,
                    last_text REAL, PRIMARY KEY(guild_id, user_id));
                CREATE INDEX IF NOT EXISTS level_ranking ON level_users(guild_id, xp DESC, user_id);
            ''')

            user_columns = {row['name'] for row in db.execute('PRAGMA table_info(level_users)')}
            if 'shop_xp' not in user_columns:
                db.execute('ALTER TABLE level_users ADD COLUMN shop_xp INTEGER NOT NULL DEFAULT 0')
            # Existing installations keep all XP and receive the new random range.
            columns = {row['name'] for row in db.execute('PRAGMA table_info(level_config)')}
            migrated = False
            for name, default in (('text_xp_min', 5), ('text_xp_max', 15)):
                if name not in columns:
                    db.execute(f'ALTER TABLE level_config ADD COLUMN {name} INTEGER NOT NULL DEFAULT {default}')
                    migrated = True
            if migrated:
                db.execute('UPDATE level_config SET text_xp_min=0,text_xp_max=0 WHERE text_xp=0')

    def connect(self):
        db = sqlite3.connect(self.path, timeout=10)
        db.row_factory = sqlite3.Row
        return db

    @staticmethod
    def config_in(db, guild_id):
        row = db.execute('SELECT * FROM level_config WHERE guild_id=?', (guild_id,)).fetchone()
        return {**DEFAULTS, **(dict(row) if row else {})}

    def config(self, guild_id):
        with closing(self.connect()) as db:
            return self.config_in(db, guild_id)

    def configure(self, guild_id, changes):
        limits = dict(text_xp_min=(0, 10000), text_xp_max=(0, 10000), voice_xp=(0, 10000), cooldown=(5, 86400),
                      base_xp=(1, 1000000), step_xp=(0, 1000000), enabled=(0, 1), notifications=(0, 1))
        for key, value in changes.items():
            if key == 'notify_channel':
                if value is not None and (not isinstance(value, int) or value <= 0):
                    raise ValueError('Invalid notification channel')
            elif key not in limits or not isinstance(value, int) or not limits[key][0] <= value <= limits[key][1]:
                raise ValueError('Invalid leveling setting')
        with closing(self.connect()) as db, db:
            db.execute('BEGIN IMMEDIATE')
            candidate = {**self.config_in(db, guild_id), **changes}
            if candidate['text_xp_min'] > candidate['text_xp_max']:
                raise ValueError('Minimum text XP must not exceed maximum text XP')
            db.execute('INSERT OR IGNORE INTO level_config(guild_id) VALUES(?)', (guild_id,))
            for key, value in changes.items():
                db.execute(f'UPDATE level_config SET {key}=? WHERE guild_id=?', (value, guild_id))
            return self.config_in(db, guild_id)

    @staticmethod
    def add_in(db, guild_id, user_id, amount, source, cfg, now=None):
        row = db.execute('SELECT * FROM level_users WHERE guild_id=? AND user_id=?', (guild_id, user_id)).fetchone()
        xp = row['xp'] if row else 0
        if source == 'text_xp' and row and row['last_text'] is not None and now - row['last_text'] < cfg['cooldown']:
            return None
        db.execute('INSERT OR IGNORE INTO level_users(guild_id,user_id) VALUES(?,?)', (guild_id, user_id))
        db.execute(f'UPDATE level_users SET xp=xp+?, {source}={source}+? WHERE guild_id=? AND user_id=?',
                   (amount, amount, guild_id, user_id))
        if source == 'text_xp':
            db.execute('UPDATE level_users SET last_text=? WHERE guild_id=? AND user_id=?', (now, guild_id, user_id))
        before = level_for(xp, cfg['base_xp'], cfg['step_xp'])
        after = level_for(xp + amount, cfg['base_xp'], cfg['step_xp'])
        return dict(user_id=user_id, before=before, after=after, xp=xp + amount)

    def award_text(self, guild_id, user_id, now=None):
        now = time.time() if now is None else now
        with closing(self.connect()) as db, db:
            db.execute('BEGIN IMMEDIATE')
            cfg = self.config_in(db, guild_id)
            if not cfg['enabled'] or not cfg['text_xp_max']:
                return None, cfg
            amount = random.randint(cfg['text_xp_min'], cfg['text_xp_max'])
            return self.add_in(db, guild_id, user_id, amount, 'text_xp', cfg, now), cfg

    def award_voice(self, guild_id, minutes):
        with closing(self.connect()) as db, db:
            db.execute('BEGIN IMMEDIATE')
            cfg = self.config_in(db, guild_id)
            if not cfg['enabled'] or not cfg['voice_xp']:
                return [], cfg
            results = [self.add_in(db, guild_id, user_id, count * cfg['voice_xp'], 'voice_xp', cfg)
                       for user_id, count in minutes if count > 0]
            return results, cfg

    def edit(self, guild_id, user_id, unit, mode, amount):
        if unit not in ('xp', 'level') or mode not in ('add', 'subtract', 'set'):
            raise ValueError('Invalid edit operation')
        if not isinstance(amount, int) or amount < 0 or amount > (MAX_LEVEL if unit == 'level' else 1000000000):
            raise ValueError('Invalid edit amount')
        with closing(self.connect()) as db, db:
            db.execute('BEGIN IMMEDIATE')
            cfg = self.config_in(db, guild_id)
            row = db.execute('SELECT xp FROM level_users WHERE guild_id=? AND user_id=?',
                             (guild_id, user_id)).fetchone()
            old_xp = row['xp'] if row else 0
            old_level = level_for(old_xp, cfg['base_xp'], cfg['step_xp'])
            current = old_xp if unit == 'xp' else old_level
            target = {'add': current + amount, 'subtract': max(0, current - amount), 'set': amount}[mode]
            if unit == 'level':
                target = min(MAX_LEVEL, target)
                # Adding/subtracting zero or hitting a bound leaves progress intact.
                new_xp = old_xp if mode != 'set' and target == old_level else threshold(target, cfg['base_xp'], cfg['step_xp'])
            else:
                new_xp = target
            if new_xp > 9223372036854775807:
                raise ValueError('XP is too large')
            db.execute('INSERT OR IGNORE INTO level_users(guild_id,user_id) VALUES(?,?)', (guild_id, user_id))
            # Keep earned text/voice totals and cooldowns intact; the difference is admin adjustment.
            db.execute('UPDATE level_users SET xp=? WHERE guild_id=? AND user_id=?', (new_xp, guild_id, user_id))
            return dict(user_id=user_id, old_xp=old_xp, xp=new_xp, before=old_level,
                        after=level_for(new_xp, cfg['base_xp'], cfg['step_xp'])), cfg

    def rank(self, guild_id, user_id):
        with closing(self.connect()) as db:
            cfg = self.config_in(db, guild_id)
            row = db.execute('SELECT * FROM level_users WHERE guild_id=? AND user_id=?', (guild_id, user_id)).fetchone()
            user = dict(row) if row else dict(xp=0, text_xp=0, voice_xp=0, shop_xp=0)
            user['rank'] = None if not row else 1 + db.execute(
                'SELECT COUNT(*) FROM level_users WHERE guild_id=? AND (xp>? OR (xp=? AND user_id<?))',
                (guild_id, user['xp'], user['xp'], user_id)).fetchone()[0]
            return user, cfg

    def leaderboard(self, guild_id, page):
        with closing(self.connect()) as db:
            rows = db.execute('SELECT user_id,xp FROM level_users WHERE guild_id=? ORDER BY xp DESC,user_id LIMIT 10 OFFSET ?',
                              (guild_id, (page - 1) * 10)).fetchall()
            return [dict(row) for row in rows], self.config_in(db, guild_id)


def eligible_voice(guild):
    """Mute is allowed; deaf, AFK, bots, solo rooms and stage audience are excluded."""
    eligible = set()
    for channel in guild.voice_channels:
        if channel == guild.afk_channel:
            continue
        humans = [m for m in channel.members if not m.bot and m.voice is not None
                  and not m.voice.deaf and not m.voice.self_deaf and not m.voice.suppress]
        if len(humans) >= 2:
            eligible.update(m.id for m in humans)
    return eligible


class Leveling:
    def __init__(self, bot, store=None):
        self.bot = bot
        self.store = store or LevelStore()
        self.sessions = {}
        self.locks = {}
        self.connected = False
        self.reward_lock = asyncio.Lock()
        self.rewarded = set()

    def lock(self, guild_id):
        return self.locks.setdefault(guild_id, asyncio.Lock())

    async def reward_roles(self, guild, results, cfg, fallback=None):
        for result in results:
            if not result or result['after'] < REGULAR_LEVEL:
                continue
            key = (guild.id, result['user_id'])
            async with self.reward_lock:
                if key in self.rewarded:
                    continue
                role = guild.get_role(REGULAR_ROLE_ID)
                if role is None:
                    continue  # The reward only belongs to the guild containing this role.
                try:
                    # Fetch current roles to avoid duplicates from a stale member cache.
                    member = await guild.fetch_member(result['user_id'])
                    if any(item.id == REGULAR_ROLE_ID for item in member.roles):
                        self.rewarded.add(key)
                        continue
                    if not guild.me or not guild.me.guild_permissions.manage_roles or not role.is_assignable():
                        log.warning('Cannot grant regular role: check Manage Roles and role hierarchy (guild %s)', guild.id)
                        continue
                    await member.add_roles(role, reason='Leveling reward: reached level 30', atomic=True)
                except discord.HTTPException:
                    log.warning('Could not grant regular role in guild %s to user %s', guild.id, result['user_id'])
                    continue
                self.rewarded.add(key)
                channel = (guild.get_channel(cfg['notify_channel']) if cfg['notify_channel'] else None) or fallback
                if channel is None:
                    voice = getattr(member, 'voice', None)
                    channel = getattr(voice, 'channel', None) or guild.system_channel
                if channel is not None:
                    try:
                        await channel.send(f"<@{member.id}>\\n{REGULAR_MESSAGE}",
                                           allowed_mentions=discord.AllowedMentions.none())
                    except discord.HTTPException:
                        log.warning('Regular role granted but celebration could not be sent in guild %s', guild.id)

    async def notify(self, guild, results, cfg, fallback=None):
        from casino import CasinoStore, CasinoError
        rewards = {}
        for result in results:
            if result:
                try:
                    if not hasattr(self, 'jewel_store'):
                        self.jewel_store = CasinoStore()
                    rewards[result['user_id']] = await asyncio.to_thread(
                        self.jewel_store.reward_levels, guild.id, result['user_id'],
                        result['before'], result['after'])
                except (sqlite3.Error, OSError, CasinoError):
                    log.exception('Failed to grant level-up jewels in guild %s', guild.id)
        await self.reward_roles(guild, results, cfg, fallback)
        if not cfg['notifications']:
            return
        channel = guild.get_channel(cfg['notify_channel']) if cfg['notify_channel'] else fallback
        if channel is None:
            return
        lines = [f"🎉 <@{r['user_id']}> が **Lv.{r['after']}** にレベルアップしました！"
                 + (f" 💎 {rewards[r['user_id']]:,}ジュエル獲得！" if rewards.get(r['user_id']) else '')
                 for r in results if r and r['after'] > r['before']]
        try:
            for offset in range(0, len(lines), 10):
                await channel.send('\n'.join(lines[offset:offset + 10]), allowed_mentions=discord.AllowedMentions.none())
        except discord.HTTPException:
            log.warning('Cannot send level-up notification in guild %s', guild.id)

    async def on_message(self, message):
        if message.guild is None or message.author.bot or message.webhook_id:
            return
        if not message.content.strip() and not message.attachments:
            return
        try:
            result, cfg = await asyncio.to_thread(self.store.award_text, message.guild.id, message.author.id)
            if result:
                await self.notify(message.guild, [result], cfg, message.channel)
        except (sqlite3.Error, OSError):
            log.exception('Failed to save text XP in guild %s', message.guild.id)

    async def reconcile_voice(self, guild, now=None):
        async with self.lock(guild.id):
            if not self.connected or not self.bot.is_ready():
                return
            cfg = await asyncio.to_thread(self.store.config, guild.id)
            if not self.connected or not self.bot.is_ready():
                return
            now = time.monotonic() if now is None else now
            active = eligible_voice(guild) if cfg['enabled'] and cfg['voice_xp'] else set()
            awards = []
            for key, started in list(self.sessions.items()):
                if key[0] != guild.id:
                    continue
                elapsed = max(0, now - started)
                # A long scheduler/reconnect gap must never grant offline time.
                minutes = int(elapsed // 60) if elapsed <= 90 else 0
                if minutes and cfg['enabled']:
                    awards.append((key[1], minutes))
                if key[1] not in active:
                    del self.sessions[key]
                elif elapsed > 90:
                    self.sessions[key] = now
                else:
                    self.sessions[key] += minutes * 60
            for user_id in active:
                self.sessions.setdefault((guild.id, user_id), now)
            if awards:
                results, latest_cfg = await asyncio.to_thread(self.store.award_voice, guild.id, awards)
                await self.notify(guild, results, latest_cfg)

    async def on_voice_state_update(self, member, before, after):
        try:
            await self.reconcile_voice(member.guild)
        except (sqlite3.Error, OSError):
            log.exception('Failed to update voice XP in guild %s', member.guild.id)

    async def on_disconnect(self):
        self.connected = False
        self.sessions.clear()

    async def on_ready(self):
        self.connected = True
        self.sessions.clear()
        for guild in self.bot.guilds:
            try:
                await self.reconcile_voice(guild)
            except (sqlite3.Error, OSError):
                log.exception('Failed to initialize voice XP in guild %s', guild.id)
        if not self.voice_tick.is_running():
            self.voice_tick.start()

    async def on_resumed(self):
        await self.on_ready()

    @tasks.loop(seconds=15)
    async def voice_tick(self):
        if not self.connected or not self.bot.is_ready():
            return
        for guild in self.bot.guilds:
            try:
                await self.reconcile_voice(guild)
            except Exception:
                log.exception('Failed to save voice XP in guild %s', guild.id)

    async def rank(self, interaction, member=None):
        await interaction.response.defer(thinking=True)
        member = member or interaction.user
        user, cfg = await asyncio.to_thread(self.store.rank, interaction.guild_id, member.id)
        level = level_for(user['xp'], cfg['base_xp'], cfg['step_xp'])
        start = threshold(level, cfg['base_xp'], cfg['step_xp'])
        next_xp = threshold(level + 1, cfg['base_xp'], cfg['step_xp'])
        embed = discord.Embed(title=f'{member.display_name} のレベル', color=discord.Color.blue())
        embed.add_field(name='レベル', value=f'Lv.{level}')
        embed.add_field(name='順位', value=f"{user['rank']}位" if user['rank'] else '未参加')
        embed.add_field(name='累計XP', value=f"{user['xp']:,} XP")
        if level == MAX_LEVEL:
            embed.add_field(name='次のレベルまで', value=f'最大レベルに到達しました（MAX Lv.{MAX_LEVEL}）', inline=False)
        else:
            embed.add_field(name='次のレベルまで', value=f"{next_xp - user['xp']:,} XP（{user['xp'] - start:,} / {next_xp - start:,}）", inline=False)
        embed.add_field(name='獲得内訳', value=f"テキスト：{user['text_xp']:,} XP\nVC：{user['voice_xp']:,} XP", inline=False)
        if user['shop_xp']:
            embed.add_field(name='ショップで購入したXP', value=f"{user['shop_xp']:,} XP", inline=False)
        adjustment = user['xp'] - user['text_xp'] - user['voice_xp'] - user['shop_xp']
        if adjustment:
            embed.add_field(name='管理者によるXP調整', value=f'{adjustment:+,} XP', inline=False)
        await interaction.edit_original_response(embed=embed, allowed_mentions=discord.AllowedMentions.none())

    async def leaderboard(self, interaction, page):
        await interaction.response.defer(thinking=True)
        rows, cfg = await asyncio.to_thread(self.store.leaderboard, interaction.guild_id, page)
        lines = []
        for index, row in enumerate(rows, (page - 1) * 10 + 1):
            level = level_for(row['xp'], cfg['base_xp'], cfg['step_xp'])
            lines.append(f"**{index}位** <@{row['user_id']}> — Lv.{level} / {row['xp']:,} XP")
        embed = discord.Embed(title=f'レベルランキング — {page}ページ', description='\n'.join(lines) or 'まだデータがありません。', color=discord.Color.blue())
        await interaction.edit_original_response(embed=embed, allowed_mentions=discord.AllowedMentions.none())

    async def edit(self, interaction, unit, mode, amount, member=None):
        await interaction.response.defer(thinking=True, ephemeral=True)
        if interaction.guild is None or not interaction.user.guild_permissions.manage_guild:
            await interaction.edit_original_response(content='この操作には「サーバー管理」権限が必要です。')
            return
        member = member or interaction.user
        if member.bot:
            await interaction.edit_original_response(content='BotのXP・レベルは編集できません。')
            return
        try:
            async with self.lock(interaction.guild_id):
                result, cfg = await asyncio.to_thread(self.store.edit, interaction.guild_id, member.id, unit, mode, amount)
        except ValueError:
            await interaction.edit_original_response(content='編集値が無効です。数値は0以上、レベルの指定値は100以下にしてください。')
            return
        labels = {'add': '加算', 'subtract': '減算', 'set': '指定'}
        log.info('Leveling edit guild=%s actor=%s target=%s unit=%s mode=%s amount=%s xp=%s->%s',
                 interaction.guild_id, interaction.user.id, member.id, unit, mode, amount, result['old_xp'], result['xp'])
        await interaction.edit_original_response(
            content=(f"<@{member.id}> の{'XP' if unit == 'xp' else 'レベル'}を{labels[mode]}しました。\\n"
                     f"累計XP：{result['old_xp']:,} → {result['xp']:,} XP\\n"
                     f"レベル：Lv.{result['before']} → Lv.{result['after']}"),
            allowed_mentions=discord.AllowedMentions.none())
        await self.notify(interaction.guild, [result], cfg, interaction.channel)

    async def configure(self, interaction, changes=None):
        await interaction.response.defer(thinking=True, ephemeral=True)
        if not interaction.user.guild_permissions.manage_guild:
            await interaction.edit_original_response(content='この操作には「サーバー管理」権限が必要です。')
            return
        async with self.lock(interaction.guild_id):
            if changes:
                try:
                    cfg = await asyncio.to_thread(self.store.configure, interaction.guild_id, changes)
                except ValueError:
                    await interaction.edit_original_response(content='設定が無効です。テキストXPの最小値は最大値以下にしてください。')
                    return
                # Prevent partial VC minutes from crossing a configuration change.
                self.sessions = {k: v for k, v in self.sessions.items() if k[0] != interaction.guild_id}
            else:
                cfg = await asyncio.to_thread(self.store.config, interaction.guild_id)
        channel = f"<#{cfg['notify_channel']}>" if cfg['notify_channel'] else 'テキストは発言先、VCは通知なし'
        text = (f"レベリング：{'有効' if cfg['enabled'] else '無効'}\n"
                f"テキスト：{cfg['text_xp_min']}〜{cfg['text_xp_max']} XP（ランダム）/ {cfg['cooldown']}秒に1回\nVC：{cfg['voice_xp']} XP / 1分\n"
                f"最大レベル：{MAX_LEVEL}\n"
                f"次レベルの必要XP：{cfg['base_xp']} + 現在レベル × {cfg['step_xp']}\n"
                f"レベルアップ通知：{'有効' if cfg['notifications'] else '無効'}\n通知先：{channel}\n"
                '必要XPの設定を変えると、保存済みの累計XPからレベルを再計算します。')
        await interaction.edit_original_response(content=text, allowed_mentions=discord.AllowedMentions.none())


def register_leveling(bot):
    service = Leveling(bot)

    @bot.tree.command(name='rank', description='自分またはメンバーのレベル・XPを確認します')
    @app_commands.guild_only()
    @app_commands.describe(member='確認するメンバー（省略で自分）')
    async def rank(interaction: discord.Interaction, member: discord.Member | None = None):
        await service.rank(interaction, member)

    @bot.tree.command(name='leaderboard', description='サーバーのレベルランキングを表示します')
    @app_commands.guild_only()
    async def leaderboard(interaction: discord.Interaction, page: app_commands.Range[int, 1, 1000] = 1):
        await service.leaderboard(interaction, page)

    group = app_commands.Group(name='level', description='レベリングの管理者設定', guild_only=True,
                               default_permissions=discord.Permissions(manage_guild=True))

    @group.command(name='settings', description='レベリング設定を確認します')
    async def settings(interaction: discord.Interaction):
        await service.configure(interaction)

    @group.command(name='configure', description='獲得XP・必要XP・有効無効を変更します')
    @app_commands.describe(text_xp_min='1発言の最小XP（既定5）', text_xp_max='1発言の最大XP（既定15。両方0で無効）', voice_xp='VC1分のXP（0で無効）',
                           cooldown='テキストXPを付与する間隔（秒）', base_xp='最初のレベルアップに必要なXP',
                           step_xp='レベルごとに追加で必要なXP', enabled='レベリング全体の有効無効', notifications='レベルアップ通知')
    async def configure(interaction: discord.Interaction,
                        text_xp_min: app_commands.Range[int, 0, 10000] | None = None,
                        text_xp_max: app_commands.Range[int, 0, 10000] | None = None,
                        voice_xp: app_commands.Range[int, 0, 10000] | None = None,
                        cooldown: app_commands.Range[int, 5, 86400] | None = None,
                        base_xp: app_commands.Range[int, 1, 1000000] | None = None,
                        step_xp: app_commands.Range[int, 0, 1000000] | None = None,
                        enabled: bool | None = None, notifications: bool | None = None):
        changes = {k: int(v) for k, v in dict(text_xp_min=text_xp_min, text_xp_max=text_xp_max, voice_xp=voice_xp, cooldown=cooldown,
                   base_xp=base_xp, step_xp=step_xp, enabled=enabled, notifications=notifications).items() if v is not None}
        await service.configure(interaction, changes)

    @group.command(name='notify', description='レベルアップ通知先を指定します。省略で既定に戻します')
    async def notify(interaction: discord.Interaction, channel: discord.TextChannel | None = None):
        await service.configure(interaction, {'notify_channel': channel.id if channel else None})

    choices = [
        app_commands.Choice(name='加算', value='add'),
        app_commands.Choice(name='減算', value='subtract'),
        app_commands.Choice(name='指定', value='set'),
    ]

    @group.command(name='edit', description='メンバーのレベルを加算・減算・指定します')
    @app_commands.describe(mode='編集方法', amount='レベル数（0〜100）', member='対象メンバー（省略で自分）')
    @app_commands.choices(mode=choices)
    async def level_edit(interaction: discord.Interaction, mode: str,
                         amount: app_commands.Range[int, 0, 100], member: discord.Member | None = None):
        await service.edit(interaction, 'level', mode, amount, member)

    xp_group = app_commands.Group(name='xp', description='管理者用XP編集', guild_only=True,
                                   default_permissions=discord.Permissions(manage_guild=True))

    @xp_group.command(name='edit', description='メンバーのXPを加算・減算・指定します')
    @app_commands.describe(mode='編集方法', amount='XP数（0〜1000000000）', member='対象メンバー（省略で自分）')
    @app_commands.choices(mode=choices)
    async def xp_edit(interaction: discord.Interaction, mode: str,
                      amount: app_commands.Range[int, 0, 1000000000], member: discord.Member | None = None):
        await service.edit(interaction, 'xp', mode, amount, member)

    bot.tree.add_command(xp_group)
    bot.tree.add_command(group)
    for event in ('on_message', 'on_voice_state_update', 'on_ready', 'on_resumed', 'on_disconnect'):
        bot.add_listener(getattr(service, event), event)
    return service
