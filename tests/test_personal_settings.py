import asyncio
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace as NS
import unittest
from unittest.mock import AsyncMock, Mock

import discord
from discord.ext import commands

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'Discord-GameBot'))
from preferences import VoiceStore, VOICE_DEFAULTS
from personal_settings import Settings, SettingsView, VoiceView, VoiceSelect, SpeedModal, parse_speed, register_personal_settings
from sound_common import SoundError


class PersonalTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.addCleanup(self.folder.cleanup)
        self.store = VoiceStore(Path(self.folder.name) / 'levels.sqlite3')
        self.store.put('user', 7, VOICE_DEFAULTS | dict(speed=1.5))
        self.reading = NS(store=self.store, preference=lambda uid: self.store.get('user', uid, VOICE_DEFAULTS),
            providers=NS(voices=AsyncMock(return_value={'3': 'ずんだもん', '2': '四国めたん'})))
        self.service = Settings(self.reading)
        self.interaction = NS(user=NS(id=7), response=NS(defer=AsyncMock(), send_message=AsyncMock(),
            send_modal=AsyncMock()), edit_original_response=AsyncMock(), followup=NS(send=AsyncMock()))
        self.bot = commands.Bot(command_prefix='!', intents=discord.Intents.none())
        self.addAsyncCleanup(self.bot.close)

    async def test_command_defers_privately_before_loading_settings(self):
        register_personal_settings(self.bot, self.reading)
        command = self.bot.tree.get_command('settings')
        self.assertTrue(command.guild_only)
        await command.callback(self.interaction)
        self.interaction.response.defer.assert_awaited_once_with(ephemeral=True, thinking=True)
        self.reading.providers.voices.assert_not_awaited()
        self.assertEqual(self.interaction.edit_original_response.call_args.kwargs['embed'].title, '⚙️ 個人設定')

    async def test_other_user_cannot_operate_view_or_submit_modal(self):
        other = self.interaction
        other.user.id = 8
        view = SettingsView(self.service, 7, self.store.personal(7))
        self.assertFalse(await view.interaction_check(other))
        modal = SpeedModal(self.service, 7)
        await modal.on_submit(other)
        self.assertEqual(self.store.get('user', 7, {})['speed'], 1.5)
        self.assertEqual(self.store.personal(8), self.store.personal(7))

    async def test_toggle_persists_and_does_not_change_other_user_or_voice(self):
        view = SettingsView(self.service, 7, self.store.personal(7))
        await view.notify_toggle.callback(self.interaction)
        reopened = VoiceStore(self.store.path)
        self.assertFalse(reopened.personal(7)['level_notifications'])
        self.assertTrue(reopened.personal(7)['reading_enabled'])
        self.assertTrue(reopened.personal(8)['level_notifications'])
        self.assertEqual(reopened.get('user', 7, {})['speed'], 1.5)

    async def test_voice_pagination_and_selection_preserve_speed(self):
        catalog = {str(i): '声' + str(i) for i in range(60)}
        view = VoiceView(self.service, 7, catalog)
        select = next(child for child in view.children if isinstance(child, VoiceSelect))
        self.assertEqual(len(select.options), 25)
        await view.next.callback(self.interaction)
        new_view = self.interaction.edit_original_response.call_args.kwargs['view']
        select = next(child for child in new_view.children if isinstance(child, VoiceSelect))
        self.assertEqual(select.options[0].value, '25')
        select._values = ['26']
        await select.callback(self.interaction)
        self.assertEqual(self.store.get('user', 7, {})['voice'], '26')
        self.assertEqual(self.store.get('user', 7, {})['speed'], 1.5)

    async def test_invalid_speed_never_writes_and_updates_merge(self):
        for value in ('nan', 'inf', '0.4', '2.1', 'abc'):
            with self.assertRaises(SoundError):
                parse_speed(value)
        await self.service.set_voice(7, {'voice': '2'})
        await self.service.set_voice(7, {'speed': parse_speed('1.2')})
        self.assertEqual(self.store.get('user', 7, {}), dict(provider='voicevox', voice='2', speed=1.2))

    async def test_speed_form_preserves_voice_and_stops_old_panel_timeout(self):
        parent = SettingsView(self.service, 7, self.store.personal(7))
        modal = SpeedModal(self.service, 7, parent)
        modal.value._value = '1.8'
        await modal.on_submit(self.interaction)
        saved = VoiceStore(self.store.path).get('user', 7, {})
        self.assertEqual(saved['voice'], '3')
        self.assertEqual(saved['speed'], 1.8)
        self.assertTrue(parent.is_finished())
        self.interaction.edit_original_response.assert_awaited_once()

    async def test_queued_reading_opt_out_prevents_external_synthesis(self):
        from voice_reading import Reading
        self.store.update('personal', 7, dict(reading_enabled=False))
        reading = NS(store=self.store, preference=Mock(), providers=NS(synthesize=AsyncMock()))
        await Reading.speak(reading, NS(), NS(author=NS(id=7)), 'private message')
        reading.providers.synthesize.assert_not_awaited()

    async def test_level_notice_off_preserves_rewards_and_only_hides_owner(self):
        from leveling import LevelStore, Leveling
        levels = Leveling(self.bot, LevelStore(self.store.path))
        self.store.update('personal', 7, dict(level_notifications=False))
        levels.jewel_store = NS(reward_levels=Mock(return_value=100))
        levels.reward_roles = AsyncMock()
        channel = NS(send=AsyncMock())
        results = [dict(user_id=uid, before=1, after=2) for uid in (7, 8)]
        await levels.notify(NS(id=1), results, dict(notifications=True, notify_channel=None), channel)
        self.assertEqual(levels.jewel_store.reward_levels.call_count, 2)
        levels.reward_roles.assert_awaited_once()
        sent = channel.send.call_args.args[0]
        self.assertNotIn('<@7>', sent)
        self.assertIn('<@8>', sent)

    async def test_expired_view_disables_buttons(self):
        view = SettingsView(self.service, 7, self.store.personal(7))
        view.message = NS(edit=AsyncMock())
        await view.on_timeout()
        self.assertTrue(all(child.disabled for child in view.children))
        view.message.edit.assert_awaited_once()
