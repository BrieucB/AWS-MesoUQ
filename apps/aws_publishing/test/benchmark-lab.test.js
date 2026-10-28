import test from 'node:test';
import assert from 'node:assert/strict';
import {benchmarkPlot,benchmarkResults} from '../src/benchmark-lab.js';
test('synthetic server plot series render as escaped, finite SVG without computing posteriors',()=>{
  const plot={title:'Synthetic <posterior>',x_label:'ka',y_label:'Density',series:[{name:'Reference <unsafe>',x:[1,2,3],y:[0,1,0]},{name:'HNPE',x:[1,2,3],y:[0,.8,0]}]};
  const markup=benchmarkPlot(plot);
  assert.match(markup,/Synthetic &lt;posterior&gt;/);
  assert.match(markup,/Reference &lt;unsafe&gt;/);
  assert.equal((markup.match(/class="lab-chart-line"/g)||[]).length,2);
  assert.doesNotMatch(markup,/NaN|Infinity|onerror=|style=/);
  assert.deepEqual(plot.series[0].y,[0,1,0]);
  for(const bad of [null,{series:[]},{series:[{x:[1],y:[Infinity]}]},{series:[{x:[1,2],y:[1]}]}])assert.doesNotMatch(benchmarkPlot(bad),/<svg/);
});
test('results present reported method totals and separate reference cost without inventing missing values',()=>{
  const result={costs:{methods:[{method:'HNPE',training_seconds:20,inference_seconds:2,total_seconds:22},{method:'TMCMC',inference_seconds:8,total_seconds:8}],reference_seconds:7},metrics:{'Marginal W2':.08},training_histories:[],failures:[{stage:'Synthetic stage',message:'<unsafe>'}]};
  const markup=benchmarkResults(result);
  assert.match(markup,/Numerical-reference summed stage elapsed: 7 seconds/);
  assert.match(markup,/>22<\/td>/);
  assert.match(markup,/Marginal W2/);
  assert.match(markup,/&lt;unsafe&gt;/);
  assert.match(markup,/Training history not reported/);
  assert.match(markup,/Not reported/);
  assert.doesNotMatch(markup,/<unsafe>/);
  assert.match(benchmarkResults(null),/publishes verified evidence/);
});

