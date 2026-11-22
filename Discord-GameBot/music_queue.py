"""Per-guild YouTube queues and a shared voice-channel music dashboard."""
import asyncio
from collections import deque
from dataclasses import dataclass, field
import logging
from types import SimpleNamespace

import discord
from discord import app_commands

from sound import SoundError
from youtube_play import YouTubeAudio, normalize_url, normalize_playlist, load_playlist, search_youtube, normalize_search, MAX_QUEUE

log = logging.getLogger(__name__)


def safe_title(value):
    text = ''.join(c for c in str(value) if ord(c) >= 32).replace('@', '＠')[:90]
    return discord.utils.escape_markdown(text) or 'YouTube動画'


@dataclass
class Track:
    url: str
    title: str = ''

    def label(self):
        return f'[{safe_title(self.title or self.url.rsplit("=", 1)[-1])}]({self.url})'


@dataclass
class Session:
    guild: object
    channel: object
    queue: deque = field(default_factory=deque)
    current: object = None
    source: object = None
    task: object = None
    dashboard: object = None
    ui_task: object = None
    dirty: bool = False
    fetches: set = field(default_factory=set)
    control_lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    generation: int = 0
    status: str = '待機中'
    error: str = ''
    skipping: bool = False
    loop_mode: str = 'off'


class PlaybackContext:
    """Authorized queue playback must not depend on an expired interaction token."""
    def __init__(self, service, session):
        self.service, self.session = service, session
        self.guild, self.guild_id, self.channel = session.guild, session.guild.id, session.channel
        # Authorization occurs when a real participant enqueues or operates music.
        self.user = SimpleNamespace(voice=SimpleNamespace(channel=session.channel))

    async def edit_original_response(self, *, content, **kwargs):
        self.session.status = content
        self.service.refresh(self.session)


class MusicView(discord.ui.View):
    def __init__(self, service, loop_mode='off'):
        super().__init__(timeout=None)
        self.service = service
        self.loop_track.style = discord.ButtonStyle.success if loop_mode == 'track' else discord.ButtonStyle.secondary
        self.loop_queue.style = discord.ButtonStyle.success if loop_mode == 'queue' else discord.ButtonStyle.secondary

    @discord.ui.button(label='次へ', emoji='⏭️', style=discord.ButtonStyle.primary, custom_id='music:v1:skip')
    async def skip(self, interaction, button):
        await self.service.control(interaction, 'skip')

    @discord.ui.button(label='再生', emoji='▶️', style=discord.ButtonStyle.success, custom_id='music:v1:resume')
    async def resume(self, interaction, button):
        await self.service.control(interaction, 'resume')

    @discord.ui.button(label='ストップ', emoji='⏸️', style=discord.ButtonStyle.secondary, custom_id='music:v1:pause')
    async def pause(self, interaction, button):
        await self.service.control(interaction, 'pause')

    @discord.ui.button(label='曲ループ', emoji='🔂', style=discord.ButtonStyle.secondary, custom_id='music:v1:loop_track')
    async def loop_track(self, interaction, button):
        await self.service.control(interaction, 'loop_track')

    @discord.ui.button(label='全曲ループ', emoji='🔁', style=discord.ButtonStyle.secondary, custom_id='music:v1:loop_queue')
    async def loop_queue(self, interaction, button):
        await self.service.control(interaction, 'loop_queue')


