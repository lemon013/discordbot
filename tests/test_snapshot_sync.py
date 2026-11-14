import asyncio
import json
import sys
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'Discord-GameBot'))
from snapshot_sync import REGISTRY, SNAPSHOTS, encode_snapshot, decode_snapshot, atomic_write, receive_latest

SECRET = 'test-secret-' * 4


def sample(score=42):
    return {REGISTRY: [{'worldName': 'unlimited', 'scoreboard': 'up', 'gameKey': 'unlimited'}],
            SNAPSHOTS: {'unlimited': {'scoreboards': {'up': {'updatedAt': 1000,
                         'players': {'alice': {'name': 'Alice', 'score': score}}}}}}}


class SnapshotTests(unittest.TestCase):
    def test_roundtrip_and_private_data_excluded(self):
        original = sample()
        original['custom_link:links'] = {'secret': 'must-not-upload'}
        raw = encode_snapshot(original, SECRET, 100)
        self.assertNotIn(b'must-not-upload', raw)
        decoded = decode_snapshot(raw, SECRET)
        self.assertEqual(decoded[SNAPSHOTS], original[SNAPSHOTS])

    def test_bad_signature_and_replay(self):
        raw = encode_snapshot(sample(), SECRET, 100)
        with self.assertRaises(ValueError):
            decode_snapshot(raw, 'wrong-secret')
        with self.assertRaises(ValueError):
            decode_snapshot(raw, SECRET, 100)
        envelope = json.loads(raw)
        envelope['payload'] = envelope['payload'].replace('42', '999')
        with self.assertRaises(ValueError):
            decode_snapshot(json.dumps(envelope).encode(), SECRET)

    def test_invalid_schema_and_future_clock(self):
        bad = sample()
        bad[REGISTRY] = 'invalid'
        with self.assertRaises(ValueError):
            decode_snapshot(encode_snapshot(bad, SECRET), SECRET)
        with self.assertRaises(ValueError):
            decode_snapshot(encode_snapshot(sample(), SECRET, time.time_ns() + 600_000_000_000), SECRET)

    def test_atomic_replace_preserves_original_on_failure(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'data.json'
            atomic_write(path, sample())
            with self.assertRaises(ValueError):
                atomic_write(path, {'invalid': float('nan')})
            self.assertEqual(json.loads(path.read_text()), sample())


class ReceiverTests(unittest.TestCase):
    def test_sender_filter_and_catchup_after_restart(self):
        # The receiver's I/O boundaries are mocked; no sockets or event loop needed.
        from unittest.mock import patch
        async def immediate(function, *args):
            return function(*args)
        with patch('snapshot_sync.asyncio.to_thread', immediate):
            coroutine = self.receive_case()
            try:
                coroutine.send(None)
            except StopIteration:
                pass
            else:
                self.fail('Unexpected external I/O in mocked receiver test')

    async def receive_case(self):
        raw = encode_snapshot(sample(), SECRET)
        reads = []
        class Attachment:
            filename = 'minecraft-status.json'
            size = len(raw)
            async def read(self):
                reads.append(1)
                return raw
        messages = [SimpleNamespace(author=SimpleNamespace(id=9), content='minecraft-status-sync-v1', attachments=[Attachment()]),
                    SimpleNamespace(author=SimpleNamespace(id=7), content='minecraft-status-sync-v1', attachments=[Attachment()])]
        class Channel:
            async def history(self, limit):
                for message in messages:
                    yield message
        bot = SimpleNamespace(get_channel=lambda channel: Channel())
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'data.json'
            await receive_latest(bot, 1, 7, SECRET, path)
            self.assertEqual(len(reads), 1)
            first = path.read_bytes()
            await receive_latest(bot, 1, 7, SECRET, path)
            self.assertEqual(first, path.read_bytes())
            from cloud_status import render
            self.assertIn('42', render(json.loads(first), 'unlimited', 'Alice'))


if __name__ == '__main__':
    unittest.main()
