from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'Discord-GameBot'))
from voice_diagnostics import number, snapshot, describe


class DiagnosticsTests(unittest.TestCase):
    def test_numeric_limits_unlimited_missing_and_malformed(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'limit'
            for value, expected in [('419430400', 419430400), ('max', None),
                                    ('9223372036854771712', None), ('-1', None), ('bad', None)]:
                path.write_text(value, encoding='ascii')
                self.assertEqual(number(path), expected)
            self.assertIsNone(number(Path(folder) / 'missing'))

    def test_cgroup_limit_usage_and_process_rss_are_separate_and_no_env_values_are_printed(self):
        readings = {'/sys/fs/cgroup/memory.max': 400 * 1048576,
                    '/sys/fs/cgroup/memory.current': 160 * 1048576}
        with patch('voice_diagnostics.number', side_effect=lambda path: readings.get(path)), \
             patch('voice_diagnostics.Path.read_text', return_value='Name: python\nVmRSS:\t102400 kB\n'), \
             patch.dict('os.environ', {'VOICEVOX_URL': 'https://private:secret@engine.invalid'}):
            data = snapshot('.')
        self.assertEqual(data['rss'], 100 * 1048576)
        self.assertEqual(data['limit'], 400 * 1048576)
        result = describe(data, True)
        self.assertIn('160.0 MiB', result)
        self.assertIn('100.0 MiB', result)
        self.assertIn('設定済み', result)
        self.assertNotIn('secret', result)
        self.assertIn('保存容量枠の残量を表しません', result)

    def test_v1_fallback_does_not_claim_unknown_capacity_is_available(self):
        readings = {'/sys/fs/cgroup/memory/memory.limit_in_bytes': 400 * 1048576,
                    '/sys/fs/cgroup/memory/memory.usage_in_bytes': 100 * 1048576}
        with patch('voice_diagnostics.number', side_effect=lambda path: readings.get(path)):
            data = snapshot('.')
        self.assertEqual(data['limit'], 400 * 1048576)
        self.assertEqual(data['used'], 100 * 1048576)
        data['limit'] = None
        self.assertIn('上限：取得できませんでした', describe(data))
