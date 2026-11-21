import asyncio
from pathlib import Path
import random
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

import discord
from discord.ext import commands

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'Discord-GameBot'))
from othello import Othello, coordinate, flips, legal_moves, new_board, register_othello
from casino import CasinoStore
from leveling import LevelStore
from party_rewards import PartyRewards, reward_party as actual_reward_party


def interaction(uid=10, channel=2):
    return SimpleNamespace(user=SimpleNamespace(id=uid, display_name=f'Player{uid}', bot=False),
        guild=SimpleNamespace(id=1), channel=SimpleNamespace(id=channel),
        response=SimpleNamespace(defer=AsyncMock(), send_modal=AsyncMock(), send_message=AsyncMock()),
        edit_original_response=AsyncMock(return_value=SimpleNamespace(edit=AsyncMock())))


class RulesTests(unittest.TestCase):
    def test_initial_legal_moves_and_invalid_cells(self):
        board = new_board()
        self.assertEqual(set(legal_moves(board, 1)), {(2, 3), (3, 2), (4, 5), (5, 4)})
        self.assertEqual(set(legal_moves(board, 2)), {(2, 4), (3, 5), (4, 2), (5, 3)})
        for r, c in ((3, 3), (0, 0), (-1, 2), (8, 2)):
            self.assertEqual(flips(board, 1, r, c), [])

    def test_all_eight_capture_directions_and_no_wrap(self):
        board = [[0] * 8 for _ in range(8)]
        for dr, dc in ((-1,-1),(-1,0),(-1,1),(0,-1),(0,1),(1,-1),(1,0),(1,1)):
            board[3 + dr][3 + dc] = 2
            board[3 + 2*dr][3 + 2*dc] = 1
        self.assertEqual(len(flips(board, 1, 3, 3)), 8)
        self.assertEqual(flips(board, 1, 0, 0), [])

    def test_coordinate_accepts_full_width_but_rejects_outside_board(self):
        self.assertEqual(coordinate(' ｄ３ '), (2, 3))
        self.assertEqual(coordinate('H8'), (7, 7))
        for text in ('I1', 'A0', 'A9', '1A', '', 'A11'):
            with self.assertRaises(ValueError):
                coordinate(text)


class GameTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.registry = {}
        self.game = Othello(SimpleNamespace(), interaction(), self.registry, 2)
        self.registry[self.game.key] = self.game
        self.game.board = SimpleNamespace(edit=AsyncMock())
        self.rewards = AsyncMock()
        self.patcher = patch('othello.reward_party', self.rewards)
        self.patcher.start()
        self.addCleanup(self.patcher.stop)
        self.addAsyncCleanup(self.cleanup_game)

    async def cleanup_game(self):
        self.game.cancel_timer()
        self.game.stop()
        if self.game.timer:
            await asyncio.gather(self.game.timer, return_exceptions=True)

    async def start_game(self):
        await self.game.dispatch(interaction(20), 'join')
        with patch('othello.random.sample', return_value=[10, 20]):
            await self.game.dispatch(interaction(10), 'start')
        self.assertEqual(self.game.phase, 'playing')

    async def play_first(self, double=False):
        uid = self.game.colors[self.game.turn]
        row, col = next(iter(legal_moves(self.game.cells, self.game.turn)))
        await self.game.play(interaction(uid), row, col, double, self.game.revision)
        return uid

    async def test_lobby_permissions_capacity_and_random_colors(self):
        await self.game.dispatch(interaction(20), 'start')
        self.assertEqual(self.game.phase, 'lobby')
        await self.game.dispatch(interaction(10), 'start')
        self.assertEqual(self.game.phase, 'lobby')
        await self.start_game()
        self.assertEqual(self.game.colors, {1:10, 2:20})
        self.assertEqual(self.game.remaining, {10:2, 20:2})
        await self.game.dispatch(interaction(30), 'join')
        self.assertNotIn(30, self.game.players)

    async def test_ordinary_move_flips_and_changes_turn(self):
        await self.start_game()
        await self.game.play(interaction(), 2, 3, False, self.game.revision)
        self.assertEqual(self.game.cells[3][3], 1)
        self.assertEqual(sum(row.count(1) for row in self.game.cells), 4)
        self.assertEqual(self.game.turn, 2)
        self.assertEqual(self.game.remaining[10], 2)

    async def test_double_move_consumes_once_and_cannot_chain(self):
        await self.start_game()
        for _ in range(4):
            await self.play_first()
        await self.play_first(double=True)
        self.assertEqual(self.game.turn, 1)
        self.assertTrue(self.game.second)
        self.assertEqual(self.game.remaining[10], 1)
        revision = self.game.revision
        candidates = legal_moves(self.game.cells, 1)
        for (row, col), captured in candidates.items():
            trial = [line[:] for line in self.game.cells]
            trial[row][col] = 1
            for r, c in captured:
                trial[r][c] = 1
            if legal_moves(trial, 2):
                break
        self.assertTrue(legal_moves(trial, 2))
        await self.game.play(interaction(), row, col, True, revision)
        self.assertEqual(self.game.revision, revision)
        await self.game.play(interaction(), row, col, False, revision)
        self.assertEqual(self.game.turn, 2)
        self.assertFalse(self.game.second)
        self.assertEqual(self.game.remaining[10], 1)

    async def test_invalid_out_of_turn_stale_and_zero_skill_do_not_mutate(self):
        await self.start_game()
        revision = self.game.revision
        await self.game.play(interaction(20), 2, 3, False, revision)
        await self.game.play(interaction(10), 0, 0, True, revision)
        await self.game.play(interaction(10), 2, 3, False, revision - 1)
        self.assertEqual(self.game.cells, new_board())
        self.assertEqual(self.game.remaining[10], 2)
        self.game.remaining[10] = 0
        await self.game.play(interaction(10), 2, 3, True, revision)
        self.assertEqual(self.game.cells, new_board())

    async def test_multiple_open_modals_cannot_submit_two_first_moves(self):
        await self.start_game()
        for _ in range(4):
            await self.play_first()
        call = interaction()
        await self.game.dispatch(call, 'double')
        modal = call.response.send_modal.call_args.args[0]
        row, col = next(iter(legal_moves(self.game.cells, self.game.turn)))
        modal.position._value = f'{chr(65+col)}{row+1}'
        await modal.on_submit(interaction())
        after = [row[:] for row in self.game.cells]
        await modal.on_submit(interaction())
        self.assertEqual(self.game.cells, after)
        self.assertEqual(self.game.remaining[10], 1)

    async def test_opponent_without_moves_passes_automatically(self):
        await self.start_game()
        self.game.cells = [[1] * 8 for _ in range(8)]
        self.game.cells[0][1] = self.game.cells[4][1] = 2
        self.game.cells[0][2] = self.game.cells[4][2] = 0
        await self.game.play(interaction(), 0, 2, False, self.game.revision)
        self.assertEqual(self.game.phase, 'playing')
        self.assertEqual(self.game.turn, 1)
        self.assertIn('自動パス', self.game.note)

    async def test_double_second_move_unavailable_passes_to_opponent(self):
        await self.start_game()
        # A deterministic search yields an endgame where only the opponent can move next.
        rng = random.Random(812)
        for _ in range(200):
            board = [[rng.choices((0,1,2), weights=(1,8,8))[0] for _ in range(8)] for _ in range(8)]
            found = None
            for (row, col), captured in legal_moves(board, 1).items():
                trial = [line[:] for line in board]
                trial[row][col] = 1
                for r,c in captured:
                    trial[r][c] = 1
                if not legal_moves(trial,1) and legal_moves(trial,2):
                    found = (row,col)
                    break
            if found:
                break
        self.assertIsNotNone(found)
        self.game.cells = board
        self.game.move_count = 10
        await self.game.play(interaction(), *found, True, self.game.revision)
        self.assertEqual(self.game.turn, 2)
        self.assertFalse(self.game.second)
        self.assertEqual(self.game.remaining[10], 1)
        self.assertIn('2手目を置けない', self.game.note)

    async def test_full_game_finishes_once_with_correct_rewards(self):
        await self.start_game()
        for n in range(60):
            if self.game.phase == 'finished':
                break
            await self.play_first()
        self.assertEqual(self.game.phase, 'finished')
        self.assertGreaterEqual(n, 40)
        self.assertNotIn(self.game.key, self.registry)
        self.rewards.assert_awaited_once()
        black = sum(row.count(1) for row in self.game.cells)
        white = sum(row.count(2) for row in self.game.cells)
        outcomes = self.rewards.call_args.args[4]
        self.assertEqual(outcomes, {10:black > white, 20:white > black})
        await self.game.finish('again', {10:True})
        self.rewards.assert_awaited_once()

    async def test_early_finish_with_empty_cells_and_draw(self):
        await self.start_game()
        self.game.cells = [[0]*8 for _ in range(8)]
        self.game.cells[0][:4] = [1,2,0,0]
        await self.game.play(interaction(), 0, 2, False, self.game.revision)
        self.assertEqual(self.game.phase, 'finished')
        self.rewards.assert_awaited_once()
        self.assertGreater(sum(row.count(0) for row in self.game.cells), 0)

    async def test_draw_gives_both_participation_rewards(self):
        await self.start_game()
        # Black gains 2 stones from the final move, bringing 30:33 to 32:32.
        self.game.cells = [[1 if r < 4 else 2 for c in range(8)] for r in range(8)]
        self.game.cells[0][0] = 0
        self.game.cells[0][1] = 2
        await self.game.play(interaction(), 0, 0, False, self.game.revision)
        self.assertEqual(self.game.phase, 'finished')
        self.assertEqual(self.game.outcomes, {10:False, 20:False})
        self.assertIn('引き分け', self.game.result)

    async def test_timeout_ignores_stale_timer_and_has_no_rewards(self):
        await self.start_game()
        with patch('othello.asyncio.sleep', new=AsyncMock()):
            await self.game.expire(self.game.timer_token - 1, 0)
            self.assertEqual(self.game.phase, 'playing')
            await self.game.expire(self.game.timer_token, 0)
        self.assertEqual(self.game.phase, 'finished')
        self.assertNotIn(self.game.key, self.registry)
        self.rewards.assert_not_awaited()

    async def test_surrender_confirmation_and_no_rewards(self):
        await self.start_game()
        call = interaction()
        await self.game.dispatch(call, 'resign')
        view = call.response.send_message.call_args.kwargs['view']
        await view.confirm.callback(interaction(30))
        self.assertEqual(self.game.phase, 'playing')
        await view.confirm.callback(interaction())
        self.assertEqual(self.game.phase, 'finished')
        self.rewards.assert_not_awaited()

    async def test_pending_reward_retry_uses_same_game_and_outcomes(self):
        await self.start_game()
        self.rewards.side_effect = RuntimeError('temporary database failure')
        await self.game.finish('winner', {10:True, 20:False})
        self.assertTrue(self.game.rewards_pending)
        first = self.rewards.call_args.args
        self.rewards.side_effect = None
        await self.game.dispatch(interaction(), 'retry')
        self.assertFalse(self.game.rewards_pending)
        self.assertEqual(self.rewards.call_args.args, first)
        await self.game.dispatch(interaction(), 'retry')
        self.assertEqual(self.rewards.await_count, 2)

    async def test_triple_rewards_credit_wallet_and_xp_once_and_preserve_other_games(self):
        self.assertIn('300ジュエル＋30XP', self.game.embed().description)
        await self.start_game()
        with tempfile.TemporaryDirectory() as directory:
            casino = CasinoStore(Path(directory) / 'casino.sqlite3')
            levels = LevelStore(Path(directory) / 'levels.sqlite3')
            store = PartyRewards(casino, levels)
            self.game.bot.party_rewards = store
            self.game.bot.leveling_service = SimpleNamespace(notify=AsyncMock(
                side_effect=[RuntimeError('notification failed'), None, None]))
            with patch('othello.reward_party', new=actual_reward_party):
                with self.assertLogs('othello', level='ERROR'):
                    await self.game.finish('winner', {10:True, 20:False})
                self.assertTrue(self.game.rewards_pending)
                self.assertEqual(casino.balance(1,10), 300)
                await self.game.dispatch(interaction(), 'retry')
            self.assertFalse(self.game.rewards_pending)
            for uid, gems, xp in ((10,300,30),(20,150,15)):
                self.assertEqual(casino.balance(1,uid), gems)
                self.assertEqual(levels.rank(1,uid)[0]['xp'], xp)
            self.assertIn('150ジュエル＋15XP', str(self.game.embed().to_dict()))
            for uid, game_id, won, gems, xp in ((30,'ito:unchanged',True,100,10),
                                               (40,'insider:unchanged',False,50,5),
                                               (50,'othello:draw',False,150,15)):
                store.grant(1,uid,game_id,won)
                store.grant(1,uid,game_id,won)
                self.assertEqual(casino.balance(1,uid), gems)
                self.assertEqual(levels.rank(1,uid)[0]['xp'], xp)

    async def test_deleted_board_releases_game(self):
        self.game.board.edit.side_effect = discord.NotFound(SimpleNamespace(status=404, reason='deleted'), 'gone')
        await self.game.dispatch(interaction(20), 'join')
        self.assertEqual(self.game.phase, 'finished')
        self.assertNotIn(self.game.key, self.registry)

    async def test_registration_option_and_duplicate_lobby(self):
        bot = commands.Bot(command_prefix='!', intents=discord.Intents.default())
        self.addAsyncCleanup(bot.close)
        registry = register_othello(bot)
        command = bot.tree.get_command('othello')
        option = command.parameters[0]
        self.assertEqual((option.display_name, option.min_value, option.max_value, option.default), ('連続置き回数',0,10,1))
        option = command.parameters[1]
        self.assertEqual((option.display_name, option.min_value, option.max_value, option.default), ('連続置き解禁ターン',1,60,5))
        call = interaction()
        await command.callback(call, 0)
        other = registry[(1,2)]
        self.assertEqual(other.double_uses, 0)
        self.assertEqual(other.unlock_turn, 5)
        await command.callback(interaction(), 2)
        self.assertIs(registry[(1,2)], other)
        await command.callback(interaction(channel=3), 3, 10)
        custom = registry[(1,3)]
        self.assertEqual((custom.double_uses, custom.unlock_turn), (3,10))
        custom.cancel_timer()
        custom.stop()
        await asyncio.gather(custom.timer, return_exceptions=True)
        other.cancel_timer()
        other.stop()
        await asyncio.gather(other.timer, return_exceptions=True)

    async def test_default_unlock_is_fifth_move_and_rejections_are_free(self):
        await self.start_game()
        for n in range(4):
            uid = self.game.colors[self.game.turn]
            row, col = next(iter(legal_moves(self.game.cells, self.game.turn)))
            await self.game.play(interaction(uid), row, col, True, self.game.revision)
            self.assertEqual(self.game.move_count, n)
            self.assertEqual(self.game.remaining, {10:2, 20:2})
            call = interaction(uid)
            await self.game.dispatch(call, 'double')
            call.response.send_modal.assert_not_awaited()
            await self.play_first()
        self.assertTrue(self.game.double_unlocked)
        await self.play_first(double=True)
        self.assertEqual(self.game.remaining[10], 1)
        self.assertEqual(self.game.move_count, 5)
        self.assertTrue(self.game.second)

    async def test_custom_unlock_turn_is_honored(self):
        await self.start_game()
        self.game.unlock_turn = 10
        self.game.move_count = 8
        await self.play_first(double=True)
        self.assertEqual(self.game.move_count, 8)
        self.game.move_count = 9
        await self.play_first(double=True)
        self.assertEqual(self.game.move_count, 10)
        self.assertEqual(self.game.remaining[10], 1)


if __name__ == '__main__':
    unittest.main()
