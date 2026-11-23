from contextlib import closing
import io
import json
import os
from pathlib import Path
import sqlite3
import sys
import tempfile
import unittest
from unittest.mock import patch
import zipfile

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'Discord-GameBot'))
from leveling import LevelStore
from casino import CasinoStore
import player_backup as backup


def run_inline(coro):
    try:
        coro.send(None)
    except StopIteration as result:
        return result.value
    raise AssertionError('Unexpected asynchronous IO in offline test')


class BackupTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        env = patch.dict(os.environ, {'BOT_DATA_DIR': str(self.root)}, clear=True)
        env.start()
        self.addCleanup(env.stop)
        self.levels, self.casino = LevelStore(), CasinoStore()
        self.levels.edit(1, 2, 'xp', 'set', 1234)
        self.casino.edit(1, 2, 'seed', 'add', 456, 99)
        with closing(self.casino.connect()) as db, db:
            db.execute('CREATE TABLE shop_owned (item TEXT)')
            db.execute("INSERT INTO shop_owned VALUES ('record')")
        self.raw = backup.pack()

    def test_file_backup_avoids_loading_databases_and_preserves_full_archive(self):
        destination = self.root / 'streamed.zip'
        with patch.object(Path, 'read_bytes', side_effect=AssertionError('whole file read')):
            backup.pack_to(destination)
        self.assertEqual(backup.validate(destination.read_bytes()), backup.validate(self.raw))

    def test_path_upload_reopens_stream_for_retry_and_closes_handles(self):
        destination = self.root / 'upload.zip'
        backup.pack_to(destination)
        transport = backup.Transport('fake', bot_id='10', message_id='20')
        streams = []
        async def request(session, method, route, **kwargs):
            for _ in range(2):
                form = kwargs['data_factory']()
                streams.append(form._fields[-1][2])
                self.assertEqual(streams[-1].read(2), b'PK')
            self.assertIsNot(streams[0], streams[1])
            return dict(id='20', timestamp='old', edited_timestamp='new')
        transport.request = request
        run_inline(transport.upload(None, destination))
        self.assertTrue(all(stream.closed for stream in streams))

    def test_restore_pair_after_entire_local_storage_removed(self):
        for path in list(self.root.iterdir()):
            path.unlink()
        backup.install(self.raw, 'remote1')
        self.assertEqual(LevelStore().rank(1, 2)[0]['xp'], 1234)
        self.assertEqual(CasinoStore().balance(1, 2), 456)
        with closing(self.casino.connect()) as db:
            self.assertEqual(db.execute('SELECT item FROM shop_owned').fetchone()[0], 'record')

    def test_normal_restart_preserves_changes_since_last_upload(self):
        backup.save_revision('remote1')
        self.casino.edit(1, 2, 'new', 'add', 10, 99)
        backup.install(self.raw, 'remote1')
        self.assertEqual(self.casino.balance(1, 2), 466)

    def test_remote_restore_repairs_missing_db_with_initialization_marker(self):
        backup.save_revision('remote1')
        self.levels.path.unlink()
        with patch.dict(os.environ, {'PLAYER_BACKUP_ACTIVE': '1', 'CLOUD_MODE': '1'}):
            backup.install(self.raw, 'remote1')
            self.assertEqual(LevelStore().rank(1, 2)[0]['xp'], 1234)

    def test_newer_remote_replaces_old_local(self):
        backup.save_revision('old')
        self.casino.edit(1, 2, 'new', 'add', 10, 99)
        backup.install(self.raw, 'new')
        self.assertEqual(self.casino.balance(1, 2), 456)

    def test_tampered_backup_does_not_modify_either_database(self):
        original = zipfile.ZipFile(io.BytesIO(self.raw))
        out = io.BytesIO()
        with zipfile.ZipFile(out, 'w') as archive:
            for name in original.namelist():
                archive.writestr(name, b'corrupt' if name == 'casino.sqlite3' else original.read(name))
        with self.assertRaisesRegex(RuntimeError, 'checksum'):
            backup.install(out.getvalue(), 'bad')
        self.assertEqual(self.levels.rank(1, 2)[0]['xp'], 1234)
        self.assertEqual(self.casino.balance(1, 2), 456)

    def test_invalid_archive_members_rejected(self):
        out = io.BytesIO()
        with zipfile.ZipFile(out, 'w') as archive:
            archive.writestr('../escape', 'bad')
        with self.assertRaises(RuntimeError):
            backup.validate(out.getvalue())

    def test_own_backup_only_and_paginates_shared_channel(self):
        transport = backup.Transport('fake', bot_id='10')
        own = dict(id='2', author={'id': '10'}, content=backup.PREFIX,
                   attachments=[{'filename': backup.FILENAME}])
        unrelated = [dict(id=str(200-i), author={'id': '99'}, content=backup.PREFIX,
                          attachments=[{'filename': backup.FILENAME}]) for i in range(100)]
        routes = []
        async def request(session, method, route):
            routes.append(route)
            return unrelated if len(routes) == 1 else [own]
        transport.request = request
        self.assertEqual(run_inline(transport.latest(None))[0]['id'], '2')
        self.assertIn('before=101', routes[1])

    def test_broken_latest_backup_is_not_silently_skipped(self):
        transport = backup.Transport('fake', bot_id='10')
        async def request(*args):
            return [dict(id='2', author={'id': '10'}, content=backup.PREFIX, attachments=[])]
        transport.request = request
        with self.assertRaisesRegex(RuntimeError, 'missing attachment'):
            run_inline(transport.latest(None))

    def test_upload_edits_existing_message_and_marks_acknowledgement(self):
        transport = backup.Transport('fake', bot_id='10', message_id='20')
        async def request(session, method, route, **kwargs):
            self.assertEqual(method, 'PATCH')
            self.assertTrue(route.endswith('/messages/20'))
            self.assertIsNot(kwargs['data_factory'](), kwargs['data_factory']())
            return dict(id='20', timestamp='old', edited_timestamp='new')
        transport.request = request
        run_inline(transport.upload(None, self.raw))
        self.assertEqual(json.loads(backup.marker().read_text())['revision'], '20:new')

    def test_missing_history_permission_blocks_startup(self):
        transport = backup.Transport('fake', bot_id='10')
        async def request(session, method, route):
            if '/channels/' in route:
                return dict(type=0, guild_id='1', permission_overwrites=[])
            if route.endswith('/roles'):
                return [dict(id='1', permissions=str(1024 | 2048 | 32768))]
            return dict(roles=[])
        transport.request = request
        with self.assertRaisesRegex(RuntimeError, 'Read Message History'):
            run_inline(transport.check_permissions(None))

    def test_member_overwrite_can_grant_history_permission(self):
        transport = backup.Transport('fake', bot_id='10')
        async def request(session, method, route):
            if '/channels/' in route:
                return dict(type=0, guild_id='1', permission_overwrites=[dict(id='10', type=1, allow='65536', deny='0')])
            if route.endswith('/roles'):
                return [dict(id='1', permissions=str(1024 | 2048 | 32768))]
            return dict(roles=[])
        transport.request = request
        run_inline(transport.check_permissions(None))

    def test_bootstrap_loads_before_services_are_created(self):
        class Session:
            def __init__(self, **kwargs):
                pass
            async def __aenter__(self):
                return self
            async def __aexit__(self, *args):
                pass
        async def inline(fn, *args):
            return fn(*args)
        async def identity(*args, **kwargs):
            return {'id': '10'}
        async def permissions(*args):
            pass
        async def latest(*args):
            return dict(id='20', timestamp='stamp'), {}
        async def download(*args):
            return self.raw
        for path in list(self.root.iterdir()):
            path.unlink()
        with patch.dict(os.environ, {'DISCORD_BOT_TOKEN': 'fake', 'CLOUD_MODE': '1'}), \
             patch.object(backup.aiohttp, 'ClientSession', Session), \
             patch.object(backup.Transport, 'request', identity), \
             patch.object(backup.Transport, 'check_permissions', permissions), \
             patch.object(backup.Transport, 'latest', latest), \
             patch.object(backup.Transport, 'download', download), \
             patch.object(backup.asyncio, 'to_thread', inline), patch.object(backup, 'STATE', None):
            run_inline(backup.bootstrap())
            self.assertEqual(backup.STATE.message_id, None)
            self.assertEqual(LevelStore().rank(1, 2)[0]['xp'], 1234)
            self.assertEqual(CasinoStore().balance(1, 2), 456)

    def test_failed_bootstrap_does_not_initialize_empty_databases(self):
        class Session:
            def __init__(self, **kwargs):
                pass
            async def __aenter__(self):
                return self
            async def __aexit__(self, *args):
                pass
        async def denied(*args, **kwargs):
            raise RuntimeError('Discord player backup HTTP 403')
        for path in list(self.root.iterdir()):
            path.unlink()
        with patch.dict(os.environ, {'DISCORD_BOT_TOKEN': 'fake', 'CLOUD_MODE': '1'}), \
             patch.object(backup.aiohttp, 'ClientSession', Session), \
             patch.object(backup.Transport, 'request', denied):
            with self.assertRaisesRegex(RuntimeError, '403'):
                run_inline(backup.bootstrap())
        self.assertEqual(list(self.root.iterdir()), [])


if __name__ == '__main__':
    unittest.main()
