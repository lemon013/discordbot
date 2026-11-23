import asyncio
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock, patch

import discord
from discord.ext import commands

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'Discord-GameBot'))
from sound import MAX_BYTES, SoundError
from youtube_play import YouTubeAudio, normalize_url, register_youtube_play, kill_download
import youtube_worker

URL = 'https://www.youtube.com/watch?v=BaW_jenozKc'


class UrlTests(unittest.TestCase):
    def test_supported_links_are_reduced_to_a_single_canonical_video(self):
        for url in (URL, URL + '&list=PLtest&index=4', 'https://youtu.be/BaW_jenozKc?t=15',
                    'https://music.youtube.com/watch?v=BaW_jenozKc', 'https://m.youtube.com/watch?v=BaW_jenozKc',
                    'https://www.youtube.com/shorts/BaW_jenozKc', '<https://youtu.be/BaW_jenozKc>'):
            self.assertEqual(normalize_url(url), URL)

    def test_arbitrary_hosts_credentials_ports_playlists_and_invalid_ids_are_rejected(self):
        for url in ('http://youtu.be/BaW_jenozKc', 'file:///etc/passwd', 'https://localhost/video',
                    'https://127.0.0.1/', 'https://youtube.com.example.org/watch?v=BaW_jenozKc',
                    'https://youtube.com@localhost/watch?v=BaW_jenozKc',
                    'https://user:pass@youtube.com/watch?v=BaW_jenozKc',
                    'https://youtube.com:443/watch?v=BaW_jenozKc',
                    'https://www.youtube.com/playlist?list=abc', 'https://youtu.be/short',
                    URL + '&v=abcdefghijk', 'https://youtu.be/BaW_jenozKc/extra', 'x' * 2049):
            with self.subTest(url=url), self.assertRaises(SoundError):
                normalize_url(url)


class DownloadTests(unittest.IsolatedAsyncioTestCase):
    async def test_slash_registration_and_shared_sound_service(self):
        bot = commands.Bot(command_prefix='!', intents=discord.Intents.none())
        sound = SimpleNamespace(invoke=AsyncMock())
        music = register_youtube_play(bot, sound)
        music.enqueue = AsyncMock()
        command = bot.tree.get_command('play')
        self.assertTrue(command.guild_only)
        self.assertEqual(command.parameters[0].name, 'url')
        call = SimpleNamespace(response=SimpleNamespace(send_message=AsyncMock()))
        await command.callback(call, URL)
        music.enqueue.assert_awaited_once_with(call, URL)
        self.assertIs(sound.music, music)
        self.assertIsNotNone(bot.tree.get_command('music'))
        await bot.close()

    async def test_download_updates_title_and_size_and_keeps_secrets_out_of_worker_environment(self):
        with tempfile.TemporaryDirectory() as folder:
            target = Path(folder) / 'audio.webm'
            async def communicate():
                target.write_bytes(b'audio')
                return json.dumps({'ok': True, 'title': '@everyone Song\nname'}).encode(), None
            process = SimpleNamespace(communicate=AsyncMock(side_effect=communicate))
            spawn = AsyncMock(return_value=process)
            with patch('youtube_play.asyncio.create_subprocess_exec', spawn), \
                 patch.dict('os.environ', {'DISCORD_BOT_TOKEN': 'private-token', 'GROQ_API_KEY': 'private-key', 'MC_SYNC_SECRET': 'private-secret'}):
                audio = YouTubeAudio(URL, asyncio.Semaphore(2))
                await audio.save(target)
            self.assertEqual(audio.size, 5)
            self.assertIn('＠everyone', audio.filename)
            self.assertNotIn('\n', audio.filename)
            options = spawn.call_args.kwargs
            self.assertNotIn('DISCORD_BOT_TOKEN', options['env'])
            self.assertNotIn('GROQ_API_KEY', options['env'])
            self.assertNotIn('MC_SYNC_SECRET', options['env'])
            self.assertIn(str(Path(sys.path[0]).resolve()), options['env']['PYTHONPATH'])
            self.assertEqual(options['stderr'], subprocess.DEVNULL)
            self.assertNotIn('shell', options)

    async def test_timeout_and_cancellation_terminate_worker_before_returning(self):
        for effect in (asyncio.TimeoutError(), asyncio.CancelledError()):
            process = SimpleNamespace(communicate=AsyncMock(side_effect=effect))
            kill = AsyncMock()
            with patch('youtube_play.asyncio.create_subprocess_exec', AsyncMock(return_value=process)), \
                 patch('youtube_play.kill_download', kill):
                audio = YouTubeAudio(URL, asyncio.Semaphore(2))
                with self.assertRaises(asyncio.CancelledError if isinstance(effect, asyncio.CancelledError) else SoundError):
                    await audio.save('unused.webm')
                kill.assert_awaited_once_with(process)

    async def test_safe_worker_errors_and_invalid_output(self):
        for output in (b'{bad json', b'[]', b'x' * 8193, b'{"ok":true}', b'{"ok":false,"reason":"live"}',
                       b'{"ok":false,"reason":"dependency"}', b'{"ok":false,"reason":"private-provider-body"}'):
            process = SimpleNamespace(communicate=AsyncMock(return_value=(output, None)))
            with patch('youtube_play.asyncio.create_subprocess_exec', AsyncMock(return_value=process)):
                with self.assertRaises(SoundError) as caught:
                    await YouTubeAudio(URL, asyncio.Semaphore(2)).save('unused.webm')
                self.assertNotIn('private-provider-body', str(caught.exception))

    async def test_real_child_process_can_be_stopped(self):
        flags = ({'creationflags': subprocess.CREATE_NO_WINDOW | subprocess.CREATE_NEW_PROCESS_GROUP}
                 if os.name == 'nt' else {'start_new_session': True})
        process = await asyncio.create_subprocess_exec(sys.executable, '-c', 'import time; time.sleep(30)',
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, **flags)
        await kill_download(process)
        self.assertIsNotNone(process.returncode)


