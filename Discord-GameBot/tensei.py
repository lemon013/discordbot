"""A fictional lifetime gacha for one player."""
import random
from collections import Counter
from dataclasses import dataclass

import discord

PERIODS = ('乳児期', '幼児期', '学童期', '青年期', '成人期', '老年期')


@dataclass(frozen=True)
class Event:
    text: str
    income: int = 0  # Fictional contribution to final annual income, in 10,000 yen.
    happiness: int = 0
    theme: str = 'ordinary'


STUDY = Event('勉強', 15, -5, 'study')
EVENTS = (
    (STUDY, Event('よく寝る', 0, 8, 'sleep'), Event('泣き声で家族を指揮', 5, 3),
     Event('離乳食の食レポ', 5, 5, 'food'), Event('すでに社長の風格', 20, -3, 'business'),
     Event('ぬいぐるみと会議', 0, 6), Event('拍手だけで褒められる', 0, 10), Event('リモコンを独占', 0, 4)),
    (STUDY, Event('砂場に王国を建設', 10, 6, 'create'), Event('毎日お昼寝', 0, 8, 'sleep'),
     Event('お菓子の交換で財を成す', 20, 4, 'business'), Event('空想の友達と冒険', 0, 8, 'adventure'),
     Event('ブロック遊びに没頭', 10, 6, 'create'), Event('好き嫌いで食卓と交渉', 0, -3, 'food'),
     Event('ヒーローになると宣言', 0, 10, 'adventure')),
    (STUDY, Event('ゲームの攻略本になる', 5, 8, 'game'), Event('給食じゃんけん全勝', 0, 10, 'food'),
     Event('夏休み最終日に本気を出す', 0, -8), Event('自由研究が特許になる', 80, 5, 'create'),
     Event('友達と秘密基地を建設', 0, 12, 'friends'), Event('授業中に夢の世界へ', -5, 5, 'sleep'),
     Event('お年玉を全額貯金', 20, -2, 'business')),
    (STUDY, Event('部活に青春を捧げる', 10, 12, 'friends'), Event('推しに全力投資', -20, 12, 'hobby'),
     Event('自作ゲームが大ヒット', 150, 10, 'game'), Event('進路希望に「自由」と記入', -10, 8, 'adventure'),
     Event('恋と課題に追われる', 0, 6, 'friends'), Event('起業して一度失敗', -30, -8, 'business'),
     Event('世界一周へ出発', -20, 14, 'adventure')),
    (STUDY, Event('会社員として地道に働く', 420, 0, 'ordinary'), Event('起業して大成功', 2500, 8, 'business'),
     Event('好きなことを仕事にする', 350, 20, 'hobby'), Event('プロゲーマーになる', 800, 12, 'game'),
     Event('発明で世界を変える', 1800, 15, 'create'), Event('転職を繰り返す', 260, -10),
     Event('畑と暮らす', 180, 18, 'nature'), Event('宝くじ当選後も働く', 5000, 5, 'luck'),
     Event('働かず趣味を極める', 0, 10, 'hobby'), Event('出世したが休日がない', 1200, -30, 'business'),
     Event('友人と小さな店を開く', 300, 18, 'friends')),
    (STUDY, Event('縁側でお茶を飲む', -100, 15, 'nature'), Event('現役社長を続ける', 800, -8, 'business'),
     Event('孫とゲームで真剣勝負', -100, 18, 'game'), Event('世界中を旅する', -150, 20, 'adventure'),
     Event('家庭菜園で野菜に話しかける', -100, 15, 'nature'), Event('昼寝の研究を完成', -150, 18, 'sleep'),
     Event('趣味の作品が突然売れる', 500, 18, 'create'), Event('昔の仲間と毎日集まる', -100, 22, 'friends'),
     Event('通帳を眺めて一日が終わる', 200, -20, 'business')),
)


@dataclass(frozen=True)
class Life:
    periods: tuple[str, ...]
    income: int
    happiness: int
    explanation: str