class Music:
    def __init__(self, bot, sound):
        self.bot, self.sound = bot, sound
        self.sessions = {}
        self.slots = asyncio.Semaphore(2)
        self.search_slots = asyncio.Semaphore(2)
        sound.music = self

    def has_work(self, guild_id):
        session = self.sessions.get(guild_id)
        return bool(session and (session.queue or session.current or session.fetches))

    def session_for(self, interaction):
        channel = self.sound.target(interaction)
        voice = interaction.guild.voice_client
        if voice and voice.is_connected() and voice.channel.id != channel.id:
            raise SoundError('Botが参加しているVCのチャットで操作してください。')
        previous = self.sessions.get(interaction.guild.id)
        if previous and previous.channel.id != channel.id:
            if self.has_work(interaction.guild.id):
                raise SoundError('別のVCのキューが進行中です。先にそのVCで /sound leave を実行してください。')
            self.clear(interaction.guild.id)
            previous = None
        if previous is None:
            previous = self.sessions[interaction.guild.id] = Session(interaction.guild, channel)
        return previous

    def embed(self, session):
        voice = self.sound.music_voice(session.guild)
        source = self.sound.playing.get(session.guild.id)
        if voice and voice.is_paused():
            status = '⏸️ 一時停止中'
        elif session.current and session.source is None:
            status = '📥 音声を準備中'
        elif source:
            status = '▶️ 再生中'
        elif session.fetches:
            status = '📋 プレイリスト取得中'
        else:
            status = '待機中'
        embed = discord.Embed(title='🎵 音楽ダッシュボード', description=status, colour=0x3498DB)
        current = session.current.label() if session.current else safe_title(getattr(source, 'title', '再生していません'))
        embed.add_field(name='現在の曲', value=current, inline=False)
        lines = [f'{index}. {track.label()}' for index, track in enumerate(list(session.queue)[:8], 1)]
        if len(session.queue) > 8:
            lines.append(f'ほか {len(session.queue) - 8}曲')
        # Keep the field below Discord's 1024 UTF-16-unit limit, including emoji.
        upcoming = '\n'.join(lines) or 'キューは空です。`/play` で追加できます。'
        while len(upcoming.encode('utf-16-le')) // 2 > 1000:
            lines.pop()
            upcoming = '\n'.join(lines) + '\n…'
        embed.add_field(name=f'次に流す曲（{len(session.queue)}/{MAX_QUEUE}）', value=upcoming, inline=False)
        embed.add_field(name='ループ', value={'off': 'OFF', 'track': '🔂 現在の曲',
                                             'queue': '🔁 プレイリスト全体'}[session.loop_mode], inline=False)
        if session.error:
            embed.add_field(name='直近の案内', value=safe_title(session.error), inline=False)
        embed.set_footer(text='ストップ＝一時停止／再生＝続きから再開。キュー終了後60秒で自動退出。')
        return embed

    def refresh(self, session):
        session.dirty = True
        if session.ui_task and not session.ui_task.done():
            return
        session.ui_task = asyncio.create_task(self._refresh(session), name='music-dashboard')

    async def _refresh(self, session):
        try:
            while session.dirty:
                session.dirty = False
                if self.sessions.get(session.guild.id) is not session:
                    return
                view = MusicView(self, session.loop_mode)
                try:
                    if session.dashboard is not None:
                        try:
                            await session.dashboard.edit(embed=self.embed(session), view=view,
                                allowed_mentions=discord.AllowedMentions.none())
                        except discord.NotFound:
                            session.dashboard = None
                    if session.dashboard is None:
                        session.dashboard = await session.channel.send(embed=self.embed(session), view=view,
                            allowed_mentions=discord.AllowedMentions.none())
                except discord.DiscordException:
                    log.warning('Could not update music dashboard (guild_id=%s)', session.guild.id)
                    return
                await asyncio.sleep(0.25)
        finally:
            session.ui_task = None

    def idle(self, session):
        if not self.has_work(session.guild.id) and session.guild.id not in self.sound.playing:
            self.sound.cancel_idle(session.guild.id)
            self.sound.idle_tasks[session.guild.id] = asyncio.create_task(self.sound.idle_disconnect(session.guild))

    def kick(self, session):
        if (self.sessions.get(session.guild.id) is not session or session.current is not None or not session.queue
                or (session.task and not session.task.done()) or session.guild.id in self.sound.playing):
            return
        self.sound.cancel_idle(session.guild.id)
        session.task = asyncio.create_task(self._pump(session), name='music-next-track')

    async def _pump(self, session):
        cancelled = False
        try:
            while session.queue and self.sessions.get(session.guild.id) is session:
                while session.guild.id in self.sound.connecting:
                    await asyncio.sleep(0.1)
                async with self.sound.lock(session.guild.id):
                    if session.guild.id in self.sound.playing:
                        return
                    if session.guild.id in self.sound.connecting:
                        session.error = '前の接続の終了処理を待っています。'
                        continue
                    session.current = session.queue.popleft()
                    session.error = ''
                    self.refresh(session)
                    audio = YouTubeAudio(session.current.url, self.slots)
                    try:
                        await self.sound.play(PlaybackContext(self, session), session.channel, audio)
                    except asyncio.CancelledError:
                        raise
                    except Exception as exc:
                        session.error = str(exc) if isinstance(exc, SoundError) else '音声を取得・再生できなかったため、この曲をスキップしました。'
                        log.warning('Music track failed (guild_id=%s, error_type=%s)', session.guild.id, type(exc).__name__)
                        session.current, session.source = None, None
                        self.refresh(session)
                        continue
                    session.current.title = audio.filename.removesuffix('.webm')
                    session.source = self.sound.playing.get(session.guild.id)
                    self.refresh(session)
                    return
        except asyncio.CancelledError:
            cancelled = True
        finally:
            if session.task is asyncio.current_task():
                session.task = None
            if not cancelled:
                self.idle(session)
                self.refresh(session)

    def on_finished(self, guild, source):
        session = self.sessions.get(guild.id)
        if session is None:
            return
        if session.source is source:
            # Only successful YouTube tracks repeat. Failed audio must not loop forever.
            track = session.current
            if track is not None and not source.playback_error:
                if session.loop_mode == 'queue':
                    session.queue.append(track)
                elif session.loop_mode == 'track' and not session.skipping:
                    session.queue.appendleft(track)
            session.current, session.source, session.skipping = None, None, False
        elif session.source is not None:
            return
        session.skipping = False
        self.kick(session)
        self.refresh(session)

    def clear(self, guild_id):
        session = self.sessions.get(guild_id)
        if session is None:
            return
        session.generation += 1
        session.queue.clear()
        session.loop_mode = 'off'
        session.current, session.source, session.skipping = None, None, False
        for task in list(session.fetches):
            task.cancel()
        if session.task and session.task is not asyncio.current_task():
            session.task.cancel()
        self.refresh(session)

    async def cancel_pending(self, guild_id, channel_id):
        session = self.sessions.get(guild_id)
        if session is None or session.channel.id != channel_id:
            return False
        had_work = self.has_work(guild_id)
        task = session.task
        self.clear(guild_id)
        if task and task is not asyncio.current_task():
            await asyncio.gather(task, return_exceptions=True)
        return had_work

    async def enqueue(self, interaction, url):
        await interaction.response.send_message('曲をキューに追加します。', ephemeral=True,
                                                allowed_mentions=discord.AllowedMentions.none())
        session, fetch = None, None
        try:
            playlist = normalize_playlist(url)
            if playlist is None:
                url = normalize_url(url)
            session = self.session_for(interaction)
            permissions = session.channel.permissions_for(interaction.guild.me)
            if not (permissions.view_channel and permissions.connect and permissions.speak):
                raise SoundError('BotにこのVCの「チャンネルを見る」「接続」「発言」権限が必要です。')
            if len(session.queue) >= MAX_QUEUE:
                raise SoundError(f'キューは最大{MAX_QUEUE}曲です。曲が減ってから追加してください。')
            generation = session.generation
            if playlist:
                fetch = asyncio.create_task(load_playlist(playlist, self.slots), name='music-playlist')
                session.fetches.add(fetch)
                self.sound.cancel_idle(session.guild.id)
                self.refresh(session)
                entries, limited = await fetch
            else:
                entries, limited = [{'url': url, 'title': ''}], False
            # Recheck caller membership and the session after network preparation.
            self.sound.target(interaction)
            if self.sessions.get(session.guild.id) is not session or session.generation != generation:
                raise SoundError('キューへの追加は取り消されました。')
            room = MAX_QUEUE - len(session.queue)
            accepted = entries[:room]
            if not accepted:
                raise SoundError('キューがいっぱいになりました。曲が減ってから追加してください。')
            session.queue.extend(Track(item['url'], item['title']) for item in accepted)
            self.sound.cancel_idle(session.guild.id)
            self.kick(session)
            self.refresh(session)
            answer = f'✅ {len(accepted)}曲を順番に追加しました。ダッシュボードで確認できます。'
            if limited or len(accepted) < len(entries):
                answer += f' 今回は上限内の曲だけ追加しました（待機は最大{MAX_QUEUE}曲）。'
        except asyncio.CancelledError:
            if asyncio.current_task().cancelling():
                raise
            answer = 'プレイリストの追加を取り消しました。'
        except SoundError as exc:
            answer = str(exc)
        except Exception as exc:
            log.warning('Music enqueue failed (error_type=%s)', type(exc).__name__)
            answer = '曲を追加できませんでした。時間をおいて再試行してください。'
        finally:
            if session and fetch:
                session.fetches.discard(fetch)
                self.idle(session)
                self.refresh(session)
        await self.sound.status(interaction, answer)

    async def search(self, interaction, query):
        await interaction.response.send_message('🔎 YouTubeを検索しています。',
                                                allowed_mentions=discord.AllowedMentions.none())
        try:
            query = normalize_search(query)
            entries = await search_youtube(query, self.search_slots)
            embed = discord.Embed(title='🔎 YouTube検索結果', description=safe_title(query), colour=0x3498DB)
            for index, entry in enumerate(entries, 1):
                embed.add_field(name=f'候補 {index}',
                    value=f'{safe_title(entry["title"])}\n{entry["url"]}', inline=False)
            if not entries:
                embed.description = '該当する動画が見つかりませんでした。別のキーワードで検索してください。'
            embed.set_footer(text='再生するにはVCのチャットで /play url:動画のリンク を実行してください。')
            await interaction.edit_original_response(content=None, embed=embed,
                                                    allowed_mentions=discord.AllowedMentions.none())
        except SoundError as exc:
            await self.sound.status(interaction, str(exc))
        except Exception as exc:
            log.warning('YouTube search failed (error_type=%s)', type(exc).__name__)
            await self.sound.status(interaction, 'YouTubeを検索できませんでした。時間をおいて再試行してください。')

    async def control(self, interaction, action):
        await interaction.response.defer(ephemeral=True, thinking=True)
        try:
            self.sound.target(interaction)
            session = self.sessions.get(interaction.guild.id)
            if session is None or session.channel.id != interaction.channel.id:
                raise SoundError('このVCのキューはありません。`/play` で曲を追加してください。')
            if session.control_lock.locked():
                raise SoundError('切り替え中です。少しお待ちください。')
            async with session.control_lock:
                if action in ('loop_track', 'loop_queue'):
                    voice = self.sound.music_voice(session.guild)
                    if voice and voice.is_connected() and voice.channel.id != session.channel.id:
                        raise SoundError('Botが参加しているVCで操作してください。')
                    mode = 'track' if action == 'loop_track' else 'queue'
                    session.loop_mode = 'off' if session.loop_mode == mode else mode
                    answer = {'off': 'ループ再生をOFFにしました。',
                              'track': '🔂 曲ループをONにしました。同じ曲を繰り返します。',
                              'queue': '🔁 全曲ループをONにしました。現在の曲とキューを順番に繰り返します。'}[session.loop_mode]
                    self.refresh(session)
                    await self.sound.status(interaction, answer)
                    return
                if action == 'skip' and session.task and not session.task.done():
                    if session.loop_mode == 'queue' and session.current is not None:
                        session.queue.append(session.current)
                    session.current, session.source = None, None
                    task = session.task
                    task.cancel()
                    await asyncio.gather(task, return_exceptions=True)
                if self.sound.lock(session.guild.id).locked():
                    raise SoundError('音声の準備・操作中です。少し待ってから操作してください。')
                async with self.sound.lock(session.guild.id):
                    voice = self.sound.music_voice(session.guild)
                    if voice and voice.is_connected() and voice.channel.id != session.channel.id:
                        raise SoundError('Botが参加しているVCで操作してください。')
                    if action == 'skip':
                        if session.skipping:
                            raise SoundError('次の曲に切り替え中です。')
                        if voice and (voice.is_playing() or voice.is_paused()):
                            session.skipping = True
                            voice.stop()
                        else:
                            self.kick(session)
                        answer = '⏭️ 次の曲へ進みます。待機曲がなければ再生を終了します。'
                    elif action == 'pause':
                        if not voice or not voice.is_playing():
                            raise SoundError('現在、再生中の曲はありません。')
                        voice.pause()
                        self.sound.cancel_idle(session.guild.id)
                        answer = '⏸️ 一時停止しました。「再生」で続きから再開できます。'
                    else:
                        if voice and voice.is_paused():
                            voice.resume()
                            self.sound.cancel_idle(session.guild.id)
                            answer = '▶️ 続きから再生します。'
                        elif session.current is not None:
                            answer = '現在の曲を再生中、または準備中です。'
                        elif session.queue:
                            self.kick(session)
                            answer = '▶️ キューの先頭から再生します。'
                        else:
                            raise SoundError('キューは空です。`/play` で追加してください。')
                self.idle(session)
                self.refresh(session)
        except SoundError as exc:
            answer = str(exc)
        except Exception as exc:
            log.warning('Music control failed (error_type=%s)', type(exc).__name__)
            answer = '音楽の操作に失敗しました。もう一度試してください。'
        await self.sound.status(interaction, answer)


