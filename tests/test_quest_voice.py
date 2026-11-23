import asyncio
import json
from pathlib import Path
import sys
import time
import tempfile
import unittest
from unittest.mock import AsyncMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'Discord-GameBot'))
from quest_voice import QuestVoice, VOICES, API, audio_urls
from sound import SoundError
from voice_reading import Providers

STATUS = 'https://audio2.tts.quest/v1/data/' + 'a' * 64 + '/status.json'
WAV_URL = STATUS.removesuffix('status.json') + 'audio.wav'
RESULT = dict(success=True, speakerName='VOICEVOX:ずんだもん（ノーマル）', audioStatusUrl=STATUS, wavDownloadUrl=WAV_URL)
WAV = b'RIFF' + b'0' * 4 + b'WAVE' + b'0' * 40


class QuestTests(unittest.IsolatedAsyncioTestCase):
    async def test_creation_poll_download_and_post_body_keep_text_and_key_out_of_urls(self):
        service = QuestVoice()
        service.key = 'test-private-key'
        service.request = AsyncMock(side_effect=[RESULT, {'isAudioReady': True}, WAV])
        result = await service.synthesize('hello private text', '3')
        self.assertEqual(result, WAV)
        calls = service.request.call_args_list
        first = calls[0]
        self.assertEqual(first.args[1:3], ('POST', API))
        self.assertEqual(first.kwargs['params'], {'speaker': '3'})
        self.assertEqual(first.kwargs['data'], {'text': 'hello private text', 'key': 'test-private-key'})
        self.assertEqual(calls[1].args[2], STATUS)
        self.assertEqual(calls[2].args[2], WAV_URL)

    async def test_file_synthesis_validates_wav_header_without_loading_full_audio(self):
        service = QuestVoice()
        async def request(client, method, url, **kwargs):
            if url == API: return RESULT
            if url == STATUS: return {'isAudioReady': True}
            self.assertTrue(kwargs['audio'])
            Path(kwargs['destination']).write_bytes(WAV)
            return len(WAV)
        service.request = AsyncMock(side_effect=request)
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'reading.wav'
            result = await service.synthesize('hello', '3', destination=path)
            self.assertEqual(result, len(WAV))
            self.assertEqual(path.read_bytes(), WAV)

    def test_audio_urls_reject_arbitrary_hosts_ports_credentials_queries_and_mismatched_jobs(self):
        for status, audio in [
            (STATUS.replace('audio2.tts.quest', 'localhost'), WAV_URL),
            (STATUS.replace('audio2.tts.quest', 'audio2.tts.quest.evil.example'), WAV_URL),
            (STATUS.replace('https://', 'https://secret@'), WAV_URL),
            (STATUS.replace('.quest/', '.quest:443/'), WAV_URL),
            (STATUS + '?redirect=http://localhost', WAV_URL),
            (STATUS, WAV_URL.replace('a' * 64, 'b' * 64)),
            (STATUS, 'https://127.0.0.1/private'),
        ]:
            with self.subTest(status=status), self.assertRaises(SoundError):
                audio_urls({'audioStatusUrl': status, 'wavDownloadUrl': audio})
        self.assertEqual(audio_urls(RESULT), (STATUS, WAV_URL))

    async def test_rejected_request_respects_retry_after_and_does_not_retry_immediately(self):
        service = QuestVoice()
        service.request = AsyncMock(return_value={'success': False, 'retryAfter': 300})
        with self.assertRaises(SoundError):
            await service.synthesize('test', '3')
        self.assertGreater(service.next_request - time.monotonic(), 290)
        with self.assertRaises(SoundError):
            await service.synthesize('test again', '3')
        self.assertEqual(service.request.await_count, 1)

    async def test_wrong_speaker_and_failed_generation_do_not_play_fallback_audio(self):
        for responses in ([RESULT | {'speakerName': 'VOICEVOX:別の声'}],
                          [RESULT, {'isAudioError': True}],
                          [RESULT, {'isAudioReady': True}, b'not a wav']):
            service = QuestVoice()
            service.request = AsyncMock(side_effect=responses)
            with self.assertRaises(SoundError):
                await service.synthesize('test', '3')

    async def test_polling_cancellation_releases_shared_lock_without_more_requests(self):
        service = QuestVoice()
        entered = asyncio.Event()
        async def request(client, method, url, **kwargs):
            if method == 'POST':
                return RESULT
            entered.set()
            return {'isAudioReady': False}
        service.request = AsyncMock(side_effect=request)
        task = asyncio.create_task(service.synthesize('test', '3'))
        await entered.wait()
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertFalse(service.lock.locked())
        self.assertEqual(service.request.await_count, 2)

    async def test_unknown_voice_and_overlong_input_do_not_contact_provider(self):
        service = QuestVoice()
        service.request = AsyncMock()
        for text, voice in [('test', '999999'), ('x' * 301, '3'), ('', '3')]:
            with self.assertRaises(SoundError):
                await service.synthesize(text, voice)
        service.request.assert_not_awaited()

    async def test_default_provider_requires_no_local_engine_and_exposes_reviewed_extra_voices(self):
        provider = Providers()
        provider.base = ''
        provider.quest.synthesize = AsyncMock(return_value=WAV)
        catalog = await provider.voices('voicevox')
        self.assertIn('九州そら', catalog['16'])
        self.assertIn('中国うさぎ', catalog['61'])
        self.assertGreaterEqual(len(catalog), 100)
        self.assertIn('春日部つむぎ', catalog['8'])
        self.assertIn('青山龍星', catalog['13'])
        audio = await provider.synthesize('test', {'provider': 'voicevox', 'voice': '16', 'speed': 1.5})
        self.assertEqual(audio, WAV)
        provider.quest.synthesize.assert_awaited_once_with('test', '16')


