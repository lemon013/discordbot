"""Mix a bottomless bowl of shaved ice and rate its fictional recipe."""
from dataclasses import dataclass
from itertools import combinations

import discord


@dataclass(frozen=True)
class Ingredient:
    name: str
    colour: int
    group: str


INGREDIENTS = {
    'strawberry': Ingredient('イチゴ', 0xED4567, 'fruit'),
    'blue': Ingredient('ブルーハワイ', 0x16B7EF, 'soda'),
    'melon': Ingredient('メロン', 0x64CF59, 'fruit'),
    'lemon': Ingredient('レモン', 0xF5E34C, 'citrus'),
    'mango': Ingredient('マンゴー', 0xFFB52E, 'fruit'),
    'cola': Ingredient('コーラ', 0x704537, 'soda'),
    'grape': Ingredient('グレープ', 0x8F55B6, 'fruit'),
    'peach': Ingredient('桃', 0xF5ADBC, 'fruit'),
    'milk': Ingredient('練乳', 0xFFF5D6, 'milk'),
    'orange': Ingredient('オレンジ', 0xFF963B, 'citrus'),
    'ramune': Ingredient('ラムネ', 0x7AD7EB, 'soda'),
    'matcha': Ingredient('抹茶', 0x668C38, 'japanese'),
    'kuromitsu': Ingredient('黒蜜', 0x775039, 'japanese'),
    'azuki': Ingredient('小豆', 0x9B5361, 'japanese'),
}
PAIR_BONUSES = {
    frozenset(('strawberry', 'milk')): 40,
    frozenset(('mango', 'milk')): 35,
    frozenset(('peach', 'milk')): 32,
    frozenset(('matcha', 'milk')): 35,
    frozenset(('matcha', 'azuki')): 40,
    frozenset(('matcha', 'kuromitsu')): 30,
    frozenset(('azuki', 'milk')): 35,
    frozenset(('azuki', 'kuromitsu')): 30,
    frozenset(('kuromitsu', 'milk')): 28,
    frozenset(('blue', 'ramune')): 25,
    frozenset(('lemon', 'ramune')): 25,
    frozenset(('cola', 'lemon')): 22,
    frozenset(('cola', 'milk')): 12,
    frozenset(('mango', 'orange')): 28,
    frozenset(('peach', 'strawberry')): 25,
}


def pair_bonus(first, second):
    explicit = PAIR_BONUSES.get(frozenset((first, second)))
    if explicit is not None:
        return explicit
    a, b = INGREDIENTS[first].group, INGREDIENTS[second].group
    if 'japanese' in (a, b) and ('soda' in (a, b) or 'citrus' in (a, b)):
        return -35
    if a in ('fruit', 'citrus') and b in ('fruit', 'citrus'):
        return 15
    if 'milk' in (a, b):
        return 8
    if a == b:
        return 12
    return -8


def evaluate(recipe):
    total = sum(recipe.values())
    if not total:
        return None
    pairs = list(combinations(recipe, 2))
    weight = sum(recipe[a] * recipe[b] for a, b in pairs)
    affinity = sum(pair_bonus(a, b) * recipe[a] * recipe[b] for a, b in pairs) / weight if weight else 12
    excess = min(40, max(0, total - 10) * 1.5)
    variety = max(0, len(recipe) - 4) * 4
    milk_excess = max(0, recipe.get('milk', 0) / total - 0.5) * 25
    score = max(0, min(100, round(72 + affinity * 0.7 - excess - variety - milk_excess)))
    if len(recipe) == len(INGREDIENTS):
        comment = '全部入り。かき氷の上で、全シロップによる会議が始まった。'
    elif total > 30:
        comment = 'もはや氷にシロップをかけたのか、シロップに氷を入れたのかわからない。'
    elif score >= 90:
        comment = 'これはお店に出せそう！主役と引き立て役の相性が抜群。'
    elif score >= 75:
        comment = 'ひと口目から期待できそう。夏のお供にぴったりの一杯。'
    elif score >= 50:
        comment = '個性派の一杯。好きな人には、かなり刺さるかもしれない。'
    elif score >= 25:
        comment = '味の方向性が迷子。ひと口で何種類もの夏が押し寄せる。'
    else:
        comment = 'シロップ同士が主役の座を争っている。氷だけが平常心。'
    return score, comment


def bowl_colour(recipe):
    total = sum(recipe.values())
    if not total:
        return 0xA8DDF0
    channels = []
    for shift in (16, 8, 0):
        mixed = sum(((INGREDIENTS[key].colour >> shift) & 255) * amount for key, amount in recipe.items()) / total
        channels.append(round(mixed * 0.75 + 255 * 0.25))
    return (channels[0] << 16) | (channels[1] << 8) | channels[2]


