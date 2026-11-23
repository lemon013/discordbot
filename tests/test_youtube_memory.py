import asyncio
from pathlib import Path
import sys
from types import SimpleNamespace as NS
import unittest
from unittest.mock import AsyncMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'Discord-GameBot'))
import youtube_play as yt
from music_queue import Music


class MemoryTests(unittest.IsolatedAsyncioTestCase):
    async def test_search_and_download_share_one_worker_slot(self):
        entered = asyncio.Event()
        release = asyncio.Event()
        calls = []
        async def work(url, destination, **kwargs):
            calls.append(url)
            entered.set()
            await release.wait()
            return b'ok'
        with patch.object(yt, '_run_worker', side_effect=work):
            first = asyncio.create_task(yt.run_worker('search', '-', search=True))
            await entered.wait()
            second = asyncio.create_task(yt.run_worker('download', 'audio'))
            await asyncio.sleep(0)
            self.assertEqual(calls, ['search'])
            second.cancel()
            await asyncio.gather(second, return_exceptions=True)
            release.set()
            self.assertEqual(await first, b'ok')
            self.assertEqual(calls, ['search'])
        music = Music(NS(), NS())
        self.assertIs(music.slots, music.search_slots)

    async def test_high_baseline_refuses_to_spawn(self):
        with patch.object(yt, 'memory_mib', return_value=310), patch.object(yt.asyncio, 'create_subprocess_exec', new=AsyncMock()) as spawn:
            with self.assertRaises(yt.SoundError):
                await yt.run_worker('url', 'path')
            spawn.assert_not_awaited()

    async def test_memory_spike_kills_worker_and_reports_safe_failure(self):
        killed = asyncio.Event()
        async def communicate():
            await killed.wait()
            return b'partial provider details', None
        async def kill(process):
            killed.set()
        process = NS(communicate=AsyncMock(side_effect=communicate))
        real_sleep = asyncio.sleep
        async def fast_sleep(delay):
            await real_sleep(0)
        with patch.object(yt, 'memory_mib', side_effect=[85, 305]), \
             patch.object(yt.asyncio, 'create_subprocess_exec', new=AsyncMock(return_value=process)), \
             patch.object(yt, 'kill_download', side_effect=kill) as terminate, \
             patch.object(yt.asyncio, 'sleep', side_effect=fast_sleep):
            with self.assertRaises(yt.SoundError) as caught:
                await yt.run_worker('url', 'path')
            self.assertIn('メモリ', str(caught.exception))
            self.assertNotIn('provider details', str(caught.exception))
            terminate.assert_awaited_once_with(process)

    async def test_unknown_memory_still_uses_deno_heap_budget_and_cleans_monitor(self):
        process = NS(communicate=AsyncMock(return_value=(b'ok', None)))
        with patch.object(yt, 'memory_mib', return_value=None), patch.object(yt.asyncio, 'create_subprocess_exec', new=AsyncMock(return_value=process)) as spawn:
            self.assertEqual(await yt.run_worker('url', 'path'), b'ok')
        self.assertEqual(spawn.call_args.kwargs['env']['DENO_V8_FLAGS'], '--max-old-space-size=64,--max-semi-space-size=4')
        self.assertFalse(any(task.get_name() == 'youtube-memory-guard' and not task.done() for task in asyncio.all_tasks()))
