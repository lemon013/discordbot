"""Two-player Othello with an optional, limited consecutive-move skill."""
import asyncio
from datetime import timedelta
import logging
import random
import re
import unicodedata
import uuid

import discord
from discord import app_commands
from party_rewards import reward_party

log = logging.getLogger(__name__)
DIRECTIONS = tuple((r, c) for r in (-1, 0, 1) for c in (-1, 0, 1) if r or c)


def new_board():
    board = [[0] * 8 for _ in range(8)]
    board[3][3] = board[4][4] = 2
    board[3][4] = board[4][3] = 1
    return board


def flips(board, color, row, col):
    if not (0 <= row < 8 and 0 <= col < 8) or board[row][col]:
        return []
    result = []
    for dr, dc in DIRECTIONS:
        ray = []
        r, c = row + dr, col + dc
        while 0 <= r < 8 and 0 <= c < 8 and board[r][c] == 3 - color:
            ray.append((r, c))
            r, c = r + dr, c + dc
        if ray and 0 <= r < 8 and 0 <= c < 8 and board[r][c] == color:
            result.extend(ray)
    return result


def legal_moves(board, color):
    return {(r, c): captured for r in range(8) for c in range(8)
            if (captured := flips(board, color, r, c))}


def coordinate(text):
    value = re.sub(r'\s+', '', unicodedata.normalize('NFKC', text)).upper()
    match = re.fullmatch(r'([A-H])([1-8])', value)
    if not match:
        raise ValueError('位置は A1〜H8 の形式で入力してください。例：D3')
    return int(match[2]) - 1, ord(match[1]) - ord('A')


def name(user):
    return discord.utils.escape_markdown(user.display_name[:60])


class MoveModal(discord.ui.Modal, title='コマを置く'):
    position = discord.ui.TextInput(label='位置（A1〜H8）', placeholder='例：D3', max_length=12)

    def __init__(self, game, uid, double):
        super().__init__(timeout=180)
        self.game, self.uid, self.double = game, uid, double
        self.revision = game.revision

    async def on_submit(self, interaction):
        await interaction.response.defer(ephemeral=True)
        if interaction.user.id != self.uid:
            await interaction.edit_original_response(content='フォームを開いた本人だけが置けます。')
            return
        try:
            row, col = coordinate(self.position.value)
        except ValueError as exc:
            await interaction.edit_original_response(content=str(exc))
            return
        await self.game.play(interaction, row, col, self.double, self.revision)


class Action(discord.ui.Button):
    def __init__(self, game, action, label, style=discord.ButtonStyle.primary):
        super().__init__(label=label, style=style)
        self.game, self.action = game, action

    async def callback(self, interaction):
        await self.game.dispatch(interaction, self.action)


