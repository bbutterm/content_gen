import asyncio
import unittest
from unittest.mock import patch
from aiohttp.test_utils import TestClient, TestServer
from core import App
from db import DB
from web import build_web

NOW = 1700000000

class DatabaseResetTests(unittest.TestCase):
    def setUp(self):
        self.db = DB(':memory:')
        for i, age in enumerate((0, 3600, 3601, 86400, 86401), 1):
            self.db.add_post('fixture', i, NOW-age, 'original', 'fixture-link')
        self.db.mark_all_used()
        self.db.add_post('fixture', 6, NOW, 'pending', 'fixture-link')
    def tearDown(self):
        self.db.conn.close()
    def test_only_used_posts_with_inclusive_boundary_are_reset(self):
        self.assertEqual(self.db.reanalysis_count(NOW-3600), 2)
        self.assertEqual(self.db.reset_analysis(NOW-3600), 2)
        self.assertEqual(self.db.reset_analysis(NOW-3600), 0)
        self.assertEqual(self.db.pending_count(), 3)
        self.assertEqual(self.db.reset_analysis(NOW-86400), 2)
        self.assertEqual(self.db.reset_analysis(None), 1)
        self.assertEqual(self.db.pending_count(), 6)

    def test_content_reset_rolls_back_every_table_if_transaction_fails(self):
        self.db.add_proposal('keep', 'draft', [])
        self.db.add_event('info', 'keep')
        self.db.conn.execute("CREATE TRIGGER block_event_delete BEFORE DELETE ON events BEGIN SELECT RAISE(ABORT, 'fixture'); END")
        with self.assertRaises(Exception):
            self.db.reset_test_data()
        self.assertEqual(self.db._one('SELECT COUNT(*) FROM posts'), 6)
        self.assertEqual(self.db._one('SELECT COUNT(*) FROM proposals'), 1)
        self.assertEqual(self.db._one('SELECT COUNT(*) FROM events'), 1)

class ReanalysisApiTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.db = DB(':memory:')
        self.app = App(self.db)
        self.db.add_channel('fixture')
        self.db.channel_fetched('fixture', 99, 'Fixture', 0)
        self.db.set('user_session', 'synthetic-session')
        self.db.set('paused', '1')
        for i, age in enumerate((0, 3600, 3601, 86400, 86401), 1):
            self.db.add_post('fixture', i, NOW-age, 'original', 'link')
        self.db.mark_all_used()
        for status in ('new', 'published', 'skipped'):
            pid = self.db.add_proposal(status, 'draft', [('fixture', 1)])
            self.db._exec('UPDATE proposals SET status=? WHERE id=?', (status, pid))
        self.db.add_event('info', 'synthetic event')
        self.client = TestClient(TestServer(build_web(self.app, 'fixture-password')))
        await self.client.start_server()
        r = await self.client.post('/api/login', json={'password':'fixture-password'})
        self.cookie = {'Cookie':'nb_session='+r.cookies['nb_session'].value}
        self.headers = {**self.cookie, 'X-Panel':'1'}
        self.client.session.cookie_jar.clear()
    async def asyncTearDown(self):
        await self.client.close()
        self.db.conn.close()
    def snapshot(self):
        return {table:[tuple(r) for r in self.db.conn.execute('SELECT * FROM '+table)]
                for table in ('settings','channels','proposals','events','collection_cycles')}
    async def test_guarded_preview_and_reset_preserve_other_records(self):
        before = self.snapshot()
        with patch('core.time.time', return_value=NOW):
            r = await self.client.get('/api/posts/reanalyse?period=hour')
            self.assertEqual(r.status, 401)
            r = await self.client.get('/api/posts/reanalyse?period=hour', headers=self.headers)
            self.assertEqual(r.status, 200)
            self.assertEqual((await r.json())['count'], 2)
            r = await self.client.post('/api/posts/reanalyse', json={'period':'hour'}, headers=self.cookie)
            self.assertEqual(r.status, 403)
            for period, count in (('hour',2), ('hour',0), ('day',2), ('all',1)):
                r = await self.client.post('/api/posts/reanalyse', json={'period':period}, headers=self.headers)
                self.assertEqual(r.status, 200)
                self.assertEqual((await r.json())['affected'], count)
        self.assertEqual(before, self.snapshot())
        self.assertFalse(self.app._tasks)
    async def test_test_data_reset_requires_exact_confirmation_and_preserves_configuration(self):
        for key in ('tg_api_id','tg_api_hash','bot_token','llm_api_key','bot_session','persona','mod.breaking.auto_publish','auto_publish'):
            self.db.set(key, 'synthetic-'+key)
        self.db.set('last_run', '123')
        self.db.set('last_result', '{"posts":5}')
        self.db._exec("INSERT INTO collection_cycles(started_at,status,report) VALUES(1,'complete','{}')")
        settings = {r['key']:r['value'] for r in self.db.conn.execute('SELECT * FROM settings')
                    if r['key'] not in ('last_run','last_result')}
        r = await self.client.get('/api/test-data/reset')
        self.assertEqual(r.status, 401)
        r = await self.client.get('/api/test-data/reset', headers=self.headers)
        self.assertEqual(r.status, 200)
        self.assertEqual((await r.json())['counts'], {'posts':5,'proposals':3,'events':1,'collection_cycles':1})
        r = await self.client.post('/api/test-data/reset', json={'confirmation':'СБРОС'}, headers=self.cookie)
        self.assertEqual(r.status, 403)
        for confirmation in ('', 'сброс', 'СБРОС ', None):
            r = await self.client.post('/api/test-data/reset', json={'confirmation':confirmation}, headers=self.headers)
            self.assertEqual(r.status, 400)
        async with self.app.lock:
            r = await self.client.post('/api/test-data/reset', json={'confirmation':'СБРОС'}, headers=self.headers)
            self.assertEqual(r.status, 409)
        r = await self.client.post('/api/test-data/reset', json={'confirmation':'СБРОС'}, headers=self.headers)
        self.assertEqual(r.status, 200)
        self.assertEqual((await r.json())['deleted'], {'posts':5,'proposals':3,'events':1,'collection_cycles':1})
        for table in ('posts','proposals','events','collection_cycles'):
            self.assertEqual(self.db._one('SELECT COUNT(*) FROM '+table), 0)
        self.assertEqual(settings, {r['key']:r['value'] for r in self.db.conn.execute('SELECT * FROM settings')})
        channel = self.db.channels()[0]
        self.assertEqual(channel['username'], 'fixture')
        self.assertEqual(channel['title'], 'Fixture')
        self.assertEqual(channel['last_id'], 0)
        self.assertIsNone(channel['last_fetch'])
        self.assertEqual(channel['posts_total'], 0)
        self.assertFalse(self.app._tasks)
        r = await self.client.post('/api/test-data/reset', json={'confirmation':'СБРОС'}, headers=self.headers)
        self.assertEqual((await r.json())['deleted'], dict.fromkeys(('posts','proposals','events','collection_cycles'),0))

    async def test_reset_requires_paused_schedule_to_prevent_paid_autorun(self):
        self.db.set('paused', '0')
        for route, payload in (('/api/posts/reanalyse', {'period':'all'}),
                               ('/api/test-data/reset', {'confirmation':'СБРОС'})):
            before = self.snapshot()
            r = await self.client.post(route, json=payload, headers=self.headers)
            self.assertEqual(r.status, 409)
            self.assertEqual(self.db.pending_count(), 0)
            self.assertEqual(before, self.snapshot())
        self.assertFalse(self.app._tasks)

    async def test_concurrent_duplicate_requests_report_actual_changes_once(self):
        results = await asyncio.gather(*(self.app.reanalyse_posts('all') for _ in range(2)))
        self.assertEqual(sorted(results), [0, 5])
        self.assertFalse(self.app._tasks)

    async def test_invalid_period_and_busy_lock_do_not_mutate(self):
        for period in ('week', '', None, [], 1):
            r = await self.client.post('/api/posts/reanalyse', json={'period':period}, headers=self.headers)
            self.assertEqual(r.status, 400)
        r = await self.client.get('/api/posts/reanalyse?period=week', headers=self.headers)
        self.assertEqual(r.status, 400)
        async with self.app.lock:
            r = await self.client.post('/api/posts/reanalyse', json={'period':'all'}, headers=self.headers)
            self.assertEqual(r.status, 409)
        self.assertEqual(self.db.pending_count(), 0)
        self.assertFalse(self.app.lock.locked())

if __name__ == '__main__': unittest.main(verbosity=2)