class Reply:
    def __init__(self, body, status=200, headers=None):
        self.body, self.status, self.headers = body, status, headers or {}
        self.content = self
    async def __aenter__(self):
        return self
    async def __aexit__(self, *args):
        pass
    async def iter_chunked(self, count):
        yield self.body[:4]
        yield self.body[4:]


class Client:
    def __init__(self, reply):
        self.reply = reply
        self.kwargs = None
    def request(self, *args, **kwargs):
        self.kwargs = kwargs
        return self.reply


class HttpTests(unittest.IsolatedAsyncioTestCase):
    async def test_streamed_audio_download_writes_file_and_bounds_size(self):
        service = QuestVoice()
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'reading.wav'
            size = await service.request(Client(Reply(WAV)), 'GET', WAV_URL, audio=True, destination=path)
            self.assertEqual(size, len(WAV))
            self.assertEqual(path.read_bytes(), WAV)
            with patch('quest_voice.MAX_BYTES', 10), self.assertRaises(SoundError):
                await service.request(Client(Reply(WAV)), 'GET', WAV_URL, audio=True, destination=path)
            self.assertFalse(path.exists())

    async def test_cancelled_audio_download_removes_partial_file(self):
        service = QuestVoice()
        started = asyncio.Event()
        class BlockingReply(Reply):
            async def iter_chunked(self, count):
                yield b'partial'
                started.set()
                await asyncio.Event().wait()
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'reading.wav'
            task = asyncio.create_task(service.request(Client(BlockingReply(b'')), 'GET', WAV_URL,
                                                       audio=True, destination=path))
            await started.wait()
            task.cancel()
            with self.assertRaises(asyncio.CancelledError): await task
            self.assertFalse(path.exists())

    async def test_chunked_response_redirects_and_errors_are_handled_without_raw_bodies(self):
        service = QuestVoice()
        client = Client(Reply(json.dumps({'ok': True}).encode()))
        self.assertEqual(await service.request(client, 'GET', STATUS), {'ok': True})
        self.assertFalse(client.kwargs['allow_redirects'])
        for code in (302, 403, 429, 500):
            client = Client(Reply(b'private-provider-body', code, {'Retry-After': '120'}))
            with self.assertRaises(SoundError) as caught:
                await service.request(client, 'GET', STATUS)
            self.assertNotIn('private-provider-body', str(caught.exception))
        self.assertGreater(service.next_request - time.monotonic(), 110)
