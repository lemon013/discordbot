"""A single Discord PCM player with independently controlled audio lanes."""
import asyncio
from array import array
import os
from pathlib import Path
import sys
import tempfile
import threading

import discord

from sound import UploadedAudio, SoundError, MAX_BYTES, ffmpeg_executable, verify_audio, wait_for_voice_encryption


class Mixer(discord.AudioSource):
    def __init__(self, settings):
        self.settings = settings
        self.sources = {}
        self.paused = False
        self.closed = False
        self.lock = threading.RLock()

    def add(self, key, source):
        with self.lock:
            if self.closed:
                raise SoundError('VC接続が終了しました。もう一度接続してください。')
            if key == 'music' and key in self.sources:
                raise SoundError('音楽を再生中です。')
            if key.startswith('board:') and sum(k.startswith('board:') for k in self.sources) >= 3:
                raise SoundError('サウンドボードは同時に3件までです。')
            self.sources[key] = source

    def stop(self, key):
        with self.lock:
            source = self.sources.pop(key, None)
            if source:
                source.cleanup()
            if key == 'music':
                self.paused = False

    def read(self):
        with self.lock:
            if self.closed:
                return b''
            mixed = [0.0] * 1920  # 20 ms, stereo, 48 kHz, signed 16-bit PCM
            config = self.settings()
            duck = config['duck'] and 'tts' in self.sources
            for key, source in list(self.sources.items()):
                if key == 'music' and self.paused:
                    continue
                try:
                    data = source.read()
                except Exception as exc:
                    source.playback_error = type(exc).__name__
                    data = b''
                if not data:
                    error = getattr(source, '_current_error', None)
                    if error:
                        source.playback_error = type(error).__name__
                    self.stop(key)
                    continue
                lane = 'board' if key.startswith('board:') else key
                gain = config[lane + '_volume'] / 100
                if key == 'music' and duck:
                    gain *= config['duck_volume'] / 100
                samples = array('h', data[:3840])
                if sys.byteorder != 'little':
                    samples.byteswap()
                for index, value in enumerate(samples):
                    mixed[index] += value * gain
            result = array('h', (max(-32768, min(32767, round(value))) for value in mixed))
            if sys.byteorder != 'little':
                result.byteswap()
            return result.tobytes()

    def cleanup(self):
        with self.lock:
            self.closed = True
            for key in list(self.sources):
                self.stop(key)


class MusicVoice:
    """Expose lane controls without pausing speech or soundboard audio."""
    def __init__(self, hub, guild):
        self.hub, self.guild = hub, guild

    @property
    def channel(self):
        return self.guild.voice_client.channel

    def is_connected(self):
        return bool(self.guild.voice_client and self.guild.voice_client.is_connected())

    def is_paused(self):
        mixer = self.hub.mixers.get(self.guild.id)
        return bool(mixer and 'music' in mixer.sources and mixer.paused)

    def is_playing(self):
        mixer = self.hub.mixers.get(self.guild.id)
        return bool(mixer and 'music' in mixer.sources and not mixer.paused)

    def pause(self):
        self.hub.mixers[self.guild.id].paused = True

    def resume(self):
        self.hub.mixers[self.guild.id].paused = False

    def stop(self):
        mixer = self.hub.mixers.get(self.guild.id)
        if mixer:
            mixer.stop('music')


