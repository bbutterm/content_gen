"""SQLite-хранилище: настройки, каналы, посты, предложения, журнал."""
import json
import os
import sqlite3
import time

from settings import DEFAULTS

SCHEMA = """
CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT);
CREATE TABLE IF NOT EXISTS channels (
    username    TEXT PRIMARY KEY,
    title       TEXT,
    last_id     INTEGER NOT NULL DEFAULT 0,
    enabled     INTEGER NOT NULL DEFAULT 1,
    added_at    INTEGER NOT NULL DEFAULT 0,
    last_fetch  INTEGER,
    last_error  TEXT,
    posts_total INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS posts (
    channel TEXT NOT NULL, msg_id INTEGER NOT NULL, date INTEGER NOT NULL,
    text TEXT NOT NULL, link TEXT, used INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (channel, msg_id)
);
CREATE TABLE IF NOT EXISTS proposals (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    created      INTEGER NOT NULL,
    title        TEXT,
    text         TEXT NOT NULL,
    sources      TEXT NOT NULL DEFAULT '[]',
    status       TEXT NOT NULL DEFAULT 'new',
    tg_msgs      TEXT NOT NULL DEFAULT '[]',
    published_at INTEGER
);
CREATE TABLE IF NOT EXISTS collection_cycles (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    started_at INTEGER NOT NULL,
    finished_at INTEGER,
    status TEXT NOT NULL,
    report TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS events (
    id INTEGER PRIMARY KEY AUTOINCREMENT, ts INTEGER NOT NULL, level TEXT, text TEXT
);
"""

# Колонки, добавленные позже: доезжают в старые базы автоматически
MIGRATIONS = {
    "posts": [("media", "TEXT")],
    "proposals": [("original_text", "TEXT"), ("media", "TEXT"), ("queued_at", "INTEGER"),
                  ("kind", "TEXT NOT NULL DEFAULT 'normal'")],
}


