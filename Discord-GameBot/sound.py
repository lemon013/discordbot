"""Play an attached audio file in the voice channel containing its command."""
import asyncio
import importlib
import io
import logging
import math
import os
from pathlib import Path
import subprocess
import struct
import tempfile
import threading
import wave
from types import SimpleNamespace
from importlib.metadata import version, PackageNotFoundError

import discord
from discord import app_commands

log = logging.getLogger(__name__)
MAX_BYTES = 25 * 1024 * 1024
CONNECT_TIMEOUT = 20
DAVE_READY_TIMEOUT = 10
EXTENSIONS = {'.mp3', '.wav', '.ogg', '.opus', '.m4a', '.aac', '.flac', '.webm', '.mp4'}


class SoundError(Exception):
    pass


def make_test_tone():
    """Three seconds of moderate-volume 440 Hz audio, independent of user files."""
    buffer = io.BytesIO()
    with wave.open(buffer, 'wb') as audio:
        audio.setnchannels(1)
        audio.setsampwidth(2)
        audio.setframerate(48000)
        audio.writeframes(b''.join(struct.pack('<h', int(8000 * math.sin(2 * math.pi * 440 * n / 48000)
            * min(1, n / 960, (144000 - n) / 960))) for n in range(144000)))
    data = buffer.getvalue()
    async def save(path):
        return await asyncio.to_thread(Path(path).write_bytes, data)
    return SimpleNamespace(filename='sound-test-440Hz.wav', size=len(data), save=save)


async def wait_for_voice_encryption(voice):
    """Do not start media before the SDK has negotiated its required DAVE keys."""
    state = getattr(voice, '_connection', None)
    if state is None:
        return
    deadline = asyncio.get_running_loop().time() + DAVE_READY_TIMEOUT
    while getattr(state, 'dave_protocol_version', 0) > 0 and not state.can_encrypt:
        if not voice.is_connected():
            raise SoundError('暗号化の準備中にVC接続が切れました。')
        if asyncio.get_running_loop().time() >= deadline:
            raise SoundError('VCには接続しましたが、音声の暗号化（DAVE）の準備が完了しませんでした。/sound status の診断結果を確認してください。')
        await asyncio.sleep(0.1)


def ffmpeg_executable():
    try:
        importlib.import_module('nacl')
        importlib.import_module('davey')
        installed_davey = version('davey')
        if discord.__version__ != '2.7.1' or not installed_davey.startswith(('0.1.', '0.2.')):
            raise SoundError(f'音声ライブラリの組み合わせが未対応です（discord.py={discord.__version__}、davey={installed_davey}）。'
                             '最新版requirements.txtで再インストールしてください。動作確認版はdiscord.py 2.7.1とdavey 0.2.0です。')
        from imageio_ffmpeg import get_ffmpeg_exe
        return get_ffmpeg_exe()
    except (ImportError, RuntimeError, PackageNotFoundError):
        raise SoundError('音声再生の準備ができていません。最新版のrequirements.txtでライブラリをインストールし、Botを再起動してください。') from None


async def verify_audio(executable, path):
    """Check an audio stream before joining; never pass a user URL to FFmpeg."""
    process = await asyncio.create_subprocess_exec(
        executable, '-nostdin', '-v', 'error', '-protocol_whitelist', 'file,pipe',
        '-i', str(path), '-map', '0:a:0', '-t', '0.1', '-f', 'null', '-',
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        **({'creationflags': subprocess.CREATE_NO_WINDOW} if os.name == 'nt' else {}))
    try:
        await asyncio.wait_for(process.wait(), timeout=15)
    except (asyncio.TimeoutError, asyncio.CancelledError):
        if process.returncode is None:
            process.kill()
        await process.wait()
        raise
    if process.returncode:
        raise SoundError('音声を読み取れませんでした。壊れていない音声ファイルを添付してください。')


class UploadedAudio(discord.AudioSource):
    """Own the temporary directory until discord.py releases the audio source."""
    def __init__(self, source, directory, finished, stderr=None):
        self.source, self.directory, self.finished = source, directory, finished
        self.stderr = stderr
        self._cleanup_lock = threading.Lock()
        self._cleaned = False
        self.frames = self.audio_bytes = self.skipped_headers = 0
        self.guild_id = None
        self.playback_error = None

    def read(self):
        data = self.source.read()
        while data.startswith((b'OpusHead', b'OpusTags')):
            self.skipped_headers += 1
            data = self.source.read()
        if data:
            self.frames += 1
            self.audio_bytes += len(data)
            if self.frames == 1:
                log.info('Sound audio frames started (guild_id=%s)', self.guild_id)
        return data

    @property
    def _current_error(self):
        return getattr(self.source, '_current_error', None)

    def is_opus(self):
        return self.source.is_opus()

    def cleanup(self):
        with self._cleanup_lock:
            if self._cleaned:
                return
            self._cleaned = True
            try:
                self.source.cleanup()
            finally:
                try:
                    if self.stderr is not None:
                        self.stderr.close()
                finally:
                    try:
                        self.directory.cleanup()
                    finally:
                        self.finished(self)


