import asyncio
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from pathlib import Path
import sys
import sqlite3
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'Discord-GameBot'))
import discord
from discord.ext import commands
from leveling import LevelStore, Leveling, eligible_voice, level_for, threshold, register_leveling


def human(user_id, bot=False, deaf=False, muted=False):
    return SimpleNamespace(id=user_id, bot=bot, voice=SimpleNamespace(deaf=False, self_deaf=deaf,
                           mute=False, self_mute=muted, suppress=False))


class LevelStoreTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.store = LevelStore(Path(self.temp.name) / 'levels.sqlite3')
        self.random_patch = patch('leveling.random.randint', side_effect=lambda low, high: high)
        self.random_patch.start()
        self.addCleanup(self.random_patch.stop)

    def test_level_threshold_boundaries_and_large_values(self):
        for base, step in ((300, 50), (100, 0), (1, 1000000)):
            for level in (0, 1, 2, 10, 1000, 1000000):
                xp = threshold(level, base, step)
                self.assertEqual(level_for(xp, base, step), min(level, 100))
                if level:
                    self.assertEqual(level_for(xp - 1, base, step), min(level - 1, 100))
        self.assertEqual(threshold(1), 300)
        self.assertEqual(threshold(2), 650)

    def test_cooldown_persists_and_guilds_are_isolated(self):
        result, _ = self.store.award_text(1, 10, 100)
        self.assertEqual(result['xp'], 15)
        other = LevelStore(self.store.path)
        self.assertIsNone(other.award_text(1, 10, 159)[0])
        self.assertEqual(other.award_text(1, 10, 160)[0]['xp'], 30)
        self.assertEqual(other.award_text(2, 10, 100)[0]['xp'], 15)
        self.assertEqual(self.store.rank(1, 10)[0]['text_xp'], 30)

    def test_atomic_duplicate_messages_and_mixed_xp(self):
        with ThreadPoolExecutor(max_workers=6) as pool:
            results = list(pool.map(lambda _: self.store.award_text(1, 10, 100)[0], range(12)))
        self.assertEqual(sum(result is not None for result in results), 1)
        self.store.award_voice(1, [(10, 3)])
        row, _ = self.store.rank(1, 10)
        self.assertEqual((row['xp'], row['text_xp'], row['voice_xp']), (45, 15, 30))

    def test_disabled_rates_settings_and_multi_level_award(self):
        self.store.configure(1, {'text_xp_min': 1000, 'text_xp_max': 1000})
        result, _ = self.store.award_text(1, 10, 100)
        self.assertEqual((result['before'], result['after']), (0, 2))
        self.store.configure(1, {'enabled': 0})
        self.assertIsNone(self.store.award_text(1, 10, 200)[0])
        self.assertEqual(self.store.award_voice(1, [(10, 10)])[0], [])
        self.store.configure(1, {'enabled': 1, 'text_xp_min': 0, 'text_xp_max': 0, 'voice_xp': 0})
        self.assertIsNone(self.store.award_text(1, 10, 300)[0])
        for changes in ({'unknown': 1}, {'cooldown': 0}, {'base_xp': 0}, {'step_xp': -1}):
            with self.assertRaises(ValueError):
                self.store.configure(1, changes)

    def test_leaderboard_rank_pagination_and_reopen(self):
        self.store.award_voice(1, [(user_id, 1) for user_id in range(1, 13)])
        self.store.award_voice(2, [(99, 99)])
        rows, _ = self.store.leaderboard(1, 2)
        self.assertEqual([row['user_id'] for row in rows], [11, 12])
        self.assertEqual(self.store.rank(1, 11)[0]['rank'], 11)
        self.assertIsNone(self.store.rank(1, 100)[0]['rank'])
        self.assertEqual(LevelStore(self.store.path).rank(1, 12)[0]['xp'], 10)

    def test_random_text_range_includes_both_endpoints(self):
        self.assertEqual((self.store.config(1)['text_xp_min'], self.store.config(1)['text_xp_max']), (5, 15))
        with patch('leveling.random.randint', side_effect=[5, 15]) as random_xp:
            self.assertEqual(self.store.award_text(1, 10, 100)[0]['xp'], 5)
            self.assertEqual(self.store.award_text(1, 10, 160)[0]['xp'], 20)
            self.assertEqual(random_xp.call_args_list[0].args, (5, 15))
        with self.assertRaises(ValueError):
            self.store.configure(1, {'text_xp_min': 16})
        self.assertEqual(self.store.config(1)['text_xp_min'], 5)

    def test_upgrade_old_database_preserves_xp_and_disabled_text(self):
        old_path = Path(self.temp.name) / 'old.sqlite3'
        with closing(sqlite3.connect(old_path)) as db, db:
            db.executescript('''
                CREATE TABLE level_config (
                    guild_id INTEGER PRIMARY KEY, enabled INTEGER NOT NULL DEFAULT 1,
                    text_xp INTEGER NOT NULL DEFAULT 15, cooldown INTEGER NOT NULL DEFAULT 60,
                    voice_xp INTEGER NOT NULL DEFAULT 10, base_xp INTEGER NOT NULL DEFAULT 300,
                    step_xp INTEGER NOT NULL DEFAULT 50, notifications INTEGER NOT NULL DEFAULT 1,
                    notify_channel INTEGER);
                INSERT INTO level_config(guild_id,text_xp) VALUES(1,15),(2,0);
                CREATE TABLE level_users (
                    guild_id INTEGER NOT NULL,user_id INTEGER NOT NULL,xp INTEGER NOT NULL DEFAULT 0,
                    text_xp INTEGER NOT NULL DEFAULT 0,voice_xp INTEGER NOT NULL DEFAULT 0,
                    last_text REAL,PRIMARY KEY(guild_id,user_id));
                INSERT INTO level_users(guild_id,user_id,xp,text_xp) VALUES(1,10,250,250);
            ''')
        upgraded = LevelStore(old_path)
        self.assertEqual(upgraded.rank(1, 10)[0]['xp'], 250)
        self.assertEqual(upgraded.config(1)['text_xp_min'], 5)
        self.assertEqual(upgraded.config(2)['text_xp_max'], 0)
        upgraded.configure(2, {'text_xp_min': 5, 'text_xp_max': 15})
        reopened = LevelStore(old_path)
        self.assertEqual(reopened.config(2)['text_xp_max'], 15)
        self.assertEqual(reopened.rank(1, 10)[0]['xp'], 250)

    def test_level_100_caps_both_sources_but_retains_total_xp(self):
        self.store.award_voice(1, [(10, 30000)])
        result, _ = self.store.award_text(1, 10, 100)
        self.assertEqual((result['before'], result['after']), (100, 100))
        self.assertEqual(self.store.rank(1, 10)[0]['xp'], 300015)
        self.assertEqual(level_for(10**30), 100)
        self.assertEqual(level_for(10**30, step=0), 100)


class VoiceTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.store = LevelStore(Path(self.temp.name) / 'levels.sqlite3')
        self.random_patch = patch('leveling.random.randint', side_effect=lambda low, high: high)
        self.random_patch.start()
        self.addCleanup(self.random_patch.stop)
        self.channel = SimpleNamespace(members=[human(10), human(20, muted=True)])
        self.guild = SimpleNamespace(id=1, voice_channels=[self.channel], afk_channel=None, get_channel=lambda _: None)
        self.bot = SimpleNamespace(is_ready=lambda: True, guilds=[self.guild])
        self.service = Leveling(self.bot, self.store)
        self.service.connected = True
        async def inline(function, *args, **kwargs):
            return function(*args, **kwargs)
        self.thread_patch = patch('leveling.asyncio.to_thread', side_effect=inline)
        self.thread_patch.start()
        self.addCleanup(self.thread_patch.stop)

    async def test_voice_full_minutes_and_departure(self):
        await self.service.reconcile_voice(self.guild, 0)
        await self.service.reconcile_voice(self.guild, 59)
        self.assertEqual(self.store.rank(1, 10)[0]['xp'], 0)
        await self.service.reconcile_voice(self.guild, 60)
        await self.service.reconcile_voice(self.guild, 60)
        self.assertEqual(self.store.rank(1, 10)[0]['xp'], 10)
        self.assertEqual(self.store.rank(1, 20)[0]['xp'], 10)
        self.channel.members.pop()
        await self.service.reconcile_voice(self.guild, 90)
        self.assertFalse(self.service.sessions)
        await self.service.reconcile_voice(self.guild, 1000)
        self.assertEqual(self.store.rank(1, 10)[0]['xp'], 10)

    async def test_no_offline_or_long_gap_awards(self):
        await self.service.reconcile_voice(self.guild, 0)
        await self.service.reconcile_voice(self.guild, 1000)
        self.assertEqual(self.store.rank(1, 10)[0]['xp'], 0)
        await self.service.on_disconnect()
        self.assertFalse(self.service.sessions)
        await self.service.reconcile_voice(self.guild, 2000)
        self.assertFalse(self.service.sessions)
        self.service.connected = True
        await self.service.reconcile_voice(self.guild, 3000)
        await self.service.reconcile_voice(self.guild, 3060)
        self.assertEqual(self.store.rank(1, 10)[0]['xp'], 10)

    async def test_text_filters_and_non_ping_level_notice(self):
        channel = SimpleNamespace(send=AsyncMock())
        guild = SimpleNamespace(id=1, get_channel=lambda _: channel)
        msg = SimpleNamespace(guild=guild, author=human(10), webhook_id=None,
                              content='hello', attachments=[], channel=channel)
        self.store.configure(1, {'text_xp_min': 300, 'text_xp_max': 300})
        await self.service.on_message(msg)
        self.assertEqual(self.store.rank(1, 10)[0]['xp'], 300)
        channel.send.assert_awaited_once()
        self.assertFalse(channel.send.call_args.kwargs['allowed_mentions'].everyone)
        self.assertFalse(channel.send.call_args.kwargs['allowed_mentions'].users)
        msg.author = human(30, bot=True)
        await self.service.on_message(msg)
        self.assertEqual(self.store.rank(1, 30)[0]['xp'], 0)
        msg.author = human(40)
        msg.content = '  '
        await self.service.on_message(msg)
        self.assertEqual(self.store.rank(1, 40)[0]['xp'], 0)

    async def test_gateway_resume_restarts_measurement_without_catchup(self):
        await self.service.reconcile_voice(self.guild, 0)
        await self.service.on_disconnect()
        with patch.object(self.service.voice_tick, 'is_running', return_value=True), patch('leveling.time.monotonic', return_value=500):
            await self.service.on_resumed()
        self.assertTrue(self.service.connected)
        self.assertEqual(self.service.sessions[(1, 10)], 500)
        self.assertEqual(self.store.rank(1, 10)[0]['xp'], 0)
        await self.service.reconcile_voice(self.guild, 560)
        self.assertEqual(self.store.rank(1, 10)[0]['xp'], 10)

    async def test_configuration_permissions_and_registration(self):
        interaction = SimpleNamespace(guild_id=1, user=SimpleNamespace(guild_permissions=SimpleNamespace(manage_guild=False)),
            response=SimpleNamespace(defer=AsyncMock()), edit_original_response=AsyncMock())
        await self.service.configure(interaction, {'text_xp_min': 999, 'text_xp_max': 999})
        self.assertEqual(self.store.config(1)['text_xp_max'], 15)
        bot = commands.Bot(command_prefix='!', intents=discord.Intents.default())
        with patch('leveling.LevelStore', return_value=self.store):
            register_leveling(bot)
        self.assertEqual({c.name for c in bot.tree.get_commands()}, {'rank', 'leaderboard', 'level'})
        self.assertTrue(bot.tree.get_command('level').default_permissions.manage_guild)
        self.assertTrue(bot.intents.voice_states)
        self.assertIn('on_voice_state_update', bot.extra_events)

    def test_voice_eligibility(self):
        self.assertEqual(eligible_voice(self.guild), {10, 20})
        self.channel.members = [human(10), human(20, deaf=True), human(30, bot=True)]
        self.assertEqual(eligible_voice(self.guild), set())
        self.channel.members = [human(10), human(20)]
        self.guild.afk_channel = self.channel
        self.assertEqual(eligible_voice(self.guild), set())

    async def test_max_level_rank_has_no_next_level_or_negative_xp(self):
        self.store.award_voice(1, [(10, 30000)])
        interaction = SimpleNamespace(guild_id=1, user=SimpleNamespace(id=10, display_name='Player'),
            response=SimpleNamespace(defer=AsyncMock()), edit_original_response=AsyncMock())
        await self.service.rank(interaction)
        embed = interaction.edit_original_response.call_args.kwargs['embed']
        next_field = next(field for field in embed.fields if field.name == '次のレベルまで')
        self.assertIn('MAX Lv.100', next_field.value)
        self.assertNotIn('-', next_field.value)
        self.assertEqual(next(field for field in embed.fields if field.name == 'レベル').value, 'Lv.100')


if __name__ == '__main__':
    unittest.main()
