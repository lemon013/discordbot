"""Keep presence rotation alive through gateway disconnects without update bursts."""
import asyncio
import logging
import time

import aiohttp
import discord

log = logging.getLogger(__name__)


class PresenceRotation:
    def __init__(self, bot, messages):
        self.bot, self.messages = bot, tuple(messages)
        self.index = 0
        self.last_attempt = None
        self.waiting = False

    def connected(self):
        socket = getattr(getattr(self.bot, 'ws', None), 'socket', None)
        return (not self.bot.is_closed() and self.bot.is_ready()
                and socket is not None and not socket.closed)

    async def update(self):
        if not self.connected():
            return
        if self.last_attempt is not None:
            delay = 5 - (time.monotonic() - self.last_attempt)
            if delay > 0:
                await asyncio.sleep(delay)
        # The socket can change or close during the spacing wait.
        if not self.connected():
            return
        self.last_attempt = time.monotonic()
        try:
            await asyncio.wait_for(self.bot.change_presence(
                activity=discord.CustomActivity(name=self.messages[self.index % len(self.messages)])), timeout=10)
        except (aiohttp.ClientError, discord.ConnectionClosed, OSError, asyncio.TimeoutError) as exc:
            if not self.waiting:
                log.warning('Status update deferred during gateway connection trouble (error_type=%s)', type(exc).__name__)
            self.waiting = True
            return
        if self.waiting:
            log.info('Status updates resumed after gateway recovery')
        self.waiting = False
        self.index += 1
