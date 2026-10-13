"""Offline acceptance: real App/SQLite/SDK, fake Telegram and HTTP only."""
import sys
from pathlib import Path
STAGE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(STAGE))
import asyncio
import json
import tempfile
import unittest
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import patch
import httpx2
import openai
from db import DB
from core import App
import llm

class Reader:
    def __init__(self): self.reads = []
    async def get_entity(self, key):
        return SimpleNamespace(username=str(key), title=str(key), id=100)
    async def iter_messages(self, entity, **kwargs):
        self.reads.append((entity.username, kwargs))
        if kwargs.get('min_id', 0) < 10:
            yield SimpleNamespace(id=10, date=datetime.now(timezone.utc),
                                  message='A verified source news item. ' * 4, photo=None)

class Poster:
    def __init__(self): self.sent=[]; self.handlers=[]; self.edits=[]; self.deleted=[]
    async def send_message(self, target, text, **kwargs):
        self.sent.append((target,text,kwargs))
        return SimpleNamespace(id=len(self.sent))
    async def edit_message(self, *args, **kwargs): self.edits.append((args,kwargs))
    async def delete_messages(self, chat, ids, revoke=True): self.deleted.append((chat,ids,revoke))
    def on(self, event):
        def decorate(fn): self.handlers.append(fn); return fn
        return decorate

