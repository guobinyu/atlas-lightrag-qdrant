"""Versioned SQLite store for the culture library. Connections are short-lived, writes transactional."""
from contextlib import contextmanager
from datetime import datetime, timezone
import sqlite3

from .prompts import THEMES

SCHEMA = '''
CREATE TABLE metadata (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE themes (label TEXT PRIMARY KEY, origin TEXT NOT NULL, at TEXT NOT NULL);
CREATE TABLE samples (id TEXT PRIMARY KEY, data TEXT NOT NULL);
CREATE TABLE batches (id TEXT PRIMARY KEY, data TEXT NOT NULL, status TEXT NOT NULL);
CREATE TABLE chunks (id TEXT PRIMARY KEY, batch_id TEXT NOT NULL REFERENCES batches(id),
    data TEXT NOT NULL, status TEXT NOT NULL, error TEXT, raw TEXT, issues TEXT);
CREATE TABLE elements (id TEXT PRIMARY KEY, type TEXT NOT NULL, norm TEXT NOT NULL, status TEXT NOT NULL,
    data TEXT NOT NULL, UNIQUE(type, norm));
CREATE TABLE relations (id TEXT PRIMARY KEY, subject TEXT NOT NULL REFERENCES elements(id), predicate TEXT NOT NULL,
    object TEXT NOT NULL REFERENCES elements(id), data TEXT NOT NULL);
CREATE TABLE evidence (id TEXT PRIMARY KEY, element_id TEXT REFERENCES elements(id), relation_id TEXT REFERENCES relations(id),
    chunk_id TEXT NOT NULL REFERENCES chunks(id), sample_id TEXT NOT NULL, data TEXT NOT NULL);
CREATE TABLE indexed (element_id TEXT PRIMARY KEY, hash TEXT NOT NULL);
PRAGMA user_version=1;
'''


class Store:
    def __init__(self, path, *, recover=True):
        self.path = path
        path.parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as db:
            version = db.execute('PRAGMA user_version').fetchone()[0]
            if version > 1:
                raise ValueError('文化知识库版本较新，请升级程序')
            if version == 0:
                db.executescript(SCHEMA)
                at = datetime.now(timezone.utc).isoformat()
                db.executemany("INSERT INTO themes VALUES (?, 'preset', ?)", [(t, at) for t in THEMES])
            # Single process ownership: restarting never initiates new model requests.
            if recover:
                db.execute("UPDATE batches SET status='interrupted' WHERE status='running'")
                db.execute("UPDATE chunks SET status='interrupted' WHERE status='running'")

    @contextmanager
    def connect(self):
        db = sqlite3.connect(self.path, timeout=30)
        db.row_factory = sqlite3.Row
        db.execute('PRAGMA foreign_keys=ON')
        try:
            with db:
                yield db
        finally:
            db.close()