class WorkerTests(unittest.TestCase):
    def test_real_node_challenge_provider_accepts_memory_options(self):
        from yt_dlp.extractor.youtube.jsc._builtin.node import NodeJCP
        runtime = SimpleNamespace(
            runtime_info=SimpleNamespace(path=youtube_worker.node_runtime(), version_tuple=(24, 19, 0)),
            ejs_setting=lambda key, default: default, logger=youtube_worker.QuietLogger(), _ARGS=['-'])
        runtime._clean_stderr = lambda value: NodeJCP._clean_stderr(runtime, value)
        options = '--max-old-space-size=128 --max-semi-space-size=2'
        with patch.dict(os.environ, {'NODE_OPTIONS': options}):
            output = NodeJCP._run_js_runtime(runtime,
                'console.log(JSON.stringify({rss:process.memoryUsage().rss, '
                'heap:require("node:v8").getHeapStatistics().heap_size_limit}))')
        values = json.loads(output)
        self.assertLessEqual(values['heap'], 150 * 1024 * 1024)
        self.assertGreater(values['rss'], 0)

    def test_worker_import_does_not_load_discord_or_voice_stack(self):
        code = ('import youtube_worker, sys; '
                'assert not ({"discord", "sound", "music_queue", "youtube_play"} & sys.modules.keys())')
        result = subprocess.run([sys.executable, '-c', code],
            cwd=Path(__file__).resolve().parents[1] / 'Discord-GameBot',
            capture_output=True, text=True, timeout=15)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_two_and_a_half_minute_track_remains_supported(self):
        info = {'duration': 150, 'title': '150 seconds', 'extractor_key': 'Youtube'}
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'audio.webm'
            with patch('yt_dlp.YoutubeDL', self.fake_downloader(info, path)):
                self.assertEqual(youtube_worker.download(URL, path), {'ok': True, 'title': '150 seconds'})
            self.assertEqual(path.read_bytes(), b'audio')

    def test_real_downloader_accepts_options_with_the_bundled_js_runtime(self):
        import yt_dlp
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'audio.webm'
            def extract(downloader, url, download):
                self.assertEqual(url, URL)
                self.assertTrue(Path(downloader.params['js_runtimes']['node']['path']).is_file())
                self.assertEqual(downloader.params['format'], 'bestaudio[protocol=https]')
                self.assertEqual(downloader.params['extractor_args']['youtube']['skip'], ['hls', 'dash', 'translated_subs'])
                self.assertTrue(download)
                path.write_bytes(b'audio')
                return {'extractor_key': 'Youtube', 'title': 'configured'}
            with patch.object(yt_dlp.YoutubeDL, 'extract_info', autospec=True, side_effect=extract):
                self.assertEqual(youtube_worker.download(URL, path), {'ok': True, 'title': 'configured'})

    def fake_downloader(self, info, path, oversized=False):
        class Downloader:
            def __init__(self, options):
                self.options = options
            def __enter__(self):
                return self
            def __exit__(self, *args):
                return False
            def extract_info(self, url, download):
                self_url.assertEqual(url, URL)
                rejection = self.options['match_filter'](info, incomplete=False)
                if not rejection:
                    if oversized:
                        self.options['progress_hooks'][0]({'downloaded_bytes': MAX_BYTES + 1})
                    Path(path).write_bytes(b'audio')
                return info
        self_url = self
        return Downloader

    def test_worker_checks_duration_live_and_file_limits_before_downloading(self):
        cases = [({'duration': 1801}, 'duration'), ({'duration': 10, 'is_live': True}, 'live'),
                 ({'duration': None}, 'unavailable'), ({'duration': 10, 'filesize': MAX_BYTES + 1}, 'size')]
        for info, reason in cases:
            with tempfile.TemporaryDirectory() as folder:
                path = Path(folder) / 'audio.webm'
                with patch('yt_dlp.YoutubeDL', self.fake_downloader(info, path)):
                    result = youtube_worker.download(URL, path)
                self.assertEqual(result, {'ok': False, 'reason': reason})
                self.assertFalse(path.exists())

    def test_worker_returns_only_safe_metadata_and_aborts_unknown_size_downloads(self):
        info = {'duration': 10, 'title': 'test song', 'extractor_key': 'Youtube', 'url': 'private-signed-media-url'}
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'audio.webm'
            with patch('yt_dlp.YoutubeDL', self.fake_downloader(info, path)):
                self.assertEqual(youtube_worker.download(URL, path), {'ok': True, 'title': 'test song'})
            path.unlink()
            with patch('yt_dlp.YoutubeDL', self.fake_downloader(info, path, oversized=True)):
                self.assertEqual(youtube_worker.download(URL, path), {'ok': False, 'reason': 'size'})
            self.assertFalse(path.exists())

    def test_missing_dependency_is_reported_without_traceback(self):
        with patch.dict('sys.modules', {'yt_dlp': None}):
            self.assertEqual(youtube_worker.download(URL, 'unused.webm'), {'ok': False, 'reason': 'dependency'})
