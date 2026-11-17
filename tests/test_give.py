from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'Discord-GameBot'))
import discord
from discord.ext import commands
from casino import CasinoStore, CasinoError, MAX_BALANCE, register_casino
from give import GiveStore, GiveService, GiveView, OWNER_ID, duration_seconds, register_give


class GiveTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.casino = CasinoStore(Path(self.temp.name) / 'casino.sqlite3')
        self.store = GiveStore(self.casino)
        self.store.create('board', 1, 2, 200, '受け取る', 1000)
        self.store.attach('board', 3)

    def test_once_per_board_and_user_across_restart(self):
        self.assertEqual(self.store.claim('board', 1, 3, 10, now=900)['balance'], 200)
        restarted = GiveStore(CasinoStore(self.casino.path))
        with self.assertRaises(CasinoError):
            restarted.claim('board', 1, 3, 10, now=901)
        self.assertEqual(restarted.claim('board', 1, 3, 11, now=901)['balance'], 200)
        self.store.create('other', 1, 2, 300, '受け取る', 1000)
        self.store.attach('other', 4)
        self.assertEqual(self.store.claim('other', 1, 4, 10, now=902)['balance'], 500)

    def test_concurrent_claims_pay_once(self):
        def claim(_):
            try:
                return self.store.claim('board', 1, 3, 10, now=900)
            except CasinoError:
                return None
        with ThreadPoolExecutor(max_workers=6) as pool:
            results = list(pool.map(claim, range(12)))
        self.assertEqual(sum(r is not None for r in results), 1)
        self.assertEqual(self.casino.balance(1, 10), 200)
        self.assertEqual(len(self.casino.history(1, 10)), 1)

    def test_expired_missing_or_wrong_board_cannot_pay(self):
        for args in [('board', 1, 3, 10, 1000), ('board', 2, 3, 10, 900),
                     ('board', 1, 4, 10, 900), ('missing', 1, 3, 10, 900)]:
            with self.assertRaises(CasinoError):
                self.store.claim(*args)
        self.store.disable('board')
        with self.assertRaises(CasinoError):
            self.store.claim('board', 1, 3, 10, now=900)
        self.assertEqual(self.casino.balance(1, 10), 0)

    def test_balance_limit_failure_rolls_back_claim(self):
        self.casino.balance(1, 10)
        with closing(self.casino.connect()) as db, db:
            db.execute('UPDATE casino_wallets SET balance=?', (MAX_BALANCE,))
        with self.assertRaises(CasinoError):
            self.store.claim('board', 1, 3, 10, now=900)
        self.casino.edit(1, 10, 'reset', 'set', 0, OWNER_ID)
        self.assertEqual(self.store.claim('board', 1, 3, 10, now=900)['balance'], 200)

    def test_duration_units_and_invalid_values(self):
        for value, expected in [('60', 60), ('30分', 1800), ('24時間', 86400),
                                ('7日', 604800), ('30m', 1800), ('1h', 3600)]:
            self.assertEqual(duration_seconds(value), expected)
        for value in ['0', '-1', '367日', 'forever', '1.5h']:
            with self.assertRaises(ValueError):
                duration_seconds(value)

    def test_concurrent_different_users_respect_capacity(self):
        self.store.create('limited', 1, 2, 200, 'Claim', 1000, max_claims=3)
        self.store.attach('limited', 4)
        def claim(user_id):
            try:
                return self.store.claim('limited', 1, 4, user_id, now=900)
            except CasinoError:
                return None
        with ThreadPoolExecutor(max_workers=6) as pool:
            results = list(pool.map(claim, range(10, 30)))
        self.assertEqual(sum(r is not None for r in results), 3)
        self.assertEqual(sum(self.casino.balance(1, user) for user in range(10, 30)), 600)
        restarted = GiveStore(CasinoStore(self.casino.path))
        with self.assertRaises(CasinoError):
            restarted.claim('limited', 1, 4, 99, now=901)
        row = next(r for r in restarted.pending() if r['board_id'] == 'limited')
        self.assertEqual((row['max_claims'], row['claimed_count']), (3, 3))

    def test_failed_or_duplicate_claim_does_not_consume_capacity(self):
        self.store.create('limited', 1, 2, 200, 'Claim', 1000, max_claims=2)
        self.store.attach('limited', 4)
        self.casino.balance(1, 10)
        with closing(self.casino.connect()) as db, db:
            db.execute('UPDATE casino_wallets SET balance=? WHERE user_id=10', (MAX_BALANCE,))
        with self.assertRaises(CasinoError):
            self.store.claim('limited', 1, 4, 10, now=900)
        self.store.claim('limited', 1, 4, 11, now=900)
        with self.assertRaises(CasinoError):
            self.store.claim('limited', 1, 4, 11, now=900)
        self.store.claim('limited', 1, 4, 12, now=900)
        with self.assertRaises(CasinoError):
            self.store.claim('limited', 1, 4, 13, now=900)

    def test_optional_capacity_and_old_schema_migration(self):
        for value in [0, -1, True, 1.5, 10**9 + 1]:
            with self.assertRaises(ValueError):
                self.store.create('invalid', 1, 2, 200, 'Claim', 1000, value)
        with closing(self.casino.connect()) as db, db:
            db.execute('ALTER TABLE jewel_give_boards DROP COLUMN max_claims')
            db.execute('ALTER TABLE jewel_give_boards DROP COLUMN claimed_count')
        migrated = GiveStore(self.casino)
        self.assertIsNone(migrated.pending()[0]['max_claims'])
        for user in range(10, 30):
            migrated.claim('board', 1, 3, user, now=900)


