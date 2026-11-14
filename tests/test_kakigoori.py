import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'Discord-GameBot'))
import discord
from kakigoori import INGREDIENTS, KakigooriView, bowl_colour, evaluate, register_kakigoori


class RecipeTests(unittest.TestCase):
    def test_requested_ingredients_are_unique(self):
        self.assertEqual({item.name for item in INGREDIENTS.values()}, {
            'イチゴ', 'ブルーハワイ', 'メロン', 'レモン', 'マンゴー', 'コーラ', 'グレープ',
            '桃', '練乳', 'オレンジ', 'ラムネ', '抹茶', '黒蜜', '小豆',
        })
        self.assertEqual(len(INGREDIENTS), 14)

    def test_pairing_and_amount_affect_rating_consistently(self):
        tasty = {'strawberry': 1, 'milk': 1}
        self.assertEqual(evaluate(tasty), evaluate(tasty))
        self.assertGreater(evaluate(tasty)[0], evaluate({'matcha': 1, 'ramune': 1})[0])
        self.assertGreater(evaluate(tasty)[0], evaluate({'strawberry': 50, 'milk': 50})[0])
        self.assertIsNone(evaluate({}))
        for recipe in (tasty, {'milk': 1000000}, dict.fromkeys(INGREDIENTS, 100)):
            self.assertTrue(0 <= evaluate(recipe)[0] <= 100)
            self.assertTrue(0 <= bowl_colour(recipe) <= 0xFFFFFF)


class BowlTests(unittest.IsolatedAsyncioTestCase):
    def interaction(self, player_id=1):
        return SimpleNamespace(user=SimpleNamespace(id=player_id, display_name='TestPlayer'),
                               response=SimpleNamespace(send_message=AsyncMock(), edit_message=AsyncMock()))

    async def test_mix_repeat_finish_and_reset(self):
        view = KakigooriView(1, 'TestPlayer')
        interaction = self.interaction()
        await view.finish.callback(interaction)
        self.assertFalse(view.finished)
        view.selected = ['strawberry', 'milk']
        view.amount = 100
        await view.add.callback(interaction)
        await view.add.callback(interaction)
        self.assertEqual(view.recipe, {'strawberry': 200, 'milk': 200})
        await view.finish.callback(interaction)
        self.assertTrue(view.finished)
        self.assertTrue(view.add.disabled)
        self.assertFalse(view.reset.disabled)
        self.assertIn('美味しそう度', [field.name for field in view.embed().fields])
        await view.add.callback(interaction)
        self.assertEqual(view.recipe['milk'], 200)
        await view.reset.callback(interaction)
        self.assertFalse(view.finished)
        self.assertEqual(view.recipe, {})
        self.assertTrue(all(not item.disabled for item in view.children))
        view.stop()

    async def test_ownership_and_no_empty_add(self):
        view = KakigooriView(1, 'TestPlayer')
        other = self.interaction(2)
        self.assertFalse(await view.interaction_check(other))
        self.assertTrue(other.response.send_message.call_args.kwargs['ephemeral'])
        own = self.interaction()
        self.assertTrue(await view.interaction_check(own))
        await view.add.callback(own)
        self.assertEqual(view.recipe, {})
        own.response.send_message.assert_awaited_once()
        view.stop()

    async def test_all_ingredients_fit_embed_and_timeout_disables_controls(self):
        view = KakigooriView(1, 'TestPlayer')
        view.recipe = dict.fromkeys(INGREDIENTS, 100)
        view.finished = True
        embed = view.embed()
        self.assertLess(len(embed), 6000)
        self.assertTrue(all(len(field.value) <= 1024 for field in embed.fields))
        view.message = SimpleNamespace(edit=AsyncMock())
        await view.on_timeout()
        self.assertTrue(all(item.disabled for item in view.children))
        view.message.edit.assert_awaited_once_with(view=view)
        view.stop()

    async def test_command_acknowledges_before_showing_bowl(self):
        bot = SimpleNamespace(tree=discord.app_commands.CommandTree(discord.Client(intents=discord.Intents.none())))
        register_kakigoori(bot)
        command = bot.tree.get_command('kakigoori')
        response = SimpleNamespace(defer=AsyncMock())
        message = SimpleNamespace(edit=AsyncMock())
        interaction = SimpleNamespace(user=SimpleNamespace(id=1, display_name='TestPlayer'), response=response,
                                      edit_original_response=AsyncMock(return_value=message))
        await command.callback(interaction)
        response.defer.assert_awaited_once_with(thinking=True)
        view = interaction.edit_original_response.call_args.kwargs['view']
        self.assertIs(view.message, message)
        self.assertEqual(len(view.choose.options), 14)
        view.stop()


if __name__ == '__main__':
    unittest.main()
