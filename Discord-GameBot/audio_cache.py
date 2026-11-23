"""Small process-local disk cache; never retain decoded audio in Python memory."""
import asyncio
from collections import OrderedDict
from pathlib import Path
import shutil
import tempfile
import threading
import time


class AudioCache:
    def __init__(self, limit=50 * 1024 * 1024, ttl=3600):
        self.limit, self.ttl = limit, ttl
        self.directory = None
        self.entries = OrderedDict()
        self.lock = threading.Lock()
        self.counter = 0

    def _remove(self, key):
        path, _, _, _ = self.entries.pop(key)
        path.unlink(missing_ok=True)

    def _expire(self):
        now = time.monotonic()
        for key, (_, _, _, saved) in list(self.entries.items()):
            if now - saved >= self.ttl:
                self._remove(key)

    def copy(self, key, destination):
        with self.lock:
            try:
                self._expire()
                entry = self.entries.get(key)
                if entry is None:
                    return None
                path, title, size, saved = entry
                if not path.is_file() or path.stat().st_size != size:
                    self._remove(key)
                    return None
                shutil.copyfile(path, destination)
                self.entries.move_to_end(key)
                return title, size
            except OSError:
                return None

    def put(self, key, source, title):
        with self.lock:
            try:
                self._expire()
                size = Path(source).stat().st_size
                if not 0 < size <= self.limit:
                    return
                if self.directory is None:
                    # Use the source filesystem, not /tmp (which may be RAM-backed).
                    self.directory = tempfile.TemporaryDirectory(
                        prefix='discord-music-cache-', dir=Path(__file__).resolve().parent)
                root = Path(self.directory.name)
                # Leave space for playback copies, downloads and the bot's databases.
                if shutil.disk_usage(root).free < size + 64 * 1024 * 1024:
                    return
                if key in self.entries:
                    self._remove(key)
                while self.entries and sum(e[2] for e in self.entries.values()) + size > self.limit:
                    self._remove(next(iter(self.entries)))
                self.counter += 1
                path = root / f'{self.counter}.audio'
                try:
                    shutil.copyfile(source, path)
                except OSError:
                    path.unlink(missing_ok=True)
                    return
                self.entries[key] = path, title, size, time.monotonic()
            except OSError:
                return  # Caching is optional; a valid download remains playable.

    def close(self):
        with self.lock:
            self.entries.clear()
            if self.directory is not None:
                self.directory.cleanup()
                self.directory = None


async def disk_call(function, *args):
    # Finish file I/O before cancellation allows the playback directory to disappear.
    task = asyncio.create_task(asyncio.to_thread(function, *args))
    try:
        return await asyncio.shield(task)
    except asyncio.CancelledError:
        await asyncio.gather(task, return_exceptions=True)
        raise


AUDIO_CACHE = AudioCache()
