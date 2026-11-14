import random
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'Discord-GameBot'))
import discord
from tensei import PERIODS, SPECIAL_LIVES, TenseiView, generate_life, life_embed, register_tensei


class LifeTests(unittest.TestCase):
    def test_random_lifetimes_have_valid_values_and_variety(self):
        results = [generate_life(random.Random(seed)) for seed in range(200)]
        self.assertGreater(len(set(results)), 150)
        for result in results:
            self.assertEqual(len(result.periods), 6)
            self.assertTrue(all(result.periods))
            self.assertGreaterEqual(result.income, 0)
            self.assertEqual(result.income % 10000, 0)
            self.assertTrue(0 <= result.happiness <= 100)
            self.assertTrue(result.explanation)

    def test_study_special_matches_requested_example(self):
        rng = SimpleNamespace(randrange=lambda _: 0, choice=lambda options: options[0])
        life = generate_life(rng)
        self.assertEqual(life.periods, ('勉強',) * 6)
        self.assertEqual((life.income, life.happiness, life.explanation), (0, 0, '学び過ぎた'))
        embed = life_embed(life, 'TestPlayer')
        self.assertEqual([field.name for field in embed.fields], [*PERIODS, '最終年収', '幸福度', '説明'])
        self.assertEqual([field.value for field in embed.fields][-3:], ['0円', '0%', '学び過ぎた'])
        self.assertLess(len(embed), 6000)


class InteractionTests(unittest.IsolatedAsyncioTestCase):
    async def test_other_players_cannot_reroll(self):
        view = TenseiView(1)
        interaction = SimpleNamespace(user=SimpleNamespace(id=2), response=SimpleNamespace(send_message=AsyncMock()))
        self.assertFalse(await view.interaction_check(interaction))
        self.assertTrue(interaction.response.send_message.call_args.kwargs['ephemeral'])
        interaction.user.id = 1
        self.assertTrue(await view.interaction_check(interaction))
        view.stop()

    async def test_command_defers_then_sends_and_button_replaces_life(self):
        bot = SimpleNamespace(tree=discord.app_commands.CommandTree(discord.Client(intents=discord.Intents.none())))
        register_tensei(bot)
        command = bot.tree.get_command('tensei')
        self.assertEqual(command.description, '世界でもっとも深いガチャ')
        message = SimpleNamespace(edit=AsyncMock())
        response = SimpleNamespace(defer=AsyncMock(), edit_message=AsyncMock())
        interaction = SimpleNamespace(user=SimpleNamespace(id=1, display_name='TestPlayer'), response=response,
                                      edit_original_response=AsyncMock(return_value=message))
        with patch('tensei.generate_life', return_value=SPECIAL_LIVES[0]):
            await command.callback(interaction)
        response.defer.assert_awaited_once_with(thinking=True)
        view = interaction.edit_original_response.call_args.kwargs['view']
        self.assertIs(view.message, message)
        with patch('tensei.generate_life', return_value=SPECIAL_LIVES[1]):
            await view.children[0].callback(interaction)
        self.assertEqual(response.edit_message.call_args.kwargs['embed'].fields[-2].value, '100%')
        await view.on_timeout()
        self.assertTrue(all(child.disabled for child in view.children))
        message.edit.assert_awaited_once_with(view=view)
        view.stop()


if __name__ == '__main__':
    unittest.main()
