"""SQLite-хранилище: настройки, каналы, посты, предложения, журнал."""
import json
import os
import sqlite3
import time
from decimal import Decimal, localcontext

from settings import DEFAULTS
from workspace_sql import ScopedConnection

GLOBAL_KEYS = frozenset(('tg_api_id', 'tg_api_hash', 'user_session', 'user_phone',
    'bot_token', 'bot_session', 'bot_session_token', 'admin_ids', 'link_code',
    'llm_provider', 'llm_base_url', 'llm_api_key', 'panel_password_generated',
    'plan_provider', 'plan_api_key'))

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
        self._migrate_workspaces()
        from plan_store import SCHEMA as PLAN_SCHEMA
        self.conn.executescript(PLAN_SCHEMA)
        # Recovery happens exactly once at root DB startup. Never infer that an
        # in-flight Telegram request was unsent; retain its confirmed IDs.
        self.conn.execute("UPDATE content_plan SET status='interrupted',error='Подготовка прервана при перезапуске; повторите вручную.' WHERE status IN ('generating','rendering')")
        self.conn.execute("UPDATE content_plan SET status='uncertain',error='Отправка прервана при перезапуске; проверьте канал, повтор запрещён.' WHERE status='publishing'")
        # Single service process: a persisted running row belongs to a prior startup.
        for table in ('collection_cycles','llm_operations'):
            for row in self.conn.execute(f"SELECT id,report FROM {table} WHERE status='running'").fetchall():
                report = json.loads(row['report'])
                report.update(status='interrupted', finished_at=int(time.time()), cost_usd=None,
                              cost_complete=False, tokens_complete=False, provenance='unknown/incomplete')
                for attempt in report.get('attempts',[]):
                    if attempt['status'] == 'pending':
                        attempt['status'] = 'interrupted'
                self.conn.execute(f'UPDATE {table} SET status=?,finished_at=?,report=? WHERE id=?',
                                  ('interrupted', report['finished_at'], json.dumps(report), row['id']))
        self.conn.commit()
        try:
            self._raw = self.conn
            self.workspace_id = 1
            self.conn = ScopedConnection(self._raw, 1)
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

    def _migrate_workspaces(self):
        # Atomic migration. No credentials or session values are copied.
        with self.conn:
            self.conn.execute('CREATE TABLE IF NOT EXISTS workspaces (id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT NOT NULL, position INTEGER NOT NULL)')
            self.conn.execute("INSERT OR IGNORE INTO workspaces(id,name,position) VALUES(1,'Основная',0)")
            self.conn.execute('CREATE TABLE IF NOT EXISTS workspace_settings (workspace_id INTEGER NOT NULL, key TEXT NOT NULL, value TEXT, PRIMARY KEY(workspace_id,key))')
            for table in ('channels', 'posts'):
                cols = self.conn.execute(f'PRAGMA table_info({table})').fetchall()
                if any(r['name'] == 'workspace_id' for r in cols):
                    continue
                names = ','.join(r['name'] for r in cols)
                declarations = ','.join(r['name'] + ' ' + r['type'] +
                    (' NOT NULL' if r['notnull'] else '') +
                    (' DEFAULT ' + r['dflt_value'] if r['dflt_value'] is not None else '') for r in cols)
                keys = ','.join(r['name'] for r in sorted(cols, key=lambda r:r['pk']) if r['pk'])
                self.conn.execute(f'ALTER TABLE {table} RENAME TO legacy_{table}')
                self.conn.execute(f'CREATE TABLE {table}(workspace_id INTEGER NOT NULL DEFAULT 1,{declarations},PRIMARY KEY(workspace_id,{keys}))')
                self.conn.execute(f'INSERT INTO {table}(workspace_id,{names}) SELECT 1,{names} FROM legacy_{table}')
                self.conn.execute(f'DROP TABLE legacy_{table}')
            for table in ('proposals','events','collection_cycles'):
                cols = {r['name'] for r in self.conn.execute(f'PRAGMA table_info({table})')}
                if 'workspace_id' not in cols:
                    self.conn.execute(f'ALTER TABLE {table} ADD COLUMN workspace_id INTEGER NOT NULL DEFAULT 1')
            cols = {r['name'] for r in self.conn.execute('PRAGMA table_info(proposals)')}
            if 'target_channel' not in cols:
                self.conn.execute('ALTER TABLE proposals ADD COLUMN target_channel TEXT NOT NULL DEFAULT \'\'')
                self.conn.execute("UPDATE proposals SET target_channel=COALESCE((SELECT value FROM settings WHERE key='target_channel'),'')")
            self.conn.execute('CREATE TABLE IF NOT EXISTS app_meta(key TEXT PRIMARY KEY,value TEXT NOT NULL)')
            if not self.conn.execute("SELECT 1 FROM app_meta WHERE key='workspaces_v1'").fetchone():
                for row in self.conn.execute('SELECT key,value FROM settings').fetchall():
                    if row['key'] not in GLOBAL_KEYS:
                        self.conn.execute('INSERT OR IGNORE INTO workspace_settings(workspace_id,key,value) VALUES(1,?,?)', (row['key'],row['value']))
                self.conn.execute("INSERT INTO app_meta(key,value) VALUES('workspaces_v1','1')")
            self.conn.execute('CREATE TABLE IF NOT EXISTS llm_operations(id INTEGER PRIMARY KEY AUTOINCREMENT, workspace_id INTEGER NOT NULL, started_at INTEGER NOT NULL, finished_at INTEGER, status TEXT NOT NULL, report TEXT NOT NULL)')
            for table in ('channels','posts','proposals','events','collection_cycles','llm_operations'):
                self.conn.execute(f'CREATE INDEX IF NOT EXISTS idx_{table}_workspace ON {table}(workspace_id)')

    def workspace(self, workspace_id):
        if not isinstance(workspace_id, int) or isinstance(workspace_id, bool) or workspace_id < 1:
            raise ValueError('Invalid workspace ID')
        if not self._raw.execute('SELECT 1 FROM workspaces WHERE id=?', (workspace_id,)).fetchone():
            raise KeyError('Workspace not found')
        view = object.__new__(DB)
        view.path, view._raw, view.workspace_id = self.path, self._raw, workspace_id
        view.conn = ScopedConnection(self._raw, workspace_id)
        return view

    def workspaces(self):
        return [dict(r) for r in self._raw.execute('SELECT * FROM workspaces ORDER BY position,id')]

    def create_workspace(self, name):
        from settings import UserError
        if not isinstance(name, str) or not name.strip() or len(name.strip()) > 80:
            raise UserError('Имя пространства: от 1 до 80 символов')
        with self._raw:
            cur = self._raw.execute('INSERT INTO workspaces(name,position) VALUES(?,COALESCE((SELECT MAX(position)+1 FROM workspaces),0))', (name.strip(),))
            wid = cur.lastrowid
            for key,value in (('paused','1'),('auto_publish','0'),('mod.breaking.auto_publish','0')):
                self._raw.execute('INSERT INTO workspace_settings(workspace_id,key,value) VALUES(?,?,?)', (wid,key,value))
        return wid

    def rename_workspace(self, workspace_id, name):
        from settings import UserError
        self.workspace(workspace_id)
        if not isinstance(name, str) or not name.strip() or len(name.strip()) > 80:
            raise UserError('Имя пространства: от 1 до 80 символов')
        with self._raw:
            self._raw.execute('UPDATE workspaces SET name=? WHERE id=?', (name.strip(),workspace_id))

    def reorder_workspaces(self, ids):
        from settings import UserError
        existing = [w['id'] for w in self.workspaces()]
        if (not isinstance(ids,list) or any(type(i) is not int for i in ids) or
                len(ids) != len(existing) or set(ids) != set(existing)):
            raise UserError('Порядок должен содержать все пространства ровно один раз')
        with self._raw:
            self._raw.executemany('UPDATE workspaces SET position=? WHERE id=?', enumerate(ids))

    def _one(self, sql, *params):
        return self.conn.execute(sql, params).fetchone()[0]

    # ---------- настройки ----------
    def get(self, key, default=None) -> str:
        row = (self._raw.execute('SELECT value FROM settings WHERE key=?', (key,)) if key in GLOBAL_KEYS else
               self._raw.execute('SELECT value FROM workspace_settings WHERE workspace_id=? AND key=?', (self.workspace_id,key))).fetchone()
        if row:
            return row["value"]
        return default if default is not None else DEFAULTS.get(key, "")

    def geti(self, key) -> int:
        try:
            return int(self.get(key))
        except ValueError:
            return int(DEFAULTS.get(key) or 0)

    def set(self, key, value):
        if key in GLOBAL_KEYS:
            self._raw.execute('INSERT INTO settings(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value', (key,str(value)))
        else:
            self._raw.execute('INSERT INTO workspace_settings(workspace_id,key,value) VALUES(?,?,?) ON CONFLICT(workspace_id,key) DO UPDATE SET value=excluded.value', (self.workspace_id,key,str(value)))
        self._raw.commit()

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
        counts = {table: self._one('SELECT COUNT(*) FROM ' + table)
                  for table in ('posts', 'events', 'collection_cycles')}
        counts['proposals'] = self._raw.execute("SELECT COUNT(*) FROM proposals WHERE workspace_id=? AND kind!='planned'", (self.workspace_id,)).fetchone()[0]
        return counts

    def reset_test_data(self):
        # Exact legacy-news scope; retain durable planned tasks/proposals,
        # dispatch evidence and the operation ledger, plus credentials/config.
        with self.conn:
            deleted = {table: self.conn.execute('DELETE FROM ' + table).rowcount
                       for table in ('posts', 'events', 'collection_cycles')}
            deleted['proposals'] = self._raw.execute("DELETE FROM proposals WHERE workspace_id=? AND kind!='planned'", (self.workspace_id,)).rowcount
            self.conn.execute('UPDATE channels SET last_id=0,last_fetch=NULL,last_error=NULL,posts_total=0')
            self._raw.execute("DELETE FROM workspace_settings WHERE workspace_id=? AND key IN ('last_run','last_result')", (self.workspace_id,))
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
        cur = self._exec("INSERT INTO proposals(created, title, text, original_text, sources, kind, target_channel) "
                         "VALUES(?,?,?,?,?,?,?)",
                         (int(time.time()), title, text, text,
                          json.dumps(sources, ensure_ascii=False), kind, self.get('target_channel')))
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
    def recorded_spending(self):
        from cycle_cost import decimal_cost
        values, incomplete, records = [], 0, 0
        # Each operation owns exactly one row in one of these ledgers. Never
        # add its attempts or the overlapping running/last/history views.
        for table in ('collection_cycles', 'llm_operations'):
            rows = self._raw.execute(f'SELECT status,report FROM {table} WHERE workspace_id=?',
                                     (self.workspace_id,)).fetchall()
            for row in rows:
                records += 1
                try:
                    report = json.loads(row['report'])
                    cost = decimal_cost(report.get('cost_usd'))
                    complete = cost is not None and report.get('cost_complete') is True and row['status'] != 'running'
                    known = cost if cost is not None else decimal_cost(report.get('known_cost_usd'))
                except (ValueError, TypeError, AttributeError):
                    complete, known = False, None
                incomplete += not complete
                if known is not None:
                    values.append(known)
        # Keep tiny recorded amounts exact, without floats or display rounding.
        with localcontext() as ctx:
            ctx.prec = max(28, sum(len(v.as_tuple().digits) + abs(v.as_tuple().exponent) for v in values) + 1)
            total = sum(values, Decimal(0))
        return {'known_cost_usd': format(total, 'f'), 'incomplete_records': incomplete,
                'records': records, 'scope': 'recorded_reports_only'}

    def start_cycle(self, report):
        return self._exec('INSERT INTO collection_cycles(started_at,status,report) VALUES(?,?,?)',
                          (report['started_at'], report['status'], json.dumps(report))).lastrowid

    def save_cycle(self, cid, report):
        self._exec('UPDATE collection_cycles SET finished_at=?,status=?,report=? WHERE id=?',
                   (report['finished_at'], report['status'], json.dumps(report), cid))

    def start_operation(self, report):
        return self._exec('INSERT INTO llm_operations(started_at,status,report) VALUES(?,?,?)',
                          (report['started_at'],report['status'],json.dumps(report))).lastrowid

    def save_operation(self, oid, report):
        self._exec('UPDATE llm_operations SET finished_at=?,status=?,report=? WHERE id=?',
                   (report['finished_at'],report['status'],json.dumps(report),oid))

    def operation_cost_state(self, limit=20, before=None):
        return self._cost_state('llm_operations',limit,before)

    def cycle_cost_state(self, limit=20, before=None):
        return self._cost_state('collection_cycles',limit,before)

    def _cost_state(self, table, limit, before):
        rows = self.conn.execute('SELECT report FROM ' + table + ' ' +
                                 ('WHERE id<? ' if before is not None else '') +
                                 'ORDER BY id DESC LIMIT ?',
                                 (before, limit) if before is not None else (limit,)).fetchall()
        history = [json.loads(r['report']) for r in rows]
        last = self.conn.execute("SELECT report FROM " + table + " WHERE status!='running' "
                                 'ORDER BY id DESC LIMIT 1').fetchone()
        running = self.conn.execute("SELECT report FROM " + table + " WHERE status='running' "
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
