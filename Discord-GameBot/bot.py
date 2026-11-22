import os, random, logging, unicodedata
from dataclasses import dataclass, field
from typing import Optional
import discord
from discord import app_commands
from discord.ext import commands, tasks
from dotenv import load_dotenv

load_dotenv()
TOKEN = os.getenv('DISCORD_BOT_TOKEN')
TEST_GUILD_ID = os.getenv('TEST_GUILD_ID')
CLOUD_MODE = os.getenv('CLOUD_MODE', '').lower() in ('1', 'true', 'yes')
MIN_PLAYERS, MAX_PLAYERS = 3, 20
logging.basicConfig(level=logging.INFO)
log = logging.getLogger('wordwolfex')

# ジャンルごとのお題ペア。NGワードはジャンルに依存しない共通リストを使用します。
GENRES = {
 '食べ物': {'pairs':[
  ('りんご','なし'),('ラーメン','うどん'),('カレー','シチュー'),('寿司','刺身'),('チョコレート','クッキー'),
  ('オムライス','親子丼'),('焼肉','しゃぶしゃぶ'),('ピザ','お好み焼き'),('たこ焼き','たい焼き'),('餃子','焼売'),
  ('プリン','ゼリー'),('ケーキ','タルト'),('そば','そうめん'),('ハンバーグ','ステーキ'),('唐揚げ','天ぷら'),
  ('おにぎり','サンドイッチ'),('コーヒー','紅茶'),('アイスクリーム','かき氷'),('味噌汁','豚汁'),('メロンパン','クロワッサン'),
  ('牛丼','親子丼'),('ポテトチップス','フライドポテト'),('いちご','さくらんぼ'),('パンケーキ','ホットケーキ'),
  ('キムチ','漬物'),('おでん','鍋'),('みかん','グレープフルーツ'),('チーズ','バター'),('ドーナツ','マフィン'),
  ('ビーフシチュー','ハヤシライス')
 ]},
 '動物': {'pairs':[
  ('犬','猫'),('ライオン','トラ'),('イルカ','シャチ'),('ウサギ','ハムスター'),('ワシ','タカ'),('カメ','トカゲ'),
  ('キツネ','タヌキ'),('ヒョウ','チーター'),('ワニ','ヘビ'),('ペンギン','アザラシ'),('パンダ','コアラ'),
  ('シマウマ','キリン'),('クマ','オオカミ'),('サメ','エイ'),('カラス','ハト'),('フクロウ','ミミズク'),
  ('カエル','イモリ'),('馬','ロバ'),('羊','ヤギ'),('ブタ','イノシシ'),('モルモット','チンチラ'),
  ('クジラ','イルカ'),('ハチ','アリ'),('カブトムシ','クワガタ'),('ラクダ','アルパカ'),
  ('リス','プレーリードッグ'),('ワラビー','カンガルー'),('ニワトリ','アヒル'),('ハリネズミ','ヤマアラシ'),
  ('ホッキョクグマ','ヒグマ')
 ]},
 '乗り物': {'pairs':[
  ('電車','新幹線'),('飛行機','ヘリコプター'),('自転車','バイク'),('船','フェリー'),('バス','タクシー'),
  ('地下鉄','路面電車'),('トラック','トレーラー'),('救急車','消防車'),('パトカー','白バイ'),('モノレール','ロープウェイ'),
  ('キックボード','スケートボード'),('ヨット','ボート'),('潜水艦','客船'),('軽自動車','普通自動車'),('スクーター','原付'),
  ('リニアモーターカー','新幹線'),('気球','飛行船'),('除雪車','ブルドーザー'),('キャンピングカー','バン'),
  ('路線バス','高速バス'),('馬車','人力車'),('セグウェイ','電動キックボード'),('ゴンドラ','ケーブルカー'),
  ('戦車','装甲車'),('フォークリフト','クレーン車'),('貨物列車','旅客列車'),('水上バイク','モーターボート'),
  ('電動自転車','マウンテンバイク'),('タンクローリー','ミキサー車'),('トロッコ','汽車')
 ]},
 '学校': {'pairs':[
  ('学校','塾'),('先生','講師'),('数学','理科'),('テスト','宿題'),('教室','図書室'),('文化祭','体育祭'),
  ('修学旅行','遠足'),('校長先生','教頭先生'),('部活動','委員会'),('給食','学食'),('黒板','ホワイトボード'),
  ('鉛筆','シャープペンシル'),('消しゴム','修正テープ'),('国語','英語'),('社会','地理'),('体育','保健'),
  ('夏休み','冬休み'),('通知表','テスト結果'),('制服','ジャージ'),('上履き','体育館シューズ'),
  ('運動会','球技大会'),('音楽室','美術室'),('保健室','職員室'),('生徒会','学級委員'),
  ('大学','専門学校'),('小学校','中学校'),('高校','高専'),('ノート','ルーズリーフ'),
  ('昼休み','放課後'),('受験','定期テスト')
 ]},
 'スポーツ': {'pairs':[
  ('サッカー','バスケットボール'),('野球','ソフトボール'),('テニス','バドミントン'),('水泳','陸上'),('卓球','バレーボール'),
  ('柔道','剣道'),('ラグビー','アメリカンフットボール'),('スキー','スノーボード'),('ボクシング','キックボクシング'),
  ('ゴルフ','ボウリング'),('マラソン','駅伝'),('体操','新体操'),('ハンドボール','ドッジボール'),
  ('フェンシング','弓道'),('乗馬','競馬'),('相撲','レスリング'),('サーフィン','スケートボード'),
  ('短距離走','長距離走'),('ビーチバレー','バレーボール'),('フットサル','サッカー'),('クリケット','野球'),
  ('カーリング','ボウリング'),('トライアスロン','デュアスロン'),('空手','テコンドー'),
  ('卓球','テニス'),('バスケットボール','ハンドボール'),('水球','水泳'),('ボート競技','カヌー'),
  ('フィギュアスケート','アイスホッケー'),('モータースポーツ','自転車競技')
 ]},
 '自然': {'pairs':[
  ('海','プール'),('夏','冬'),('桜','梅'),('雨','雪'),('山','森'),('川','湖'),('太陽','月'),('虹','オーロラ'),
  ('台風','竜巻'),('地震','火山噴火'),('砂漠','草原'),('洞窟','鍾乳洞'),('滝','渓谷'),('氷山','氷河'),
  ('朝焼け','夕焼け'),('雷','稲妻'),('霧','雲'),('温泉','間欠泉'),('竹林','森林'),('サンゴ礁','岩礁'),
  ('満月','新月'),('流れ星','彗星'),('紅葉','新緑'),('干潟','湿地'),('高原','平野'),
  ('雪崩','土砂崩れ'),('潮干狩り','釣り'),('春','秋'),('湖','沼'),('海岸','砂浜')
 ]}
}
GENRE_NAMES = list(GENRES)

