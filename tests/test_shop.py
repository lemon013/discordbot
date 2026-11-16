from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
import hashlib
import hmac
import importlib.util
import json
from pathlib import Path
import shutil
import sqlite3
import subprocess
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'Discord-GameBot'))
import discord
from discord.ext import commands
from casino import CasinoStore
from leveling import LevelStore, threshold
from shop import ShopStore, ShopError, ShopService, ShopView, ConfirmView, ITEMS, CATEGORIES, PAGE_SIZE, category_page, signed_snapshot, register_shop


class ShopStoreTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.casino = CasinoStore(Path(self.temp.name) / 'casino.sqlite3')
        self.levels = LevelStore(Path(self.temp.name) / 'levels.sqlite3')
        self.store = ShopStore(self.casino, self.levels)
        self.casino.edit(1, 10, 'seed', 'set', 50000, 99)

    def test_xp_purchase_exact_credit_replay_and_repeat(self):
        result = self.store.buy(1, 10, 'purchase1', 'xp5')
        self.assertEqual((result['price'], result['balance'], result['xp']), (1000, 49000, 5))
        self.assertEqual(self.levels.rank(1, 10)[0]['shop_xp'], 5)
        self.assertEqual(self.store.buy(1, 10, 'purchase1', 'xp5'), result)
        self.assertEqual(self.levels.rank(1, 10)[0]['xp'], 5)
        self.store.buy(1, 10, 'purchase2', 'xp5')
        self.assertEqual(self.levels.rank(1, 10)[0]['xp'], 10)
        self.assertEqual(self.casino.balance(1, 10), 48000)

    def test_cosmetics_ownership_free_equip_and_persistent_active(self):
        self.store.buy(1, 10, 'camera', 'record_camera')
        self.store.buy(1, 10, 'welcome', 'welcome_wild')
        self.store.buy(1, 10, 'orb', 'record_orb')
        result = self.store.buy(1, 10, 'equip', 'record_camera')
        self.assertEqual(result['price'], 0)
        reopened = ShopStore(self.casino, self.levels)
        profile = reopened.profile(1, 10)
        self.assertEqual(profile['active'], {'record': 'record_camera', 'welcome': 'welcome_wild'})
        self.assertEqual(len(profile['owned']), 3)
        self.assertEqual(profile['balance'], 42500)
        self.assertEqual(reopened.snapshot(1)['10']['record'], 'record_camera')
        self.assertEqual(reopened.snapshot(2), {})

    def test_insufficient_funds_and_unknown_product_do_not_charge(self):
        with self.assertRaises(ShopError):
            self.store.buy(1, 20, 'poor', 'xp5')
        with self.assertRaises(ShopError):
            self.store.buy(1, 10, 'unknown', 'not-an-item')
        self.assertEqual(self.levels.rank(1, 20)[0]['xp'], 0)
        self.assertEqual(self.casino.balance(1, 10), 50000)

    def test_xp_failure_rolls_back_payment_and_history(self):
        with closing(self.levels.connect()) as db, db:
            db.execute("CREATE TRIGGER reject_shop_xp BEFORE UPDATE OF xp ON level_users BEGIN SELECT RAISE(ABORT, 'test'); END")
        with self.assertRaises(sqlite3.IntegrityError):
            self.store.buy(1, 10, 'rollback', 'xp5')
        self.assertEqual(self.casino.balance(1, 10), 50000)
        self.assertEqual(len(self.casino.history(1, 10)), 1)
        self.assertEqual(self.levels.rank(1, 10)[0]['xp'], 0)

    def test_concurrent_duplicate_purchase_is_once(self):
        with ThreadPoolExecutor(max_workers=6) as pool:
            results = list(pool.map(lambda _: self.store.buy(1, 10, 'same', 'xp5'), range(12)))
        self.assertTrue(all(result['balance'] == 49000 for result in results))
        self.assertEqual(self.levels.rank(1, 10)[0]['xp'], 5)

    def test_concurrent_cosmetics_charge_only_once(self):
        with ThreadPoolExecutor(max_workers=6) as pool:
            list(pool.map(lambda index: self.store.buy(1, 10, str(index), 'record_camera'), range(12)))
        self.assertEqual(self.casino.balance(1, 10), 48000)
        self.assertEqual(self.store.profile(1, 10)['owned'], ['record_camera'])

    def test_shared_database_paths_also_work(self):
        shared_levels = LevelStore(self.casino.path)
        shared = ShopStore(self.casino, shared_levels)
        shared.buy(1, 10, 'shared-xp', 'xp5')
        self.assertEqual(shared_levels.rank(1, 10)[0]['xp'], 5)
        self.assertEqual(self.casino.balance(1, 10), 49000)

    def test_purchase_level_boundary_and_maximum(self):
        self.levels.edit(1, 10, 'xp', 'set', threshold(30) - 5)
        result = self.store.buy(1, 10, 'to30', 'xp5')
        self.assertEqual((result['before'], result['after']), (29, 30))
        self.levels.edit(1, 10, 'level', 'set', 100)
        result = self.store.buy(1, 10, 'max', 'xp5')
        self.assertEqual((result['before'], result['after']), (100, 100))
        self.assertEqual(self.levels.rank(1, 10)[0]['shop_xp'], 10)

    def test_signed_snapshot_and_cross_language_validation(self):
        secret = 'test-shop-secret-' * 3
        raw = signed_snapshot(1, {'10': {'record': 'record_camera', 'welcome': 'welcome_wild'}}, secret, stamp=1000)
        envelope = json.loads(raw)
        expected = hmac.new(secret.encode(), envelope['payload'].encode(), hashlib.sha256).hexdigest()
        self.assertEqual(expected, envelope['signature'])
        if shutil.which('node'):
            url = (ROOT / 'Minecraft-Discord-Bridge/scripts/shopEffects.js').as_uri()
            script = f"import {{verifyShopSnapshot}} from {json.dumps(url)}; let raw=''; for await(const part of process.stdin) raw+=part; const result=verifyShopSnapshot(raw,{json.dumps(secret)},'1',0,2000); if(result.active['10'].record!=='record_camera') process.exit(1);"
            result = subprocess.run(['node', '--input-type=module', '-e', script], input=raw, capture_output=True)
            self.assertEqual(result.returncode, 0, result.stderr.decode())


