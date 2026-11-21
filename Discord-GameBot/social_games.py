"""Chat/voice based ito and Insider, with the existing durable party rewards."""
import asyncio
from collections import Counter
from datetime import timedelta
import logging
import random
import re
import uuid

import discord
from discord import app_commands
from party_rewards import reward_party

log = logging.getLogger(__name__)
RNG = random.SystemRandom()
ITO_TOPICS = (
    '強そうな動物', 'もらって嬉しいプレゼント', '無人島に持っていきたいもの',
    '朝起きたら困る出来事', '豪華だと思う食べ物', '怖い場所',
    '便利な超能力', '学校にあったら嬉しい設備', '冒険で頼れる持ち物',
    'テンションが上がる瞬間', 'なくなると困るもの', '目立つ登場の仕方',
    '旅行してみたい場所', '難しそうな仕事', '夏らしいもの', '冬に嬉しいもの',
    '時間を忘れてしまう遊び', 'ボスとして強そうなもの', '家に置きたい家具',
    '友達と挑戦したいこと', '嬉しい休日の過ごし方', 'ゲームで欲しい能力',
)
INSIDER_TOPICS = (
    '冷蔵庫', '傘', '自転車', '図書館', '花火', '消しゴム', 'エレベーター',
    '水族館', '電子レンジ', '郵便ポスト', '望遠鏡', 'トランポリン', '風鈴',
    '横断歩道', '砂時計', '炊飯器', '観覧車', '温泉', '信号機', '段ボール',
    '救急車', '扇風機', '改札機', '手袋', '滑り台', '鍵', 'ピアノ', '地球儀',
    '自動販売機', 'キャンプ', '映画館', 'スキー', 'カレンダー', 'テント',
    'プラネタリウム', 'かき氷', '水筒', '将棋', '磁石', 'パラシュート',
)


def ito_order_ok(numbers, played, user_id):
    remaining = [value for uid, value in numbers.items() if uid not in played]
    return numbers[user_id] == min(remaining)


def insider_caught(votes, insider_id):
    counts = Counter(votes.values())
    if not counts:
        return False
    leaders = [uid for uid, count in counts.items() if count == max(counts.values())]
    return leaders == [insider_id]


def clean(text):
    return discord.utils.escape_mentions(discord.utils.escape_markdown(text))


class Action(discord.ui.Button):
    def __init__(self, game, action, label, style=discord.ButtonStyle.secondary):
        super().__init__(label=label, style=style)
        self.game, self.action = game, action

    async def callback(self, interaction):
        await self.game.dispatch(interaction, self.action)


class HintModal(discord.ui.Modal, title='数字を言わずにヒントを伝える'):
    text = discord.ui.TextInput(label='お題に沿った例え（数字の直接入力は禁止）', max_length=100)

    def __init__(self, game):
        super().__init__()
        self.game = game

    async def on_submit(self, interaction):
        await interaction.response.defer(ephemeral=True)
        await self.game.submit_hint(interaction, str(self.text).strip())


class VoteSelect(discord.ui.Select):
    def __init__(self, game):
        self.game = game
        super().__init__(placeholder='インサイダーだと思う人', options=[
            discord.SelectOption(label=user.display_name[:80], value=str(uid))
            for uid, user in game.players.items() if uid != game.host])

    async def callback(self, interaction):
        await interaction.response.defer(ephemeral=True)
        await self.game.submit_vote(interaction, int(self.values[0]))


