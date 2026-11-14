"""Read Bridge snapshots without starting a Minecraft connection."""
import asyncio
import json
import logging
import os
from pathlib import Path

log = logging.getLogger(__name__)
DATA_FILE = Path(os.environ.get('PLAYER_STATUS_FILE') or (
    Path(__file__).resolve().parent.parent / 'Minecraft-Discord-Bridge' / '.discord-mcbe' / 'data.json'))
GAMES = {
    'unlimited': ('unlimited', [('up', 'Level'), ('wingame', '勝利数'), ('killCount', 'キル数'), ('deathCount', 'デス数')]),
    'MINIGAMER': ('mini_neo', [('levellevel', 'Level'), ('rankrank', 'Rank'), ('RCrocal', 'RocalCoin'), ('winwin', 'Win'), ('matchmatch', 'Match')]),
    'MurderMysteryRe:MASTER': ('MurderMystery', [('Murder', '殺人鬼陣営になった回数'), ('Crew', '村人陣営になった回数'), ('wingame', '勝利数')]),
}


EMBED_COLOURS = {'MINIGAMER': 0x3498DB, 'unlimited': 0x9966CC, 'MurderMysteryRe:MASTER': 0xE2041B}


def classify(entry, registry):
    if entry.get('gameKey'):
        return entry['gameKey']
    world = entry.get('worldName', '').lower()
    board = entry.get('scoreboard', '').lower()
    if 'minigamer_neo' in board or any(
        other.get('worldName') == entry.get('worldName') and 'minigamer_neo' in other.get('scoreboard', '').lower()
        for other in registry
    ):
        return 'mini_neo'
    if 'murdermystery' in world or '人狼' in world or '�l�t' in world:
        return 'MurderMystery'
    if 'unlimited' in world:
        return 'unlimited'
    if 'minigamer' in world:
        return 'mini_neo'
    return None


def find_status(data, name, game):
    game_key, metrics = GAMES[game]
    registry = data.get('custom_player_status:saved_scoreboards', [])
    snapshots = data.get('custom_player_status:snapshots', {})
    scores, updated = {}, []
    for entry in registry:
        if classify(entry, registry) != game_key:
            continue
        board = entry['scoreboard']
        key = next((key for key, _ in metrics if key.lower() == board.lower()), None)
        if key is None:
            continue
        snapshot = snapshots.get(entry['worldName'], {}).get('scoreboards', {}).get(board, {})
        player = snapshot.get('players', {}).get(name.lower())
        if not isinstance(player, dict) or type(player.get('score')) not in (int, float):
            continue
        scores[key] = player['score']
        if snapshot.get('updatedAt'):
            updated.append(snapshot['updatedAt'])
    return scores, min(updated) if updated else 0


def safe(name):
    return ''.join(c for c in name if c not in '\\`*_~|<>[]\r\n')


def render(data, game, name, opponent=None):
    if game not in GAMES:
        return 'ワールドを選択肢から選んでください。'
    names = [name.strip()] + ([opponent.strip()] if opponent is not None else [])
    if any(not value or len(value) > 64 for value in names):
        return 'Minecraft名を1～64文字で入力してください。'
    if len(names) == 2 and names[0].lower() == names[1].lower():
        return '自身とは異なる対戦相手を指定してください。'
    statuses = [find_status(data, value, game) for value in names]
    missing = [safe(value) for value, (scores, _) in zip(names, statuses) if not scores]
    if missing:
        return f'{"、".join(missing)} の {game} の保存データがありません。'
    lines = [f'**{" VS ".join(safe(value) for value in names)}**', f'ワールド: {game}']
    if len(names) == 2:
        lines.append('表示順: 自身 / 相手 （差 = 自身 − 相手）')
    for key, label in GAMES[game][1]:
        values = [scores.get(key) for scores, _ in statuses]
        line = f'**{label}**: ' + ' / '.join(str(v) if v is not None else '未記録' for v in values)
        if len(values) == 2:
            delta = values[0] - values[1] if all(v is not None for v in values) else None
            line += f' （差: {delta:+g}）' if delta is not None else ' （差: 比較不可）'
        lines.append(line)
    lines.append('最終取得時点の成績です。記録更新時刻をご確認ください。')
    for value, (_, timestamp) in zip(names, statuses):
        if timestamp:
            lines.append(f'{safe(value)} の記録更新: <t:{int(timestamp / 1000)}:R>')
    return '\n'.join(lines)


async def respond(interaction, game, name, opponent=None):
    import discord
    await interaction.response.defer()
    try:
        # Read each time so replacing the snapshot does not require a restart.
        data = await asyncio.to_thread(lambda: json.loads(DATA_FILE.read_text(encoding='utf-8-sig')))
        content = render(data, game, name, opponent)
    except FileNotFoundError:
        content = '保存データがありません。'
    except Exception:
        log.exception('Failed to read player snapshots')
        content = '成績を読み込めませんでした。しばらくしてから再実行してください。'
    if opponent is None:
        embed = discord.Embed(title='Minecraft プレイヤーステータス', description=content, colour=EMBED_COLOURS.get(game, 0x3498DB))
        await interaction.edit_original_response(content=None, embed=embed, allowed_mentions=discord.AllowedMentions.none())
    else:
        await interaction.edit_original_response(content=content, allowed_mentions=discord.AllowedMentions.none())
