import asyncio
from pathlib import Path
import sys
from types import SimpleNamespace as NS
import unittest
from unittest.mock import AsyncMock, patch

import aiohttp

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'Discord-GameBot'))
from presence_rotation import PresenceRotation


class PresenceTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.socket = NS(closed=False)
        self.bot = NS(ws=NS(socket=self.socket), is_ready=lambda: True,
                      is_closed=lambda: False, change_presence=AsyncMock())
        self.rotation = PresenceRotation(self.bot, ['first', 'second'])

    async def test_disconnected_or_closing_gateway_is_skipped(self):
        for ws in (None, NS(socket=NS(closed=True))):
            self.bot.ws = ws
            await self.rotation.update()
        self.bot.ws = NS(socket=self.socket)
        self.bot.is_ready = lambda: False
        await self.rotation.update()
        self.bot.change_presence.assert_not_awaited()
        self.assertEqual(self.rotation.index, 0)

    async def test_closing_transport_race_is_retried_after_recovery_without_skipping_text(self):
        self.bot.change_presence.side_effect = [aiohttp.ClientConnectionResetError('Cannot write to closing transport'), None, None]
        with patch('presence_rotation.time.monotonic', return_value=100) as clock, patch('presence_rotation.log') as log:
            await self.rotation.update()
            self.assertTrue(self.rotation.waiting)
            self.assertEqual(self.rotation.index, 0)
            clock.return_value = 105
            self.bot.ws = NS(socket=NS(closed=False))
            await self.rotation.update()
            clock.return_value = 110
            await self.rotation.update()
            self.assertEqual([call.kwargs['activity'].name for call in self.bot.change_presence.await_args_list], ['first', 'first', 'second'])
            self.assertEqual(log.warning.call_count, 1)
            self.assertFalse(self.rotation.waiting)

    async def test_catchup_waits_five_seconds_and_rechecks_socket_before_send(self):
        self.rotation.last_attempt = 100
        async def disconnect(delay):
            self.assertEqual(delay, 4)
            self.socket.closed = True
        with patch('presence_rotation.time.monotonic', return_value=101), patch('presence_rotation.asyncio.sleep', side_effect=disconnect):
            await self.rotation.update()
        self.bot.change_presence.assert_not_awaited()
        self.assertEqual(self.rotation.last_attempt, 100)

    async def test_shutdown_cancellation_and_programming_errors_are_not_swallowed(self):
        self.bot.change_presence.side_effect = asyncio.CancelledError()
        with self.assertRaises(asyncio.CancelledError):
            await self.rotation.update()
        self.rotation.last_attempt = None
        self.bot.change_presence.side_effect = ValueError('unexpected')
        with self.assertRaises(ValueError):
            await self.rotation.update()
