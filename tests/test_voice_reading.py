import asyncio
from array import array
import base64
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace as NS
import unittest
from unittest.mock import AsyncMock, Mock, patch

import discord
from discord.ext import commands

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'Discord-GameBot'))
from sound import Sound, SoundError, make_test_tone
from voice_mixer import Mixer, VoiceHub
from voice_reading import DEFAULT, VoiceStore, Providers, Reading, reading_text, register_voice, CREDIT


class Tone:
    playback_error = None
    def __init__(self, value, frames=10):
        self.value, self.frames, self.cleaned = value, frames, False
    def read(self):
        self.frames -= 1
        return array('h', [self.value] * 1920).tobytes() if self.frames >= 0 else b''
    def cleanup(self):
        self.cleaned = True


class MixTests(unittest.TestCase):
    def setUp(self):
        self.config = DEFAULT | dict(music_volume=100, tts_volume=100, board_volume=100, duck=False)
        self.mixer = Mixer(lambda: self.config)
    def sample(self):
        return array('h', self.mixer.read())[0]
    def test_three_lanes_mix_and_clip_instead_of_wrapping(self):
        for key in ('music', 'tts', 'board:1'):
            self.mixer.add(key, Tone(20000))
        self.assertEqual(self.sample(), 32767)
        self.config.update(music_volume=10, tts_volume=10, board_volume=10)
        self.assertEqual(self.sample(), 6000)
        self.mixer.cleanup()
        self.assertEqual(self.mixer.read(), b'')
    def test_ducking_only_changes_music_and_restores_after_speech(self):
        self.config.update(duck=True, duck_volume=25)
        self.mixer.add('music', Tone(1000))
        self.mixer.add('tts', Tone(1000, frames=1))
        self.assertEqual(self.sample(), 1250)
        self.sample()  # source reaches EOF
        self.assertEqual(self.sample(), 1000)
    def test_pausing_music_keeps_board_and_tts_running(self):
        music = Tone(1000)
        self.mixer.add('music', music)
        self.mixer.add('tts', Tone(2000))
        self.mixer.add('board:1', Tone(3000))
        self.mixer.paused = True
        self.assertEqual(self.sample(), 5000)
        self.assertEqual(music.frames, 10)
        self.mixer.stop('music')
        self.assertTrue(music.cleaned)
        self.assertEqual(self.sample(), 5000)
    def test_board_limit_and_failed_source_cleanup(self):
        for i in range(3):
            self.mixer.add(f'board:{i}', Tone(1))
        with self.assertRaises(SoundError):
            self.mixer.add('board:4', Tone(1))
        bad = Tone(1)
        bad.read = Mock(side_effect=RuntimeError('private'))
        self.mixer.add('tts', bad)
        self.sample()
        self.assertEqual(bad.playback_error, 'RuntimeError')
        self.assertTrue(bad.cleaned)

    def test_ffmpeg_failure_at_eof_is_reported_to_music_repeat_logic(self):
        bad = Tone(1, frames=0)
        bad._current_error = RuntimeError('private diagnostic')
        self.mixer.add('music', bad)
        self.sample()
        self.assertEqual(bad.playback_error, 'RuntimeError')
        self.assertTrue(bad.cleaned)


class ReadingTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.addCleanup(self.folder.cleanup)
        self.store = VoiceStore(Path(self.folder.name) / 'levels.sqlite3')
        self.bot = commands.Bot(command_prefix='!', intents=discord.Intents.none())
        self.bot._connection.user = NS(id=99)
        self.addAsyncCleanup(self.bot.close)
        self.sound = Sound(self.bot)
        self.providers = Providers()
        self.providers.base = 'http://voicevox.invalid'
        self.providers.voices = AsyncMock(return_value={'3': 'ずんだもん'})
        self.providers.synthesize = AsyncMock(return_value=b'wav')
        self.service = Reading(self.bot, self.sound, self.store, self.providers)
        self.channel = Mock(spec=discord.VoiceChannel)
        self.channel.id = 10
        self.channel.permissions_for.return_value = NS(view_channel=True, connect=True, speak=True)
        self.channel.send = AsyncMock()
        self.guild = NS(id=1, me=object(), voice_client=NS(channel=self.channel, is_connected=lambda: True))
        self.user = NS(id=2, bot=False, display_name='User', voice=NS(channel=self.channel), guild_permissions=NS(manage_guild=True))
        self.message = NS(id=3, guild=self.guild, channel=self.channel, author=self.user,
                          content='Hello', clean_content='Hello', attachments=[])
        self.interaction = NS(guild=self.guild, guild_id=1, channel=self.channel, user=self.user,
            response=NS(defer=AsyncMock()), edit_original_response=AsyncMock())
        self.addAsyncCleanup(self.cleanup)
    async def cleanup(self):
        await self.service.halt(1)
        self.service.hub.close(1)
        self.sound.cancel_idle(1)
        await asyncio.sleep(0)
    async def settle(self):
        for _ in range(20):
            await asyncio.sleep(0)
    def test_storage_persists_settings_without_overwriting_other_scopes(self):
        self.store.put('user', 2, dict(provider='google', voice='ja-JP-Standard-A', speed=1.5))
        self.store.put('guild', 1, dict(music_volume=25))
        other = VoiceStore(self.store.path)
        self.assertEqual(other.get('user', 2, {})['speed'], 1.5)
        self.assertEqual(other.get('guild', 1, {})['music_volume'], 25)
        self.assertEqual(other.get('user', 7, {'speed': 1}), {'speed': 1})
    def test_text_filters_and_dictionary_do_not_cascade_or_read_spoilers(self):
        self.message.clean_content = 'abc https://example.org 😀 <:smile:123> ||secret|| ```code```'
        result = reading_text(self.message, DEFAULT, {'abc': 'def', 'def': 'bad'})
        self.assertIn('def', result)
        self.assertNotIn('bad', result)
        for forbidden in ('secret', 'example.org', '😀', '<:smile', '```'):
            self.assertNotIn(forbidden, result)
        self.message.clean_content = ''
        self.assertEqual(reading_text(self.message, DEFAULT, {}), '')
    async def test_membership_and_admin_permissions_are_enforced(self):
        self.user.voice = None
        with self.assertRaises(SoundError):
            self.service.channel(self.interaction)
        self.user.guild_permissions.manage_guild = False
        with self.assertRaises(SoundError):
            self.service.admin(self.interaction)
    async def test_messages_are_opt_in_and_outsiders_and_self_are_ignored(self):
        self.service.speak = AsyncMock()
        await self.service.on_message(self.message)
        self.service.speak.assert_not_awaited()
        self.service.sessions[1] = 10
        self.user.voice = None
        await self.service.on_message(self.message)
        self.user.voice = NS(channel=self.channel)
        self.user.id = 99
        await self.service.on_message(self.message)
        self.service.speak.assert_not_awaited()
    async def test_reading_is_fifo_skip_can_cancel_current_and_halt_clears_pending(self):
        self.service.sessions[1] = 10
        entered = asyncio.Event()
        async def blocked(*args):
            entered.set()
            await asyncio.Event().wait()
        self.service.speak = AsyncMock(side_effect=blocked)
        await self.service.on_message(self.message)
        await entered.wait()
        await self.service.on_message(self.message)
        self.assertEqual(self.service.jobs[1].qsize(), 1)
        self.service.current[1].cancel()
        await self.settle()
        self.assertEqual(self.service.speak.await_count, 2)
        await self.service.halt(1)
        self.assertNotIn(1, self.service.jobs)
        self.assertNotIn(1, self.service.sessions)
    async def test_change_and_volume_commands_save_and_show_credit(self):
        with patch('voice_reading.VoiceStore', return_value=self.store), patch('voice_reading.Providers', return_value=self.providers):
            service = register_voice(self.bot, self.sound)
        change = self.bot.tree.get_command('voice').get_command('change')
        await change.callback(self.interaction, 'voicevox', '3', 1.5)
        self.assertEqual(self.store.get('user', 2, {})['speed'], 1.5)
        self.assertIn(CREDIT, self.interaction.edit_original_response.call_args.kwargs['content'])
        command = self.bot.tree.get_command('voice').get_command('volume')
        self.user.guild_permissions.manage_guild = False
        await command.callback(self.interaction, 1, 2, 3)
        self.assertIn('権限', self.interaction.edit_original_response.call_args.kwargs['content'])
        self.user.guild_permissions.manage_guild = True
        await command.callback(self.interaction, 10, 20, 30)
        self.assertEqual(service.config(1)['tts_volume'], 20)
        self.assertEqual(self.store.get('guild', 1, {})['board_volume'], 30)
    async def test_real_ffmpeg_pcm_is_mixed_and_temporary_audio_is_released(self):
        voice = NS(channel=self.channel, is_connected=lambda: True, is_playing=lambda: False,
                   play=Mock())
        self.guild.voice_client = voice
        tone = make_test_tone()
        event, key = await self.service.hub.play(self.interaction, self.channel, tone, 'board')
        mixer = self.service.hub.mixers[1]
        source = mixer.sources[key]
        path = Path(source.directory.name)
        frame = await asyncio.to_thread(mixer.read)
        self.assertEqual(len(frame), 3840)
        self.assertTrue(any(frame))
        mixer.stop(key)
        await self.settle()
        self.assertTrue(event.is_set())
        self.assertFalse(path.exists())

    async def test_hub_music_controls_do_not_stop_overlapping_soundboard(self):
        running = []
        def play(source):
            running.append(source)
        self.guild.voice_client = NS(channel=self.channel, is_connected=lambda: True,
                                    is_playing=lambda: bool(running), play=play)
        await self.sound.play(self.interaction, self.channel, make_test_tone())
        _, board_key = await self.service.hub.play(self.interaction, self.channel, make_test_tone(), 'board')
        mixer = self.service.hub.mixers[1]
        self.assertEqual(len(running), 1)
        control = self.sound.music_voice(self.guild)
        control.pause()
        self.assertTrue(control.is_paused())
        frame = await asyncio.to_thread(mixer.read)
        self.assertTrue(any(frame))
        control.resume()
        self.assertTrue(control.is_playing())
        control.stop()
        await self.settle()
        self.assertNotIn(1, self.sound.playing)
        self.assertIn(board_key, mixer.sources)
        self.assertTrue(any(await asyncio.to_thread(mixer.read)))

    async def test_leaving_during_preparation_prevents_late_voice_reconnection(self):
        entered = asyncio.Event()
        proceed = asyncio.Event()
        async def save(path):
            entered.set()
            await proceed.wait()
            await make_test_tone().save(path)
        clip = NS(filename='clip.wav', size=1, save=save)
        self.sound.connect_voice = AsyncMock()
        pending = asyncio.create_task(self.service.hub.play(self.interaction, self.channel, clip, 'board'))
        await entered.wait()
        self.service.hub.close(1)
        self.guild.voice_client = None
        proceed.set()
        with self.assertRaises(SoundError):
            await pending
        self.sound.connect_voice.assert_not_awaited()


