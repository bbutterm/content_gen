import asyncio
import json
import unittest
from unittest.mock import patch
try:
    import httpx
except ImportError:
    import httpx2 as httpx
from openai import AsyncOpenAI
from core import App
from db import DB
from llm import LLM

class CycleTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.db = DB(':memory:')
        self.app = App(self.db)
        self.app.user = object()
        self.db.set('llm_api_key', 'fixture-not-a-secret')
        self.db.set('llm_model', 'fixture-model')
        self.clients = []
    async def asyncTearDown(self):
        for c in self.clients:
            await c.close()
        self.db.conn.close()
    def llm_boundary(self, handler):
        def factory(**kw):
            kw['http_client'] = httpx.AsyncClient(transport=httpx.MockTransport(handler))
            client = AsyncOpenAI(**kw)
            self.clients.append(client)
            return client
        return patch('llm.AsyncOpenAI', factory)
    async def test_tariff_usage_is_postfactum_and_sanitized(self):
        await self.app.save_settings({'llm_input_usd_per_million': '2.5',
                                      'llm_output_usd_per_million': '10'})
        self.db.add_post('fixture', 1, 9999999999, 'source', 'fixture-link')
        async def collect(*args):
            return 1
        def handler(request):
            return httpx.Response(200, json={'id': 'fixture-id', 'object': 'chat.completion',
                'created': 1, 'model': 'fixture-model', 'choices': [{'index': 0,
                'message': {'role': 'assistant', 'content': '{"proposals":[]}'},
                'finish_reason': 'stop'}], 'usage': {'prompt_tokens': 100, 'completion_tokens': 20,
                                                   'total_tokens': 120}})
        with patch('core.collect', collect), self.llm_boundary(handler):
            await self.app.run_cycle()
        report = self.app.state()['cycle_cost']['last_completed']
        self.assertEqual(report['calls'], 1)
        self.assertEqual(report['input_tokens'], 100)
        self.assertEqual(report['output_tokens'], 20)
        self.assertEqual(report['cost_usd'], '0.00045')
        self.assertEqual(report['provenance'], 'tariff_estimate')
        self.assertEqual(report['attempts'][0]['stage'], 'propose')
        self.assertNotIn('source', json.dumps(report))
        self.assertNotIn('fixture-not-a-secret', json.dumps(report))

    async def test_http_retries_and_json_fallback_are_individual_unknown_attempts(self):
        await self.app.save_settings({'llm_input_usd_per_million': '2.5',
                                      'llm_output_usd_per_million': '10'})
        self.db.add_post('fixture', 1, 9999999999, 'source', '')
        seen = []
        def handler(request):
            seen.append(json.loads(request.content))
            if len(seen) == 1:
                return httpx.Response(500, json={'error': {'message': 'fixture private body'}})
            if len(seen) == 2:
                return httpx.Response(400, json={'error': {'message': 'json unsupported'}})
            return httpx.Response(200, json={'id':'fixture', 'created':1, 'model':'fixture-model',
                'object':'chat.completion', 'choices':[{'index':0, 'message':{'role':'assistant',
                'content':'{"proposals":[]}'},'finish_reason':'stop'}],
                'usage':{'prompt_tokens':100,'completion_tokens':20,'total_tokens':120}})
        async def collect(*a):
            return 0
        with patch('core.collect', collect), self.llm_boundary(handler):
            await self.app.run_cycle()
        report = self.app.state()['cycle_cost']['last_completed']
        self.assertEqual(len(seen), 3)
        self.assertEqual(report['calls'], 3)
        self.assertEqual([a['status'] for a in report['attempts']], ['failed','failed','completed'])
        self.assertIsNone(report['cost_usd'])
        self.assertEqual(report['known_cost_usd'], '0.00045')
        self.assertEqual(report['provenance'], 'unknown/incomplete')
        self.assertFalse(report['tokens_complete'])
        self.assertNotIn('private body', json.dumps(report))
        self.assertNotIn('response_format', seen[-1])

    async def test_provider_reported_cost_takes_priority_including_explicit_zero(self):
        await self.app.save_settings({'llm_provider':'openrouter',
            'llm_input_usd_per_million':'2.5','llm_output_usd_per_million':'10'})
        async def collect(*a):
            return 0
        costs = ['0.003', '0']
        def handler(request):
            return httpx.Response(200, json={'id':'fixture', 'created':1, 'model':'fixture-model',
                'object':'chat.completion', 'choices':[{'index':0,'message':{'role':'assistant',
                'content':'{"proposals":[]}'},'finish_reason':'stop'}],
                'usage':{'prompt_tokens':100,'completion_tokens':20,'total_tokens':120,'cost':costs.pop(0)}})
        with patch('core.collect', collect), self.llm_boundary(handler):
            for expected in ('0.003', '0'):
                self.db.add_post('fixture', len(costs), 9999999999, 'source','')
                await self.app.run_cycle()
                report = self.app.state()['cycle_cost']['last_completed']
                self.assertEqual(report['cost_usd'], expected)
                self.assertEqual(report['provenance'], 'provider_reported')

    async def test_local_no_bill_requires_explicit_setting(self):
        await self.app.save_settings({'llm_provider':'custom','llm_base_url':'http://127.0.0.1:11434/v1'})
        async def collect(*a):
            return 0
        def handler(r):
            return httpx.Response(200,json={'id':'fixture','created':1,'model':'fixture-model',
                'object':'chat.completion','choices':[{'index':0,'message':{'role':'assistant',
                'content':'{"proposals":[]}'},'finish_reason':'stop'}]})
        with patch('core.collect', collect), self.llm_boundary(handler):
            self.db.add_post('fixture',1,9999999999,'source','')
            await self.app.run_cycle()
            self.assertIsNone(self.app.state()['cycle_cost']['last_completed']['cost_usd'])
            await self.app.save_settings({'llm_local_no_bill':'1'})
            self.db.add_post('fixture',2,9999999999,'source','')
            await self.app.run_cycle()
        report = self.app.state()['cycle_cost']['last_completed']
        self.assertEqual(report['cost_usd'], '0')
        self.assertEqual(report['provenance'], 'local_no_bill')
        self.assertFalse(report['tokens_complete'])

    async def test_model_change_does_not_reuse_old_tariffs(self):
        await self.app.save_settings({'llm_input_usd_per_million':'2','llm_output_usd_per_million':'3'})
        await self.app.save_settings({'llm_model':'another'})
        self.assertEqual(self.db.get('llm_input_usd_per_million'),'')
        self.assertEqual(self.db.get('llm_output_usd_per_million'),'')
        await self.app.save_settings({'llm_input_usd_per_million':'2','llm_output_usd_per_million':'3'})
        await self.app.save_settings({'llm_model':'yet-another','llm_input_usd_per_million':'2',
                                      'llm_output_usd_per_million':'3'})
        self.assertEqual(self.db.get('llm_input_usd_per_million'),'')
        self.assertEqual(self.db.get('llm_output_usd_per_million'),'')

    async def test_interrupted_report_is_not_forever_running(self):
        from cycle_cost import CycleReport
        import tempfile
        from pathlib import Path
        with tempfile.TemporaryDirectory() as tmp:
            db = DB(str(Path(tmp)/'fixture.db'))
            cycle = CycleReport(db,False)
            cycle.attempt('openai','fixture','propose','','')
            db.conn.close()
            db = DB(str(Path(tmp)/'fixture.db'))
            app = App(db)
            report = app.state()['cycle_cost']
            self.assertIsNone(report['running'])
            self.assertEqual(report['last_completed']['status'],'interrupted')
            self.assertIsNone(report['last_completed']['cost_usd'])
            db.conn.close()

    async def test_failed_cycle_and_missing_usage_are_not_zero(self):
        self.db.add_post('fixture',1,9999999999,'source','')
        async def collect(*a): return 0
        def handler(r):
            return httpx.Response(200,json={'id':'fixture','created':1,'model':'fixture-model',
                'object':'chat.completion','choices':[{'index':0,'message':{'role':'assistant',
                'content':'not json'},'finish_reason':'stop'}]})
        with patch('core.collect',collect),self.llm_boundary(handler):
            with self.assertRaises(ValueError): await self.app.run_cycle()
        report=self.app.state()['cycle_cost']['last_completed']
        self.assertEqual(report['status'],'failed')
        self.assertEqual(report['calls'],1)
        self.assertIsNone(report['cost_usd'])
        self.assertFalse(report['tokens_complete'])

    async def test_running_scope_excludes_concurrent_test_call(self):
        entered=asyncio.Event(); release=asyncio.Event()
        async def collect(*a):
            entered.set(); await release.wait(); return 0
        def handler(r):
            return httpx.Response(200,json={'id':'fixture','created':1,'model':'fixture-model',
                'object':'chat.completion','choices':[{'index':0,'message':{'role':'assistant',
                'content':'works'},'finish_reason':'stop'}],
                'usage':{'prompt_tokens':10,'completion_tokens':2,'total_tokens':12}})
        with patch('core.collect',collect),self.llm_boundary(handler):
            task=asyncio.create_task(self.app.run_cycle())
            await entered.wait()
            self.assertEqual(self.app.state()['cycle_cost']['running']['status'],'running')
            await self.app.test_llm()
            release.set();await task
        self.assertEqual(self.app.state()['cycle_cost']['last_completed']['calls'],0)

    async def test_empty_cycle_persists_zero_without_claiming_model_is_free(self):
        self.assertIsNone(self.app.state().get('cycle_cost', {}).get('last_completed'))
        async def collect(*args):
            return 0
        with patch('core.collect', collect), self.llm_boundary(lambda r: self.fail('No LLM call expected')):
            await self.app.run_cycle()
        report = self.app.state()['cycle_cost']['last_completed']
        self.assertEqual(report['status'], 'empty')
        self.assertEqual(report['calls'], 0)
        self.assertEqual(report['cost_usd'], '0')
        self.assertEqual(report['provenance'], 'no_calls')
        self.assertEqual(len(self.app.state()['cycle_cost']['history']), 1)

if __name__ == '__main__':
    unittest.main(verbosity=2)
