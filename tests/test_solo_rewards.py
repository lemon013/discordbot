import asyncio
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from pathlib import Path
import sqlite3
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'Discord-GameBot'))
from casino import CasinoStore, CasinoError, MAX_BALANCE
from leveling import LevelStore, threshold, level_for
from solo_rewards import SoloRewards, step_xp
from game_8192 import Game, Game8192View, register_8192
from kakigoori import KakigooriView
from blackjack import BlackjackStore
from aki import AkiService, AkiGame


def call(uid=10, guild=1):
    return SimpleNamespace(user=SimpleNamespace(id=uid), guild_id=guild, guild=SimpleNamespace(id=guild),
        channel=SimpleNamespace(id=2), response=SimpleNamespace(defer=AsyncMock(), send_message=AsyncMock(), edit_message=AsyncMock()),
        followup=SimpleNamespace(send=AsyncMock()), edit_original_response=AsyncMock(return_value=SimpleNamespace(edit=AsyncMock())))


class StoreTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.levels = LevelStore(Path(self.temp.name) / 'levels.db')
        self.casino = CasinoStore(Path(self.temp.name) / 'casino.db', levels=self.levels)
        self.store = SoloRewards(self.casino, self.levels)

    def xp(self):
        return self.levels.rank(1, 10)[0]['xp']

    def test_normal_awards_are_persistent_idempotent_and_do_not_change_text_voice_totals(self):
        first = self.store.grant(1, 10, 'solo:kaki:one', 3)
        self.assertTrue(first['new'])
        reopened = SoloRewards(CasinoStore(self.casino.path), LevelStore(self.levels.path))
        self.assertFalse(reopened.grant(1, 10, 'solo:kaki:one', 3)['new'])
        self.assertEqual(self.xp(), 3)
        user, _ = self.levels.rank(1, 10)
        self.assertEqual((user['text_xp'], user['voice_xp'], user['shop_xp']), (0, 0, 0))
        with self.assertRaises(ValueError):
            self.store.grant(1, 10, 'solo:kaki:one', 5)

    def test_clear_adds_exactly_three_levels_preserves_progress_and_pays_once(self):
        self.levels.edit(1, 10, 'xp', 'set', threshold(5) + 27)
        before = self.xp()
        reward = self.store.grant(1, 10, 'solo:8192:clear', 36, 3, 3000)
        self.assertEqual(reward['levels_gained'], 3)
        self.assertEqual(self.xp(), threshold(8) + 27 + 36)
        self.assertEqual(self.casino.balance(1, 10), 3000)
        self.store.grant(1, 10, 'solo:8192:clear', 36, 3, 3000)
        self.assertEqual(self.xp(), threshold(8) + 63)
        self.assertEqual(self.casino.balance(1, 10), 3000)

    def test_clear_near_level_cap_never_discards_xp(self):
        self.levels.edit(1, 10, 'xp', 'set', threshold(99) + 20)
        reward = self.store.grant(1, 10, 'clear99', 36, 3, 3000)
        self.assertEqual(reward['levels_gained'], 1)
        self.assertEqual(self.xp(), threshold(100) + 56)
        reward = self.store.grant(1, 10, 'clear100', 36, 3, 3000)
        self.assertEqual(reward['levels_gained'], 0)
        self.assertEqual(self.xp(), threshold(100) + 92)

    def test_clear_rolls_back_both_databases_if_jewel_limit_prevents_payment(self):
        self.casino.edit(1, 10, 'seed', 'set', 10**9, 99)
        with closing(self.casino.connect()) as db, db:
            db.execute('UPDATE casino_wallets SET balance=?', (MAX_BALANCE,))
        with self.assertRaises(CasinoError):
            self.store.grant(1, 10, 'failed-clear', 36, 3, 3000)
        self.assertEqual(self.xp(), 0)
        self.assertEqual(self.casino.balance(1, 10), MAX_BALANCE)
        self.casino.edit(1, 10, 'spend', 'subtract', 3000, 99)
        self.store.grant(1, 10, 'failed-clear', 36, 3, 3000)
        self.assertGreater(self.xp(), 36)
        self.assertEqual(self.casino.balance(1, 10), MAX_BALANCE)

    def test_concurrent_awards_credit_once_and_same_database_layout_is_supported(self):
        with ThreadPoolExecutor(max_workers=4) as pool:
            rewards = list(pool.map(lambda _: self.store.grant(1, 10, 'concurrent', 3), range(8)))
        self.assertEqual(sum(r['new'] for r in rewards), 1)
        self.assertEqual(self.xp(), 3)
        path = Path(self.temp.name) / 'combined.db'
        levels = LevelStore(path)
        casino = CasinoStore(path, levels=levels)
        SoloRewards(casino, levels).grant(1, 10, 'combined', 3, 3, 3000)
        self.assertEqual(casino.balance(1, 10), 3000)
        self.assertEqual(levels.rank(1, 10)[0]['xp'], threshold(3) + 3)

    def test_casino_win_loss_push_and_invalid_bet_have_atomic_xp(self):
        self.casino.edit(1, 10, 'seed', 'set', 1000, 99)
        for index, payout in enumerate((190, 0, 100)):
            with patch('casino.draw', return_value={'outcome': 'result', 'payout': payout}):
                result = self.casino.play(1, 10, f'bet{index}', 'coinflip', 100, 0, now=100 + index * 4)
            self.assertEqual(result['xp_reward']['base_xp'], 5 if index == 0 else 3)
            replay = self.casino.play(1, 10, f'bet{index}', 'coinflip', 100, 0, now=100)
            self.assertFalse(replay['xp_reward']['new'])
        self.assertEqual(self.xp(), 11)
        with self.assertRaises(CasinoError):
            self.casino.play(1, 10, 'invalid', 'coinflip', 10000, 0, now=200)
        self.assertEqual(self.xp(), 11)

    def test_casino_money_and_xp_rollback_together_on_ledger_failure(self):
        self.casino.edit(1, 10, 'seed', 'set', 1000, 99)
        actual_finish = self.casino.finish
        def finish(db, event, guild, user, kind, old, result, now):
            if kind == 'coinflip':
                raise sqlite3.OperationalError('ledger unavailable')
            return actual_finish(db, event, guild, user, kind, old, result, now)
        with patch.object(self.casino, 'finish', side_effect=finish):
            with self.assertRaises(sqlite3.Error):
                self.casino.play(1, 10, 'bet', 'coinflip', 100, 0, now=100)
        self.assertEqual(self.xp(), 0)
        self.assertEqual(self.casino.balance(1, 10), 1000)

    def test_blackjack_natural_and_timeout_pay_only_when_settled(self):
        self.casino.edit(1, 10, 'seed', 'set', 1000, 99)
        store = BlackjackStore(self.casino)
        def deck(*draws):
            return [x for x in range(52) if x not in draws] + list(reversed(draws))
        with patch('blackjack.new_deck', return_value=deck(0, 8, 12, 6)):
            state = store.start(1, 10, 'natural', 100, now=100)
        self.assertEqual(state['xp_reward']['base_xp'], 5)
        store.start(1, 10, 'natural', 100, now=101)
        self.assertEqual(self.xp(), 5)
        with patch('blackjack.new_deck', return_value=deck(9, 8, 6, 7)):
            state = store.start(1, 10, 'timeout', 100, now=104)
        self.assertIsNone(state.get('xp_reward'))
        self.assertEqual(self.xp(), 5)
        settled = store.expire(now=1000)
        self.assertEqual(settled[0]['xp_reward']['base_xp'], 3)
        self.assertEqual(self.xp(), 8)
        self.assertEqual(store.expire(now=2000), [])
        self.assertFalse(store.start(1, 10, 'timeout', now=2001)['xp_reward']['new'])


class FlowTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.levels = LevelStore(Path(self.temp.name) / 'levels.db')
        self.casino = CasinoStore(Path(self.temp.name) / 'casino.db', levels=self.levels)
        self.bot = SimpleNamespace(solo_rewards=SoloRewards(self.casino, self.levels),
            leveling_service=SimpleNamespace(store=self.levels, notify=AsyncMock()))

    def xp(self):
        return self.levels.rank(1, 10)[0]['xp']

    async def test_8192_full_clear_has_correct_incremental_rewards_and_stale_buttons_are_rejected(self):
        registry = {}
        game = Game(self.bot, call(), registry)
        registry[game.key] = game
        view = Game8192View(game)
        for count in range(1, 14):
            interaction = call()
            old = view
            await view.choose(interaction, view.correct_color)
            self.assertEqual(game.count, count)
            base = 3 * count * (count - 1) // 2
            self.assertEqual(self.xp(), base if count < 13 else threshold(3) + base)
            self.assertEqual(self.casino.balance(1, 10), 0 if count < 13 else 3000)
            again = call()
            await old.choose(again, old.correct_color)
            again.response.defer.assert_not_awaited()
            if count < 13:
                view = interaction.edit_original_response.call_args.kwargs['view']
            else:
                self.assertIsNone(interaction.edit_original_response.call_args.kwargs['view'])
        self.assertFalse(registry)
        self.assertEqual(sum(step_xp(n) for n in range(1, 14)), 234)

    async def test_8192_reward_failure_is_retryable_and_loss_keeps_previous_rewards(self):
        game = Game(self.bot, call(), {})
        game.count = 1
        view = Game8192View(game)
        actual = self.bot.solo_rewards.grant
        with patch.object(self.bot.solo_rewards, 'grant', side_effect=sqlite3.OperationalError('busy')):
            await view.choose(call(), view.correct_color)
        self.assertEqual(game.count, 1)
        interaction = call()
        await view.choose(interaction, view.correct_color)
        self.assertEqual(self.xp(), 3)
        next_view = interaction.edit_original_response.call_args.kwargs['view']
        await next_view.choose(call(), 'green' if next_view.correct_color == 'blue' else 'blue')
        self.assertTrue(game.done)
        self.assertEqual(self.xp(), 3)
        view.stop()

    async def test_kakigoori_completion_once_per_recipe_and_reset_starts_new_reward(self):
        view = KakigooriView(10, 'player', self.bot)
        view.recipe = {'strawberry': 1}
        await view.finish.callback(call())
        await view.finish.callback(call())
        self.assertEqual(self.xp(), 3)
        await view.reset.callback(call())
        await view.finish.callback(call())
        self.assertEqual(self.xp(), 3)
        view.recipe = {'milk': 1}
        await view.finish.callback(call())
        self.assertEqual(self.xp(), 6)
        view.stop()

    async def test_clear_uses_existing_level_up_jewel_rewards_separately(self):
        from leveling import Leveling
        leveling = Leveling(self.bot, self.levels)
        leveling.jewel_store = self.casino
        self.bot.leveling_service = leveling
        self.levels.configure(1, {'notifications': 0})
        game = Game(self.bot, call(), {})
        game.count = 12
        view = Game8192View(game)
        await view.choose(call(), view.correct_color)
        self.assertEqual(level_for(self.xp()), 3)
        self.assertEqual(self.casino.balance(1, 10), 3000 + 3 * 200)

    async def test_8192_wrong_user_and_timeout_never_award_xp(self):
        registry = {}
        game = Game(self.bot, call(), registry)
        registry[game.key] = game
        view = Game8192View(game)
        await view.choose(call(uid=11), view.correct_color)
        self.assertEqual(self.xp(), 0)
        view.message = SimpleNamespace(edit=AsyncMock())
        await view.on_timeout()
        self.assertFalse(registry)
        self.assertEqual(self.xp(), 0)
        view.stop()

    async def test_aki_result_pays_once_quit_does_not_and_failed_reward_retries_without_ai(self):
        ai = SimpleNamespace(bot=self.bot)
        service = AkiService(ai)
        game = AkiGame(service, call(), 'gemini-2.5-flash')
        game.message = SimpleNamespace(guild=call().guild, channel=call().channel, edit=AsyncMock())
        game.answers = [{'question': 'Q', 'answer': 'はい'}] * 4
        game.phase, game.text = 'guess', 'マリオ'
        service.games[game.key] = game
        with patch.object(self.bot.solo_rewards, 'grant', side_effect=sqlite3.OperationalError('busy')):
            await game.act(call(), 0, 'correct')
        self.assertFalse(game.done)
        self.assertIsNotNone(game.pending_finish)
        service.engine.next_step = AsyncMock()
        await game.act(call(), 1, 'retry')
        self.assertTrue(game.done)
        self.assertEqual(self.xp(), 3)
        service.engine.next_step.assert_not_awaited()
        await game.act(call(), 2, 'correct')
        self.assertEqual(self.xp(), 3)
        quit_game = AkiGame(service, call(), 'gemini-2.5-flash')
        quit_game.message = game.message
        await quit_game.act(call(), 0, 'quit')
        self.assertEqual(self.xp(), 3)