class GiveUITests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.bot = commands.Bot(command_prefix='!', intents=discord.Intents.none())
        self.addAsyncCleanup(self.bot.close)
        jewel = discord.app_commands.Group(name='jewel', description='Jewels', guild_only=True)
        self.service = register_give(self.bot, CasinoStore(Path(self.temp.name) / 'casino.sqlite3'), jewel)
        self.bot.tree.add_command(jewel)

    def interaction(self, user_id):
        return SimpleNamespace(id=123, user=SimpleNamespace(id=user_id), guild_id=1, channel_id=2,
                               response=SimpleNamespace(send_message=AsyncMock(), defer=AsyncMock()),
                               followup=SimpleNamespace(send=AsyncMock()), channel=SimpleNamespace(send=AsyncMock()))

    async def test_only_specific_owner_can_create(self):
        interaction = self.interaction(1)
        await self.service.create(interaction, 'test', 'body', 'claim', 200, '青', '1h')
        interaction.response.send_message.assert_awaited_once()
        interaction.channel.send.assert_not_awaited()

    async def test_owner_publication_and_newlines(self):
        interaction = self.interaction(OWNER_ID)
        message = SimpleNamespace(id=3, jump_url='https://discord.com/channels/1/2/3', edit=AsyncMock())
        interaction.channel.send.return_value = message
        self.assertIsNone(self.bot.tree.get_command('give'))
        command = self.bot.tree.get_command('jewel').get_command('give')
        self.assertEqual(command.qualified_name, 'jewel give')
        await command.callback(interaction, 'Title', 'one\\ntwo', 'Claim', 200, '青', '1h')
        kwargs = interaction.channel.send.call_args.kwargs
        self.assertEqual(kwargs['embed'].description, 'one\ntwo')
        self.assertEqual(kwargs['embed'].color.value, 0x3498DB)
        self.assertTrue(kwargs['view'].children[0].disabled)
        self.assertFalse(message.edit.call_args.kwargs['view'].children[0].disabled)
        self.assertEqual(self.service.store.pending()[0]['message_id'], 3)

    async def test_restart_registers_persistent_buttons_and_expires(self):
        self.service.store.create('old', 1, 2, 200, 'Claim', 1)
        self.service.store.attach('old', 3)
        edit = AsyncMock()
        self.bot.get_channel = lambda _: SimpleNamespace(get_partial_message=lambda _: SimpleNamespace(edit=edit))
        await self.service.on_ready()
        self.service.expire_tick.cancel()
        self.assertEqual(len(self.bot.persistent_views), 1)
        await self.service.expire_tick()
        self.assertTrue(edit.call_args.kwargs['view'].children[0].disabled)
        self.assertEqual(self.service.store.pending(), [])

    async def test_capacity_option_and_full_board_disabled(self):
        interaction = self.interaction(OWNER_ID)
        message = SimpleNamespace(id=3, jump_url='https://discord.com/channels/1/2/3', edit=AsyncMock())
        interaction.channel.send.return_value = message
        command = self.bot.tree.get_command('jewel').get_command('give')
        parameter = next(p for p in command.parameters if p.name == 'max_claims')
        self.assertFalse(parameter.required)
        await command.callback(interaction, 'Title', 'Body', 'Claim', 200, '青', '1h', 1)
        embed = interaction.channel.send.call_args.kwargs['embed']
        self.assertIn('先着1人', [field.value for field in embed.fields])
        self.service.store.claim('123', 1, 3, 10)
        edit = AsyncMock()
        self.bot.get_channel = lambda _: SimpleNamespace(get_partial_message=lambda _: SimpleNamespace(edit=edit))
        await self.service.expire_tick()
        self.assertTrue(edit.call_args.kwargs['view'].children[0].disabled)
        self.assertEqual(self.service.store.pending(), [])


if __name__ == '__main__':
    unittest.main()