SPECIAL_LIVES = (
    Life(('勉強',) * 6, 0, 0, '学び過ぎた'),
    Life(('睡眠',) * 6, 0, 100, '夢の中では、すべてを手に入れていた。'),
    Life(('ゲーム',) * 6, 0, 100, '人生の実績は未解除。ゲームの実績は全解除。'),
    Life(('貯金',) * 6, 100000000, 1, '使う日を待っていたら、使う日がなくなった。'),
    Life(('遊ぶ', '遊ぶ', '遊ぶ', '遊ぶ', '遊ぶ', '遊ぶ'), 0, 100, '最後まで「明日から本気出す」を貫いた。'),
)


def generate_life(rng=random):
    if rng.randrange(100) == 0:
        return rng.choice(SPECIAL_LIVES)
    events = tuple(rng.choice(pool) for pool in EVENTS)
    if all(event.theme == 'study' for event in events):
        return SPECIAL_LIVES[0]
    income = max(0, sum(event.income for event in events) + rng.randint(-20, 20)) * 10000
    happiness = max(0, min(100, 50 + sum(event.happiness for event in events) + rng.randint(-8, 8)))
    theme, count = Counter(event.theme for event in events).most_common(1)[0]
    if income >= 10000000 and happiness < 40:
        explanation = '口座の残高は増えた。自分のための時間は減った。'
    elif income < 2000000 and happiness >= 80:
        explanation = 'お金持ちにはなれなかったが、毎日は好きなものでいっぱいだった。'
    elif theme == 'study' and count >= 3:
        explanation = '人生という問題集を解き続けた。答え合わせは、まだしていない。'
    elif theme == 'game' and count >= 2:
        explanation = '失敗しても再挑戦。人生にも、その癖が役に立った。'
    elif theme == 'friends' and count >= 2:
        explanation = '人生のハイライトには、いつも誰かが隣にいた。'
    elif theme == 'business' and count >= 2:
        explanation = '人生まで事業計画に入れた。予定どおりかは別の話。'
    elif events[4].theme != events[5].theme:
        explanation = f'「{events[4].text}」から「{events[5].text}」へ。最後まで人生は読めなかった。'
    else:
        explanation = '大きな事件も小さな寄り道も、まとめて自分だけの一生になった。'
    return Life(tuple(event.text for event in events), income, happiness, explanation)


def life_embed(life, display_name):
    embed = discord.Embed(title='世界でもっとも深いガチャ',
                          description=f'**{discord.utils.escape_markdown(display_name)} の人生**', colour=0x3498DB)
    for period, text in zip(PERIODS, life.periods):
        embed.add_field(name=period, value=text, inline=False)
    embed.add_field(name='最終年収', value=f'{life.income:,}円', inline=True)
    embed.add_field(name='幸福度', value=f'{life.happiness}%', inline=True)
    embed.add_field(name='説明', value=life.explanation, inline=False)
    embed.set_footer(text='フィクションの人生ガチャです。もう一度転生すると結果が入れ替わります。')
    return embed


class TenseiView(discord.ui.View):
    def __init__(self, player_id):
        super().__init__(timeout=180)
        self.player_id = player_id
        self.message = None

    async def interaction_check(self, interaction):
        if interaction.user.id != self.player_id:
            await interaction.response.send_message('自分の人生は `/tensei` で引いてください。', ephemeral=True)
            return False
        return True

    @discord.ui.button(label='もう一度転生する', emoji='🔄', style=discord.ButtonStyle.primary)
    async def reroll(self, interaction, button):
        await interaction.response.edit_message(embed=life_embed(generate_life(), interaction.user.display_name), view=self)

    async def on_timeout(self):
        for child in self.children:
            child.disabled = True
        if self.message:
            try:
                await self.message.edit(view=self)
            except discord.HTTPException:
                pass


def register_tensei(bot):
    @bot.tree.command(name='tensei', description='世界でもっとも深いガチャ')
    async def tensei(interaction: discord.Interaction):
        await interaction.response.defer(thinking=True)
        view = TenseiView(interaction.user.id)
        view.message = await interaction.edit_original_response(
            embed=life_embed(generate_life(), interaction.user.display_name), view=view,
            allowed_mentions=discord.AllowedMentions.none())