# どのジャンルでも会話中に出やすい、共通NGワード候補。
# 実際に各プレイヤーへ配布する語は、このリストから重複なしで抽選します。
COMMON_NG_WORDS = [
 'どっち','どちら','どっちも','どちらも','好き','大好き','嫌い','苦手','得意','有名','人気','普通',
 '一般的','たぶん','例えば','つまり','なんか','たしかに','そうそう','わかる','知らない','聞いたことある',
 '見たことある','行ったことある','食べたことある','使ったことある','持っている','持ってる','買える',
 '売っている','売ってる','食べる','飲む','使う','行く','見る','聞く','買う','作る','遊ぶ','飼う',
 '大きい','小さい','高い','安い','長い','短い','重い','軽い','速い','遅い','硬い','柔らかい',
 '甘い','辛い','苦い','酸っぱい','おいしい','かわいい','かっこいい','きれい','便利','必要',
 '日本','海外','世界','昔','今','最近','未来','子供','大人','友達','家族','学校','家','店','場所',
 '種類','名前','形','色','味','匂い','音','見た目','値段','季節','毎日','休日','旅行','仕事',
 '有料','無料','簡単','難しい','危ない','安全','本物','偽物','有名人','テレビ','ネット','動画',
 '一番','二番','最初','最後','たくさん','少し','絶対','本当に','実際','結構','かなり','多分',
 'どこ','いつ','誰','何','どうして','なぜ','どうやって','知ってる','知られている','珍しい'
]

@dataclass
class Player:
    user: discord.User
    role: str = ''
    word: str = ''
    ng_words: list[str] = field(default_factory=list)
    violated_words: set[str] = field(default_factory=set)
    role_revealed: bool = False

@dataclass
class Game:
    reward_id: str = field(default_factory=lambda: __import__('uuid').uuid4().hex, init=False)
    guild_id: int
    channel_id: int
    host_id: int
    players: dict[int, Player] = field(default_factory=dict)
    phase: str = 'recruiting'
    genre: str = '食べ物'
    topic_pair: tuple[str, str] = ('', '')
    max_wolves: int = 1
    ng_count: int = 1
    allow_no_wolf: bool = False
    ng_enabled: bool = True
    reveal_role_on_ng: bool = False
    end_if_wolf_ng: bool = False
    wolf_ids: set[int] = field(default_factory=set)
    board_message_id: Optional[int] = None
    votes: dict[int, Optional[int]] = field(default_factory=dict)

