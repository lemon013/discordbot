from contextlib import closing
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace as NS
import unittest
from unittest.mock import AsyncMock, patch

import discord
from discord.ext import commands

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'Discord-GameBot'))
from leveling import LevelStore, Leveling
from xp_role_bonus import BonusStore, RankBonusView, register_xp_bonus
from casino import CasinoStore, CasinoError, MAX_BALANCE
from solo_rewards import SoloRewards
from party_rewards import PartyRewards
from shop import ShopStore, ITEMS


class BonusTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.addCleanup(self.folder.cleanup)
        self.levels = LevelStore(Path(self.folder.name) / 'levels.db')
        self.bonus = self.levels.bonuses
        self.casino = CasinoStore(Path(self.folder.name) / 'casino.db', levels=self.levels)
        self.bonus.remember(1, 10, [1, 11, 12])
        self.bot = commands.Bot(command_prefix='!', intents=discord.Intents.none())
        self.addAsyncCleanup(self.bot.close)

    async def test_five_percent_steps_and_guild_isolation(self):
        for percent in (0, -5, 6, 1001, 12.5, True):
            with self.assertRaises(ValueError): self.bonus.set(1, 11, percent)
        self.bonus.set(1, 11, 25)
        self.assertEqual(self.bonus.list(1), [(11, 25)])
        self.assertEqual(self.bonus.list(2), [])

    async def test_five_and_ten_percent_are_fifteen_not_compounded(self):
        self.bonus.set(1, 11, 5)
        self.bonus.set(1, 12, 10)
        self.levels.configure(1, dict(text_xp_min=100, text_xp_max=100))
        result, _ = self.levels.award_text(1, 10, now=100, roles=[11, 12, 11, 999])
        self.assertEqual(result['xp'], 115)
        self.assertEqual(SoloRewards(self.casino, self.levels).grant(1, 10, 'stacked:solo', 100)['xp'], 115)

    async def test_rank_shows_target_members_roles_and_combined_percentage(self):
        self.bonus.set(1, 11, 5)
        self.bonus.set(1, 12, 10)
        service = Leveling(self.bot, self.levels)
        interaction = NS(user=NS(id=99, roles=[NS(id=999)]), guild_id=1,
            response=NS(defer=AsyncMock()), edit_original_response=AsyncMock())
        member = NS(id=10, display_name='Target', roles=[NS(id=11), NS(id=12)])
        await service.rank(interaction, member)
        embed = interaction.edit_original_response.call_args.kwargs['embed']
        self.assertIn('Target', embed.title)
        fields = {field.name: field.value for field in embed.fields}
        self.assertIn('＋15％', fields['獲得XPボーナス合計'])
        self.assertIn('115％', fields['獲得XPボーナス合計'])
        self.assertIn('<@&11>：＋5％', fields['加算対象ロール（1/1ページ）'])
        self.assertIn('<@&12>：＋10％', fields['加算対象ロール（1/1ページ）'])
        member.roles = []
        await service.rank(interaction, member)
        fields = interaction.edit_original_response.call_args.kwargs['embed'].fields
        self.assertIn('＋0％', fields[-2].value)
        self.assertIn('ありません', fields[-1].value)

    async def test_rank_bonus_pages_keep_total_and_reject_other_operators(self):
        roles = [(i, 5) for i in range(35)]
        view = RankBonusView(99, discord.Embed(title='rank'), roles)
        self.assertIn('＋175％', view.embed().fields[-2].value)
        interaction = NS(user=NS(id=99), response=NS(edit_message=AsyncMock(), send_message=AsyncMock()))
        await view.next.callback(interaction)
        embed = interaction.response.edit_message.call_args.kwargs['embed']
        self.assertIn('2/4', embed.fields[-1].name)
        self.assertIn('<@&10>', embed.fields[-1].value)
        self.assertIn('＋175％', embed.fields[-2].value)
        interaction.user.id = 100
        self.assertFalse(await view.interaction_check(interaction))
        view.stop()

    async def test_role_percentages_stack_additively_and_removal_is_immediate(self):
        self.levels.configure(1, dict(text_xp_min=100, text_xp_max=100))
        self.bonus.set(1, 11, 25)
        self.bonus.set(1, 12, 50)
        result, _ = self.levels.award_text(1, 10, now=100)
        self.assertEqual(result['xp'], 175)
        self.bonus.remove(1, 12)
        result, _ = self.levels.award_text(1, 10, now=200)
        self.assertEqual(result['xp'], 300)
        result, _ = self.levels.award_text(1, 10, now=300, roles=[])
        self.assertEqual(result['xp'], 400)

    async def test_small_fraction_survives_restart_and_replay_does_not_add_twice(self):
        self.bonus.set(1, 11, 5)
        rewards = SoloRewards(self.casino, self.levels)
        for i in range(6): rewards.grant(1, 10, f'fraction:{i}', 3)
        self.assertEqual(self.levels.rank(1, 10)[0]['xp'], 18)
        reopened = LevelStore(self.levels.path)
        rewards = SoloRewards(self.casino, reopened)
        result = rewards.grant(1, 10, 'fraction:6', 3)
        self.assertEqual(result['xp'], 4)
        self.bonus.set(1, 11, 100)
        self.assertFalse(rewards.grant(1, 10, 'fraction:6', 3)['new'])
        self.assertEqual(reopened.rank(1, 10)[0]['xp'], 22)

    async def test_voice_bonus_and_text_cooldown(self):
        self.bonus.set(1, 11, 50)
        self.levels.configure(1, dict(text_xp_min=10, text_xp_max=10))
        self.levels.award_text(1, 10, now=100)
        self.assertIsNone(self.levels.award_text(1, 10, now=101)[0])
        rows, _ = self.levels.award_voice(1, [(10, 1)], roles={10: [11]})
        self.assertEqual(rows[0]['xp'], 30)

    async def test_party_casino_and_purchased_xp_receive_bonus(self):
        self.bonus.set(1, 11, 100)
        party = PartyRewards(self.casino, self.levels)
        result = party.grant(1, 10, 'ito:test', True)
        self.assertEqual(result['xp'], 20)
        self.casino.edit(1, 10, 'seed', 'add', 100000, 99)
        result = self.casino.play(1, 10, 'casino:test', 'coinflip', 10, choice=0)
        self.assertEqual(result['xp_reward']['xp'], result['xp_reward']['base_xp'] * 2)
        item = next(item for item in ITEMS.values() if item['kind'] == 'xp')
        result = ShopStore(self.casino, self.levels).buy(1, 10, 'shop:test', item['id'])
        self.assertEqual(result['xp'], item['xp'] * 2)

    async def test_failed_reward_rolls_back_fraction_and_xp(self):
        self.bonus.set(1, 11, 5)
        self.casino.edit(1, 10, 'seed', 'set', 1000000000, 99)
        with closing(self.casino.connect()) as db, db:
            db.execute('UPDATE casino_wallets SET balance=?', (MAX_BALANCE,))
        with self.assertRaises(CasinoError):
            SoloRewards(self.casino, self.levels).grant(1, 10, 'failed', 3, gems=1)
        with closing(self.levels.connect()) as db:
            self.assertEqual(db.execute('SELECT count(*) FROM xp_bonus_carry').fetchone()[0], 0)
        self.assertEqual(self.levels.rank(1, 10)[0]['xp'], 0)

    async def test_admin_edits_remain_exact(self):
        self.bonus.set(1, 11, 100)
        self.levels.edit(1, 10, 'xp', 'set', 100)
        self.assertEqual(self.levels.rank(1, 10)[0]['xp'], 100)

    async def test_permission_checks_and_public_paged_list(self):
        register_xp_bonus(self.bot, Leveling(self.bot, self.levels))
        group = self.bot.tree.get_command('xpbonus')
        interaction = NS(guild_id=1, user=NS(guild_permissions=NS(manage_guild=False)),
            response=NS(defer=AsyncMock()), edit_original_response=AsyncMock())
        await group.get_command('set').callback(interaction, NS(guild=NS(id=1), id=11, mention='<@&11>'), 25)
        self.assertEqual(self.bonus.list(1), [])
        self.assertIn('権限', interaction.edit_original_response.call_args.kwargs['content'])
        for role in range(20, 35): self.bonus.set(1, role, 25)
        await group.get_command('list').callback(interaction, 2)
        embed = interaction.edit_original_response.call_args.kwargs['embed']
        self.assertEqual(len(embed.description.splitlines()), 5)
        self.assertIn('2/2', embed.footer.text)
