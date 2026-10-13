"""Task-independent SQLite scope for the application's fixed SQL vocabulary.

Every view keeps its immutable numeric ID. No connection-global selected workspace,
no second database/Telegram client. SQL is authored by DB methods, never by users.
Reads scope every nested FROM; writes scope their target as well as nested reads.
Schema/credentials use the root's private raw connection, not this facade.
"""
import re

TABLES = ('channels', 'posts', 'proposals', 'events', 'collection_cycles', 'llm_operations')
NAMES = '|'.join(TABLES)

class ScopedConnection:
    def __init__(self, raw, workspace_id):
        self.raw = raw
        self.workspace_id = int(workspace_id)

    def sql(self, sql):
        wid = self.workspace_id
        # Include rowid because legacy channel ordering uses it.
        sql = re.sub(r'\b(FROM|JOIN)\s+(' + NAMES + r')\b',
            lambda m: f'{m[1]} (SELECT rowid AS rowid,* FROM main.{m[2]} WHERE workspace_id={wid}) AS {m[2]}',
            sql, flags=re.I)
        insert = re.match(r'(INSERT(?: OR \w+)? INTO) (' + NAMES + r')\s*\(([^)]+)\)\s*VALUES\s*\(', sql, re.I)
        if insert:
            sql = sql[:insert.start()] + f'{insert[1]} {insert[2]}(workspace_id,{insert[3]}) VALUES({wid},' + sql[insert.end():]
        elif re.match(r'UPDATE\s+(' + NAMES + r')\b', sql, re.I):
            # WHERE refers to target; nested FROM expressions already scoped.
            match = re.search(r'\bWHERE\b', sql, re.I)
            if match:
                sql = sql[:match.end()] + f' workspace_id={wid} AND ' + sql[match.end():]
            else:
                sql += f' WHERE workspace_id={wid}'
        elif re.match(r'DELETE FROM \(', sql, re.I):
            # Undo substitution only at the DELETE target, keep subqueries scoped.
            pattern = r'^DELETE FROM \(SELECT rowid AS rowid,\* FROM main\.(\w+) WHERE workspace_id=\d+\) AS \w+'
            match = re.match(pattern, sql, re.I)
            if not match:
                raise ValueError('Unsupported scoped DELETE')
            rest = sql[match.end():]
            if re.match(r'\s*WHERE\b', rest, re.I):
                rest = re.sub(r'^\s*WHERE\b', f' WHERE workspace_id={wid} AND', rest, count=1, flags=re.I)
            else:
                rest = f' WHERE workspace_id={wid}' + rest
            sql = f'DELETE FROM {match[1]}' + rest
        return sql

    def execute(self, sql, params=()):
        return self.raw.execute(self.sql(sql), params)

    def executemany(self, sql, params):
        return self.raw.executemany(self.sql(sql), params)

    def commit(self): return self.raw.commit()
    def close(self): return self.raw.close()
    def __enter__(self): self.raw.__enter__(); return self
    def __exit__(self, *args): return self.raw.__exit__(*args)
