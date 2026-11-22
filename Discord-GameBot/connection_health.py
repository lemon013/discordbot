"""Log gateway lifecycle without payloads, credentials or user messages."""
import logging
import os
import time


log = logging.getLogger(__name__)


def register_connection_health(bot):
    started = time.monotonic()
    disconnected = None
    pid = os.getpid()
    log.info('Bot process started (pid=%s)', pid)

    async def on_disconnect():
        nonlocal disconnected
        if disconnected is None:
            disconnected = time.monotonic()
        log.warning('Discord gateway disconnected (pid=%s, uptime_s=%.0f); SDK reconnect remains enabled',
                    pid, time.monotonic() - started)

    async def restored(event):
        nonlocal disconnected
        now = time.monotonic()
        downtime = now - disconnected if disconnected is not None else 0
        disconnected = None
        log.info('Discord gateway %s (pid=%s, uptime_s=%.0f, disconnected_s=%.1f)',
                 event, pid, now - started, downtime)

    async def on_ready():
        await restored('ready')

    async def on_resumed():
        await restored('resumed')

    bot.add_listener(on_disconnect, 'on_disconnect')
    bot.add_listener(on_ready, 'on_ready')
    bot.add_listener(on_resumed, 'on_resumed')
