import asyncio
from pathlib import Path
import signal
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'Discord-GameBot'))
import runtime_probe as mod


class ProbeTests(unittest.IsolatedAsyncioTestCase):
    def test_thread_children_walk_skips_unrelated_host_processes(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for pid, parent, children in [(2, 1, ''), (10, 2, '11'), (11, 10, '')]:
                path = root / str(pid)
                path.mkdir()
                (path / 'status').write_text(f'PPid:\t{parent}\nVmRSS:\t1024 kB\n')
                task = path / 'task' / str(pid)
                task.mkdir(parents=True)
                (task / 'children').write_text(children)
            # Subprocesses spawned by a non-main thread must also be counted.
            task = root / '2' / 'task' / '3'
            task.mkdir()
            (task / 'children').write_text('10')
            original = Path.iterdir
            def checked(path):
                self.assertNotEqual(path, root, 'must not scan all host processes')
                return original(path)
            with patch.object(Path, 'iterdir', checked):
                self.assertEqual(mod.process_memory(root, 2), (1, 3, 2))

    def test_proportional_memory_avoids_shared_page_double_count_and_falls_back_per_process(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for pid, parent, rss, pss in [(2, 1, 100000, 50000), (10, 2, 200000, 150000)]:
                path = root / str(pid)
                path.mkdir()
                (path / 'status').write_text(f'PPid:\t{parent}\nVmRSS:\t{rss} kB\n')
                (path / 'smaps_rollup').write_text(f'Pss:\t{pss} kB\n')
            self.assertEqual(mod.process_memory(root, 2)[1], 300000 / 1024)
            self.assertEqual(mod.process_memory(root, 2, proportional=True), (50000 / 1024, 200000 / 1024, 1))
            (root / '10' / 'smaps_rollup').unlink()
            self.assertEqual(mod.process_memory(root, 2, proportional=True)[1], 250000 / 1024)

    def test_memory_counts_recursive_children_but_not_other_processes(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for pid, parent, rss in [(2, 1, 60000), (10, 2, 30000), (11, 10, 50000), (99, 1, 100000)]:
                path = root / str(pid)
                path.mkdir()
                (path / 'status').write_text(f'PPid:\t{parent}\nVmRSS:\t{rss} kB\n')
            own, total, children = mod.process_memory(root, 2)
            self.assertEqual(own, 60000 / 1024)
            self.assertEqual(total, 140000 / 1024)
            self.assertEqual(children, 2)
            self.assertEqual(mod.process_memory(root / 'absent'), (None, None, None))

    def test_delayed_loop_logs_identifiers_without_local_secrets(self):
        secret = 'do-not-log-this-value'
        probe = mod.RuntimeProbe()
        with patch.object(mod.time, 'monotonic', return_value=probe.started + 30), patch.object(mod, 'log') as log, patch.object(mod, 'process_memory', return_value=(60, 120, 2)):
            probe.tick = probe.started + 10
            probe.sample()
            self.assertEqual(log.warning.call_args.args[2], 19)
            self.assertNotIn(secret, str(log.mock_calls))
            self.assertIn('main_stack', log.warning.call_args.args[0])

    async def test_repeated_ready_keeps_one_heartbeat_and_cancellation_clears_tick(self):
        probe = mod.RuntimeProbe()
        probe.bind()
        task = probe.beat_task
        probe.bind()
        self.assertIs(task, probe.beat_task)
        await asyncio.sleep(0)
        self.assertIsNotNone(probe.tick)
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        self.assertIsNone(probe.tick)

    def test_termination_preserves_default_signal_exit_and_existing_handler(self):
        probe = mod.RuntimeProbe()
        with patch.object(mod.os, 'write'), patch.object(mod.signal, 'signal') as reset, patch.object(mod.os, 'kill') as kill:
            probe.signal_handler(signal.SIGTERM, None, signal.SIG_DFL)
            reset.assert_called_once_with(signal.SIGTERM, signal.SIG_DFL)
            kill.assert_called_once_with(mod.os.getpid(), signal.SIGTERM)
            previous = Mock()
            probe.signal_handler(signal.SIGTERM, None, previous)
            previous.assert_called_once_with(signal.SIGTERM, None)
