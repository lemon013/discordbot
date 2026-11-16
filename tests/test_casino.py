from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from itertools import product
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'Discord-GameBot'))
import discord
from discord.ext import commands
from casino import Casino, CasinoStore, CasinoError, draw, register_casino


class CasinoStoreTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / 'casino.sqlite3'
        self.store = CasinoStore(self.path)

    def test_initial_wallet_is_once_persistent_and_guild_local(self):
        self.assertEqual(self.store.balance(1, 10), 1000)
        self.store.edit(1, 10, 'edit', 'subtract', 900, 99)
        self.assertEqual(CasinoStore(self.path).balance(1, 10), 100)
        self.assertEqual(self.store.balance(2, 10), 1000)

    def test_jst_daily_boundary_duplicate_and_replay(self):
        now = datetime(2026, 11, 16, 14, 59, 59, tzinfo=timezone.utc).timestamp()
        self.assertEqual(self.store.daily(1, 10, 'daily1', now)['balance'], 1500)
        self.assertEqual(self.store.daily(1, 10, 'daily1', now)['balance'], 1500)
        with self.assertRaises(CasinoError):
            self.store.daily(1, 10, 'daily2', now)
        self.assertEqual(self.store.daily(1, 10, 'daily3', now + 1)['balance'], 2000)
        self.assertEqual(len(self.store.history(1, 10)), 2)

    def test_all_slot_outcomes_and_payouts(self):
        total = 0
        for reels in product(range(6), repeat=3):
            with patch('casino.secrets.randbelow', side_effect=reels):
                result = draw('slot', 10)
                total += result['payout']
                self.assertGreaterEqual(result['payout'], 0)
        self.assertEqual(total, 1980)
        with patch('casino.secrets.randbelow', return_value=0):
            self.assertEqual(draw('slot', 10)['payout'], 500)
        with patch('casino.secrets.randbelow', side_effect=[0, 0, 1]):
            self.assertEqual(draw('slot', 10)['payout'], 10)

    def test_coinflip_dice_wins_losses_and_rounding(self):
        with patch('casino.secrets.randbelow', return_value=0):
            self.assertEqual(draw('coinflip', 11, 0)['payout'], 20)
            self.assertEqual(draw('coinflip', 11, 1)['payout'], 0)
            self.assertEqual(draw('dice', 11, 1)['payout'], 60)
            self.assertEqual(draw('dice', 11, 6)['payout'], 0)

    def test_game_atomic_charge_payout_cooldown_and_replay(self):
        with patch('casino.secrets.randbelow', return_value=0) as rng:
            result = self.store.play(1, 10, 'first', 'coinflip', 100, 0, now=100)
            self.assertEqual((result['payout'], result['balance']), (190, 1090))
            self.assertEqual(self.store.play(1, 10, 'first', 'coinflip', 100, 0, now=100), result)
            self.assertEqual(rng.call_count, 1)
            with self.assertRaises(CasinoError):
                self.store.play(1, 10, 'second', 'coinflip', 100, 0, now=102)
            self.assertEqual(self.store.balance(1, 10), 1090)
            self.assertEqual(self.store.play(1, 10, 'third', 'coinflip', 100, 1, now=103)['balance'], 990)

    def test_insufficient_funds_and_invalid_inputs_do_not_draw(self):
        with patch('casino.secrets.randbelow') as rng:
            for wager in (-1, 0, 9, 10001, 2000):
                with self.assertRaises(CasinoError):
                    self.store.play(1, 10, str(wager), 'slot', wager, now=100)
            for game, choice in [('invalid', None), ('coinflip', 3), ('dice', 0), ('dice', 7)]:
                with self.assertRaises(CasinoError):
                    self.store.play(1, 10, game + str(choice), game, 10, choice, now=100)
            rng.assert_not_called()
        self.assertEqual(self.store.balance(1, 10), 1000)
        self.assertEqual(self.store.history(1, 10), [])

    def test_concurrent_same_game_and_daily_only_pay_once(self):
        with ThreadPoolExecutor(max_workers=6) as pool, patch('casino.secrets.randbelow', return_value=0) as rng:
            results = list(pool.map(lambda _: self.store.play(1, 10, 'same', 'coinflip', 100, 0, now=100), range(12)))
            self.assertTrue(all(result['balance'] == 1090 for result in results))
            self.assertEqual(rng.call_count, 1)
            list(pool.map(lambda _: self.store.daily(1, 10, 'same-daily', now=100), range(12)))
        self.assertEqual(self.store.balance(1, 10), 1590)
        self.assertEqual(len(self.store.history(1, 10)), 2)

    def test_concurrent_distinct_bets_cannot_double_spend(self):
        self.store.edit(1, 10, 'seed', 'set', 100, 99)
        def bet(index):
            try:
                return self.store.play(1, 10, str(index), 'coinflip', 100, 1, now=100)
            except CasinoError:
                return None
        with ThreadPoolExecutor(max_workers=6) as pool, patch('casino.secrets.randbelow', return_value=0):
            results = list(pool.map(bet, range(12)))
        self.assertEqual(sum(result is not None for result in results), 1)
        self.assertEqual(self.store.balance(1, 10), 0)

    def test_edit_modes_replay_audit_and_ranking(self):
        self.assertEqual(self.store.edit(1, 10, 'a', 'add', 500, 99)['balance'], 1500)
        self.assertEqual(self.store.edit(1, 10, 'a', 'add', 500, 99)['balance'], 1500)
        self.assertEqual(self.store.edit(1, 10, 'b', 'subtract', 2000, 99)['balance'], 0)
        self.assertEqual(self.store.edit(1, 10, 'c', 'set', 5000, 99)['actor_id'], 99)
        self.store.balance(1, 20)
        self.store.edit(2, 30, 'other', 'set', 999999, 99)
        self.assertEqual([r['user_id'] for r in self.store.leaderboard(1)], [10, 20])
        with self.assertRaises(CasinoError):
            self.store.edit(2, 10, 'a', 'add', 500, 99)

    def test_database_defaults_to_level_data_directory(self):
        with patch.dict('os.environ', {'LEVEL_DB_PATH': str(Path(self.temp.name) / 'persistent' / 'levels.sqlite3'), 'CASINO_DB_PATH': ''}):
            store = CasinoStore()
            self.assertEqual(store.path, Path(self.temp.name) / 'persistent' / 'casino.sqlite3')


class CasinoCommandTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.store = CasinoStore(Path(self.temp.name) / 'casino.sqlite3')
        self.service = Casino(None, self.store)
        async def inline(function, *args, **kwargs):
            return function(*args, **kwargs)
        thread_patch = patch('casino.asyncio.to_thread', side_effect=inline)
        thread_patch.start()
        self.addCleanup(thread_patch.stop)

    def interaction(self, admin=False):
        return SimpleNamespace(id=100, guild_id=1, guild=SimpleNamespace(id=1),
            user=SimpleNamespace(id=10, bot=False, guild_permissions=SimpleNamespace(manage_guild=admin)),
            response=SimpleNamespace(defer=AsyncMock()), edit_original_response=AsyncMock())

    async def test_early_acknowledgement_and_public_game_result(self):
        interaction = self.interaction()
        def rng(*args):
            interaction.response.defer.assert_awaited_once_with(thinking=True, ephemeral=False)
            return 0
        with patch('casino.secrets.randbelow', side_effect=rng):
            await self.service.execute(interaction, 'coinflip', wager=100, choice=0)
        embed = interaction.edit_original_response.call_args.kwargs['embed']
        self.assertEqual(next(f.value for f in embed.fields if f.name == '残高'), '💎 1,090 ジュエル')

    async def test_admin_edit_authorization_and_target(self):
        interaction = self.interaction()
        await self.service.execute(interaction, 'edit', mode='set', amount=9000)
        self.assertEqual(self.store.history(1, 10), [])
        interaction.user.guild_permissions.manage_guild = True
        await self.service.execute(interaction, 'edit', mode='set', amount=9000, member=SimpleNamespace(id=20, bot=False))
        self.assertEqual(self.store.balance(1, 20), 9000)
        self.assertEqual(self.store.balance(1, 10), 1000)
        self.assertFalse(interaction.edit_original_response.call_args.kwargs['allowed_mentions'].users)

    async def test_registration_and_six_sided_choice_validation(self):
        bot = commands.Bot(command_prefix='!', intents=discord.Intents.default())
        with patch('casino.CasinoStore', return_value=self.store):
            service = register_casino(bot)
        self.assertEqual({c.name for c in bot.tree.get_command('casino').commands}, {'slot', 'coinflip', 'dice', 'help'})
        self.assertEqual({c.name for c in bot.tree.get_command('jewel').commands}, {'balance', 'daily', 'history', 'leaderboard', 'edit'})
        service.execute = AsyncMock()
        await bot.tree.get_command('casino').get_command('dice').callback('interaction', 100, 6)
        service.execute.assert_awaited_once_with('interaction', 'dice', wager=100, choice=6)

    async def test_reject_dm_and_bot_target(self):
        interaction = self.interaction()
        interaction.guild = None
        await self.service.execute(interaction, 'daily')
        self.assertEqual(self.store.history(1, 10), [])
        interaction.guild = SimpleNamespace(id=1)
        await self.service.execute(interaction, 'balance', member=SimpleNamespace(id=20, bot=True))
        self.assertEqual(self.store.leaderboard(1), [])


if __name__ == '__main__':
    unittest.main()
