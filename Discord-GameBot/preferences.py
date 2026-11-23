"""Small persistent user preferences shared by voice, leveling, and settings UI."""
from contextlib import closing
import json
from pathlib import Path
import sqlite3
from storage import database_path

PERSONAL_DEFAULTS = dict(reading_enabled=True, level_notifications=True)
VOICE_DEFAULTS = dict(provider='voicevox', voice='3', speed=1.0)


class VoiceStore:
    def __init__(self, path=None):
        self.path = Path(path) if path else database_path('level')
        with closing(sqlite3.connect(self.path)) as db:
            db.execute('CREATE TABLE IF NOT EXISTS voice_preferences (scope TEXT, item TEXT, value TEXT NOT NULL, PRIMARY KEY(scope,item))')
            db.commit()

    def get(self, scope, item, default):
        with closing(sqlite3.connect(self.path)) as db:
            row = db.execute('SELECT value FROM voice_preferences WHERE scope=? AND item=?', (scope, str(item))).fetchone()
        return json.loads(row[0]) if row else dict(default)

    def put(self, scope, item, value):
        with closing(sqlite3.connect(self.path)) as db:
            db.execute('INSERT OR REPLACE INTO voice_preferences VALUES(?,?,?)', (scope, str(item), json.dumps(value)))
            db.commit()


    def update(self, scope, item, changes, default=None):
        # Merge under a write transaction so separate panels keep unrelated fields.
        with closing(sqlite3.connect(self.path)) as db:
            db.execute('BEGIN IMMEDIATE')
            row = db.execute('SELECT value FROM voice_preferences WHERE scope=? AND item=?', (scope, str(item))).fetchone()
            value = dict(default or {}) | (json.loads(row[0]) if row else {}) | changes
            db.execute('INSERT OR REPLACE INTO voice_preferences VALUES(?,?,?)', (scope, str(item), json.dumps(value)))
            db.commit()
        return value

    def personal(self, uid):
        return PERSONAL_DEFAULTS | self.get('personal', uid, {})
