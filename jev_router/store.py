"""Session-scoped immutable snapshots and locally auditable events."""
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import time


def dumps(value):
    return json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":"), allow_nan=False)


def digest(value):
    return hashlib.sha256(dumps(value).encode()).hexdigest()


class Store:
    def __init__(self, path):
        path = Path(path).expanduser()
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        fd = os.open(path, os.O_CREAT | os.O_RDWR, 0o600)
        os.close(fd)
        os.chmod(path, 0o600)
        self.db = sqlite3.connect(path)
        self.db.row_factory = sqlite3.Row
        self.db.executescript("""
            PRAGMA journal_mode=WAL;
            PRAGMA busy_timeout=5000;
            CREATE TABLE IF NOT EXISTS snapshots (
                id TEXT PRIMARY KEY, session TEXT, call_id TEXT, raw_hash TEXT, data TEXT
            );
            CREATE INDEX IF NOT EXISTS snapshot_call ON snapshots(session, call_id);
            CREATE TABLE IF NOT EXISTS chunks (
                session TEXT, ref TEXT, snapshot TEXT, text TEXT,
                PRIMARY KEY(session, ref)
            );
            CREATE TABLE IF NOT EXISTS decisions (
                session TEXT, key TEXT, answer TEXT, PRIMARY KEY(session, key)
            );
            CREATE TABLE IF NOT EXISTS arrivals (
                session TEXT, snapshot TEXT, config TEXT, result TEXT,
                PRIMARY KEY(session, snapshot, config)
            );
            CREATE TABLE IF NOT EXISTS views (
                session TEXT, call_id TEXT, hash TEXT, snapshot TEXT,
                PRIMARY KEY(session, call_id, hash, snapshot)
            );
            CREATE TABLE IF NOT EXISTS sent (
                session TEXT PRIMARY KEY, time REAL, model TEXT, host TEXT, sizes TEXT
            );
            CREATE TABLE IF NOT EXISTS sent_views (
                session TEXT, call_id TEXT, view TEXT, PRIMARY KEY(session, call_id)
            );
            CREATE TABLE IF NOT EXISTS events (
                id INTEGER PRIMARY KEY, session TEXT, kind TEXT, time REAL, data TEXT
            );
        """)

    def snapshot(self, session, call_id, data):
        sid = digest([session, call_id, data["raw"], data["tool"]])
        with self.db:
            self.db.execute("INSERT OR IGNORE INTO snapshots VALUES(?,?,?,?,?)",
                            (sid, session, call_id, digest(data["raw"]), dumps(data)))
        self.view(session, call_id, data["raw"], sid)
        return sid

    def view(self, session, call_id, raw, sid):
        with self.db:
            self.db.execute("INSERT OR IGNORE INTO views VALUES(?,?,?,?)", (session, call_id, digest(raw), sid))

    def find(self, session, call_id, raw):
        # Ambiguous or modified host messages are never overwritten.
        rows = self.db.execute("SELECT DISTINCT snapshot FROM views WHERE session=? AND call_id=? AND hash=?",
                               (session, call_id, digest(raw))).fetchall()
        if len(rows) != 1:
            return None
        row = self.db.execute("SELECT id,data FROM snapshots WHERE session=? AND id=?", (session, rows[0][0])).fetchone()
        return (row["id"], json.loads(row["data"])) if row else None

    def put_chunk(self, session, ref, sid, text):
        with self.db:
            self.db.execute("INSERT OR IGNORE INTO chunks VALUES(?,?,?,?)", (session, ref, sid, text))

    def expand(self, session, ref):
        row = self.db.execute("SELECT text FROM chunks WHERE session=? AND ref=?", (session, ref)).fetchone()
        if row is None:
            raise KeyError("Unknown reference in this session")
        return row[0]

    def cached(self, session, key):
        row = self.db.execute("SELECT answer FROM decisions WHERE session=? AND key=?", (session, key)).fetchone()
        return json.loads(row[0]) if row else None

    def cache(self, session, key, answer):
        with self.db:
            self.db.execute("INSERT OR REPLACE INTO decisions VALUES(?,?,?)", (session, key, dumps(answer)))

    def arrival(self, session, sid, config):
        row = self.db.execute("SELECT result FROM arrivals WHERE session=? AND snapshot=? AND config=?", (session, sid, config)).fetchone()
        return json.loads(row[0]) if row else None

    def save_arrival(self, session, sid, config, result):
        with self.db:
            self.db.execute("INSERT OR IGNORE INTO arrivals VALUES(?,?,?,?)", (session, sid, config, dumps(result)))

    def sent_state(self, session):
        row = self.db.execute("SELECT time,model,host,sizes FROM sent WHERE session=?", (session,)).fetchone()
        return {"time": row[0], "model": row[1], "host": json.loads(row[2]), "sizes": json.loads(row[3])} if row else None

    def save_sent(self, session, when, model, host, sizes):
        with self.db:
            self.db.execute("INSERT OR REPLACE INTO sent VALUES(?,?,?,?,?)", (session, when, model, dumps(host), dumps(sizes)))

    def sent_view(self, session, call_id):
        row = self.db.execute("SELECT view FROM sent_views WHERE session=? AND call_id=?", (session, call_id)).fetchone()
        return json.loads(row[0]) if row else None

    def save_sent_view(self, session, call_id, view):
        with self.db:
            self.db.execute("INSERT OR REPLACE INTO sent_views VALUES(?,?,?)", (session, call_id, dumps(view)))

    def event(self, session, kind, data):
        with self.db:
            self.db.execute("INSERT INTO events(session,kind,time,data) VALUES(?,?,?,?)", (session, kind, time.time(), dumps(data)))

    def close(self):
        self.db.close()
