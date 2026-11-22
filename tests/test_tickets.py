import asyncio
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace as NS
import unittest
from unittest.mock import AsyncMock, Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'Discord-GameBot'))
import discord
from discord.ext import commands
from casino import CasinoStore
from leveling import LevelStore
from shop import ShopStore, ShopError, ShopService
from solo_rewards import SoloRewards
from party_rewards import PartyRewards
from ticket_store import TicketStore, RewardAudio
from tickets import register_tickets, ranking_embed, percentage, OWNER_ID
import player_backup


class TicketStoreTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.levels = LevelStore(Path(self.temp.name) / 'levels.sqlite3')
        self.casino = CasinoStore(Path(self.temp.name) / 'casino.sqlite3', levels=self.levels)
        self.tickets = TicketStore(self.casino)
        self.shop = ShopStore(self.casino, self.levels)

    def count(self, user=10, guild=1):
        return next((r['count'] for r in self.tickets.ranking(guild) if r['user_id'] == user), 0)

    def test_purchase_cost_repeat_and_replay_survive_reopen_without_equipping(self):
        self.casino.edit(1, 10, 'seed', 'set', 1500, 99)
        for event in ['one', 'one', 'two']:
            self.shop.buy(1, 10, event, 'special_ticket')
        self.assertEqual(self.casino.balance(1, 10), 500)
        self.assertEqual(self.count(), 2)
        self.assertEqual(TicketStore(CasinoStore(self.casino.path)).ranking(1)[0]['count'], 2)
        self.assertNotIn('special_ticket', self.shop.profile(1, 10)['owned'])
        self.assertFalse(self.shop.profile(1, 10)['active'])

    def test_insufficient_jewels_does_not_create_ticket(self):
        with self.assertRaises(ShopError):
            self.shop.buy(1, 10, 'poor', 'special_ticket')
        self.assertEqual(self.count(), 0)

    def test_concurrent_same_event_is_counted_once_and_guilds_are_isolated(self):
        with ThreadPoolExecutor(max_workers=4) as pool:
            results = list(pool.map(lambda _: self.tickets.grant(1, 10, 'same', 3), range(8)))
        self.assertEqual(sum(results), 1)
        self.assertEqual(self.count(), 3)
        self.assertEqual(self.count(guild=2), 0)
        with self.assertRaises(ValueError):
            self.tickets.grant(2, 10, 'same', 3)

    def test_weighted_draw_boundaries_replay_and_no_ticket_consumption(self):
        self.tickets.grant(1, 10, 'a', 1)
        self.tickets.grant(1, 20, 'b', 3)
        self.tickets.grant(2, 30, 'outside', 100)
        for offset, expected in [(0, 10), (1, 20), (2, 20), (3, 20)]:
            with patch('ticket_store.secrets.randbelow', return_value=offset) as random:
                result = self.tickets.draw(1, 'draw' + str(offset))
                self.assertEqual(result['user_id'], expected)
                random.assert_called_once_with(4)
                self.assertEqual(self.tickets.draw(1, 'draw' + str(offset)), result)
                self.assertEqual(random.call_count, 1)
        self.assertEqual((self.count(), self.count(20)), (1, 3))
        with self.assertRaises(ValueError):
            self.tickets.draw(3, 'empty')

    def test_rewards_use_exact_amounts_with_no_8192_step_ticket(self):
        solo = SoloRewards(self.casino, self.levels)
        for event in ['solo:aki:a', 'solo:kakigoori:b']:
            solo.grant(1, 10, event, 3)
            solo.grant(1, 10, event, 3)
        solo.grant(1, 10, 'solo:8192:step:2', 3)
        self.assertEqual(self.count(), 2)
        party = PartyRewards(self.casino, self.levels)
        for game in ['guess:a', 'insider:a', 'ito:a', 'wordwolf:a', 'othello:a']:
            party.grant(1, 10, game, True)
            party.grant(1, 10, game, True)
        self.assertEqual(self.count(), 17)
        self.casino.edit(1, 10, 'seed', 'set', 1000, 99)
        for index, game in enumerate(['slot', 'coinflip', 'dice']):
            choice = None if game == 'slot' else 1
            self.casino.play(1, 10, game, game, 10, choice, now=100 + index * 5)
            self.casino.play(1, 10, game, game, 10, choice, now=100 + index * 5)
        self.assertEqual(self.count(), 20)

    def test_entire_database_backup_restores_tickets_and_draw_history(self):
        self.tickets.grant(1, 10, 'backup-ticket', 3)
        result = self.tickets.draw(1, 'backup-draw')
        targets = {'levels.sqlite3': self.levels.path, 'casino.sqlite3': self.casino.path}
        with patch('player_backup.paths', return_value=targets):
            raw = player_backup.pack()
            for path in targets.values():
                path.unlink()
            player_backup.install(raw, 'ticket-version')
        reopened = TicketStore(CasinoStore(self.casino.path))
        self.assertEqual(reopened.ranking(1), [{'user_id': 10, 'count': 3}])
        self.assertEqual(reopened.draw(1, 'backup-draw'), result)


