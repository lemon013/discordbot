import asyncio
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import AsyncMock, patch
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'Discord-GameBot'))
from audio_cache import AudioCache
from youtube_play import YouTubeAudio


class CacheTests(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.addCleanup(self.folder.cleanup)
        self.source = Path(self.folder.name) / 'input'
        self.output = Path(self.folder.name) / 'output'
        self.source.write_bytes(b'1234')
        self.cache = AudioCache(limit=8, ttl=60)
        self.addCleanup(self.cache.close)

    def test_lru_limit_expiry_and_playback_copy_survives_eviction(self):
        self.cache.put('a', self.source, 'A')
        self.cache.put('b', self.source, 'B')
        self.assertEqual(self.cache.copy('a', self.output), ('A', 4))
        self.cache.put('c', self.source, 'C')
        self.assertIsNone(self.cache.copy('b', self.output))
        self.assertEqual(self.output.read_bytes(), b'1234')
        self.assertLessEqual(sum(e[2] for e in self.cache.entries.values()), 8)
        self.cache.ttl = 0
        self.assertIsNone(self.cache.copy('a', self.output))
        self.assertFalse(self.cache.entries)
        self.assertEqual(self.output.read_bytes(), b'1234')

    def test_low_disk_and_missing_file_are_cache_misses(self):
        with patch('audio_cache.shutil.disk_usage', return_value=type('Space', (), {'free': 0})()):
            self.cache.put('a', self.source, 'A')
        self.assertFalse(self.cache.entries)
        self.cache.put('a', self.source, 'A')
        self.cache.entries['a'][0].unlink()
        self.assertIsNone(self.cache.copy('a', self.output))


class CachedDownloadTests(unittest.IsolatedAsyncioTestCase):
    async def test_two_concurrent_requests_use_one_download_and_cache_hit_keeps_title(self):
        cache = AudioCache()
        self.addCleanup(cache.close)
        with tempfile.TemporaryDirectory() as folder:
            async def worker(url, destination):
                await asyncio.sleep(0.01)
                Path(destination).write_bytes(b'audio')
                return b'{"ok":true,"title":"Song"}'
            run = AsyncMock(side_effect=worker)
            slots = asyncio.Semaphore(1)
            first = YouTubeAudio('https://www.youtube.com/watch?v=BaW_jenozKc', slots)
            second = YouTubeAudio(first.url, slots)
            with patch('youtube_play.AUDIO_CACHE', cache), patch('youtube_play.run_worker', run):
                await asyncio.gather(first.save(Path(folder) / 'a'), second.save(Path(folder) / 'b'))
            self.assertEqual(run.await_count, 1)
            self.assertEqual(second.filename, 'Song.webm')
            self.assertEqual((Path(folder) / 'b').read_bytes(), b'audio')
