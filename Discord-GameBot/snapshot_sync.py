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
MAX_BYTES = 7_000_000


def encode_snapshot(data, secret, timestamp=None):
    payload = json.dumps({'version': 1, 'sent_at': timestamp or time.time_ns(),
                          'data': {REGISTRY: data.get(REGISTRY, []), SNAPSHOTS: data.get(SNAPSHOTS, {})}},
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
    if item.get('version') != 1 or type(stamp) is not int or stamp <= previous or stamp > time.time_ns() + 300_000_000_000:
        raise ValueError('Invalid version or stale timestamp')
    data = item['data']
    registry, snapshots = data[REGISTRY], data[SNAPSHOTS]
    if not isinstance(registry, list) or not isinstance(snapshots, dict):
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
    return {REGISTRY: registry, SNAPSHOTS: snapshots, 'cloud_sync:sent_at': stamp}


def atomic_write(path, data):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(path.name + '.sync.tmp')
    try:
        temp.write_text(json.dumps(data, ensure_ascii=False, allow_nan=False), encoding='utf-8')
        os.replace(temp, path)
    finally:
        if temp.exists():
            temp.unlink()


async def receive_latest(bot, channel_id, sender_id, secret, path):
    channel = bot.get_channel(channel_id) or await bot.fetch_channel(channel_id)
    try:
        previous = json.loads(path.read_text(encoding='utf-8-sig')).get('cloud_sync:sent_at', 0)
    except (FileNotFoundError, json.JSONDecodeError):
        previous = 0
    async for message in channel.history(limit=50):
        if message.author.id != sender_id or message.content != 'minecraft-status-sync-v1':
            continue
        for attachment in message.attachments:
            if attachment.filename != 'minecraft-status.json' or attachment.size > MAX_BYTES:
                continue
            try:
                data = decode_snapshot(await attachment.read(), secret, previous)
            except (ValueError, KeyError, TypeError):
                continue
            await asyncio.to_thread(atomic_write, path, data)
            log.info('Minecraft scores synchronized from PC')
            return


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
    while not bot.is_closed():
        try:
            await receive_latest(bot, int(channel), int(sender), secret, DATA_FILE)
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception('Score sync failed; retrying in 30 seconds')
        await asyncio.sleep(30)
