"""Versioned SQLite journal. All connections are short-lived, writes transactional."""
from contextlib import contextmanager
import json
import sqlite3
import uuid


def dumps(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True)


class Store:
    def __init__(self, path, *, recover=True):
        self.path = path
        path.parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as db:
            version = db.execute('PRAGMA user_version').fetchone()[0]
            if version > 1:
                raise ValueError('语义数据库版本较新，请升级程序')
            if version == 0:
                db.executescript('''
                    CREATE TABLE metadata (key TEXT PRIMARY KEY, value TEXT NOT NULL);
                    CREATE TABLE batches (id TEXT PRIMARY KEY, data TEXT NOT NULL, status TEXT NOT NULL);
                    CREATE TABLE chunks (id TEXT PRIMARY KEY, batch_id TEXT NOT NULL REFERENCES batches(id),
                        data TEXT NOT NULL, status TEXT NOT NULL, error TEXT, raw TEXT);
                    CREATE TABLE entities (id TEXT PRIMARY KEY, chunk_id TEXT NOT NULL REFERENCES chunks(id), data TEXT NOT NULL);
                    CREATE TABLE candidates (id TEXT PRIMARY KEY, chunk_id TEXT NOT NULL REFERENCES chunks(id),
                        data TEXT NOT NULL, validation TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'pending');
                    CREATE TABLE reviews (id INTEGER PRIMARY KEY, candidate_id TEXT NOT NULL REFERENCES candidates(id),
                        action TEXT NOT NULL, at TEXT NOT NULL, reviewer TEXT NOT NULL, data TEXT NOT NULL);
                    CREATE TABLE publications (id INTEGER PRIMARY KEY, candidate_id TEXT NOT NULL REFERENCES candidates(id),
                        review_id INTEGER NOT NULL REFERENCES reviews(id), at TEXT NOT NULL, revoked_at TEXT);
                    CREATE UNIQUE INDEX active_publication ON publications(candidate_id) WHERE revoked_at IS NULL;
                    PRAGMA user_version=1;
                ''')
                db.execute('INSERT INTO metadata VALUES (?,?)', ('project_uri', 'urn:atlas:project:' + str(uuid.uuid4()) + ':'))
            # Single process ownership: restarting never initiates new model requests.
            if recover:
                db.execute("UPDATE batches SET status='interrupted' WHERE status='running'")
                db.execute("UPDATE chunks SET status='interrupted' WHERE status='running'")
            self.base = db.execute("SELECT value FROM metadata WHERE key='project_uri'").fetchone()[0]

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
