"""Persistent queues, evidence, conservative budgets and delivery receipts."""
from __future__ import annotations

from datetime import datetime, timezone, timedelta
import hashlib
import json
from pathlib import Path
import sqlite3

TZ = timezone(timedelta(hours=8))


def now():
    return datetime.now(TZ).isoformat()


def digest(value):
    return hashlib.sha256(value.encode('utf-8')).hexdigest()


class Store:
    def __init__(self, path):
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(path)
        self.db.row_factory = sqlite3.Row
        self.db.executescript('''
        PRAGMA journal_mode=WAL;
        CREATE TABLE IF NOT EXISTS items (
          id TEXT PRIMARY KEY, url TEXT UNIQUE, title TEXT, source TEXT,
          published TEXT, discovered TEXT, status TEXT DEFAULT 'pending',
          attempts INTEGER DEFAULT 0, retry_at TEXT DEFAULT '',
          document TEXT, classification TEXT, research TEXT);
        CREATE TABLE IF NOT EXISTS events (
          seq INTEGER PRIMARY KEY, at TEXT, item TEXT, stage TEXT,
          status TEXT, reason TEXT, data TEXT);
        CREATE TABLE IF NOT EXISTS budgets (
          day TEXT PRIMARY KEY, tokens INTEGER DEFAULT 0,
          usd REAL DEFAULT 0, calls INTEGER DEFAULT 0);
        CREATE TABLE IF NOT EXISTS deliveries (
          key TEXT PRIMARY KEY, day TEXT, channel TEXT, status TEXT,
          payload TEXT, updated TEXT);
        CREATE TABLE IF NOT EXISTS cache (key TEXT PRIMARY KEY, value TEXT);
        ''')

    def event(self, item, stage, status, reason='', **data):
        data.setdefault('reserved_tokens', 0)
        data.setdefault('reserved_usd', 0)
        self.db.execute('INSERT INTO events(at,item,stage,status,reason,data) VALUES(?,?,?,?,?,?)',
                        (now(), item, stage, status, reason, json.dumps(data, ensure_ascii=False)))
        self.db.commit()

    def add(self, row):
        key = digest(row['url'])[:24]
        with self.db:
            result = self.db.execute('''INSERT OR IGNORE INTO items
                (id,url,title,source,published,discovered) VALUES(?,?,?,?,?,?)''',
                (key, row['url'], row.get('title', ''), row.get('source', ''),
                 row.get('published', ''), now()))
        self.event(key, 'discovery', 'new' if result.rowcount else 'duplicate', 'canonical_url')
        return key

    def update(self, key, **values):
        allowed = {'status', 'attempts', 'retry_at', 'document', 'classification', 'research'}
        if not values.keys() <= allowed:
            raise ValueError('Unsupported state field')
        encoded = {k: json.dumps(v, ensure_ascii=False) if isinstance(v, (dict, list)) else v
                   for k, v in values.items()}
        with self.db:
            self.db.execute('UPDATE items SET ' + ','.join(k+'=?' for k in encoded) + ' WHERE id=?',
                            [*encoded.values(), key])

    def reserve(self, tokens, usd, limits):
        day = now()[:10]
        with self.db:
            self.db.execute('INSERT OR IGNORE INTO budgets(day) VALUES(?)', (day,))
            row = self.db.execute('SELECT * FROM budgets WHERE day=?', (day,)).fetchone()
            if (row['tokens']+tokens > limits['daily_tokens'] or
                row['usd']+usd > limits['daily_usd'] or row['calls'] >= limits['daily_calls']):
                raise RuntimeError('daily_budget_exhausted')
            self.db.execute('UPDATE budgets SET tokens=tokens+?,usd=usd+?,calls=calls+1 WHERE day=?',
                            (tokens, usd, day))
        # Reservations survive timeouts; no refund for unknown provider consumption.
        return day

    def cached(self, key):
        r = self.db.execute('SELECT value FROM cache WHERE key=?', (key,)).fetchone()
        return json.loads(r[0]) if r else None

    def cache(self, key, value):
        with self.db:
            self.db.execute('INSERT OR REPLACE INTO cache VALUES(?,?)',
                            (key, json.dumps(value, ensure_ascii=False)))
            self.db.execute('DELETE FROM cache WHERE rowid NOT IN (SELECT rowid FROM cache ORDER BY rowid DESC LIMIT 1000)')

    def receipt(self, key, channel, status, payload):
        with self.db:
            self.db.execute('INSERT OR REPLACE INTO deliveries VALUES(?,?,?,?,?,?)',
                (key, now()[:10], channel, status, json.dumps(payload, ensure_ascii=False), now()))

    def checkpoint(self):
        self.db.commit()
        self.db.execute('PRAGMA wal_checkpoint(TRUNCATE)')

