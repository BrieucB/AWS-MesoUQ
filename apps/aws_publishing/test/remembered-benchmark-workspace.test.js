import test from 'node:test';
import assert from 'node:assert/strict';
import {readFile} from 'node:fs/promises';
import {setTimeout as pause} from 'node:timers/promises';
const preset=JSON.parse(await readFile(new URL('../../../src/aws_mesouq/presets/indentation-v1.json',import.meta.url)));

test('remembered renewal preserves Benchmark Lab scenario controls and selected campaign without another submission',async()=>{
  const events={},labEvents={},calls=[],timers=[];
  const labRoot={addEventListener:(name,fn)=>labEvents[name]=fn,querySelector:()=>null};
  const app={innerHTML:'',addEventListener:(name,fn)=>events[name]=fn,
    querySelector(selector){return selector==='[data-benchmark-lab]'&&this.innerHTML.includes('data-benchmark-lab')?labRoot:null;}};
  const config={apiBaseUrl:'https://api.example.test',cognitoDomain:'https://identity.example.test',clientId:'public',
    redirectUri:'https://portal.example.test/auth/callback',logoutUri:'https://portal.example.test/auth/login',uploadOrigins:[]};
  const defaults={scenario:{family:'baseline',scale:1},seed:1000,
    controls:{hnpe:{width:128,depth:2,learning_rate:.0003,batch_size:2048,max_updates:1000000,training_budget:1800},
      tmcmc:{population_size:4096,target_cov:.6,proposal_scale:1,burn_in:7,max_generations:1000}},
    evaluation:{case_count:16,posterior_draws:2048,cohort:'central_90'},budget:{mode:'fixed',method_gpu_seconds:14400},
    reference:{initial_power:14,maximum_power:20,scrambles:4,relative_tolerance:.02}};
  const original={document:globalThis.document,location:globalThis.location,history:globalThis.history,
    sessionStorage:globalThis.sessionStorage,fetch:globalThis.fetch,setTimeout:globalThis.setTimeout,setInterval:globalThis.setInterval};
  let generation=0,campaign;
  const birth=Date.now()/1000,session=()=>({access_token:generation?'renewed':'original',expires_at:Date.now()/1000+300,
    renewable:true,absolute_expires_at:birth+30*86400,idle_expires_at:birth+7*86400,generation});
  globalThis.document={querySelector:selector=>selector==='#app'?app:null,hidden:false};
  globalThis.location={origin:'https://portal.example.test',search:'',pathname:'/',assign:()=>assert.fail('Renewal must not navigate')};
  globalThis.history={replaceState(){}};
  globalThis.sessionStorage={removeItem(){},getItem:()=>assert.fail('No auth storage'),setItem:()=>assert.fail('No auth storage')};
  globalThis.setTimeout=(fn,ms)=>{timers.push({fn,ms});return timers.length;};
  globalThis.setInterval=()=>0;
  globalThis.fetch=async(input,options={})=>{
    const path=String(input);calls.push({path,options});let data;
    if(path==='/auth/session')data=session();
    else if(path==='/auth/refresh'){assert.deepEqual(JSON.parse(options.body),{activity:false});generation++;data=session();}
    else if(path==='runtime-config.json')data=config;
    else if(path==='presets/indentation-v1.json')data=preset;
    else if(path.endsWith('/v1/me'))data={subject:'researcher',email:'synthetic@example.test'};
    else if(path.endsWith('/v1/projects'))data={projects:[{id:'p',name:'Synthetic project'}]};
    else if(path.endsWith('/models'))data={models:[]};
    else if(path.endsWith('/jobs'))data={jobs:[]};
    else if(path.endsWith('/benchmarks/capabilities'))data={capabilities:{defaults,limits:{},actions:{archive:true}}};
    else if(path.endsWith('/benchmarks')&&options.method==='POST'){
      campaign={id:'selected-campaign',status:'running',created_at:birth,configuration:JSON.parse(options.body),
        stages:[{id:'train_global',status:'running'}]};data={campaign};
    }else if(path.endsWith('/benchmarks'))data={campaigns:campaign?[campaign]:[]};
    else if(path.endsWith('/benchmarks/selected-campaign'))data={campaign};
    else throw Error('Unexpected synthetic route: '+path);
    return {ok:true,status:200,json:async()=>data};
  };
  const wait=async predicate=>{for(let n=0;n<200&&!predicate();n++)await pause(2);assert.ok(predicate());};
  try{
    await import('../src/app.js?remembered-benchmark-preservation');
    await wait(()=>app.innerHTML.includes('Configure your analysis'));
    events.click({target:{closest:()=>({dataset:{action:'tab',tab:'benchmarks'},disabled:false})}});
    await wait(()=>app.innerHTML.includes('Start benchmark campaign'));
    const field={dataset:{labField:'scenario.family'},value:'higher_noise'};
    labEvents.input({target:field});labEvents.change({target:field});
    labEvents.input({target:{dataset:{labField:'controls.hnpe.width'},value:'256'}});
    labEvents.submit({target:{matches:()=>true},preventDefault(){}});
    await wait(()=>app.innerHTML.includes('Campaign submitted'));
    const before=app.innerHTML,submissions=calls.filter(c=>c.path.endsWith('/benchmarks')&&c.options.method==='POST');
    assert.equal(submissions.length,1);assert.equal(campaign.configuration.controls.hnpe.width,256);
    const key=campaign.configuration.idempotency_key;
    timers[0].fn();
    await wait(()=>calls.some(c=>c.path==='/auth/refresh'));await pause(3);
    assert.equal(app.innerHTML,before);
    assert.equal(campaign.configuration.idempotency_key,key);
    assert.equal(calls.filter(c=>c.path.endsWith('/benchmarks')&&c.options.method==='POST').length,1);
    labEvents.click({target:{closest:()=>({dataset:{labAction:'refresh-detail'},disabled:false})}});
    await wait(()=>calls.some(c=>c.path.endsWith('/benchmarks/selected-campaign')));
    const detail=calls.filter(c=>c.path.endsWith('/benchmarks/selected-campaign')).at(-1);
    assert.equal(detail.options.headers.Authorization,'Bearer renewed');
    assert.match(app.innerHTML,/selected-campaign/);
  }finally{Object.assign(globalThis,original);}
});
