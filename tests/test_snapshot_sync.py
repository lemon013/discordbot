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
    def test_generic_world_murder_scores_include_zero_and_shared_wins(self):
        from cloud_status import classify, find_status, render
        registry = [{'worldName': 'World', 'scoreboard': board, 'gameKey': None}
                    for board in ('wingame', 'Murder', 'Crew', 'Other')]
        registry += [{'worldName': 'Unlimited Test', 'scoreboard': 'wingame', 'gameKey': 'unlimited'},
                     {'worldName': 'Another World', 'scoreboard': 'wingame', 'gameKey': None}]
        data = {REGISTRY: registry, SNAPSHOTS: {'World': {'scoreboards': {
            board: {'updatedAt': 1000, 'players': {'alice': {'name': 'Alice', 'score': 0}}}
            for board in ('wingame', 'Murder', 'Crew', 'Other')}}}}
        decoded = decode_snapshot(encode_snapshot(data, SECRET), SECRET)
        self.assertEqual(find_status(decoded, 'Alice', 'MurderMysteryRe:MASTER')[0],
                         {'wingame': 0, 'Murder': 0, 'Crew': 0, 'Other': 0})
        self.assertIn('**第三陣営になった数**: 0', render(decoded, 'MurderMysteryRe:MASTER', 'Alice'))
        self.assertEqual(classify(registry[-2], registry), 'unlimited')
        self.assertIsNone(classify(registry[-1], registry))
        explicit = {'worldName': 'World', 'scoreboard': 'wingame', 'gameKey': 'unlimited'}
        self.assertEqual(classify(explicit, registry), 'unlimited')
        self.assertIsNone(classify({'worldName': 'World', 'scoreboard': 'Other'},
                                   [{'worldName': 'World', 'scoreboard': 'Other'}]))

    def test_unlimited_scores_render_after_sync(self):
        from cloud_status import find_status, render
        world = 'Unlimited-test-world'
        expected = {'up': 1234, 'wingame': 12, 'killCount': 34, 'deathCount': 0}
        data = {REGISTRY: [{'worldName': world, 'scoreboard': board, 'gameKey': 'unlimited'}
                           for board in expected],
                SNAPSHOTS: {world: {'scoreboards': {
                    board: {'updatedAt': 1794659238496, 'players': {
                        'alice': {'name': 'Alice', 'score': score}}}
                    for board, score in expected.items()}}}}
        decoded = decode_snapshot(encode_snapshot(data, SECRET), SECRET)
        self.assertEqual(find_status(decoded, 'Alice', 'unlimited')[0], expected)
        self.assertIn('1234', render(decoded, 'unlimited', 'Alice'))

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
    def run_mocked(self, coroutine):
        from unittest.mock import patch
        async def immediate(function, *args):
            return function(*args)
        with patch('snapshot_sync.asyncio.to_thread', immediate):
            try:
                coroutine.send(None)
            except StopIteration as completed:
                return completed.value
            self.fail('Unexpected external I/O in mocked receiver test')

    def test_signed_attachment_without_message_content(self):
        result, saved = self.run_mocked(self.diagnostic_case(content=''))
        self.assertEqual(result, 'synchronized')
        self.assertEqual(saved[SNAPSHOTS], sample()[SNAPSHOTS])

    def test_wrong_sender_is_reported_without_saving(self):
        result, saved = self.run_mocked(self.diagnostic_case(sender=9))
        self.assertIn('sender ID 9 differs from MC_SYNC_SENDER_ID=7', result)
        self.assertIsNone(saved)

    def test_wrong_secret_is_reported_without_saving(self):
        result, saved = self.run_mocked(self.diagnostic_case(secret='different-secret'))
        self.assertIn('MC_SYNC_SECRET is identical', result)
        self.assertIsNone(saved)

    def test_rejected_new_snapshot_not_hidden_by_older_saved_snapshot(self):
        result, saved = self.run_mocked(self.diagnostic_case(secret='different-secret', older=True))
        self.assertIn('MC_SYNC_SECRET is identical', result)
        self.assertEqual(saved['cloud_sync:sent_at'], 100)

    async def diagnostic_case(self, content='minecraft-status-sync-v1', sender=7, secret=SECRET, older=False):
        raw = encode_snapshot(sample(), SECRET)
        class Attachment:
            filename = 'minecraft-status.json'
            size = len(raw)
            async def read(self):
                return raw
        messages = [SimpleNamespace(author=SimpleNamespace(id=sender), content=content, attachments=[Attachment()])]
        if older:
            class OlderAttachment(Attachment):
                async def read(self):
                    return encode_snapshot(sample(), secret, 100)
            messages.append(SimpleNamespace(author=SimpleNamespace(id=7), content=content, attachments=[OlderAttachment()]))
        class Channel:
            async def history(self, limit):
                for message in messages:
                    yield message
        bot = SimpleNamespace(get_channel=lambda channel: Channel())
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'data.json'
            if older:
                atomic_write(path, decode_snapshot(encode_snapshot(sample(), secret, 100), secret))
            result = await receive_latest(bot, 1, 7, secret, path)
            return result, json.loads(path.read_text(encoding='utf-8')) if path.exists() else None

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
