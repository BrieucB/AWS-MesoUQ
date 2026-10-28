import test from 'node:test';
import assert from 'node:assert/strict';
import {readFile} from 'node:fs/promises';
import {setTimeout as pause} from 'node:timers/promises';
const preset=JSON.parse(await readFile(new URL('../../../src/aws_mesouq/presets/indentation-v1.json',import.meta.url)));
test('synthetic BenchmarkLab navigation submits matched budgets and keeps calibration/session isolation',async()=>{
  const events={},labEvents={},calls=[];
  const root={addEventListener:(type,fn)=>labEvents[type]=fn,querySelector:()=>null};
  const app={innerHTML:'',addEventListener:(type,fn)=>events[type]=fn,querySelector(selector){return selector==='[data-benchmark-lab]'&&this.innerHTML.includes('data-benchmark-lab')?root:null;}};
  const config={apiBaseUrl:'https://api.example.test',cognitoDomain:'https://identity.example.test',clientId:'public',redirectUri:'https://portal.example.test/auth/callback',logoutUri:'https://portal.example.test/auth/login',uploadOrigins:['https://storage.example.test']};
  const defaults={scenario:{family:'baseline',scale:1},seed:1000,controls:{hnpe:{width:128,depth:2,learning_rate:.0003,batch_size:2048,max_updates:1000000,training_budget:1800},tmcmc:{population_size:4096,target_cov:.6,proposal_scale:1,burn_in:7,max_generations:1000}},evaluation:{case_count:16,posterior_draws:2048,cohort:'central_90'},budget:{mode:'fixed',method_gpu_seconds:14400},reference:{initial_power:14,maximum_power:20,scrambles:4,relative_tolerance:.02}};
  let campaign={id:'synthetic-benchmark',status:'running',created_at:1767225600,configuration:defaults,stages:[{id:'train_global',status:'running'}]},releaseResult;
  const original={document:globalThis.document,location:globalThis.location,history:globalThis.history,sessionStorage:globalThis.sessionStorage,fetch:globalThis.fetch,setTimeout:globalThis.setTimeout,setInterval:globalThis.setInterval};
  globalThis.document={querySelector:s=>s==='#app'?app:null,hidden:false};
  globalThis.location={origin:'https://portal.example.test',search:'',pathname:'/',assign:()=>{}};
  globalThis.history={replaceState:()=>{}};
  globalThis.sessionStorage={removeItem:()=>{}};
  globalThis.setTimeout=()=>0;globalThis.setInterval=()=>0;
  globalThis.fetch=async(input,options={})=>{
    const path=String(input);calls.push({path,options});let data;
    if(path==='/auth/session')data={access_token:'synthetic-memory-token',expires_at:Date.now()/1000+300};
    else if(path==='runtime-config.json')data=config;
    else if(path==='presets/indentation-v1.json')data=preset;
    else if(path.endsWith('/v1/me'))data={subject:'researcher',email:'synthetic@example.test'};
    else if(path.endsWith('/v1/projects'))data={projects:[{id:'p',name:'Synthetic project',site:'karolina'}]};
    else if(path.endsWith('/models'))data={models:[{model_version:'approved-v1',status:'released',provenance:{pipeline_binding:{id:preset.scientific_profile}}}]};
    else if(path.endsWith('/jobs'))data={jobs:[]};
    else if(path.endsWith('/benchmarks/capabilities'))data={capabilities:{defaults,limits:{'controls.hnpe.width':{choices:[64,128,256]}},actions:{archive:true}}};
    else if(path.endsWith('/benchmarks')&&options.method==='POST'){campaign={...campaign,configuration:JSON.parse(options.body)};data={campaign};}
    else if(path.endsWith('/benchmarks'))data={campaigns:[campaign]};
    else if(path.endsWith('/benchmarks/synthetic-benchmark/results')){await new Promise(resolve=>releaseResult=resolve);data={result:{metrics:{'Synthetic marginal W2':.08}}};}
    else if(path.endsWith('/benchmarks/synthetic-benchmark/cancel')){campaign={...campaign,status:'cancel_requested'};data={campaign};}
    else if(path.endsWith('/benchmarks/synthetic-benchmark'))data={campaign};
    else throw Error('Unexpected synthetic route');
    return {ok:true,status:200,json:async()=>data};
  };
  const settle=async pattern=>{for(let n=0;n<150&&!pattern.test(app.innerHTML);n++)await pause(2);assert.match(app.innerHTML,pattern);};
  const click=(action,extra={})=>events.click({target:{closest:()=>({dataset:{action,...extra},disabled:false})}});
  const labClick=(labAction,labId)=>labEvents.click({target:{closest:()=>({dataset:{labAction,labId},disabled:false})}});
  try{
    await import('../src/app.js?benchmark-workspace-synthetic');
    await settle(/Configure your analysis/);
    click('tab',{tab:'benchmarks'});await settle(/Start benchmark campaign/);
    assert.match(app.innerHTML,/Maximum fit time per factor/);
    const scenario={dataset:{labField:'scenario.family'},value:'higher_noise'};
    labEvents.input({target:scenario});labEvents.change({target:scenario});
    assert.match(app.innerHTML,/2×/);
    labEvents.input({target:{dataset:{labField:'controls.hnpe.width'},value:'256'}});
    labEvents.submit({target:{matches:()=>true},preventDefault(){}});
    await settle(/Campaign submitted/);
    const create=calls.find(c=>c.path.endsWith('/benchmarks')&&c.options.method==='POST');
    const payload=JSON.parse(create.options.body);
    assert.deepEqual(payload.scenario,{family:'higher_noise',scale:2});
    assert.equal(payload.controls.hnpe.width,256);
    assert.equal(payload.controls.hnpe.training_budget,1800);
    assert.equal(payload.budget.method_gpu_seconds,14400);
    assert.equal(payload.evaluation.case_count,16);
    assert.equal(create.options.headers.Authorization,'Bearer synthetic-memory-token');
    labClick('cancel');await settle(/Cancellation requested/);
    campaign={...campaign,status:'succeeded'};
    labClick('refresh-detail');await settle(/lab-status-succeeded/);
    labClick('results');
    for(let n=0;n<100&&!releaseResult;n++)await pause(2);
    click('tab',{tab:'workspace'});await settle(/Configure your analysis/);
    releaseResult();await pause(5);
    assert.doesNotMatch(app.innerHTML,/Synthetic marginal W2/);
    assert.match(app.innerHTML,/Upload &amp; verify|Select one CSV curve/);
    assert.ok(!calls.some(c=>/\/jobs$/.test(c.path)&&c.options.method==='POST'));
  }finally{Object.assign(globalThis,original);}
});