class KakigooriView(discord.ui.View):
    def __init__(self, player_id, display_name):
        super().__init__(timeout=600)
        self.player_id = player_id
        self.display_name = display_name
        self.recipe = {}
        self.selected = []
        self.amount = 1
        self.finished = False
        self.message = None

    def embed(self):
        embed = discord.Embed(title='🍧 かき氷シロップバー', colour=bowl_colour(self.recipe))
        name = discord.utils.escape_markdown(self.display_name)
        embed.description = f'**{name} のかき氷**\n' + (
            '完成！あなたのオリジナルかき氷をどうぞ。' if self.finished else
            '種類と量を選んで「盛る」を押してください。何度でも追加できます。')
        contents = '\n'.join(f'{INGREDIENTS[key].name} × {amount:,}杯' for key, amount in self.recipe.items())
        embed.add_field(name='盛り付け', value=contents or 'まだ氷だけ。何をかける？', inline=False)
        embed.add_field(name='合計', value=f'{sum(self.recipe.values()):,}杯 / {len(self.recipe)}種類', inline=False)
        if self.finished:
            score, comment = evaluate(self.recipe)
            embed.add_field(name='美味しそう度', value=f'**{score} / 100点**', inline=False)
            embed.add_field(name='ひと口コメント', value=comment, inline=False)
        else:
            selected = '・'.join(INGREDIENTS[key].name for key in self.selected) or '未選択'
            embed.add_field(name='次に盛るもの', value=f'{selected}\n各 {self.amount}杯ずつ', inline=False)
        embed.set_footer(text='「最初から」で作り直せます。')
        return embed

    async def interaction_check(self, interaction):
        if interaction.user.id != self.player_id:
            await interaction.response.send_message('自分のかき氷は `/kakigoori` で作ってください。', ephemeral=True)
            return False
        return True

    async def refresh(self, interaction):
        for child in self.children:
            child.disabled = self.finished and child is not self.reset
        await interaction.response.edit_message(embed=self.embed(), view=self)

    @discord.ui.select(placeholder='シロップ・トッピングを選ぶ（複数選択OK）', min_values=1,
                       max_values=len(INGREDIENTS), row=0,
                       options=[discord.SelectOption(label=item.name, value=key) for key, item in INGREDIENTS.items()])
    async def choose(self, interaction, select):
        self.selected = list(select.values)
        await self.refresh(interaction)

    @discord.ui.select(placeholder='1種類につき何杯盛る？（初期値：1杯）', row=1,
                       options=[discord.SelectOption(label=f'{amount}杯', value=str(amount)) for amount in (1, 3, 5, 10, 50, 100)])
    async def quantity(self, interaction, select):
        self.amount = int(select.values[0])
        await self.refresh(interaction)

    @discord.ui.button(label='盛る', emoji='🥄', style=discord.ButtonStyle.primary, row=2)
    async def add(self, interaction, button):
        if self.finished:
            await interaction.response.send_message('完成済みです。「最初から」で新しく作れます。', ephemeral=True)
            return
        if not self.selected:
            await interaction.response.send_message('先にシロップ・トッピングを選んでください。', ephemeral=True)
            return
        for key in self.selected:
            self.recipe[key] = self.recipe.get(key, 0) + self.amount
        await self.refresh(interaction)

    @discord.ui.button(label='完成！', emoji='🍧', style=discord.ButtonStyle.success, row=2)
    async def finish(self, interaction, button):
        if not self.recipe:
            await interaction.response.send_message('まずは1杯かけてみましょう。', ephemeral=True)
            return
        self.finished = True
        await self.refresh(interaction)

    @discord.ui.button(label='最初から', style=discord.ButtonStyle.secondary, row=2)
    async def reset(self, interaction, button):
        self.recipe.clear()
        self.selected.clear()
        self.amount = 1
        self.finished = False
        for select in (self.choose, self.quantity):
            for option in select.options:
                option.default = False
        await self.refresh(interaction)

    async def on_timeout(self):
        for child in self.children:
            child.disabled = True
        if self.message:
            try:
                await self.message.edit(view=self)
            except discord.HTTPException:
                pass


def register_kakigoori(bot):
    @bot.tree.command(name='kakigoori', description='シロップ盛り放題！自分だけのかき氷を作って美味しそう度を採点')
    async def kakigoori(interaction: discord.Interaction):
        await interaction.response.defer(thinking=True)
        view = KakigooriView(interaction.user.id, interaction.user.display_name)
        view.message = await interaction.edit_original_response(
            embed=view.embed(), view=view, allowed_mentions=discord.AllowedMentions.none())
