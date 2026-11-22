"""Talk voices and per-library credits; song/Nemo IDs are not exposed."""
import json
from pathlib import Path
import unicodedata

DATA = json.loads(Path(__file__).with_suffix('.json').read_text(encoding='utf-8'))
CHARACTERS = {entry['name']: entry for entry in DATA['characters']}
VOICES = {style['id']: f'{entry["name"]}（{style["name"]}）'
          for entry in DATA['characters'] for style in entry['styles']}
API_LABELS = {style['id']: f'{entry.get("api_name", entry["name"])}（{style["name"]}）'
              for entry in DATA['characters'] for style in entry['styles']}
VOICE_INFO = {style['id']: entry for entry in DATA['characters'] for style in entry['styles']}
REVIEWED_NAMES = set(CHARACTERS)


def normalized(text):
    return ''.join(unicodedata.normalize('NFKC', str(text)).casefold().split())


def speaker_matches(expected, actual):
    return normalized(expected) in normalized(actual)


def voice_info(voice):
    return VOICE_INFO.get(str(voice))


def credits_text():
    lines = ['VOICEVOX音声クレジット・利用規約一覧',
             '以下は対応候補ごとの表記例です。利用実績の一覧ではありません。',
             '個人の非商用チャット読み上げを前提とした候補です。各音声の規約を確認してください。',
             '画像・キャラクターのなりきり・企業利用・有料サービス化は別の条件が適用されることがあります。', '']
    for entry in CHARACTERS.values():
        lines.extend([entry['credit'], entry['terms']])
        if entry['note']:
            lines.append(entry['note'])
        lines.append('')
    return '\n'.join(lines)
