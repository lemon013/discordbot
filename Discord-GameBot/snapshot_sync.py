"""Authenticated snapshots transported through a private Discord channel."""
import asyncio
import hashlib
import hmac
import json
import logging
import math
import os
import time
from pathlib import Path

log = logging.getLogger(__name__)
REGISTRY = 'custom_player_status:saved_scoreboards'
SNAPSHOTS = 'custom_player_status:snapshots'
TOMBSTONES = 'custom_player_status:deleted_scoreboards'
MAX_BYTES = 7_000_000
BACKUP_LIMIT = 20


def encode_snapshot(data, secret, timestamp=None):
    payload = json.dumps({'version': 1, 'sent_at': timestamp or time.time_ns(),
                          'data': {REGISTRY: data.get(REGISTRY, []), SNAPSHOTS: data.get(SNAPSHOTS, {}),
                                   TOMBSTONES: data.get(TOMBSTONES, {})}},
                         ensure_ascii=False, separators=(',', ':'), allow_nan=False)
    signature = hmac.new(secret.encode(), payload.encode(), hashlib.sha256).hexdigest()
    result = json.dumps({'payload': payload, 'signature': signature}, ensure_ascii=False).encode()
    if len(result) > MAX_BYTES:
        raise ValueError('Snapshot exceeds transport limit')
    return result


