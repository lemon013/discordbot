"""Opt-in VC text reading, provider synthesis, and persistent voice preferences."""
import asyncio
import base64
from contextlib import closing
import json
import logging
import os
from pathlib import Path
import re
import sqlite3
import time
from types import SimpleNamespace
import unicodedata

import aiohttp
import discord
from discord import app_commands

from sound import SoundError, MAX_BYTES
from storage import database_path
from voice_mixer import VoiceHub

log = logging.getLogger(__name__)
CREDIT = 'クレジットはBotのプロフィール欄を参照してください。'


def short_display(text):
    return text.encode('utf-16-le')[:3800].decode('utf-16-le', errors='ignore')


DEFAULT = dict(music_volume=70, tts_volume=100, board_volume=80, duck=True, duck_volume=30,
               names=False, urls='label', emoji=False, attachments=True, bots=False, max_chars=150)


class VoiceStore:
    def __init__(self, path=None):
        self.path = Path(path) if path else database_path('level')
        with closing(sqlite3.connect(self.path)) as db:
            db.execute('CREATE TABLE IF NOT EXISTS voice_preferences (scope TEXT, item TEXT, value TEXT NOT NULL, PRIMARY KEY(scope,item))')
            db.commit()

    def get(self, scope, item, default):
        with closing(sqlite3.connect(self.path)) as db:
            row = db.execute('SELECT value FROM voice_preferences WHERE scope=? AND item=?', (scope, str(item))).fetchone()
        return json.loads(row[0]) if row else dict(default)

    def put(self, scope, item, value):
        with closing(sqlite3.connect(self.path)) as db:
            db.execute('INSERT OR REPLACE INTO voice_preferences VALUES(?,?,?)', (scope, str(item), json.dumps(value)))
            db.commit()


class Providers:
    def __init__(self):
        self.base = os.getenv('VOICEVOX_URL', '').rstrip('/')
        self.key = os.getenv('GOOGLE_TTS_API_KEY', '')
        self.cache = {}
        self.credentials = None
        self.auth_lock = asyncio.Lock()
        self.slots = asyncio.Semaphore(2)

    async def headers(self):
        if self.key:
            return {'x-goog-api-key': self.key}
        def authorize():
            import google.auth
            from google.auth.transport.requests import Request
            if self.credentials is None:
                self.credentials, _ = google.auth.default(scopes=['https://www.googleapis.com/auth/cloud-platform'])
            if not self.credentials.valid:
                request = Request()
                self.credentials.refresh(lambda *args, **kwargs: request(*args, **(kwargs | {'timeout': 15})))
            return {'Authorization': 'Bearer ' + self.credentials.token}
        async with self.auth_lock:
            return await asyncio.to_thread(authorize)

    async def request(self, method, url, **kwargs):
        async with self.slots:
            async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=20)) as client:
                async with client.request(method, url, allow_redirects=False, **kwargs) as response:
                    if response.status != 200:
                        raise SoundError('音声サービスへの接続・認証に失敗しました。管理者に設定の確認を依頼してください。')
                    data = bytearray()
                    async for chunk in response.content.iter_chunked(65536):
                        data.extend(chunk)
                        if len(data) > MAX_BYTES:
                            raise SoundError('音声サービスの応答が大きすぎます。')
                    return bytes(data)

    async def voices(self, provider):
        cached = self.cache.get(provider)
        if cached and time.monotonic() - cached[0] < 600:
            return cached[1]
        if provider == 'voicevox':
            if not self.base:
                raise SoundError('VOICEVOX_URLが未設定です。管理者が音声エンジンの接続先を設定してください。')
            data = json.loads(await self.request('GET', self.base + '/speakers'))
            voices = {str(style['id']): f'{speaker["name"]}（{style["name"]}）'
                      for speaker in data if speaker['name'] in ('ずんだもん', '四国めたん')
                      for style in speaker['styles']}
        else:
            data = json.loads(await self.request('GET', 'https://texttospeech.googleapis.com/v1/voices',
                params={'languageCode': 'ja-JP'}, headers=await self.headers()))
            # Standard/WaveNet support the requested speakingRate range.
            voices = {v['name']: v['name'] for v in data.get('voices', [])
                      if 'ja-JP' in v.get('languageCodes', []) and
                      ('-Standard-' in v['name'] or '-Wavenet-' in v['name'])}
        if not voices:
            raise SoundError('利用できる日本語音声がありません。')
        self.cache[provider] = time.monotonic(), voices
        return voices

    async def synthesize(self, text, preference):
        provider, voice, speed = preference['provider'], preference['voice'], preference['speed']
        catalog = await self.voices(provider)
        if voice not in catalog:
            raise SoundError('保存した声が見つかりません。/voice change で選び直してください。')
        if provider == 'voicevox':
            query = json.loads(await self.request('POST', self.base + '/audio_query',
                                                  params={'text': text, 'speaker': voice}))
            query['speedScale'] = speed
            return await self.request('POST', self.base + '/synthesis', params={'speaker': voice}, json=query)
        data = json.loads(await self.request('POST', 'https://texttospeech.googleapis.com/v1/text:synthesize',
            headers=await self.headers(), json={'input': {'text': text},
                'voice': {'languageCode': 'ja-JP', 'name': voice},
                'audioConfig': {'audioEncoding': 'LINEAR16', 'speakingRate': speed}}))
        audio = base64.b64decode(data['audioContent'], validate=True)
        if not 0 < len(audio) <= MAX_BYTES:
            raise SoundError('生成した音声のサイズが不正です。')
        return audio