def register_music(bot, sound):
    service = Music(bot, sound)
    bot.add_view(MusicView(service))

    @bot.tree.command(name='play', description='YouTube動画・プレイリストを音楽キューに追加します')
    @app_commands.guild_only()
    @app_commands.describe(url='YouTube動画または公開プレイリストのURL')
    async def play(interaction: discord.Interaction, url: str):
        await service.enqueue(interaction, url)

    @bot.tree.command(name='search', description='YouTubeを検索し、タイトルとリンクを最大7件表示します')
    @app_commands.guild_only()
    @app_commands.describe(query='検索ワード（空白OK・200文字まで）')
    async def search(interaction: discord.Interaction, query: str):
        await service.search(interaction, query)

    @bot.tree.command(name='music', description='このVCの音楽ダッシュボードを表示します')
    @app_commands.guild_only()
    async def music(interaction: discord.Interaction):
        try:
            session = service.session_for(interaction)
        except SoundError as exc:
            await interaction.response.send_message(str(exc), ephemeral=True)
            return
        await interaction.response.send_message(embed=service.embed(session), view=MusicView(service, session.loop_mode),
                                                allowed_mentions=discord.AllowedMentions.none())
        try:
            session.dashboard = await interaction.original_response()
        except discord.DiscordException:
            pass
    return service
