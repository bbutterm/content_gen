"""Explicit workspace-scoped plan SQL; never routed through ScopedConnection."""
import json
import time

from settings import ProposalNotFound, UserError

SCHEMA = """
CREATE TABLE IF NOT EXISTS content_plan (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    workspace_id INTEGER NOT NULL,
    created_at INTEGER NOT NULL,
    topic TEXT NOT NULL,
    brief TEXT NOT NULL DEFAULT '',
    scheduled_at INTEGER NOT NULL,
    target_channel TEXT NOT NULL,
    with_video INTEGER NOT NULL DEFAULT 1,
    status TEXT NOT NULL DEFAULT 'scheduled',
    proposal_id INTEGER,
    operation_id INTEGER,
    payload TEXT,
    video_meta TEXT,
    dispatch TEXT NOT NULL DEFAULT '{}',
    error TEXT
);
CREATE INDEX IF NOT EXISTS idx_content_plan_workspace ON content_plan(workspace_id,status,scheduled_at);
CREATE UNIQUE INDEX IF NOT EXISTS idx_content_plan_proposal ON content_plan(proposal_id) WHERE proposal_id IS NOT NULL;
"""


class PlanStore:
    def __init__(self, db):
        self.db = db
        self.conn = db._raw
        self.wid = db.workspace_id

    @staticmethod
    def decode(row):
        if row is None: return None
        item = dict(row)
        for key in ('payload', 'video_meta', 'dispatch'):
            item[key] = json.loads(item[key]) if item[key] else None
        item['with_video'] = bool(item['with_video'])
        return item

    def get(self, iid):
        if type(iid) is not int or not 1 <= iid <= 9223372036854775807:
            raise UserError('Неверный ID задания')
        row = self.conn.execute('SELECT * FROM content_plan WHERE workspace_id=? AND id=?', (self.wid, iid)).fetchone()
        if row is None: raise ProposalNotFound('Задание не найдено в этом пространстве')
        return self.decode(row)

    def insert(self, values):
        return self.insert_many([values])[0]

    def insert_many(self, entries):
        ids = []
        with self.conn:
            for values in entries:
                cur = self.conn.execute('INSERT INTO content_plan(workspace_id,created_at,topic,brief,scheduled_at,target_channel,with_video) VALUES(?,?,?,?,?,?,?)',
                    (self.wid, int(time.time()), values['topic'], values['brief'], values['scheduled_at'], values['target_channel'], int(values['with_video'])))
                ids.append(cur.lastrowid)
        return [self.get(iid) for iid in ids]

    def update(self, iid, **values):
        self.get(iid)
        allowed = {'topic','brief','scheduled_at','with_video','status','proposal_id','operation_id','payload','video_meta','dispatch','error'}
        if not values or not values.keys() <= allowed: raise ValueError('Invalid plan fields')
        values = {k: json.dumps(v, ensure_ascii=False) if k in ('payload','video_meta','dispatch') and v is not None else v for k,v in values.items()}
        with self.conn:
            self.conn.execute('UPDATE content_plan SET ' + ','.join(k+'=?' for k in values) + ' WHERE workspace_id=? AND id=?', (*values.values(), self.wid, iid))
        return self.get(iid)

    def cache_payload(self, iid, payload):
        item = self.get(iid)
        with self.conn:
            pid = item['proposal_id']
            if pid is None:
                pid = self.conn.execute('INSERT INTO proposals(workspace_id,created,title,text,original_text,sources,kind,target_channel) VALUES(?,?,?,?,?,\'[]\',\'planned\',?)',
                    (self.wid, int(time.time()), payload['title'], payload['text'], payload['text'], item['target_channel'])).lastrowid
            self.conn.execute('UPDATE content_plan SET payload=?,proposal_id=? WHERE workspace_id=? AND id=?',
                (json.dumps(payload, ensure_ascii=False), pid, self.wid, iid))
        return self.get(iid)

    def for_proposal(self, pid):
        row = self.conn.execute('SELECT * FROM content_plan WHERE workspace_id=? AND proposal_id=?', (self.wid,pid)).fetchone()
        if row is None: raise ProposalNotFound('Задание предложения не найдено')
        return self.decode(row)

    def published(self, iid, dispatch):
        item = self.get(iid)
        with self.conn:
            self.conn.execute("UPDATE content_plan SET status='published',dispatch=?,error=NULL WHERE workspace_id=? AND id=?", (json.dumps(dispatch),self.wid,iid))
            self.conn.execute("UPDATE proposals SET status='published',published_at=? WHERE workspace_id=? AND id=?", (int(time.time()),self.wid,item['proposal_id']))

    def listing(self, limit=200):
        total = self.conn.execute('SELECT COUNT(*) FROM content_plan WHERE workspace_id=?', (self.wid,)).fetchone()[0]
        rows = self.conn.execute('SELECT * FROM content_plan WHERE workspace_id=? ORDER BY scheduled_at DESC,id DESC LIMIT ?', (self.wid, limit)).fetchall()
        return [self.decode(row) for row in rows], total
