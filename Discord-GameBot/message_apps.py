"""Message context menus: private reports and a playful random aptitude score."""
import logging
import random

import discord
from discord import app_commands

REPORT_RECIPIENT_ID = 799820074990698496
log = logging.getLogger(__name__)


def clip(text, limit):
    """Discord limits count UTF-16 units, including astral characters twice."""
    encoded = text.encode('utf-16-le')
    if len(encoded) <= limit * 2:
        return text
    return encoded[:(limit - 1) * 2].decode('utf-16-le', errors='ignore') + '…'


class ReportModal(discord.ui.Modal, title='メッセージ通報'):
    reason = discord.ui.TextInput(
        label='通報内容', style=discord.TextStyle.paragraph,
        placeholder='問題だと思った点や状況を入力してください。',
        required=True, min_length=1, max_length=1500)

    def __init__(self, bot, message, reporter_id):
        super().__init__(timeout=300)
        self.bot = bot
        self.message = message
        self.reporter_id = reporter_id
        self.submitting = False

    async def on_submit(self, interaction):
        await interaction.response.defer(thinking=True, ephemeral=True)
        if interaction.user.id != self.reporter_id:
            await interaction.edit_original_response(content='フォームを開いた本人だけが送信できます。')
            return
        reason = self.reason.value.strip()
        if not reason:
            await interaction.edit_original_response(content='通報内容を入力してください。')
            return
        if self.submitting:
            await interaction.edit_original_response(content='この通報は送信中、または送信済みです。')
            return
        self.submitting = True
        embed = discord.Embed(title='メッセージ通報',
                              description=clip(discord.utils.escape_markdown(reason), 4000),
                              color=discord.Color.orange(),
                              timestamp=discord.utils.utcnow())
        embed.add_field(name='メッセージリンク', value=self.message.jump_url, inline=False)
        reporter = discord.utils.escape_markdown(str(interaction.user))
        embed.add_field(name='通報者', value=clip(f'{reporter}\nID: {interaction.user.id}', 1024), inline=False)
        try:
            recipient = self.bot.get_user(REPORT_RECIPIENT_ID)
            if recipient is None:
                recipient = await self.bot.fetch_user(REPORT_RECIPIENT_ID)
            await recipient.send(embed=embed, allowed_mentions=discord.AllowedMentions.none())
        except discord.HTTPException:
            self.submitting = False
            log.warning('Could not deliver message report by DM (message_id=%s)', self.message.id)
            await interaction.edit_original_response(
                content='通報先へのDM送信に失敗しました。DMの受信設定やBotの接続状況を確認し、もう一度お試しください。')
            return
        self.stop()
        await interaction.edit_original_response(content='通報を送信しました。内容は通報先のDMだけに送っています。')


def register_message_apps(bot):
    @app_commands.guild_only()
    async def report_message(interaction: discord.Interaction, message: discord.Message):
        if interaction.guild is None or message.guild is None or message.guild.id != interaction.guild.id:
            await interaction.response.send_message('この操作はサーバー内のメッセージで使用してください。', ephemeral=True)
            return
        await interaction.response.send_modal(ReportModal(bot, message, interaction.user.id))

    @app_commands.guild_only()
    async def kosen_aptitude(interaction: discord.Interaction, message: discord.Message):
        await interaction.response.defer(thinking=True, ephemeral=True)
        if interaction.guild is None or message.guild is None or message.guild.id != interaction.guild.id:
            await interaction.edit_original_response(content='この操作はサーバー内のメッセージで使用してください。')
            return
        score = random.randint(0, 100)
        text = (f'高専適正度：**{score}%**\n'
                '※ランダムなお遊び診断です。高専への適性を必ずお約束するものではありません。')
        try:
            await message.reply(text, mention_author=False, fail_if_not_exists=True,
                                allowed_mentions=discord.AllowedMentions.none())
        except discord.HTTPException:
            await interaction.edit_original_response(
                content='返信できませんでした。対象メッセージの削除や、Botの送信・履歴閲覧権限を確認してください。')
            return
        await interaction.edit_original_response(content='対象メッセージに診断結果を返信しました。')

    bot.tree.add_command(app_commands.ContextMenu(name='メッセージ通報', callback=report_message))
    bot.tree.add_command(app_commands.ContextMenu(name='高専適正度', callback=kosen_aptitude))
