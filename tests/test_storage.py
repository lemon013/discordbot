from contextlib import closing
from pathlib import Path
import os
import sqlite3
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'Discord-GameBot'))
import storage
from leveling import LevelStore
from casino import CasinoStore


class StorageTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.source = self.root / 'checkout'
        self.source.mkdir()
        self.data = self.root / 'persistent'
        env = patch.dict(os.environ, {'BOT_DATA_DIR': str(self.data)}, clear=True)
        env.start()
        self.addCleanup(env.stop)
        src = patch.object(storage, 'SOURCE', self.source)
        src.start()
        self.addCleanup(src.stop)

    def test_migrate_both_databases_preserves_xp_wallet_and_history(self):
        levels = LevelStore(self.source / 'levels.sqlite3')
        levels.edit(1, 2, 'xp', 'add', 1234)
        casino = CasinoStore(self.source / 'casino.sqlite3')
        casino.edit(1, 2, 'test', 'add', 456, 99)
        migrated_levels, migrated_casino = LevelStore(), CasinoStore()
        self.assertEqual(migrated_levels.rank(1, 2)[0]['xp'], 1234)
        self.assertEqual(migrated_casino.balance(1, 2), 456)
        self.assertEqual(len(migrated_casino.history(1, 2)), 1)
        self.assertTrue((self.source / 'levels.sqlite3').exists())
        self.assertEqual(migrated_levels.path, self.data / 'levels.sqlite3')

    def test_checkout_replaced_and_cwd_changed_preserves_data(self):
        levels = LevelStore()
        levels.edit(1, 2, 'level', 'set', 10)
        CasinoStore().edit(1, 2, 'seed', 'add', 987, 99)
        replacement = self.root / 'new-checkout'
        replacement.mkdir()
        with patch.object(storage, 'SOURCE', replacement), patch('pathlib.Path.cwd', return_value=replacement):
            self.assertEqual(LevelStore().rank(1, 2)[0]['xp'], levels.rank(1, 2)[0]['xp'])
            self.assertEqual(CasinoStore().balance(1, 2), 987)

    def test_explicit_path_and_casino_sibling(self):
        with patch.dict(os.environ, {'LEVEL_DB_PATH': str(self.root / 'volume' / 'custom.db')}):
            self.assertEqual(storage.database_path('level'), self.root / 'volume' / 'custom.db')
            self.assertEqual(storage.database_path('casino'), self.root / 'volume' / 'casino.sqlite3')

    def test_missing_initialized_database_does_not_silently_reset(self):
        store = LevelStore()
        store.path.unlink()
        with self.assertRaisesRegex(RuntimeError, 'refusing to reset'):
            LevelStore()

    def test_existing_destination_never_overwritten_and_backup_restores(self):
        LevelStore(self.source / 'levels.sqlite3').edit(1, 2, 'xp', 'set', 999)
        target = LevelStore()
        target.edit(1, 2, 'xp', 'set', 123)
        self.assertEqual(LevelStore().rank(1, 2)[0]['xp'], 123)
        storage.backup_databases([target.path])
        backup = next((self.data / 'backups').glob('levels-*.sqlite3'))
        with closing(sqlite3.connect(backup)) as db:
            self.assertEqual(db.execute('SELECT xp FROM level_users').fetchone()[0], 123)


if __name__ == '__main__':
    unittest.main()
