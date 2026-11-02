import os
from pathlib import Path
import sqlite3
import time

from config import ROOT


class Store:
    def __init__(self, path=None):
        path = Path(path or ROOT / 'data' / 'state.db')
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.db = sqlite3.connect(path, timeout=30)
        os.chmod(path, 0o600)
        self.db.executescript('''
            CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS sent (id TEXT PRIMARY KEY, sent_at REAL NOT NULL);
            CREATE TABLE IF NOT EXISTS alerts (kind TEXT PRIMARY KEY, sent_at REAL NOT NULL);
            CREATE TABLE IF NOT EXISTS incidents (
                id INTEGER PRIMARY KEY, kind TEXT NOT NULL, started_at REAL NOT NULL,
                alerted_at REAL, recovered_at REAL
            );
            CREATE UNIQUE INDEX IF NOT EXISTS incident_open_kind
                ON incidents(kind) WHERE recovered_at IS NULL;
        ''')

    def initialized(self):
        return self.db.execute("SELECT 1 FROM meta WHERE key='initialized'").fetchone() is not None

    def baseline(self, ids):
        with self.db:
            self.db.executemany('INSERT OR IGNORE INTO sent VALUES (?, ?)',
                                [(str(i), time.time()) for i in ids])
            self.db.execute("INSERT OR REPLACE INTO meta VALUES ('initialized', '1')")

    def unseen(self, ids):
        return {str(i) for i in ids if not self.db.execute('SELECT 1 FROM sent WHERE id=?', (str(i),)).fetchone()}

    def mark_sent(self, ids):
        with self.db:
            self.db.executemany('INSERT OR IGNORE INTO sent VALUES (?, ?)',
                                [(str(i), time.time()) for i in ids])

    def alert_due(self, kind):
        row = self.db.execute('SELECT sent_at FROM alerts WHERE kind=?', (kind,)).fetchone()
        return row is None or time.time() - row[0] >= 3600

    def mark_alert(self, kind):
        now = time.time()
        with self.db:
            self.db.execute('INSERT OR REPLACE INTO alerts VALUES (?, ?)', (kind, now))
            self.db.execute('INSERT OR IGNORE INTO incidents (kind, started_at) VALUES (?, ?)', (kind, now))
            self.db.execute('''UPDATE incidents SET alerted_at=COALESCE(alerted_at, ?)
                WHERE kind=? AND recovered_at IS NULL''', (now, kind))

    def record_failure(self, kind):
        with self.db:
            self.db.execute('INSERT OR IGNORE INTO incidents (kind, started_at) VALUES (?, ?)', (kind, time.time()))

    def mark_recovered(self):
        with self.db:
            self.db.execute('DELETE FROM incidents WHERE alerted_at IS NULL')
            self.db.execute('UPDATE incidents SET recovered_at=? WHERE recovered_at IS NULL', (time.time(),))

    def pending_recoveries(self):
        columns = ('id', 'kind', 'started_at', 'alerted_at', 'recovered_at')
        return [dict(zip(columns, row)) for row in self.db.execute('''SELECT id, kind, started_at,
            alerted_at, recovered_at FROM incidents WHERE recovered_at IS NOT NULL ORDER BY started_at, id''')]

    def finish_recovery(self, incident_id):
        with self.db:
            self.db.execute('DELETE FROM incidents WHERE id=? AND recovered_at IS NOT NULL', (incident_id,))

    def close(self):
        self.db.close()
