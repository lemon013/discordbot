import asyncio
import json
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'Discord-GameBot'))
from gemini_chat import AIError, GeminiChat, response_text, thinking_config, register_gemini
import discord
from discord.ext import commands


class Typing:
    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return False


def message(text, guild=1, channel=10, admin=False):
    return SimpleNamespace(
        content=text, guild=SimpleNamespace(id=guild), webhook_id=None,
        author=SimpleNamespace(id=20, bot=False, guild_permissions=SimpleNamespace(manage_guild=admin)),
        channel=SimpleNamespace(id=channel, send=AsyncMock(), typing=Typing))


class GeminiTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / 'ai_settings.json'
        self.bot = SimpleNamespace(user=SimpleNamespace(id=99), add_listener=lambda *args: None)
        self.service = GeminiChat(self.bot, self.path)

    async def execute(self, msg):
        if not msg.content.startswith('./ai '):
            await self.service.on_message(msg)
            return
        action, _, value = msg.content[5:].partition(' ')
        async def edit(content, **kwargs):
            await msg.channel.send(content)
        interaction = SimpleNamespace(
            guild=msg.guild, channel=msg.channel, channel_id=msg.channel.id, user=msg.author,
            response=SimpleNamespace(defer=AsyncMock()),
            edit_original_response=AsyncMock(side_effect=edit),
            followup=SimpleNamespace(send=AsyncMock(side_effect=edit)))
        await self.service.invoke(interaction, action, value)
        interaction.response.defer.assert_awaited_once()
        return interaction

    async def test_settings_persist_isolated_and_require_permission(self):
        msg = message('./ai prompt test')
        await self.execute(msg)
        self.assertFalse(self.path.exists())
        self.assertIn('サーバー管理', msg.channel.send.call_args.args[0])
        msg.author.guild_permissions.manage_guild = True
        msg.content = './ai prompt first\nsecond'
        await self.execute(msg)
        self.assertEqual(GeminiChat(self.bot, self.path).settings(1)['prompt'], 'first\nsecond')
        self.assertNotEqual(self.service.settings(2)['prompt'], 'first\nsecond')
        msg.content = './ai model ../bad'
        await self.execute(msg)
        self.assertEqual(json.loads(self.path.read_text(encoding='utf-8'))['1']['model'], 'gemini-2.5-flash')

    async def test_mentions_context_isolation_and_signatures(self):
        parts = [{'text': 'Internal', 'thought': True}, {'text': 'Hello', 'thoughtSignature': 'signature'}]
        self.service.request = AsyncMock(return_value={'candidates': [{'content': {'parts': parts}}]})
        first = message('<@99> first')
        await self.service.on_message(first)
        self.assertEqual(first.channel.send.call_args.args[0], 'Hello')
        self.assertEqual(self.service.request.call_args.args[2]['contents'][0]['parts'][0]['text'], 'first')
        self.service.last_used.clear()
        await self.service.on_message(message('<@99> second'))
        contents = self.service.request.call_args.args[2]['contents']
        self.assertEqual(len(contents), 3)
        self.assertEqual(contents[1]['parts'], parts)
        self.service.last_used.clear()
        await self.service.on_message(message('<@99> separate', channel=11))
        self.assertEqual(len(self.service.request.call_args.args[2]['contents']), 1)
        self.service.last_used.clear()
        self.service.history[(1, 10)] = (contents, 0)
        await self.service.on_message(message('<@!99> expired'))
        self.assertEqual(len(self.service.request.call_args.args[2]['contents']), 1)

    async def test_ignored_messages_and_dm(self):
        self.service.request = AsyncMock()
        for msg in (message('normal conversation'), message('./aichat question'), message('./ai chat old prefix')):
            await self.service.on_message(msg)
        msg = message('<@99> bot')
        msg.author.bot = True
        await self.execute(msg)
        msg = message('./ai chat DM')
        msg.guild = None
        await self.execute(msg)
        self.service.request.assert_not_awaited()

    async def test_failed_api_does_not_store_history_and_clears_busy(self):
        self.service.request = AsyncMock(side_effect=AIError('quota'))
        msg = message('./ai chat question')
        await self.execute(msg)
        self.assertFalse(self.service.history)
        self.assertFalse(self.service.busy)
        self.assertEqual(msg.channel.send.call_args.args[0], 'quota')

    async def test_no_key_and_quota_error_are_safe(self):
        with patch.dict('os.environ', {'GEMINI_API_KEY': ''}):
            with self.assertRaisesRegex(AIError, '未設定'):
                await self.service.request('POST', '/models/test:generateContent', {})
        response = SimpleNamespace(status=429)
        class ResponseContext(Typing):
            async def __aenter__(self):
                return response
        class Session(Typing):
            async def __aenter__(self):
                return self
            def request(self, *args, **kwargs):
                self.headers = kwargs['headers']
                return ResponseContext()
        with patch.dict('os.environ', {'GEMINI_API_KEY': 'secret-test-key'}), patch('gemini_chat.aiohttp.ClientSession', return_value=Session()):
            with self.assertRaisesRegex(AIError, '利用上限') as error:
                await self.service.request('POST', '/models/test:generateContent', {})
            self.assertNotIn('secret-test-key', str(error.exception))

    async def test_busy_reset_and_configuration(self):
        self.service.busy.add((1, 10))
        msg = message('./ai reset', admin=True)
        await self.execute(msg)
        self.assertIn('回答中', msg.channel.send.call_args.args[0])
        self.service.busy.clear()
        self.service.history[(1, 10)] = ([], 0)
        self.service.history[(2, 10)] = ([], 0)
        msg.content = './ai model gemini-3.5-flash-lite'
        await self.execute(msg)
        self.assertNotIn((1, 10), self.service.history)
        self.assertIn((2, 10), self.service.history)
        self.assertEqual(self.service.settings(1)['reasoning'], 'auto')

    async def test_discord_utf16_limit_and_no_pings(self):
        msg = message('')
        text = '😀' * 2000 + '@everyone'
        await self.service.send(msg, text)
        self.assertEqual(''.join(c.args[0] for c in msg.channel.send.call_args_list), text)
        for call in msg.channel.send.call_args_list:
            self.assertLessEqual(len(call.args[0].encode('utf-16-le')) // 2, 1900)
            self.assertFalse(call.kwargs['allowed_mentions'].everyone)

    def test_thinking_and_empty_output(self):
        self.assertEqual(thinking_config('gemini-2.5-flash', 'low')['thinkingConfig']['thinkingBudget'], 1024)
        self.assertEqual(thinking_config('gemini-3.5-flash-lite', 'high')['thinkingConfig']['thinkingLevel'], 'HIGH')
        self.assertEqual(thinking_config('unknown', 'auto'), {})
        for model, level in [('gemini-2.5-pro', 'off'), ('unknown', 'low'), ('gemini-3.5-flash-lite', 'off')]:
            with self.assertRaises(AIError):
                thinking_config(model, level)
        with self.assertRaisesRegex(AIError, '出力上限'):
            response_text({'candidates': [{'finishReason': 'MAX_TOKENS', 'content': {'parts': [{'thought': True, 'text': 'hidden'}]}}]})

    async def test_slash_defers_before_api_and_splits_followups(self):
        msg = message('')
        interaction = SimpleNamespace(
            guild=msg.guild, channel=msg.channel, channel_id=msg.channel.id, user=msg.author,
            response=SimpleNamespace(defer=AsyncMock()),
            edit_original_response=AsyncMock(),
            followup=SimpleNamespace(send=AsyncMock()))
        async def request(*args):
            interaction.response.defer.assert_awaited_once_with(thinking=True, ephemeral=False)
            return {'candidates': [{'content': {'parts': [{'text': 'A' * 2100}]}}]}
        self.service.request = request
        await self.service.invoke(interaction, 'chat', 'hello')
        self.assertEqual(len(interaction.edit_original_response.call_args.kwargs['content']), 1900)
        self.assertEqual(len(interaction.followup.send.call_args.args[0]), 200)
        self.assertFalse(interaction.followup.send.call_args.kwargs['ephemeral'])
        self.assertEqual(len(self.service.history[(1, 10)][0]), 2)

    async def test_slash_registration_and_choices(self):
        bot = commands.Bot(command_prefix='!', intents=discord.Intents.default())
        with patch('gemini_chat.GeminiChat', return_value=self.service):
            register_gemini(bot)
        group = bot.tree.get_command('ai')
        self.assertTrue(group.guild_only)
        self.assertEqual({command.name for command in group.commands},
                         {'chat', 'help', 'settings', 'models', 'model', 'prompt', 'reasoning', 'reset'})
        reasoning = group.get_command('reasoning')
        self.assertEqual([choice.value for choice in reasoning.parameters[0].choices],
                         ['auto', 'off', 'minimal', 'low', 'medium', 'high'])
        self.service.invoke = AsyncMock()
        interaction = SimpleNamespace()
        await group.get_command('chat').callback(interaction, 'hello')
        self.service.invoke.assert_awaited_once_with(interaction, 'chat', 'hello')


if __name__ == '__main__':
    unittest.main()
