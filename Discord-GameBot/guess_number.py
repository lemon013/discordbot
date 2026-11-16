import asyncio
import secrets
import uuid
import discord
from discord import app_commands
from party_rewards import reward_party


class Answer(discord.ui.Modal, title='数字を予想'):
    value = discord.ui.TextInput(label='自分に配られたと思う数字', max_length=12)
    def __init__(self, game):
        super().__init__()
        self.game = game
    async def on_submit(self, interaction):
        try:
            value = int(str(self.value))
        except ValueError:
            await interaction.response.send_message('整数を入力してください。',ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True)
        game=self.game
        async with game.lock:
            if game.phase!='answer' or interaction.user.id not in game.players:
                await interaction.edit_original_response(content='回答受付は終了しました。')
                return
            game.answers[interaction.user.id]=value
            await interaction.edit_original_response(content='回答を受け付けました。全員の回答が揃うまでは変更できます。')
            if len(game.answers)==len(game.players):
                await game.finish()


class Config(discord.ui.Modal, title='数字当ての設定'):
    minimum=discord.ui.TextInput(label='最小値',default='1',max_length=7)
    maximum=discord.ui.TextInput(label='最大値',default='100',max_length=7)
    tolerance=discord.ui.TextInput(label='正解にする誤差（0で完全一致）',default='0',max_length=7)
    misses=discord.ui.TextInput(label='クリアに許容する不正解人数（0で全員正解）',default='0',max_length=2)
    def __init__(self,game):
        super().__init__()
        self.game=game
    async def on_submit(self,interaction):
        game=self.game
        try:
            low,high,tolerance,misses=map(int,map(str,(self.minimum,self.maximum,self.tolerance,self.misses)))
            assert -1000000<=low<=high<=1000000 and 0<=tolerance<=1000000 and 0<=misses<20
        except (ValueError,AssertionError):
            await interaction.response.send_message('範囲は−100万〜100万、誤差は0以上、許容人数は0〜19で指定してください。',ephemeral=True)
            return
        if game.phase!='recruit' or interaction.user.id!=game.host:
            await interaction.response.send_message('募集主が募集時に設定できます。',ephemeral=True)
            return
        game.low,game.high,game.tolerance,game.misses=low,high,tolerance,misses
        await interaction.response.send_message('設定を保存しました。',ephemeral=True)
        await game.board.edit(embed=game.embed())


