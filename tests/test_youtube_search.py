import asyncio
import json
from pathlib import Path
import sys
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

import discord
from discord.ext import commands

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'Discord-GameBot'))
from sound import SoundError
from youtube_play import normalize_search, normalize_url, search_youtube, register_youtube_play
import youtube_worker

URL = 'https://www.youtube.com/watch?v=BaW_jenozKc'
IDS = ['BaW_jenozKc', 'abcdefghijk', 'lmnopqrstuv', '12345678901', 'ABCDEFGHIJK', 'LMNOPQRSTUV', 'ZYXWVUTSRQP']


class SearchTests(unittest.IsolatedAsyncioTestCase):
    def test_words_with_spaces_and_japanese_are_supported_and_bounded(self):
        self.assertEqual(normalize_search('  初音ミク   作業用 BGM  '), '初音ミク 作業用 BGM')
        self.assertEqual(normalize_search('https://example.org/file'), 'https://example.org/file')
        for value in ('', ' \n ', 'a' * 201, 'bad\x00keyword'):
            with self.assertRaises(SoundError):
                normalize_search(value)

    def test_amazon_links_explain_that_direct_playback_is_not_supported(self):
        for url in ('https://music.amazon.co.jp/albums/B012345678',
                    'https://music.amazon.com/tracks/B012345678', 'https://amzn.to/test'):
            with self.assertRaises(SoundError) as caught:
                normalize_url(url)
            self.assertIn('Amazon Music', str(caught.exception))
            self.assertIn('/search', str(caught.exception))

    async def test_search_protocol_returns_seven_canonical_links_and_empty_results(self):
        entries = [{'url': 'https://www.youtube.com/watch?v=' + video, 'title': 'Title ' + str(i)}
                   for i, video in enumerate(IDS)]
        worker = AsyncMock(return_value=json.dumps({'ok': True, 'entries': entries}).encode())
        with patch('youtube_play.run_worker', worker):
            results = await search_youtube('hello world', asyncio.Semaphore(2))
        worker.assert_awaited_once_with('hello world', '-', search=True)
        self.assertEqual(results, entries)
        with patch('youtube_play.run_worker', AsyncMock(return_value=b'{"ok":true,"entries":[]}')):
            self.assertEqual(await search_youtube('missing', asyncio.Semaphore(2)), [])

    async def test_bad_results_and_raw_provider_errors_are_not_exposed(self):
        for data in (b'[]', b'{bad', b'x' * 32769,
                json.dumps({'ok': True, 'entries': [{'url': 'https://127.0.0.1/secret'}]}).encode(),
                json.dumps({'ok': True, 'entries': [{'url': URL}] * 8}).encode(),
                b'{"ok":false,"reason":"private-provider-body"}'):
            with patch('youtube_play.run_worker', AsyncMock(return_value=data)):
                with self.assertRaises(SoundError) as caught:
                    await search_youtube('test', asyncio.Semaphore(2))
                self.assertNotIn('private-provider-body', str(caught.exception))

    async def test_timeout_cancels_pending_search_work(self):
        cancelled = asyncio.Event()
        async def waiting(*args, **kwargs):
            try:
                await asyncio.Event().wait()
            finally:
                cancelled.set()
        real_wait_for = asyncio.wait_for
        async def short_wait(awaitable, timeout):
            self.assertEqual(timeout, 45)
            return await real_wait_for(awaitable, timeout=0.01)
        with patch('youtube_play.run_worker', side_effect=waiting), \
             patch('youtube_play.asyncio.wait_for', side_effect=short_wait):
            with self.assertRaises(SoundError) as caught:
                await search_youtube('test', asyncio.Semaphore(2))
        self.assertIn('タイムアウト', str(caught.exception))
        self.assertTrue(cancelled.is_set())

    async def test_search_can_run_in_text_channel_without_voice_and_shows_titles_and_links(self):
        bot = commands.Bot(command_prefix='!', intents=discord.Intents.none())
        service = register_youtube_play(bot, SimpleNamespace(status=AsyncMock()))
        command = bot.tree.get_command('search')
        self.assertTrue(command.guild_only)
        self.assertEqual(command.parameters[0].name, 'query')
        interaction = SimpleNamespace(response=SimpleNamespace(send_message=AsyncMock()),
                                      edit_original_response=AsyncMock())
        entries = [{'url': 'https://www.youtube.com/watch?v=' + video,
                    'title': '@everyone [Song] ' + str(i)} for i, video in enumerate(IDS)]
        with patch('music_queue.search_youtube', AsyncMock(return_value=entries)) as search:
            await command.callback(interaction, '初音ミク 作業用 BGM')
        search.assert_awaited_once_with('初音ミク 作業用 BGM', service.search_slots)
        interaction.response.send_message.assert_awaited_once()
        result = interaction.edit_original_response.call_args.kwargs
        self.assertIsNone(result['content'])
        self.assertEqual(len(result['embed'].fields), 7)
        self.assertEqual([button.label for button in result['view'].children], [f'候補{i}' for i in range(1, 8)])
        self.assertEqual([button.video_url for button in result['view'].children], [entry['url'] for entry in entries])
        for row in result['view'].to_components():
            for button in row['components']:
                self.assertIn('custom_id', button)
                self.assertNotIn('url', button)
        self.assertNotIn('@everyone', str(result['embed'].to_dict()))
        for field, video in zip(result['embed'].fields, IDS):
            self.assertIn('https://www.youtube.com/watch?v=' + video, field.value)
            self.assertIn('Song', field.value)
        self.assertFalse(result['allowed_mentions'].everyone)
        await bot.close()

    async def test_search_empty_results_and_failure_are_explained_without_provider_details(self):
        bot = commands.Bot(command_prefix='!', intents=discord.Intents.none())
        sound = SimpleNamespace(status=AsyncMock())
        service = register_youtube_play(bot, sound)
        interaction = SimpleNamespace(response=SimpleNamespace(send_message=AsyncMock()),
                                      edit_original_response=AsyncMock())
        with patch('music_queue.search_youtube', AsyncMock(return_value=[])):
            await service.search(interaction, 'nothing')
        self.assertIn('見つかりません', interaction.edit_original_response.call_args.kwargs['embed'].description)
        self.assertIsNone(interaction.edit_original_response.call_args.kwargs['view'])
        with patch('music_queue.search_youtube', AsyncMock(side_effect=RuntimeError('private-provider-body'))):
            await service.search(interaction, 'test')
        self.assertNotIn('private-provider-body', sound.status.call_args.args[1])
        await bot.close()

    def test_worker_uses_real_downloader_in_search_mode_without_downloading_audio(self):
        import yt_dlp
        def extract(downloader, query, download):
            self.assertEqual(query, 'ytsearch7:hello world')
            self.assertFalse(download)
            self.assertTrue(downloader.params['extract_flat'])
            self.assertTrue(downloader.params['skip_download'])
            self.assertEqual(downloader.params['playlistend'], 7)
            return {'entries': [{'id': video, 'title': str(i), 'url': 'private-signed-media-url'}
                                for i, video in enumerate(IDS)]}
        with patch.object(yt_dlp.YoutubeDL, 'extract_info', autospec=True, side_effect=extract):
            result = youtube_worker.search('hello world')
        self.assertTrue(result['ok'])
        self.assertEqual(len(result['entries']), 7)
        self.assertNotIn('private-signed-media-url', json.dumps(result))

    def test_worker_skips_invalid_and_duplicate_ids_and_never_extracts_input_as_url(self):
        import yt_dlp
        def extract(downloader, query, download):
            self.assertEqual(query, 'ytsearch7:https://127.0.0.1/secret')
            return {'entries': [None, {'id': '../bad'}, {'id': IDS[0]}, {'id': IDS[0]}, {'id': IDS[1]}]}
        with patch.object(yt_dlp.YoutubeDL, 'extract_info', autospec=True, side_effect=extract):
            result = youtube_worker.search('https://127.0.0.1/secret')
        self.assertEqual([entry['url'] for entry in result['entries']], [URL, 'https://www.youtube.com/watch?v=' + IDS[1]])

