import json
from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'Discord-GameBot'))
from voice_catalog import CHARACTERS, VOICES, API_LABELS, VOICE_INFO, speaker_matches, credits_text, DATA


class CatalogTests(unittest.TestCase):
    def test_talk_catalog_has_unique_ids_and_covers_old_and_new_voices_without_song_ids(self):
        styles = [style for entry in CHARACTERS.values() for style in entry['styles']]
        self.assertGreaterEqual(len(CHARACTERS), 40)
        self.assertGreaterEqual(len(styles), 110)
        self.assertEqual(len(styles), len(VOICES))
        self.assertEqual(len({style['id'] for style in styles}), len(styles))
        self.assertTrue(all(0 <= int(style['id']) < 3000 for style in styles))
        for name in ('春日部つむぎ', '雨晴はう', '青山龍星', 'WhiteCUL', '東北きりたん', 'ナースロボ＿タイプＴ'):
            self.assertIn(name, CHARACTERS)
        self.assertNotIn('里石ユカ', CHARACTERS)
        self.assertTrue(any(entry['name'] == '里石ユカ' for entry in DATA['excluded']))

    def test_special_credits_and_conditions_are_preserved(self):
        self.assertIn('明日葉よもぎ', CHARACTERS['もち子さん']['credit'])
        self.assertIn('丹下桜', CHARACTERS['Voidoll']['credit'])
        self.assertIn('神崎零', CHARACTERS['ユーレイちゃん']['credit'])
        self.assertIn('事前', CHARACTERS['青山龍星']['note'])
        self.assertIn('非商用', CHARACTERS['No.7']['note'])
        text = credits_text()
        self.assertNotIn('github.com', text.lower())
        self.assertIn('利用実績の一覧ではありません', text)
        self.assertTrue(all(entry['credit'] in text and entry['terms'] in text for entry in CHARACTERS.values()))

    def test_speaker_matching_accepts_width_and_spacing_but_not_a_different_voice_or_style(self):
        self.assertTrue(speaker_matches('ナースロボ＿タイプＴ（ノーマル）', 'VOICEVOX:ナースロボ_タイプT(ノーマル)'))
        self.assertTrue(speaker_matches('†聖騎士 紅桜†（ノーマル）', 'VOICEVOX:†聖騎士紅桜†（ノーマル）'))
        self.assertFalse(speaker_matches('ずんだもん（ささやき）', 'VOICEVOX:ずんだもん（ノーマル）'))
        self.assertFalse(speaker_matches('青山龍星（ノーマル）', 'VOICEVOX:玄野武宏（ノーマル）'))

    def test_recorded_provider_responses_match_all_exposed_characters_including_cv_aliases(self):
        fixture = json.loads((Path(__file__).parent / 'fixtures' / 'quest_voice_catalog_responses.json').read_text(encoding='utf-8'))
        self.assertEqual({row['character'] for row in fixture['responses']}, set(CHARACTERS))
        for row in fixture['responses']:
            with self.subTest(character=row['character']):
                self.assertTrue(row['success'])
                self.assertTrue(speaker_matches(API_LABELS[row['voice']], row['speakerName']))
        self.assertIn('明日葉よもぎ', API_LABELS['20'])
        self.assertIn('丹下桜', API_LABELS['89'])
        self.assertIn('神崎零', API_LABELS['102'])
