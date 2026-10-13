"""All-history, workspace-local aggregation of recorded report totals."""
import json
import unittest
from db import DB
from core import App

class SpendingTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.db = DB(':memory:'); self.app = App(self.db)
        self.other = self.app.workspace(self.db.create_workspace('Sport'))
    async def asyncTearDown(self): self.db.conn.close()
    def report(self, db, table, cost, complete=True, known='0', status='completed'):
        report={'started_at':1,'finished_at':2,'status':status,'cost_usd':cost,
                'cost_complete':complete,'known_cost_usd':known,
                'attempts':[{'cost_usd':'9999'}], 'workspace_id':999}
        rid = (db.start_cycle if table=='cycle' else db.start_operation)(report)
        report['id'] = rid
        (db.save_cycle if table=='cycle' else db.save_operation)(rid, report)
        return rid
    async def test_workspace_totals_use_each_report_once_with_precise_partial_and_unknown_costs(self):
        self.assertTrue(callable(getattr(self.db, 'recorded_spending', None)), 'Missing workspace spending total')
        self.report(self.db,'cycle','0.000000000000000001')
        self.report(self.db,'operation','0.000000000000000002')
        self.report(self.db,'cycle',None,False,'0.125')
        self.report(self.db,'operation','0')
        self.report(self.db,'operation',None,False,'0',status='running')
        self.report(self.db,'operation','NaN',False,'bad')
        for _ in range(25): self.report(self.db,'cycle','0.01')
        self.report(self.other.db,'cycle','3.25')
        self.report(self.other.db,'operation',None,False,'0.50')
        a=self.db.recorded_spending(); b=self.other.db.recorded_spending()
        self.assertEqual(a['known_cost_usd'],'0.375000000000000003')
        self.assertEqual(a['incomplete_records'],3)
        self.assertEqual(a['records'],31)
        self.assertEqual(b['known_cost_usd'],'3.75')
        self.assertEqual(b['incomplete_records'],1)
        self.assertEqual(self.app.state()['recorded_spending'],a)
        self.assertEqual(self.other.state()['recorded_spending'],b)
        # Record IDs collide across tables; both different operations count.
        self.assertEqual(self.db.recorded_spending(),a)
    async def test_empty_history_is_explicitly_recorded_zero_not_complete_billing(self):
        self.assertTrue(callable(getattr(self.db, 'recorded_spending', None)), 'Missing workspace spending total')
        self.assertEqual(self.db.recorded_spending(), {'known_cost_usd':'0','incomplete_records':0,'records':0,'scope':'recorded_reports_only'})

    async def test_authenticated_workspace_list_exposes_scoped_totals_without_secrets(self):
        from aiohttp.test_utils import TestClient, TestServer
        from web import build_web
        self.report(self.db,'cycle','0.125')
        self.report(self.other.db,'operation','3.50')
        self.db.set('llm_api_key','PRIVATE-SENTINEL')
        async with TestClient(TestServer(build_web(self.app,'offline-password'))) as client:
            self.assertEqual((await client.get('/api/workspaces')).status,401)
            await client.post('/api/login',json={'password':'offline-password'})
            response = await client.get('/api/workspaces')
            body = await response.json()
            self.assertNotIn('PRIVATE-SENTINEL', json.dumps(body))
            rows = {w['id']:w for w in body['workspaces']}
            self.assertIn('recorded_spending', rows[1])
            self.assertEqual(rows[1]['recorded_spending']['known_cost_usd'], '0.125')
            self.assertEqual(rows[self.other.db.workspace_id]['recorded_spending']['known_cost_usd'], '3.50')
            self.assertEqual((await client.get('/api/workspaces',headers={'X-Workspace-ID':'999'})).status,404)
            self.assertEqual((await client.post('/api/settings',json={'publication_hashtags':'#IT'})).status,403)
            hs={'X-Panel':'1','X-Workspace-ID':str(self.other.db.workspace_id)}
            self.assertEqual((await client.post('/api/settings',json={'publication_hashtags':'#IT'},headers=hs)).status,200)
            self.assertEqual(self.other.db.get('publication_hashtags'),'#IT')
            self.assertEqual(self.db.get('publication_hashtags'),'')

    async def test_real_report_partial_attempts_are_counted_only_via_operation_total(self):
        from cycle_cost import CycleReport
        from types import SimpleNamespace
        cycle=CycleReport(self.db, True)
        attempt=cycle.attempt('openrouter','offline','propose','','')
        cycle.response(attempt,SimpleNamespace(usage=SimpleNamespace(prompt_tokens=1,completion_tokens=1,cost='0.125')),provider_cost_supported=True)
        cycle.attempt('openrouter','offline','retry','','')
        cycle.finish('failed')
        standalone=CycleReport(self.db,True,'character_prompt')
        attempt=standalone.attempt('openrouter','offline','character_prompt','','')
        standalone.response(attempt,SimpleNamespace(usage=SimpleNamespace(prompt_tokens=1,completion_tokens=1,cost='0.25')),provider_cost_supported=True)
        standalone.finish('completed')
        self.assertEqual(self.db.recorded_spending(),{'known_cost_usd':'0.375','incomplete_records':1,'records':2,'scope':'recorded_reports_only'})
        self.assertEqual(self.other.db.recorded_spending()['known_cost_usd'],'0')

if __name__=='__main__': unittest.main()