class Sound:
    def __init__(self, bot):
        self.bot = bot
        self.locks, self.playing, self.idle_tasks = {}, {}, {}
        self.preparing, self.connecting = set(), {}
        self.last_stats = {}

    def lock(self, guild_id):
        return self.locks.setdefault(guild_id, asyncio.Lock())

    def target(self, interaction):
        if interaction.guild is None or not isinstance(interaction.channel, discord.VoiceChannel):
            raise SoundError('音声を流したいVC内のチャットで実行してください。')
        user_voice = getattr(interaction.user, 'voice', None)
        if not user_voice or not user_voice.channel or user_voice.channel.id != interaction.channel.id:
            raise SoundError('このVCに参加してから実行してください。')
        return interaction.channel

    def cancel_idle(self, guild_id):
        task = self.idle_tasks.pop(guild_id, None)
        if task and task is not asyncio.current_task():
            task.cancel()

    async def status(self, interaction, text):
        try:
            await asyncio.wait_for(interaction.edit_original_response(
                content=text, allowed_mentions=discord.AllowedMentions.none()), timeout=5)
        except (discord.DiscordException, asyncio.TimeoutError):
            log.warning('Could not update sound command status (guild_id=%s)', interaction.guild_id)

    async def cleanup_connection(self, guild, attempt):
        voice = None
        try:
            try:
                voice = await attempt
            except (asyncio.CancelledError, Exception):
                pass
            failed = guild.voice_client or voice
            if failed:
                await failed.disconnect(force=True)
        except Exception:
            log.warning('Could not clean failed voice connection (guild_id=%s)', guild.id)
        finally:
            if self.connecting.get(guild.id) is asyncio.current_task():
                self.connecting.pop(guild.id, None)

    async def connect_voice(self, guild, channel):
        # wait_for waits for SDK cancellation/cleanup; that can itself take another
        # connection timeout. Return the error first and finish cleanup separately.
        log.info('Starting sound voice connection (guild_id=%s, channel_id=%s, timeout=%s)',
                 guild.id, channel.id, CONNECT_TIMEOUT)
        from voice_compat import CompatibleVoiceClient
        attempt = asyncio.create_task(channel.connect(
            timeout=CONNECT_TIMEOUT, reconnect=False, self_deaf=True, cls=CompatibleVoiceClient))
        self.connecting[guild.id] = attempt
        try:
            done, _ = await asyncio.wait({attempt}, timeout=CONNECT_TIMEOUT)
            if not done:
                raise asyncio.TimeoutError()
            voice = attempt.result()
        except BaseException:
            log.warning('Sound voice connection did not complete (guild_id=%s)', guild.id)
            attempt.cancel()
            self.connecting[guild.id] = asyncio.create_task(self.cleanup_connection(guild, attempt))
            raise
        self.connecting.pop(guild.id, None)
        return voice

    async def finished(self, guild, source):
        async with self.lock(guild.id):
            if self.playing.get(guild.id) is not source:
                return
            self.playing.pop(guild.id, None)
            self.last_stats[guild.id] = (source.frames, source.audio_bytes, source.skipped_headers, source.playback_error)
            log.info('Sound audio completed (guild_id=%s, frames=%s, bytes=%s, skipped_headers=%s, error=%s)',
                     guild.id, *self.last_stats[guild.id])
            self.cancel_idle(guild.id)
            self.idle_tasks[guild.id] = asyncio.create_task(self.idle_disconnect(guild))

    def playback_status(self, guild):
        voice = guild.voice_client
        state = getattr(voice, '_connection', None)
        required = getattr(state, 'dave_protocol_version', 0) > 0
        encryption = ('準備済み' if getattr(state, 'can_encrypt', False) else '準備未完了') if required else 'DAVE不要または情報なし'
        source = self.playing.get(guild.id)
        stats = ((source.frames, source.audio_bytes, source.skipped_headers, source.playback_error)
                 if source else self.last_stats.get(guild.id, (0, 0, 0, None)))
        dependencies = []
        for package in ('discord.py', 'davey'):
            try:
                dependencies.append(f'{package}={version(package)}')
            except PackageNotFoundError:
                dependencies.append(f'{package}=未導入')
        return (f'VC接続：{"接続済み" if voice and voice.is_connected() else "未接続"}\n'
                'DAVE互換処理：0.2系の鍵切り替え対応版\n'
                f'DAVE暗号化：{encryption}\n'
                f'{"再生中" if source else "直近の再生"}：読込 {stats[0]}フレーム／{stats[1]}バイト\n'
                f'除外したヘッダー：{stats[2]}／再生エラー：{stats[3] or "なし"}\n'
                + '、'.join(dependencies) + '\n※読込数は参加者への到達・聴取を保証するものではありません。')

    async def idle_disconnect(self, guild):
        try:
            await asyncio.sleep(60)
            async with self.lock(guild.id):
                voice = guild.voice_client
                if (voice and voice.is_connected() and guild.id not in self.playing
                        and guild.id not in self.preparing and guild.id not in self.connecting
                        and not voice.is_playing() and not voice.is_paused()):
                    await voice.disconnect(force=True)
        except asyncio.CancelledError:
            pass
        except discord.DiscordException:
            log.warning('Could not leave idle voice channel (guild_id=%s)', guild.id)
        finally:
            if self.idle_tasks.get(guild.id) is asyncio.current_task():
                self.idle_tasks.pop(guild.id, None)

    async def play(self, interaction, channel, attachment):
        suffix = Path(attachment.filename).suffix.lower()
        if suffix not in EXTENSIONS:
            raise SoundError('MP3・WAV・OGG・OPUS・M4A・AAC・FLAC・WEBM・MP4のファイルを添付してください。MP4は音声のみ再生します。')
        if attachment.size <= 0 or attachment.size > MAX_BYTES:
            raise SoundError('音声ファイルは25MB以内で添付してください。')
        permissions = channel.permissions_for(interaction.guild.me)
        if not (permissions.view_channel and permissions.connect and permissions.speak):
            raise SoundError('BotにこのVCの「チャンネルを見る」「接続」「発言」権限が必要です。')
        guild = interaction.guild
        voice = guild.voice_client
        if voice and voice.is_connected() and voice.channel.id != channel.id:
            raise SoundError('Botは別のVCで使用中です。先にそのVCで /sound leave を実行してください。')
        if guild.id in self.playing or (voice and (voice.is_playing() or voice.is_paused())):
            raise SoundError('再生中です。終了を待つか /sound stop で停止してから再生してください。')
        if guild.id in self.connecting:
            raise SoundError('前回のVC接続を終了処理中です。終了を待ってから再試行してください。')
        executable = await asyncio.to_thread(ffmpeg_executable)
        directory = tempfile.TemporaryDirectory(prefix='discord-sound-')
        source = None
        error_output = None
        started = False
        joined = False
        self.preparing.add(guild.id)
        self.cancel_idle(guild.id)
        try:
            path = Path(directory.name) / ('audio' + suffix)
            await self.status(interaction, '📥 添付ファイルを取得しています。')
            await asyncio.wait_for(attachment.save(path), timeout=getattr(attachment, 'download_timeout', 30))
            if path.stat().st_size <= 0 or path.stat().st_size > MAX_BYTES:
                raise SoundError('音声ファイルは25MB以内で添付してください。')
            await self.status(interaction, '🎵 ファイルの音声を確認しています。MP4は音声のみ使用します。')
            await verify_audio(executable, path)
            # Recheck membership after downloading/validating the file.
            self.target(interaction)
            if voice and not voice.is_connected():
                await voice.disconnect(force=True)
                voice = None
            if voice is None:
                await self.status(interaction, '🔊 VCに接続しています（最大20秒）。')
                voice = await self.connect_voice(guild, channel)
                joined = True
            await self.status(interaction, '🔐 音声の暗号化準備を確認しています。')
            await wait_for_voice_encryption(voice)
            loop = asyncio.get_running_loop()

            def cleanup_complete(audio):
                try:
                    loop.call_soon_threadsafe(lambda: asyncio.create_task(self.finished(guild, audio)))
                except RuntimeError:
                    pass  # Bot shutdown: the audio and temporary file are already released.

            # discord.py expects a writable file object, not the integer DEVNULL
            # sentinel: otherwise its stderr reader calls (-3).write(...) and fails.
            error_output = open(os.devnull, 'wb')
            inner = discord.FFmpegOpusAudio(
                str(path), executable=executable, bitrate=128,
                before_options='-nostdin -protocol_whitelist file,pipe',
                options='-vn -map 0:a:0 -frame_duration 20 -application audio', stderr=error_output)
            source = UploadedAudio(inner, directory, cleanup_complete, stderr=error_output)
            source.guild_id = guild.id

            def after(error):
                if error:
                    source.playback_error = type(error).__name__
                    log.warning('Voice playback failed (guild_id=%s, error_type=%s)', guild.id, type(error).__name__)

            self.playing[guild.id] = source
            log.info('Sound playback starting (guild_id=%s, dave_required=%s, dave_ready=%s)', guild.id,
                     getattr(getattr(voice, '_connection', None), 'dave_protocol_version', 0) > 0,
                     bool(getattr(getattr(voice, '_connection', None), 'can_encrypt', False)))
            self.cancel_idle(guild.id)
            voice.play(source, after=after)
            started = True
        finally:
            self.preparing.discard(guild.id)
            if not started:
                self.playing.pop(guild.id, None)
                if source:
                    source.cleanup()
                else:
                    if error_output is not None:
                        error_output.close()
                    directory.cleanup()
                if joined and voice:
                    completed = asyncio.get_running_loop().create_future()
                    completed.set_result(voice)
                    self.connecting[guild.id] = asyncio.create_task(self.cleanup_connection(guild, completed))
                elif voice and voice.is_connected() and guild.id not in self.connecting:
                    self.cancel_idle(guild.id)
                    self.idle_tasks[guild.id] = asyncio.create_task(self.idle_disconnect(guild))
        filename = discord.utils.escape_markdown(attachment.filename[:100])
        return f'▶️ {filename} を再生しています。再生終了後、60秒で自動退出します。'

    async def invoke(self, interaction, action, attachment=None):
        # Resolve Discord's "thinking" indicator before file/network preparation.
        await interaction.response.send_message(
            '音声の準備を始めます。' if action in ('play', 'test') else '音声の操作を受け付けました。',
            ephemeral=True, allowed_mentions=discord.AllowedMentions.none())
        try:
            channel = self.target(interaction)
            if action == 'status':
                await self.status(interaction, self.playback_status(interaction.guild))
                return
            lock = self.lock(interaction.guild.id)
            if lock.locked():
                raise SoundError('音声の準備・操作中です。表示が更新されるまでお待ちください。')
            async with lock:
                if action in ('play', 'test'):
                    if action == 'test':
                        attachment = await asyncio.to_thread(make_test_tone)
                    answer = await self.play(interaction, channel, attachment)
                else:
                    voice = interaction.guild.voice_client
                    if not voice or not voice.is_connected():
                        raise SoundError('BotはVCに接続していません。')
                    if voice.channel.id != channel.id:
                        raise SoundError('Botが参加しているVCのチャットで実行してください。')
                    if action == 'stop':
                        voice.stop()
                        self.cancel_idle(interaction.guild.id)
                        self.idle_tasks[interaction.guild.id] = asyncio.create_task(self.idle_disconnect(interaction.guild))
                        answer = '⏹️ 再生を停止しました。60秒で自動退出します。'
                    else:
                        self.cancel_idle(interaction.guild.id)
                        self.playing.pop(interaction.guild.id, None)
                        await voice.disconnect(force=True)
                        answer = 'VCから退出しました。'
        except SoundError as exc:
            answer = str(exc)
        except asyncio.TimeoutError:
            answer = ('音声の取得・確認、またはVC接続がタイムアウトしました。接続失敗後の退出であり、自動退出ではありません。'
                      'MWSの音声対応ネットワーク・Discord音声サーバーへの通信（UDPを含む）を確認してください。終了処理はバックグラウンドで行います。')
        except (discord.DiscordException, OSError, RuntimeError):
            log.warning('Sound command failed (action=%s, guild_id=%s)', action, interaction.guild_id)
            answer = '音声再生に失敗しました。BotのVC権限、音声用ライブラリ、MWSの音声対応ネットワーク設定を確認してください。'
        await self.status(interaction, answer)


def register_sound(bot):
    service = Sound(bot)
    group = app_commands.Group(name='sound', description='VC内チャットで添付した音声ファイルを再生', guild_only=True)

    @group.command(name='play', description='このVCに参加して、添付した音声ファイルを再生します')
    @app_commands.describe(file='再生する音声ファイル・MP4動画（音声のみ／25MB以内）')
    async def play(interaction: discord.Interaction, file: discord.Attachment):
        await service.invoke(interaction, 'play', file)

    @group.command(name='stop', description='このVCでの音声再生を停止します')
    async def stop(interaction: discord.Interaction):
        await service.invoke(interaction, 'stop')

    @group.command(name='leave', description='BotをこのVCから退出させます')
    async def leave(interaction: discord.Interaction):
        await service.invoke(interaction, 'leave')

    @group.command(name='test', description='このVCで3秒間のテスト音を再生します')
    async def test(interaction: discord.Interaction):
        await service.invoke(interaction, 'test')

    @group.command(name='status', description='音声の読込フレーム数と暗号化の状態を確認します')
    async def status(interaction: discord.Interaction):
        await service.invoke(interaction, 'status')

    bot.tree.add_command(group)
    return service
