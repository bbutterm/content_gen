"""Source-only outlook UI fixture; loopback, synthetic state, no app imports.

Run: python3 tests/serve_outlook_settings_fixture.py [--port 18150]
Open /fixture and evaluate tests/ui_outlook_settings.js in a real browser.
All fetch calls are intercepted; unexpected routes throw, never use real APIs.
"""
import argparse
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
FIXTURE = r'''
window.fixture = {calls:[], confirmations:[], confirmAnswer:true, promptAnswer:'New workspace', delaySave:false, pendingSave:[], delayState:false, pendingState:[], failNext:false, failNextState:false};
window.confirm = message => {fixture.confirmations.push(message); return fixture.confirmAnswer};
window.prompt = message => {fixture.confirmations.push(message); return fixture.promptAnswer};
const fixtureCopy = x => JSON.parse(JSON.stringify(x));
const fixtureDefaults = {paused:'1',auto_publish:'0',llm_model:'synthetic-model',llm_provider:'openrouter',persona:'editor',persona_custom:'',target_channel:'synthetic_destination',plan_provider:'shared',plan_model:'synthetic-plan',plan_paused:'1',news_outlook_enabled:'1',news_outlook_horizon:'days_weeks',news_outlook_max_len:'600',news_outlook_focus:''};
fixture.spaces = {1:{...fixtureDefaults},2:{...fixtureDefaults,news_outlook_enabled:'0',news_outlook_horizon:'weeks',news_outlook_max_len:'400',news_outlook_focus:'<img src=x onerror="window.pwned=1"> literal focus'}};
fixture.names = {1:'Основная',2:'Sport <b>literal</b>'};
const fixtureSpending = {known_cost_usd:'0',incomplete_records:0,records:0};
window.fetch = async (path,opt={}) => {
 const method=opt.method||'GET',wid=Number(opt.headers?.['X-Workspace-ID']||1),body=opt.body?JSON.parse(opt.body):undefined;
 fixture.calls.push({path,method,wid,body,headers:opt.headers});
 const response=(data,status=200)=>({ok:status<400,status,json:async()=>fixtureCopy(data)});
 if(fixture.failNext && method==='POST'){fixture.failNext=false;return response({error:'Synthetic failure'},400)}
 if(path==='/api/state'){
  if(fixture.failNextState){fixture.failNextState=false;return response({error:'Synthetic state failure'},500)}
  const data={workspace:{id:wid,name:fixture.names[wid]},settings:fixtureCopy(fixture.spaces[wid]),tz:'Europe/Moscow',recorded_spending:fixtureSpending,stats:{new:0,published:0,published_24h:0,posts_24h:0},steps:[],modules:[],user:{status:'off'},bot:{status:'off'},llm:{ok:false,configured:true},providers:{openrouter:{name:'Synthetic'}},personas:{editor:{name:'Editor',desc:'synthetic',text:'Synthetic instruction'}},placeholders:{},publication_hashtags_auto:'#synthetic'};
  if(fixture.delayState)return new Promise(resolve=>fixture.pendingState.push(()=>resolve(response(data))));
  return response(data);
 }
 if(path==='/api/workspaces' && method==='GET')return response({workspaces:Object.keys(fixture.spaces).map(id=>({id:Number(id),name:fixture.names[id],recorded_spending:fixtureSpending}))});
 if(path==='/api/workspaces' && method==='POST'){const id=Math.max(...Object.keys(fixture.spaces).map(Number))+1;fixture.spaces[id]={...fixtureDefaults};fixture.names[id]=body.name;return response({id})}
 if(path==='/api/settings' && method==='POST'){
  Object.assign(fixture.spaces[wid],body);
  if(fixture.delaySave)return new Promise(resolve=>fixture.pendingSave.push(()=>resolve(response({ok:true}))));
  return response({ok:true});
 }
 if(path==='/api/content-plan' && method==='GET')return response({paused:true,timezone:'Europe/Moscow',now_local:'2026-10-13T12:00',items:[],total:0,has_more:false});
 if(path==='/api/channels'||path==='/api/events'||path.startsWith('/api/proposals?'))return response([]);
 throw new Error('Unexpected SYNTHETIC route: '+method+' '+path);
};
window.fixtureErrors=[];
window.addEventListener('error',e=>fixtureErrors.push(e.message));
window.addEventListener('unhandledrejection',e=>fixtureErrors.push(String(e.reason)));
'''

class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path == '/tests/ui_outlook_settings.js':
            data = (ROOT / 'tests/ui_outlook_settings.js').read_bytes()
            content_type = 'text/javascript; charset=utf-8'
        elif self.path == '/fixture':
            html = (ROOT / 'static/index.html').read_text()
            html = html.replace('<body>', '<body><div style="padding:8px;text-align:center;background:#fdf1dc;color:#16181d">SYNTHETIC UI · no real API, keys or Telegram</div>', 1)
            html = html.replace('<script>', '<script>' + FIXTURE + '</script><script>', 1)
            data = html.encode()
            content_type = 'text/html; charset=utf-8'
        else:
            self.send_error(404)
            return
        self.send_response(200)
        self.send_header('Content-Type', content_type)
        self.send_header('Content-Length', str(len(data)))
        self.send_header('Cache-Control', 'no-store')
        self.send_header('Content-Security-Policy', "default-src 'self'; script-src 'self' 'unsafe-inline' 'unsafe-eval'; style-src 'self' 'unsafe-inline'; connect-src 'none'; img-src 'self' data:; media-src 'none'")
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, *_):
        pass

if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--port', type=int, default=18150)
    args = parser.parse_args()
    HTTPServer(('127.0.0.1', args.port), Handler).serve_forever()