games: dict[int, Game] = {}
intents = discord.Intents.default()
intents.message_content = True
STATUS_MESSAGES = (
    '✨made by ちょこもち！',
    '🤔メンションして聞いてね！',
    '💬話してレベルアップ！',
    '📢週1以上の頻度でアプデ！',
    '🏆目指せ全知全能bot！',
    '👳アキネーター実装！',
    '💎ジュエルを稼いで特典GET！',
    '✅powered by MWS！',
    '🎼音声/動画ファイルを再生可！',
    '🥇ボドゲで目指せ優勝！',
    '🎁/giftで贈り物を送れる！',
)
bot = commands.Bot(command_prefix='!', intents=intents,
                   activity=discord.CustomActivity(name=STATUS_MESSAGES[0]))


@tasks.loop(seconds=3)
async def rotate_status():
    await bot.wait_until_ready()
    text = STATUS_MESSAGES[rotate_status.current_loop % len(STATUS_MESSAGES)]
    await bot.change_presence(activity=discord.CustomActivity(name=text))

def safe_name(user):
    return discord.utils.escape_markdown(getattr(user, 'display_name', user.name))

def is_host(interaction, game):
    return interaction.user.id == game.host_id or (isinstance(interaction.user, discord.Member) and interaction.user.guild_permissions.manage_guild)

def choose_topic(game):
    game.topic_pair = random.choice(GENRES[game.genre]['pairs'])

def recruitment_embed(game):
    embed = discord.Embed(title='🐺 wordwolfex — 参加者募集中', description=(
        'ボタンから参加してください。参加ボタンをもう一度押すと退出できます。\n\n'
        f'**今回のジャンル：{game.genre}**\nお題は開始時に各参加者へDMで配布します。'), color=discord.Color.blue())
    names = [safe_name(p.user) for p in game.players.values()]
    embed.add_field(name=f'参加者 ({len(names)}/{MAX_PLAYERS})', value='\n'.join('• '+n for n in names) or 'まだいません', inline=False)
    embed.add_field(name='現在の設定', value=(f'最大ウルフ数：{game.max_wolves}人\n1人あたりのNGワード数：{game.ng_count}個\nウルフなし試合：{"有効（20%）" if game.allow_no_wolf else "無効"}\nNGワード：{"有効" if game.ng_enabled else "無効"}\nNG違反時の役職公開：{"有効" if game.reveal_role_on_ng else "無効"}\nNG違反したウルフの即終了：{"有効" if game.end_if_wolf_ng else "無効"}'), inline=False)
    embed.set_footer(text=f'募集主ID: {game.host_id} | 最低{MIN_PLAYERS}人')
    return embed

async def update_board(interaction, game):
    if not game.board_message_id or not interaction.channel: return
    try:
        msg = await interaction.channel.fetch_message(game.board_message_id)
        await msg.edit(embed=recruitment_embed(game), view=RecruitmentView(game.guild_id))
    except (discord.NotFound, discord.Forbidden, discord.HTTPException):
        log.exception('募集ボードを更新できませんでした')

class SettingsView(discord.ui.View):
    def __init__(self, game):
        super().__init__(timeout=90)
        self.add_item(WolfCountSelect(game.guild_id))
        self.add_item(NGCountSelect(game.guild_id))
        self.add_item(NoWolfSelect(game.guild_id))
        self.add_item(NGSettingsButton(game.guild_id))

class BaseSettingSelect(discord.ui.Select):
    async def get_game_and_check(self, interaction):
        game = games.get(self.guild_id)
        if not game or game.phase != 'recruiting':
            await interaction.response.send_message('募集時のみ設定できます。', ephemeral=True); return None
        if not is_host(interaction, game):
            await interaction.response.send_message('募集主またはサーバー管理者のみ変更できます。', ephemeral=True); return None
        return game

class WolfCountSelect(BaseSettingSelect):
    def __init__(self, gid):
        self.guild_id = gid
        super().__init__(placeholder='最大ウルフ数を選択', options=[discord.SelectOption(label=f'{n}人', value=str(n)) for n in range(1,4)], row=0)
    async def callback(self, interaction):
        game = await self.get_game_and_check(interaction)
        if game:
            game.max_wolves = int(self.values[0]); await interaction.response.send_message(f'最大ウルフ数を{game.max_wolves}人に設定しました。', ephemeral=True); await update_board(interaction, game)

class NGCountSelect(BaseSettingSelect):
    def __init__(self, gid):
        self.guild_id = gid
        super().__init__(placeholder='1人あたりのNGワード数を選択', options=[discord.SelectOption(label=f'{n}個', value=str(n)) for n in range(1,4)], row=1)
    async def callback(self, interaction):
        game = await self.get_game_and_check(interaction)
        if game:
            game.ng_count = int(self.values[0]); await interaction.response.send_message(f'NGワード数を1人あたり{game.ng_count}個に設定しました。', ephemeral=True); await update_board(interaction, game)

