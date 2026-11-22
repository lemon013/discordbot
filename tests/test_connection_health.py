import importlib.util
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch


spec = importlib.util.spec_from_file_location('connection_health', Path(__file__).resolve().parents[1] / 'Discord-GameBot' / 'connection_health.py')
health = importlib.util.module_from_spec(spec)
spec.loader.exec_module(health)


class ConnectionHealthTests(unittest.IsolatedAsyncioTestCase):
    async def test_resume_measures_outage_from_first_disconnect_without_restarting_bot(self):
        bot = SimpleNamespace(add_listener=Mock())
        with patch.object(health.time, 'monotonic', return_value=100) as clock, patch.object(health, 'log') as log:
            health.register_connection_health(bot)
            listeners = {name: callback for callback, name in (call.args for call in bot.add_listener.call_args_list)}
            clock.return_value = 110
            await listeners['on_disconnect']()
            clock.return_value = 115
            await listeners['on_disconnect']()
            clock.return_value = 130
            await listeners['on_resumed']()
            self.assertEqual(log.info.call_args.args[1], 'resumed')
            self.assertEqual(log.info.call_args.args[-1], 20)
            clock.return_value = 140
            await listeners['on_ready']()
            self.assertEqual(log.info.call_args.args[-1], 0)

    def test_presence_rotation_stays_within_gateway_limit(self):
        import ast
        source = Path(__file__).resolve().parents[1] / 'Discord-GameBot' / 'bot.py'
        tree = ast.parse(source.read_text(encoding='utf-8'))
        rotate = next(node for node in tree.body if isinstance(node, ast.AsyncFunctionDef) and node.name == 'rotate_status')
        interval = next(keyword.value.value for decorator in rotate.decorator_list for keyword in decorator.keywords if keyword.arg == 'seconds')
        self.assertGreaterEqual(interval, 5)