class Game(discord.ui.View):
    def __init__(self,bot,interaction,registry):
        super().__init__(timeout=None)
        self.bot,self.channel,self.guild=bot,interaction.channel,interaction.guild
        self.host=interaction.user.id
        self.registry=registry
        self.players={self.host:interaction.user}
        self.low,self.high,self.tolerance,self.misses=1,100,0,0
        self.phase='recruit'
        self.answers={}
        self.numbers={}
        self.lock=asyncio.Lock()
        self.game_id='guess:'+uuid.uuid4().hex
    def embed(self):
        return discord.Embed(title='🔢 数字当てゲーム',description=(
            f'範囲：{self.low}〜{self.high}／誤差：±{self.tolerance}／許容不正解：{self.misses}人\n'
            f'参加者：{len(self.players)}人（2〜20人）\n'+ '\n'.join(f'<@{uid}>' for uid in self.players)+
            '\n自分の数字は非公開。他の人の数字をDMで配ります。ヒントを出し合って自分の数字を予想！\n'
            '募集主が /guess number を再実行すると回答受付へ進みます。\n成功：全員100ジュエル＋10XP／失敗：全員50ジュエル＋5XP'))
    @discord.ui.button(label='参加／退出',style=discord.ButtonStyle.success)
    async def join(self,interaction,button):
        if self.phase!='recruit' or interaction.user.bot:
            await interaction.response.send_message('募集は終了しました。',ephemeral=True);return
        uid=interaction.user.id
        if uid==self.host:
            await interaction.response.send_message('募集主は退出できません。',ephemeral=True);return
        if uid in self.players:self.players.pop(uid)
        elif len(self.players)<20:self.players[uid]=interaction.user
        else:
            await interaction.response.send_message('定員20人です。',ephemeral=True);return
        await interaction.response.edit_message(embed=self.embed())
    @discord.ui.button(label='設定',style=discord.ButtonStyle.secondary)
    async def configure(self,interaction,button):
        if interaction.user.id!=self.host or self.phase!='recruit':
            await interaction.response.send_message('募集主が募集時に設定できます。',ephemeral=True);return
        await interaction.response.send_modal(Config(self))
    @discord.ui.button(label='開始',style=discord.ButtonStyle.primary)
    async def start(self,interaction,button):
        if interaction.user.id!=self.host or self.phase!='recruit':
            await interaction.response.send_message('募集主だけが開始できます。',ephemeral=True);return
        if len(self.players)<2 or self.misses>=len(self.players):
            await interaction.response.send_message('2人以上参加し、許容不正解人数を参加人数未満にしてください。',ephemeral=True);return
        self.phase='sending'
        await interaction.response.defer(ephemeral=True)
        numbers={uid:secrets.randbelow(self.high-self.low+1)+self.low for uid in self.players}
        try:
            for uid,user in self.players.items():
                lines=[f'{other.display_name}：{numbers[oid]}' for oid,other in self.players.items() if oid!=uid]
                await user.send('🔢 数字当てゲーム\n自分以外の数字です。自分の数字は推理してください。\n'+'\n'.join(lines))
        except discord.HTTPException:
            self.phase='recruit'
            await interaction.edit_original_response(content='DMを配れませんでした。DM設定を確認して再度開始してください。先ほど届いた数字は無効です。');return
        self.numbers=numbers
        self.phase='discussion'
        await self.board.edit(view=None)
        await self.channel.send('🔢 DM配布完了！ヒントを出し合ってください。募集主が `/guess number` を再実行すると予想入力に進みます。')
        await interaction.edit_original_response(content='開始しました。')
    @discord.ui.button(label='予想を入力',style=discord.ButtonStyle.primary)
    async def answer(self,interaction,button):
        if self.phase!='answer' or interaction.user.id not in self.players:
            await interaction.response.send_message('参加者が回答受付中に使用できます。',ephemeral=True);return
        await interaction.response.send_modal(Answer(self))
    async def finish(self):
        self.phase='finished'
        correct={uid:abs(self.answers[uid]-number)<=self.tolerance for uid,number in self.numbers.items()}
        won=sum(not value for value in correct.values())<=self.misses
        await reward_party(self.bot,self.guild,self.channel,self.game_id,{uid:won for uid in self.players})
        lines=[f"<@{uid}>：予想 {self.answers[uid]}／正解 {number} {'✅' if correct[uid] else '❌'}" for uid,number in self.numbers.items()]
        await self.channel.send(embed=discord.Embed(title='🎉 GAME CLEAR' if won else 'GAME FAILED',description='\n'.join(lines)+('\n報酬：100ジュエル＋10XP' if won else '\n報酬：50ジュエル＋5XP')),allowed_mentions=discord.AllowedMentions.none())
        self.registry.pop(self.guild.id,None)
        self.stop()


def register_guess(bot):
    games={}
    group=app_commands.Group(name='guess',description='みんなで数字当て',guild_only=True)
    @group.command(name='number',description='数字当てを募集／回答受付へ進めます')
    async def number(interaction:discord.Interaction):
        game=games.get(interaction.guild_id)
        if game is None:
            game=Game(bot,interaction,games);games[interaction.guild_id]=game
            await interaction.response.send_message(embed=game.embed(),view=game,allowed_mentions=discord.AllowedMentions.none())
            game.board=await interaction.original_response();return
        if interaction.channel_id!=game.channel.id or interaction.user.id!=game.host:
            await interaction.response.send_message('開始したチャンネルで募集主が操作してください。',ephemeral=True);return
        if game.phase=='discussion':
            game.phase='answer'
            for child in game.children:child.disabled=child is not game.answer
            await interaction.response.send_message('全員、ボタンから自分の数字を予想してください。全員の回答が揃うと結果を公開します。',view=game)
        else:await interaction.response.send_message('現在のボードを使用してください。',ephemeral=True)
    bot.tree.add_command(group)
