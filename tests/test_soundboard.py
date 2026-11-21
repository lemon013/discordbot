import array
import asyncio
import os
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock, patch

import discord
from discord.ext import commands

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'Discord-GameBot'))
from sound import Sound, SoundError, ffmpeg_executable, verify_audio
from soundboard import ASSET_DIR, CLIPS, SoundboardView, register_soundboard


class SoundboardTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.bot = commands.Bot(command_prefix='!', intents=discord.Intents.none())
        self.service = Sound(self.bot)
        register_soundboard(self.bot, self.service)
        self.channel = Mock(spec=discord.VoiceChannel)
        self.channel.id = 2
        self.guild = SimpleNamespace(id=1, voice_client=None)
        self.user = SimpleNamespace(bot=False, voice=SimpleNamespace(channel=self.channel))
        self.call = SimpleNamespace(guild=self.guild, guild_id=1, channel=self.channel,
            user=self.user, response=SimpleNamespace(send_message=AsyncMock()),
            edit_original_response=AsyncMock())
        self.message = SimpleNamespace(guild=self.guild, channel=self.channel, author=self.user,
            content='./soundboard', reply=AsyncMock())
        self.listener = self.bot.extra_events['on_message'][0]
        self.addAsyncCleanup(self.bot.close)

    async def test_prefix_and_slash_publish_shared_panels_without_changing_prefix(self):
        await self.listener(self.message)
        panel = self.message.reply.call_args.kwargs
        self.assertEqual(len(panel['view'].children), 8)
        self.assertIsNone(panel.get('ephemeral'))
        self.assertFalse(panel['mention_author'])
        self.assertEqual(self.bot.command_prefix, '!')
        self.message.content = './soundboard extra'
        await self.listener(self.message)
        self.assertEqual(self.message.reply.await_count, 1)
        await self.bot.tree.get_command('soundboard').callback(self.call)
        panel = self.call.response.send_message.call_args.kwargs
        self.assertEqual(len(panel['view'].children), 8)
        self.assertIsNone(panel.get('ephemeral'))

    async def test_panels_and_clicks_require_membership_in_the_panel_voice_channel(self):
        self.user.voice = None
        await self.listener(self.message)
        self.assertNotIn('view', self.message.reply.call_args.kwargs)
        await self.bot.tree.get_command('soundboard').callback(self.call)
        self.assertTrue(self.call.response.send_message.call_args.kwargs['ephemeral'])
        with patch.object(self.service, 'play', new=AsyncMock()) as play:
            await SoundboardView(self.service).children[0].callback(self.call)
            play.assert_not_awaited()
            self.call.edit_original_response.assert_awaited_once()
        self.call.user.voice = SimpleNamespace(channel=SimpleNamespace(id=999))
        with patch.object(self.service, 'play', new=AsyncMock()) as play:
            await SoundboardView(self.service).children[0].callback(self.call)
            play.assert_not_awaited()

    async def test_all_buttons_deliver_the_exact_packaged_file_to_the_existing_player(self):
        self.service.invoke = AsyncMock()
        with tempfile.TemporaryDirectory() as directory:
            for button, (key, label) in zip(SoundboardView(self.service).children, CLIPS):
                await button.callback(self.call)
                interaction, action, clip = self.service.invoke.call_args.args
                self.assertIs(interaction, self.call)
                self.assertEqual(action, 'play')
                self.assertEqual(clip.filename, label + '.mp3')
                destination = Path(directory) / 'clip.mp3'
                await clip.save(destination)
                self.assertEqual(destination.read_bytes(), (ASSET_DIR / f'{key}.mp3').read_bytes())
                self.assertEqual(clip.size, destination.stat().st_size)

    async def test_registered_panel_survives_restart_with_the_same_button_ids(self):
        registered = self.bot.persistent_views[0]
        restarted = SoundboardView(Sound(self.bot))
        self.assertTrue(registered.is_persistent())
        ids = [button.custom_id for button in registered.children]
        self.assertEqual(ids, [button.custom_id for button in restarted.children])
        self.assertEqual(len(set(ids)), 8)
        self.assertTrue(all(button.service is self.service for button in registered.children))

    async def test_missing_asset_is_reported_without_starting_voice(self):
        button = SoundboardView(self.service).children[0]
        button.path = ASSET_DIR / 'missing.mp3'
        self.service.invoke = AsyncMock()
        await button.callback(self.call)
        self.service.invoke.assert_not_awaited()
        self.assertTrue(self.call.response.send_message.call_args.kwargs['ephemeral'])

    async def test_text_channels_bots_and_dms_cannot_generate_a_panel(self):
        self.message.channel = SimpleNamespace(id=10)
        await self.listener(self.message)
        self.assertNotIn('view', self.message.reply.call_args.kwargs)
        self.message.reply.reset_mock()
        self.message.author.bot = True
        await self.listener(self.message)
        self.message.author.bot = False
        self.message.guild = None
        await self.listener(self.message)
        self.message.reply.assert_not_awaited()

    async def test_busy_player_is_shared_with_sound_commands_and_does_not_queue(self):
        self.service.play = AsyncMock()
        async with self.service.lock(self.guild.id):
            await SoundboardView(self.service).children[0].callback(self.call)
        self.service.play.assert_not_awaited()
        self.call.edit_original_response.assert_awaited_once()


class PackagedAudioTests(unittest.IsolatedAsyncioTestCase):
    async def test_all_eight_mp3s_encode_to_audible_20ms_opus(self):
        try:
            executable = ffmpeg_executable()
        except SoundError:
            self.skipTest('Install voice and FFmpeg requirements for audio integration tests')
        for key, _ in CLIPS:
            with self.subTest(clip=key):
                path = ASSET_DIR / f'{key}.mp3'
                self.assertGreater(path.stat().st_size, 0)
                await verify_audio(executable, path)
                with open(os.devnull, 'wb') as errors:
                    source = discord.FFmpegOpusAudio(str(path), executable=executable, bitrate=128,
                        before_options='-nostdin -protocol_whitelist file,pipe',
                        options='-vn -map 0:a:0 -frame_duration 20 -application audio', stderr=errors)
                    try:
                        packets = await asyncio.wait_for(
                            asyncio.to_thread(lambda: list(iter(source.read, b''))), timeout=15)
                        packets = [p for p in packets if not p.startswith((b'OpusHead', b'OpusTags'))]
                        self.assertTrue(packets)
                        try:
                            decoder = discord.opus.Decoder()
                        except discord.opus.OpusNotLoaded:
                            self.skipTest('Native Opus decoder unavailable for amplitude verification')
                        samples = array.array('h', b''.join(decoder.decode(p, fec=False) for p in packets))
                        self.assertGreater(max(abs(value) for value in samples), 500)
                        self.assertTrue(all(decoder.packet_get_nb_frames(p)
                            * decoder.packet_get_samples_per_frame(p) == 960 for p in packets))
                    finally:
                        source.cleanup()
