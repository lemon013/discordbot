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


if __name__ == '__main__':
    unittest.main()