class Othello(discord.ui.View):
    def __init__(self, bot, interaction, registry, double_uses, unlock_turn=5):
        super().__init__(timeout=None)
        self.bot, self.guild, self.channel = bot, interaction.guild, interaction.channel
        self.host = interaction.user.id
        self.players = {self.host: interaction.user}
        self.registry, self.key = registry, (self.guild.id, self.channel.id)
        self.double_uses = double_uses
        self.unlock_turn, self.move_count = unlock_turn, 0
        self.remaining, self.colors = {}, {}
        self.cells, self.turn, self.second = new_board(), 1, False
        self.phase, self.result, self.note = 'lobby', '', ''
        self.board, self.timer, self.deadline = None, None, None
        self.revision = self.timer_token = 0
        self.lock = asyncio.Lock()
        self.game_id = 'othello:' + uuid.uuid4().hex
        self.outcomes, self.rewards_pending = None, False
        self.controls()

    @property
    def double_unlocked(self):
        return self.move_count + 1 >= self.unlock_turn

    def controls(self):
        self.clear_items()
        if self.phase == 'lobby':
            actions = [('join', '参加／退出'), ('start', '開始（募集主）'), ('cancel', '中止（募集主）')]
        elif self.phase == 'playing':
            actions = [('move', 'コマを置く'), ('double', '2連続で置く'), ('resign', '投了する')]
        elif self.rewards_pending:
            actions = [('retry', '報酬を再試行（募集主）')]
        else:
            actions = []
        for action, label in actions:
            button = Action(self, action, label,
                            discord.ButtonStyle.danger if action in ('cancel', 'resign') else discord.ButtonStyle.primary)
            if action == 'double':
                button.disabled = (not self.double_unlocked or self.second
                                   or not self.remaining.get(self.colors.get(self.turn), 0))
            self.add_item(button)

    def embed(self):
        embed = discord.Embed(title='⚫ オセロ ⚪', color=0x228B22)
        if self.phase == 'lobby':
            embed.description = (f'2人対戦／現在 {len(self.players)}人\n'
                + '、'.join(name(user) for user in self.players.values())
                + f'\n2連続置き：各プレイヤー **{self.double_uses}回**（0回なら通常ルール）\n'
                + f'解禁：**{self.unlock_turn}ターン目**から（黒白合計の手数）\n'
                '相手が「参加」を押したら、募集主が開始してください。先手・後手はランダムです。\n'
                '完走時：勝利300ジュエル＋30XP／敗北・引き分け150ジュエル＋15XP。\n'
                '投了・中止・時間切れは報酬なし。')
        else:
            moves = legal_moves(self.cells, self.turn) if self.phase == 'playing' else {}
            rows = ['⬛ＡＢＣＤＥＦＧＨ']
            for r in range(8):
                rows.append(f'{r + 1}️⃣' + ''.join(
                    '⚫' if cell == 1 else '⚪' if cell == 2 else '🟨' if (r, c) in moves else '🟩'
                    for c, cell in enumerate(self.cells[r])))
            black = sum(row.count(1) for row in self.cells)
            white = sum(row.count(2) for row in self.cells)
            embed.description = '\n'.join(rows) + f'\n⚫ {black}枚 ／ ⚪ {white}枚'
            for color, uid in self.colors.items():
                embed.add_field(name=('⚫' if color == 1 else '⚪') + ' ' + name(self.players[uid]),
                                value=f'2連続置き：残り {self.remaining[uid]}回', inline=True)
            if self.phase == 'playing':
                current = self.players[self.colors[self.turn]]
                embed.add_field(name='現在の手番', value=name(current)
                                + f'／第{self.move_count + 1}手'
                                + ('（連続置きの2手目）' if self.second else ''), inline=False)
                embed.add_field(name='2連続置きの解禁', value=f'{self.unlock_turn}ターン目から'
                                + ('（解禁済み）' if self.double_unlocked else '（まだ使用できません）'), inline=False)
                embed.add_field(name='置ける場所', value='、'.join(
                    f'{chr(65 + c)}{r + 1}' for r, c in sorted(moves)), inline=False)
                embed.add_field(name='操作', value='🟨に置けます。「コマを置く」で位置を入力。\n'
                    '特技は「2連続で置く」で1手目を入力すると1回消費し、次も自分の番になります。\n'
                    '2手目で特技の重ね掛けはできません。置けない場合は自動パスします。', inline=False)
                if self.note:
                    embed.add_field(name='直前の手', value=self.note, inline=False)
            else:
                embed.add_field(name='結果', value=self.result, inline=False)
                if self.outcomes is not None:
                    embed.add_field(name='報酬（未処理あり：再試行してください）' if self.rewards_pending else '報酬',
                                    value='\n'.join(name(self.players[uid]) + '：' +
                                        ('300ジュエル＋30XP' if won else '150ジュエル＋15XP')
                                        for uid, won in self.outcomes.items()), inline=False)
        if self.deadline:
            embed.set_footer(text='募集5分／1手3分。Bot再起動で対局は終了します。')
            embed.add_field(name='締切', value=f'<t:{int(self.deadline.timestamp())}:R>', inline=False)
        return embed

    def release(self):
        if self.registry.get(self.key) is self:
            self.registry.pop(self.key, None)

    def cancel_timer(self):
        self.timer_token += 1
        if self.timer and self.timer is not asyncio.current_task():
            self.timer.cancel()
        self.deadline = None

    def arm(self, seconds):
        self.cancel_timer()
        self.deadline = discord.utils.utcnow() + timedelta(seconds=seconds)
        self.timer = asyncio.create_task(self.expire(self.timer_token, seconds))

    async def expire(self, token, seconds):
        try:
            await asyncio.sleep(seconds)
            async with self.lock:
                if token == self.timer_token and self.phase in ('lobby', 'playing'):
                    await self.finish('時間切れで終了しました。報酬はありません。')
        except asyncio.CancelledError:
            pass
        except Exception:
            log.exception('Othello timer failed: %s', self.game_id)
            self.release()
            self.stop()

    async def update(self):
        self.controls()
        if self.board is not None:
            try:
                await self.board.edit(embed=self.embed(), view=self, allowed_mentions=discord.AllowedMentions.none())
            except discord.HTTPException:
                log.warning('Othello board unavailable: %s', self.game_id)
                self.phase = 'finished'
                self.cancel_timer()
                self.release()
                self.stop()
                return False
        return True

    async def settle(self):
        try:
            await reward_party(self.bot, self.guild, self.channel, self.game_id, self.outcomes)
            self.rewards_pending = False
        except Exception:
            log.exception('Othello rewards pending: %s', self.game_id)
            self.rewards_pending = True

    async def finish(self, result, outcomes=None):
        if self.phase == 'finished':
            return
        self.phase, self.result, self.outcomes = 'finished', result, outcomes
        self.cancel_timer()
        self.release()
        if outcomes is not None:
            await self.settle()
        await self.update()
        if not self.rewards_pending:
            self.stop()

    async def play(self, interaction, row, col, double, revision):
        async with self.lock:
            uid = interaction.user.id
            if self.phase != 'playing' or uid != self.colors[self.turn] or revision != self.revision:
                await interaction.edit_original_response(content='手番が変わったか、古い入力フォームです。盤面のボタンから開き直してください。')
                return
            if double and (not self.double_unlocked or self.second or self.remaining[uid] <= 0):
                await interaction.edit_original_response(content=f'2連続置きは{self.unlock_turn}ターン目以降、残り回数がある通常の手番で使えます。')
                return
            captured = flips(self.cells, self.turn, row, col)
            if not captured:
                await interaction.edit_original_response(content='そこには置けません。黄色いマスから選んでください。')
                return
            color = self.turn
            self.cells[row][col] = color
            for r, c in captured:
                self.cells[r][c] = color
            self.revision += 1
            self.move_count += 1
            self.note = f'{name(self.players[uid])}：{chr(65 + col)}{row + 1}（{len(captured)}枚ひっくり返しました）'
            if double:
                self.remaining[uid] -= 1
            own, opponent = legal_moves(self.cells, color), legal_moves(self.cells, 3 - color)
            if not own and not opponent:
                black = sum(line.count(1) for line in self.cells)
                white = sum(line.count(2) for line in self.cells)
                winner = None if black == white else self.colors[1 if black > white else 2]
                result = '引き分け！' if winner is None else name(self.players[winner]) + ' の勝利！'
                await self.finish(result, {pid: pid == winner for pid in self.players})
            else:
                self.second = bool(double and own)
                if self.second:
                    self.note += '\n特技発動！続けてもう1枚置けます。'
                elif opponent:
                    self.turn = 3 - color
                    if double:
                        self.note += '\n2手目を置けないため自動パスしました。'
                else:
                    self.note += '\n相手が置けないため、自動パス。もう一度あなたの番です。'
                self.arm(180)
                await self.update()
            await interaction.edit_original_response(content='コマを置きました。盤面を確認してください。')

    async def dispatch(self, interaction, action):
        uid = interaction.user.id
        if action in ('move', 'double'):
            # No await before validation + modal creation; submission revalidates under the lock.
            if self.phase != 'playing' or uid != self.colors[self.turn]:
                await interaction.response.send_message('現在の手番のプレイヤーだけが置けます。', ephemeral=True)
                return
            if action == 'double' and (not self.double_unlocked or self.second or not self.remaining[uid]):
                await interaction.response.send_message(f'2連続置きは{self.unlock_turn}ターン目以降、残り回数がある通常の手番で使えます。', ephemeral=True)
                return
            await interaction.response.send_modal(MoveModal(self, uid, action == 'double'))
            return
        if action == 'resign':
            if self.phase != 'playing' or uid not in self.players:
                await interaction.response.send_message('対局中の参加者だけが投了できます。', ephemeral=True)
                return
            view = ResignConfirm(self, uid)
            await interaction.response.send_message('本当に投了しますか？投了時は双方とも報酬なしです。', view=view, ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True)
        async with self.lock:
            reply = '現在は操作できません。'
            if action == 'join' and self.phase == 'lobby':
                if uid == self.host:
                    reply = '募集主は退出できません。中止ボタンを使ってください。'
                elif interaction.user.bot:
                    reply = 'Botは参加できません。'
                elif uid in self.players:
                    self.players.pop(uid)
                    reply = '退出しました。'
                elif len(self.players) == 2:
                    reply = '定員は2人です。'
                else:
                    self.players[uid] = interaction.user
                    reply = '参加しました。'
                await self.update()
            elif action == 'start' and self.phase == 'lobby' and uid == self.host:
                if len(self.players) != 2:
                    reply = '2人そろってから開始してください。'
                else:
                    order = random.sample(list(self.players), 2)
                    self.colors = {1: order[0], 2: order[1]}
                    self.remaining = {pid: self.double_uses for pid in self.players}
                    self.phase = 'playing'
                    self.revision += 1
                    self.arm(180)
                    await self.update()
                    reply = '開始しました。黒が先手です。'
            elif action == 'cancel' and self.phase == 'lobby' and uid == self.host:
                await self.finish('募集を中止しました。報酬はありません。')
                reply = '中止しました。'
            elif action == 'retry' and self.phase == 'finished' and self.rewards_pending and uid == self.host:
                await self.settle()
                await self.update()
                if not self.rewards_pending:
                    self.stop()
                reply = '報酬を再試行しました。盤面の報酬欄を確認してください。'
            await interaction.edit_original_response(content=reply)


