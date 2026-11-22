import asyncio
import json
from pathlib import Path
import sys
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock, patch

import discord
from discord.ext import commands

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'Discord-GameBot'))
from aki import AkiService, MAX_QUESTIONS, MAX_GUESSES, FIRST_QUESTION, SYSTEM, register_aki
from gemini_chat import AIError


def response(kind='question', text='日本で生まれましたか？'):
    return {'candidates': [{'finishReason': 'STOP', 'content': {'parts': [
        {'text': json.dumps({'kind': kind, 'text': text}, ensure_ascii=False)}]}}]}


def interaction(uid=870012345678901234, guild=1, board=None):
    board = board or SimpleNamespace(edit=AsyncMock())
    return SimpleNamespace(user=SimpleNamespace(id=uid), guild_id=guild,
        guild=SimpleNamespace(id=guild) if guild else None, message=board,
        response=SimpleNamespace(send_message=AsyncMock(), defer=AsyncMock()),
        original_response=AsyncMock(return_value=board))


class AkiTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.env = patch.dict('os.environ', {'GEMINI_API_KEY': 'test-placeholder'})
        self.env.start()
        self.addCleanup(self.env.stop)
        self.ai = SimpleNamespace(settings=Mock(return_value={'model': 'gemini-2.5-flash', 'reasoning': 'high'}),
            request=AsyncMock(return_value=response()), history={'chat': ['unrelated conversation']})
        self.service = AkiService(self.ai)
        self.addCleanup(self.release_games)

    def release_games(self):
        for game in list(self.service.games.values()):
            game.release()

    async def start(self, uid=870012345678901234, guild=1):
        call = interaction(uid, guild)
        await self.service.start(call)
        return self.service.games[(guild, uid)], call

    def four_answers(self, game):
        game.answers = [{'question': f'質問{i}？', 'answer': 'はい'} for i in range(4)]

    async def test_start_has_five_answers_no_api_call_and_a_guild_only_slash_command(self):
        bot = commands.Bot(command_prefix='!', intents=discord.Intents.none())
        try:
            register_aki(bot, self.ai)
            self.assertTrue(bot.tree.get_command('aki').guild_only)
        finally:
            await bot.close()
        game, call = await self.start()
        call.response.send_message.assert_awaited_once()
        self.assertEqual(len(game.view.children), 6)
        self.assertIn(FIRST_QUESTION, call.response.send_message.call_args.kwargs['embed'].description)
        self.ai.request.assert_not_awaited()

    async def test_real_engine_request_uses_only_game_history_and_pins_the_model(self):
        game, _ = await self.start()
        self.ai.settings.return_value = {'model': 'some-other-model', 'reasoning': 'off'}
        await game.view.children[0].callback(interaction(board=game.message))
        method, endpoint, payload = self.ai.request.call_args.args
        self.assertEqual((method, endpoint), ('POST', '/models/gemini-2.5-flash:generateContent'))
        self.assertEqual(payload['system_instruction']['parts'][0]['text'], SYSTEM)
        state = json.loads(payload['contents'][0]['parts'][0]['text'])
        self.assertEqual(state['answers'], [{'question': FIRST_QUESTION, 'answer': 'はい'}])
        self.assertEqual(payload['generationConfig']['thinkingConfig'], {'thinkingBudget': 1024})
        self.assertEqual(payload['generationConfig']['responseMimeType'], 'application/json')
        self.assertIn('responseJsonSchema', payload['generationConfig'])
        self.assertNotIn(str(game.owner), json.dumps(payload))
        self.assertNotIn('unrelated conversation', json.dumps(payload))
        self.assertEqual(self.ai.history, {'chat': ['unrelated conversation']})

    async def test_strangers_and_stale_buttons_never_call_ai(self):
        game, _ = await self.start()
        old = game.view.children[0]
        stranger = interaction(uid=2)
        await old.callback(stranger)
        self.assertTrue(stranger.response.send_message.call_args.kwargs['ephemeral'])
        self.ai.request.assert_not_awaited()
        await old.callback(interaction())
        await old.callback(interaction())
        self.assertEqual(self.ai.request.await_count, 1)
        self.assertEqual(len(game.answers), 1)

    async def test_double_click_during_ai_request_only_advances_once(self):
        game, _ = await self.start()
        entered, release = asyncio.Event(), asyncio.Event()
        async def next_step(_):
            entered.set()
            await release.wait()
            return 'question', '日本人ですか？'
        self.service.engine.next_step = AsyncMock(side_effect=next_step)
        button = game.view.children[0]
        first = asyncio.create_task(button.callback(interaction()))
        await entered.wait()
        second = interaction()
        await button.callback(second)
        release.set()
        await first
        self.service.engine.next_step.assert_awaited_once()
        self.assertEqual(len(game.answers), 1)
        second.response.defer.assert_not_awaited()

    async def test_error_retry_keeps_exactly_one_answer_and_then_resumes(self):
        game, _ = await self.start()
        self.ai.request.side_effect = [AIError('利用制限です。'), response()]
        await game.act(interaction(), 0, 'no')
        self.assertEqual(game.phase, 'error')
        self.assertEqual([b.action for b in game.view.children], ['retry', 'quit'])
        await game.view.children[0].callback(interaction())
        self.assertEqual(game.phase, 'question')
        self.assertEqual(len(game.answers), 1)
        self.assertEqual(game.answers[0]['answer'], 'いいえ')
        self.assertEqual(self.ai.request.await_count, 2)

    async def test_correct_guess_ends_once_and_removes_buttons(self):
        game, _ = await self.start()
        self.four_answers(game)
        game.phase, game.text, game.guesses = 'guess', 'マリオ', 1
        await game.act(interaction(), 0, 'correct')
        self.assertTrue(game.done)
        self.assertNotIn(game.key, self.service.games)
        self.assertIn('マリオ', game.message.edit.call_args.kwargs['embed'].description)
        self.assertIsNone(game.message.edit.call_args.kwargs['view'])
        await game.act(interaction(), 1, 'correct')
        self.ai.request.assert_not_awaited()

    async def test_wrong_guesses_are_excluded_and_third_miss_ends(self):
        game, _ = await self.start()
        self.four_answers(game)
        game.phase, game.text, game.guesses = 'guess', 'マリオ', 1
        self.ai.request.side_effect = [response('guess', 'ルイージ'), response('guess', 'ピーチ')]
        for _ in range(MAX_GUESSES):
            await game.act(interaction(), game.revision, 'wrong')
        self.assertEqual(game.rejected, ['マリオ', 'ルイージ', 'ピーチ'])
        self.assertTrue(game.done)
        self.assertEqual(self.ai.request.await_count, 2)
        state = json.loads(self.ai.request.call_args.args[2]['contents'][0]['parts'][0]['text'])
        self.assertEqual(state['rejected_guesses'], ['マリオ', 'ルイージ'])

    async def test_twentieth_answer_forces_a_guess_and_cannot_add_question_21(self):
        game, _ = await self.start()
        game.answers = [{'question': f'過去の質問{i}', 'answer': 'はい'} for i in range(19)]
        self.ai.request.return_value = response('guess', 'カービィ')
        await game.act(interaction(), 0, 'unknown')
        self.assertEqual(len(game.answers), MAX_QUESTIONS)
        self.assertEqual(game.phase, 'guess')
        state = json.loads(self.ai.request.call_args.args[2]['contents'][0]['parts'][0]['text'])
        self.assertTrue(state['force_guess'])
        self.ai.request.return_value = response('question', '次の質問？')
        await game.act(interaction(), 1, 'wrong')
        self.assertEqual(game.phase, 'error')
        self.assertEqual(len(game.answers), 20)

    async def test_invalid_response_types_repetitions_and_early_guesses_are_rejected(self):
        game, _ = await self.start()
        cases = [response('guess', 'マリオ'), response('wrong_kind', 'text'), response('question', ''),
            response('question', ['not a string']), response('question', 'x' * 181),
            {'candidates': [{'content': {'parts': [{'text': '{bad JSON'}]}}]},
            {'candidates': [{'content': {'parts': [{'text': '[]'}]}}]}]
        for data in cases:
            self.ai.request.return_value = data
            with self.subTest(data=data), self.assertRaises(AIError):
                await self.service.engine.next_step(game)
        self.four_answers(game)
        for data in (response('question', '質問0？'), response('question', '質問0?')):
            self.ai.request.return_value = data
            with self.assertRaises(AIError):
                await self.service.engine.next_step(game)
        game.rejected = ['マリオ']
        self.ai.request.return_value = response('guess', 'マリオ')
        with self.assertRaises(AIError):
            await self.service.engine.next_step(game)

    async def test_timeout_and_unexpected_failures_do_not_expose_provider_details(self):
        game, _ = await self.start()
        self.ai.request.side_effect = asyncio.TimeoutError()
        await game.act(interaction(), 0, 'probably')
        self.assertEqual(game.phase, 'error')
        self.assertIn('タイムアウト', game.text)
        self.ai.request.side_effect = RuntimeError('private provider payload')
        with self.assertLogs('aki', level='WARNING') as output:
            await game.act(interaction(), 1, 'retry')
        self.assertNotIn('private provider payload', ''.join(output.output))
        self.assertNotIn('private provider payload', game.text)

    async def test_give_up_quit_and_inactivity_release_the_user(self):
        game, _ = await self.start()
        self.ai.request.return_value = response('give_up', 'わからない')
        await game.act(interaction(), 0, 'probably_not')
        self.assertTrue(game.done)
        other, _ = await self.start(uid=2)
        await other.act(interaction(uid=2), 0, 'quit')
        self.assertTrue(other.done)
        idle, _ = await self.start(uid=3)
        await idle.view.on_timeout()
        self.assertTrue(idle.done)
        self.assertFalse(self.service.games)

    async def test_start_guards_and_send_failure_leave_no_invisible_game(self):
        game, _ = await self.start()
        duplicate = interaction()
        await self.service.start(duplicate)
        self.assertTrue(duplicate.response.send_message.call_args.kwargs['ephemeral'])
        game.release()
        rapid = interaction()
        await self.service.start(rapid)
        self.assertIn('30秒', rapid.response.send_message.call_args.args[0])
        with patch.dict('os.environ', {'GEMINI_API_KEY': ''}):
            missing = interaction(uid=2)
            await self.service.start(missing)
            self.assertNotIn((1, 2), self.service.games)
        dm = interaction(guild=None)
        await self.service.start(dm)
        self.assertTrue(dm.response.send_message.call_args.kwargs['ephemeral'])
        failed = interaction(uid=3)
        failed.response.send_message.side_effect = RuntimeError('send failed')
        with self.assertRaises(RuntimeError):
            await self.service.start(failed)
        self.assertNotIn((1, 3), self.service.games)

    async def test_deleted_panel_releases_game_and_early_click_uses_message_from_button(self):
        game, _ = await self.start()
        board = game.message
        game.message = None
        await game.act(interaction(board=board), 0, 'yes')
        self.assertIs(game.message, board)
        board.edit.side_effect = discord.NotFound(SimpleNamespace(status=404, reason='Gone'), 'Gone')
        with self.assertLogs('aki', level='WARNING'):
            await game.act(interaction(board=board), 1, 'quit')
        self.assertTrue(game.done)
        self.assertNotIn(game.key, self.service.games)
