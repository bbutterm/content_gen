import unittest
from aiohttp.test_utils import TestClient, TestServer
from core import App
from db import DB
from web import build_web
import prompts

class PersonaApiTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.db = DB(':memory:')
        self.app = App(self.db)
        self.client = TestClient(TestServer(build_web(self.app,'fixture-password')))
        await self.client.start_server()
        r = await self.client.post('/api/login',json={'password':'fixture-password'})
        self.cookie = 'nb_session=' + r.cookies['nb_session'].value
        self.headers = {'Cookie':self.cookie, 'X-Panel':'1'}
        self.client.session.cookie_jar.clear()
    async def asyncTearDown(self):
        await self.client.close()
        self.db.conn.close()
    async def test_history_route_and_tariff_settings_are_guarded(self):
        r = await self.client.get('/api/cycles')
        self.assertEqual(r.status,401)
        r = await self.client.get('/api/cycles',headers=self.headers)
        self.assertEqual(r.status,200)
        self.assertEqual((await r.json())['history'],[])
        r = await self.client.post('/api/settings',json={'llm_input_usd_per_million':'2.5'},
                                   headers={'Cookie':self.cookie})
        self.assertEqual(r.status,403)
        r = await self.client.post('/api/settings',json={'llm_input_usd_per_million':'2.5'},headers=self.headers)
        self.assertEqual(r.status,200)
        r = await self.client.get('/api/state',headers=self.headers)
        self.assertEqual((await r.json())['settings']['llm_input_usd_per_million'],'2.5')
        for bad in ('abc','NaN','Infinity','-1','0','1e999','1e-999999'):
            r = await self.client.post('/api/settings',json={'llm_output_usd_per_million':bad},headers=self.headers)
            self.assertEqual(r.status,400,bad)
        for query in ('?before=bad','?before=-1'):
            r = await self.client.get('/api/cycles'+query,headers=self.headers)
            self.assertEqual(r.status,400)

    async def test_patriotic_persona_is_persisted_and_used_in_both_prompts(self):
        r = await self.client.post('/api/settings',json={'persona':'patriotic_ru'},headers=self.headers)
        self.assertEqual(r.status,200)
        r = await self.client.get('/api/state',headers=self.headers)
        state = await r.json()
        p = state['personas']['patriotic_ru']
        self.assertEqual(p['name'],'Патриотический пророссийский')
        self.assertEqual(state['settings']['persona'],'patriotic_ru')
        self.assertIn(p['text'],prompts.build_propose(self.db.get,3))
        self.assertIn(p['text'],prompts.build_rewrite(self.db.get))
        for term in ('источников','мнения','цитаты','дегуманизации','манипуляции'):
            self.assertIn(term,p['text'])
        self.assertEqual(self.db.get('auto_publish'),'0')
        self.assertEqual(self.db.get('mod.breaking.auto_publish'),'')

if __name__ == '__main__': unittest.main(verbosity=2)
