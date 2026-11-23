from contextlib import closing
import time
from shop import ShopStore
from leveling import DEFAULTS, level_for


class PartyRewards(ShopStore):
    def grant(self, guild_id, user_id, game_id, won):
        event = f'party:{game_id}:{user_id}'
        with closing(self.casino.connect()) as db, db:
            alias = 'main'
            if self.casino.path.resolve() != self.levels.path.resolve():
                db.execute('ATTACH DATABASE ? AS levels', (str(self.levels.path.resolve()),))
                alias = 'levels'
                for name in ('main', 'levels'):
                    if db.execute(f'PRAGMA {name}.journal_mode').fetchone()[0] not in ('delete', 'truncate', 'persist'):
                        raise ValueError('Party rewards require rollback journals')
            db.execute('BEGIN IMMEDIATE')
            from ticket_store import add_in
            add_in(db, guild_id, user_id, 'ticket:' + event, 3, 'party')
            previous = self.casino.replay(db, event, guild_id, user_id, 'party_reward')
            if previous is not None:
                return previous
            gems, xp = (100, 10) if won else (50, 5)
            if game_id.startswith('othello:'):
                gems, xp = gems * 3, xp * 3
            from xp_role_bonus import apply_bonus
            xp = apply_bonus(db, alias, guild_id, user_id, xp)
            cfg_row = db.execute(f'SELECT * FROM {alias}.level_config WHERE guild_id=?', (guild_id,)).fetchone()
            cfg = {**DEFAULTS, **(dict(cfg_row) if cfg_row else {})}
            db.execute(f'INSERT OR IGNORE INTO {alias}.level_users(guild_id,user_id) VALUES(?,?)', (guild_id, user_id))
            before = db.execute(f'SELECT xp FROM {alias}.level_users WHERE guild_id=? AND user_id=?', (guild_id, user_id)).fetchone()[0]
            db.execute(f'UPDATE {alias}.level_users SET xp=xp+? WHERE guild_id=? AND user_id=?', (xp, guild_id, user_id))
            wallet = self.casino.wallet(db, guild_id, user_id)
            result = dict(balance=wallet['balance'] + gems, xp=xp, gems=gems,
                          user_id=user_id, before=level_for(before,cfg['base_xp'],cfg['step_xp']),
                          after=level_for(before+xp,cfg['base_xp'],cfg['step_xp']))
            return self.casino.finish(db,event,guild_id,user_id,'party_reward',wallet['balance'],result,time.time())


async def reward_party(bot, guild, channel, game_id, outcomes):
    import asyncio
    store = bot.party_rewards
    for user_id, won in outcomes.items():
        from xp_role_bonus import remember_roles
        await remember_roles(bot, guild, user_id)
        result = await asyncio.to_thread(store.grant,guild.id,user_id,game_id,won)
        cfg = await asyncio.to_thread(store.levels.config,guild.id)
        await bot.leveling_service.notify(guild,[result],cfg,channel)