class NoWolfSelect(BaseSettingSelect):
    def __init__(self, gid):
        self.guild_id = gid
        super().__init__(placeholder='ウルフなし試合の設定', options=[discord.SelectOption(label='無効', value='off'), discord.SelectOption(label='有効（20%）', value='on')], row=2)
    async def callback(self, interaction):
        game = await self.get_game_and_check(interaction)
        if game:
            game.allow_no_wolf = self.values[0] == 'on'
            await interaction.response.send_message('ウルフなし試合を'+('有効（20%）' if game.allow_no_wolf else '無効')+'にしました。', ephemeral=True)
            await update_board(interaction, game)


class NGSettingsView(discord.ui.View):
    def __init__(self, game):
        super().__init__(timeout=90)
        self.add_item(NGEnabledSelect(game.guild_id))
        self.add_item(RevealRoleSelect(game.guild_id))
        self.add_item(EndOnWolfNGSelect(game.guild_id))


class NGSettingsButton(discord.ui.Button):
    def __init__(self, guild_id):
        super().__init__(label='NGワード詳細設定', style=discord.ButtonStyle.secondary, emoji='🚫', row=3)
        self.guild_id = guild_id

    async def callback(self, interaction):
        game = games.get(self.guild_id)
        if not game or game.phase != 'recruiting':
            return await interaction.response.send_message('募集時のみ設定できます。', ephemeral=True)
        if not is_host(interaction, game):
            return await interaction.response.send_message('募集主またはサーバー管理者のみ変更できます。', ephemeral=True)
        await interaction.response.send_message('NGワード関連の設定を選択してください。', view=NGSettingsView(game), ephemeral=True)


class NGEnabledSelect(BaseSettingSelect):
    def __init__(self, gid):
        self.guild_id = gid
        super().__init__(placeholder='NGワードの有効 / 無効', options=[
            discord.SelectOption(label='有効', value='on'),
            discord.SelectOption(label='無効', value='off')
        ], row=0)

    async def callback(self, interaction):
        game = await self.get_game_and_check(interaction)
        if game:
            game.ng_enabled = self.values[0] == 'on'
            await interaction.response.send_message('NGワードを' + ('有効' if game.ng_enabled else '無効') + 'にしました。', ephemeral=True)
            await update_board(interaction, game)


class RevealRoleSelect(BaseSettingSelect):
    def __init__(self, gid):
        self.guild_id = gid
        super().__init__(placeholder='NG違反時に役職を公開するか', options=[
            discord.SelectOption(label='公開する', value='on'),
            discord.SelectOption(label='公開しない', value='off')
        ], row=1)

    async def callback(self, interaction):
        game = await self.get_game_and_check(interaction)
        if game:
            game.reveal_role_on_ng = self.values[0] == 'on'
            await interaction.response.send_message('NG違反時の役職公開を' + ('有効' if game.reveal_role_on_ng else '無効') + 'にしました。', ephemeral=True)
            await update_board(interaction, game)


class EndOnWolfNGSelect(BaseSettingSelect):
    def __init__(self, gid):
        self.guild_id = gid
        super().__init__(placeholder='ウルフがNG違反したら試合終了', options=[
            discord.SelectOption(label='終了する（市民勝利）', value='on'),
            discord.SelectOption(label='終了しない', value='off')
        ], row=2)

    async def callback(self, interaction):
        game = await self.get_game_and_check(interaction)
        if game:
            game.end_if_wolf_ng = self.values[0] == 'on'
            await interaction.response.send_message('ウルフのNG違反時は' + ('即時終了（市民勝利）' if game.end_if_wolf_ng else '試合を続行') + 'にしました。', ephemeral=True)
            await update_board(interaction, game)


