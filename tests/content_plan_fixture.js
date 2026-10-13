// SYNTHETIC boundary: no requests leave this fixture, no real keys/publication.
window.fixture = {calls:[], confirmations:[], confirmAnswer:true, promptAnswer:null, delayPlan:false, pendingPlan:[], failNext:false};
window.confirm = message => {fixture.confirmations.push(message); return fixture.confirmAnswer};
window.prompt = message => {fixture.confirmations.push(message); return fixture.promptAnswer};
const copy = x => JSON.parse(JSON.stringify(x));
const settings = {paused:'1',auto_publish:'0',llm_model:'news-model',llm_provider:'openrouter',persona:'editor',persona_custom:'',target_channel:'synthetic_destination',plan_provider:'shared',plan_api_key_hint:'••synthetic',plan_model:'fixture-plan-model',plan_input_usd_per_million:'',plan_output_usd_per_million:'',plan_post_max_len:'2200',plan_paused:'1'};
const spaces = {1:{...settings},2:{...settings,plan_model:'sport-plan-model',target_channel:'sport_destination'}};
const spending = {known_cost_usd:'0',incomplete_records:0,records:0};
const plans = {1:[{id:11,topic:'<img src=x onerror="window.pwned=1"> BPMN',brief:'Owner brief',scheduled_local:'2026-12-20T12:00',scheduled_at:1797768000,status:'scheduled',target_channel:'synthetic_destination',with_video:true,has_video:false,cost:null}, {id:12,topic:'Kafka synthetic',brief:'',scheduled_local:'2026-12-21T12:00',status:'ready',target_channel:'original_destination',with_video:true,has_video:true,title:'Synthetic ready',text:'**Title** <script>window.pwned=1<\/script>',rendered_html:'<b>Synthetic ready</b> &lt;script&gt;literal&lt;/script&gt;',proposal_id:112,cost:{cost_usd:null,known_cost_usd:'0.003',provenance:'unknown/incomplete'}},{id:13,topic:'UML synthetic error',brief:'',scheduled_local:'2026-12-22T12:00',status:'error',target_channel:'synthetic_destination',with_video:true,has_video:false,cost:{cost_usd:'0.005',provenance:'tariff_estimate'},error:'Synthetic render failure'}],2:[]};
window.fixture.spaces=spaces; window.fixture.plans=plans;
window.fetch = async (path,opt={}) => {
  const method=opt.method||'GET', wid=Number(opt.headers?.['X-Workspace-ID']||1), body=opt.body?JSON.parse(opt.body):undefined;
  fixture.calls.push({path,method,wid,body});
  const response=(data,status=200)=>({ok:status<400,status,json:async()=>copy(data)});
  if(fixture.failNext && method==='POST'){fixture.failNext=false;return response({error:'Synthetic failure'},400)}
  if(path==='/api/state')return response({workspace:{id:wid,name:wid===1?'Основная':'Sport'},settings:spaces[wid],tz:'Europe/Moscow',recorded_spending:spending,stats:{new:0,published:0,published_24h:0,posts_24h:0},steps:[],modules:[],user:{status:'off'},bot:{status:'off'},llm:{ok:false,configured:true},providers:{openrouter:{name:'OpenRouter'}},personas:{editor:{name:'Editor',desc:'synthetic',text:'Synthetic instruction'}},placeholders:{},publication_hashtags_auto:'#synthetic'});
  if(path==='/api/workspaces')return response({workspaces:[{id:1,name:'Основная',recorded_spending:spending},{id:2,name:'Sport',recorded_spending:spending}]});
  if(path==='/api/settings'){Object.assign(spaces[wid],body);if(body.plan_provider){for(const s of Object.values(spaces))s.plan_provider=body.plan_provider}return response({ok:true})}
  if(path==='/api/content-plan' && method==='GET'){
    const data={ok:true,paused:spaces[wid].plan_paused==='1',timezone:'Europe/Moscow',now_local:'2026-10-13T12:00',items:copy(plans[wid]),total:plans[wid].length,has_more:false};
    if(fixture.delayPlan){return new Promise(resolve=>fixture.pendingPlan.push(()=>resolve(response(data))))}return response(data);
  }
  if(path==='/api/content-plan/pause'){spaces[wid].plan_paused=body.paused?'1':'0';return response({ok:true,paused:body.paused})}
  if(path==='/api/content-plan' && method==='POST'){const item={...body,id:100+plans[wid].length,status:'scheduled',target_channel:spaces[wid].target_channel,has_video:false,cost:null};plans[wid].push(item);return response({ok:true,item})}
  if(path==='/api/content-plan/bulk'){const items=body.topics.split('\n').filter(x=>x.trim()).map((topic,n)=>({id:200+n,topic,brief:'',scheduled_local:body.start_date+'T'+body.time,status:'scheduled',with_video:body.with_video,target_channel:spaces[wid].target_channel}));plans[wid].push(...items);return response({ok:true,items})}
  const match=path.match(/^\/api\/content-plan\/(\d+)(?:\/(\w+))?$/);
  if(match){const item=plans[wid].find(i=>i.id===Number(match[1]));if(!item)return response({error:'Foreign fixture item'},404);const action=match[2];if(!action)Object.assign(item,body);else if(action==='generate'||action==='retry')item.status='generating';else if(action==='publish')item.status='published';else if(action==='cancel')item.status='cancelled';return response({ok:true,item},action==='generate'?202:200)}
  const textMatch=path.match(/^\/api\/proposals\/(\d+)\/text$/);
  if(textMatch && method==='POST'){const item=plans[wid].find(i=>i.proposal_id===Number(textMatch[1]));if(!item)return response({error:'Foreign synthetic proposal'},404);item.text=body.text;delete item.rendered_html;return response({ok:true})}
  if(path.startsWith('/api/proposals'))return response(fixture.proposals||[]);
  if(path==='/api/channels'||path==='/api/events')return response([]);
  throw new Error('Unexpected SYNTHETIC route: '+method+' '+path);
};