class VoiceHub:
    def __init__(self, sound, settings):
        self.sound, self.settings = sound, settings
        self.mixers = {}
        self.join_locks = {}
        self.reading = set()
        self.counter = 0
        self.generations = {}

    def music_voice(self, guild):
        return MusicVoice(self, guild)

    def busy(self, guild_id):
        mixer = self.mixers.get(guild_id)
        return guild_id in self.reading or bool(mixer and mixer.sources)

    async def ensure(self, guild, channel, expected=None):
        async with self.join_locks.setdefault(guild.id, asyncio.Lock()):
            if expected is not None and expected != self.generations.get(guild.id, 0):
                raise SoundError('VC退出により音声の準備を取り消しました。')
            if guild.id in self.sound.connecting:
                raise SoundError('前のVC接続の終了を待ってください。')
            voice = guild.voice_client
            if voice and voice.is_connected() and voice.channel.id != channel.id:
                raise SoundError('Botが参加しているVCで操作してください。')
            # PCM mixing needs the platform Opus encoder as well as FFmpeg.
            await asyncio.to_thread(ffmpeg_executable)
            def encoder():
                library = os.getenv('VOICE_OPUS_LIBRARY')
                if library and not discord.opus.is_loaded():
                    discord.opus.load_opus(library)
                try:
                    return discord.opus.Encoder()
                except discord.opus.OpusNotLoaded:
                    raise SoundError('同時再生用のOpusライブラリがありません。ホスト側でlibopusを導入するか、VOICE_OPUS_LIBRARYにライブラリのパスを設定してください。') from None
            await asyncio.to_thread(encoder)
            if voice and not voice.is_connected():
                await voice.disconnect(force=True)
                voice = None
            if voice is None:
                voice = await self.sound.connect_voice(guild, channel)
            await wait_for_voice_encryption(voice)
            if expected is not None and expected != self.generations.get(guild.id, 0):
                await voice.disconnect(force=True)
                raise SoundError('VC退出により音声の準備を取り消しました。')
            mixer = self.mixers.get(guild.id)
            if not mixer or mixer.closed or not voice.is_playing():
                if mixer:
                    mixer.cleanup()
                self.settings(guild.id)
                mixer = Mixer(lambda: self.settings(guild.id))
                self.mixers[guild.id] = mixer
                voice.play(mixer)
            self.sound.cancel_idle(guild.id)
            return mixer

    async def play(self, interaction, channel, attachment, lane='music'):
        guild = interaction.guild
        generation = self.generations.get(guild.id, 0)
        if lane == 'music' and guild.id in self.sound.playing:
            raise SoundError('音楽を再生中です。終了を待つか停止してください。')
        if not 0 < attachment.size <= MAX_BYTES:
            raise SoundError('音声ファイルは25MB以内で指定してください。')
        directory = tempfile.TemporaryDirectory(prefix='discord-mix-')
        source, error_output = None, None
        complete = asyncio.Event()
        loop = asyncio.get_running_loop()
        try:
            path = Path(directory.name) / ('audio' + Path(attachment.filename).suffix.lower())
            await asyncio.wait_for(attachment.save(path), getattr(attachment, 'download_timeout', 30))
            if not 0 < path.stat().st_size <= MAX_BYTES:
                raise SoundError('音声ファイルは25MB以内で指定してください。')
            executable = await asyncio.to_thread(ffmpeg_executable)
            await verify_audio(executable, path)
            self.sound.target(interaction)
            permissions = channel.permissions_for(guild.me)
            if not (permissions.view_channel and permissions.connect and permissions.speak):
                raise SoundError('BotにVCの接続・発言権限が必要です。')
            mixer = await self.ensure(guild, channel, expected=generation)
            def finished(audio):
                def dispatch():
                    complete.set()
                    if lane == 'music':
                        asyncio.create_task(self.sound.finished(guild, audio))
                    else:
                        self.arm_idle(guild)
                try:
                    loop.call_soon_threadsafe(dispatch)
                except RuntimeError:
                    pass
            error_output = open(os.devnull, 'wb')
            tempo = float(getattr(attachment, 'tempo', 1.0))
            if not 0.5 <= tempo <= 2.0:
                raise SoundError('読み上げ速度は0.5〜2.0倍で指定してください。')
            options = '-vn -map 0:a:0' + (f' -af atempo={tempo:g}' if tempo != 1.0 else '')
            inner = discord.FFmpegPCMAudio(str(path), executable=executable,
                before_options='-nostdin -protocol_whitelist file,pipe', options=options, stderr=error_output)
            source = UploadedAudio(inner, directory, finished, stderr=error_output)
            source.ticket_award = getattr(attachment, 'ticket_award', None)
            source.guild_id, source.title = guild.id, attachment.filename
            self.counter += 1
            key = f'board:{self.counter}' if lane == 'board' else lane
            mixer.add(key, source)
            if lane == 'music':
                self.sound.playing[guild.id] = source
            return complete, key
        except BaseException:
            if source:
                source.cleanup()
            else:
                if error_output:
                    error_output.close()
                directory.cleanup()
            self.arm_idle(guild)
            raise

    def arm_idle(self, guild):
        self.sound.cancel_idle(guild.id)
        self.sound.idle_tasks[guild.id] = asyncio.create_task(self.sound.idle_disconnect(guild))

    def stop(self, guild_id, lane='tts'):
        mixer = self.mixers.get(guild_id)
        if mixer:
            mixer.stop(lane)

    def close(self, guild_id):
        self.generations[guild_id] = self.generations.get(guild_id, 0) + 1
        mixer = self.mixers.pop(guild_id, None)
        if mixer:
            mixer.cleanup()
        self.reading.discard(guild_id)

    async def board(self, interaction, clip):
        await interaction.response.send_message('🔊 音声を準備します。', ephemeral=True)
        try:
            channel = self.sound.target(interaction)
            await self.play(interaction, channel, clip, 'board')
            text = '🔊 サウンドボードを再生します。'
        except Exception as exc:
            text = str(exc) if isinstance(exc, SoundError) else '音声を再生できませんでした。'
        await self.sound.status(interaction, text)