class Acceptance(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = DB(str(Path(self.tmp.name)/'test.sqlite'))
        self.app = App(self.db)
        self.app.user=Reader(); self.app.bot=Poster()
        self.db.set('admin_ids','123'); self.db.set('llm_api_key','offline-key')
        self.db.set('llm_model','offline-model')
        self.calls=[]; self.clients=[]; self.http_statuses=[]; self.usage={'prompt_tokens':100,'completion_tokens':20}
        self.response_text=None
        def respond(request):
            self.calls.append(json.loads(request.content))
            status=self.http_statuses.pop(0) if self.http_statuses else 200
            if status != 200:
                return httpx2.Response(status,json={'error':{'message':'DO-NOT-LOG offline-key response body','type':'failure'}})
            return httpx2.Response(200, json={'id':'fake','object':'chat.completion', 'created':1,
                'model':json.loads(request.content)['model'],
                'choices':[{'index':0,'finish_reason':'stop','message':{'role':'assistant','content':self.response_text or json.dumps(
                    {'proposals':[{'title':'News','text':'Draft from original sources','sources':[1]}]})}}],
                'usage':self.usage})
        def sdk(**kwargs):
            client=openai.AsyncOpenAI(**kwargs,http_client=httpx2.AsyncClient(transport=httpx2.MockTransport(respond)))
            self.clients.append(client); return client
        self.sdkpatch=patch.object(llm,'AsyncOpenAI',sdk); self.sdkpatch.start()
    async def asyncTearDown(self):
        self.sdkpatch.stop()
        for client in self.clients: await client.close()
        self.db.conn.close(); self.tmp.cleanup()
    async def test_old_database_migration_preserves_all_rows_and_ids_idempotently(self):
        import importlib.util
        path=STAGE/'tests/fixtures/legacy_db.py'
        spec=importlib.util.spec_from_file_location('baseline_db',path); oldmod=importlib.util.module_from_spec(spec)
        exec(compile(path.read_text(),str(path),'exec'),oldmod.__dict__)
        legacy_path=str(Path(self.tmp.name)/'legacy.sqlite'); old=oldmod.DB(legacy_path)
        for key,value in {'tg_api_hash':'synthetic-hash','user_session':'synthetic-reader-session','bot_session':'synthetic-bot-session',
                'target_channel':'legacy_out','auto_publish':'1','paused':'0','llm_model':'legacy-model','mod.queue.last_fired':'old-slot'}.items(): old.set(key,value)
        old.add_channel('same_source'); old.channel_fetched('same_source',90,'Old source',4)
        old.add_post('same_source',90,1,'old source','https://t.me/same_source/90','photo'); old.mark_all_used()
        pid=old.add_proposal('Legacy','Legacy draft',[('same_source',90)]); old.set_tg_msgs(pid,[[123,99]])
        old.add_event('info','legacy event')
        before={r['key']:r['value'] for r in old.conn.execute('SELECT * FROM settings')}
        old.conn.close()
        migrated=DB(legacy_path)
        self.assertEqual({r['key']:r['value'] for r in migrated._raw.execute('SELECT * FROM settings')},before)
        self.assertEqual(migrated.get('auto_publish'),'1'); self.assertEqual(migrated.get('paused'),'0')
        self.assertEqual(migrated.get('mod.queue.last_fired'),'old-slot')
        self.assertEqual(migrated.channels()[0]['last_id'],90)
        p=migrated.get_proposal(pid); self.assertEqual(p['tg_msgs'],[(123,99)])
        self.assertEqual(p['sources'],[('same_source',90)]); self.assertEqual(p['target_channel'],'legacy_out')
        self.assertEqual(migrated.pending_count(),0)
        other=migrated.workspace(migrated.create_workspace('Sport'))
        self.assertEqual(other.get('auto_publish'),'0');self.assertEqual(other.get('paused'),'1')
        self.assertEqual(other.get('target_channel'),''); self.assertEqual(other.get('llm_model'),'')
        self.assertEqual(other.get('user_session'),'synthetic-reader-session')
        self.assertEqual(other.proposals(),[]); self.assertEqual(other.channels(),[])
        self.assertEqual(migrated._raw.execute("SELECT count(*) FROM workspace_settings WHERE key IN ('user_session','tg_api_hash','bot_session')").fetchone()[0],0)
        migrated.set('last_run','321'); migrated.set('target_channel','new_out'); migrated.conn.close()
        migrated=DB(legacy_path); self.assertEqual(migrated.get('last_run'),'321');self.assertEqual(migrated.get('target_channel'),'new_out')
        self.assertEqual(len(migrated.proposals()),1);self.assertEqual(len(migrated.events()),1)
        self.assertEqual(migrated._raw.execute('PRAGMA integrity_check').fetchone()[0],'ok');migrated.conn.close()

    async def test_operations_restart_is_interrupted_only_at_root_startup(self):
        from cycle_cost import CycleReport
        cycle=CycleReport(self.db,True,'character_prompt')
        cycle.attempt('openai','offline-model','character_prompt','','')
        wid=self.db.create_workspace('Sport'); self.app.workspace(wid)
        self.assertEqual(self.db.operation_cost_state()['running']['status'],'running')
        self.db.conn.close(); self.db=DB(str(Path(self.tmp.name)/'test.sqlite')); self.app=App(self.db)
        state=self.db.operation_cost_state()
        self.assertIsNone(state['running'])
        self.assertEqual(state['last_completed']['status'],'interrupted')
        self.assertIsNone(state['last_completed']['cost_usd'])
        self.assertEqual(state['last_completed']['attempts'][0]['status'],'interrupted')

    async def test_global_provider_change_invalidates_tariffs_in_every_workspace(self):
        wid=self.db.create_workspace('Games'); b=self.app.workspace(wid)
        for d in (self.db,b.db):
            d.set('llm_input_usd_per_million','1'); d.set('llm_output_usd_per_million','2')
            d.set('llm_local_no_bill','1'); d.set('llm_ok','1')
        await b.save_settings({'llm_provider':'openrouter'})
        for d in (self.db,b.db):
            self.assertEqual(d.get('llm_provider'),'openrouter')
            self.assertEqual(d.get('llm_input_usd_per_million'),'')
            self.assertEqual(d.get('llm_output_usd_per_million'),'')
            self.assertEqual(d.get('llm_local_no_bill'),'0')
            self.assertEqual(d.get('llm_ok'),'0')

    async def test_character_generation_explicit_editable_operation_cost(self):
        from aiohttp.test_utils import TestClient,TestServer
        from web import build_web
        wid=self.db.create_workspace('Games'); space=self.app.workspace(wid)
        space.db.set('llm_model','games-model')
        space.db.set('llm_input_usd_per_million','1'); space.db.set('llm_output_usd_per_million','2')
        self.app.user=None; self.app.bot=None
        async with TestClient(TestServer(build_web(self.app,'offline-password'))) as c:
            route='/api/persona/generate'; h={'X-Panel':'1','X-Workspace-ID':str(wid)}
            self.assertEqual((await c.post(route,json={'keywords':'precise'},headers=h)).status,401)
            await c.post('/api/login',json={'password':'offline-password'})
            self.assertEqual((await c.post(route,json={'keywords':'precise'},headers={'X-Workspace-ID':str(wid)})).status,403)
            for bad in ('', 'x'*401, None, ['x']):
                self.assertEqual((await c.post(route,json={'keywords':bad},headers=h)).status,400)
            self.assertEqual(self.calls,[])
            r=await c.post(route,json={'keywords':'precise, friendly'},headers=h)
            self.assertEqual(r.status,200)
            result=await r.json(); self.assertTrue(result['instruction'])
            self.assertEqual(space.db.get('persona_custom'),'')
            self.assertEqual(self.calls[0]['model'],'games-model')
            self.assertLessEqual(self.calls[0]['max_tokens'],1500)
            self.assertIn('факт',self.calls[0]['messages'][0]['content'])
            self.assertEqual(space.db.cycle_cost_state()['history'],[])
            ops=await (await c.get('/api/operations',headers=h)).json()
            self.assertEqual(len(ops['history']),1)
            report=ops['history'][0]
            self.assertEqual(report['operation_type'],'character_prompt')
            self.assertEqual(report['workspace_id'],wid)
            self.assertEqual(report['provenance'],'tariff_estimate')
            from decimal import Decimal
            expected=(Decimal(100)+Decimal(20)*2)/Decimal(1000000)
            self.assertEqual(Decimal(report['cost_usd']),expected)
            self.assertEqual(self.db.operation_cost_state()['history'],[])
            self.assertNotIn('precise',json.dumps(space.db.events()))
            await c.post('/api/settings',json={'persona':'custom','persona_custom':'Owner edit'},headers=h)
            self.assertEqual(space.db.get('persona_custom'),'Owner edit')

    async def test_scheduler_and_queue_tick_are_owned_by_each_space(self):
        wid=self.db.create_workspace('Sport'); b=self.app.workspace(wid)
        for space in (self.app,b):
            space.db.set('paused','0'); space.db.set('llm_model','offline-model')
            space.db.set('target_channel','space_'+str(space.db.workspace_id)); space.db.add_channel('same_source')
        await self.app.scheduler_tick()
        self.assertEqual(len(self.db.proposals()),1); self.assertEqual(len(b.db.proposals()),1)
        self.assertEqual(len(self.calls),2)
        # Paused space never receives ticks, including a publication queue.
        for space in (self.app,b):
            space.modules['queue'].save({'enabled':'1'})
            pid=space.db.proposals()[0]['id']; await space.approve(pid)
        self.db.set('paused','1')
        with patch.object(b.modules['queue'],'on_tick',b.modules['queue'].fire):
            await self.app.scheduler_tick()
        self.assertEqual(self.db.proposals()[0]['status'],'queued')
        self.assertEqual(b.db.proposals()[0]['status'],'published')
        self.assertEqual(self.db.get('mod.queue.last_fired'),'')

    async def test_telegram_callback_dispatch_and_concurrent_publish(self):
        import tgbot
        wid=self.db.create_workspace('Sport'); b=self.app.workspace(wid)
        b.db.set('target_channel','sport_out')
        pid=b.db.add_proposal('sport','sport text',[])
        self.app.bot_me=SimpleNamespace(id=999,username='offline_bot')
        tgbot.register(self.app,self.app.bot)
        callback=next(h for h in self.app.bot.handlers if h.__name__=='on_button')
        answers=[]
        async def answer(*args,**kwargs): answers.append(args)
        event=SimpleNamespace(data=f'pub:{pid}'.encode(),sender_id=123,answer=answer,respond=answer)
        await callback(event)
        self.assertEqual(b.db.get_proposal(pid)['status'],'published')
        self.assertEqual([x[0] for x in self.app.bot.sent],['sport_out'])
        await callback(event)
        self.assertEqual(len(self.app.bot.sent),1)
        # A double-click while transport is in-flight must not send twice.
        pid=b.db.add_proposal('sport2','sport text2',[])
        entered=asyncio.Event(); release=asyncio.Event()
        send=self.app.bot.send_message
        async def slow(*args,**kwargs):
            entered.set(); await release.wait(); return await send(*args,**kwargs)
        with patch.object(self.app.bot,'send_message',slow):
            first=asyncio.create_task(b.publish(pid)); await entered.wait()
            second=asyncio.create_task(b.publish(pid)); await asyncio.sleep(0)
            release.set(); results=await asyncio.gather(first,second,return_exceptions=True)
        self.assertEqual(len(self.app.bot.sent),2)
        self.assertEqual(sum(isinstance(r,Exception) for r in results),1)

    async def test_api_workspace_crud_guards_and_independent_clients(self):
        from aiohttp.test_utils import TestClient, TestServer
        from web import build_web
        self.app.user=None; self.app.bot=None
        async with TestClient(TestServer(build_web(self.app,'offline-password'))) as client:
            self.assertEqual((await client.get('/api/workspaces')).status,401)
            await client.post('/api/login',json={'password':'offline-password'})
            self.assertEqual((await client.post('/api/workspaces',json={'name':'Sport'})).status,403)
            h={'X-Panel':'1'}
            response=await client.post('/api/workspaces',json={'name':'Sport'},headers=h)
            self.assertEqual(response.status,200)
            wid=(await response.json())['id']; hs={**h,'X-Workspace-ID':str(wid)}
            st=await (await client.get('/api/state',headers=hs)).json()
            self.assertEqual(st['workspace']['id'],wid)
            self.assertEqual(st['settings']['paused'],'1')
            self.assertEqual(st['settings']['auto_publish'],'0')
            self.assertEqual(st['settings']['target_channel'],'')
            self.assertEqual(st['settings']['llm_model'],'')
            for bad,status in (('0',400),('-1',400),('1.0',400),('01',400),('9999',404)):
                r=await client.get('/api/state',headers={'X-Workspace-ID':bad})
                self.assertEqual(r.status,status,bad)
            self.assertEqual((await client.get('/api/state?workspace_id=9999')).status,404)
            self.assertEqual((await client.get('/api/state?workspace_id=1',headers=hs)).status,400)
            await client.post(f'/api/workspaces/{wid}',json={'name':'Games'},headers=h)
            await client.post('/api/workspaces/order',json={'ids':[wid,1]},headers=h)
            rows=await (await client.get('/api/workspaces')).json()
            self.assertEqual([w['name'] for w in rows['workspaces']],['Games','Основная'])
            await client.post('/api/channels',json={'items':'same_source'},headers=h)
            await client.post('/api/channels',json={'items':'same_source'},headers=hs)
            await client.delete('/api/channels/same_source',headers=hs)
            self.assertEqual(len(await (await client.get('/api/channels',headers=h)).json()),1)
            pid=self.db.add_proposal('one','only workspace one',[])
            self.assertEqual((await client.post(f'/api/proposals/{pid}/skip',headers=hs)).status,404)
            self.assertEqual(self.db.get_proposal(pid)['status'],'new')
            # Shared global credentials update from a workspace restarts ROOT only.
            with patch.object(self.app,'start_bot',new=unittest.mock.AsyncMock()) as start:
                r=await client.post('/api/settings',json={'bot_token':'fake-token'},headers=hs)
                self.assertEqual(r.status,200); start.assert_awaited_once()
            self.assertEqual(self.db.get('bot_token'),'fake-token')

    async def test_two_spaces_collect_draft_approve_publish_share_clients(self):
        second_id=self.db.create_workspace('Sport')
        a=self.app.workspace(1); b=self.app.workspace(second_id)
        self.assertIs(a.user,b.user); self.assertIs(a.bot,b.bot)
        for space,target,model in ((a,'politics_out','politics-model'),(b,'sport_out','sport-model')):
            space.db.set('target_channel',target); space.db.set('llm_model',model)
            space.db.add_channel('same_source')
            result=await space.run_cycle(manual=True)
            self.assertEqual(result['added'],1); self.assertEqual(result['proposals'],1)
        pa=a.db.proposals()[0]; pb=b.db.proposals()[0]
        self.assertNotEqual(pa['id'],pb['id'])
        self.assertIsNone(a.db.get_proposal(pb['id']))
        a.db.set('target_channel','changed_out')
        await a.approve(pa['id']); await b.approve(pb['id'])
        published=[x for x in self.app.bot.sent if x[0] != 123]
        self.assertEqual([x[0] for x in published],['politics_out','sport_out'])
        previews=[x[1] for x in self.app.bot.sent if x[0]==123]
        self.assertIn('Основная',previews[0]); self.assertIn('Sport',previews[1])
        self.assertIn('https://t.me/same_source/10',previews[0])
        self.assertEqual([x['model'] for x in self.calls],['politics-model','sport-model'])
        self.assertEqual(len(a.db.cycle_cost_state()['history']),1)
        self.assertEqual(len(b.db.cycle_cost_state()['history']),1)
        self.assertEqual(a.db.channels()[0]['last_id'],10)
        b.db.remove_channel('same_source'); self.assertEqual(len(a.db.channels()),1)

if __name__=='__main__':
    suite=unittest.TestSuite([unittest.defaultTestLoader.loadTestsFromTestCase(Acceptance),
        unittest.defaultTestLoader.discover(str(STAGE/'tests/regression'),pattern='test_*.py')])
    for name in ('core','db','web','llm','tgbot','qr_login','cycle_cost'):
        if name in sys.modules:
            assert Path(sys.modules[name].__file__).resolve().parent==STAGE, name+' imports wrong stage'
    result=unittest.TextTestRunner(verbosity=2).run(suite)
    raise SystemExit(not result.wasSuccessful())
