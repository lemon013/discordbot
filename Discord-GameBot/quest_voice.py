"""TTS Quest v3 adapter: opt-in external synthesis, bounded polling and downloads."""
import asyncio
import json
import math
import os
import re
import time
from pathlib import Path
from audio_io import stream_audio

import aiohttp

from sound import MAX_BYTES, SoundError
from voice_catalog import VOICES, API_LABELS, REVIEWED_NAMES, speaker_matches

API = 'https://api.tts.quest/v3/voicevox/synthesis'


def audio_urls(result):
    status = result.get('audioStatusUrl', '')
    if not isinstance(status, str):
        raise SoundError('外部音声APIの応答形式が不正です。')
    match = re.fullmatch(r'https://audio[1-9][0-9]*\.tts\.quest/v1/data/[a-f0-9]{64}/status\.json', status)
    if not match or result.get('wavDownloadUrl') != status.removesuffix('status.json') + 'audio.wav':
        raise SoundError('外部音声APIが返した音声URLを確認できませんでした。')
    return status, result['wavDownloadUrl']


class QuestVoice:
    def __init__(self):
        self.key = os.getenv('TTS_QUEST_API_KEY', '')
        self.lock = asyncio.Lock()
        self.next_request = 0

    def throttle(self, value, default=3):
        try:
            seconds = float(value)
            if not math.isfinite(seconds) or seconds < 0:
                raise ValueError()
        except (ValueError, TypeError):
            seconds = default
        self.next_request = max(self.next_request, time.monotonic() + max(3, seconds))

    async def request(self, client, method, url, *, audio=False, destination=None, **kwargs):
        async with client.request(method, url, allow_redirects=False, **kwargs) as response:
            limit = MAX_BYTES if audio else 16384
            if audio and destination is not None and response.status == 200:
                return await stream_audio(response, destination, limit)
            data = bytearray()
            async for chunk in response.content.iter_chunked(65536):
                data.extend(chunk)
                if len(data) > limit:
                    raise SoundError('外部音声APIの応答が大きすぎます。')
            if response.status != 200:
                self.throttle(response.headers.get('Retry-After'), 60)
                if response.status == 429:
                    raise SoundError('外部音声APIの利用制限中です。時間をおいて再試行してください。')
                raise SoundError(f'外部音声APIへの接続が拒否・失敗しました（HTTP {response.status}）。')
            if audio:
                return bytes(data)
            try:
                result = json.loads(data)
                if not isinstance(result, dict):
                    raise ValueError()
                return result
            except (ValueError, TypeError):
                raise SoundError('外部音声APIの応答を読み取れませんでした。') from None

    async def synthesize(self, text, voice, destination=None):
        if voice not in VOICES:
            raise SoundError('この外部API構成で利用できる声を /voice change で選んでください。')
        if not isinstance(text, str) or not 1 <= len(text) <= 300:
            raise SoundError('読み上げる文章は1〜300文字で指定してください。')
        try:
            return await asyncio.wait_for(self._synthesize(text, voice, destination), 90)
        except asyncio.TimeoutError:
            raise SoundError('外部音声APIの生成待ちがタイムアウトしました。混雑時は時間をおいてお試しください。') from None
        except aiohttp.ClientError:
            raise SoundError('外部音声APIへ接続できませんでした。ネットワーク・プロキシ設定を確認してください。') from None

    async def _synthesize(self, text, voice, destination=None):
        async with self.lock:
            delay = self.next_request - time.monotonic()
            if delay > 30:
                raise SoundError('外部音声APIの利用制限・待機時間中です。時間をおいて再試行してください。')
            if delay > 0:
                await asyncio.sleep(delay)
            async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=15),
                    headers={'User-Agent': 'DiscordVoiceBot/1.0'}, trust_env=True) as client:
                form = {'text': text}
                if self.key:
                    form['key'] = self.key
                # Keep text/key in the POST body, never in the URL or logs.
                self.throttle(3)
                result = await self.request(client, 'POST', API, params={'speaker': voice}, data=form)
                if 'retryAfter' in result:
                    self.throttle(result['retryAfter'])
                if result.get('success') is not True:
                    raise SoundError('外部音声APIで合成できませんでした。利用制限や対応音声を確認してください。')
                if not speaker_matches(API_LABELS[voice], result.get('speakerName', '')):
                    raise SoundError('指定した声の音声を外部APIから取得できませんでした。')
                status_url, wav_url = audio_urls(result)
                # Poll at most once every 3 seconds; no unbounded retries.
                for _ in range(24):
                    status = await self.request(client, 'GET', status_url)
                    if 'retryAfter' in status:
                        self.throttle(status['retryAfter'])
                        raise SoundError('外部音声APIの利用制限中です。時間をおいて再試行してください。')
                    if status.get('isAudioError'):
                        raise SoundError('外部音声APIで音声生成が失敗しました。')
                    if status.get('isAudioReady') is True:
                        options = {'destination': destination} if destination is not None else {}
                        data = await self.request(client, 'GET', wav_url, audio=True, **options)
                        if destination is not None:
                            with Path(destination).open('rb') as handle:
                                header = handle.read(12)
                            valid = Path(destination).stat().st_size >= 44 and header[:4] == b'RIFF' and header[8:12] == b'WAVE'
                        else:
                            valid = len(data) >= 44 and data[:4] == b'RIFF' and data[8:12] == b'WAVE'
                        if not valid:
                            raise SoundError('外部音声APIから有効なWAVを取得できませんでした。')
                        return data
                    await asyncio.sleep(3)
                raise SoundError('外部音声APIでの生成に時間がかかっています。時間をおいてお試しください。')
