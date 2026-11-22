"""Replay-safe solo XP, level and jewel rewards in one SQLite transaction."""
import asyncio
from contextlib import closing
import logging
import time

from casino import CasinoStore
from leveling import LevelStore, MAX_LEVEL, level_for, threshold

log = logging.getLogger(__name__)


def attach_levels(db, casino, levels):
    alias = 'main'
    if casino.path.resolve() != levels.path.resolve():
        db.execute('ATTACH DATABASE ? AS levels', (str(levels.path.resolve()),))
        alias = 'levels'
        if any(db.execute(f'PRAGMA {name}.journal_mode').fetchone()[0] not in ('delete', 'truncate', 'persist')
               for name in ('main', 'levels')):
            raise ValueError('Solo rewards require rollback journals')
    return alias


def grant_in(db, casino, alias, guild_id, user_id, event_id, xp, levels=0, gems=0):
    if any(type(value) is not int or value < 0 for value in (xp, levels, gems)):
        raise ValueError('Invalid solo reward')
    previous = casino.replay(db, event_id, guild_id, user_id, 'solo_reward')
    if previous is not None:
        if (previous['base_xp'], previous['level_bonus'], previous['gems']) != (xp, levels, gems):
            raise ValueError('Solo reward does not match recorded result')
        return {**previous, 'new': False}
    cfg_row = db.execute(f'SELECT * FROM {alias}.level_config WHERE guild_id=?', (guild_id,)).fetchone()
    from leveling import DEFAULTS
    cfg = {**DEFAULTS, **(dict(cfg_row) if cfg_row else {})}
    db.execute(f'INSERT OR IGNORE INTO {alias}.level_users(guild_id,user_id) VALUES(?,?)', (guild_id, user_id))
    before_xp = db.execute(f'SELECT xp FROM {alias}.level_users WHERE guild_id=? AND user_id=?', (guild_id, user_id)).fetchone()[0]
    base, step = cfg['base_xp'], cfg['step_xp']
    intermediate = level_for(before_xp + xp, base, step)
    target = min(MAX_LEVEL, intermediate + levels)
    # Keep progress within the level; do not discard accumulated XP at the cap.
    bonus_xp = threshold(target, base, step) - threshold(intermediate, base, step)
    total_xp = before_xp + xp + bonus_xp
    db.execute(f'UPDATE {alias}.level_users SET xp=? WHERE guild_id=? AND user_id=?', (total_xp, guild_id, user_id))
    wallet = casino.wallet(db, guild_id, user_id)
    result = dict(user_id=user_id, balance=wallet['balance'] + gems, base_xp=xp, level_bonus=levels,
                  gems=gems, xp=xp + bonus_xp, total_xp=total_xp, levels_gained=target - intermediate,
                  before=level_for(before_xp, base, step), after=level_for(total_xp, base, step))
    casino.finish(db, event_id, guild_id, user_id, 'solo_reward', wallet['balance'], result, time.time())
    return {**result, 'new': True}


class SoloRewards:
    def __init__(self, casino=None, levels=None):
        self.casino = casino or CasinoStore()
        self.levels = levels or LevelStore()

    def grant(self, guild_id, user_id, event_id, xp, levels=0, gems=0):
        with closing(self.casino.connect()) as db, db:
            alias = attach_levels(db, self.casino, self.levels)
            db.execute('BEGIN IMMEDIATE')
            return grant_in(db, self.casino, alias, guild_id, user_id, event_id, xp, levels, gems)


async def notify_solo(bot, guild, channel, result):
    if not result or not result.get('new') or guild is None:
        return
    try:
        cfg = await asyncio.to_thread(bot.leveling_service.store.config, guild.id)
        await bot.leveling_service.notify(guild, [result], cfg, channel)
    except Exception as exc:
        # The award has committed. A Discord notification failure must not undo it.
        log.warning('Solo reward saved; notification failed (error_type=%s)', type(exc).__name__)


async def reward_solo(bot, guild, channel, user_id, event_id, xp, levels=0, gems=0):
    result = await asyncio.to_thread(bot.solo_rewards.grant, guild.id, user_id, 'solo:' + event_id, xp, levels, gems)
    await notify_solo(bot, guild, channel, result)
    return result


def step_xp(correct_count):
    return max(0, correct_count - 1) * 3
