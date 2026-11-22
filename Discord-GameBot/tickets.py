"""Ticket balances, paged guild rankings and owner-authorized weighted draws."""
import asyncio
import logging
import discord
from discord import app_commands
from ticket_store import TicketStore

OWNER_ID = 799820074990698496
PAGE_SIZE = 10
log = logging.getLogger(__name__)


def percentage(count, total):
    return f'{count / total * 100:.4f}%' if total else '0.0000%'


def user_name(bot, guild, user_id):
    member = guild.get_member(user_id) or bot.get_user(user_id)
    name = getattr(member, 'display_name', f'ユーザーID {user_id}')
    return discord.utils.escape_markdown(str(name)).replace('@', '＠')[:80]


async def load_names(bot, guild, rows):
    names = {}
    semaphore = asyncio.Semaphore(3)
    async def resolve(row):
        user_id = row['user_id']
        member = guild.get_member(user_id) or bot.get_user(user_id)
        if member is None:
            try:
                async with semaphore:
                    member = await asyncio.wait_for(bot.fetch_user(user_id), timeout=5)
            except (discord.DiscordException, asyncio.TimeoutError):
                pass
        if member is not None:
            names[user_id] = discord.utils.escape_markdown(str(member.display_name)).replace('@', '＠')[:80]
    await asyncio.gather(*(resolve(row) for row in rows))
    return names


def ranking_embed(bot, guild, rows, page=0, names=None):
    pages = max(1, (len(rows) + PAGE_SIZE - 1) // PAGE_SIZE)
    page = max(0, min(page, pages - 1))
    total = sum(row['count'] for row in rows)
    embed = discord.Embed(title='🎟️ スペシャルチケットランキング',
        description=f'このサーバーの総数：**{total:,}枚**／所持者：{len(rows):,}人', colour=0xF1C40F)
    for index, row in enumerate(rows[page * PAGE_SIZE:(page + 1) * PAGE_SIZE], page * PAGE_SIZE + 1):
        name = (names or {}).get(row['user_id']) or user_name(bot, guild, row['user_id'])
        embed.add_field(name=f'{index}位　{name}',
            value=f'🎟️ **{row["count"]:,}枚**／保有割合 **{percentage(row["count"], total)}**\nID：{row["user_id"]}', inline=False)
    if not rows:
        embed.add_field(name='所持者なし', value='対象のゲームを遊ぶか、/shop で購入できます。')
    embed.set_footer(text=f'{page + 1}/{pages}ページ・割合はサーバー内の全チケットに対する値です。')
    return embed, page, pages


class RankingView(discord.ui.View):
    def __init__(self, bot, guild, rows, owner):
        super().__init__(timeout=180)
        self.bot, self.guild, self.rows, self.owner, self.page = bot, guild, rows, owner, 0
        self.update()

    def update(self):
        pages = max(1, (len(self.rows) + PAGE_SIZE - 1) // PAGE_SIZE)
        self.previous.disabled = self.page == 0
        self.following.disabled = self.page >= pages - 1

    async def interaction_check(self, interaction):
        if interaction.user.id != self.owner:
            await interaction.response.send_message('自分の /ticket leaderboard でページを操作してください。', ephemeral=True)
            return False
        return True

    async def show(self, interaction, step):
        await interaction.response.defer()
        _, self.page, _ = ranking_embed(self.bot, self.guild, self.rows, self.page + step)
        names = await load_names(self.bot, self.guild, self.rows[self.page * PAGE_SIZE:(self.page + 1) * PAGE_SIZE])
        embed, _, _ = ranking_embed(self.bot, self.guild, self.rows, self.page, names)
        self.update()
        await interaction.edit_original_response(embed=embed, view=self)

    @discord.ui.button(label='前へ')
    async def previous(self, interaction, button):
        await self.show(interaction, -1)

    @discord.ui.button(label='次へ')
    async def following(self, interaction, button):
        await self.show(interaction, 1)


def register_tickets(bot, casino):
    bot.ticket_store = store = TicketStore(casino)
    group = app_commands.Group(name='ticket', description='スペシャルチケットの所持数・ランキング', guild_only=True)

    @group.command(name='balance', description='自分のスペシャルチケット所持数と保有割合を表示します')
    async def balance(interaction: discord.Interaction):
        await interaction.response.defer(thinking=True)
        rows = await asyncio.to_thread(store.ranking, interaction.guild_id)
        count = next((r['count'] for r in rows if r['user_id'] == interaction.user.id), 0)
        total = sum(r['count'] for r in rows)
        embed = discord.Embed(title='🎟️ スペシャルチケット',
            description=user_name(bot, interaction.guild, interaction.user.id), colour=0xF1C40F)
        embed.add_field(name='所持数', value=f'{count:,}枚')
        embed.add_field(name='保有割合', value=percentage(count, total))
        embed.set_footer(text=f'このサーバーの総数：{total:,}枚／1枚＝抽選1口')
        await interaction.edit_original_response(embed=embed, allowed_mentions=discord.AllowedMentions.none())

    @group.command(name='leaderboard', description='全所持者の枚数・名前・保有割合をランキング表示します')
    async def leaderboard(interaction: discord.Interaction):
        await interaction.response.defer(thinking=True)
        rows = await asyncio.to_thread(store.ranking, interaction.guild_id)
        names = await load_names(bot, interaction.guild, rows[:PAGE_SIZE])
        embed, _, pages = ranking_embed(bot, interaction.guild, rows, names=names)
        await interaction.edit_original_response(embed=embed,
            view=RankingView(bot, interaction.guild, rows, interaction.user.id) if pages > 1 else None,
            allowed_mentions=discord.AllowedMentions.none())

    bot.tree.add_command(group)

    @bot.tree.command(name='tyuusen', description='スペシャルチケット1枚を1口として当選者を1人抽選します（指定管理者のみ）')
    @app_commands.guild_only()
    async def draw(interaction: discord.Interaction):
        if interaction.user.id != OWNER_ID:
            await interaction.response.send_message('⚠️あなたには必要な権限がありません。', ephemeral=True)
            return
        await interaction.response.defer(thinking=True)
        try:
            result = await asyncio.to_thread(store.draw, interaction.guild_id, str(interaction.id))
        except ValueError as exc:
            await interaction.edit_original_response(content=str(exc))
            return
        names = await load_names(bot, interaction.guild, [result])
        winner = names.get(result['user_id']) or user_name(bot, interaction.guild, result['user_id'])
        embed = discord.Embed(title='🎉 スペシャルチケット抽選結果',
            description=f'当選者：**{winner}**\nユーザーID：{result["user_id"]}', colour=0xF1C40F)
        embed.add_field(name='当選者の応募口数', value=f'{result["count"]:,}口')
        embed.add_field(name='総応募口数', value=f'{result["total"]:,}口')
        embed.add_field(name='当選確率', value=percentage(result['count'], result['total']))
        embed.set_footer(text='1チケット＝1口／抽選でチケットは消費しません。')
        await interaction.edit_original_response(embed=embed, allowed_mentions=discord.AllowedMentions.none())
