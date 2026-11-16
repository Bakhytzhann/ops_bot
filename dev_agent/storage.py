import json
import secrets
import sqlite3
import time


class Storage:
    def __init__(self, path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(path)
        self.db.row_factory = sqlite3.Row
        self.db.executescript("""
        PRAGMA journal_mode=WAL;
        CREATE TABLE IF NOT EXISTS tasks (
          id INTEGER PRIMARY KEY, owner INTEGER, created REAL, status TEXT,
          prompt TEXT, thread TEXT, base TEXT, result TEXT, tests TEXT);
        CREATE TABLE IF NOT EXISTS confirmations (
          token TEXT PRIMARY KEY, owner INTEGER, expires REAL, payload TEXT);
        CREATE TABLE IF NOT EXISTS updates (id INTEGER PRIMARY KEY, created REAL);
        CREATE TABLE IF NOT EXISTS state (key TEXT PRIMARY KEY, value TEXT);
        """)
        self.db.execute(
            "UPDATE tasks SET status='failed', result='Agent interrupted; session may be resumed' WHERE status IN ('running','waiting_user')"
        )
        self.db.execute("DELETE FROM confirmations")
        self.db.commit()

    def seen(self, update_id):
        cur = self.db.execute(
            "INSERT OR IGNORE INTO updates VALUES (?,?)", (update_id, time.time())
        )
        self.db.commit()
        return cur.rowcount == 0

    def create(self, owner, prompt, thread=None):
        cur = self.db.execute(
            "INSERT INTO tasks(owner,created,status,prompt,thread) VALUES (?,?,?,?,?)",
            (owner, time.time(), "queued", prompt, thread),
        )
        self.db.commit()
        return cur.lastrowid

    def tasks(self):
        return [
            dict(r)
            for r in self.db.execute("SELECT * FROM tasks ORDER BY id DESC LIMIT 30")
        ]

    def task(self, task_id):
        row = self.db.execute("SELECT * FROM tasks WHERE id=?", (task_id,)).fetchone()
        return dict(row) if row else None

    def next_queued(self):
        row = self.db.execute(
            "SELECT * FROM tasks WHERE status='queued' ORDER BY id LIMIT 1"
        ).fetchone()
        return dict(row) if row else None

    def change(self, task_id, **values):
        allowed = {"status", "thread", "base", "result", "tests", "prompt"}
        if not values.keys() <= allowed:
            raise ValueError("Invalid task fields")
        self.db.execute(
            "UPDATE tasks SET " + ",".join(k + "=?" for k in values) + " WHERE id=?",
            (*values.values(), task_id),
        )
        self.db.commit()

    def confirm(self, owner, payload, ttl=300):
        token = secrets.token_hex(12)
        self.db.execute(
            "INSERT INTO confirmations VALUES (?,?,?,?)",
            (token, owner, time.time() + ttl, json.dumps(payload)),
        )
        self.db.commit()
        return token

    def consume(self, owner, token):
        with self.db:
            row = self.db.execute(
                "DELETE FROM confirmations WHERE token=? AND owner=? AND expires>? RETURNING payload",
                (token, owner, time.time()),
            ).fetchone()
        if not row:
            raise ValueError("Confirmation expired or already used")
        return json.loads(row[0])

    def put(self, key, value):
        self.db.execute(
            "INSERT OR REPLACE INTO state VALUES (?,?)", (key, json.dumps(value))
        )
        self.db.commit()

    def get(self, key, default=None):
        row = self.db.execute("SELECT value FROM state WHERE key=?", (key,)).fetchone()
        return json.loads(row[0]) if row else default

    def prune(self, days):
        cutoff = time.time() - days * 86400
        self.db.execute(
            "UPDATE tasks SET prompt='',result='',tests='',thread=NULL WHERE created<? AND status IN ('completed','failed','cancelled')",
            (cutoff,),
        )
        self.db.execute("DELETE FROM updates WHERE created<?", (cutoff,))
        self.db.execute("DELETE FROM confirmations WHERE expires<?", (time.time(),))
        self.db.commit()
