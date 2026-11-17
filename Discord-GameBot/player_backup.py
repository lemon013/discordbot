"""Private Discord transport for complete, consistent player database snapshots."""
import asyncio
from contextlib import ExitStack, closing
import hashlib
import io
import json
import logging
import os
from pathlib import Path
import sqlite3
import tempfile
from urllib.parse import urlparse
import zipfile

import aiohttp
from storage import database_path, snapshot

log = logging.getLogger(__name__)
CHANNEL = '1571130824697323582'
FILENAME = 'gamebot-player-data.zip'
PREFIX = 'GameBot player database backup v1'
MAX_UPLOAD = 9_000_000
MAX_EXPANDED = 64_000_000
STATE = None


def enabled():
    return os.getenv('CLOUD_MODE', '').lower() in ('1', 'true', 'yes')


def paths():
    return {'levels.sqlite3': database_path('level'), 'casino.sqlite3': database_path('casino')}


def pack():
    targets = paths()
    with tempfile.TemporaryDirectory() as temp, ExitStack() as stack:
        # Same lock order as shop/party transactions.
        for name in ('casino.sqlite3', 'levels.sqlite3'):
            db = stack.enter_context(closing(sqlite3.connect(targets[name].as_uri() + '?mode=rw', uri=True, timeout=10)))
            db.execute('BEGIN IMMEDIATE')
        files = {}
        for name, path in targets.items():
            dest = Path(temp) / name
            snapshot(path, dest)
            files[name] = dest.read_bytes()
    manifest = dict(version=1, sha256={n: hashlib.sha256(b).hexdigest() for n, b in files.items()})
    out = io.BytesIO()
    with zipfile.ZipFile(out, 'w', zipfile.ZIP_DEFLATED) as archive:
        archive.writestr('manifest.json', json.dumps(manifest))
        for name, raw in files.items():
            archive.writestr(name, raw)
    raw = out.getvalue()
    if len(raw) > MAX_UPLOAD:
        raise RuntimeError('Player backup exceeds Discord upload limit; configure persistent storage.')
    return raw


