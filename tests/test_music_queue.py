import asyncio
import json
from pathlib import Path
import sys
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock, patch

import discord

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'Discord-GameBot'))
from sound import Sound, SoundError
from music_queue import Music, MusicView, Track
from youtube_play import normalize_playlist, load_playlist, MAX_QUEUE
import youtube_worker

URL_A = 'https://www.youtube.com/watch?v=BaW_jenozKc'
URL_B = 'https://www.youtube.com/watch?v=abcdefghijk'
URL_C = 'https://www.youtube.com/watch?v=lmnopqrstuv'
PLAYLIST = 'https://www.youtube.com/playlist?list=PL123456789012345'


class QueueTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.sound = Sound(SimpleNamespace())
        self.music = Music(SimpleNamespace(), self.sound)
        self.channel = Mock(spec=discord.VoiceChannel)
        self.channel.id = 2
        self.message = SimpleNamespace(edit=AsyncMock())
        self.channel.send = AsyncMock(return_value=self.message)
        self.channel.permissions_for.return_value = SimpleNamespace(view_channel=True, connect=True, speak=True)
        self.guild = SimpleNamespace(id=1, me=object(), voice_client=None)
        self.active, self.paused, self.connected = False, False, True
        self.auto_finish = True
        self.voice = SimpleNamespace(channel=self.channel, is_playing=lambda:self.active,
            is_paused=lambda:self.paused, is_connected=lambda:self.connected,
            pause=self.pause, resume=self.resume, stop=self.stop, disconnect=AsyncMock(side_effect=self.disconnect))
        self.sound.play = AsyncMock(side_effect=self.start_track)
        self.addAsyncCleanup(self.cleanup)

    def call(self, uid=10, joined=True, channel=None):
        channel = channel or self.channel
        return SimpleNamespace(guild=self.guild, guild_id=1, channel=channel, channel_id=channel.id,
            user=SimpleNamespace(id=uid, voice=SimpleNamespace(channel=channel) if joined else None),
            response=SimpleNamespace(send_message=AsyncMock(), defer=AsyncMock()),
            edit_original_response=AsyncMock())

    def pause(self):
        self.paused, self.active = True, False

    def resume(self):
        self.paused, self.active = False, True

    def stop(self):
        self.active, self.paused = False, False
        source = self.sound.playing.get(1)
        if source and self.auto_finish:
            asyncio.create_task(self.sound.finished(self.guild, source))

    async def disconnect(self, force):
        self.connected = False
        self.guild.voice_client = None

    async def start_track(self, context, channel, audio):
        self.assertTrue(self.sound.lock(1).locked())
        self.sound.target(context)
        audio.filename = audio.url.rsplit('=', 1)[-1] + '.webm'
        self.guild.voice_client = self.voice
        self.active, self.paused = True, False
        self.sound.playing[1] = SimpleNamespace(frames=1, audio_bytes=100, skipped_headers=0,
                                              playback_error=None, title=audio.filename)
        return 'playing'

    async def settle(self):
        for _ in range(20):
            await asyncio.sleep(0)

    async def cleanup(self):
        for session in self.music.sessions.values():
            for task in [session.task, session.ui_task, *session.fetches]:
                if task and not task.done():
                    task.cancel()
        await self.settle()
        for task in list(self.sound.idle_tasks.values()):
            task.cancel()
        await self.settle()

    async def test_enqueue_while_playing_keeps_fifo_and_advances_on_completion(self):
        await self.music.enqueue(self.call(), URL_A)
        await self.settle()
        await self.music.enqueue(self.call(uid=11), URL_B)
        await self.music.enqueue(self.call(uid=12), URL_C)
        session = self.music.sessions[1]
        self.assertEqual([t.url for t in session.queue], [URL_B, URL_C])
        self.assertEqual(self.sound.play.await_count, 1)
        source = session.source
        self.active = False
        await self.sound.finished(self.guild, source)
        await self.settle()
        self.assertEqual(session.current.url, URL_B)
        self.assertEqual([t.url for t in session.queue], [URL_C])
        await self.sound.finished(self.guild, source)
        self.assertEqual(self.sound.play.await_count, 2)

    async def test_pause_resume_keep_same_source_and_do_not_consume_queue(self):
        await self.music.enqueue(self.call(), URL_A)
        await self.settle()
        await self.music.enqueue(self.call(), URL_B)
        session = self.music.sessions[1]
        source = session.source
        await self.music.control(self.call(uid=11), 'pause')
        self.assertTrue(self.paused)
        self.assertIs(session.source, source)
        self.assertEqual(len(session.queue), 1)
        self.assertIn('一時停止', self.music.embed(session).description)
        await self.music.control(self.call(), 'resume')
        self.assertTrue(self.active)
        self.assertEqual(self.sound.play.await_count, 1)
        self.assertIs(session.source, source)

    async def finish_track(self, error=None):
        session = self.music.sessions[1]
        self.active, self.paused = False, False
        session.source.playback_error = error
        await self.sound.finished(self.guild, session.source)
        await self.settle()

    async def test_track_loop_repeats_current_and_skip_advances(self):
        await self.music.enqueue(self.call(), URL_A)
        await self.settle()
        await self.music.enqueue(self.call(), URL_B)
        await self.music.control(self.call(), 'loop_track')
        session = self.music.sessions[1]
        await self.finish_track()
        self.assertEqual(session.current.url, URL_A)
        self.assertEqual([t.url for t in session.queue], [URL_B])
        await self.music.control(self.call(), 'skip')
        await self.settle()
        self.assertEqual(session.current.url, URL_B)
        self.assertFalse(session.queue)
        await self.finish_track()
        self.assertEqual(session.current.url, URL_B)

    async def test_queue_loop_cycles_all_tracks_and_keeps_skipped_track_for_next_round(self):
        await self.music.enqueue(self.call(), URL_A)
        await self.settle()
        await self.music.enqueue(self.call(), URL_B)
        await self.music.enqueue(self.call(), URL_C)
        await self.music.control(self.call(), 'loop_queue')
        session = self.music.sessions[1]
        for expected in (URL_B, URL_C, URL_A):
            await self.finish_track()
            self.assertEqual(session.current.url, expected)
            self.assertEqual(len(session.queue), 2)
        await self.music.control(self.call(), 'skip')
        await self.settle()
        self.assertEqual(session.current.url, URL_B)
        self.assertEqual([t.url for t in session.queue], [URL_C, URL_A])

    async def test_loop_modes_are_exclusive_toggle_off_and_require_membership(self):
        await self.music.enqueue(self.call(), URL_A)
        await self.settle()
        session = self.music.sessions[1]
        await self.music.control(self.call(joined=False), 'loop_queue')
        self.assertEqual(session.loop_mode, 'off')
        await self.music.control(self.call(), 'loop_track')
        self.assertEqual(session.loop_mode, 'track')
        await self.music.control(self.call(), 'loop_queue')
        self.assertEqual(session.loop_mode, 'queue')
        view = MusicView(self.music, session.loop_mode)
        self.assertEqual(view.loop_queue.style, discord.ButtonStyle.success)
        self.assertEqual(view.loop_track.style, discord.ButtonStyle.secondary)
        view.stop()
        await self.music.control(self.call(), 'loop_queue')
        self.assertEqual(session.loop_mode, 'off')
        await self.finish_track()
        self.assertIsNone(session.current)
        self.assertFalse(session.queue)

    async def test_failed_playback_is_not_repeated_in_either_loop_mode(self):
        for action in ('loop_track', 'loop_queue'):
            await self.music.enqueue(self.call(), URL_A)
            await self.settle()
            await self.music.control(self.call(), action)
            await self.finish_track(error='RuntimeError')
            session = self.music.sessions[1]
            self.assertIsNone(session.current)
            self.assertFalse(session.queue)
            self.music.clear(1)
        self.assertEqual(self.sound.play.await_count, 2)

    async def test_full_stop_disables_loop_and_does_not_restart(self):
        for action in ('stop', 'leave'):
            self.connected = True
            await self.music.enqueue(self.call(), URL_A)
            await self.settle()
            await self.music.control(self.call(), 'loop_queue')
            count = self.sound.play.await_count
            await self.sound.invoke(self.call(), action)
            await self.settle()
            session = self.music.sessions[1]
            self.assertEqual(session.loop_mode, 'off')
            self.assertFalse(session.queue)
            self.assertIsNone(session.current)
            self.assertEqual(self.sound.play.await_count, count)

    async def test_loop_can_be_set_during_preparation_and_skip_preserves_queue_cycle(self):
        entered = asyncio.Event()
        first = True
        async def play(context, channel, audio):
            nonlocal first
            if first:
                first = False
                entered.set()
                await asyncio.Event().wait()
            return await self.start_track(context, channel, audio)
        self.sound.play.side_effect = play
        await self.music.enqueue(self.call(), URL_A)
        await entered.wait()
        await self.music.enqueue(self.call(), URL_B)
        await self.music.control(self.call(), 'loop_queue')
        await self.music.control(self.call(), 'skip')
        await self.settle()
        session = self.music.sessions[1]
        self.assertEqual(session.current.url, URL_B)
        self.assertEqual([t.url for t in session.queue], [URL_A])

    async def test_skip_does_not_double_advance_before_cleanup(self):
        await self.music.enqueue(self.call(), URL_A)
        await self.settle()
        await self.music.enqueue(self.call(), URL_B)
        self.auto_finish = False
        await self.music.control(self.call(), 'skip')
        again = self.call()
        await self.music.control(again, 'skip')
        self.assertIn('切り替え中', again.edit_original_response.call_args.kwargs['content'])
        session = self.music.sessions[1]
        await self.sound.finished(self.guild, session.source)
        await self.settle()
        self.assertEqual(session.current.url, URL_B)
        self.assertFalse(session.skipping)

    async def test_failed_track_is_skipped_and_next_track_plays(self):
        async def play(context, channel, audio):
            if audio.url == URL_A:
                raise SoundError('unavailable')
            return await self.start_track(context, channel, audio)
        self.sound.play.side_effect = play
        await self.music.enqueue(self.call(), URL_A)
        await self.music.enqueue(self.call(), URL_B)
        await self.settle()
        self.assertEqual(self.music.sessions[1].current.url, URL_B)
        self.assertEqual(self.sound.play.await_count, 2)

    async def test_queue_is_authorized_at_enqueue_and_dashboard_actions(self):
        await self.music.enqueue(self.call(joined=False), URL_A)
        self.assertFalse(self.music.sessions)
        await self.music.enqueue(self.call(), URL_A)
        await self.settle()
        await self.music.control(self.call(joined=False), 'pause')
        self.assertTrue(self.active)
        other = Mock(spec=discord.VoiceChannel)
        other.id = 9
        await self.music.enqueue(self.call(channel=other), URL_B)
        self.assertFalse(self.music.sessions[1].queue)

    async def test_playlist_import_is_ordered_bounded_and_not_downloaded_until_playback(self):
        entries = [{'url': URL_B, 'title': 'Second'}, {'url': URL_C, 'title': 'Third'}]
        await self.music.enqueue(self.call(), URL_A)
        await self.settle()
        with patch('music_queue.load_playlist', AsyncMock(return_value=(entries, False))) as load:
            await self.music.enqueue(self.call(), PLAYLIST)
        load.assert_awaited_once()
        self.assertEqual([t.title for t in self.music.sessions[1].queue], ['Second', 'Third'])
        self.assertEqual(self.sound.play.await_count, 1)
        self.music.sessions[1].queue.extend(Track(URL_B) for _ in range(MAX_QUEUE - 2))
        denied = self.call()
        await self.music.enqueue(denied, URL_C)
        self.assertIn('最大50曲', denied.edit_original_response.call_args.kwargs['content'])

    async def test_skip_cancels_preparation_and_starts_next_queued_track(self):
        entered = asyncio.Event()
        async def play(context, channel, audio):
            if audio.url == URL_A:
                entered.set()
                await asyncio.Event().wait()
            return await self.start_track(context, channel, audio)
        self.sound.play.side_effect = play
        await self.music.enqueue(self.call(), URL_A)
        await entered.wait()
        await self.music.enqueue(self.call(), URL_B)
        await self.music.control(self.call(), 'skip')
        await self.settle()
        self.assertEqual(self.music.sessions[1].current.url, URL_B)

    async def test_sound_stop_and_leave_clear_queue_without_autoplay(self):
        for action in ('stop', 'leave'):
            self.connected = True
            await self.music.enqueue(self.call(), URL_A)
            await self.settle()
            await self.music.enqueue(self.call(), URL_B)
            count = self.sound.play.await_count
            await self.sound.invoke(self.call(), action)
            await self.settle()
            self.assertFalse(self.music.sessions[1].queue)
            self.assertIsNone(self.music.sessions[1].current)
            self.assertEqual(self.sound.play.await_count, count)

    async def test_stop_cancels_playlist_request_and_cannot_append_after_clear(self):
        entered = asyncio.Event()
        async def loading(*args):
            entered.set()
            await asyncio.Event().wait()
        with patch('music_queue.load_playlist', side_effect=loading):
            pending = asyncio.create_task(self.music.enqueue(self.call(), PLAYLIST))
            await entered.wait()
            await self.sound.invoke(self.call(), 'leave')
            await pending
        self.assertFalse(self.music.sessions[1].queue)
        self.sound.play.assert_not_awaited()

    async def test_queue_after_external_file_waits_for_source_cleanup(self):
        self.guild.voice_client = self.voice
        self.active = True
        external = SimpleNamespace(frames=10, audio_bytes=100, skipped_headers=0, playback_error=None)
        self.sound.playing[1] = external
        await self.music.enqueue(self.call(), URL_A)
        await self.settle()
        self.sound.play.assert_not_awaited()
        await self.music.control(self.call(), 'skip')
        await self.settle()
        self.assertEqual(self.music.sessions[1].current.url, URL_A)
        self.assertFalse(self.music.sessions[1].skipping)

    async def test_idle_timeout_does_not_disconnect_while_playlist_is_pending(self):
        self.guild.voice_client = self.voice
        session = self.music.session_for(self.call())
        job = asyncio.create_task(asyncio.Event().wait())
        session.fetches.add(job)
        with patch('sound.asyncio.sleep', AsyncMock()):
            await self.sound.idle_disconnect(self.guild)
        self.voice.disconnect.assert_not_awaited()
        session.fetches.discard(job)
        job.cancel()
        await asyncio.gather(job, return_exceptions=True)
        with patch('sound.asyncio.sleep', AsyncMock()):
            await self.sound.idle_disconnect(self.guild)
        self.voice.disconnect.assert_awaited_once()

    async def test_dashboard_buttons_persist_and_embed_escapes_titles(self):
        view = MusicView(self.music)
        self.assertTrue(view.is_persistent())
        self.assertEqual([b.label for b in view.children], ['次へ', '再生', 'ストップ', '曲ループ', '全曲ループ'])
        session = self.music.session_for(self.call())
        session.queue.extend(Track(URL_A, '@everyone ' + '😀' * 90) for _ in range(50))
        embed = self.music.embed(session)
        self.assertNotIn('@everyone', embed.to_dict().__str__())
        self.assertTrue(all(len(f.value.encode('utf-16-le')) // 2 <= 1024 for f in embed.fields))
        view.stop()


class PlaylistTests(unittest.IsolatedAsyncioTestCase):
    def test_playlist_normalization_rejects_hosts_credentials_and_bad_ids(self):
        self.assertEqual(normalize_playlist(URL_A + '&list=PL123456789012345'), PLAYLIST)
        self.assertIsNone(normalize_playlist(URL_A))
        for url in ('https://evil.example/playlist?list=PL123456789012345', PLAYLIST + '&list=PLother00000',
                    'https://youtube.com/playlist?list=../secret', 'https://user@youtube.com/playlist?list=PL123456789012345'):
            with self.assertRaises(SoundError):
                normalize_playlist(url)

    async def test_playlist_response_is_checked_and_media_urls_are_never_used_directly(self):
        data = {'ok': True, 'entries': [{'url': URL_A, 'title': 'First'}, {'url': URL_B, 'title': 'Second'}], 'limited': False}
        with patch('youtube_play.run_worker', AsyncMock(return_value=json.dumps(data).encode())):
            entries, limited = await load_playlist(PLAYLIST, asyncio.Semaphore(2))
        self.assertEqual([e['url'] for e in entries], [URL_A, URL_B])
        data['entries'][0]['url'] = 'https://127.0.0.1/private'
        with patch('youtube_play.run_worker', AsyncMock(return_value=json.dumps(data).encode())):
            with self.assertRaises(SoundError):
                await load_playlist(PLAYLIST, asyncio.Semaphore(2))

    def test_playlist_worker_uses_flat_extraction_and_skips_private_or_bad_entries(self):
        options = {}
        class Downloader:
            def __init__(self, params):
                options.update(params)
            def __enter__(self):
                return self
            def __exit__(self, *args):
                return False
            def extract_info(self, url, download):
                assert url == PLAYLIST and download is False
                return {'_type': 'playlist', 'entries': [
                    {'id': 'BaW_jenozKc', 'title': 'one'}, None,
                    {'id': 'abcdefghijk', 'availability': 'private'}, {'id': '../../bad'},
                    {'id': 'lmnopqrstuv', 'title': 'two'}]}
        with patch('yt_dlp.YoutubeDL', Downloader):
            result = youtube_worker.playlist(PLAYLIST)
        self.assertEqual([e['url'] for e in result['entries']], [URL_A, URL_C])
        self.assertEqual(options['playlistend'], MAX_QUEUE)
        self.assertEqual(options['extract_flat'], 'in_playlist')
