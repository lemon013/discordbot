"""Report gateway and interaction response latency with /ping."""
import logging
import math
from time import perf_counter

import discord
from discord import app_commands

log = logging.getLogger(__name__)


def register_ping(bot):
    @bot.tree.command(name='ping', description='BotとDiscordの接続・メッセージ送信の遅延を確認します')
    @app_commands.guild_only()
    async def ping(interaction: discord.Interaction):
        latency = bot.latency
        gateway = f'{latency * 1000:.0f} ms' if math.isfinite(latency) and latency >= 0 else '計測待ち'
        content = f'🏓 Pong!\nDiscord接続：**{gateway}**\nメッセージ送信：計測中…'
        try:
            started = perf_counter()
            await interaction.response.send_message(content, allowed_mentions=discord.AllowedMentions.none())
            elapsed = (perf_counter() - started) * 1000
            await interaction.edit_original_response(
                content=f'🏓 Pong!\nDiscord接続：**{gateway}**\nメッセージ送信：**{elapsed:.0f} ms**',
                allowed_mentions=discord.AllowedMentions.none())
        except discord.HTTPException as exc:
            log.warning('Ping response failed (channel_id=%s, error_type=%s)', interaction.channel_id, type(exc).__name__)
