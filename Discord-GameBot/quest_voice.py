"""TTS Quest v3 adapter: opt-in external synthesis, bounded polling and downloads."""
import asyncio
import json
import math
import os
import re
import time

import aiohttp

from sound import MAX_BYTES, SoundError

# IDs from VOICEVOX's published talk-model metadata. Only reviewed libraries.
VOICES = {
    '3': 'ずんだもん（ノーマル）', '1': 'ずんだもん（あまあま）',
    '7': 'ずんだもん（ツンツン）', '5': 'ずんだもん（セクシー）',
    '22': 'ずんだもん（ささやき）', '38': 'ずんだもん（ヒソヒソ）',
    '2': '四国めたん（ノーマル）', '0': '四国めたん（あまあま）',
    '6': '四国めたん（ツンツン）', '4': '四国めたん（セクシー）',
    '36': '四国めたん（ささやき）', '37': '四国めたん（ヒソヒソ）',
    '16': '九州そら（ノーマル）', '15': '九州そら（あまあま）',
    '18': '九州そら（ツンツン）', '17': '九州そら（セクシー）',
    '19': '九州そら（ささやき）',
    '61': '中国うさぎ（ノーマル）', '62': '中国うさぎ（おどろき）',
    '63': '中国うさぎ（こわがり）', '64': '中国うさぎ（へろへろ）',
}
REVIEWED_NAMES = {'ずんだもん', '四国めたん', '九州そら', '中国うさぎ'}
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

    async def request(self, client, method, url, *, audio=False, **kwargs):
        async with client.request(method, url, allow_redirects=False, **kwargs) as response:
            limit = MAX_BYTES if audio else 16384
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

    async def synthesize(self, text, voice):
        if voice not in VOICES:
            raise SoundError('この外部API構成で利用できる声を /voice change で選んでください。')
        if not isinstance(text, str) or not 1 <= len(text) <= 300:
            raise SoundError('読み上げる文章は1〜300文字で指定してください。')
        try:
            return await asyncio.wait_for(self._synthesize(text, voice), 90)
        except asyncio.TimeoutError:
            raise SoundError('外部音声APIの生成待ちがタイムアウトしました。混雑時は時間をおいてお試しください。') from None
        except aiohttp.ClientError:
            raise SoundError('外部音声APIへ接続できませんでした。ネットワーク・プロキシ設定を確認してください。') from None

    async def _synthesize(self, text, voice):
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
                if VOICES[voice] not in str(result.get('speakerName', '')):
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
                        data = await self.request(client, 'GET', wav_url, audio=True)
                        if len(data) < 44 or data[:4] != b'RIFF' or data[8:12] != b'WAVE':
                            raise SoundError('外部音声APIから有効なWAVを取得できませんでした。')
                        return data
                    await asyncio.sleep(3)
                raise SoundError('外部音声APIでの生成に時間がかかっています。時間をおいてお試しください。')
