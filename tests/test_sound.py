import asyncio
import os
from pathlib import Path
import sys
import subprocess
import tempfile
from types import SimpleNamespace
import unittest
import wave
from unittest.mock import AsyncMock, Mock, create_autospec, patch

import discord
from discord.ext import commands

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'Discord-GameBot'))
from sound import MAX_BYTES, Sound, UploadedAudio, SoundError, register_sound, verify_audio, ffmpeg_executable

REAL_FFMPEG_OPUS_AUDIO = discord.FFmpegOpusAudio


class SoundTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.service = Sound(SimpleNamespace())
        self.voice = SimpleNamespace(channel=SimpleNamespace(id=2), is_connected=Mock(return_value=True),
            is_playing=Mock(return_value=False), is_paused=Mock(return_value=False),
            play=Mock(), stop=Mock(), disconnect=AsyncMock())
        self.channel = Mock(spec=discord.VoiceChannel)
        self.channel.id = 2
        self.channel.permissions_for.return_value = SimpleNamespace(view_channel=True, connect=True, speak=True)
        self.channel.connect = AsyncMock(return_value=self.voice)
        self.guild = SimpleNamespace(id=1, me=object(), voice_client=None)
        self.call = SimpleNamespace(guild=self.guild, guild_id=1, channel=self.channel,
            user=SimpleNamespace(voice=SimpleNamespace(channel=self.channel)),
            response=SimpleNamespace(defer=AsyncMock(), send_message=AsyncMock()), edit_original_response=AsyncMock())
        self.file = SimpleNamespace(filename='test.wav', size=4)
        async def save(path):
            Path(path).write_bytes(b'test')
        self.file.save = AsyncMock(side_effect=save)
        self.inner = SimpleNamespace(read=Mock(return_value=b'packet'), is_opus=Mock(return_value=True), cleanup=Mock())
        self.patches = [patch('sound.ffmpeg_executable', return_value='ffmpeg'),
                        patch('sound.verify_audio', new=AsyncMock()),
                        patch('sound.discord.FFmpegOpusAudio', return_value=self.inner)]
        for item in self.patches:
            item.start()
            self.addCleanup(item.stop)
        self.addAsyncCleanup(self.cleanup)

    async def cleanup(self):
        for source in list(self.service.playing.values()):
            source.cleanup()
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        tasks = list(self.service.idle_tasks.values())
        tasks += list(self.service.connecting.values())
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)

    async def test_play_attachment_joins_and_encodes_to_opus(self):
        await self.service.invoke(self.call, 'play', self.file)
        self.call.response.defer.assert_not_awaited()
        self.call.response.send_message.assert_awaited_once()
        self.assertTrue(self.call.response.send_message.call_args.kwargs['ephemeral'])
        self.channel.connect.assert_awaited_once_with(timeout=20, reconnect=False, self_deaf=True)
        source = self.voice.play.call_args.args[0]
        self.assertIsInstance(source, UploadedAudio)
        self.assertTrue(source.is_opus())
        self.assertEqual(source.read(), b'packet')
        directory = Path(source.directory.name)
        self.assertTrue(directory.exists())
        self.inner.cleanup.assert_not_called()
        source.cleanup()
        source.cleanup()
        self.assertFalse(directory.exists())
        self.inner.cleanup.assert_called_once()
        self.assertIn('再生しています', self.call.edit_original_response.call_args.kwargs['content'])

    async def test_text_channel_and_users_outside_voice_are_rejected(self):
        self.call.channel = SimpleNamespace(id=2)
        await self.service.invoke(self.call, 'play', self.file)
        self.assertIn('VC内のチャット', self.call.edit_original_response.call_args.kwargs['content'])
        self.call.channel = self.channel
        self.call.user.voice = None
        await self.service.invoke(self.call, 'play', self.file)
        self.assertIn('参加してから', self.call.edit_original_response.call_args.kwargs['content'])
        self.file.save.assert_not_awaited()

    async def test_file_type_size_and_permissions_are_checked_before_download(self):
        for filename, size in (('bad.exe',1), ('bad.mp3',MAX_BYTES+1), ('empty.wav',0)):
            self.file.filename, self.file.size = filename, size
            await self.service.invoke(self.call, 'play', self.file)
        self.file.filename, self.file.size = 'fine.mp3', 1
        self.channel.permissions_for.return_value.speak = False
        await self.service.invoke(self.call, 'play', self.file)
        self.file.save.assert_not_awaited()
        self.channel.connect.assert_not_awaited()

    async def test_other_voice_and_active_playback_are_not_interrupted(self):
        self.guild.voice_client = self.voice
        self.voice.channel.id = 3
        await self.service.invoke(self.call, 'play', self.file)
        self.assertIn('別のVC', self.call.edit_original_response.call_args.kwargs['content'])
        self.voice.channel.id = 2
        self.voice.is_playing.return_value = True
        await self.service.invoke(self.call, 'play', self.file)
        self.assertIn('再生中', self.call.edit_original_response.call_args.kwargs['content'])
        self.file.save.assert_not_awaited()
        self.voice.stop.assert_not_called()

    async def test_failed_download_cleans_temp_and_does_not_join(self):
        directories = []
        async def save(path):
            directories.append(Path(path).parent)
            Path(path).write_bytes(b'partial')
            raise discord.HTTPException(SimpleNamespace(status=500, reason='bad'), 'download error')
        self.file.save.side_effect = save
        await self.service.invoke(self.call, 'play', self.file)
        self.assertFalse(directories[0].exists())
        self.channel.connect.assert_not_awaited()
        self.assertFalse(self.service.playing)

    async def test_membership_rechecked_after_download(self):
        async def save(path):
            Path(path).write_bytes(b'test')
            self.call.user.voice = None
        self.file.save.side_effect = save
        await self.service.invoke(self.call, 'play', self.file)
        self.channel.connect.assert_not_awaited()
        self.assertIn('参加してから', self.call.edit_original_response.call_args.kwargs['content'])

    async def test_connect_timeout_cleans_failed_voice_connection(self):
        async def connect(**kwargs):
            self.guild.voice_client = self.voice
            raise asyncio.TimeoutError()
        self.channel.connect.side_effect = connect
        await self.service.invoke(self.call, 'play', self.file)
        await asyncio.gather(*list(self.service.connecting.values()), return_exceptions=True)
        self.voice.disconnect.assert_awaited_once_with(force=True)
        self.assertFalse(self.service.playing)
        self.assertIn('タイムアウト', self.call.edit_original_response.call_args.kwargs['content'])

    async def test_timeout_reply_does_not_wait_for_slow_sdk_cleanup(self):
        cleanup_gate = asyncio.Event()
        async def connect(**kwargs):
            self.guild.voice_client = self.voice
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                await cleanup_gate.wait()
                raise
        self.channel.connect.side_effect = connect
        try:
            with patch('sound.CONNECT_TIMEOUT', 0.01):
                await asyncio.wait_for(self.service.invoke(self.call, 'play', self.file), timeout=2)
            self.assertIn('タイムアウト', self.call.edit_original_response.call_args.kwargs['content'])
            self.assertIn(1, self.service.connecting)
            self.voice.disconnect.assert_not_awaited()
            await self.service.invoke(self.call, 'play', self.file)
            self.assertIn('終了処理中', self.call.edit_original_response.call_args.kwargs['content'])
            self.channel.connect.assert_awaited_once()
        finally:
            cleanup_gate.set()
            await asyncio.gather(*list(self.service.connecting.values()), return_exceptions=True)
        self.assertFalse(self.service.connecting)

    async def test_initial_status_precedes_download_and_old_idle_timer_is_cancelled(self):
        self.guild.voice_client = self.voice
        old_timer = asyncio.create_task(self.service.idle_disconnect(self.guild))
        self.service.idle_tasks[1] = old_timer
        async def save(path):
            self.call.response.send_message.assert_awaited_once()
            self.call.response.defer.assert_not_awaited()
            self.assertIn(1, self.service.preparing)
            self.assertNotIn(1, self.service.idle_tasks)
            Path(path).write_bytes(b'test')
        self.file.save.side_effect = save
        await self.service.invoke(self.call, 'play', self.file)
        await asyncio.gather(old_timer, return_exceptions=True)
        self.voice.disconnect.assert_not_awaited()
        self.assertFalse(self.service.preparing)

    async def test_busy_sound_command_replies_instead_of_waiting_on_guild_lock(self):
        async with self.service.lock(1):
            await asyncio.wait_for(self.service.invoke(self.call, 'play', self.file), timeout=1)
        self.assertIn('準備・操作中', self.call.edit_original_response.call_args.kwargs['content'])
        self.file.save.assert_not_awaited()

    async def test_play_failure_releases_file_source_and_new_voice_connection(self):
        self.voice.play.side_effect = RuntimeError('cannot play')
        await self.service.invoke(self.call, 'play', self.file)
        await asyncio.gather(*list(self.service.connecting.values()), return_exceptions=True)
        self.inner.cleanup.assert_called_once()
        self.voice.disconnect.assert_awaited_once_with(force=True)
        self.assertFalse(self.service.playing)
        source = self.voice.play.call_args.args[0]
        self.assertFalse(Path(source.directory.name).exists())

    async def test_stop_leave_and_auto_disconnect(self):
        self.guild.voice_client = self.voice
        await self.service.invoke(self.call, 'stop')
        self.voice.stop.assert_called_once()
        self.assertIn(1, self.service.idle_tasks)
        await self.service.invoke(self.call, 'leave')
        self.voice.disconnect.assert_awaited_once_with(force=True)
        self.assertNotIn(1, self.service.idle_tasks)
        self.voice.disconnect.reset_mock()
        with patch('sound.asyncio.sleep', new=AsyncMock()):
            await self.service.idle_disconnect(self.guild)
        self.voice.disconnect.assert_awaited_once_with(force=True)

    async def test_stale_finished_callback_cannot_clear_new_playback(self):
        current = object()
        self.service.playing[1] = current
        await self.service.finished(self.guild, object())
        self.assertIs(self.service.playing.pop(1), current)
        self.assertFalse(self.service.idle_tasks)

    async def test_voice_play_arguments_match_installed_discord_api(self):
        self.voice.play = create_autospec(discord.VoiceClient.play, instance=False)
        # Bind self as a real method to enforce the SDK signature.
        from types import MethodType
        self.voice.play = MethodType(self.voice.play, self.voice)
        await self.service.invoke(self.call, 'play', self.file)
        self.assertIn('再生しています', self.call.edit_original_response.call_args.kwargs['content'])

    async def test_registration_uses_attachment_and_guild_only_group(self):
        bot = commands.Bot(command_prefix='!', intents=discord.Intents.default())
        self.addAsyncCleanup(bot.close)
        service = register_sound(bot)
        group = bot.tree.get_command('sound')
        self.assertTrue(group.guild_only)
        self.assertEqual({c.name for c in group.commands}, {'play','stop','leave'})
        self.assertEqual(group.get_command('play').parameters[0].type, discord.AppCommandOptionType.attachment)
        service.invoke = AsyncMock()
        await group.get_command('play').callback(self.call, self.file)
        service.invoke.assert_awaited_once_with(self.call, 'play', self.file)

    async def test_real_wav_is_converted_to_opus_and_temp_file_is_removed(self):
        try:
            ffmpeg_executable()
        except SoundError:
            self.skipTest('Install voice and FFmpeg requirements for the decoder integration test')
        async def save_wave(path):
            with wave.open(str(path), 'wb') as audio:
                audio.setnchannels(1)
                audio.setsampwidth(2)
                audio.setframerate(48000)
                audio.writeframes(b'\x00\x00' * 9600)
        self.file.save.side_effect = save_wave
        with patch('sound.ffmpeg_executable', new=ffmpeg_executable), \
             patch('sound.verify_audio', new=verify_audio), \
             patch('sound.discord.FFmpegOpusAudio', new=REAL_FFMPEG_OPUS_AUDIO):
            await self.service.invoke(self.call, 'play', self.file)
        self.assertIn('再生しています', self.call.edit_original_response.call_args.kwargs['content'])
        source = self.service.playing[1]
        path = Path(source.directory.name)
        def read_all():
            return list(iter(source.read, b''))
        try:
            packets = await asyncio.wait_for(asyncio.to_thread(read_all), timeout=15)
            self.assertTrue(packets)
            self.assertTrue(source.is_opus())
        finally:
            source.cleanup()
        self.assertFalse(path.exists())

    async def make_mp4(self, path, audio):
        try:
            executable = ffmpeg_executable()
        except SoundError:
            self.skipTest('Install voice and FFmpeg requirements for MP4 integration tests')
        args = [executable, '-nostdin', '-y', '-v', 'error', '-f', 'lavfi',
                '-i', 'color=c=black:s=16x16:r=10']
        if audio:
            args += ['-f', 'lavfi', '-i', 'anullsrc=r=48000:cl=mono']
        args += ['-t', '0.2', '-c:v', 'mpeg4']
        args += ['-c:a', 'aac'] if audio else ['-an']
        process = await asyncio.create_subprocess_exec(*args, str(path), stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL, **({'creationflags': subprocess.CREATE_NO_WINDOW} if os.name == 'nt' else {}))
        self.assertEqual(await asyncio.wait_for(process.wait(), timeout=15), 0)

    async def test_real_mp4_plays_audio_only(self):
        self.file.filename = 'clip.MP4'
        async def save(path):
            await self.make_mp4(path, audio=True)
        self.file.save.side_effect = save
        with patch('sound.ffmpeg_executable', new=ffmpeg_executable), \
             patch('sound.verify_audio', new=verify_audio), \
             patch('sound.discord.FFmpegOpusAudio', new=REAL_FFMPEG_OPUS_AUDIO):
            await self.service.invoke(self.call, 'play', self.file)
        self.assertIn('再生しています', self.call.edit_original_response.call_args.kwargs['content'])
        source = self.service.playing[1]
        try:
            packets = await asyncio.wait_for(asyncio.to_thread(lambda: list(iter(source.read, b''))), timeout=15)
            self.assertTrue(packets)
            self.assertTrue(source.is_opus())
        finally:
            source.cleanup()

    async def test_mp4_without_audio_is_rejected_before_joining(self):
        self.file.filename = 'silent.mp4'
        paths = []
        async def save(path):
            paths.append(Path(path).parent)
            await self.make_mp4(path, audio=False)
        self.file.save.side_effect = save
        with patch('sound.ffmpeg_executable', new=ffmpeg_executable), \
             patch('sound.verify_audio', new=verify_audio):
            await self.service.invoke(self.call, 'play', self.file)
        self.channel.connect.assert_not_awaited()
        self.assertIn('音声を読み取れません', self.call.edit_original_response.call_args.kwargs['content'])
        self.assertFalse(paths[0].exists())


class ProbeTests(unittest.IsolatedAsyncioTestCase):
    async def test_rejects_missing_audio_and_kills_timed_out_process(self):
        process = SimpleNamespace(wait=AsyncMock(return_value=1), returncode=1, kill=Mock())
        with patch('sound.asyncio.create_subprocess_exec', new=AsyncMock(return_value=process)):
            with self.assertRaises(SoundError):
                await verify_audio('ffmpeg', 'file.wav')
        process.returncode = None
        process.wait = AsyncMock(side_effect=[asyncio.TimeoutError(), 0])
        with patch('sound.asyncio.create_subprocess_exec', new=AsyncMock(return_value=process)):
            with self.assertRaises(asyncio.TimeoutError):
                await verify_audio('ffmpeg', 'file.wav')
        process.kill.assert_called_once()
