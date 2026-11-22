"""Thirteen coin-flip successes, increasing XP and an atomic clear reward."""
import asyncio
import logging
import random
import uuid

import discord
from discord import app_commands
from solo_rewards import reward_solo, step_xp

log = logging.getLogger(__name__)


def game_embed(count):
    embed = discord.Embed(title='🎰 1/8192 GAME', description='当たりだと思う扉を選んでください。',
                          colour=discord.Colour.blurple())
    embed.add_field(name='CURRENT PROBABILITY', value=f'## 1/{2 ** (count + 1):,}', inline=False)
    embed.add_field(name='連続正解', value=f'{count} / 13 回')
    embed.add_field(name='次の成功報酬', value=f'+{step_xp(count + 1)} XP', inline=False)
    embed.set_footer(text='1/4到達で3XP、以後の成功ごとに報酬+3XP。クリアでさらに3レベル＋3000ジュエル。')
    return embed


class Game:
    def __init__(self, bot, interaction, registry):
        self.bot, self.registry = bot, registry
        self.owner, self.guild_id = interaction.user.id, interaction.guild_id
        self.key = (self.guild_id, self.owner)
        self.id = uuid.uuid4().hex
        self.count, self.done = 0, False
        self.lock = asyncio.Lock()

    def close(self):
        self.done = True
        if self.registry.get(self.key) is self:
            self.registry.pop(self.key)


class Game8192View(discord.ui.View):
    def __init__(self, game):
        super().__init__(timeout=600)
        self.game, self.count = game, game.count
        self.correct_color = random.choice(('blue', 'green'))
        self.message = None

    async def interaction_check(self, interaction):
        if interaction.user.id != self.game.owner or interaction.guild_id != self.game.guild_id:
            await interaction.response.send_message('開始した本人だけが操作できます。', ephemeral=True)
            return False
        if self.game.done or self.count != self.game.count:
            await interaction.response.send_message('このボタンは受付終了です。最新のボードを使ってください。', ephemeral=True)
            return False
        return True

    async def on_timeout(self):
        async with self.game.lock:
            if not self.game.done and self.game.count == self.count:
                self.game.close()
                for child in self.children:
                    child.disabled = True
                if self.message:
                    try:
                        await self.message.edit(content='時間切れで終了しました。獲得済みのXPは保持されます。', view=self)
                    except discord.HTTPException:
                        pass

    async def choose(self, interaction, selected):
        if not await self.interaction_check(interaction):
            return
        game = self.game
        if game.lock.locked():
            await interaction.response.send_message('結果を保存中です。少しお待ちください。', ephemeral=True)
            return
        async with game.lock:
            await interaction.response.defer()
            if selected == self.correct_color:
                count = self.count + 1
                xp, clear = step_xp(count), count == 13
                try:
                    reward = (await reward_solo(game.bot, interaction.guild, interaction.channel, game.owner,
                        f'8192:{game.id}:{count}', xp, levels=3 if clear else 0, gems=3000 if clear else 0)) if xp else None
                    if clear:
                        from ticket_store import reward_ticket
                        await reward_ticket(game.bot, interaction.guild, game.owner, 'ticket:8192:' + game.id, 1)
                except Exception as exc:
                    log.warning('8192 reward save failed (game_id=%s, error_type=%s)', game.id, type(exc).__name__)
                    await interaction.followup.send('報酬を保存できませんでした。同じ扉をもう一度押すと再試行できます。', ephemeral=True)
                    return
                game.count = count
                if clear:
                    game.close()
                    embed = discord.Embed(title='🎉 1/8192 GAME CLEAR!',
                        description='**見事、13回連続正解！**', colour=discord.Colour.gold())
                    embed.add_field(name='達成確率', value='## 1/8192', inline=False)
                    embed.add_field(name='今回の成功報酬', value=f'+{xp} XP')
                    embed.add_field(name='クリア報酬', value=f"+{reward['levels_gained']}レベル（上限100）／💎 3000ジュエル", inline=False)
                    embed.add_field(name='成功報酬の累計', value=f'{3 * count * (count - 1) // 2} XP（レベル報酬の加算分を除く）', inline=False)
                    view = None
                else:
                    embed, view = game_embed(count), Game8192View(game)
                text = f'✅ 正解！ +{xp} XP' if xp else '✅ 正解！XPは1/4到達からもらえます。'
            else:
                try:
                    from ticket_store import reward_ticket
                    await reward_ticket(game.bot, interaction.guild, game.owner, 'ticket:8192:' + game.id, 1)
                except Exception:
                    await interaction.followup.send('チケットの保存に失敗しました。同じボタンで再試行してください。', ephemeral=True)
                    return
                game.close()
                embed = discord.Embed(title='❌ GAME OVER',
                    description=f'連続正解：{self.count}回\n挑戦した確率：1/{2 ** (self.count + 1):,}\n獲得済みのXPは保持されます。',
                    colour=discord.Colour.red())
                text, view = None, None
            self.stop()
            try:
                message = await interaction.edit_original_response(content=text, embed=embed, view=view)
                if view:
                    view.message = message
            except discord.HTTPException:
                game.close()
                if view:
                    view.stop()
                log.warning('8192 result saved but panel unavailable (game_id=%s)', game.id)

    @discord.ui.button(label='青の扉', style=discord.ButtonStyle.primary, emoji='🔵')
    async def blue(self, interaction, button):
        await self.choose(interaction, 'blue')

    @discord.ui.button(label='緑の扉', style=discord.ButtonStyle.success, emoji='🟢')
    async def green(self, interaction, button):
        await self.choose(interaction, 'green')


def register_8192(bot):
    games = {}

    @bot.tree.command(name='8192game', description='1/8192の確率に挑戦！成功でXP、クリアで3レベル＋3000ジュエル')
    @app_commands.guild_only()
    async def start(interaction: discord.Interaction):
        await interaction.response.defer(thinking=True)
        key = (interaction.guild_id, interaction.user.id)
        if key in games:
            await interaction.edit_original_response(content='進行中のゲームがあります。元のボードから続けてください。')
            return
        game = Game(bot, interaction, games)
        games[key] = game
        view = Game8192View(game)
        try:
            view.message = await interaction.edit_original_response(embed=game_embed(0), view=view)
        except BaseException:
            game.close()
            view.stop()
            raise
    alias = app_commands.Command(name='8192games', description='1/8192ゲーム（/8192game と同じ）', callback=start.callback)
    alias.guild_only = True
    bot.tree.add_command(alias)
    return games