class ShopUITests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        casino = CasinoStore(Path(self.temp.name) / 'casino.sqlite3')
        levels = LevelStore(Path(self.temp.name) / 'levels.sqlite3')
        self.store = ShopStore(casino, levels)
        self.service = ShopService(SimpleNamespace(), self.store)
        async def inline(function, *args, **kwargs):
            return function(*args, **kwargs)
        patcher = patch('shop.asyncio.to_thread', side_effect=inline)
        patcher.start()
        self.addCleanup(patcher.stop)

    async def test_board_single_selection_and_ownership_guard(self):
        profile = self.store.profile(1, 10)
        view = ShopView(self.service, 10, profile)
        self.assertEqual((view.children[0].min_values, view.children[0].max_values), (1, 1))
        self.assertEqual(len(view.children[0].options), 3)
        self.assertEqual(len(self.service.board(profile).fields), 3)
        interaction = SimpleNamespace(user=SimpleNamespace(id=20), guild=SimpleNamespace(id=1), response=SimpleNamespace(send_message=AsyncMock()))
        self.assertFalse(await view.interaction_check(interaction))
        interaction.user.id = 10
        self.assertTrue(await view.interaction_check(interaction))
        view.stop()

    async def test_confirmation_button_cannot_buy_twice(self):
        self.service.purchase = AsyncMock()
        view = ConfirmView(self.service, 10, 'xp5', False)
        button = next(child for child in view.children if child.label == '購入する')
        interaction = SimpleNamespace(response=SimpleNamespace(send_message=AsyncMock()))
        await button.callback(interaction)
        await button.callback(interaction)
        self.service.purchase.assert_awaited_once_with(interaction, 'xp5', page=0)
        view.stop()

    async def test_shop_command_registration(self):
        bot = commands.Bot(command_prefix='!', intents=discord.Intents.default())
        with patch('shop.ShopService', return_value=self.service):
            register_shop(bot)
        self.assertTrue(bot.tree.get_command('shop').guild_only)
        self.assertIn('on_ready', bot.extra_events)

    async def test_xp_purchase_notifies_level_service(self):
        self.store.casino.edit(1, 10, 'seed', 'set', 2000, 99)
        self.store.levels.edit(1, 10, 'xp', 'set', threshold(30) - 5)
        leveling = SimpleNamespace(store=self.store.levels, notify=AsyncMock())
        self.service.bot = SimpleNamespace(leveling_service=leveling)
        interaction = SimpleNamespace(id=123, guild_id=1, user=SimpleNamespace(id=10),
            guild=SimpleNamespace(id=1), channel=SimpleNamespace(),
            response=SimpleNamespace(defer=AsyncMock()), edit_original_response=AsyncMock())
        await self.service.purchase(interaction, 'xp5')
        result = leveling.notify.call_args.args[1][0]
        self.assertEqual((result['before'], result['after']), (29, 30))
        interaction.edit_original_response.call_args.kwargs['view'].stop()

    async def test_signed_sync_is_scoped_and_heartbeats_without_duplicate_posts(self):
        channel = SimpleNamespace(guild=SimpleNamespace(id=1), send=AsyncMock())
        self.service.bot = SimpleNamespace(get_channel=lambda channel_id: channel)
        with patch.dict('os.environ', {'MC_SYNC_CHANNEL_ID': '12', 'MC_SYNC_SECRET': 'test-secret-' * 4}):
            self.assertFalse(await self.service.sync(2))
            self.assertTrue(await self.service.sync(1))
            self.assertTrue(await self.service.sync(1))
            self.assertEqual(channel.send.await_count, 1)
            digest, stamp = self.service.sent[1]
            self.service.sent[1] = (digest, stamp - 601)
            self.assertTrue(await self.service.sync(1))
            self.assertEqual(channel.send.await_count, 2)


    async def test_each_category_is_paged_without_missing_or_mixing_products(self):
        profile = self.store.profile(1, 10)
        self.assertEqual(set(CATEGORIES), {'xp', 'record', 'welcome'})
        for category in CATEGORIES:
            collected = []
            _, _, pages = category_page(category)
            for page in range(pages):
                view = ShopView(self.service, 10, profile, category=category, page=page)
                selector = next(child for child in view.children if isinstance(child, discord.ui.Select) and child.row == 1)
                ids = [option.value for option in selector.options]
                self.assertLessEqual(len(ids), PAGE_SIZE)
                self.assertTrue(all(ITEMS[item_id]['kind'] == category for item_id in ids))
                self.assertEqual(len(self.service.board(profile, category, page).fields), len(ids))
                collected.extend(ids)
                view.stop()
            self.assertEqual(set(collected), {item_id for item_id, item in ITEMS.items() if item['kind'] == category})
            self.assertEqual(len(collected), len(set(collected)))
        self.assertEqual(category_page('record', 999)[1], 1)
        self.assertEqual(category_page('record', -1)[1], 0)

    async def test_navigation_and_confirmation_return_preserve_category_page(self):
        profile = self.store.profile(1, 10)
        self.service.show = AsyncMock()
        view = ShopView(self.service, 10, profile, category='record', page=0)
        next_button = next(child for child in view.children if getattr(child, 'label', '') == '次のページ')
        self.assertTrue(next(child for child in view.children if getattr(child, 'label', '') == '前のページ').disabled)
        await next_button.callback('interaction')
        self.service.show.assert_awaited_once_with('interaction', category='record', page=1, replace=True)
        self.service.show.reset_mock()
        confirm = ConfirmView(self.service, 10, category_page('record', 1)[0][0]['id'], False, page=1)
        await confirm.back.callback('interaction')
        self.service.show.assert_awaited_once_with('interaction', category='record', page=1, replace=True)
        view.stop()
        confirm.stop()

    async def test_page_changes_edit_existing_board_and_do_not_spend(self):
        interaction = SimpleNamespace(guild_id=1, user=SimpleNamespace(id=10),
            response=SimpleNamespace(defer=AsyncMock()), edit_original_response=AsyncMock())
        await self.service.show(interaction, category='record', page=1, replace=True)
        interaction.response.defer.assert_awaited_once_with(ephemeral=True, thinking=False)
        view = interaction.edit_original_response.call_args.kwargs['view']
        self.assertEqual((view.category, view.page), ('record', 1))
        self.assertEqual(self.store.casino.balance(1, 10), 0)
        self.assertEqual(self.store.casino.history(1, 10), [])
        view.stop()

    async def test_original_products_and_new_catalog_survive_upgrade(self):
        self.assertEqual(ITEMS['xp5']['price'], 1000)
        self.assertEqual(ITEMS['record_camera']['sound'], 'random.screenshot')
        self.assertEqual(ITEMS['welcome_wild']['text'], 'あ！野生の<プレイヤーネーム>だ！')
        self.assertEqual(len([item for item in ITEMS.values() if item['kind'] == 'record']), 15)
        self.assertEqual(len([item for item in ITEMS.values() if item['kind'] == 'welcome']), 13)
        self.assertTrue(all('<プレイヤーネーム>' in item['text'] for item in ITEMS.values() if item['kind'] == 'welcome'))


if __name__ == '__main__':
    unittest.main()
