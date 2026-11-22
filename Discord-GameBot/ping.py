"""Respond to ./ping without changing existing command prefixes."""
import logging
import math
from time import perf_counter

import discord

log = logging.getLogger(__name__)


def register_ping(bot):
    async def on_ping_message(message):
        if (message.author.bot or getattr(message, 'webhook_id', None)
                or message.guild is None or message.content.strip() != './ping'):
            return
        latency = bot.latency
        gateway = f'{latency * 1000:.0f} ms' if math.isfinite(latency) and latency >= 0 else '計測待ち'
        content = f'🏓 Pong!\nDiscord接続：**{gateway}**\nメッセージ送信：計測中…'
        try:
            started = perf_counter()
            reply = await message.channel.send(content, allowed_mentions=discord.AllowedMentions.none())
            elapsed = (perf_counter() - started) * 1000
            await reply.edit(content=f'🏓 Pong!\nDiscord接続：**{gateway}**\nメッセージ送信：**{elapsed:.0f} ms**',
                             allowed_mentions=discord.AllowedMentions.none())
        except discord.HTTPException as exc:
            log.warning('Ping response failed (channel_id=%s, error_type=%s)', message.channel.id, type(exc).__name__)

    bot.add_listener(on_ping_message, 'on_message')