class RecruitmentView(discord.ui.View):
    def __init__(self, guild_id):
        super().__init__(timeout=None); self.guild_id = guild_id
    def get_game(self): return games.get(self.guild_id)

    @discord.ui.button(label='参加 / 退出', style=discord.ButtonStyle.success, emoji='🙋', custom_id='wordwolfex:join', row=0)
    async def join(self, interaction, button):
        game = self.get_game()
        if not game or game.phase != 'recruiting': return await interaction.response.send_message('現在、参加受付を行っていません。', ephemeral=True)
        if interaction.user.bot: return await interaction.response.send_message('Botは参加できません。', ephemeral=True)
        if interaction.user.id in game.players:
            del game.players[interaction.user.id]; reply = '参加を取り消しました。'
        else:
            if len(game.players) >= MAX_PLAYERS: return await interaction.response.send_message(f'定員は{MAX_PLAYERS}人です。', ephemeral=True)
            game.players[interaction.user.id] = Player(interaction.user); reply = '参加しました！'
        await interaction.response.send_message(reply, ephemeral=True); await update_board(interaction, game)

    @discord.ui.button(label='スタート', style=discord.ButtonStyle.primary, emoji='▶️', custom_id='wordwolfex:start', row=0)
    async def start(self, interaction, button):
        game = self.get_game()
        if not game or game.phase != 'recruiting': return await interaction.response.send_message('募集は終了しています。', ephemeral=True)
        if not is_host(interaction, game): return await interaction.response.send_message('募集主またはサーバー管理者のみ操作できます。', ephemeral=True)
        if len(game.players) < MIN_PLAYERS: return await interaction.response.send_message(f'最低{MIN_PLAYERS}人必要です。現在{len(game.players)}人です。', ephemeral=True)
        game.phase = 'starting'; await interaction.response.defer(ephemeral=True); await start_game(interaction, game)

    @discord.ui.button(label='ジャンル変更', style=discord.ButtonStyle.secondary, emoji='🗂️', custom_id='wordwolfex:genre', row=0)
    async def change_genre(self, interaction, button):
        game = self.get_game()
        if not game or game.phase != 'recruiting': return await interaction.response.send_message('募集時のみジャンルを変更できます。', ephemeral=True)
        if not is_host(interaction, game): return await interaction.response.send_message('募集主またはサーバー管理者のみ変更できます。', ephemeral=True)
        game.genre = random.choice([g for g in GENRE_NAMES if g != game.genre]); choose_topic(game)
        await interaction.response.send_message(f'ジャンルを **{game.genre}** に変更しました。', ephemeral=True); await update_board(interaction, game)

    @discord.ui.button(label='設定', style=discord.ButtonStyle.secondary, emoji='⚙️', custom_id='wordwolfex:settings', row=0)
    async def settings(self, interaction, button):
        game = self.get_game()
        if not game or game.phase != 'recruiting': return await interaction.response.send_message('募集時のみ設定できます。', ephemeral=True)
        if not is_host(interaction, game): return await interaction.response.send_message('募集主またはサーバー管理者のみ変更できます。', ephemeral=True)
        await interaction.response.send_message('ゲーム設定を選択してください。', view=SettingsView(game), ephemeral=True)

def player_dm(player, others, genre):
    # 役職と本人のNGワードはDMに含めません。
    embed = discord.Embed(title='🐺 wordwolfex — あなたへの情報', color=discord.Color.blurple())
    embed.add_field(name='今回のジャンル', value=f'**{genre}**', inline=False)
    embed.add_field(name='あなたのお題', value=f'**{player.word}**', inline=False)
    if player.ng_words:
        lines = [f'• {safe_name(p.user)}：' + ' / '.join(f'`{w}`' for w in p.ng_words) for p in others]
        embed.add_field(name='他プレイヤーのNGワード', value='\n'.join(lines)[:1024] or 'なし', inline=False)
    else:
        embed.add_field(name='NGワード', value='この試合ではNGワードは無効です。', inline=False)
    embed.add_field(name='ルール', value='• 自分のお題に沿って会話してください。\n• 他のプレイヤーのNGワードを言わせる作戦も有効です。\n• 自分の役職と自分のNGワードは表示されません。\n• NGワードを発言すると設定に応じて検知・公開されます。\n• 議論終了後、投票で追放する人を選びます。', inline=False)
    return embed

async def start_game(interaction, game):
    if not game.topic_pair: choose_topic(game)
    ids = list(game.players)
    no_wolf = game.allow_no_wolf and random.random() < 0.20
    wolf_count = 0 if no_wolf else min(game.max_wolves, len(ids)-1)
    game.wolf_ids = set(random.sample(ids, wolf_count)) if wolf_count else set()
    citizen_word, wolf_word = game.topic_pair
    needed = len(ids) * game.ng_count if game.ng_enabled else 0
    if needed > len(COMMON_NG_WORDS):
        game.phase = 'recruiting'
        return await interaction.followup.send('共通NGワード候補が不足しています。COMMON_NG_WORDSに候補を追加してください。', ephemeral=True)
    chosen = random.sample(COMMON_NG_WORDS, needed) if needed else []
    for index, (pid, player) in enumerate(game.players.items()):
        player.role = 'ウルフ' if pid in game.wolf_ids else '市民'
        player.word = wolf_word if pid in game.wolf_ids else citizen_word
        player.ng_words = chosen[index*game.ng_count:(index+1)*game.ng_count]
        player.violated_words.clear()
        player.role_revealed = False
    failed = []
    for pid, player in game.players.items():
        others = [p for other_id, p in game.players.items() if other_id != pid]
        try: await player.user.send(embed=player_dm(player, others, game.genre))
        except (discord.Forbidden, discord.HTTPException): failed.append(safe_name(player.user))
    if failed:
        game.phase = 'recruiting'; game.players.clear()
        await interaction.channel.send('⚠️ DMを送信できない参加者がいたため開始を中止しました。対象：'+', '.join(failed)+'\nDM受信設定を確認して再参加してください。')
        return await interaction.followup.send('DM送信に失敗したため開始を中止しました。', ephemeral=True)
    game.phase = 'discussion'
    wolf_info = 'ウルフなし試合です。' if wolf_count == 0 else f'ウルフは{wolf_count}人です。'
    await interaction.channel.send(f'✅ **wordwolfex開始！**\nジャンル：**{game.genre}**\n{wolf_info}\n各参加者に個別DMを送信しました。議論終了後、募集主が `/wordwolfex` を再実行してください。')
    await interaction.followup.send('ゲームを開始しました。参加者全員にDMを送信しました。', ephemeral=True)

