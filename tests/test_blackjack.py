from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'Discord-GameBot'))
import discord
from discord.ext import commands
from casino import CasinoStore, CasinoError, MAX_BALANCE
from blackjack import BlackjackStore, BlackjackService, BlackjackView, blackjack_embed, hand_value, register_blackjack


def deck_for(*draws):
    assert len(draws) == len(set(draws))
    return [card for card in range(52) if card not in draws] + list(reversed(draws))


class BlackjackStoreTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.casino = CasinoStore(Path(self.temp.name) / 'casino.sqlite3')
        self.store = BlackjackStore(self.casino)
        self.casino.edit(1, 10, 'seed', 'set', 1000, 99)

    def begin(self, *draws, wager=100, now=100):
        with patch('blackjack.new_deck', return_value=deck_for(*draws)):
            return self.store.start(1, 10, 'round', wager, now)

    def test_aces_and_face_cards(self):
        self.assertEqual(hand_value([0, 13, 9]), 12)
        self.assertEqual(hand_value([0, 5]), 17)
        self.assertEqual(hand_value([10, 11]), 20)
        self.assertEqual(hand_value([0, 13, 26, 39]), 14)

    def test_natural_payout_and_replay(self):
        state = self.begin(0, 8, 12, 6)
        self.assertEqual((state['status'], state['outcome'], state['payout'], state['balance']), ('done', 'blackjack', 250, 1150))
        again = self.store.start(1, 10, 'round', 100, 200)
        self.assertEqual(again['balance'], 1150)
        self.assertEqual(len(self.casino.history(1, 10)), 3)

    def test_natural_rounding_and_both_natural_push(self):
        state = self.begin(0, 8, 12, 6, wager=11)
        self.assertEqual(state['payout'], 27)
        self.casino.edit(1, 20, 'seed20', 'set', 1000, 99)
        with patch('blackjack.new_deck', return_value=deck_for(0, 13, 12, 25)):
            pushed = self.store.start(1, 20, 'both', 100, 200)
        self.assertEqual((pushed['outcome'], pushed['payout'], pushed['balance']), ('push', 100, 1000))

    def test_dealer_natural_and_player_bust(self):
        state = self.begin(9, 0, 6, 12)
        self.assertEqual((state['outcome'], state['balance']), ('dealer_blackjack', 900))
        with patch('blackjack.new_deck', return_value=deck_for(9, 8, 6, 7, 5)):
            state = self.store.start(1, 10, 'second', 100, 104)
        state = self.store.act(1, 10, 'second', 'hit', 'hit', 0, 105)
        self.assertEqual((state['outcome'], state['payout'], state['balance']), ('bust', 0, 800))

    def test_hit_21_is_regular_win_not_natural(self):
        self.begin(0, 8, 5, 7, 3)
        state = self.store.act(1, 10, 'round', 'hit', 'hit', 0, 101)
        self.assertEqual((state['outcome'], state['payout']), ('win', 200))

    def test_dealer_stands_on_soft_17_and_busts_when_below_17(self):
        self.begin(9, 0, 10, 5)
        state = self.store.act(1, 10, 'round', 'stand', 'stand', 0, 101)
        self.assertEqual((state['outcome'], len(state['dealer'])), ('win', 2))
        with patch('blackjack.new_deck', return_value=deck_for(9, 8, 6, 5, 12)):
            self.store.start(1, 10, 'second', 100, 104)
        state = self.store.act(1, 10, 'second', 'stand2', 'stand', 0, 105)
        self.assertEqual((state['outcome'], hand_value(state['dealer'])), ('win', 25))

    def test_push_and_normal_loss(self):
        self.begin(9, 8, 6, 7)
        state = self.store.act(1, 10, 'round', 'stand', 'stand', 0, 101)
        self.assertEqual((state['outcome'], state['balance']), ('push', 1000))
        with patch('blackjack.new_deck', return_value=deck_for(9, 8, 6, 19, 4)):
            self.store.start(1, 10, 'second', 100, 104)
        state = self.store.act(1, 10, 'second', 'stand2', 'stand', 0, 105)
        self.assertEqual((state['outcome'], state['balance']), ('lose', 900))

    def test_resume_reopen_and_no_second_bet(self):
        self.begin(9, 8, 6, 7)
        reopened = BlackjackStore(CasinoStore(self.casino.path))
        state = reopened.start(1, 10, 'new-command', 10000, 101)
        self.assertEqual((state['round_id'], state['wager'], state['balance']), ('round', 100, 900))
        state = reopened.start(1, 10, 'resume', None, 101)
        self.assertEqual(state['round_id'], 'round')

    def test_action_replay_stale_buttons_and_wrong_user(self):
        self.begin(1, 8, 2, 7, 3, 4)
        state = self.store.act(1, 10, 'round', 'hit', 'hit', 0, 101)
        self.assertEqual(len(state['player']), 3)
        same = self.store.act(1, 10, 'round', 'hit', 'hit', 0, 102)
        self.assertEqual(same['player'], state['player'])
        with self.assertRaises(CasinoError):
            self.store.act(1, 10, 'round', 'new-hit', 'hit', 0, 102)
        for guild_id, user_id in [(1, 20), (2, 10)]:
            with self.assertRaises(CasinoError):
                self.store.act(guild_id, user_id, 'round', 'intruder', 'stand', 1, 102)

    def test_timeout_auto_stand_and_late_action_do_not_repay(self):
        self.begin(9, 8, 6, 7)
        self.assertEqual(self.store.expire(219), [])
        states = self.store.expire(220)
        self.assertEqual((states[0]['outcome'], states[0]['balance']), ('push', 1000))
        self.assertEqual(self.store.expire(221), [])
        result = self.store.act(1, 10, 'round', 'late', 'hit', 0, 221)
        self.assertEqual(result['status'], 'done')
        self.assertEqual(self.casino.balance(1, 10), 1000)

    def test_concurrent_starts_and_settlements_are_once(self):
        with ThreadPoolExecutor(max_workers=6) as pool, patch('blackjack.new_deck', side_effect=lambda: deck_for(9, 8, 6, 7)):
            states = list(pool.map(lambda i: self.store.start(1, 10, str(i), 100, 100), range(12)))
        self.assertEqual(len({state['round_id'] for state in states}), 1)
        round_id = states[0]['round_id']
        self.assertEqual(self.casino.balance(1, 10), 900)
        with ThreadPoolExecutor(max_workers=6) as pool:
            list(pool.map(lambda i: self.store.act(1, 10, round_id, 'action' + str(i), 'stand', 0, 221), range(12)))
        self.assertEqual(self.casino.balance(1, 10), 1000)
        self.assertEqual(len([row for row in self.casino.history(1, 10) if row['kind'] == 'blackjack']), 1)

    def test_resuming_expired_round_settles_without_new_charge(self):
        self.begin(9, 8, 6, 7)
        state = self.store.start(1, 10, 'resume-expired', 500, 221)
        self.assertEqual((state['round_id'], state['status'], state['wager'], state['balance']), ('round', 'done', 100, 1000))
        self.assertEqual(len([row for row in self.casino.history(1, 10) if row['kind'] == 'blackjack_bet']), 1)

    def test_validation_before_drawing_and_escrow_not_spendable(self):
        with patch('blackjack.new_deck') as draw:
            for amount in (None, 0, 9, 10001, 2000):
                with self.assertRaises(CasinoError):
                    self.store.start(1, 10, str(amount), amount, 100)
            draw.assert_not_called()
        self.begin(9, 8, 6, 7)
        with self.assertRaises(CasinoError):
            self.casino.gift(1, 10, 20, 'too-much', 901)
        self.assertEqual(self.casino.balance(1, 10), 900)

    def test_payout_headroom_is_reserved_until_settlement(self):
        with closing(self.casino.connect()) as db, db:
            db.execute('UPDATE casino_wallets SET balance=? WHERE guild_id=1 AND user_id=10', (MAX_BALANCE - 150,))
        state = self.begin(9, 8, 6, 7)
        with self.assertRaises(CasinoError):
            self.casino.edit(1, 10, 'overflow', 'add', 1, 99)
        self.store.act(1, 10, 'round', 'stand', 'stand', 0, 101)
        self.assertEqual(self.casino.balance(1, 10), MAX_BALANCE - 150)


