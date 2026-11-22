"""Guild-local, transactionally recorded raffle entries in casino.sqlite3."""
import asyncio
from contextlib import closing
import json
import logging
import secrets
import time

MAX_TICKETS = 10 ** 12
log = logging.getLogger(__name__)


def initialize(db):
    db.executescript('''
        CREATE TABLE IF NOT EXISTS ticket_wallets (
            guild_id INTEGER NOT NULL,user_id INTEGER NOT NULL,
            count INTEGER NOT NULL CHECK(count>=0),PRIMARY KEY(guild_id,user_id));
        CREATE TABLE IF NOT EXISTS ticket_events (
            event_id TEXT PRIMARY KEY,guild_id INTEGER NOT NULL,user_id INTEGER NOT NULL,
            amount INTEGER NOT NULL,reason TEXT NOT NULL,created_at REAL NOT NULL);
        CREATE TABLE IF NOT EXISTS ticket_draws (
            event_id TEXT PRIMARY KEY,guild_id INTEGER NOT NULL,result TEXT NOT NULL,created_at REAL NOT NULL);
        CREATE INDEX IF NOT EXISTS ticket_ranking ON ticket_wallets(guild_id,count DESC,user_id);
    ''')


def add_in(db, guild_id, user_id, event_id, amount, reason):
    if type(amount) is not int or not 0 < amount <= MAX_TICKETS:
        raise ValueError('Invalid ticket amount')
    previous = db.execute('SELECT * FROM ticket_events WHERE event_id=?', (event_id,)).fetchone()
    if previous:
        if tuple(previous[key] for key in ('guild_id', 'user_id', 'amount', 'reason')) != (guild_id, user_id, amount, reason):
            raise ValueError('Ticket event mismatch')
        return False
    db.execute('INSERT OR IGNORE INTO ticket_wallets VALUES(?,?,0)', (guild_id, user_id))
    count = db.execute('SELECT count FROM ticket_wallets WHERE guild_id=? AND user_id=?', (guild_id, user_id)).fetchone()[0]
    if count + amount > MAX_TICKETS:
        raise ValueError('Ticket limit exceeded')
    db.execute('UPDATE ticket_wallets SET count=count+? WHERE guild_id=? AND user_id=?', (amount, guild_id, user_id))
    db.execute('INSERT INTO ticket_events VALUES(?,?,?,?,?,?)', (event_id, guild_id, user_id, amount, reason, time.time()))
    return True


class TicketStore:
    def __init__(self, casino=None):
        if casino is None:
            from casino import CasinoStore
            casino = CasinoStore()
        self.casino = casino

    def grant(self, guild_id, user_id, event_id, amount, reason='completion'):
        with closing(self.casino.connect()) as db, db:
            db.execute('BEGIN IMMEDIATE')
            return add_in(db, guild_id, user_id, event_id, amount, reason)

    def ranking(self, guild_id):
        with closing(self.casino.connect()) as db:
            return [dict(row) for row in db.execute('SELECT user_id,count FROM ticket_wallets WHERE guild_id=? AND count>0 ORDER BY count DESC,user_id', (guild_id,))]

    def draw(self, guild_id, event_id):
        with closing(self.casino.connect()) as db, db:
            db.execute('BEGIN IMMEDIATE')
            previous = db.execute('SELECT * FROM ticket_draws WHERE event_id=?', (event_id,)).fetchone()
            if previous:
                if previous['guild_id'] != guild_id:
                    raise ValueError('Draw guild mismatch')
                return json.loads(previous['result'])
            rows = list(db.execute('SELECT user_id,count FROM ticket_wallets WHERE guild_id=? AND count>0 ORDER BY user_id', (guild_id,)))
            total = sum(row['count'] for row in rows)
            if not total:
                raise ValueError('チケットを持っている人がいません。')
            selected = secrets.randbelow(total)
            for row in rows:
                selected -= row['count']
                if selected < 0:
                    result = dict(user_id=row['user_id'], count=row['count'], total=total)
                    break
            db.execute('INSERT INTO ticket_draws VALUES(?,?,?,?)', (event_id, guild_id, json.dumps(result), time.time()))
            return result


async def reward_ticket(bot, guild, user_id, event_id, amount):
    store = getattr(bot, 'ticket_store', None)
    if store is None or guild is None:
        return
    try:
        return await asyncio.to_thread(store.grant, guild.id, user_id, event_id, amount)
    except Exception as exc:
        log.error('Ticket completion reward failed (event_id=%s, user_id=%s, error_type=%s)', event_id, user_id, type(exc).__name__)
        raise


class RewardAudio:
    """Keep attachment interfaces intact while carrying playback reward ownership."""
    def __init__(self, attachment, user_id, event_id, amount):
        self.attachment = attachment
        self.ticket_award = (user_id, event_id, amount)

    def __getattr__(self, name):
        return getattr(self.attachment, name)
