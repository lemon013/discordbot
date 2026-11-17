"""Keep player databases outside replaceable source checkouts."""
from contextlib import closing, ExitStack
import logging
import os
from pathlib import Path
import sqlite3
import tempfile
from datetime import datetime, timezone

log = logging.getLogger(__name__)
SOURCE = Path(__file__).resolve().parent


def absolute_path(value):
    path = Path(value).expanduser()
    return (SOURCE / path).resolve() if not path.is_absolute() else path.resolve()


def data_directory():
    return absolute_path(os.getenv('BOT_DATA_DIR') or Path.home() / '.discord-gamebot')


def snapshot(source, destination):
    """Use SQLite backup so a live journal/WAL is included in the copy."""
    source, destination = Path(source), Path(destination)
    destination.parent.mkdir(parents=True, exist_ok=True)
    handle, temp = tempfile.mkstemp(prefix='.db-backup-', dir=destination.parent)
    os.close(handle)
    try:
        with closing(sqlite3.connect(source.as_uri() + '?mode=ro', uri=True)) as src:
            if src.execute('PRAGMA quick_check').fetchone()[0] != 'ok':
                raise RuntimeError(f'Database integrity check failed: {source}')
            with closing(sqlite3.connect(temp)) as dst:
                src.backup(dst)
                if dst.execute('PRAGMA quick_check').fetchone()[0] != 'ok':
                    raise RuntimeError('Backup integrity check failed')
        os.replace(temp, destination)
    finally:
        Path(temp).unlink(missing_ok=True)


def database_path(kind):
    name = 'levels.sqlite3' if kind == 'level' else 'casino.sqlite3'
    explicit = os.getenv('LEVEL_DB_PATH' if kind == 'level' else 'CASINO_DB_PATH')
    cloud = os.getenv('CLOUD_MODE', '').lower() in ('1', 'true', 'yes')
    if cloud and not (explicit or os.getenv('BOT_DATA_DIR') or
                      (kind == 'casino' and os.getenv('LEVEL_DB_PATH'))):
        raise RuntimeError(
            'MWS: persistent player storage is not configured. Set BOT_DATA_DIR '
            'to the actual persistent volume supplied by your host. '
            'Refusing to create an empty database in temporary home storage.')
    if explicit:
        target = absolute_path(explicit)
    elif kind == 'casino' and os.getenv('LEVEL_DB_PATH'):
        target = absolute_path(os.environ['LEVEL_DB_PATH']).with_name(name)
    else:
        target = data_directory() / name
    if cloud and target.is_relative_to(SOURCE.parent.resolve()):
        raise RuntimeError('MWS: player database must be outside the replaceable source checkout; configure a persistent volume.')
    target.parent.mkdir(parents=True, exist_ok=True)
    marker = target.with_name(target.name + '.initialized')
    legacy = SOURCE / name
    if not target.exists():
        if marker.exists():
            raise RuntimeError(f'Player database is missing: {target}. Restore a backup before starting; refusing to reset balances/XP.')
        if legacy.exists() and legacy.resolve() != target:
            snapshot(legacy.resolve(), target)
            log.warning('Migrated player database from %s to %s (original retained)', legacy, target)
    return target


def mark_initialized(path):
    path = Path(path).resolve()
    path.with_name(path.name + '.initialized').touch(exist_ok=True)
    log.info('Player database: %s', path)


def backup_databases(paths):
    stamp = datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%fZ')
    # Match the shop's casino -> levels lock order. Keep both snapshots at
    # the same point in time so purchases and party rewards stay consistent.
    paths = sorted(set(Path(p).resolve() for p in paths), key=lambda p: (p.name != 'casino.sqlite3', str(p)))
    with ExitStack() as stack:
        for path in paths:
            db = stack.enter_context(closing(sqlite3.connect(path.as_uri() + '?mode=rw', uri=True, timeout=10)))
            db.execute('BEGIN IMMEDIATE')
        for path in paths:
            snapshot(path, path.parent / 'backups' / f'{path.stem}-{stamp}.sqlite3')
    # Keep the latest 7 days of hourly backups; never remove the originals.
    for path in paths:
        backups = sorted((path.parent / 'backups').glob(f'{path.stem}-*.sqlite3'), reverse=True)
        for old in backups[168:]:
            old.unlink()


def register_backups(bot):
    """Back up at startup and hourly, without blocking Discord's event loop."""
    import asyncio
    from discord.ext import tasks

    @tasks.loop(hours=1)
    async def tick():
        try:
            await asyncio.to_thread(backup_databases,
                [database_path('level'), database_path('casino')])
        except Exception:
            log.exception('Player database backup failed')

    async def ready():
        if not tick.is_running():
            tick.start()

    bot.add_listener(ready, 'on_ready')
    bot.player_database_backups = tick
