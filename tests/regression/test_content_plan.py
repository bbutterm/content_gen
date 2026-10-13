"""Offline plan acceptance: real SQLite/App/routes, fake paid/Telegram boundaries."""
import asyncio
import json
import tempfile
import time
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
from zoneinfo import ZoneInfo

from aiohttp.test_utils import TestClient, TestServer
from core import App
from db import DB
from settings import UserError, ProposalNotFound
from web import build_web


class PlanTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = str(Path(self.tmp.name) / 'test.sqlite')
        self.db = DB(self.path)
        self.app = App(self.db)
        self.app.tz = ZoneInfo('Europe/Berlin')
        self.other = self.app.workspace(self.db.create_workspace('Other'))
        for db in (self.db, self.other.db):
            db.set('target_channel', 'out_' + str(db.workspace_id))
        self.client = TestClient(TestServer(build_web(self.app, 'offline-password')))
        await self.client.start_server()
        self.h = {'X-Panel': '1'}
        self.h2 = {**self.h, 'X-Workspace-ID': str(self.other.db.workspace_id)}

    async def asyncTearDown(self):
        for task in list(self.app._tasks): task.cancel()
        if self.app._tasks: await asyncio.gather(*self.app._tasks, return_exceptions=True)
        await self.client.close()
        self.db.conn.close()
        self.tmp.cleanup()

    async def login(self):
        await self.client.post('/api/login', json={'password': 'offline-password'})

    def data(self, **extra):
        return {'topic': ' BPMN → UML / Kafka ', 'brief': 'Owner notes',
                'scheduled_local': (datetime.now(self.app.tz) + timedelta(days=2)).strftime('%Y-%m-%dT%H:%M'),
                'with_video': True, **extra}

    async def test_create_list_scoped_paused_snapshot_without_paid_calls(self):
        route = '/api/content-plan'
        self.assertEqual((await self.client.get(route)).status, 401)
        await self.login()
        self.assertEqual((await self.client.post(route, json=self.data())).status, 403)
        response = await self.client.post(route, json=self.data(), headers=self.h)
        self.assertEqual(response.status, 200, await response.text())
        item = (await response.json())['item']
        self.assertEqual(item['topic'], 'BPMN → UML / Kafka')
        self.assertEqual(item['target_channel'], 'out_1')
        self.assertEqual(item['status'], 'scheduled')
        self.assertIsNone(item['proposal_id'])
        self.db.set('target_channel', 'changed_out')
        state = await (await self.client.get(route)).json()
        self.assertTrue(state['paused'])
        self.assertEqual(state['timezone'], 'Europe/Berlin')
        self.assertEqual(state['total'], 1)
        self.assertFalse(state['has_more'])
        self.assertEqual(state['items'][0]['target_channel'], 'out_1')
        self.assertEqual((await (await self.client.get(route, headers=self.h2)).json())['items'], [])
        self.assertEqual(self.db.recorded_spending()['records'], 0)
        self.assertEqual(self.db.proposals(), [])
    async def test_local_schedule_validation_bulk_atomic_edit_and_cancel(self):
        await self.login()
        route = '/api/content-plan'
        bads = [{'topic': ''}, {'topic': 'a'*301}, {'brief': 'b'*4001},
                {'with_video': 'false'}, {'scheduled_local': 'tomorrow'},
                {'scheduled_local': '2099-03-29T02:30'},
                {'scheduled_local': '2099-10-25T02:30'},
                {'scheduled_local': '2000-01-01T12:00'}]
        # Use known Berlin DST dates instead of assuming future calendar dates.
        bads[5]['scheduled_local'] = '2030-03-31T02:30'
        bads[6]['scheduled_local'] = '2030-10-27T02:30'
        for bad in bads:
            r = await self.client.post(route, json=self.data(**bad), headers=self.h)
            self.assertEqual(r.status, 400, (bad, await r.text()))
        self.other.db.set('target_channel', '')
        self.assertEqual((await self.client.post(route,json=self.data(),headers=self.h2)).status,400)
        day = (datetime.now(self.app.tz)+timedelta(days=5)).strftime('%Y-%m-%d')
        bulk = {'start_date': day, 'time': '12:00', 'topics': ['BPMN', '', 'Kafka'], 'with_video': True}
        self.assertEqual((await self.client.post(route+'/bulk',json=bulk,headers=self.h)).status,400)
        self.assertEqual((await (await self.client.get(route)).json())['total'],0)
        bulk['topics'] = 'BPMN\nUML\nKafka'
        r = await self.client.post(route+'/bulk',json=bulk,headers=self.h)
        self.assertEqual(r.status,200,await r.text())
        items = (await r.json())['items']
        self.assertEqual([i['topic'] for i in items],['BPMN','UML','Kafka'])
        self.assertEqual([i['scheduled_local'] for i in items],
            [(datetime.strptime(day,'%Y-%m-%d')+timedelta(days=n)).strftime('%Y-%m-%dT12:00') for n in range(3)])
        iid = items[0]['id']
        for value in (str(iid), '01', '-1', '1.0', '9999'):
            r=await self.client.post(route+'/'+value,json=self.data(topic='EDIT'),headers=self.h2)
            self.assertIn(r.status,(400,404))
        r=await self.client.post(route+f'/{iid}',json=self.data(topic='EDIT'),headers=self.h)
        self.assertEqual(r.status,200,await r.text())
        self.db.set('target_channel','new_out')
        self.assertEqual((await r.json())['item']['target_channel'],'out_1')
        r=await self.client.post(route+f'/{iid}/cancel',json={},headers=self.h)
        self.assertEqual(r.status,200,await r.text())
        self.assertEqual((await r.json())['item']['status'],'cancelled')
        self.assertEqual((await self.client.post(route+f'/{iid}',json=self.data(),headers=self.h)).status,400)
        before=(self.db.get('paused'),self.db.get('auto_publish'))
        r=await self.client.post(route+'/pause',json={'paused':False},headers=self.h)
        self.assertEqual(r.status,200,await r.text())
        self.assertEqual(self.db.get('plan_paused'),'0')
        self.assertEqual(before,(self.db.get('paused'),self.db.get('auto_publish')))
        self.assertEqual(self.db.recorded_spending()['records'],0)
    async def test_plan_settings_isolated_masked_and_tariffs_invalidated(self):
        await self.login()
        self.db.set('llm_input_usd_per_million','1')
        self.other.db.set('llm_input_usd_per_million','2')
        r=await self.client.post('/api/settings',json={'plan_model':'owner-model','plan_input_usd_per_million':'3',
            'plan_output_usd_per_million':'4','plan_post_max_len':2500,'plan_api_key':'PLAN-SECRET-SENTINEL'},headers=self.h2)
        self.assertEqual(r.status,200,await r.text())
        state=await (await self.client.get('/api/state',headers=self.h2)).json()
        self.assertEqual(state['settings']['plan_model'],'owner-model')
        self.assertEqual(state['settings']['plan_paused'],'1')
        self.assertEqual(state['settings']['plan_provider'],'shared')
        self.assertEqual(state['settings']['plan_api_key_hint'],'…INEL')
        self.assertNotIn('PLAN-SECRET-SENTINEL',json.dumps(state))
        self.assertNotIn('plan_api_key',state['settings'])
        self.assertEqual(self.db.get('plan_model'),'')
        self.assertEqual(self.db.get('plan_api_key'),'PLAN-SECRET-SENTINEL')
        for bad in ({'plan_provider':'unknown'},{'plan_input_usd_per_million':'NaN'},
                    {'plan_post_max_len':3501},{'plan_paused':'maybe'}):
            self.assertEqual((await self.client.post('/api/settings',json=bad,headers=self.h)).status,400)
        await self.other.save_settings({'plan_model':'changed'})
        self.assertEqual(self.other.db.get('plan_input_usd_per_million'),'')
        self.db.set('plan_input_usd_per_million','5')
        self.other.db.set('plan_input_usd_per_million','6')
        await self.other.save_settings({'plan_provider':'anthropic'})
        self.assertEqual(self.db.get('plan_input_usd_per_million'),'')
        self.assertEqual(self.other.db.get('plan_input_usd_per_million'),'')
        self.assertEqual(self.db.get('llm_input_usd_per_million'),'1')
        self.assertEqual(self.other.db.get('llm_input_usd_per_million'),'2')
        self.assertEqual(self.db.get('llm_provider'),'openai')
        self.assertEqual(self.db.recorded_spending()['records'],0)
    def payload(self):
        return {'title':'Kafka: очередь событий', 'text':'**Kafka: очередь событий**\n\n📰 Kafka хранит поток событий.\n\n#Kafka #Архитектура #Данные',
            'scenes':[{'title':name,'text':'Короткое объяснение','icon':'network','steps':['Producer','Broker','Consumer']} for name in ('Введение','Механизм','Пример','Вывод')]}

    async def test_explicit_shared_generation_one_call_caches_renders_records_cost(self):
        import httpx2
        import openai
        import llm
        from plan_store import PlanStore
        self.db.set('llm_api_key','offline-key')
        self.db.set('llm_provider','openrouter')
        self.db.set('llm_model','news-model')
        self.other.db.set('plan_model','plan-model')
        self.other.db.set('plan_input_usd_per_million','3')
        self.other.db.set('plan_output_usd_per_million','4')
        self.other.db.set('target_channel','intended_out')
        item=self.app.plan.create(self.other,self.data())
        self.other.db.set('target_channel','changed_out')
        calls=[]; rendered=[]
        def respond(request):
            calls.append(json.loads(request.content))
            return httpx2.Response(200,json={'id':'offline','object':'chat.completion','created':1,'model':'plan-model',
                'choices':[{'index':0,'finish_reason':'stop','message':{'role':'assistant','content':json.dumps(self.payload())}}],
                'usage':{'prompt_tokens':100,'completion_tokens':20,'cost':'0.125'}})
        def sdk(**kwargs):
            return openai.AsyncOpenAI(**kwargs,http_client=httpx2.AsyncClient(transport=httpx2.MockTransport(respond)))
        async def render(storyboard,path):
            cached=PlanStore(self.other.db).get(item['id'])
            self.assertEqual(cached['payload'],self.payload())
            self.assertEqual(cached['status'],'rendering')
            rendered.append(storyboard)
            Path(path).write_bytes(b'FAKE-MP4-TRANSPORT')
            return {'duration':24,'width':720,'height':1280,'fps':24,'bytes':18}
        await self.login()
        route=f"/api/content-plan/{item['id']}/generate"
        with patch.object(llm,'AsyncOpenAI',sdk), patch.object(self.app.plan,'render',render,create=True):
            r=await self.client.post(route,json={},headers=self.h2)
            self.assertEqual(r.status,202,await r.text())
            self.assertEqual((await self.client.post(route,json={},headers=self.h2)).status,400)
            await asyncio.gather(*list(self.app._tasks))
        state=self.app.plan.listing(self.other)['items'][0]
        self.assertEqual(state['status'],'ready')
        self.assertTrue(state['has_video'])
        self.assertEqual(len(calls),1)
        self.assertEqual(calls[0]['model'],'plan-model')
        self.assertNotIn('response_format',calls[0])
        self.assertEqual(rendered,[{'scenes':self.payload()['scenes']}])
        p=self.other.db.get_proposal(state['proposal_id'])
        self.assertEqual(p['kind'],'planned')
        self.assertEqual(p['target_channel'],'intended_out')
        self.assertEqual(p['text'],self.payload()['text'])
        report=self.other.db.operation_cost_state()['history'][0]
        self.assertEqual(report['operation_type'],'content_plan')
        self.assertEqual(report['calls'],1)
        self.assertEqual(report['cost_usd'],'0.125')
        self.assertEqual(state['cost']['cost_usd'],'0.125')
        self.assertEqual(self.db.recorded_spending()['records'],0)
        self.assertNotIn('offline-key',json.dumps(self.other.db.events()))
        self.assertEqual(self.db.get('llm_model'),'news-model')
    async def test_native_anthropic_shape_usage_cache_unknown_and_no_shared_mutation(self):
        import httpx2
        import plan_llm
        calls=[]
        self.db.set('plan_provider','anthropic')
        self.db.set('plan_api_key','NATIVE-SECRET-SENTINEL')
        self.db.set('plan_model','literal-owner-model')
        self.db.set('plan_input_usd_per_million','2')
        self.db.set('plan_output_usd_per_million','5')
        usage={'input_tokens':100,'output_tokens':20,'cache_creation_input_tokens':0,'cache_read_input_tokens':0}
        def respond(request):
            calls.append(request)
            return httpx2.Response(200,json={'content':[{'type':'text','text':json.dumps(self.payload())}],
                'usage':usage,'stop_reason':'end_turn'})
        real_client=httpx2.AsyncClient
        def client(**kwargs): return real_client(**kwargs,transport=httpx2.MockTransport(respond))
        with patch.object(plan_llm,'NativeClient',client,create=True):
            first=self.app.plan.create(self.app,self.data(with_video=False))
            self.app.plan.start_job(self.app,first['id'])
            await asyncio.gather(*list(self.app._tasks))
            self.assertEqual(self.app.plan.listing(self.app)['items'][0]['status'],'ready')
            report=self.db.operation_cost_state()['history'][0]
            self.assertEqual(report['calls'],1)
            self.assertEqual(report['input_tokens'],100)
            self.assertEqual(report['output_tokens'],20)
            self.assertEqual(report['cost_usd'],'0.0003')
            usage['cache_read_input_tokens']=1000
            second=self.app.plan.create(self.app,self.data(with_video=False))
            self.app.plan.start_job(self.app,second['id'])
            await asyncio.gather(*list(self.app._tasks))
            report=self.db.operation_cost_state()['history'][0]
            self.assertFalse(report['cost_complete'])
            self.assertIsNone(report['cost_usd'])
            self.assertEqual(report['input_tokens'],1100,'Cached input is still token usage')
        self.assertEqual(len(calls),2)
        request=calls[0]
        self.assertEqual(str(request.url),'https://api.anthropic.com/v1/messages')
        self.assertEqual(request.headers['x-api-key'],'NATIVE-SECRET-SENTINEL')
        self.assertEqual(request.headers['anthropic-version'],'2023-06-01')
        payload=json.loads(request.content)
        self.assertEqual(payload['model'],'literal-owner-model')
        self.assertGreater(payload['max_tokens'],0)
        self.assertIsInstance(payload['system'],str)
        self.assertTrue('{emoji}' not in payload['system'],'Unresolved emoji placeholder')
        self.assertTrue('outlook' not in payload['system'],'News-only schema leaked into plan prompt')
        self.assertEqual([m['role'] for m in payload['messages']],['user'])
        self.assertEqual(self.db.get('llm_api_key'),'')
        self.assertNotIn('NATIVE-SECRET-SENTINEL',json.dumps(self.db.operation_cost_state()))
        self.assertEqual(self.db.recorded_spending()['incomplete_records'],1)
    async def test_render_retry_reuses_cached_payload_and_video_route_rejects_symlinks(self):
        import plan_llm
        from plan_store import PlanStore
        payload=self.payload()
        item=self.app.plan.create(self.app,self.data())
        calls=[]
        async def generate(db,item): calls.append(item['id']); return payload
        async def broken(storyboard,path): raise RuntimeError('SECRET prompt body /root/private')
        with patch.object(plan_llm,'generate',generate),patch.object(self.app.plan,'render',broken):
            self.app.plan.start_job(self.app,item['id'])
            await asyncio.gather(*list(self.app._tasks))
        store=PlanStore(self.db)
        cached=store.get(item['id'])
        self.assertEqual(cached['status'],'error')
        self.assertEqual(cached['payload'],payload)
        self.assertNotIn('SECRET',cached['error'])
        async def render(storyboard,path):
            Path(path).write_bytes(b'FAKE-MP4')
            return {'duration':24,'width':720,'height':1280,'fps':24,'bytes':8,'path':'/root/private','token':'SECRET'}
        with patch.object(plan_llm,'generate',generate),patch.object(self.app.plan,'render',render):
            self.app.plan.start_job(self.app,item['id'],retry=True)
            await asyncio.gather(*list(self.app._tasks))
        self.assertEqual(calls,[item['id']])
        self.assertEqual(len(self.db.proposals()),1)
        public=self.app.plan.listing(self.app)['items'][0]
        self.assertEqual(public['status'],'ready')
        self.assertNotIn('SECRET',json.dumps(public))
        self.assertNotIn('/root/',json.dumps(public))
        route=f"/api/content-plan/{item['id']}/video"
        self.assertEqual((await self.client.get(route)).status,401)
        await self.login()
        self.assertEqual((await self.client.get(route,headers=self.h2)).status,404)
        r=await self.client.get(route)
        self.assertEqual(r.status,200,await r.text())
        self.assertEqual(r.headers['Cache-Control'],'no-store')
        self.assertEqual(await r.read(),b'FAKE-MP4')
        path=self.app.plan.artifact(self.app,item['id'])
        path.unlink()
        private=Path(self.tmp.name)/'private.mp4'; private.write_bytes(b'PRIVATE')
        path.symlink_to(private)
        self.assertFalse(self.app.plan.listing(self.app)['items'][0]['has_video'])
        self.assertEqual((await self.client.get(route)).status,404)
        path.unlink(); path.parent.rmdir()
        path.parent.symlink_to(Path(self.tmp.name),target_is_directory=True)
        self.assertEqual((await self.client.get(route)).status,404)
        with self.assertRaises(UserError): self.app.plan.artifact(self.app,item['id'],create=True)
    async def test_plan_tick_independent_responsive_bounded_and_missed_no_catchup(self):
        import plan_llm
        from plan_store import PlanStore
        self.db.set('paused','1'); self.other.db.set('paused','1')
        now=int(time.time())
        a=self.app.plan.create(self.app,self.data(with_video=False))
        b=self.app.plan.create(self.other,self.data(with_video=False))
        old=self.app.plan.create(self.app,self.data(with_video=False))
        for app,item in ((self.app,a),(self.other,b)):
            PlanStore(app.db).update(item['id'],scheduled_at=now-10)
        PlanStore(self.db).update(old['id'],scheduled_at=now-901)
        entered=asyncio.Event(); release=asyncio.Event(); calls=[]; active=0
        async def generate(db,item):
            nonlocal active
            active+=1; self.assertEqual(active,1)
            calls.append((db.workspace_id,item['id'])); entered.set()
            await release.wait(); active-=1
            return self.payload()
        with patch.object(plan_llm,'generate',generate):
            await self.app.scheduler_tick()
            self.assertEqual(self.app._tasks,set())
            self.db.set('plan_paused','0'); self.other.db.set('plan_paused','0')
            async with asyncio.timeout(1): await self.app.scheduler_tick()
            self.assertEqual(len(self.app.plan.jobs),2,'Due plans must spawn jobs without awaiting provider')
            async with asyncio.timeout(1): await entered.wait()
            for _ in range(3): await self.app.scheduler_tick()
            self.assertEqual(len(self.app.plan.jobs),2)
            self.assertEqual(PlanStore(self.db).get(old['id'])['status'],'missed')
            self.assertEqual(len(calls),1)
            # Only an already running preparation finishes after pause.
            self.db.set('plan_paused','1'); self.other.db.set('plan_paused','1')
            release.set(); await asyncio.gather(*list(self.app._tasks))
            self.assertEqual(len(calls),1)
            self.assertEqual(PlanStore(self.db).get(a['id'])['status'],'ready')
            self.assertEqual(PlanStore(self.other.db).get(b['id'])['status'],'scheduled')
            await self.app.scheduler_tick()
            self.assertEqual(len(calls),1)
        self.assertEqual(self.app.plan.jobs,{})
        self.assertEqual(self.db.get('paused'),'1')
    def ready(self, app=None, with_video=True, text=None):
        from plan_store import PlanStore
        app=app or self.app
        item=self.app.plan.create(app,self.data(with_video=with_video))
        payload=self.payload()
        if text is not None: payload['text']=text
        store=PlanStore(app.db)
        item=store.cache_payload(item['id'],payload)
        if with_video:
            path=self.app.plan.artifact(app,item['id'],create=True)
            path.write_bytes(b'FAKE-MP4')
            store.update(item['id'],video_meta={'duration':24,'width':720,'height':1280,'fps':24,'bytes':8})
        return store.update(item['id'],status='ready')

    async def test_planned_video_publish_bypasses_queue_shares_lock_and_reserves_caption(self):
        from plan_store import PlanStore
        from telethon.extensions import html as telegram_html
        sent=[]; entered=asyncio.Event(); release=asyncio.Event()
        async def send_file(target,path,**kwargs):
            entered.set(); await release.wait()
            sent.append(('video',target,kwargs)); return SimpleNamespace(id=51)
        async def send_message(target,text,**kwargs):
            sent.append(('text',target,{**kwargs,'text':text})); return SimpleNamespace(id=52)
        self.app.bot=SimpleNamespace(send_file=send_file,send_message=send_message)
        self.app.modules['queue'].save({'enabled':'1'})
        self.db.set('publication_hashtags','#ManagementOnly')
        item=self.ready()
        pid=item['proposal_id']
        first=asyncio.create_task(self.app.publish(pid))
        await asyncio.sleep(0)
        # Ready planned publication must use video, never module fallback.
        self.assertTrue(entered.is_set(),'Planned MP4 must be dispatched before text')
        second=asyncio.create_task(self.app.approve(pid))
        release.set()
        results=await asyncio.gather(first,second,return_exceptions=True)
        self.assertEqual(sum(isinstance(r,Exception) for r in results),1)
        self.assertEqual([x[0] for x in sent],['video'])
        self.assertEqual(sent[0][1],'out_1')
        caption=sent[0][2]['caption']
        visible,entities=telegram_html.parse(caption)
        self.assertIn('#Kafka',visible)
        self.assertNotIn('#ManagementOnly',visible)
        self.assertTrue(any(type(e).__name__=='MessageEntityBold' for e in entities))
        state=PlanStore(self.db).get(item['id'])
        self.assertEqual(state['status'],'published')
        self.assertEqual(state['dispatch']['video_id'],51)
        self.assertEqual(self.db.get_proposal(pid)['status'],'published')
        sent.clear()
        long=self.ready(text='**Длинная статья**\n\n'+('Текст для публикации. '*80)+'\n\n#Kafka #Системы #Обучение')
        await self.login()
        route=f"/api/content-plan/{long['id']}/publish"
        self.assertEqual((await self.client.post(route,json={},headers=self.h)).status,400)
        r=await self.client.post(route,json={'confirmation':'ОПУБЛИКОВАТЬ'},headers=self.h)
        self.assertEqual(r.status,200,await r.text())
        self.assertEqual([x[0] for x in sent],['video','text'])
        self.assertEqual(sent[1][2]['reply_to'],51)
        self.assertNotIn('Текст для публикации',sent[0][2]['caption'])
        self.assertIn('Текст для публикации',sent[1][2]['text'])
        self.assertEqual(PlanStore(self.db).get(long['id'])['dispatch']['text_id'],52)
        self.assertEqual((await self.client.post(route,json={'confirmation':'ОПУБЛИКОВАТЬ'},headers=self.h)).status,400)
        self.assertEqual(len(sent),2)
    async def test_partial_dispatch_is_uncertain_no_retry_fallback_or_restart_duplicate(self):
        from plan_store import PlanStore
        sent=[]
        async def video(target,path,**kwargs):
            sent.append('video'); return SimpleNamespace(id=71)
        async def broken(target,text,**kwargs):
            sent.append('text'); raise TimeoutError('SECRET request body')
        self.app.bot=SimpleNamespace(send_file=video,send_message=broken)
        item=self.ready(text='**Long**\n\n'+('Article text. '*140))
        try: await self.app.publish(item['proposal_id'])
        except Exception as error: self.assertIsInstance(error,UserError,'Transport errors must be sanitized')
        else: self.fail('Ambiguous dispatch cannot be reported successful')
        stored=PlanStore(self.db).get(item['id'])
        self.assertEqual(stored['status'],'uncertain')
        self.assertEqual(stored['dispatch']['video_id'],71)
        self.assertEqual(stored['dispatch']['phase'],'text_dispatching')
        self.assertNotIn('SECRET',stored['error'])
        with self.assertRaises(UserError): self.app.plan.start_job(self.app,item['id'],retry=True)
        with self.assertRaises(UserError): await self.app.plan.cancel(self.app,item['id'])
        with self.assertRaises(UserError): await self.app.publish(item['proposal_id'])
        self.db.set('plan_paused','0'); self.db.set('paused','1')
        await self.app.scheduler_tick()
        self.assertEqual(sent,['video','text'])
        generating=self.app.plan.create(self.app,self.data(with_video=False))
        rendering=self.app.plan.create(self.other,self.data())
        dispatching=self.ready(with_video=False)
        PlanStore(self.db).update(generating['id'],status='generating')
        PlanStore(self.other.db).update(rendering['id'],status='rendering')
        PlanStore(self.db).update(dispatching['id'],status='publishing',dispatch={'phase':'text_dispatching'})
        self.db.conn.close(); self.db=DB(self.path); self.app=App(self.db)
        store=PlanStore(self.db)
        self.assertEqual(store.get(generating['id'])['status'],'interrupted')
        self.assertEqual(store.get(dispatching['id'])['status'],'uncertain')
        self.assertEqual(store.get(item['id'])['dispatch']['video_id'],71)
        self.assertEqual(PlanStore(self.db.workspace(self.other.db.workspace_id)).get(rendering['id'])['status'],'interrupted')
        self.assertEqual(self.db._raw.execute('PRAGMA integrity_check').fetchone()[0],'ok')
        await self.app.scheduler_tick()
        self.assertEqual(self.app._tasks,set())
    async def test_ready_auto_dispatch_due_only_pause_checked_after_shared_lock(self):
        from plan_store import PlanStore
        sent=[]
        async def text(target,body,**kwargs): sent.append(target); return SimpleNamespace(id=81)
        self.app.bot=SimpleNamespace(send_message=text)
        self.db.set('paused','1'); self.db.set('plan_paused','0')
        item=self.ready(with_video=False)
        self.db.set('target_channel','changed_out')
        await self.app.scheduler_tick()
        self.assertEqual(sent,[])
        store=PlanStore(self.db)
        store.update(item['id'],scheduled_at=int(time.time())-10)
        await self.app.plan.semaphore.acquire()
        try:
            await self.app.scheduler_tick()
            self.assertIn(item['id'],self.app.plan.jobs,'Ready due items must dispatch asynchronously')
            self.db.set('plan_paused','1')
        finally: self.app.plan.semaphore.release()
        await asyncio.gather(*list(self.app._tasks))
        self.assertEqual(sent,[])
        self.assertEqual(store.get(item['id'])['status'],'ready')
        self.db.set('plan_paused','0')
        await self.app.scheduler_tick()
        await asyncio.gather(*list(self.app._tasks))
        self.assertEqual(sent,['out_1'])
        self.assertEqual(store.get(item['id'])['status'],'published')
        for _ in range(3): await self.app.scheduler_tick()
        self.assertEqual(sent,['out_1'])
        self.assertEqual(self.db.recorded_spending()['records'],0)
    async def test_planned_skip_cancels_and_generic_rewrite_is_disabled_with_safe_preview(self):
        from plan_store import PlanStore
        item=self.ready(self.other)
        pid=item['proposal_id']
        p=self.other.db.get_proposal(pid)
        self.assertFalse(any(d.startswith('rw:') for row in self.other.button_rows(p) for _,d in row),
                         'Generic rewrite must not desynchronize a planned storyboard')
        with self.assertRaises(UserError): await self.other.rewrite(pid)
        await self.login()
        rows=await (await self.client.get('/api/proposals',headers=self.h2)).json()
        planned=next(row for row in rows if row['id']==pid)
        self.assertEqual(planned['plan_item_id'],item['id'])
        plan_list=await (await self.client.get('/api/content-plan',headers=self.h2)).json()
        self.assertIn('rendered_html',plan_list['items'][0])
        self.assertIn('<b>',plan_list['items'][0]['rendered_html'])
        self.assertEqual(planned['plan_video_url'],f"/api/content-plan/{item['id']}/video?workspace_id={self.other.db.workspace_id}")
        self.assertNotIn('/root/',json.dumps(planned))
        await self.other.skip(pid)
        self.assertEqual(PlanStore(self.other.db).get(item['id'])['status'],'cancelled')
        self.assertEqual(self.other.db.get_proposal(pid)['status'],'skipped')
        self.other.db.set('plan_paused','0'); self.other.db.set('paused','1')
        PlanStore(self.other.db).update(item['id'],scheduled_at=int(time.time())-10)
        await self.app.scheduler_tick()
        self.assertEqual(self.app._tasks,set())
        next_item=self.ready()
        r=await self.client.post(f"/api/content-plan/{next_item['id']}/cancel",json={},headers=self.h)
        self.assertEqual(r.status,200,await r.text())
        self.assertEqual(self.db.get_proposal(next_item['proposal_id'])['status'],'skipped')
    async def test_running_cancel_preserves_history_and_never_returns_to_ready(self):
        import plan_llm
        from plan_store import PlanStore
        entered=asyncio.Event(); release=asyncio.Event(); renders=[]
        async def generate(db,item): entered.set(); await release.wait(); return self.payload()
        async def render(*args): renders.append(True)
        item=self.app.plan.create(self.app,self.data())
        with patch.object(plan_llm,'generate',generate),patch.object(self.app.plan,'render',render):
            self.app.plan.start_job(self.app,item['id'])
            async with asyncio.timeout(1): await entered.wait()
            await self.app.plan.cancel(self.app,item['id'])
            release.set(); await asyncio.gather(*list(self.app._tasks))
        self.assertEqual(PlanStore(self.db).get(item['id'])['status'],'cancelled')
        self.assertEqual(renders,[])
        self.assertEqual(len(self.db.operation_cost_state()['history']),1)
        self.assertEqual(len(self.db.proposals()),0)
        # Workspace views must expose the exact same engine/semaphore, not clones.
        self.assertIs(getattr(self.other,'plan',None),self.app.plan)
    async def test_shared_connection_or_inherited_model_change_invalidates_plan_rates_only_when_affected(self):
        for db in (self.db,self.other.db):
            db.set('llm_model','news-model')
            db.set('plan_input_usd_per_million','3'); db.set('plan_output_usd_per_million','4')
        self.other.db.set('plan_model','separate-model')
        await self.app.save_settings({'llm_model':'new-news'})
        self.assertEqual(self.db.get('plan_input_usd_per_million'),'')
        self.assertEqual(self.other.db.get('plan_input_usd_per_million'),'3')
        self.db.set('plan_input_usd_per_million','5')
        await self.other.save_settings({'llm_provider':'openrouter'})
        self.assertEqual(self.db.get('plan_input_usd_per_million'),'')
        self.assertEqual(self.other.db.get('plan_input_usd_per_million'),'')
        self.db.set('plan_provider','anthropic')
        self.db.set('plan_input_usd_per_million','7')
        await self.app.save_settings({'llm_provider':'deepseek'})
        self.assertEqual(self.db.get('plan_input_usd_per_million'),'7')
    async def test_storyboard_validation_rejects_all_asset_urls_and_untrusted_fields(self):
        from copy import deepcopy
        from plan_llm import validate_payload
        for url in ('https://bad.example/x','file:///etc/passwd','ftp://bad.example/x','data:image/png;base64,AAA','javascript:alert(1)'):
            payload=deepcopy(self.payload()); payload['scenes'][0]['text']=url
            with self.assertRaises(UserError,msg=url): validate_payload(payload,2200)
        for patch_value in ({'command':'echo BAD'}, {'icon':'not-an-icon'}, {'steps':['one']}, {'text':'x'*181},
                            {'title':' '*60+'a'}, {'text':'Invisible\u200bcontrol'}, {'text':'mailto:user@example.test'}, {'text':'```code```'}):
            payload=deepcopy(self.payload()); payload['scenes'][0].update(patch_value)
            with self.assertRaises(UserError): validate_payload(payload,2200)
        for bad in ({'scenes':[]},{'text':'x'*2201},{'title':None},{'path':'/root/private'}):
            payload={**self.payload(),**bad}
            with self.assertRaises(UserError): validate_payload(payload,2200)
        self.assertEqual(validate_payload(self.payload(),2200),self.payload())
    async def test_missing_prepared_video_recovers_without_recharging_or_text_fallback(self):
        from plan_store import PlanStore
        import plan_llm
        item=self.ready()
        self.app.plan.artifact(self.app,item['id']).unlink()
        sent=[]
        async def send(*args,**kwargs): sent.append(True); return SimpleNamespace(id=1)
        self.app.bot=SimpleNamespace(send_message=send,send_file=send)
        with self.assertRaises(UserError): await self.app.publish(item['proposal_id'])
        self.assertEqual(PlanStore(self.db).get(item['id'])['status'],'error','Manual preflight failure must permit cached recovery')
        self.db.set('plan_paused','0'); self.db.set('paused','1')
        PlanStore(self.db).update(item['id'],scheduled_at=int(time.time())-10)
        await self.app.scheduler_tick(); await asyncio.gather(*list(self.app._tasks))
        self.assertEqual(sent,[])
        self.assertEqual(PlanStore(self.db).get(item['id'])['status'],'error')
        renders=[]
        async def render(storyboard,path):
            renders.append(True); Path(path).write_bytes(b'FAKE-MP4')
            return {'duration':24,'width':720,'height':1280,'fps':24,'bytes':8}
        async def no_paid(*args): self.fail('Validated payload must be reused')
        with patch.object(plan_llm,'generate',no_paid),patch.object(self.app.plan,'render',render):
            self.app.plan.start_job(self.app,item['id'],retry=True)
            await asyncio.gather(*list(self.app._tasks))
        self.assertEqual(renders,[True])
        self.assertEqual(PlanStore(self.db).get(item['id'])['status'],'ready')
        self.assertEqual(self.db.recorded_spending()['records'],0)
    async def test_planned_raw_edit_is_bounded_and_serializes_with_publication(self):
        item=self.ready(); pid=item['proposal_id']
        await self.app.set_text(pid,'**Owner title**\n\nRaw **editable** article.\n\n#Kafka #Owner #Notes')
        self.assertIn('Raw **editable**',self.db.get_proposal(pid)['text'])
        with self.assertRaises(UserError): await self.app.set_text(pid,'x'*2201)
        entered=asyncio.Event(); release=asyncio.Event()
        async def video(*args,**kwargs): entered.set(); await release.wait(); return SimpleNamespace(id=91)
        self.app.bot=SimpleNamespace(send_file=video)
        publishing=asyncio.create_task(self.app.publish(pid))
        async with asyncio.timeout(1): await entered.wait()
        edit=asyncio.create_task(self.app.set_text(pid,'Changed during dispatch'))
        await asyncio.sleep(0)
        self.assertFalse(edit.done(),'Text mutation must share the publication lock')
        release.set()
        results=await asyncio.gather(publishing,edit,return_exceptions=True)
        self.assertIsInstance(results[1],UserError)
        self.assertIn('Raw **editable**',self.db.get_proposal(pid)['text'])
    async def test_legacy_test_reset_preserves_plan_history_linked_proposals_and_operations(self):
        from plan_store import PlanStore
        from cycle_cost import CycleReport
        a=self.ready(); b=self.ready(self.other)
        normal=self.db.add_proposal('News','Legacy news draft',[])
        cycle=CycleReport(self.db,True,'content_plan'); cycle.finish('completed')
        self.db.set('paused','1')
        counts=self.db.test_data_counts()
        self.assertEqual(counts['proposals'],1,'Reset preview must exclude durable planned history')
        deleted=await self.app.reset_test_data('СБРОС')
        self.assertEqual(deleted['proposals'],1)
        self.assertIsNone(self.db.get_proposal(normal))
        self.assertIsNotNone(self.db.get_proposal(a['proposal_id']))
        self.assertIsNotNone(self.other.db.get_proposal(b['proposal_id']))
        self.assertEqual(PlanStore(self.db).get(a['id'])['status'],'ready')
        self.assertEqual(self.db.recorded_spending()['records'],1)
    async def test_renderer_cannot_replace_owned_artifact_with_symlink(self):
        import plan_llm
        from plan_store import PlanStore
        private=Path(self.tmp.name)/'private.mp4'; private.write_bytes(b'PRIVATE'); private.chmod(0o640)
        async def generate(*args): return self.payload()
        async def render(storyboard,path):
            Path(path).symlink_to(private)
            return {'duration':24,'width':720,'height':1280,'fps':24,'bytes':7}
        item=self.app.plan.create(self.app,self.data())
        with patch.object(plan_llm,'generate',generate),patch.object(self.app.plan,'render',render):
            self.app.plan.start_job(self.app,item['id'])
            await asyncio.gather(*list(self.app._tasks))
        self.assertEqual(PlanStore(self.db).get(item['id'])['status'],'error')
        self.assertEqual(private.stat().st_mode & 0o777,0o640)
        self.assertFalse(self.app.plan.listing(self.app)['items'][0]['has_video'])
    async def test_plan_api_unexpected_errors_do_not_log_or_return_request_secrets(self):
        await self.login()
        with patch.object(self.app.plan,'listing',side_effect=RuntimeError('SECRET request prompt body')):
            with self.assertLogs('web',level='ERROR') as logs:
                r=await self.client.get('/api/content-plan')
            self.assertEqual(r.status,500)
            response=await r.text()
            self.assertTrue('SECRET' not in response,'API must sanitize unexpected errors')
            self.assertTrue('SECRET' not in ''.join(logs.output),'Logs must sanitize unexpected errors')
        r=await self.client.get('/api/content-plan')
        self.assertEqual(r.headers.get('Cache-Control'),'no-store')
    async def test_native_failure_unknown_usage_and_invalid_response_are_accounted_once_without_auto_retry(self):
        import httpx2
        import plan_llm
        from plan_store import PlanStore
        self.db.set('plan_provider','anthropic'); self.db.set('plan_api_key','FAKE-NATIVE-KEY'); self.db.set('plan_model','owner-model')
        self.db.set('plan_paused','0'); self.db.set('paused','1')
        calls=[]; status=500; value={'error':{'message':'SECRET raw provider body'}}
        def respond(request): calls.append(request); return httpx2.Response(status,json=value)
        real=httpx2.AsyncClient
        def client(**kwargs): return real(**kwargs,transport=httpx2.MockTransport(respond))
        with patch.object(plan_llm,'NativeClient',client):
            failed=self.app.plan.create(self.app,self.data(with_video=False))
            with self.assertLogs('core',level='ERROR') as logs:
                self.app.plan.start_job(self.app,failed['id']); await asyncio.gather(*list(self.app._tasks))
            self.assertTrue('SECRET' not in ''.join(logs.output),'Provider body leaked to log')
            self.assertEqual(PlanStore(self.db).get(failed['id'])['status'],'error')
            report=self.db.operation_cost_state()['history'][0]
            self.assertEqual(report['calls'],1); self.assertFalse(report['cost_complete']); self.assertIsNone(report['cost_usd'])
            self.assertEqual(report['attempts'][0]['http_status'],500)
            for _ in range(3): await self.app.scheduler_tick()
            self.assertEqual(len(calls),1)
            status=200; value={'content':[{'type':'text','text':json.dumps(self.payload())}],'stop_reason':'end_turn'}
            unknown=self.app.plan.create(self.app,self.data(with_video=False))
            self.app.plan.start_job(self.app,unknown['id']); await asyncio.gather(*list(self.app._tasks))
            self.assertEqual(PlanStore(self.db).get(unknown['id'])['status'],'ready')
            report=self.db.operation_cost_state()['history'][0]
            self.assertFalse(report['tokens_complete']); self.assertFalse(report['cost_complete']); self.assertIsNone(report['cost_usd'])
            value={'content':[{'type':'text','text':'not JSON SECRET'}],'usage':{'input_tokens':5,'output_tokens':2},'stop_reason':'end_turn'}
            invalid=self.app.plan.create(self.app,self.data(with_video=False))
            self.app.plan.start_job(self.app,invalid['id']); await asyncio.gather(*list(self.app._tasks))
            self.assertEqual(PlanStore(self.db).get(invalid['id'])['status'],'error')
            self.assertEqual(len(calls),3)
            self.assertEqual(self.db.recorded_spending()['records'],3)
            self.assertEqual(self.db.recorded_spending()['incomplete_records'],3)
            self.assertTrue('SECRET' not in json.dumps(self.db.events()),'Provider body leaked to events')
    async def test_all_plan_routes_guard_foreign_ids_and_history_limit_is_truthful(self):
        from plan_store import PlanStore
        item=self.app.plan.create(self.app,self.data())
        base=f"/api/content-plan/{item['id']}"
        actions=[base,base+'/generate',base+'/retry',base+'/publish',base+'/cancel','/api/content-plan/pause','/api/content-plan/bulk']
        payload=self.data()|{'confirmation':'ОПУБЛИКОВАТЬ','paused':False,'topics':['one'],'start_date':'2030-03-30','time':'12:00'}
        for route in actions:
            self.assertEqual((await self.client.post(route,json=payload,headers=self.h)).status,401)
        await self.login()
        for route in actions:
            self.assertEqual((await self.client.post(route,json=payload)).status,403)
        for route in actions[:5]:
            self.assertEqual((await self.client.post(route,json=payload,headers=self.h2)).status,404)
        self.assertEqual((await self.client.get(base+'/video',headers=self.h2)).status,404)
        self.assertEqual(self.db.recorded_spending()['records'],0)
        for bad in ('01','0','-1','1.0','abc'):
            self.assertEqual((await self.client.post('/api/content-plan/'+bad+'/generate',json={},headers=self.h)).status,400)
        count=PlanStore(self.db).listing()[1]
        for bulk in ({'start_date':'2030-03-30','time':'02:30','topics':['BPMN','UML']},
                     {'start_date':'2030-01-01','time':'12:00','topics':['topic']*31}):
            self.assertEqual((await self.client.post('/api/content-plan/bulk',json=bulk,headers=self.h)).status,400)
        self.assertEqual(PlanStore(self.db).listing()[1],count)
        values=self.app.plan.validate(self.app,self.data())
        PlanStore(self.db).insert_many([values for _ in range(200)])
        state=await (await self.client.get('/api/content-plan')).json()
        self.assertEqual(state['total'],201)
        self.assertEqual(len(state['items']),200)
        self.assertEqual(state['limit'],200)
        self.assertTrue(state['has_more'])
        self.assertEqual(len({i['id'] for i in state['items']}),200)
        self.assertEqual(self.app._tasks,set())
    async def test_due_preparation_dispatches_without_waiting_for_next_news_tick(self):
        import plan_llm
        from plan_store import PlanStore
        sent=[]
        async def generate(*args): return self.payload()
        async def send(target,text,**kwargs): sent.append(target); return SimpleNamespace(id=121)
        self.app.bot=SimpleNamespace(send_message=send)
        self.db.set('plan_paused','0'); self.db.set('paused','1')
        item=self.app.plan.create(self.app,self.data(with_video=False))
        PlanStore(self.db).update(item['id'],scheduled_at=int(time.time())-5)
        with patch.object(plan_llm,'generate',generate):
            await self.app.scheduler_tick()
            await asyncio.gather(*list(self.app._tasks))
        self.assertEqual(PlanStore(self.db).get(item['id'])['status'],'published',
                         'An ordinary news cycle must not delay a prepared due item until the next tick')
        self.assertEqual(sent,['out_1'])
        self.assertFalse(self.db.operation_cost_state()['history'][0]['manual'])


if __name__ == '__main__': unittest.main()