class VoteView(discord.ui.View):
    def __init__(self, game):
        super().__init__(timeout=180); self.guild_id = game.guild_id
        for pid, player in game.players.items():
            b = discord.ui.Button(label=f'追放：{player.user.display_name}'[:80], style=discord.ButtonStyle.secondary, custom_id=f'wordwolfex:vote:{pid}')
            b.callback = self.make_callback(pid); self.add_item(b)
        b = discord.ui.Button(label='誰も追放しない', style=discord.ButtonStyle.success, emoji='🕊️', custom_id='wordwolfex:vote:abstain')
        b.callback = self.make_callback(None); self.add_item(b)
    def make_callback(self, target):
        async def callback(interaction):
            game = games.get(self.guild_id)
            if not game or game.phase != 'voting': return await interaction.response.send_message('現在投票できません。', ephemeral=True)
            if interaction.user.id not in game.players: return await interaction.response.send_message('ゲーム参加者のみ投票できます。', ephemeral=True)
            if interaction.user.id in game.votes: return await interaction.response.send_message('投票済みです。', ephemeral=True)
            if target == interaction.user.id: return await interaction.response.send_message('自分には投票できません。', ephemeral=True)
            game.votes[interaction.user.id] = target
            await interaction.response.send_message('投票を受け付けました。', ephemeral=True)
            if len(game.votes) == len(game.players): await resolve_votes(interaction.channel, game)
        return callback
    async def on_timeout(self):
        game = games.get(self.guild_id); channel = bot.get_channel(game.channel_id) if game else None
        if game and channel and game.phase == 'voting': await resolve_votes(channel, game)

async def resolve_votes(channel, game):
    if game.phase != 'voting': return
    counts = {}
    for target in game.votes.values(): counts[target] = counts.get(target, 0) + 1
    if not counts: leaders, eliminated = [], None
    else:
        high = max(counts.values()); leaders = [target for target, count in counts.items() if count == high]
        eliminated = leaders[0] if len(leaders) == 1 else None
    tied = len(leaders) != 1
    if not game.wolf_ids:
        winner = '市民' if eliminated is None else 'ウルフ'
        note = 'ウルフなし試合：誰も追放しなければ市民勝利です。'
    elif tied or eliminated is None:
        winner, note = 'ウルフ', '同票または追放者なしのためウルフ勝利です。'
    elif eliminated in game.wolf_ids:
        winner, note = '市民', 'ウルフを追放したため市民勝利です。'
    else:
        winner, note = 'ウルフ', '市民を追放したためウルフ勝利です。'
    embed = discord.Embed(title='🐺 wordwolfex — GAME RESULT', description=f'**勝利陣営：{winner}**\n{note}', color=discord.Color.green() if winner == '市民' else discord.Color.red())
    embed.add_field(name='追放結果', value='誰も追放されませんでした。' if eliminated is None else safe_name(game.players[eliminated].user), inline=False)
    await channel.send(embed=embed)
    lines = []
    for pid, p in game.players.items():
        role = 'ウルフ' if pid in game.wolf_ids else '市民'
        violated = f' / NG違反：{", ".join(sorted(p.violated_words))}' if p.violated_words else ''
        lines.append(f'**{safe_name(p.user)}** — {role} / お題：`{p.word}` / NG：{", ".join(f"`{w}`" for w in p.ng_words)}{violated}')
    chunk = ''
    for line in lines:
        if len(chunk)+len(line)+1 > 1800:
            await channel.send('**全員の役職・お題・NGワード**\n'+chunk); chunk = ''
        chunk += line+'\n'
    if chunk: await channel.send('**全員の役職・お題・NGワード**\n'+chunk)
    from party_rewards import reward_party
    outcomes = {uid: ((uid in game.wolf_ids) == (winner == 'ウルフ')) for uid in game.players}
    await reward_party(bot, channel.guild, channel, game.reward_id, outcomes)
    games.pop(game.guild_id, None)