class ResignConfirm(discord.ui.View):
    def __init__(self, game, uid):
        super().__init__(timeout=60)
        self.game, self.uid = game, uid

    @discord.ui.button(label='投了する（確定）', style=discord.ButtonStyle.danger)
    async def confirm(self, interaction, button):
        await interaction.response.defer(ephemeral=True)
        async with self.game.lock:
            if interaction.user.id != self.uid or self.game.phase != 'playing':
                await interaction.edit_original_response(content='この投了操作は無効です。', view=None)
                return
            winner = next(pid for pid in self.game.players if pid != self.uid)
            await self.game.finish(name(self.game.players[self.uid]) + ' が投了。'
                                   + name(self.game.players[winner]) + ' の勝利！報酬はありません。')
            await interaction.edit_original_response(content='投了しました。', view=None)
            self.stop()


def register_othello(bot):
    games = {}

    @bot.tree.command(name='othello', description='2人対戦オセロ。好きな手番で使える2連続置き付き')
    @app_commands.guild_only()
    @app_commands.rename(double_uses='連続置き回数', unlock_turn='連続置き解禁ターン')
    @app_commands.describe(double_uses='各プレイヤーが使える2連続置きの回数。0は通常ルール（初期値1回）')
    @app_commands.describe(unlock_turn='何ターン目から使えるか。黒白合計の手数で数える（初期値5）')
    async def othello(interaction: discord.Interaction, double_uses: app_commands.Range[int, 0, 10] = 1,
                      unlock_turn: app_commands.Range[int, 1, 60] = 5):
        await interaction.response.defer(thinking=True)
        if interaction.guild is None or interaction.channel is None or interaction.user.bot:
            await interaction.edit_original_response(content='サーバー内でプレイヤーが実行してください。')
            return
        key = (interaction.guild.id, interaction.channel.id)
        if key in games:
            await interaction.edit_original_response(content='このチャンネルではオセロが進行中です。',
                                                     allowed_mentions=discord.AllowedMentions.none())
            return
        game = Othello(bot, interaction, games, double_uses, unlock_turn)
        games[key] = game
        game.arm(300)
        try:
            game.board = await interaction.edit_original_response(embed=game.embed(), view=game,
                                                                 allowed_mentions=discord.AllowedMentions.none())
        except Exception:
            game.cancel_timer()
            game.release()
            game.stop()
            raise
    return games