def reading_text(message, config, dictionary):
    text = message.clean_content
    text = re.sub(r'```[\s\S]*?```', ' コード省略 ', text)
    text = re.sub(r'\|\|[\s\S]*?\|\|', ' ネタバレ省略 ', text)
    if config['urls'] != 'read':
        text = re.sub(r'https?://\S+', ' リンク ' if config['urls'] == 'label' else ' ', text)
    if not config['emoji']:
        text = re.sub(r'<a?:\w+:\d+>', '', text)
        text = ''.join(c for c in text if unicodedata.category(c) not in ('So', 'Sk') and c not in '\ufe0f\u200d')
    if config['attachments'] and message.attachments:
        text += ' 添付ファイル'
    # One simultaneous substitution prevents dictionary replacements cascading.
    if dictionary:
        pattern = '|'.join(re.escape(key) for key in sorted(dictionary, key=len, reverse=True))
        text = re.sub(pattern, lambda match: dictionary[match.group()], text)
    text = ' '.join(text.split())
    if not text:
        return ''
    if config['names']:
        text = message.author.display_name + '、' + text
    return text[:config['max_chars']]


class Reading:
    def __init__(self, bot, sound, store=None, providers=None):
        self.bot, self.sound = bot, sound
        self.store, self.providers = store or VoiceStore(), providers or Providers()
        self.configs, self.sessions, self.jobs, self.workers, self.cooldowns = {}, {}, {}, {}, {}
        self.current = {}
        self.hub = sound.hub = VoiceHub(sound, self.config)
        sound.tts = self

    def config(self, guild_id):
        if guild_id not in self.configs:
            self.configs[guild_id] = DEFAULT | self.store.get('guild', guild_id, {})
        return self.configs[guild_id]

    def preference(self, uid):
        preference = self.store.get('user', uid, dict(provider='voicevox', voice='3', speed=1.0))
        if preference['provider'] != 'voicevox':
            preference = dict(provider='voicevox', voice='3', speed=preference.get('speed', 1.0))
        return preference

    def channel(self, interaction):
        state = getattr(interaction.user, 'voice', None)
        if not interaction.guild or not state or not isinstance(state.channel, discord.VoiceChannel):
            raise SoundError('VCに参加してから実行してください。')
        actual = interaction.guild.voice_client
        if actual and actual.is_connected() and actual.channel.id != state.channel.id:
            raise SoundError('Botが参加しているVCで操作してください。')
        return state.channel

    def admin(self, interaction):
        if not interaction.user.guild_permissions.manage_guild:
            raise SoundError('サーバー管理権限が必要です。')

    async def halt(self, guild_id):
        self.sessions.pop(guild_id, None)
        self.hub.reading.discard(guild_id)
        task = self.workers.pop(guild_id, None)
        if task:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        self.hub.stop(guild_id)
        self.jobs.pop(guild_id, None)

    async def start(self, interaction):
        channel = self.channel(interaction)
        generation = self.hub.generations.get(interaction.guild.id, 0)
        permissions = channel.permissions_for(interaction.guild.me)
        if not (permissions.view_channel and permissions.connect and permissions.speak):
            raise SoundError('BotにVCの接続・発言権限が必要です。')
        preference = self.preference(interaction.user.id)
        await self.providers.voices(preference['provider'])
        async with self.sound.lock(interaction.guild.id):
            self.channel(interaction)
            await self.hub.ensure(interaction.guild, channel, expected=generation)
            self.sessions[interaction.guild.id] = channel.id
            self.hub.reading.add(interaction.guild.id)
        return '🔊 このVCのチャットの読み上げを開始しました。' + CREDIT

    async def on_message(self, message):
        guild = message.guild
        if not guild or self.sessions.get(guild.id) != message.channel.id:
            return
        if message.author.id == self.bot.user.id:
            return
        config = self.config(guild.id)
        if message.author.bot and not config['bots']:
            return
        state = getattr(message.author, 'voice', None)
        if not state or not state.channel or state.channel.id != message.channel.id:
            return
        if message.content.startswith(('/', './', '!')):
            return
        voice = guild.voice_client
        if not voice or not voice.is_connected() or voice.channel.id != message.channel.id:
            await self.halt(guild.id)
            return
        queue = self.jobs.setdefault(guild.id, asyncio.Queue(maxsize=30))
        if queue.full():
            return
        text = reading_text(message, config, self.store.get('dictionary', guild.id, {}))
        if text:
            queue.put_nowait((message, text, time.monotonic()))
        if guild.id not in self.workers or self.workers[guild.id].done():
            self.workers[guild.id] = asyncio.create_task(self.consume(guild), name='voice-reading')

    async def speak(self, guild, message, text):
        data = await self.providers.synthesize(text, self.preference(message.author.id))
        async def save(path):
            await asyncio.to_thread(Path(path).write_bytes, data)
        attachment = SimpleNamespace(filename='reading.wav', size=len(data), save=save)
        state = getattr(message.author, 'voice', None)
        if self.sessions.get(guild.id) != message.channel.id or not state or not state.channel or state.channel.id != message.channel.id:
            return
        context = SimpleNamespace(guild=guild, channel=message.channel, user=message.author)
        event, key = await self.hub.play(context, message.channel, attachment, 'tts')
        try:
            await asyncio.wait_for(event.wait(), 180)
        finally:
            self.hub.stop(guild.id, key)

    async def consume(self, guild):
        queue = self.jobs[guild.id]
        while self.sessions.get(guild.id):
            try:
                message, text, added = await queue.get()
                if time.monotonic() - added > 60:
                    continue
                job = asyncio.create_task(self.speak(guild, message, text))
                self.current[guild.id] = job
                try:
                    await job
                except asyncio.CancelledError:
                    if asyncio.current_task().cancelling():
                        raise
                finally:
                    self.current.pop(guild.id, None)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                log.warning('Voice synthesis failed (guild_id=%s, error_type=%s)', guild.id, type(exc).__name__)
                last = self.cooldowns.get(guild.id, 0)
                if time.monotonic() - last > 60:
                    self.cooldowns[guild.id] = time.monotonic()
                    try:
                        await message.channel.send('読み上げ音声を生成できませんでした。/voice change の音声設定と音声サービスの設定を確認してください。', allowed_mentions=discord.AllowedMentions.none())
                    except discord.DiscordException:
                        pass

    async def on_voice_state_update(self, member, before, after):
        if member.id == self.bot.user.id and before.channel and (not after.channel or before.channel.id != after.channel.id):
            await self.halt(member.guild.id)
            if self.sound.music:
                await self.sound.music.cancel_pending(member.guild.id, before.channel.id)
            self.hub.close(member.guild.id)