class DB:
    def __init__(self, path: str):
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        self.path = path
        self.conn = sqlite3.connect(path)
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(SCHEMA)
        self._migrate()
        # Single service process: a persisted running row belongs to a prior startup.
        for row in self.conn.execute("SELECT id,report FROM collection_cycles WHERE status='running'").fetchall():
            report = json.loads(row['report'])
            report.update(status='interrupted', finished_at=int(time.time()), cost_usd=None,
                          cost_complete=False, tokens_complete=False, provenance='unknown/incomplete')
            for attempt in report['attempts']:
                if attempt['status'] == 'pending':
                    attempt['status'] = 'interrupted'
            self.conn.execute('UPDATE collection_cycles SET status=?,finished_at=?,report=? WHERE id=?',
                              ('interrupted', report['finished_at'], json.dumps(report), row['id']))
        self.conn.commit()
        try:
            os.chmod(path, 0o600)  # внутри ключи и сессия Telegram
        except OSError:
            pass

    def _migrate(self):
        for table, columns in MIGRATIONS.items():
            have = {r["name"] for r in self.conn.execute(f"PRAGMA table_info({table})")}
            for name, decl in columns:
                if name not in have:
                    self.conn.execute(f"ALTER TABLE {table} ADD COLUMN {name} {decl}")

    def _exec(self, sql, params=()):
        cur = self.conn.execute(sql, params)
        self.conn.commit()
        return cur

    def _one(self, sql, *params):
        return self.conn.execute(sql, params).fetchone()[0]

    # ---------- настройки ----------
    def get(self, key, default=None) -> str:
        row = self.conn.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
        if row:
            return row["value"]
        return default if default is not None else DEFAULTS.get(key, "")

    def geti(self, key) -> int:
        try:
            return int(self.get(key))
        except ValueError:
            return int(DEFAULTS.get(key) or 0)

    def set(self, key, value):
        self._exec("INSERT INTO settings(key, value) VALUES(?, ?) "
                   "ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, str(value)))

    # ---------- каналы ----------
    def add_channel(self, username, title=None) -> bool:
        cur = self._exec("INSERT OR IGNORE INTO channels(username, title, added_at) VALUES(?,?,?)",
                         (username, title, int(time.time())))
        return cur.rowcount > 0

    def remove_channel(self, username) -> bool:
        return self._exec("DELETE FROM channels WHERE username=?", (username,)).rowcount > 0

    def toggle_channel(self, username, enabled: bool):
        self._exec("UPDATE channels SET enabled=? WHERE username=?", (int(enabled), username))

    def channels(self, enabled_only=False):
        sql = "SELECT * FROM channels" + (" WHERE enabled=1" if enabled_only else "") + " ORDER BY added_at, rowid"
        return self.conn.execute(sql).fetchall()

    def channel_fetched(self, username, last_id, title, added):
        self._exec("UPDATE channels SET last_id=?, title=COALESCE(?, title), last_fetch=?, "
                   "last_error=NULL, posts_total=posts_total+? WHERE username=?",
                   (last_id, title, int(time.time()), added, username))

    def channel_error(self, username, error):
        self._exec("UPDATE channels SET last_fetch=?, last_error=? WHERE username=?",
                   (int(time.time()), str(error)[:300], username))

    # ---------- посты ----------
    def add_post(self, channel, msg_id, date, text, link, media=None) -> bool:
        cur = self._exec("INSERT OR IGNORE INTO posts(channel, msg_id, date, text, link, media) "
                         "VALUES(?,?,?,?,?,?)", (channel, msg_id, date, text, link, media))
        return cur.rowcount > 0

    def pending_posts(self, limit):
        rows = self.conn.execute("SELECT * FROM posts WHERE used=0 ORDER BY date DESC LIMIT ?",
                                 (limit,)).fetchall()
        return list(reversed(rows))

    def recent_pending(self, since_ts):
        return self.conn.execute("SELECT * FROM posts WHERE used=0 AND date>=? ORDER BY date",
                                 (since_ts,)).fetchall()

    def pending_count(self) -> int:
        return self._one("SELECT COUNT(*) FROM posts WHERE used=0")

    def test_data_counts(self):
        return {table: self._one('SELECT COUNT(*) FROM ' + table)
                for table in ('posts', 'proposals', 'events', 'collection_cycles')}

    def reset_test_data(self):
        # Exact content-only scope; retain schema, credentials and channel configuration.
        with self.conn:
            deleted = {table: self.conn.execute('DELETE FROM ' + table).rowcount
                       for table in ('posts', 'proposals', 'events', 'collection_cycles')}
            self.conn.execute('UPDATE channels SET last_id=0,last_fetch=NULL,last_error=NULL,posts_total=0')
            self.conn.execute("DELETE FROM settings WHERE key IN ('last_run','last_result')")
        return deleted

    def reanalysis_count(self, since_ts=None):
        sql = "SELECT COUNT(*) FROM posts WHERE used=1"
        return self._one(sql + (" AND date>=?" if since_ts is not None else ""),
                         *((since_ts,) if since_ts is not None else ()))

    def reset_analysis(self, since_ts=None):
        sql = "UPDATE posts SET used=0 WHERE used=1"
        with self.conn:
            cur = self.conn.execute(sql + (" AND date>=?" if since_ts is not None else ""),
                                    (since_ts,) if since_ts is not None else ())
        return cur.rowcount

    def mark_all_used(self):
        self._exec("UPDATE posts SET used=1 WHERE used=0")

    def mark_used(self, keys):
        self.conn.executemany("UPDATE posts SET used=1 WHERE channel=? AND msg_id=?", list(keys))
        self.conn.commit()

    def posts_by_keys(self, keys):
        rows = []
        for channel, msg_id in keys:
            row = self.conn.execute("SELECT * FROM posts WHERE channel=? AND msg_id=?",
                                    (channel, msg_id)).fetchone()
            if row:
                rows.append(row)
        return rows

    def cleanup(self, days=14):
        border = int(time.time()) - days * 86400
        self._exec("DELETE FROM posts WHERE date < ? AND used=1 AND NOT EXISTS ("
                   "SELECT 1 FROM proposals WHERE status IN ('new','queued') AND sources LIKE "
                   "'%' || json_quote(posts.channel) || ', ' || posts.msg_id || ']%')", (border,))
        self._exec("DELETE FROM events WHERE id NOT IN (SELECT id FROM events ORDER BY id DESC LIMIT 500)")

    # ---------- предложения ----------
    def add_proposal(self, title, text, sources, kind="normal") -> int:
        cur = self._exec("INSERT INTO proposals(created, title, text, original_text, sources, kind) "
                         "VALUES(?,?,?,?,?,?)",
                         (int(time.time()), title, text, text,
                          json.dumps(sources, ensure_ascii=False), kind))
        return cur.lastrowid

    @staticmethod
    def _proposal(row):
        p = dict(row)
        p["sources"] = [tuple(x) for x in json.loads(p["sources"])]
        p["tg_msgs"] = [tuple(x) for x in json.loads(p["tg_msgs"])]
        p["media"] = tuple(json.loads(p["media"])) if p.get("media") else None
        p["kind"] = p.get("kind") or "normal"
        return p

    def get_proposal(self, pid):
        row = self.conn.execute("SELECT * FROM proposals WHERE id=?", (pid,)).fetchone()
        return self._proposal(row) if row else None

    def proposals(self, status=None, limit=50, kind=None):
        where, params = [], []
        if status:
            where.append("status=?")
            params.append(status)
        if kind:
            where.append("kind=?")
            params.append(kind)
        sql = "SELECT * FROM proposals" + (" WHERE " + " AND ".join(where) if where else "")
        sql += " ORDER BY " + ("queued_at, id" if status == "queued" else "id DESC") + " LIMIT ?"
        return [self._proposal(r) for r in self.conn.execute(sql, (*params, limit)).fetchall()]

    def set_proposal_text(self, pid, text):
        self._exec("UPDATE proposals SET text=? WHERE id=?", (text, pid))

    def set_proposal_status(self, pid, status):
        self._exec("UPDATE proposals SET status=?, published_at=?, queued_at=? WHERE id=?",
                   (status, int(time.time()) if status == "published" else None,
                    int(time.time() * 1000) if status == "queued" else None, pid))

    def set_proposal_media(self, pid, media):
        self._exec("UPDATE proposals SET media=? WHERE id=?", (json.dumps(list(media)) if media else None, pid))

    def set_tg_msgs(self, pid, refs):
        self._exec("UPDATE proposals SET tg_msgs=? WHERE id=?", (json.dumps(refs), pid))

    def swap_queue(self, pid_a, pid_b):
        a = self._one("SELECT queued_at FROM proposals WHERE id=?", pid_a)
        b = self._one("SELECT queued_at FROM proposals WHERE id=?", pid_b)
        if a == b:
            b = a - 1
        self._exec("UPDATE proposals SET queued_at=? WHERE id=?", (b, pid_a))
        self._exec("UPDATE proposals SET queued_at=? WHERE id=?", (a, pid_b))

    def recent_titles(self, n=30):
        rows = self.conn.execute("SELECT title FROM proposals WHERE title IS NOT NULL AND title!='' "
                                 "ORDER BY id DESC LIMIT ?", (n,)).fetchall()
        return [r["title"] for r in rows]

    # ---------- для обучения ----------
    def published_examples(self, since, n):
        rows = self.conn.execute(
            "SELECT * FROM proposals WHERE status='published' AND created>? "
            "ORDER BY (COALESCE(original_text, text) != text) DESC, published_at DESC LIMIT ?",
            (since, n)).fetchall()
        return [self._proposal(r) for r in rows]

    def skipped_titles(self, since, n):
        rows = self.conn.execute("SELECT title, text FROM proposals WHERE status='skipped' AND created>? "
                                 "ORDER BY id DESC LIMIT ?", (since, n)).fetchall()
        return [(r["title"] or r["text"].split("\n", 1)[0])[:120] for r in rows]

    def learning_counts(self, since) -> dict:
        return {
            "published": self._one("SELECT COUNT(*) FROM proposals WHERE status='published' AND created>?", since),
            "edited": self._one("SELECT COUNT(*) FROM proposals WHERE status='published' AND created>? "
                                "AND COALESCE(original_text, text) != text", since),
            "skipped": self._one("SELECT COUNT(*) FROM proposals WHERE status='skipped' AND created>?", since),
        }

    def stats(self) -> dict:
        day = int(time.time()) - 86400
        return {
            "new": self._one("SELECT COUNT(*) FROM proposals WHERE status='new'"),
            "queued": self._one("SELECT COUNT(*) FROM proposals WHERE status='queued'"),
            "published": self._one("SELECT COUNT(*) FROM proposals WHERE status='published'"),
            "skipped": self._one("SELECT COUNT(*) FROM proposals WHERE status='skipped'"),
            "published_24h": self._one("SELECT COUNT(*) FROM proposals WHERE status='published' "
                                       "AND published_at>=?", day),
            "posts_24h": self._one("SELECT COUNT(*) FROM posts WHERE date>=?", day),
            "pending_posts": self.pending_count(),
        }

    # ---------- collection cost history (sanitized reports only) ----------
    def start_cycle(self, report):
        return self._exec('INSERT INTO collection_cycles(started_at,status,report) VALUES(?,?,?)',
                          (report['started_at'], report['status'], json.dumps(report))).lastrowid

    def save_cycle(self, cid, report):
        self._exec('UPDATE collection_cycles SET finished_at=?,status=?,report=? WHERE id=?',
                   (report['finished_at'], report['status'], json.dumps(report), cid))

    def cycle_cost_state(self, limit=20, before=None):
        rows = self.conn.execute('SELECT report FROM collection_cycles ' +
                                 ('WHERE id<? ' if before is not None else '') +
                                 'ORDER BY id DESC LIMIT ?',
                                 (before, limit) if before is not None else (limit,)).fetchall()
        history = [json.loads(r['report']) for r in rows]
        last = self.conn.execute("SELECT report FROM collection_cycles WHERE status!='running' "
                                 'ORDER BY id DESC LIMIT 1').fetchone()
        running = self.conn.execute("SELECT report FROM collection_cycles WHERE status='running' "
                                    'ORDER BY id DESC LIMIT 1').fetchone()
        return {'history': history, 'last_completed': json.loads(last['report']) if last else None,
                'running': json.loads(running['report']) if running else None,
                'next_before': history[-1]['id'] if len(history) == limit else None}

    # ---------- журнал ----------
    def add_event(self, level, text):
        self._exec("INSERT INTO events(ts, level, text) VALUES(?,?,?)", (int(time.time()), level, text))

    def events(self, limit=100):
        return [dict(r) for r in self.conn.execute(
            "SELECT * FROM events ORDER BY id DESC LIMIT ?", (limit,)).fetchall()]
