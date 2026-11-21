import sys
from pathlib import Path
from types import MethodType, SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock, create_autospec, patch

import discord
from discord.ext import commands

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'Discord-GameBot'))
from message_apps import REPORT_RECIPIENT_ID, ReportModal, register_message_apps


def interaction(uid=123, guild=1):
    return SimpleNamespace(
        user=SimpleNamespace(id=uid), guild=SimpleNamespace(id=guild) if guild else None,
        response=SimpleNamespace(defer=AsyncMock(), send_modal=AsyncMock(), send_message=AsyncMock()),
        edit_original_response=AsyncMock())


class MessageAppsTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.bot = commands.Bot(command_prefix='!', intents=discord.Intents.default())
        self.addAsyncCleanup(self.bot.close)
        register_message_apps(self.bot)
        self.message = SimpleNamespace(id=456, guild=SimpleNamespace(id=1),
                                       jump_url='https://discord.com/channels/1/2/456', reply=AsyncMock())

    def command(self, name):
        return self.bot.tree.get_command(name, type=discord.AppCommandType.message)

    async def test_registration_and_report_form_are_private(self):
        for name in ('メッセージ通報', '高専適正度'):
            self.assertTrue(self.command(name).guild_only)
        call = interaction()
        await self.command('メッセージ通報').callback(call, self.message)
        modal = call.response.send_modal.call_args.args[0]
        self.assertIsInstance(modal, ReportModal)
        self.assertEqual(modal.reporter_id, 123)
        self.assertEqual(modal.reason.label, '通報内容')
        call.response.defer.assert_not_awaited()
        self.message.reply.assert_not_awaited()

    async def test_report_delivers_only_link_reporter_and_reason_to_exact_dm(self):
        recipient = SimpleNamespace(send=AsyncMock())
        bot = SimpleNamespace(get_user=Mock(return_value=None), fetch_user=AsyncMock(return_value=recipient))
        modal = ReportModal(bot, self.message, 123)
        modal.reason._value = '迷惑な内容 @everyone'
        call = interaction()
        await modal.on_submit(call)
        bot.fetch_user.assert_awaited_once_with(REPORT_RECIPIENT_ID)
        self.assertEqual(REPORT_RECIPIENT_ID, 799820074990698496)
        call.response.defer.assert_awaited_once_with(thinking=True, ephemeral=True)
        embed = recipient.send.call_args.kwargs['embed']
        self.assertEqual(embed.description, modal.reason.value)
        self.assertEqual(embed.fields[0].value, self.message.jump_url)
        self.assertIn('123', embed.fields[1].value)
        self.assertEqual(len(embed.fields), 2)
        self.assertFalse(recipient.send.call_args.kwargs['allowed_mentions'].everyone)
        self.assertNotIn('迷惑な内容', call.edit_original_response.call_args.kwargs['content'])
        self.message.reply.assert_not_awaited()
        await modal.on_submit(interaction())
        recipient.send.assert_awaited_once()

    async def test_report_failure_is_not_reported_as_success(self):
        recipient = SimpleNamespace(send=AsyncMock(side_effect=discord.Forbidden(
            SimpleNamespace(status=403, reason='Forbidden'), 'DM blocked')))
        modal = ReportModal(SimpleNamespace(get_user=Mock(return_value=recipient)), self.message, 123)
        modal.reason._value = 'reason'
        call = interaction()
        await modal.on_submit(call)
        self.assertIn('失敗', call.edit_original_response.call_args.kwargs['content'])
        self.assertFalse(modal.submitting)

    async def test_empty_reason_and_other_submitter_are_rejected(self):
        recipient = SimpleNamespace(send=AsyncMock())
        modal = ReportModal(SimpleNamespace(get_user=Mock(return_value=recipient)), self.message, 123)
        modal.reason._value = '  '
        await modal.on_submit(interaction())
        modal.reason._value = 'reason'
        await modal.on_submit(interaction(uid=124))
        recipient.send.assert_not_awaited()

    async def test_long_unicode_reason_fits_embed_without_losing_content(self):
        recipient = SimpleNamespace(send=AsyncMock())
        modal = ReportModal(SimpleNamespace(get_user=Mock(return_value=recipient)), self.message, 123)
        modal.reason._value = '😀' * 1500
        await modal.on_submit(interaction())
        description = recipient.send.call_args.kwargs['embed'].description
        self.assertEqual(description, '😀' * 1500)
        self.assertLessEqual(len(description.encode('utf-16-le')) // 2, 4096)

    async def test_aptitude_replies_with_random_endpoints_and_disclaimer(self):
        for score in (0, 100):
            call = interaction()
            with patch('message_apps.random.randint', return_value=score) as rng:
                await self.command('高専適正度').callback(call, self.message)
            rng.assert_called_once_with(0, 100)
            call.response.defer.assert_awaited_once_with(thinking=True, ephemeral=True)
            reply = self.message.reply.call_args
            self.assertIn(f'**{score}%**', reply.args[0])
            self.assertIn('ランダム', reply.args[0])
            self.assertIn('高専への適性を必ずお約束するものではありません', reply.args[0])
            self.assertFalse(reply.kwargs['mention_author'])
            self.assertFalse(reply.kwargs['allowed_mentions'].everyone)
            self.assertNotIn('fail_if_not_exists', reply.kwargs)

    async def test_aptitude_uses_real_reply_with_supported_send_arguments(self):
        # Exercise discord.py's forwarding code; a permissive AsyncMock hid this bug.
        channel = create_autospec(discord.abc.Messageable, instance=True)
        self.message.channel = channel
        self.message.reply = MethodType(discord.Message.reply, self.message)
        call = interaction()
        await self.command('高専適正度').callback(call, self.message)
        channel.send.assert_awaited_once()
        kwargs = channel.send.call_args.kwargs
        self.assertIs(kwargs['reference'], self.message)
        self.assertFalse(kwargs['mention_author'])
        self.assertFalse(kwargs['allowed_mentions'].everyone)
        self.assertIn('返信しました', call.edit_original_response.call_args.kwargs['content'])

    async def test_deleted_message_reply_fails_privately(self):
        self.message.reply.side_effect = discord.NotFound(SimpleNamespace(status=404, reason='Not Found'), 'deleted')
        call = interaction()
        await self.command('高専適正度').callback(call, self.message)
        self.assertIn('返信できません', call.edit_original_response.call_args.kwargs['content'])

    async def test_dm_and_cross_guild_are_rejected(self):
        for guild in (None, 2):
            call = interaction(guild=guild)
            await self.command('メッセージ通報').callback(call, self.message)
            call.response.send_modal.assert_not_awaited()
            await self.command('高専適正度').callback(call, self.message)
        self.message.reply.assert_not_awaited()


if __name__ == '__main__':
    unittest.main()