class ProviderTests(unittest.IsolatedAsyncioTestCase):
    async def test_voicevox_catalog_is_limited_to_reviewed_characters_and_speed_is_sent(self):
        provider = Providers()
        provider.base = 'http://engine.invalid'
        provider.request = AsyncMock(side_effect=[
            br'[{"name":"\u305a\u3093\u3060\u3082\u3093","styles":[{"id":3,"name":"normal"}]},{"name":"other","styles":[{"id":5,"name":"normal"}]}]',
            b'{"speedScale":1}', b'wav'])
        audio = await provider.synthesize('hello', dict(provider='voicevox', voice='3', speed=1.7))
        self.assertEqual(audio, b'wav')
        self.assertEqual(provider.cache['voicevox'][1], {'3': 'ずんだもん（normal）'})
        self.assertEqual(provider.request.call_args.kwargs['json']['speedScale'], 1.7)
    async def test_google_request_is_plain_text_and_uses_selected_voice_and_speed(self):
        provider = Providers()
        provider.headers = AsyncMock(return_value={'Authorization': 'Bearer private'})
        provider.voices = AsyncMock(return_value={'ja-JP-Standard-A': 'voice'})
        provider.request = AsyncMock(return_value=b'{"audioContent":"' + base64.b64encode(b'wave') + b'"}')
        result = await provider.synthesize('<hello>', dict(provider='google', voice='ja-JP-Standard-A', speed=1.2))
        self.assertEqual(result, b'wave')
        body = provider.request.call_args.kwargs['json']
        self.assertEqual(body['input'], {'text': '<hello>'})
        self.assertEqual(body['audioConfig']['speakingRate'], 1.2)
        self.assertEqual(body['voice']['name'], 'ja-JP-Standard-A')