class TicketUITests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.casino = CasinoStore(Path(self.temp.name) / 'casino.sqlite3')
        self.bot = commands.Bot(command_prefix='!', intents=discord.Intents.none())
        register_tickets(self.bot, self.casino)
        self.addAsyncCleanup(self.bot.close)
        self.guild = NS(id=1, get_member=lambda uid: NS(display_name='Name ' + str(uid)))
        self.interaction = NS(id=900, user=NS(id=10), guild_id=1, guild=self.guild,
            response=NS(defer=AsyncMock(), send_message=AsyncMock()), edit_original_response=AsyncMock())

    async def test_balance_zero_total_and_ranking_pages_include_all_holders(self):
        command = self.bot.tree.get_command('ticket').get_command('balance')
        await command.callback(self.interaction)
        embed = self.interaction.edit_original_response.call_args.kwargs['embed']
        self.assertEqual(embed.fields[1].value, '0.0000%')
        for uid in range(1, 24):
            self.bot.ticket_store.grant(1, uid, f'user{uid}', uid)
        rows = self.bot.ticket_store.ranking(1)
        seen = []
        for page in range(3):
            embed, actual, pages = ranking_embed(self.bot, self.guild, rows, page)
            self.assertEqual(pages, 3)
            seen.extend(field.value.split('ID：')[1] for field in embed.fields)
        self.assertEqual(set(seen), {str(uid) for uid in range(1, 24)})
        self.assertEqual(percentage(1, 4), '25.0000%')

    async def test_only_exact_owner_can_draw_and_guild_results_are_public_embeds(self):
        command = self.bot.tree.get_command('tyuusen')
        with patch.object(self.bot.ticket_store, 'draw') as draw:
            await command.callback(self.interaction)
            draw.assert_not_called()
        self.assertTrue(self.interaction.response.send_message.call_args.kwargs['ephemeral'])
        self.interaction.user.id = OWNER_ID
        self.bot.ticket_store.grant(1, 10, 'eligible', 1)
        await command.callback(self.interaction)
        result = self.interaction.edit_original_response.call_args.kwargs
        self.assertIn('Name 10', result['embed'].description)
        self.assertFalse(result['allowed_mentions'].everyone)

    async def test_natural_audio_completion_pays_once_but_stop_error_and_silence_do_not(self):
        from sound import Sound
        sound = Sound(self.bot)
        sound.idle_disconnect = AsyncMock()
        for event, completed, frames, error, expected in [
            ('natural', True, 10, None, 1), ('stopped', False, 10, None, 1),
            ('error', True, 10, 'failure', 1), ('empty', True, 0, None, 1)]:
            source = NS(ticket_award=(10, event, 1), completed=completed, frames=frames,
                audio_bytes=100, skipped_headers=0, playback_error=error)
            sound.playing[1] = source
            await sound.finished(self.guild, source)
            await sound.finished(self.guild, source)
            self.assertEqual(self.bot.ticket_store.ranking(1)[0]['count'], expected)
        sound.cancel_idle(1)
        await asyncio.sleep(0)

    async def test_ticket_purchase_never_triggers_minecraft_sync(self):
        self.casino.edit(1, 10, 'seed-ui', 'set', 500, 99)
        levels = LevelStore(Path(self.temp.name) / 'levels.sqlite3')
        service = ShopService(self.bot, ShopStore(self.casino, levels))
        service.sync = AsyncMock()
        await service.purchase(self.interaction, 'special_ticket')
        service.sync.assert_not_awaited()
        self.assertEqual(self.bot.ticket_store.ranking(1)[0]['count'], 1)

    async def test_tensei_and_context_menu_award_executor_once_after_success(self):
        from tensei import register_tensei
        from message_apps import register_message_apps
        register_tensei(self.bot)
        register_message_apps(self.bot)
        self.interaction.user.display_name = 'Tester'
        await self.bot.tree.get_command('tensei').callback(self.interaction)
        await self.bot.tree.get_command('tensei').callback(self.interaction)
        self.assertEqual(self.bot.ticket_store.ranking(1)[0]['count'], 1)
        self.interaction.id = 901
        message = NS(guild=self.guild, reply=AsyncMock())
        command = self.bot.tree.get_command('高専適正度', type=discord.AppCommandType.message)
        await command.callback(self.interaction, message)
        self.assertEqual(self.bot.ticket_store.ranking(1)[0]['count'], 2)
        message.reply.side_effect = discord.Forbidden(NS(status=403, reason='Forbidden'), 'forbidden')
        self.interaction.id = 902
        await command.callback(self.interaction, message)
        self.assertEqual(self.bot.ticket_store.ranking(1)[0]['count'], 2)

    async def test_8192_loss_and_clear_award_one_but_successful_intermediate_step_does_not(self):
        from game_8192 import Game, Game8192View, register_8192
        levels = LevelStore(Path(self.temp.name) / 'levels.sqlite3')
        self.bot.solo_rewards = SoloRewards(self.casino, levels)
        self.bot.leveling_service = NS(store=levels, notify=AsyncMock())
        self.interaction.channel = NS(id=2)
        self.interaction.followup = NS(send=AsyncMock())
        self.interaction.edit_original_response.return_value = NS(edit=AsyncMock())
        registry = register_8192(self.bot)
        self.assertIsNotNone(self.bot.tree.get_command('8192games'))
        game = Game(self.bot, self.interaction, registry)
        view = Game8192View(game)
        await view.choose(self.interaction, view.correct_color)
        self.assertEqual(self.bot.ticket_store.ranking(1), [])
        view = Game8192View(game)
        await view.choose(self.interaction, 'green' if view.correct_color == 'blue' else 'blue')
        self.assertEqual(self.bot.ticket_store.ranking(1)[0]['count'], 1)
        clear_game = Game(self.bot, self.interaction, registry)
        clear_game.count = 12
        view = Game8192View(clear_game)
        await view.choose(self.interaction, view.correct_color)
        self.assertEqual(self.bot.ticket_store.ranking(1)[0]['count'], 2)
        await view.choose(self.interaction, view.correct_color)
        self.assertEqual(self.bot.ticket_store.ranking(1)[0]['count'], 2)
