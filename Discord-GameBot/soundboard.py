"""Persistent buttons for packaged sound clips, using the shared voice player."""
import asyncio
from dataclasses import dataclass
from pathlib import Path
import shutil
from types import SimpleNamespace

import discord
from discord import app_commands

from sound import SoundError


ASSET_DIR = Path(__file__).resolve().parent / 'assets' / 'soundboard'
CLIPS = (
    ('greeting', 'よろしくお願いします'),
    ('good_work', 'お疲れさまでした'),
    ('nice', 'いいね！'),
    ('cooperate', '協力しましょう'),
    ('keep_it_up', '頑張りましょう'),
    ('understood', 'なるほど！'),
    ('how_to', 'どうやるの？！'),
    ('oops', 'しまった！'),
)


@dataclass(frozen=True)
class PackagedClip:
    path: Path
    filename: str
    size: int

    async def save(self, destination):
        await asyncio.to_thread(shutil.copyfile, self.path, destination)


class ClipButton(discord.ui.Button):
    def __init__(self, service, key, label, row):
        super().__init__(label=label, style=discord.ButtonStyle.primary,
                         custom_id=f'soundboard:v1:{key}', row=row)
        self.service = service
        self.path = ASSET_DIR / f'{key}.mp3'

    async def callback(self, interaction: discord.Interaction):
        try:
            clip = PackagedClip(self.path, f'{self.label}.mp3', self.path.stat().st_size)
        except OSError:
            await interaction.response.send_message(
                '音声ファイルが見つかりません。Botの音声ファイルも更新してください。',
                ephemeral=True, allowed_mentions=discord.AllowedMentions.none())
            return
        # The player checks VC membership, permissions and its shared guild lock.
        if getattr(self.service, 'hub', None):
            await self.service.hub.board(interaction, clip)
        else:
            await self.service.invoke(interaction, 'play', clip)


class SoundboardView(discord.ui.View):
    def __init__(self, service):
        super().__init__(timeout=None)
        for index, (key, label) in enumerate(CLIPS):
            self.add_item(ClipButton(service, key, label, index // 3))


def panel_embed():
    return discord.Embed(title='🔊 サウンドボード', colour=discord.Colour.blue(),
        description='このVCに参加して、セリフのボタンを押してください。\n'
                    '音楽・読み上げと重ねて再生できます。\n'
                    '再生終了から60秒でBotが自動退出します。')


def register_soundboard(bot, service):
    # Fixed IDs restore every existing panel after a restart without a database.
    bot.add_view(SoundboardView(service))

    @bot.tree.command(name='soundboard', description='8つのセリフを再生できるサウンドボードを表示します')
    @app_commands.guild_only()
    async def soundboard(interaction: discord.Interaction):
        try:
            service.target(interaction)
        except SoundError as exc:
            await interaction.response.send_message(str(exc), ephemeral=True)
            return
        await interaction.response.send_message(embed=panel_embed(), view=SoundboardView(service),
                                                allowed_mentions=discord.AllowedMentions.none())

    async def on_soundboard_message(message):
        if message.author.bot or message.guild is None or message.content.strip() != './soundboard':
            return
        context = SimpleNamespace(guild=message.guild, channel=message.channel, user=message.author)
        try:
            service.target(context)
        except SoundError as exc:
            await message.reply(str(exc), mention_author=False,
                                allowed_mentions=discord.AllowedMentions.none())
            return
        await message.reply(embed=panel_embed(), view=SoundboardView(service), mention_author=False,
                            allowed_mentions=discord.AllowedMentions.none())

    bot.add_listener(on_soundboard_message, 'on_message')