import {BenchmarkLab,benchmarkDraftError} from '../src/benchmark-lab.js';
const defaults={scenario:{family:'baseline',scale:1},seed:1000,controls:{hnpe:{width:128,depth:2,learning_rate:.0003,batch_size:2048,max_updates:1000000,training_budget:1800},tmcmc:{population_size:4096,target_cov:.6,proposal_scale:1,burn_in:7,max_generations:1000}},evaluation:{case_count:16,posterior_draws:2048,cohort:'central_90'},budget:{mode:'fixed',method_gpu_seconds:14400},reference:{initial_power:14,maximum_power:20,scrambles:4,relative_tolerance:.02}};
function setup(request){
  const calls=[],downloads=[],context={project:'p',api:null,valid:()=>true};
  context.api={project:(p,suffix)=>'/v1/projects/'+p+suffix,request:async(path,body)=>{calls.push({path,body});return request(path,body);}};
  const lab=new BenchmarkLab({context:()=>context,onChange:()=>{},download:(...args)=>downloads.push(args)});
  return {lab,context,calls,downloads};
}
test('synthetic capabilities control form choices and exact fresh-campaign request/idempotent retry',async()=>{
  let attempts=0;
  const f=setup(async(path,body)=>{
    if(path.endsWith('/capabilities'))return {capabilities:{defaults,limits:{'controls.hnpe.width':{choices:[64,128,256]}},actions:{archive:true}}};
    if(body){if(++attempts===1)throw Error('private signed URL');return {campaign:{id:'benchmark-1',status:'pending',configuration:body}};}
    return {campaigns:[]};
  });
  await f.lab.enter();
  assert.match(f.lab.view(),/Maximum fit time per factor \(s\)/);
  assert.match(f.lab.view(),/value="128" selected/);
  assert.match(f.lab.view(),/Scenario multiplier/);
  assert.match(f.lab.view(),/adds no global GPU-hour quota/);
  await f.lab.submit();
  assert.doesNotMatch(f.lab.error,/private signed/);
  await f.lab.submit();
  const creates=f.calls.filter(c=>c.body);
  assert.equal(creates.length,2);
  assert.equal(creates[0].body.idempotency_key,creates[1].body.idempotency_key);
  assert.deepEqual(creates[1].body,{schema_version:1,...defaults,idempotency_key:creates[1].body.idempotency_key});
  assert.match(f.lab.notice,/fresh HNPE bundle/);
  assert.deepEqual(defaults.scenario,{family:'baseline',scale:1});
});
test('late capability/results responses cannot repopulate a different project or a cleared session',async()=>{
  let resolve;
  const f=setup(path=>path.endsWith('/capabilities')?new Promise(r=>resolve=r):Promise.resolve({campaigns:[]}));
  const pending=f.lab.enter();
  f.context.project='other';f.lab.reset();
  resolve({capabilities:{defaults}});
  await pending;
  assert.equal(f.lab.draft,null);
  assert.equal(f.lab.campaigns.length,0);
  f.context.project='p';
  f.context.api.request=async(path)=>path.endsWith('/capabilities')?{capabilities:{defaults}}:{campaigns:[]};
  await f.lab.enter();f.lab.detail={id:'b',status:'succeeded'};
  f.context.api.request=()=>new Promise(r=>resolve=r);
  const result=f.lab.loadResults();
  f.lab.leave();
  resolve({result:{metrics:{leak:1}}});
  await result;
  assert.equal(f.lab.result,null);
});
test('same-context session validity is rechecked after responses and unapproved archive stays hidden',async()=>{
  let valid=true,resolve;
  const f=setup(async path=>path.endsWith('/capabilities')?{capabilities:{defaults}}:{campaigns:[]});
  f.context.valid=()=>valid;
  await f.lab.enter();f.lab.detail={id:'b',status:'succeeded'};
  assert.doesNotMatch(f.lab.view(),/data-lab-action="archive"/);
  f.context.api.request=()=>new Promise(r=>resolve=r);
  const pending=f.lab.loadResults();
  valid=false;resolve({result:{metrics:{private:1}}});
  await pending;
  assert.equal(f.lab.result,null);
});
test('campaign cancellation and archival use explicit existing API routes and keep evidence',async()=>{
  const f=setup(async(path,body)=>{
    if(path.endsWith('/capabilities'))return {capabilities:{defaults,actions:{archive:true}}};
    if(path.endsWith('/cancel'))return {campaign:{id:'b',status:'cancel_requested'}};
    if(path.endsWith('/archive'))return {campaign:{id:'b',status:'completed',archive:{status:'requested'},artifacts:[{id:'a',name:'evidence.json'}]}};
    return {campaigns:[]};
  });
  await f.lab.enter();f.lab.detail={id:'b',status:'running'};
  await f.lab.changeCampaign('cancel');
  assert.equal(f.lab.detail.status,'cancel_requested');
  f.lab.detail.status='cancelled';
  await f.lab.changeCampaign('archive');
  assert.equal(f.lab.detail.artifacts[0].id,'a');
  assert.equal(f.lab.detail.status,'completed');
  assert.match(f.lab.view(),/PROJECT archive: <strong>requested/);
  assert.doesNotMatch(f.lab.notice,/Campaign archived/);
  assert.equal(f.calls.at(-1).path,'/v1/projects/p/benchmarks/b/archive');
  assert.deepEqual(f.calls.at(-1).body,{});
});

test('artifact downloads verify selected references and bytes without sending API auth to storage',async()=>{
  const bytes=new TextEncoder().encode('synthetic benchmark evidence');
  const sha=Array.from(new Uint8Array(await crypto.subtle.digest('SHA-256',bytes)),b=>b.toString(16).padStart(2,'0')).join('');
  const artifact={id:'artifact-1',name:'synthetic-evidence.json',sha256:sha,size_bytes:bytes.length};
  const f=setup(async path=>path.endsWith('/capabilities')?{capabilities:{defaults}}:path.includes('/artifacts/')?{artifact,download_url:'https://storage.example.test/synthetic?signature=private'}:{campaigns:[]});
  const storageCalls=[];
  f.context.api.validateStorageUrl=url=>{assert.equal(new URL(url).origin,'https://storage.example.test');storageCalls.push('validated');};
  f.lab.fetcher=async function(url,options){
    assert.equal(this,globalThis);assert.equal(options.credentials,'omit');assert.equal(options.redirect,'error');assert.equal(options.cache,'no-store');assert.equal(options.headers,undefined);
    storageCalls.push('fetched');return new Response(bytes);
  };
  await f.lab.enter();f.lab.detail={id:'benchmark-1'};f.lab.result={artifacts:[artifact]};
  await f.lab.downloadArtifact(artifact.id);
  assert.deepEqual(storageCalls,['validated','fetched']);
  assert.equal(f.downloads.length,1);
  assert.equal(f.downloads[0][0],artifact.name);
  f.lab.fetcher=async()=>new Response(new Uint8Array(bytes.length));
  await f.lab.downloadArtifact(artifact.id);
  assert.equal(f.downloads.length,1);
  assert.match(f.lab.error,/could not complete/);
});

