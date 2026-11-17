from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from pathlib import Path
import sys
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'Discord-GameBot'))
from casino import CasinoStore, CasinoError, MAX_BALANCE
from leveling import Leveling, LevelStore, DEFAULTS


class LevelJewelTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.store = CasinoStore(Path(self.temp.name) / 'casino.sqlite3')

    def test_single_multiple_and_restart(self):
        self.assertEqual(self.store.reward_levels(1, 2, 4, 5), 200)
        self.assertEqual(self.store.reward_levels(1, 2, 5, 8), 600)
        restarted = CasinoStore(self.store.path)
        self.assertEqual(restarted.reward_levels(1, 2, 5, 8), 0)
        self.assertEqual(restarted.balance(1, 2), 800)
        self.assertEqual(restarted.history(1, 2)[0]['kind'], 'level_reward')

    def test_decrease_reentry_cap_and_guild_isolation(self):
        self.store.reward_levels(1, 2, 29, 30)
        self.store.reward_levels(1, 2, 30, 20)
        self.assertEqual(self.store.reward_levels(1, 2, 20, 30), 0)
        self.assertEqual(self.store.reward_levels(1, 2, 30, 31), 200)
        self.assertEqual(self.store.reward_levels(2, 2, 99, 100), 200)
        self.assertEqual(self.store.reward_levels(2, 2, 100, 100), 0)
        self.assertEqual(self.store.balance(1, 2), 400)

    def test_concurrent_duplicate_is_once(self):
        with ThreadPoolExecutor(max_workers=4) as pool:
            results = list(pool.map(lambda _: self.store.reward_levels(1, 2, 0, 3), range(8)))
        self.assertEqual(sum(results), 600)
        self.assertEqual(self.store.balance(1, 2), 600)

    def test_balance_failure_rolls_back_claim(self):
        self.store.balance(1, 2)
        with closing(self.store.connect()) as db, db:
            db.execute('UPDATE casino_wallets SET balance=?', (MAX_BALANCE,))
        with self.assertRaises(CasinoError):
            self.store.reward_levels(1, 2, 0, 1)
        with closing(self.store.connect()) as db, db:
            db.execute('UPDATE casino_wallets SET balance=0')
        self.assertEqual(self.store.reward_levels(1, 2, 0, 1), 200)

    def test_notifications_disabled_still_rewards(self):
        service = Leveling(SimpleNamespace(), LevelStore(Path(self.temp.name) / 'levels.db'))
        service.jewel_store = self.store

        async def inline(fn, *args):
            return fn(*args)

        async def no_roles(*args):
            pass

        service.reward_roles = no_roles
        with patch('leveling.asyncio.to_thread', inline):
            for _ in range(2):
                task = service.notify(SimpleNamespace(id=1),
                    [dict(user_id=2, before=0, after=2)], {**DEFAULTS, 'notifications': 0})
                with self.assertRaises(StopIteration):
                    task.send(None)
        self.assertEqual(self.store.balance(1, 2), 400)


if __name__ == '__main__':
    unittest.main()