def validate(raw):
    if len(raw) > MAX_UPLOAD:
        raise RuntimeError('Player backup is too large')
    with zipfile.ZipFile(io.BytesIO(raw)) as archive:
        infos = archive.infolist()
        if len(infos) != 3 or {x.filename for x in infos} != {'manifest.json', 'levels.sqlite3', 'casino.sqlite3'}:
            raise RuntimeError('Invalid player backup file list')
        if sum(x.file_size for x in infos) > MAX_EXPANDED:
            raise RuntimeError('Expanded player backup is too large')
        manifest = json.loads(archive.read('manifest.json'))
        if manifest['version'] != 1:
            raise RuntimeError('Unsupported player backup version')
        files = {n: archive.read(n) for n in ('levels.sqlite3', 'casino.sqlite3')}
    with tempfile.TemporaryDirectory() as temp:
        for name, data in files.items():
            if hashlib.sha256(data).hexdigest() != manifest['sha256'][name]:
                raise RuntimeError('Player backup checksum mismatch')
            dest = Path(temp) / name
            dest.write_bytes(data)
            with closing(sqlite3.connect(dest)) as db:
                if db.execute('PRAGMA quick_check').fetchone()[0] != 'ok':
                    raise RuntimeError('Player backup is corrupt')
                required = {'level_users', 'level_config'} if name == 'levels.sqlite3' else {'casino_wallets', 'casino_transactions'}
                tables = {r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
                if not required <= tables:
                    raise RuntimeError('Player backup is missing required tables')
    return files


def revision(message):
    return f"{message['id']}:{message.get('edited_timestamp') or message['timestamp']}"


def marker():
    return paths()['levels.sqlite3'].with_name('discord-player-backup.json')


def save_revision(value):
    dest = marker()
    temporary = dest.with_suffix('.tmp')
    temporary.write_text(json.dumps({'revision': value}), encoding='utf-8')
    temporary.replace(dest)


def install(raw, remote_revision):
    files = validate(raw)  # Validate BOTH before modifying either database.
    targets = paths()
    try:
        local_revision = json.loads(marker().read_text(encoding='utf-8'))['revision']
    except (OSError, ValueError, KeyError):
        local_revision = None
    if local_revision == remote_revision and all(p.exists() for p in targets.values()):
        try:
            for path in targets.values():
                with closing(sqlite3.connect(path)) as db:
                    if db.execute('PRAGMA quick_check').fetchone()[0] != 'ok':
                        raise RuntimeError('Local database is corrupt')
        except (sqlite3.Error, RuntimeError):
            log.warning('Local player DB validation failed; using Discord backup')
        else:
            log.info('Keeping local player databases newer than the acknowledged Discord backup')
            return
    # Retry the entire pair after an interrupted installation.
    marker().unlink(missing_ok=True)
    for name, target in targets.items():
        if target.exists():
            snapshot(target, target.with_name(target.name + '.before-discord-restore'))
        temporary = target.with_name(target.name + '.download')
        temporary.write_bytes(files[name])
        # No DB connections have been opened by the bot yet.
        for suffix in ('-wal', '-shm', '-journal'):
            Path(str(target) + suffix).unlink(missing_ok=True)
        temporary.replace(target)
        target.with_name(target.name + '.initialized').touch()
    save_revision(remote_revision)
    log.info('Loaded complete player databases from Discord backup')


class Transport:
    def __init__(self, token, bot_id=None, message_id=None):
        self.token, self.bot_id, self.message_id = token, bot_id, message_id
        self.channel = os.getenv('PLAYER_BACKUP_CHANNEL_ID') or CHANNEL

    async def request(self, session, method, route, **kwargs):
        factory = kwargs.pop('data_factory', None)
        for attempt in range(4):
            if factory:
                kwargs['data'] = factory()
            async with session.request(method, 'https://discord.com/api/v10' + route,
                    headers={'Authorization': 'Bot ' + self.token}, **kwargs) as response:
                if response.status == 429:
                    delay = float((await response.json()).get('retry_after', 1))
                    if delay > 30:
                        raise RuntimeError('Discord backup rate limit; startup stopped')
                    await asyncio.sleep(max(0.1, delay))
                    continue
                if response.status >= 400:
                    raise RuntimeError(f'Discord player backup HTTP {response.status} ({method})')
                return await response.json()
        raise RuntimeError('Discord player backup rate limit exhausted')

    async def latest(self, session):
        before = None
        while True:
            query = '?limit=100' + (f'&before={before}' if before else '')
            messages = await self.request(session, 'GET', f'/channels/{self.channel}/messages{query}')
            for message in messages:
                if str(message['author']['id']) != str(self.bot_id) or not message.get('content', '').startswith(PREFIX):
                    continue
                attachments = [a for a in message['attachments'] if a['filename'] == FILENAME]
                if len(attachments) != 1:
                    raise RuntimeError('Latest player backup has a missing attachment; refusing reset')
                self.message_id = message['id']
                return message, attachments[0]
            if len(messages) < 100:
                return None
            before = messages[-1]['id']

    async def check_permissions(self, session):
        channel = await self.request(session, 'GET', f'/channels/{self.channel}')
        if channel.get('type') not in (0, 5) or not channel.get('guild_id'):
            raise RuntimeError('Player backup requires a server text channel')
        guild = str(channel['guild_id'])
        roles = await self.request(session, 'GET', f'/guilds/{guild}/roles')
        member = await self.request(session, 'GET', f'/guilds/{guild}/members/{self.bot_id}')
        owned_roles = {guild, *(str(x) for x in member['roles'])}
        permissions = 0
        for role in roles:
            if str(role['id']) in owned_roles:
                permissions |= int(role['permissions'])
        if permissions & 8:  # Administrator bypasses channel overwrites.
            return
        overwrites = channel.get('permission_overwrites', [])
        for entry in overwrites:
            if str(entry['id']) == guild and int(entry['type']) == 0:
                permissions = permissions & ~int(entry['deny']) | int(entry['allow'])
        deny = allow = 0
        for entry in overwrites:
            if int(entry['type']) == 0 and str(entry['id']) != guild and str(entry['id']) in owned_roles:
                deny |= int(entry['deny'])
                allow |= int(entry['allow'])
        permissions = permissions & ~deny | allow
        for entry in overwrites:
            if int(entry['type']) == 1 and str(entry['id']) == str(self.bot_id):
                permissions = permissions & ~int(entry['deny']) | int(entry['allow'])
        required = 1024 | 2048 | 32768 | 65536  # View, send, attach, read history.
        if permissions & required != required:
            raise RuntimeError('Player backup Bot needs View Channel, Send Messages, Attach Files and Read Message History')

    async def download(self, session, attachment):
        url = attachment['url']
        if urlparse(url).scheme != 'https' or urlparse(url).hostname not in ('cdn.discordapp.com', 'media.discordapp.net'):
            raise RuntimeError('Invalid backup attachment host')
        if attachment['size'] > MAX_UPLOAD:
            raise RuntimeError('Backup attachment is too large')
        async with session.get(url, allow_redirects=False) as response:
            if response.status != 200:
                raise RuntimeError('Cannot download player backup; refusing reset')
            data = bytearray()
            async for part in response.content.iter_chunked(65536):
                data.extend(part)
                if len(data) > MAX_UPLOAD:
                    raise RuntimeError('Backup download exceeds limit')
            return bytes(data)

    async def upload(self, session, raw):
        def form():
            data = aiohttp.FormData()
            data.add_field('payload_json', json.dumps({'content': PREFIX,
                'allowed_mentions': {'parse': []}, 'attachments': [{'id': 0, 'filename': FILENAME}]}))
            data.add_field('files[0]', raw, filename=FILENAME, content_type='application/zip')
            return data
        route = f'/channels/{self.channel}/messages'
        method = 'POST'
        if self.message_id:
            route += f'/{self.message_id}'
            method = 'PATCH'
        message = await self.request(session, method, route, data_factory=form)
        self.message_id = message['id']
        save_revision(revision(message))

async def bootstrap():
    """Called before importing bot.py, which creates the database services."""
    global STATE
    transport = Transport(os.environ.get('DISCORD_BOT_TOKEN', ''))
    if not transport.token:
        raise RuntimeError('DISCORD_BOT_TOKEN is required for player backup')
    async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=60)) as session:
        me = await transport.request(session, 'GET', '/users/@me')
        transport.bot_id = me['id']
        await transport.check_permissions(session)
        found = await transport.latest(session)
        os.environ['PLAYER_BACKUP_ACTIVE'] = '1'
        if found:
            message, attachment = found
            raw = await transport.download(session, attachment)
            await asyncio.to_thread(install, raw, revision(message))
        elif marker().exists():
            raise RuntimeError('Discord player backup disappeared; refusing to replace it with empty data')
        else:
            log.warning('First Discord player backup: no previous backup exists; current databases will be saved on ready')
    STATE = transport


def register(bot):
    if not enabled():
        return
    from discord.ext import tasks
    lock = asyncio.Lock()

    @tasks.loop(seconds=60)
    async def tick():
        async with lock:
            try:
                if STATE is None:
                    raise RuntimeError('Cloud startup did not initialize Discord player backup')
                raw = await asyncio.to_thread(pack)
                async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=60)) as session:
                    await STATE.upload(session, raw)
                log.info('Player data saved to Discord channel %s', STATE.channel)
            except Exception:
                log.exception('PLAYER BACKUP FAILED: latest changes are only on local disk')

    async def ready():
        if not tick.is_running():
            tick.start()

    bot.add_listener(ready, 'on_ready')
    bot.discord_player_backups = tick