async def finish_by_wolf_ng(channel, game, player, word):
    """ウルフのNG違反で試合を即時終了し、市民勝利として結果を公開する。"""
    if game.phase != 'discussion':
        return
    game.phase = 'finished'
    embed = discord.Embed(
        title='🚨 NG WORD — GAME END',
        description=(
            f'**{safe_name(player.user)}** がNGワード `「{word}」` を発言しました。\n'
            '**発言者はウルフだったため、特別ルールにより試合終了です。**\n'
            '🏆 **市民陣営の勝利！**'
        ),
        color=discord.Color.gold()
    )
    await channel.send(embed=embed)
    lines = []
    for p in game.players.values():
        role = 'ウルフ' if p.user.id in game.wolf_ids else '市民'
        violated = f' / NG違反：{", ".join(sorted(p.violated_words))}' if p.violated_words else ''
        lines.append(f'**{safe_name(p.user)}** — {role} / お題：`{p.word}` / NG：{", ".join(f"`{w}`" for w in p.ng_words) or "なし"}{violated}')
    chunk = ''
    for line in lines:
        if len(chunk) + len(line) + 1 > 1800:
            await channel.send('**全員の役職・お題・NGワード**\n' + chunk)
            chunk = ''
        chunk += line + '\n'
    if chunk:
        await channel.send('**全員の役職・お題・NGワード**\n' + chunk)
    from party_rewards import reward_party
    await reward_party(bot, channel.guild, channel, game.reward_id, {uid: uid not in game.wolf_ids for uid in game.players})
    games.pop(game.guild_id, None)


@bot.event
async def on_message(message):
    if message.author.bot or not message.guild: return
    game = games.get(message.guild.id)
    if game and game.phase == 'discussion' and message.author.id in game.players and game.ng_enabled:
        player = game.players[message.author.id]
        content = unicodedata.normalize('NFKC', message.content).casefold()
        newly_violated = []
        for word in player.ng_words:
            normalized_word = unicodedata.normalize('NFKC', word).casefold()
            if normalized_word in content and word not in player.violated_words:
                player.violated_words.add(word)
                newly_violated.append(word)

        if newly_violated:
            first_word = newly_violated[0]
            is_wolf = message.author.id in game.wolf_ids

            if is_wolf and game.end_if_wolf_ng:
                await finish_by_wolf_ng(message.channel, game, player, first_word)
            elif game.reveal_role_on_ng and not player.role_revealed:
                player.role_revealed = True
                role = 'ウルフ' if is_wolf else '市民'
                await message.channel.send(
                    f'🚨 **NGワード検知！** {safe_name(player.user)} がNGワードを発言しました。\n'
                    f'発言したNGワード：`{first_word}`\n'
                    f'公開された役職：**{role}**'
                )
    await bot.process_commands(message)

@bot.event
async def on_ready():
    log.info('Logged in as %s', bot.user)
    if not rotate_status.is_running():
        rotate_status.start()
    if CLOUD_MODE and (not hasattr(bot, '_minecraft_sync_task') or bot._minecraft_sync_task.done()):
        import asyncio
        from snapshot_sync import sync_loop
        bot._minecraft_sync_task = asyncio.create_task(sync_loop(bot))
    try:
        if CLOUD_MODE:
            from command_sync import sync_cloud_commands
            await sync_cloud_commands(bot, TEST_GUILD_ID)
        elif TEST_GUILD_ID:
            guild = discord.Object(id=int(TEST_GUILD_ID)); bot.tree.copy_global_to(guild=guild); await bot.tree.sync(guild=guild)
        else: await bot.tree.sync()
    except Exception: log.exception('Command sync failed')

# Bridgeは同じBotアカウントでDiscordへ接続するが、Minecraftの処理はNode.js側が担当する。
# ここではコマンド定義だけを一括同期し、Bridgeが受け取ったInteractionへ応答する。
# これにより、2つのプロセスが別々に同期してコマンドを消し合うことを防ぐ。
@bot.tree.command(name='link', description='Minecraftのアカウントと連携します')
@app_commands.describe(code='マインクラフト内で !link を実行すると表示される8桁のコード')
async def bridge_link(interaction: discord.Interaction, code: str):
    if CLOUD_MODE:
        await interaction.response.send_message('クラウド版では新規のMinecraft連携を受け付けていません。', ephemeral=True)
        return
    log.debug('Bridge handles /link')


bridge_player = app_commands.Group(name='player', description='保存済みのプレイヤー情報を表示します')

@bridge_player.command(name='status', description='ゲームを選択してプレイヤーのステータスを表示します')
@app_commands.describe(
    game='参照するゲーム',
    name='プレイヤー名',
)
@app_commands.choices(game=[
    app_commands.Choice(name='unlimited', value='unlimited'),
    app_commands.Choice(name='MurderMysteryRe:MASTER', value='MurderMysteryRe:MASTER'),
    app_commands.Choice(name='MINIGAMER', value='MINIGAMER'),
])
async def bridge_player_status(
    interaction: discord.Interaction,
    game: str,
    name: str,
):
    if CLOUD_MODE:
        from cloud_status import respond
        await respond(interaction, game, name)
        return
    log.debug('Bridge handles /player status')