class BlackjackUITests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.casino = CasinoStore(Path(self.temp.name) / 'casino.sqlite3')
        self.store = BlackjackStore(self.casino)
        self.casino.edit(1, 10, 'seed', 'set', 1000, 99)
        self.service = BlackjackService(SimpleNamespace(add_view=Mock()), self.store)
        async def inline(function, *args, **kwargs):
            return function(*args, **kwargs)
        patcher = patch('blackjack.asyncio.to_thread', side_effect=inline)
        patcher.start()
        self.addCleanup(patcher.stop)

    async def test_start_hides_dealer_card_and_binds_persistent_view(self):
        interaction = SimpleNamespace(id=100, guild_id=1, channel_id=123, user=SimpleNamespace(id=10),
            response=SimpleNamespace(defer=AsyncMock()), edit_original_response=AsyncMock(return_value=SimpleNamespace(id=456)))
        with patch('blackjack.new_deck', return_value=deck_for(9, 8, 6, 7)):
            await self.service.start(interaction, 100)
        interaction.response.defer.assert_awaited_once_with(thinking=True)
        kwargs = interaction.edit_original_response.call_args.kwargs
        dealer_field = kwargs['embed'].fields[1]
        self.assertIn('🂠', dealer_field.value)
        self.assertNotIn('♠8', dealer_field.value)
        self.assertTrue(kwargs['view'].is_persistent())
        row = self.store.active()[0]
        self.assertEqual((row['message_id'], row['channel_id']), (456, 123))
        kwargs['view'].stop()

    async def test_owner_guard_and_restore_after_restart(self):
        with patch('blackjack.new_deck', return_value=deck_for(9, 8, 6, 7)):
            state = self.store.start(1, 10, 'round', 100)
        self.store.bind_message(state, 123, 456)
        view = BlackjackView(self.service, state)
        interaction = SimpleNamespace(guild_id=1, user=SimpleNamespace(id=20), response=SimpleNamespace(send_message=AsyncMock()))
        self.assertFalse(await view.interaction_check(interaction))
        interaction.user.id = 10
        self.assertTrue(await view.interaction_check(interaction))
        with patch.object(self.service.expire_tick, 'is_running', return_value=True):
            await self.service.on_ready()
        self.service.bot.add_view.assert_called_once()
        self.assertEqual(self.service.bot.add_view.call_args.kwargs['message_id'], 456)
        self.service.bot.add_view.call_args.args[0].stop()
        view.stop()

    async def test_command_registration_and_resume_option(self):
        bot = commands.Bot(command_prefix='!', intents=discord.Intents.default())
        group = discord.app_commands.Group(name='casino', description='casino', guild_only=True)
        service = register_blackjack(bot, group, self.casino)
        command = group.get_command('blackjack')
        self.assertFalse(command.parameters[0].required)
        service.start = AsyncMock()
        await command.callback('interaction', None)
        service.start.assert_awaited_once_with('interaction', None)


if __name__ == '__main__':
    unittest.main()
