"""YouTube URL command feeding a local audio file to the existing voice player."""
import asyncio
import json
import logging
import os
from pathlib import Path
import re
import signal
import subprocess
import sys
import weakref
from urllib.parse import parse_qs, urlsplit

import discord
from discord import app_commands
from sound import MAX_BYTES, SoundError

MAX_DURATION = 30 * 60
DOWNLOAD_TIMEOUT = 110
MAX_QUEUE = 50
SEARCH_LIMIT = 7
WORKER_MEMORY_MIB = 300
_worker_gates = weakref.WeakKeyDictionary()
log = logging.getLogger(__name__)
ERRORS = {
    'dependency': 'YouTube再生の準備ができていません。更新後のrequirements.txtでパッケージを再インストールしてください。',
    'live': 'ライブ配信・配信予約には対応していません。公開済みの通常動画を指定してください。',
    'duration': '再生できる動画は30分以内です。',
    'size': '動画の音声が25MBの上限を超えています。短い動画を指定してください。',
    'unavailable': 'YouTubeの音声を取得できませんでした。公開動画のURLを確認してください。YouTube側のBot判定・地域・年齢・ログイン制限で取得できない場合もあります。',
}


def normalize_url(value):
    value = value.strip()
    if value.startswith('<') and value.endswith('>'):
        value = value[1:-1]
    if len(value) > 2048:
        raise SoundError('YouTube動画のURLを指定してください。')
    try:
        url = urlsplit(value)
        host = (url.hostname or '').lower()
        if host.startswith('music.amazon.') or host in ('amzn.to', 'a.co'):
            raise SoundError('Amazon Musicの直接再生には現在対応していません。/search でYouTubeを検索するか、/sound play で音声ファイルを添付してください。')
        if url.scheme != 'https' or url.username or url.password or url.port:
            raise ValueError()
        parts = url.path.strip('/').split('/')
        if host in ('youtu.be', 'www.youtu.be') and len(parts) == 1:
            video_id = parts[0]
        elif host in ('youtube.com', 'www.youtube.com', 'm.youtube.com', 'music.youtube.com'):
            if url.path == '/watch':
                ids = parse_qs(url.query).get('v', [])
                if len(ids) != 1:
                    raise ValueError()
                video_id = ids[0]
            elif len(parts) == 2 and parts[0] in ('shorts', 'embed', 'live'):
                video_id = parts[1]
            else:
                raise ValueError()
        else:
            raise ValueError()
        if not re.fullmatch(r'[A-Za-z0-9_-]{11}', video_id):
            raise ValueError()
        # Never follow arbitrary input hosts, playlist links or user query options.
        return 'https://www.youtube.com/watch?v=' + video_id
    except ValueError:
        raise SoundError('https://www.youtube.com/watch?v=... または https://youtu.be/... の動画URLを指定してください。') from None


async def kill_download(process):
    if process.returncode is not None:
        return
    if os.name == 'nt':
        killer = await asyncio.create_subprocess_exec('taskkill', '/PID', str(process.pid), '/T', '/F',
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, creationflags=subprocess.CREATE_NO_WINDOW)
        try:
            await asyncio.wait_for(killer.wait(), timeout=5)
        except asyncio.TimeoutError:
            killer.kill()
            await killer.wait()
        if process.returncode is None:
            try:
                process.kill()
            except ProcessLookupError:
                pass
    else:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
    await process.wait()


def normalize_playlist(value):
    value = value.strip().removeprefix('<').removesuffix('>')
    try:
        if len(value) > 2048:
            raise ValueError()
        url = urlsplit(value)
        ids = parse_qs(url.query).get('list', [])
        if not ids:
            return None
        if (url.scheme != 'https' or url.username is not None or url.password is not None or url.port
                or (url.hostname or '').lower() not in ('youtube.com', 'www.youtube.com', 'm.youtube.com',
                    'music.youtube.com', 'youtu.be', 'www.youtu.be')
                or len(ids) != 1 or not re.fullmatch(r'[A-Za-z0-9_-]{10,100}', ids[0])):
            raise ValueError()
        return 'https://www.youtube.com/playlist?list=' + ids[0]
    except ValueError:
        raise SoundError('公開されているYouTubeプレイリストのURLを指定してください。') from None


async def load_playlist(url, slots):
    canonical = normalize_playlist(url)
    if canonical is None:
        raise SoundError('YouTubeプレイリストのURLを指定してください。')
    async with slots:
        output = await asyncio.wait_for(run_worker(canonical, '-', playlist=True), timeout=120)
    try:
        if len(output) > 131072:
            raise ValueError()
        data = json.loads(output)
        if not data.get('ok'):
            raise SoundError(ERRORS.get(data.get('reason'), ERRORS['unavailable']))
        entries = data['entries']
        if not isinstance(entries, list) or not 0 < len(entries) <= MAX_QUEUE:
            raise ValueError()
        tracks = [{'url': normalize_url(item['url']), 'title': str(item.get('title') or '')[:150]} for item in entries]
        return tracks, bool(data.get('limited'))
    except (ValueError, KeyError, TypeError, AttributeError):
        raise SoundError('プレイリストから再生可能な動画を取得できませんでした。') from None


def normalize_search(query):
    query = ' '.join(query.split())
    if not query or len(query) > 200 or any(ord(char) < 32 for char in query):
        raise SoundError('検索ワードを1〜200文字で入力してください。空白を含むキーワードも使えます。')
    return query