test('primary comparison uses paired scores and allocated resources, with other native accounting separate',()=>{
  const result={paired_comparison:{paired_case_count:3,requested_case_count:5,methods:{hnpe:{mean_score:.12,median_score:.1},tmcmc:{mean_score:null,median_score:null}},mean_score_difference_hnpe_minus_tmcmc:null},allocation_costs:{methods:{hnpe:{allocated_gpu_seconds:200,allocated_cpu_seconds:1600,status:'complete',missing_stage_ids:[]},tmcmc:{allocated_gpu_seconds:null,allocated_cpu_seconds:null,status:'unknown',missing_stage_ids:['tmcmc']}},reference:{allocated_gpu_seconds:0,allocated_cpu_seconds:80,status:'complete',missing_stage_ids:[]}},metrics:{hnpe:{counts:{total:5,scored:4,method_failed:1,reference_unresolved:0,budget_exhausted:2},mean_score_on_scored_cases:99}},costs:{methods:[{method:'hnpe',total_seconds:17}]}};
  const markup=benchmarkResults(result),primary=markup.split('<details>')[0];
  assert.match(primary,/>3<\/strong> paired cases \/ <strong>5/);
  assert.match(primary,/Paired mean score/);assert.match(primary,/>0.12<\/td>/);
  assert.match(primary,/>200<\/td>/);assert.match(primary,/>1,600<\/td>/);
  assert.match(primary,/Allocated CPU \(core-s\)/);assert.match(primary,/>Unknown<\/td>/);
  assert.match(primary,/Numerical-reference allocation \(separate\)/);
  assert.doesNotMatch(primary,/>99<|>17<|Summed stage elapsed/);
  assert.match(markup,/Method-specific diagnostics \(unpaired\)/);
  assert.match(markup,/Method failures/);assert.match(markup,/Budget-exhausted cases/);
  assert.match(benchmarkResults({}),/Not reported/);
  assert.doesNotMatch(benchmarkResults({plots:[]}),/Posterior accuracy, tails and calibration/);
});
test('fresh campaign validation enforces integer training/method limits and strict reference ordering',()=>{
  assert.equal(benchmarkDraftError(defaults),'');
  for(const [path,value] of [['controls.hnpe.training_budget',119],['controls.hnpe.training_budget',120.5],['controls.hnpe.max_updates',1000001],['budget.method_gpu_seconds',.5],['reference.initial_power',3],['reference.maximum_power',23],['reference.scrambles',3],['reference.relative_tolerance',.11],['reference.relative_tolerance',0]]){
    const draft=structuredClone(defaults),keys=path.split('.');let item=draft;for(const key of keys.slice(0,-1))item=item[key];item[keys.at(-1)]=value;
    assert.notEqual(benchmarkDraftError(draft),'',path);
  }
  const equal=structuredClone(defaults);equal.reference.initial_power=20;equal.reference.maximum_power=20;
  assert.match(benchmarkDraftError(equal),/Reference/);
  assert.match(benchmarkDraftError(defaults,{'controls.hnpe.width':{choices:[64]}}),/choices/);
});
test('invalid draft is rejected before API create without rounding or minting an idempotency key',async()=>{
  const f=setup(async path=>path.endsWith('/capabilities')?{defaults}:{campaigns:[]});await f.lab.enter();
  f.lab.draft.controls.hnpe.training_budget=120.5;
  await f.lab.submit();assert.match(f.lab.error,/integer/);
  assert.equal(f.calls.filter(call=>call.body).length,0);assert.equal(f.lab.idempotencyKey,null);
});

test('archive admission disables no-execution campaigns and blocks direct requests without affecting eligible terminals',async()=>{
  const f=setup(async path=>path.endsWith('/capabilities')?{defaults,actions:{archive:true}}:{campaigns:[]});
  await f.lab.enter();
  f.lab.detail={id:'never-executed',status:'cancelled',archive_capability:{available:false,reason:'no_executed_scientific_stages'}};
  let markup=f.lab.view();
  assert.match(markup,/<button[^>]*data-lab-action="archive"[^>]*disabled/);
  assert.match(markup,/Archival is unavailable because no scientific stages executed and no scientific outputs were produced/);
  const before=f.calls.length;await f.lab.changeCampaign('archive');assert.equal(f.calls.length,before);
  f.lab.detail.archive_capability={available:false,reason:'<unknown reason>'};
  markup=f.lab.view();assert.match(markup,/Archival is unavailable for this campaign/);assert.doesNotMatch(markup,/<unknown reason>/);
  for(const capability of [undefined,{available:true}]){
    f.lab.detail.archive_capability=capability;
    markup=f.lab.view();
    const archiveButton=markup.match(/<button[^>]*data-lab-action="archive"[^>]*>/)?.[0];
    assert.ok(archiveButton);assert.doesNotMatch(archiveButton,/disabled/);
    assert.doesNotMatch(markup,/Archival is unavailable/);
  }
  f.lab.detail.archive={status:'verified'};
  assert.doesNotMatch(f.lab.view(),/data-lab-action="archive"/);
});
