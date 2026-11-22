"""YouTube URL command feeding a local audio file to the existing voice player."""
import asyncio
import json
import os
from pathlib import Path
import re
import signal
import subprocess
import sys
from urllib.parse import parse_qs, urlsplit

import discord
from discord import app_commands
from sound import MAX_BYTES, SoundError

MAX_DURATION = 30 * 60
DOWNLOAD_TIMEOUT = 110
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
        if url.scheme != 'https' or url.username or url.password or url.port:
            raise ValueError()
        host = (url.hostname or '').lower()
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


class YouTubeAudio:
    download_timeout = 120

    def __init__(self, url, slots):
        self.url = normalize_url(url)
        self.slots = slots
        self.filename, self.size = 'YouTube音声.webm', 1

    async def save(self, destination):
        async with self.slots:
            worker = Path(__file__).with_name('youtube_worker.py')
            flags = ({'creationflags': subprocess.CREATE_NO_WINDOW | subprocess.CREATE_NEW_PROCESS_GROUP}
                     if os.name == 'nt' else {'start_new_session': True})
            environment = {key: value for key, value in os.environ.items()
                           if not any(tag in key.upper() for tag in ('TOKEN', 'SECRET', 'API_KEY'))}
            # MWS can add its .packages directory to sys.path at launch time.
            environment['PYTHONPATH'] = os.pathsep.join(os.path.abspath(item or os.getcwd()) for item in sys.path)
            process = await asyncio.create_subprocess_exec(sys.executable, '-u', str(worker), self.url, str(destination),
                stdout=asyncio.subprocess.PIPE, stderr=subprocess.DEVNULL, env=environment, **flags)
            try:
                output, _ = await asyncio.wait_for(process.communicate(), timeout=DOWNLOAD_TIMEOUT)
            except (asyncio.TimeoutError, asyncio.CancelledError) as exc:
                await kill_download(process)
                if isinstance(exc, asyncio.CancelledError):
                    raise
                raise SoundError('YouTubeの音声取得がタイムアウトしました。短い動画でもう一度試してください。') from None
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
    slots = asyncio.Semaphore(2)

    @bot.tree.command(name='play', description='YouTube動画の音楽を、このVCで再生します')
    @app_commands.guild_only()
    @app_commands.describe(url='YouTube動画のURL（30分以内・音声25MB以内）')
    async def play(interaction: discord.Interaction, url: str):
        try:
            audio = YouTubeAudio(url, slots)
        except SoundError as exc:
            await interaction.response.send_message(str(exc), ephemeral=True)
            return
        await sound.invoke(interaction, 'play', audio)
