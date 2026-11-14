import sys
import unittest
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'Discord-GameBot'))
from command_sync import sync_cloud_commands


class CommandSyncTests(unittest.TestCase):
    def check_scopes(self, target=None):
        deleted, synced, copied = [], [], []
        names = ['wordwolfex', '8192game', 'player', 'vs', 'link', 'delete', 'unrelated']

        class Command:
            def __init__(self, name, scope):
                self.name, self.scope = name, scope
            async def delete(self):
                deleted.append((self.scope, self.name))

        class Tree:
            def get_commands(self):
                return [SimpleNamespace(name=name) for name in names[:4]]
            def copy_global_to(self, guild):
                copied.append(guild.id)
            async def sync(self, guild=None):
                synced.append(guild.id if guild else None)
            async def fetch_commands(self, guild=None):
                scope = guild.id if guild else None
                return [Command(name, scope) for name in names]

        bot = SimpleNamespace(tree=Tree(), guilds=[SimpleNamespace(id=123), SimpleNamespace(id=456)])
        coroutine = sync_cloud_commands(bot, target)
        try:
            coroutine.send(None)
        except StopIteration:
            pass
        else:
            self.fail('Unexpected external I/O')
        self.assertEqual(synced, [int(target) if target else None])
        self.assertEqual(copied, [int(target)] if target else [])
        for scope in ([None, 123, 456] if target else [123, 456]):
            self.assertIn((scope, 'link'), deleted)
            self.assertIn((scope, 'delete'), deleted)
            self.assertNotIn((scope, 'unrelated'), deleted)
            for name in names[:4]:
                self.assertEqual((scope, name) in deleted, scope != (int(target) if target else None))

    def test_global_production_cleans_old_guild_copies(self):
        self.check_scopes()

    def test_test_guild_cleans_global_and_other_guild_copies(self):
        self.check_scopes('123')


if __name__ == '__main__':
    unittest.main()