async def search_youtube(query, slots):
    query = normalize_search(query)

    async def fetch():
        async with slots:
            return await run_worker(query, '-', search=True)

    try:
        output = await asyncio.wait_for(fetch(), timeout=45)
    except asyncio.TimeoutError:
        raise SoundError('YouTube検索がタイムアウトしました。時間をおいて再試行してください。') from None
    try:
        if len(output) > 32768:
            raise ValueError()
        data = json.loads(output)
        if not data.get('ok'):
            if data.get('reason') == 'dependency':
                raise SoundError(ERRORS['dependency'])
            raise SoundError('YouTubeを検索できませんでした。時間をおいて再試行してください。')
        entries = data['entries']
        if not isinstance(entries, list) or len(entries) > SEARCH_LIMIT:
            raise ValueError()
        return [{'url': normalize_url(item['url']), 'title': str(item.get('title') or 'YouTube動画')[:150]}
                for item in entries]
    except (ValueError, KeyError, TypeError, AttributeError):
        raise SoundError('YouTubeの検索結果を読み取れませんでした。時間をおいて再試行してください。') from None


def memory_mib():
    if sys.platform != 'linux':
        return None
    from runtime_probe import process_memory
    return process_memory()[1]


async def run_worker(url, destination, playlist=False, search=False):
    # Searches, playlists and audio extraction share ONE process budget.
    loop = asyncio.get_running_loop()
    gate = _worker_gates.setdefault(loop, asyncio.Lock())
    async with gate:
        return await _run_worker(url, destination, playlist=playlist, search=search)


async def _run_worker(url, destination, playlist=False, search=False):
    usage = await asyncio.to_thread(memory_mib)
    if usage is not None and usage >= WORKER_MEMORY_MIB:
        raise SoundError('メモリ負荷が高いためYouTubeの取得を見送りました。再生中の音声が減ってから再試行してください。')
    worker = Path(__file__).with_name('youtube_worker.py')
    flags = ({'creationflags': subprocess.CREATE_NO_WINDOW | subprocess.CREATE_NEW_PROCESS_GROUP}
             if os.name == 'nt' else {'start_new_session': True})
    environment = {key: value for key, value in os.environ.items()
                   if not any(tag in key.upper() for tag in ('TOKEN', 'SECRET', 'API_KEY'))}
    # MWS can add its .packages directory to sys.path at launch time.
    environment['PYTHONPATH'] = os.pathsep.join(os.path.abspath(item or os.getcwd()) for item in sys.path)
    # This bounds V8 old-space, not the complete Deno/process RSS.
    environment['DENO_V8_FLAGS'] = '--max-old-space-size=64,--max-semi-space-size=4'
    mode = ['search'] if search else ['playlist'] if playlist else []
    arguments = [sys.executable, '-u', str(worker), url, str(destination)] + mode
    process = await asyncio.create_subprocess_exec(*arguments,
        stdout=asyncio.subprocess.PIPE, stderr=subprocess.DEVNULL, env=environment, **flags)
    exceeded = False

    async def watch_memory():
        nonlocal exceeded
        while True:
            await asyncio.sleep(0.5)
            usage = await asyncio.to_thread(memory_mib)
            if usage is not None and usage >= WORKER_MEMORY_MIB:
                exceeded = True
                log.warning('Stopping YouTube worker to protect Bot memory (tree_rss_mib=%.1f, budget_mib=%s)', usage, WORKER_MEMORY_MIB)
                await kill_download(process)
                return

    monitor = asyncio.create_task(watch_memory(), name='youtube-memory-guard')
    try:
        output, _ = await asyncio.wait_for(process.communicate(), timeout=DOWNLOAD_TIMEOUT)
    except (asyncio.TimeoutError, asyncio.CancelledError) as exc:
        await kill_download(process)
        if isinstance(exc, asyncio.CancelledError):
            raise
        raise SoundError('YouTubeの音声取得がタイムアウトしました。短い動画でもう一度試してください。') from None
    finally:
        monitor.cancel()
        await asyncio.gather(monitor, return_exceptions=True)
    if exceeded:
        raise SoundError('Botの停止を避けるため、メモリ使用量が増えたYouTube取得処理を中止しました。短い動画や音声ファイルを利用してください。')
    return output


class YouTubeAudio:
    download_timeout = 120

    def __init__(self, url, slots):
        self.url = normalize_url(url)
        self.slots = slots
        self.filename, self.size = 'YouTube音声.webm', 1

    async def save(self, destination):
        async with self.slots:
            output = await run_worker(self.url, destination)
            try:
                if len(output) > 8192:
                    raise ValueError()
                data = json.loads(output)
                if not data.get('ok'):
                    raise SoundError(ERRORS.get(data.get('reason'), ERRORS['unavailable']))
                path = Path(destination)
                self.size = path.stat().st_size
                if not 0 < self.size <= MAX_BYTES:
                    raise SoundError(ERRORS['size'])
                title = str(data.get('title') or 'YouTube音声')
                title = ''.join(c for c in title if ord(c) >= 32).replace('@', '＠')[:90]
                self.filename = title + '.webm'
            except (ValueError, KeyError, TypeError, AttributeError, OSError):
                raise SoundError(ERRORS['unavailable']) from None


def register_youtube_play(bot, sound):
    from music_queue import register_music
    return register_music(bot, sound)