def decode_snapshot(raw, secret, previous=0):
    if len(raw) > MAX_BYTES:
        raise ValueError('Snapshot too large')
    envelope = json.loads(raw)
    payload = envelope['payload']
    expected = hmac.new(secret.encode(), payload.encode(), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(expected, envelope['signature']):
        raise ValueError('Invalid signature')
    item = json.loads(payload)
    stamp = item['sent_at']
    if item.get('version') != 1 or type(stamp) is not int:
        raise ValueError('Invalid version or timestamp')
    if stamp <= previous:
        raise ValueError('Snapshot already received or older than saved data')
    if stamp > time.time_ns() + 300_000_000_000:
        raise ValueError('PC clock is more than 5 minutes ahead of cloud clock')
    data = item['data']
    registry, snapshots = data[REGISTRY], data[SNAPSHOTS]
    tombstones = data.get(TOMBSTONES, {})
    if not isinstance(registry, list) or not isinstance(snapshots, dict) or not isinstance(tombstones, dict):
        raise ValueError('Invalid data shape')
    for entry in registry:
        if not isinstance(entry, dict) or not all(isinstance(entry.get(k), str) for k in ('worldName', 'scoreboard')):
            raise ValueError('Invalid registry')
    for world in snapshots.values():
        if not isinstance(world, dict) or not isinstance(world.get('scoreboards'), dict):
            raise ValueError('Invalid world')
        for board in world['scoreboards'].values():
            if not isinstance(board, dict) or not isinstance(board.get('players'), dict):
                raise ValueError('Invalid scoreboard')
            updated = board.get('updatedAt', 0)
            if type(updated) not in (int, float) or not math.isfinite(updated) or updated < 0:
                raise ValueError('Invalid update time')
            for player in board['players'].values():
                if not isinstance(player, dict) or type(player.get('score')) not in (int, float) or not math.isfinite(player['score']):
                    raise ValueError('Invalid score')
    for key, deleted_at in tombstones.items():
        if not isinstance(key, str) or type(deleted_at) is not int or deleted_at < 0:
            raise ValueError('Invalid scoreboard deletion record')
    return {REGISTRY: registry, SNAPSHOTS: snapshots, TOMBSTONES: tombstones, 'cloud_sync:sent_at': stamp}


def merge_score_data(existing, incoming):
    """Merge incremental player records and apply explicit scoreboard deletions."""
    registry_by_key = {}
    for entry in (existing.get(REGISTRY, []) or []) + (incoming.get(REGISTRY, []) or []):
        if isinstance(entry, dict) and isinstance(entry.get('worldName'), str) and isinstance(entry.get('scoreboard'), str):
            key = (entry['worldName'], entry['scoreboard'])
            old = registry_by_key.get(key)
            if old is None or (entry.get('createdAt') or 0) >= (old.get('createdAt') or 0):
                registry_by_key[key] = dict(entry)
    tombstones = dict(existing.get(TOMBSTONES, {}) or {})
    for key, stamp in (incoming.get(TOMBSTONES, {}) or {}).items():
        if type(stamp) is int:
            tombstones[key] = max(tombstones.get(key, 0), stamp)
    def tombstone_key(world, board):
        return f'{world}\u001f{board}'
    for key, entry in list(registry_by_key.items()):
        deleted_at = tombstones.get(tombstone_key(*key), 0)
        if deleted_at and (entry.get('createdAt') or 0) <= deleted_at:
            del registry_by_key[key]
    snapshots = json.loads(json.dumps(existing.get(SNAPSHOTS, {}) or {}))
    for (world, board), entry in registry_by_key.items():
        deleted_at = tombstones.get(tombstone_key(world, board), 0)
        registered_at = entry.get('createdAt') or 0
        if deleted_at and registered_at > deleted_at:
            previous_board = snapshots.get(world, {}).get('scoreboards', {}).get(board)
            if previous_board and (previous_board.get('registeredAt') or 0) < registered_at:
                snapshots[world]['scoreboards'].pop(board, None)
    for world, world_data in (incoming.get(SNAPSHOTS, {}) or {}).items():
        if not isinstance(world_data, dict):
            continue
        target_world = snapshots.setdefault(world, {'scoreboards': {}})
        target_world.setdefault('scoreboards', {})
        for board, board_data in (world_data.get('scoreboards', {}) or {}).items():
            if (world, board) not in registry_by_key:
                continue
            previous_board = target_world['scoreboards'].get(board, {})
            players = {key: value for key, value in previous_board.get('players', {}).items()
                       if isinstance(value, dict) and isinstance(value.get('name'), str)
                       and not value['name'].strip().lower().startswith('commands.scoreboard.players.')}
            for key, value in board_data.get('players', {}).items():
                if (isinstance(value, dict) and isinstance(value.get('name'), str)
                        and not value['name'].strip().lower().startswith('commands.scoreboard.players.')):
                    players[key] = value
            created_at = registry_by_key[(world, board)].get('createdAt') or 0
            new_registration = bool(tombstones.get(tombstone_key(world, board), 0)
                                    and created_at > tombstones[tombstone_key(world, board)]
                                    and (previous_board.get('registeredAt') or 0) < created_at)
            base = {} if new_registration else previous_board
            if new_registration:
                players = {key: value for key, value in board_data.get('players', {}).items()
                           if isinstance(value, dict) and isinstance(value.get('name'), str)
                           and not value['name'].strip().lower().startswith('commands.scoreboard.players.')}
            target_board = {**base, **board_data, 'players': players}
            if created_at:
                target_board['registeredAt'] = created_at
            target_world['scoreboards'][board] = target_board
        if 'updatedAt' in world_data or 'updatedAt' in target_world:
            target_world['updatedAt'] = max(target_world.get('updatedAt', 0), world_data.get('updatedAt', 0))
    for world, world_data in list(snapshots.items()):
        boards = world_data.get('scoreboards', {})
        for board in list(boards):
            if (world, board) not in registry_by_key:
                del boards[board]
        if not boards:
            del snapshots[world]
    return {REGISTRY: list(registry_by_key.values()), SNAPSHOTS: snapshots,
            TOMBSTONES: tombstones, 'cloud_sync:sent_at': incoming.get('cloud_sync:sent_at', 0)}


def save_generation(path, limit=BACKUP_LIMIT, score_only=False):
    """Keep a bounded, timestamped copy before replacing a score file."""
    path = Path(path)
    if not path.exists():
        return None
    backup_dir = path.parent / (path.stem + '-backups')
    backup_dir.mkdir(parents=True, exist_ok=True)
    generations = sorted(backup_dir.glob(f'{path.stem}-*{path.suffix}'), key=lambda item: item.stat().st_mtime_ns)
    if score_only:
        source_data = json.loads(path.read_text(encoding='utf-8-sig'))
        content = json.dumps({key: source_data[key] for key in (REGISTRY, SNAPSHOTS, TOMBSTONES)
                              if key in source_data}, ensure_ascii=False, allow_nan=False, indent=2).encode('utf-8')
    else:
        content = path.read_bytes()
    if generations and content == generations[-1].read_bytes():
        return generations[-1]
    stamp = time.strftime('%Y%m%d-%H%M%S', time.gmtime()) + f'-{time.time_ns() % 1_000_000_000:09d}'
    destination = backup_dir / f'{path.stem}-{stamp}{path.suffix}'
    destination.write_bytes(content)
    generations = sorted(backup_dir.glob(f'{path.stem}-*{path.suffix}'), key=lambda item: item.stat().st_mtime_ns)
    for old in generations[:-limit]:
        old.unlink()
    return destination


def atomic_write(path, data):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(path.name + '.sync.tmp')
    try:
        temp.write_text(json.dumps(data, ensure_ascii=False, allow_nan=False), encoding='utf-8')
        save_generation(path)
        os.replace(temp, path)
    finally:
        if temp.exists():
            temp.unlink()


async def receive_latest(bot, channel_id, sender_id, secret, path):
    path = Path(path)
    channel = bot.get_channel(channel_id) or await bot.fetch_channel(channel_id)
    try:
        previous = json.loads(path.read_text(encoding='utf-8-sig')).get('cloud_sync:sent_at', 0)
    except FileNotFoundError:
        previous = 0
    except json.JSONDecodeError:
        return 'Existing Minecraft score file is invalid; preserving it'
    result = 'No minecraft-status.json found in the last 50 messages; check MC_SYNC_CHANNEL_ID and PC upload'
    async for message in channel.history(limit=50):
        attachments = [a for a in message.attachments if a.filename == 'minecraft-status.json']
        if not attachments:
            continue
        if message.author.id != sender_id:
            if result.startswith('No minecraft-status.json'):
                result = (f'Snapshot sender ID {message.author.id} differs from MC_SYNC_SENDER_ID={sender_id}; '
                          'set MC_SYNC_SENDER_ID to the PC Bot user ID')
            continue
        # The signed attachment and configured sender authenticate the snapshot.
        # Message content can be empty when Discord withholds Message Content.
        for attachment in attachments:
            if attachment.size > MAX_BYTES:
                result = 'Snapshot exceeds transport limit'
                continue
            try:
                data = decode_snapshot(await attachment.read(), secret, previous)
            except (ValueError, KeyError, TypeError) as error:
                if str(error) == 'Snapshot already received or older than saved data':
                    return result if result.startswith('Snapshot rejected:') else 'up-to-date'
                reason = ('Invalid signature; check that MC_SYNC_SECRET is identical on PC and MWS'
                          if str(error) == 'Invalid signature' else str(error))
                result = f'Snapshot rejected: {reason}'
                continue
            try:
                existing = json.loads(path.read_text(encoding='utf-8-sig'))
            except FileNotFoundError:
                existing = {}
            except json.JSONDecodeError:
                return 'Existing Minecraft score file is invalid; preserving it'
            merged = merge_score_data(existing, data)
            await asyncio.to_thread(atomic_write, path, merged)
            log.info('Minecraft scores synchronized from PC: worlds=%s, file=%s', len(data[SNAPSHOTS]), path)
            return 'synchronized'
    return result


async def sync_loop(bot):
    from cloud_status import DATA_FILE
    channel = os.getenv('MC_SYNC_CHANNEL_ID', '')
    sender = os.getenv('MC_SYNC_SENDER_ID', '')
    secret = os.getenv('MC_SYNC_SECRET', '')
    if not any((channel, sender, secret)):
        log.info('Minecraft score sync disabled: not configured')
        return
    if not channel.isdecimal() or not sender.isdecimal() or len(secret) < 32:
        log.error('Score sync requires channel ID, PC Bot ID and a shared secret of at least 32 characters')
        return
    log.info('Minecraft score sync started: channel=%s, PC Bot=%s, file=%s', channel, sender, DATA_FILE)
    last_result = None
    while not bot.is_closed():
        try:
            result = await receive_latest(bot, int(channel), int(sender), secret, DATA_FILE)
            if result not in ('synchronized', 'up-to-date') and result != last_result:
                log.warning('Minecraft score sync waiting: %s', result)
            last_result = result
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception('Score sync failed; retrying in 30 seconds')
        await asyncio.sleep(30)

