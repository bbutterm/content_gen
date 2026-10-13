"""Loopback-only offline UI fixture. Never imports production configuration/data."""
import sys
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
import json
import tempfile
from aiohttp import web
import httpx2
import openai
import llm
from db import DB
from core import App
from web import build_web

clients=[]
def factory(**kwargs):
    def respond(request):
        body=json.loads(request.content)
        content='Ты — точный и дружелюбный автор. Отделяй факты от мнений, сохраняй источники.'
        if 'response_format' in body:
            content=json.dumps({'proposals':[{'title':'Fixture news','text':'Verified offline draft','sources':[1]}]})
        return httpx2.Response(200,json={'id':'fixture','object':'chat.completion','created':1,
            'model':body['model'],'choices':[{'index':0,'finish_reason':'stop',
                'message':{'role':'assistant','content':content}}],
            'usage':{'prompt_tokens':100,'completion_tokens':20}})
    client=openai.AsyncOpenAI(**kwargs,http_client=httpx2.AsyncClient(transport=httpx2.MockTransport(respond)))
    clients.append(client); return client
llm.AsyncOpenAI=factory

tmp=tempfile.TemporaryDirectory()
db=DB(str(Path(tmp.name)/'fixture.sqlite'))
app=App(db)
db.set('llm_api_key','offline-fixture'); db.set('llm_model','fixture-model'); db.set('paused','1')
db.add_channel('politics_source')
db.add_proposal('Fixture headline', '**Fixture headline**\n\n**Important** & <script>window.pwned=1</script>.\n\nSecond fact.', [])
wid=db.create_workspace('Sport'); d=db.workspace(wid); d.set('llm_model','sport-model'); d.add_channel('sport_source')
def spending_fixture(space, operation, cost, known='0'):
    report={'started_at':1,'finished_at':2,'status':'completed','cost_usd':cost,
            'cost_complete':cost is not None,'known_cost_usd':known,'attempts':[],
            'calls':0,'input_tokens':0,'output_tokens':0,'tokens_complete':True,
            'provenance':'provider_reported' if cost is not None else 'unknown/incomplete',
            'result':None,'operation_type':'fixture'}
    rid=(space.start_operation if operation else space.start_cycle)(report)
    report['id']=rid
    (space.save_operation if operation else space.save_cycle)(rid,report)
spending_fixture(db,False,'0.000000000000000001')
spending_fixture(db,True,'0.000000000000000002')
spending_fixture(d,False,'3.25')
spending_fixture(d,True,None,'0.50')

@web.middleware
async def fixture(request,handler):
    if request.path=='/fixture':
        html=(Path(__file__).resolve().parents[1]/'static/index.html').read_text()
        pre='''<script>
const nativeFetch=window.fetch.bind(window);
const ready=nativeFetch('/api/login',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({password:'offline-fixture'})});
window.fetch=async(...args)=>{await ready; return nativeFetch(...args)};
</script>'''
        return web.Response(text=html.replace('<script>',pre+'<script>',1),content_type='text/html')
    return await handler(request)
server=build_web(app,'offline-fixture')
server.middlewares.insert(0,fixture)
async def cleanup(_):
    for client in clients: await client.close()
    db.conn.close(); tmp.cleanup()
server.on_cleanup.append(cleanup)
web.run_app(server,host='127.0.0.1',port=18139,print=None)