class SocialGame(discord.ui.View):
    def __init__(self, bot, interaction, registry, kind):
        super().__init__(timeout=None)
        self.bot, self.guild, self.channel = bot, interaction.guild, interaction.channel
        self.registry, self.kind = registry, kind
        self.host = interaction.user.id
        self.players = {self.host: interaction.user}
        self.minimum, self.maximum = (2, 10) if kind == 'ito' else (4, 8)
        self.phase = 'lobby'
        self.game_id = f'{kind}:{uuid.uuid4().hex}'
        self.topic = ''
        self.numbers, self.hints, self.votes = {}, {}, {}
        self.played, self.seen = [], set()
        self.insider = None
        self.board = None
        self.lock = asyncio.Lock()
        self.timer = None
        self.deadline = None
        self.result = ''
        self.outcomes = None
        self.rewards_pending = False
        self.controls()

    def release(self):
        if self.registry.get(self.guild.id) is self:
            self.registry.pop(self.guild.id, None)

    def controls(self):
        self.clear_items()
        actions = {
            'lobby': [('join', '参加／退出'), ('start', '開始')],
            'ito': [('secret', '自分の数字'), ('hint', 'ヒント入力'), ('play', '自分のカードを出す')],
            'briefing': [('secret', '役職・お題を確認'), ('begin', 'クイズ開始（マスター）')],
            'question': [('secret', '役職・お題を確認'), ('correct', '正解！（マスター）')],
            'discussion': [('secret', '自分の役職'), ('voting', '投票へ（マスター）')],
            'vote': [('vote', '投票する')],
        }.get(self.phase, [])
        for action, label in actions:
            self.add_item(Action(self, action, label, discord.ButtonStyle.primary))
        if self.phase not in ('finished', 'cancelled', 'settling'):
            self.add_item(Action(self, 'cancel', '中止（募集主）', discord.ButtonStyle.danger))
        if self.phase == 'finished' and self.rewards_pending:
            self.add_item(Action(self, 'retry', '報酬を再試行（募集主）'))

    def embed(self):
        title = '🧵 ito' if self.kind == 'ito' else '🕵️ インサイダー'
        embed = discord.Embed(title=title, colour=0x3498DB)
        names = '、'.join(clean(user.display_name) for user in self.players.values())
        if self.phase == 'lobby':
            embed.description = (f'{self.minimum}〜{self.maximum}人／参加者 {len(self.players)}人\n{names}\n\n' +
                ('秘密の1〜100の数字をお題で例えて、小さい順にカードを出そう。数字を直接言うのは禁止。'
                 if self.kind == 'ito' else
                 '募集主が公開マスター、ほかの1人が秘密のインサイダー。5分でお題を当て、その後に正体を探そう。\n'
                 'Bot版：議論3分→一斉投票1分。同票・全員未投票はインサイダー勝利。') +
                '\n\n勝利：100ジュエル＋10XP／敗北：50ジュエル＋5XP\n中止・準備中の時間切れは報酬なし。')
        elif self.phase == 'ito':
            embed.description = f'お題：**{self.topic}**\n1＝低い・弱い、100＝高い・強い。\n全員のヒントがそろったら、相談して小さいと思う人から出してください。'
            for uid, user in self.players.items():
                embed.add_field(name=clean(user.display_name), value=clean(self.hints.get(uid, '入力待ち')), inline=False)
            embed.add_field(name='出したカード', value=' → '.join(str(self.numbers[uid]) for uid in self.played) or 'まだ出ていません', inline=False)
        elif self.phase == 'briefing':
            embed.description = (f'マスター：{clean(self.players[self.host].display_name)}\n'
                f'全員がボタンで役職を確認してください（{len(self.seen)}/{len(self.players)}人）。\n'
                '確認後、マスターが「クイズ開始」を押します。秘密の画面は共有しないでください。')
        elif self.phase == 'question':
            embed.description = ('質問タイム！チャットまたはVCで、マスターに「はい／いいえ」で答えられる質問をしてください。\n'
                'マスターは質問に回答し、お題が当たったら「正解！」を押してください。\n'
                '5分以内に当てられないと全員敗北です。')
        elif self.phase == 'discussion':
            embed.description = f'正解は **{self.topic}**！\n誰が答えを知って誘導していた？マスターも交えて議論してください。\n3分後に投票へ進みます。'
        elif self.phase == 'vote':
            embed.description = (f'お題：**{self.topic}**\n投票ボタンからインサイダーだと思う人を選んでください。\n'
                f'投票済み：{len(self.votes)}/{len(self.players)}人\n最多票が1人ならその人を指名。同票・全員未投票はインサイダー勝利。')
        else:
            embed.description = self.result
            if self.outcomes is not None:
                reward_text = '\n'.join(f'{clean(self.players[uid].display_name)}：'
                    + ('100ジュエル＋10XP' if won else '50ジュエル＋5XP') for uid, won in self.outcomes.items())
                embed.add_field(name='報酬（一部未処理：再試行が必要）' if self.rewards_pending else '報酬', value=reward_text, inline=False)
        if self.deadline and self.phase not in ('finished', 'cancelled', 'settling'):
            embed.set_footer(text='秘密の情報は本人だけに表示されます。')
            embed.add_field(name='この段階の締切', value=f'<t:{int(self.deadline.timestamp())}:R>', inline=False)
        return embed

    async def update(self):
        self.controls()
        if self.board:
            await self.board.edit(embed=self.embed(), view=self, allowed_mentions=discord.AllowedMentions.none())

    def arm(self, seconds):
        if self.timer and self.timer is not asyncio.current_task():
            self.timer.cancel()
        self.deadline = discord.utils.utcnow() + timedelta(seconds=seconds)
        phase = self.phase
        self.timer = asyncio.create_task(self.expire(phase, seconds))

    async def expire(self, phase, seconds):
        try:
            await asyncio.sleep(seconds)
            async with self.lock:
                if self.phase != phase:
                    return
                if phase == 'question':
                    await self.finish('時間切れ！お題は「' + self.topic + '」。全員敗北です。', {uid: False for uid in self.players})
                elif phase == 'discussion':
                    self.phase = 'vote'
                    self.arm(60)
                    await self.update()
                elif phase == 'vote':
                    await self.finish_vote()
                else:
                    await self.cancel('時間切れで中止しました。報酬はありません。')
        except asyncio.CancelledError:
            pass
        except Exception:
            log.exception('Social game timer failed: %s', self.game_id)
            self.release()
            self.stop()

    async def cancel(self, reason):
        self.phase, self.result = 'cancelled', reason
        if self.timer and self.timer is not asyncio.current_task():
            self.timer.cancel()
        self.release()
        await self.update()
        self.stop()

    async def settle(self):
        try:
            await reward_party(self.bot, self.guild, self.channel, self.game_id, self.outcomes)
            self.rewards_pending = False
        except Exception:
            log.exception('Social game rewards pending: %s', self.game_id)
            self.rewards_pending = True

    async def finish(self, result, outcomes):
        if self.phase in ('settling', 'finished', 'cancelled'):
            return
        self.phase, self.result, self.outcomes = 'settling', result, outcomes
        if self.kind == 'insider':
            self.result += f'\nインサイダー：{clean(self.players[self.insider].display_name)}'
        if self.timer and self.timer is not asyncio.current_task():
            self.timer.cancel()
        await self.settle()
        self.phase = 'finished'
        self.release()
        await self.update()
        if not self.rewards_pending:
            self.stop()

    async def finish_vote(self):
        caught = insider_caught(self.votes, self.insider)
        counts = Counter(self.votes.values())
        tally = '\n'.join(f'{clean(user.display_name)}：{counts[uid]}票' for uid, user in self.players.items() if uid != self.host)
        await self.finish(('インサイダーを発見！マスター・庶民の勝利！' if caught else '見抜けなかった！インサイダーの勝利！') + '\n' + tally,
                          {uid: (uid != self.insider) if caught else (uid == self.insider) for uid in self.players})

    async def submit_hint(self, interaction, text):
        async with self.lock:
            uid = interaction.user.id
            if self.phase != 'ito' or uid not in self.players or uid in self.played:
                await interaction.edit_original_response(content='今はヒントを入力できません。'); return
            if not text or len(text) > 100 or re.search(r'\d', text):
                await interaction.edit_original_response(content='数字を直接書かず、1〜100文字の例えを入力してください。'); return
            self.hints[uid] = text
            await interaction.edit_original_response(content='ヒントを公開しました。カードを出す前なら変更できます。')
            await self.update()

    async def submit_vote(self, interaction, target):
        async with self.lock:
            uid = interaction.user.id
            if self.phase != 'vote' or uid not in self.players or target not in self.players or target == self.host:
                await interaction.edit_original_response(content='投票の受付は終了したか、対象が無効です。'); return
            self.votes[uid] = target
            await interaction.edit_original_response(content='投票しました。全員の投票がそろうまでは変更できます。', view=None)
            if len(self.votes) == len(self.players):
                await self.finish_vote()
            else:
                await self.update()

    async def dispatch(self, interaction, action):
        uid = interaction.user.id
        if action == 'hint':
            if self.phase == 'ito' and uid in self.players and uid not in self.played:
                await interaction.response.send_modal(HintModal(self))
            else:
                await interaction.response.send_message('参加者がカードを出す前に使えます。', ephemeral=True)
            return
        if action == 'vote':
            if self.phase == 'vote' and uid in self.players:
                view = discord.ui.View(timeout=60)
                view.add_item(VoteSelect(self))
                await interaction.response.send_message('投票先を選んでください。', view=view, ephemeral=True)
            else:
                await interaction.response.send_message('参加者が投票中に使えます。', ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True)
        async with self.lock:
            reply = ''
            if action in ('start', 'begin', 'correct', 'voting', 'cancel', 'retry') and uid != self.host:
                reply = '募集主（インサイダーではマスター）だけが操作できます。'
            elif action == 'join' and self.phase == 'lobby':
                if uid == self.host:
                    reply = '募集主は退出できません。中止ボタンを使ってください。'
                elif uid in self.players:
                    self.players.pop(uid); reply = '退出しました。'
                elif interaction.user.bot or len(self.players) >= self.maximum:
                    reply = '参加できません。人数の上限を確認してください。'
                else:
                    self.players[uid] = interaction.user; reply = '参加しました。'
                await self.update()
            elif action == 'start' and self.phase == 'lobby':
                if len(self.players) < self.minimum:
                    reply = f'{self.minimum}人以上で開始できます。'
                elif self.kind == 'ito':
                    self.numbers = dict(zip(self.players, RNG.sample(range(1, 101), len(self.players))))
                    self.topic, self.phase = RNG.choice(ITO_TOPICS), 'ito'
                    self.arm(600); await self.update(); reply = '開始！自分の数字を確認し、ヒントを入力してください。'
                else:
                    self.insider = RNG.choice([player for player in self.players if player != self.host])
                    self.topic, self.phase = RNG.choice(INSIDER_TOPICS), 'briefing'
                    self.arm(180); await self.update(); reply = '全員が役職を確認したら、クイズを開始してください。'
            elif action == 'secret' and uid in self.players and self.phase in ('ito', 'briefing', 'question', 'discussion'):
                if self.kind == 'ito':
                    reply = f'あなたの数字は **{self.numbers[uid]}**。お題：{self.topic}\n数字そのものは公開しないでください。'
                else:
                    self.seen.add(uid)
                    if uid == self.host:
                        reply = f'あなたは **マスター**。お題：**{self.topic}**\n質問に回答し、正解したら「正解！」を押してください。'
                    elif uid == self.insider:
                        reply = f'あなたは **インサイダー**。お題：**{self.topic}**\nさりげなく正解に導き、投票で正体を隠してください。'
                    else:
                        reply = 'あなたは **庶民**。お題を当てた後、答えを知っていたインサイダーを探してください。'
                    if self.phase == 'briefing':
                        await self.update()
            elif action == 'play' and self.phase == 'ito' and uid in self.players:
                if len(self.hints) != len(self.players):
                    reply = '全員がヒントを入力してから出してください。'
                elif uid in self.played:
                    reply = 'すでにカードを出しています。'
                else:
                    ok = ito_order_ok(self.numbers, self.played, uid)
                    self.played.append(uid)
                    if not ok or len(self.played) == len(self.players):
                        reveal = '\n'.join(f'{clean(user.display_name)}：{self.numbers[player]}（{clean(self.hints[player])}）' for player, user in self.players.items())
                        await self.finish(('全員で順番に出せた！成功！' if ok else 'もっと小さいカードが残っていた！失敗！') + '\n' + reveal,
                                          {player: ok for player in self.players})
                    else:
                        await self.update()
                    reply = f'{self.numbers[uid]}を出しました。'
            elif action == 'begin' and self.phase == 'briefing':
                if self.seen != set(self.players):
                    reply = 'まだ役職を確認していない参加者がいます。'
                else:
                    self.phase = 'question'; self.arm(300); await self.update(); reply = '5分のクイズを開始しました。'
            elif action == 'correct' and self.phase == 'question':
                self.phase = 'discussion'; self.arm(180); await self.update(); reply = '正解を公開しました。インサイダーを探してください。'
            elif action == 'voting' and self.phase == 'discussion':
                self.phase = 'vote'; self.arm(60); await self.update(); reply = '投票を開始しました。'
            elif action == 'cancel' and self.phase not in ('finished', 'cancelled', 'settling'):
                await self.cancel('募集主が中止しました。報酬はありません。'); reply = '中止しました。'
            elif action == 'retry' and self.phase == 'finished' and self.rewards_pending:
                await self.settle(); await self.update()
                reply = '報酬を再試行しました。受取済みの報酬は重複しません。'
                if not self.rewards_pending:
                    self.stop()
            else:
                reply = '現在はこの操作を使えません。参加状況とゲームの段階を確認してください。'
            await interaction.edit_original_response(content=reply)


def register_social_games(bot):
    games = {}

    async def open_game(interaction, kind):
        await interaction.response.defer(thinking=True)
        if interaction.guild_id in games:
            game = games[interaction.guild_id]
            await interaction.edit_original_response(content=f'このサーバーでは {game.kind} が進行中です。' +
                (f'\n{game.board.jump_url}' if game.board else ''), allowed_mentions=discord.AllowedMentions.none())
            return
        game = SocialGame(bot, interaction, games, kind)
        games[interaction.guild_id] = game
        try:
            game.board = await interaction.edit_original_response(embed=game.embed(), view=game, allowed_mentions=discord.AllowedMentions.none())
            game.arm(600)
        except Exception:
            game.release(); game.stop(); raise

    @bot.tree.command(name='ito', description='秘密の数字を言葉で例えて、小さい順に並べる協力ゲーム')
    @app_commands.guild_only()
    async def ito(interaction: discord.Interaction):
        await open_game(interaction, 'ito')

    @bot.tree.command(name='insider', description='お題を当てた後、答えを知って誘導したインサイダーを探そう')
    @app_commands.guild_only()
    async def insider(interaction: discord.Interaction):
        await open_game(interaction, 'insider')