@bot.tree.command(name='vs', description='選んだワールドの2人のステータスを比較します')
@app_commands.describe(name='自身のMinecraft名', opponent='対戦相手のMinecraft名', world='比較するワールド')
@app_commands.choices(world=[
    app_commands.Choice(name='MINIGAMER', value='MINIGAMER'),
    app_commands.Choice(name='MurderMysteryRe:MASTER', value='MurderMysteryRe:MASTER'),
    app_commands.Choice(name='unlimited', value='unlimited'),
])
async def bridge_vs(interaction: discord.Interaction, name: str, opponent: str, world: str):
    if CLOUD_MODE:
        from cloud_status import respond
        await respond(interaction, world, name, opponent)
        return
    log.debug('Bridge handles /vs')

bot.tree.add_command(bridge_player)


@bot.tree.command(name='delete', description='連携済みアカウントを削除します（管理者専用）')
@app_commands.describe(userid='削除するDiscordユーザーID（数字のみ）')
async def bridge_delete(interaction: discord.Interaction, userid: str):
    if CLOUD_MODE:
        await interaction.response.send_message('クラウド版では連携データの削除を受け付けていません。', ephemeral=True)
        return
    log.debug('Bridge handles /delete')

@bot.tree.command(name='wordwolfex', description='ワードウルフEXを開始、または議論を終了します')
@app_commands.guild_only()
async def wordwolfex(interaction: discord.Interaction):
    gid = interaction.guild_id; game = games.get(gid)
    if game is None:
        game = Game(guild_id=gid, channel_id=interaction.channel_id, host_id=interaction.user.id)
        game.genre = random.choice(GENRE_NAMES); choose_topic(game); games[gid] = game
        await interaction.response.send_message(embed=recruitment_embed(game), view=RecruitmentView(gid))
        msg = await interaction.original_response(); game.board_message_id = msg.id; return
    if game.channel_id != interaction.channel_id:
        return await interaction.response.send_message('このサーバーでは別のチャンネルでゲームが進行中です。', ephemeral=True)
    if not is_host(interaction, game):
        return await interaction.response.send_message('募集主またはサーバー管理者のみ実行できます。', ephemeral=True)
    if game.phase == 'discussion':
        game.phase = 'voting'
        await interaction.response.send_message('🗳️ **議論終了！投票してください。**\n追放したいプレイヤー、または「誰も追放しない」を選んでください。', view=VoteView(game))
    elif game.phase == 'recruiting': await interaction.response.send_message('現在、参加者を募集しています。募集ボードのボタンを使用してください。', ephemeral=True)
    elif game.phase == 'starting': await interaction.response.send_message('ゲーム開始処理中です。', ephemeral=True)
    elif game.phase == 'voting': await interaction.response.send_message('現在投票中です。', ephemeral=True)


# --- 1/8192 GAME（個人用の運試し） ---
# ユーザーIDごとに進行状況を保持し、同じユーザーの同時起動を防ぎます。
from game_8192 import register_8192
register_8192(bot)

@bot.tree.error
async def on_app_command_error(interaction, error):
    original = getattr(error, 'original', error)
    log.error('Application command error: %r', error, exc_info=(type(original), original, original.__traceback__))
    if isinstance(original, discord.NotFound) and original.code == 10062:
        log.warning('Interaction expired before acknowledgement; check receive age, network latency and duplicate bot processes')
        return
    try:
        msg = 'コマンドの実行中にエラーが発生しました。Botのログを確認してください。'
        if interaction.response.is_done(): await interaction.followup.send(msg, ephemeral=True)
        else: await interaction.response.send_message(msg, ephemeral=True)
    except discord.HTTPException: pass

if not TOKEN: raise RuntimeError('DISCORD_BOT_TOKEN が設定されていません。.env を確認してください。')
if CLOUD_MODE:
    for command_name in ('link', 'delete'):
        bot.tree.remove_command(command_name)

from tensei import register_tensei
register_tensei(bot)
from kakigoori import register_kakigoori
register_kakigoori(bot)
from gemini_chat import register_gemini
gemini_service = register_gemini(bot)
from aki import register_aki
register_aki(bot, gemini_service)
from leveling import register_leveling
bot.leveling_service = register_leveling(bot)
from casino import register_casino
casino_service = register_casino(bot)
from solo_rewards import SoloRewards
bot.solo_rewards = SoloRewards(casino_service.store, bot.leveling_service.store)
from shop import register_shop
register_shop(bot)
from party_rewards import PartyRewards
from guess_number import register_guess
bot.party_rewards = PartyRewards()
register_guess(bot)
from social_games import register_social_games
register_social_games(bot)
from message_apps import register_message_apps
register_message_apps(bot)
from othello import register_othello
register_othello(bot)
from sound import register_sound
sound_service = register_sound(bot)
from youtube_play import register_youtube_play
register_youtube_play(bot, sound_service)
from soundboard import register_soundboard
register_soundboard(bot, sound_service)
from voice_reading import register_voice
register_voice(bot, sound_service)
from ping import register_ping
register_ping(bot)

from storage import register_backups
register_backups(bot)
from player_backup import register as register_player_backup
register_player_backup(bot)
bot.run(TOKEN)
