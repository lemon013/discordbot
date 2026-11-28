import asyncio
from contextlib import closing
import time
from casino import CasinoError, MAX_BET
from shop import ShopStore
from leveling import DEFAULTS, level_for


class PartyRewards(ShopStore):
    def reserve_wager(self, guild_id, game_id, users, amount):
        if not isinstance(amount, int) or not 1 <= amount <= MAX_BET:
            raise CasinoError('賭け金は1〜10,000ジュエルで指定してください。')
        now = time.time()
        with closing(self.casino.connect()) as db, db:
            db.execute('BEGIN IMMEDIATE')
            for uid in users:
                balance = self.casino.wallet(db, guild_id, uid)['balance']
                if balance < amount:
                    raise CasinoError(f'参加者（ID: {uid}）のジュエルが不足しています。賭け金は引かれていません。')
                event = f'party-bet:{game_id}:{uid}'
                self.casino.finish(db, event, guild_id, uid, 'party_bet', balance,
                                   {'balance': balance - amount}, now)
                db.execute('INSERT INTO party_wagers VALUES(?,?,?,?,?)',
                           (game_id, guild_id, uid, amount, now + 3600))
            # Validate payout headroom after all holds have been recorded.
            for uid in users:
                balance = self.casino.wallet(db, guild_id, uid)['balance']
                self.casino.check_balance(db, guild_id, uid, balance)

    def _settle_wager(self, db, game_id, outcomes):
        rows = db.execute('SELECT * FROM party_wagers WHERE game_id=?', (game_id,)).fetchall()
        if outcomes is not None and rows and set(outcomes) != {row['user_id'] for row in rows}:
            raise ValueError('Wager participants do not match the game result')
        db.execute('DELETE FROM party_wagers WHERE game_id=?', (game_id,))
        for row in rows:
            guild_id, uid, amount = row['guild_id'], row['user_id'], row['amount']
            payout = amount if outcomes is None else (amount * 5 + 3) // 4 if outcomes[uid] else 0
            balance = self.casino.wallet(db, guild_id, uid)['balance']
            kind = 'party_refund' if outcomes is None else 'party_payout'
            self.casino.finish(db, f'party-settle:{game_id}:{uid}', guild_id, uid, kind, balance,
                               {'balance': balance + payout, 'payout': payout}, time.time())

    def settle_wager(self, game_id, outcomes=None):
        with closing(self.casino.connect()) as db, db:
            db.execute('BEGIN IMMEDIATE')
            self._settle_wager(db, game_id, outcomes)

    def refund_expired_wagers(self):
        with closing(self.casino.connect()) as db, db:
            db.execute('BEGIN IMMEDIATE')
            games = db.execute('SELECT DISTINCT game_id FROM party_wagers WHERE expires_at<=?',
                               (time.time(),)).fetchall()
            for row in games:
                self._settle_wager(db, row['game_id'], None)

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
    store = bot.party_rewards
    await asyncio.to_thread(store.settle_wager, game_id, outcomes)
    for user_id, won in outcomes.items():
        from xp_role_bonus import remember_roles
        await remember_roles(bot, guild, user_id)
        result = await asyncio.to_thread(store.grant,guild.id,user_id,game_id,won)
        cfg = await asyncio.to_thread(store.levels.config,guild.id)
        await bot.leveling_service.notify(guild,[result],cfg,channel)
