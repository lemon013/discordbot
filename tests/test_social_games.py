from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'Discord-GameBot'))
import discord
from casino import CasinoStore
from leveling import LevelStore
from party_rewards import PartyRewards
from social_games import SocialGame, insider_caught, ito_order_ok, register_social_games


class RulesTests(unittest.TestCase):
    def test_order_and_vote_ties(self):
        self.assertTrue(ito_order_ok({1: 3, 2: 90, 3: 30}, [], 1))
        self.assertFalse(ito_order_ok({1: 3, 2: 90, 3: 30}, [1], 2))
        self.assertTrue(insider_caught({1: 2, 2: 3, 3: 2}, 2))
        self.assertFalse(insider_caught({1: 2, 2: 3}, 2))
        self.assertFalse(insider_caught({}, 2))


class FlowTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.games = []
        self.reward = AsyncMock()
        self.patcher = patch('social_games.reward_party', self.reward)
        self.patcher.start()

    async def asyncTearDown(self):
        self.patcher.stop()
        for game in self.games:
            if game.timer:
                game.timer.cancel()
            game.stop()

    def interaction(self, uid=1):
        return SimpleNamespace(user=SimpleNamespace(id=uid, display_name=f'Player{uid}', bot=False),
            guild=SimpleNamespace(id=100), guild_id=100, channel=SimpleNamespace(id=200), channel_id=200,
            response=SimpleNamespace(defer=AsyncMock(), send_message=AsyncMock(), send_modal=AsyncMock()),
            edit_original_response=AsyncMock())

    def game(self, kind='ito', count=2):
        game = SocialGame(SimpleNamespace(), self.interaction(), {}, kind)
        game.players = {uid: self.interaction(uid).user for uid in range(1, count + 1)}
        game.registry[100] = game
        game.board = SimpleNamespace(edit=AsyncMock(), jump_url='https://discord.com/channels/test')
        self.games.append(game)
        return game

    async def test_ito_setup_secrets_hints_and_success_once(self):
        game = self.game()
        await game.dispatch(self.interaction(2), 'start')
        self.assertEqual(game.phase, 'lobby')
        await game.dispatch(self.interaction(), 'start')
        self.assertEqual(game.phase, 'ito')
        self.assertEqual(len(set(game.numbers.values())), 2)
        game.numbers = {1: 3, 2: 90}
        secret = self.interaction()
        await game.dispatch(secret, 'secret')
        self.assertIn('**3**', secret.edit_original_response.call_args.kwargs['content'])
        await game.submit_hint(self.interaction(), '3')
        self.assertEqual(game.hints, {})
        await game.submit_hint(self.interaction(), '小さな虫')
        await game.dispatch(self.interaction(), 'play')
        self.assertEqual(game.played, [])
        await game.submit_hint(self.interaction(2), 'ドラゴン')
        await game.dispatch(self.interaction(), 'play')
        self.reward.assert_not_awaited()
        await game.dispatch(self.interaction(2), 'play')
        self.assertEqual(game.outcomes, {1: True, 2: True})
        self.assertNotIn(100, game.registry)
        self.reward.assert_awaited_once()
        await game.dispatch(self.interaction(2), 'play')
        self.reward.assert_awaited_once()
        old = dict(game.hints)
        await game.submit_hint(self.interaction(), '古い画面からの変更')
        self.assertEqual(game.hints, old)

    async def test_duplicate_setting_is_host_only_and_locked_after_start(self):
        game = self.game()
        self.assertFalse(game.allow_duplicates)
        await game.dispatch(self.interaction(2), 'duplicates')
        self.assertFalse(game.allow_duplicates)
        await game.dispatch(self.interaction(), 'duplicates')
        self.assertTrue(game.allow_duplicates)
        self.assertIn('同じ数字', str(game.embed().to_dict()))
        with patch('social_games.RNG.choices', return_value=[42, 42]) as choose:
            await game.dispatch(self.interaction(), 'start')
        choose.assert_called_once_with(range(1, 101), k=2)
        self.assertEqual(game.numbers, {1: 42, 2: 42})
        await game.dispatch(self.interaction(), 'duplicates')
        self.assertTrue(game.allow_duplicates)
        game.hints = {1: 'りんご', 2: 'みかん'}
        await game.dispatch(self.interaction(2), 'play')
        await game.dispatch(self.interaction(), 'play')
        self.assertEqual(game.outcomes, {1: True, 2: True})

    async def test_equal_cards_still_cannot_skip_smaller_remaining_card(self):
        game = self.game(count=3)
        game.phase, game.allow_duplicates = 'ito', True
        game.numbers, game.hints = {1: 42, 2: 42, 3: 10}, {1: 'a', 2: 'b', 3: 'c'}
        await game.dispatch(self.interaction(), 'play')
        self.assertEqual(game.outcomes, {1: False, 2: False, 3: False})

    async def test_slash_option_sets_duplicate_mode(self):
        bot = SimpleNamespace(tree=discord.app_commands.CommandTree(discord.Client(intents=discord.Intents.none())))
        register_social_games(bot)
        command = bot.tree.get_command('ito')
        self.assertEqual(command.parameters[0].display_name, '数字の重複')
        self.assertFalse(command.parameters[0].required)
        interaction = self.interaction()
        await command.callback(interaction, allow_duplicates=True)
        game = interaction.edit_original_response.call_args.kwargs['view']
        self.games.append(game)
        self.assertTrue(game.allow_duplicates)

    async def test_ito_wrong_order_loses_and_cancellation_has_no_rewards(self):
        game = self.game()
        game.phase, game.numbers, game.hints = 'ito', {1: 3, 2: 90}, {1: '虫', 2: '竜'}
        await game.dispatch(self.interaction(2), 'play')
        self.assertEqual(game.outcomes, {1: False, 2: False})
        self.reward.reset_mock()
        cancelled = self.game()
        await cancelled.dispatch(self.interaction(2), 'cancel')
        self.assertEqual(cancelled.phase, 'lobby')
        await cancelled.dispatch(self.interaction(), 'cancel')
        self.assertEqual(cancelled.phase, 'cancelled')
        self.reward.assert_not_awaited()

    async def test_insider_briefing_secret_roles_and_common_victory(self):
        game = self.game('insider', 4)
        await game.dispatch(self.interaction(), 'start')
        game.topic, game.insider = '望遠鏡', 2
        self.assertNotIn('望遠鏡', game.embed().to_dict().__str__())
        await game.dispatch(self.interaction(), 'begin')
        self.assertEqual(game.phase, 'briefing')
        for uid in game.players:
            interaction = self.interaction(uid)
            await game.dispatch(interaction, 'secret')
            reply = interaction.edit_original_response.call_args.kwargs['content']
            self.assertEqual('望遠鏡' in reply, uid in (1, 2))
        await game.dispatch(self.interaction(), 'begin')
        self.assertEqual(game.phase, 'question')
        await game.dispatch(self.interaction(2), 'correct')
        self.assertEqual(game.phase, 'question')
        await game.dispatch(self.interaction(), 'correct')
        self.assertEqual(game.phase, 'discussion')
        await game.dispatch(self.interaction(), 'voting')
        for uid in game.players:
            await game.submit_vote(self.interaction(uid), 2)
        self.assertEqual(game.outcomes, {1: True, 2: False, 3: True, 4: True})
        self.reward.assert_awaited_once()

    async def test_insider_tie_and_timeouts(self):
        game = self.game('insider', 4)
        game.phase, game.insider, game.topic = 'vote', 2, '傘'
        await game.submit_vote(self.interaction(9), 2)
        await game.submit_vote(self.interaction(1), 1)
        self.assertEqual(game.votes, {})
        for uid, target in ((1, 2), (2, 3), (3, 2), (4, 3)):
            await game.submit_vote(self.interaction(uid), target)
        self.assertEqual(game.outcomes, {1: False, 2: True, 3: False, 4: False})
        expired = self.game('insider', 4)
        expired.phase, expired.insider, expired.topic = 'question', 2, '傘'
        await expired.expire('question', 0)
        self.assertTrue(all(not won for won in expired.outcomes.values()))
        self.reward.reset_mock()
        empty = self.game('insider', 4)
        await empty.expire('lobby', 0)
        self.assertEqual(empty.phase, 'cancelled')
        self.reward.assert_not_awaited()

    async def test_discussion_timeout_advances_to_vote_and_vote_timeout_finishes(self):
        game = self.game('insider', 4)
        game.phase, game.insider, game.topic = 'discussion', 2, '傘'
        await game.expire('discussion', 0)
        self.assertEqual(game.phase, 'vote')
        await game.expire('vote', 0)
        self.assertTrue(game.outcomes[2])

    async def test_reward_retry_credits_gems_and_xp_only_once_after_partial_failure(self):
        self.patcher.stop()
        with tempfile.TemporaryDirectory() as directory:
            casino = CasinoStore(Path(directory) / 'casino.sqlite3')
            levels = LevelStore(Path(directory) / 'levels.sqlite3')
            game = self.game()
            game.phase = 'ito'
            game.bot.party_rewards = PartyRewards(casino, levels)
            game.bot.leveling_service = SimpleNamespace(notify=AsyncMock(side_effect=[RuntimeError('notification failed'), None, None]))
            with self.assertLogs('social_games', level='ERROR'):
                await game.finish('成功', {1: True, 2: False})
            self.assertTrue(game.rewards_pending)
            self.assertEqual(casino.balance(100, 1), 100)
            # A newer game in the registry must not be removed by an old reward retry.
            newer = object()
            game.registry[100] = newer
            await game.dispatch(self.interaction(), 'retry')
            self.assertFalse(game.rewards_pending)
            self.assertIs(game.registry[100], newer)
            self.assertEqual(casino.balance(100, 1), 100)
            self.assertEqual(casino.balance(100, 2), 50)
            self.assertEqual(levels.rank(100, 1)[0]['xp'], 10)
            self.assertEqual(levels.rank(100, 2)[0]['xp'], 5)
        self.patcher.start()

    async def test_commands_register_and_existing_game_is_reused(self):
        bot = SimpleNamespace(tree=discord.app_commands.CommandTree(discord.Client(intents=discord.Intents.none())))
        register_social_games(bot)
        self.assertEqual({command.name for command in bot.tree.get_commands()}, {'ito', 'insider'})
        interaction = self.interaction()
        interaction.edit_original_response.return_value = SimpleNamespace(edit=AsyncMock(), jump_url='https://discord.com/channels/test')
        await bot.tree.get_command('ito').callback(interaction)
        game = interaction.edit_original_response.call_args.kwargs['view']
        self.games.append(game)
        second = self.interaction(2)
        await bot.tree.get_command('insider').callback(second)
        self.assertIn('ito', second.edit_original_response.call_args.kwargs['content'])


if __name__ == '__main__':
    unittest.main()
