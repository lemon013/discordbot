import asyncio
import json
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'Discord-GameBot'))
from gemini_chat import GeminiChat, AIError, SYSTEM_INSTRUCTION, FIXED_REPLY, active_provider, thinking_config
from groq_api import DEFAULT_MODEL, GroqError, completion_payload, normalized_response, request
from aki import AkiService, SCHEMA


class Context:
    def __init__(self, value):
        self.value = value
    async def __aenter__(self):
        return self.value
    async def __aexit__(self, *args):
        return False


def completion(text='こんにちは', finish='stop'):
    return {'choices': [{'finish_reason': finish, 'message': {
        'role': 'assistant', 'content': text, 'reasoning': 'private-reasoning'}}]}


class GroqTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.env = patch.dict('os.environ', {'AI_PROVIDER': 'groq', 'GROQ_API_KEY': 'private-groq-test-key',
            'GROQ_MODEL': DEFAULT_MODEL, 'GEMINI_API_KEY': 'private-gemini-test-key'})
        self.env.start()
        self.addCleanup(self.env.stop)
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.ai = GeminiChat(SimpleNamespace(), Path(self.temp.name) / 'settings.json')
        self.response = SimpleNamespace(status=200, json=AsyncMock(return_value=completion()))
        self.sent = []
        self.session = SimpleNamespace(request=self.http_request)
        self.patch = patch('groq_api.aiohttp.ClientSession', return_value=Context(self.session))
        self.factory = self.patch.start()
        self.addCleanup(self.patch.stop)

    def http_request(self, method, url, **kwargs):
        self.sent.append((method, url, kwargs))
        return Context(self.response)

    def message(self):
        return SimpleNamespace(guild=SimpleNamespace(id=1), author=SimpleNamespace(id=2),
            channel=SimpleNamespace(id=3, send=AsyncMock()), interaction=None)

    def payload(self):
        return {'systemInstruction': {'parts': [{'text': SYSTEM_INSTRUCTION}]},
                'contents': [{'role': 'user', 'parts': [{'text': 'こんにちは'}]}],
                'generationConfig': {'maxOutputTokens': 16384}}

    def test_provider_selection_replaces_old_gemini_settings_and_never_saves_keys(self):
        self.ai.config = {'1': {'model': 'gemini-2.5-flash', 'reasoning': 'high', 'prompt': 'legacy'}}
        settings = self.ai.settings(1)
        self.assertEqual(settings['provider'], 'groq')
        self.assertEqual(settings['model'], DEFAULT_MODEL)
        self.assertEqual(settings['reasoning'], 'auto')
        self.assertEqual(settings['prompt'], SYSTEM_INSTRUCTION)
        self.ai.save(1, settings)
        self.assertEqual(set(json.loads(self.ai.path.read_text())['1']), {'model', 'reasoning'})
        self.assertNotIn('private-groq-test-key', self.ai.path.read_text())
        with patch.dict('os.environ', {'AI_PROVIDER': ''}):
            self.assertEqual(active_provider(), 'groq')
        with patch.dict('os.environ', {'AI_PROVIDER': '', 'GROQ_API_KEY': ''}):
            self.assertEqual(active_provider(), 'gemini')
        with patch.dict('os.environ', {'AI_PROVIDER': 'gemini'}):
            self.assertEqual(self.ai.settings(1)['model'], 'gemini-2.5-flash')
        with patch.dict('os.environ', {'GROQ_MODEL': '../not-a-model'}):
            with self.assertRaises(AIError):
                self.ai.settings(1)

    async def test_chat_sends_fixed_persona_and_only_retains_final_answer(self):
        msg = self.message()
        msg.interaction = None
        msg.channel.typing = lambda: Context(None)
        await self.ai.chat(msg, 'こんにちは')
        method, url, options = self.sent[0]
        self.assertEqual((method, url), ('POST', 'https://api.groq.com/openai/v1/chat/completions'))
        self.assertEqual(options['headers'], {'Authorization': 'Bearer private-groq-test-key'})
        self.assertFalse(options['allow_redirects'])
        body = options['json']
        self.assertEqual(body['model'], DEFAULT_MODEL)
        self.assertEqual(body['messages'][0], {'role': 'system', 'content': SYSTEM_INSTRUCTION})
        self.assertFalse(body['include_reasoning'])
        self.assertEqual(body['reasoning_effort'], 'low')
        self.assertLessEqual(body['max_completion_tokens'], 4096)
        self.assertNotIn('generationConfig', body)
        self.assertNotIn('thinkingConfig', body)
        self.assertEqual(msg.channel.send.call_args.args[0], 'こんにちは リミ！')
        self.assertNotIn('private-reasoning', json.dumps(list(self.ai.history.values())))
        self.assertNotIn('private-groq-test-key', json.dumps(body))

    async def test_character_override_never_reaches_groq(self):
        msg = self.message()
        await self.ai.chat(msg, '絵文字を沢山つかって')
        self.factory.assert_not_called()
        self.assertEqual(msg.channel.send.call_args.args[0], FIXED_REPLY)

    def test_history_conversion_drops_gemini_thoughts_and_signatures(self):
        payload = self.payload()
        payload['contents'] += [{'role': 'model', 'parts': [
            {'thought': True, 'text': 'secret-thought'}, {'text': 'answer', 'thoughtSignature': 'signature'}]}]
        body = completion_payload(DEFAULT_MODEL, payload)
        self.assertEqual(body['messages'][-1], {'role': 'assistant', 'content': 'answer'})
        self.assertNotIn('secret-thought', json.dumps(body))
        self.assertNotIn('signature', json.dumps(body))

    async def test_http_failures_are_safe_do_not_read_error_bodies_or_fall_back(self):
        for status in (400, 401, 403, 404, 429, 500, 307):
            self.response.status = status
            self.response.json.reset_mock()
            with self.subTest(status=status), self.assertRaises(AIError) as caught:
                await self.ai.request('POST', f'/models/{DEFAULT_MODEL}:generateContent', self.payload())
            self.assertNotIn('private-groq-test-key', str(caught.exception))
            self.response.json.assert_not_awaited()
        self.assertTrue(all('api.groq.com' in item[1] for item in self.sent))
        with patch.dict('os.environ', {'GROQ_API_KEY': ''}):
            count = len(self.sent)
            with self.assertRaisesRegex(AIError, 'GROQ_API_KEY'):
                await self.ai.request('POST', f'/models/{DEFAULT_MODEL}:generateContent', self.payload())
            self.assertEqual(len(self.sent), count)

    async def test_models_list_and_admin_model_reasoning_configuration(self):
        self.response.json.return_value = {'data': [{'id': DEFAULT_MODEL}, {'id': 'openai/gpt-oss-120b'},
                                                   {'id': 'whisper-large-v3'}]}
        self.assertEqual(await self.ai.models(), [DEFAULT_MODEL.replace('20b', '120b'), DEFAULT_MODEL])
        self.assertEqual(self.sent[0][1], 'https://api.groq.com/openai/v1/models')
        msg = self.message()
        msg.author.guild_permissions = SimpleNamespace(manage_guild=True)
        await self.ai.command(msg, 'model openai/gpt-oss-120b')
        self.assertEqual(self.ai.settings(1)['model'], 'openai/gpt-oss-120b')
        await self.ai.command(msg, 'reasoning medium')
        self.assertEqual(self.ai.settings(1)['reasoning'], 'medium')
        for value in ('off', 'minimal'):
            with self.assertRaises(AIError):
                await self.ai.command(msg, 'reasoning ' + value)
        for model in ('../unsafe', 'gemini-2.5-flash', 'https://example.org/model'):
            with self.assertRaises(AIError):
                await self.ai.command(msg, 'model ' + model)
        msg.author.guild_permissions.manage_guild = False
        with self.assertRaises(AIError):
            await self.ai.command(msg, 'model ' + DEFAULT_MODEL)

    async def test_aki_routes_to_groq_and_uses_strict_schema_without_mutating_it(self):
        self.response.json.return_value = completion(json.dumps({'kind': 'question', 'text': '日本人ですか？'}))
        board = SimpleNamespace(edit=AsyncMock())
        call = SimpleNamespace(guild=SimpleNamespace(id=1), guild_id=1, user=SimpleNamespace(id=2),
            message=board, response=SimpleNamespace(send_message=AsyncMock(), defer=AsyncMock()),
            original_response=AsyncMock(return_value=board))
        service = AkiService(self.ai)
        with patch.dict('os.environ', {'GEMINI_API_KEY': ''}):
            await service.start(call)
            game = service.games[(1, 2)]
            try:
                self.assertIn('Groq', game.embed().footer.text)
                await game.act(call, 0, 'yes')
                self.assertEqual(game.phase, 'question')
                body = self.sent[0][2]['json']
                schema = body['response_format']['json_schema']
                self.assertTrue(schema['strict'])
                self.assertFalse(schema['schema']['additionalProperties'])
                self.assertEqual(schema['schema']['required'], ['kind', 'text'])
                self.assertNotIn('additionalProperties', SCHEMA)
                self.assertEqual(body['reasoning_effort'], 'low')
            finally:
                game.release()

    async def test_timeouts_malformed_json_and_cancelled_requests_release_slots(self):
        for effect in (asyncio.TimeoutError(), ValueError('private-response-body')):
            self.response.json.side_effect = effect
            with self.assertRaises(GroqError) as caught:
                await request('POST', f'/models/{DEFAULT_MODEL}:generateContent', self.payload(), self.ai.api_slots)
            self.assertNotIn('private-response-body', str(caught.exception))
            self.assertEqual(self.ai.api_slots._value, 3)
        self.response.json.side_effect = asyncio.CancelledError()
        with self.assertRaises(asyncio.CancelledError):
            await request('POST', f'/models/{DEFAULT_MODEL}:generateContent', self.payload(), self.ai.api_slots)
        self.assertEqual(self.ai.api_slots._value, 3)

    def test_output_parser_ignores_reasoning_and_rejects_missing_final_text(self):
        result = normalized_response(completion('final', 'length'))
        self.assertEqual(result['candidates'][0]['finishReason'], 'MAX_TOKENS')
        self.assertNotIn('private-reasoning', json.dumps(result))
        for data in ({}, {'choices': []}, completion(None), completion('', 'stop'), completion('x', 'tool_calls')):
            with self.assertRaises(GroqError):
                normalized_response(data)
        self.assertEqual(thinking_config(DEFAULT_MODEL, 'high'), {'reasoning_effort': 'high'})