def register_voice(bot, sound):
    service = Reading(bot, sound)
    group = app_commands.Group(name='voice', description='VCチャットの読み上げと設定', guild_only=True)

    async def run(interaction, operation):
        await interaction.response.defer(ephemeral=True, thinking=True)
        try:
            text = await asyncio.wait_for(operation(), 45)
        except Exception as exc:
            log.warning('Voice command failed (error_type=%s)', type(exc).__name__)
            text = str(exc) if isinstance(exc, SoundError) else '音声サービスを利用できません。接続先・認証・Opusライブラリの設定を確認してください。'
        await sound.status(interaction, text)

    @group.command(name='join', description='参加中のVCでチャット読み上げを開始・再開します')
    async def join(interaction: discord.Interaction):
        await run(interaction, lambda: service.start(interaction))

    @group.command(name='stop', description='読み上げと待機文だけ停止します。音楽とVC接続は継続します')
    async def stop(interaction: discord.Interaction):
        async def op():
            service.channel(interaction)
            await service.halt(interaction.guild.id)
            service.hub.arm_idle(interaction.guild)
            return '読み上げと待機文を停止しました。/voice join で再開できます。'
        await run(interaction, op)

    @group.command(name='leave', description='読み上げ・音楽を停止してVCから退出します')
    async def leave(interaction: discord.Interaction):
        async def op():
            channel = service.channel(interaction)
            await service.halt(interaction.guild.id)
            if sound.music:
                await sound.music.cancel_pending(interaction.guild.id, channel.id)
            async with sound.lock(interaction.guild.id):
                service.hub.close(interaction.guild.id)
                sound.cancel_idle(interaction.guild.id)
                sound.playing.pop(interaction.guild.id, None)
                if interaction.guild.voice_client:
                    await interaction.guild.voice_client.disconnect(force=True)
            return '読み上げ・音楽を停止してVCから退出しました。'
        await run(interaction, op)

    @group.command(name='change', description='自分の読み上げ音声と速度を変更します')
    @app_commands.choices(provider=[app_commands.Choice(name='VOICEVOX', value='voicevox')])
    async def change(interaction: discord.Interaction, provider: str | None = None, voice: str | None = None,
                     speed: app_commands.Range[float, 0.5, 2.0] | None = None):
        async def op():
            preference = service.preference(interaction.user.id)
            chosen = provider or preference['provider']
            if chosen != 'voicevox':
                raise SoundError('現在はVOICEVOX専用です。Google音声は使用しません。')
            catalog = await service.providers.voices(chosen)
            selected = voice or (preference['voice'] if chosen == preference['provider'] and preference['voice'] in catalog else next(iter(catalog)))
            if selected not in catalog:
                raise SoundError('音声欄の候補から声を選んでください。')
            preference.update(provider=chosen, voice=selected, speed=speed if speed is not None else preference['speed'])
            await asyncio.to_thread(service.store.put, 'user', interaction.user.id, preference)
            return f'音声：{chosen} / {catalog[selected]}、速度：{preference["speed"]}倍\n{CREDIT}'
        await run(interaction, op)

    @change.autocomplete('voice')
    async def voices(interaction: discord.Interaction, current: str):
        try:
            provider = 'voicevox'
            catalog = await asyncio.wait_for(service.providers.voices(provider), 2)
            return [app_commands.Choice(name=label[:100], value=key) for key, label in catalog.items()
                    if current.casefold() in (key + label).casefold()][:25]
        except Exception:
            return []

    @group.command(name='list', description='参加中のVCメンバーの読み上げ設定を表示します')
    async def list_settings(interaction: discord.Interaction):
        async def op():
            channel = service.channel(interaction)
            lines = []
            for member in channel.members[:50]:
                if member.bot:
                    continue
                preference = service.preference(member.id)
                catalog = service.providers.cache.get(preference['provider'], (0, {}))[1]
                label = catalog.get(preference['voice'], preference['voice'])
                lines.append(f'{discord.utils.escape_markdown(member.display_name[:30])}：{preference["provider"]} / {label} / {preference["speed"]}倍')
            return short_display('\n'.join(lines) or '参加者はいません。')
        await run(interaction, op)

    @group.command(name='volume', description='音楽・読み上げ・サウンドボードの音量を変更します（管理者）')
    async def volume(interaction: discord.Interaction, music: app_commands.Range[int, 0, 100] | None = None,
                     reading: app_commands.Range[int, 0, 100] | None = None, board: app_commands.Range[int, 0, 100] | None = None):
        async def op():
            service.admin(interaction)
            config = dict(service.config(interaction.guild.id))
            for key, value in [('music_volume', music), ('tts_volume', reading), ('board_volume', board)]:
                if value is not None:
                    config[key] = value
            await save_config(interaction.guild.id, config)
            return f'音量：音楽{config["music_volume"]}%・読み上げ{config["tts_volume"]}%・サウンドボード{config["board_volume"]}%'
        await run(interaction, op)

    async def save_config(guild_id, config):
        await asyncio.to_thread(service.store.put, 'guild', guild_id, config)
        service.configs[guild_id] = config

    @group.command(name='duck', description='読み上げ中に音楽を小さくする設定を変更します（管理者）')
    async def duck(interaction: discord.Interaction, enabled: bool, volume: app_commands.Range[int, 0, 100] = 30):
        async def op():
            service.admin(interaction)
            config = dict(service.config(interaction.guild.id), duck=enabled, duck_volume=volume)
            await save_config(interaction.guild.id, config)
            return f'読み上げ中の音楽調整：{"ON" if enabled else "OFF"}、通常音量の{volume}%'
        await run(interaction, op)

    @group.command(name='skip', description='現在の読み上げだけスキップします')
    async def skip(interaction: discord.Interaction):
        async def op():
            service.channel(interaction)
            job = service.current.get(interaction.guild.id)
            if job:
                job.cancel()
                await asyncio.gather(job, return_exceptions=True)
            service.hub.stop(interaction.guild.id)
            return '現在の読み上げをスキップしました。'
        await run(interaction, op)

    @group.command(name='clear', description='読み上げ待ちの文章を消去します。現在の音声は継続します')
    async def clear(interaction: discord.Interaction):
        async def op():
            service.channel(interaction)
            queue = service.jobs.get(interaction.guild.id)
            count = 0
            while queue and not queue.empty():
                queue.get_nowait()
                count += 1
            return f'待機文を{count}件消去しました。'
        await run(interaction, op)

    @group.command(name='settings', description='読み上げ対象を設定・表示します（変更は管理者）')
    @app_commands.choices(urls=[app_commands.Choice(name='省略', value='omit'), app_commands.Choice(name='リンクと読む', value='label'), app_commands.Choice(name='そのまま読む', value='read')])
    async def settings(interaction: discord.Interaction, names: bool | None = None, urls: str | None = None,
                       emoji: bool | None = None, attachments: bool | None = None, bots: bool | None = None,
                       max_chars: app_commands.Range[int, 10, 300] | None = None):
        async def op():
            changes = {key: value for key, value in dict(names=names, urls=urls, emoji=emoji,
                       attachments=attachments, bots=bots, max_chars=max_chars).items() if value is not None}
            config = dict(service.config(interaction.guild.id))
            if changes:
                service.admin(interaction)
                config.update(changes)
                await save_config(interaction.guild.id, config)
            return (f'名前：{config["names"]}／URL：{config["urls"]}／絵文字：{config["emoji"]}\n'
                    f'添付：{config["attachments"]}／Bot：{config["bots"]}／上限：{config["max_chars"]}文字\n{CREDIT}')
        await run(interaction, op)

    dictionary = app_commands.Group(name='dictionary', description='読み方辞書（変更は管理者）', parent=group)

    @dictionary.command(name='add', description='単語の読み方を登録します（管理者）')
    async def add_word(interaction: discord.Interaction, word: app_commands.Range[str, 1, 40], reading: app_commands.Range[str, 1, 80]):
        async def op():
            service.admin(interaction)
            data = service.store.get('dictionary', interaction.guild.id, {})
            if word not in data and len(data) >= 100:
                raise SoundError('辞書は最大100件です。')
            data[word] = reading
            await asyncio.to_thread(service.store.put, 'dictionary', interaction.guild.id, data)
            return '読み方を登録しました。'
        await run(interaction, op)

    @dictionary.command(name='remove', description='単語の読み方を削除します（管理者）')
    async def remove_word(interaction: discord.Interaction, word: str):
        async def op():
            service.admin(interaction)
            data = service.store.get('dictionary', interaction.guild.id, {})
            data.pop(word, None)
            await asyncio.to_thread(service.store.put, 'dictionary', interaction.guild.id, data)
            return '読み方を削除しました。'
        await run(interaction, op)

    @dictionary.command(name='list', description='登録済みの読み方を表示します')
    async def list_words(interaction: discord.Interaction):
        async def op():
            data = service.store.get('dictionary', interaction.guild.id, {})
            return short_display(discord.utils.escape_markdown('\n'.join(f'{key} → {value}' for key, value in data.items()))) or '辞書は空です。'
        await run(interaction, op)

    bot.tree.add_command(group)
    @group.command(name='diagnose', description='MWSのメモリ・実行環境を確認します（管理者）')
    async def diagnose(interaction: discord.Interaction):
        async def op():
            service.admin(interaction)
            from voice_diagnostics import snapshot, describe
            data = await asyncio.to_thread(snapshot, service.store.path.parent)
            return describe(data, bool(service.providers.base))
        await run(interaction, op)

    diagnosed = False
    async def ready_diagnostics():
        nonlocal diagnosed
        if diagnosed:
            return
        diagnosed = True
        from voice_diagnostics import snapshot, describe
        data = await asyncio.to_thread(snapshot, service.store.path.parent)
        log.info('VOICEVOX runtime diagnostics:\n%s', describe(data, bool(service.providers.base)))
    bot.add_listener(ready_diagnostics, 'on_ready')
    bot.add_listener(service.on_message, 'on_message')
    bot.add_listener(service.on_voice_state_update, 'on_voice_state_update')
    return service
