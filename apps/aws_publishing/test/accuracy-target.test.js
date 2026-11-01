import test,{after} from 'node:test';
import assert from 'node:assert/strict';
import {createHash} from 'node:crypto';
import {writeFile} from 'node:fs/promises';
import {AccuracyLab,accuracyDraftError,accuracyAccounting} from '../src/benchmark-accuracy.js';
import {BenchmarkLab as Workspace} from '../src/benchmark-workspace.js';

const request={
  schema_version:2,scenario:{family:'baseline',scale:1},seed:1000,
  controls:{hnpe:{width:128,depth:2,learning_rate:0.0003,batch_size:256,max_updates:1000000,training_budget:1800},tmcmc:{population_size:4096,target_cov:0.6,proposal_scale:1,burn_in:7,max_generations:1000}},
  evaluation:{cohort:'central_90',guide_case_count:8,confirmation_case_count:256,posterior_draws:2048},
  budget:{mode:'accuracy_target',method_gpu_seconds:14400},
  accuracy_target:{profile_id:'w2-rms0p1-central90-2048-244of256-v1',effort_policy_id:'bounded-effort-ladders-v1',reference_profile_id:'independent-resolution-relative-reference-v1'},
};
const caps={schema_version:2,enabled:true,reason:null,max_artifact_bytes:1048576,defaults:request,limits:{hnpe:{width:[64,128,256],depth:[1,2,3],learning_rate:[0.0001,0.0003],batch_size:[256,512,1024,2048],max_updates:{min:1,max:1000000},training_budget:{min:120,max:14400}},tmcmc:{population_size:[2048,4096,8192],target_cov:[0.4,0.6,0.8],proposal_scale:[0.25,0.5,1],burn_in:[1,7,10],max_generations:{min:100,max:10000}},method_gpu_seconds:{min:1,max:2147483647}}};
const copy=value=>structuredClone(value);
const deferred=()=>{let resolve,reject;const promise=new Promise((a,b)=>{resolve=a;reject=b;});return {promise,resolve,reject};};
const cid='1234567890abcdef1234567890abcdef';
const campaign={id:cid,schema_version:2,configuration:request,status:'completed',methods:{hnpe:{status:'terminal_unmet'},tmcmc:{status:'frozen'}},archive:{status:'not_requested',available:true},accounting:{methods:{hnpe:{allocated_gpu_seconds:0,reserved_gpu_seconds:0,cpu_seconds:0,unknown_allocations:0}},reference:{allocated_gpu_seconds:null,reserved_gpu_seconds:0,cpu_seconds:null,unknown_allocations:1}},result:{artifact_id:'result'},tasks:[]};
const captured=[];
function setup(handler=async(url,body)=>url.endsWith('/capabilities')?caps:url.includes('/campaigns?')?{campaigns:[]}:{campaign}){
  const calls=[],downloads=[],api={project:(project,suffix)=>'/v1/projects/'+encodeURIComponent(project)+suffix,request:async(...args)=>{calls.push(args);return copy(await handler(...args));},validateStorageUrl:value=>{if(!value.startsWith('https://files.example.test/'))throw Error('Unexpected storage host');}};
  let context={api,project:'mesouq',valid:value=>value===api};let ticks=0;
  const lab=new AccuracyLab({context:()=>context,onChange:()=>{},download:(...args)=>downloads.push(args),now:()=>ticks});
  lab.capabilities=copy(caps);lab.draft=copy(request);
  return {lab,api,calls,downloads,setContext:value=>{context=value;},tick:value=>{ticks=value;}};
}

test('published default request and locked assessment are valid',()=>assert.equal(accuracyDraftError(request,caps.limits),null));
test('locked evaluation and profile cannot be silently changed',()=>{for(const change of [d=>d.evaluation.confirmation_case_count=16,d=>d.evaluation.posterior_draws=256,d=>d.accuracy_target.profile_id='other']){const d=copy(request);change(d);assert.ok(accuracyDraftError(d,caps.limits));}});
test('baseline multiplier, boolean integers and out-of-menu controls are rejected',()=>{for(const change of [d=>d.scenario.scale=1.25,d=>d.seed=true,d=>d.controls.hnpe.depth=4,d=>d.controls.hnpe.max_updates=1.5,d=>d.budget.method_gpu_seconds=NaN]){const d=copy(request);change(d);assert.ok(accuracyDraftError(d,caps.limits));}});
test('selected noise and prior settings remain independent requests',async()=>{for(const [family,scale] of [['baseline',1],['higher_noise',1.25],['broader_priors',1.5]]){const {lab,calls}=setup();lab.draft.scenario={family,scale};await lab.submit();const body=calls[0][1];assert.equal(body.scenario.family,family);assert.equal(body.scenario.scale,scale);assert.equal(body.evaluation.confirmation_case_count,256);assert.equal(typeof body.idempotency_key,'string');captured.push(body);}});
test('rapid duplicate clicks issue exactly one pending submission',async()=>{const gate=deferred();const {lab,calls}=setup(()=>gate.promise);const one=lab.submit();const first=copy(calls[0][1]);await lab.submit();assert.equal(calls.length,1);lab.draft.controls.hnpe.width=64;gate.resolve({campaign});await one;assert.equal(first.controls.hnpe.width,128);assert.equal(calls[0][1].idempotency_key,first.idempotency_key);assert.equal(lab.detail.id,cid);});
test('ambiguous submission reconciles the same frozen body and nonce',async()=>{let attempt=0;const {lab,calls}=setup(async()=>{if(++attempt===1)throw Error('Network unavailable');return {campaign};});await lab.submit();assert.match(lab.error,/original submission/i);assert.ok(lab.intent);await lab.submit();assert.equal(calls.length,1);const body=copy(calls[0][1]);await lab.reconcileSubmission();assert.equal(calls.length,2);assert.deepEqual(calls[1][1],body);assert.equal(lab.intent,null);});
test('project reset rejects a late submission response',async()=>{const gate=deferred();const s=setup(()=>gate.promise);const pending=s.lab.submit();s.lab.reset();s.setContext({api:s.api,project:'other',valid:()=>true});gate.resolve({campaign});await pending;assert.equal(s.lab.detail,null);assert.equal(s.lab.intent,null);});
test('capability activation never bypasses server scientific approval',async()=>{const s=setup(async()=>({enabled:false,reason:'Scientific release pending'}));s.lab.capabilities=null;s.lab.draft=null;await s.lab.loadCapabilities();await s.lab.submit();assert.equal(s.calls.length,1);assert.match(s.lab.view(),/Scientific release pending/);});
test('late capabilities from a different project are discarded',async()=>{const gate=deferred();const s=setup(()=>gate.promise);s.lab.capabilities=null;const pending=s.lab.loadCapabilities();s.lab.reset();s.setContext({api:s.api,project:'other',valid:()=>true});gate.resolve(caps);await pending;assert.equal(s.lab.capabilities,null);});
test('wrapper preserves fixed-budget mode and gates accuracy entry',()=>{const options={context:()=>({api:{},project:'mesouq',valid:()=>true}),onChange:()=>{},download:()=>{}};const workspace=new Workspace(options);assert.equal(workspace.mode,'fixed_budget');assert.match(workspace.view(),/data-benchmark-mode="accuracy_target"[^>]*disabled/);workspace.accuracy.capabilities=caps;assert.doesNotMatch(workspace.view(),/data-benchmark-mode="accuracy_target"[^>]*disabled/);});
test('unknown allocated cost is never displayed as zero',()=>{const html=accuracyAccounting(campaign.accounting);assert.match(html,/>0<\/td>/);assert.match(html,/Incomplete/);assert.match(html,/Unresolved allocations/);});
test('completed or archived campaigns are not polled; active campaigns are throttled',async()=>{const s=setup();s.lab.detail=copy(campaign);await s.lab.poll();assert.equal(s.calls.length,0);s.lab.detail.status='running';await s.lab.poll();assert.equal(s.calls.length,2);await s.lab.poll();assert.equal(s.calls.length,2);s.tick(15001);s.lab.detail.status='running';await s.lab.poll();assert.equal(s.calls.length,4);});
test('task/scientific values are escaped and no target threshold is computed in the view',()=>{const s=setup();s.lab.detail=copy(campaign);s.lab.detail.scientific_status='<script>bad</script>';s.lab.detail.tasks=[{kind:'<img>',status:'failed',attempt:1,factor:'<svg>'}];s.lab.result={scientific_status:'terminal_unmet',qualification:{source_flag:'<iframe>'}};const html=s.lab.view();assert.doesNotMatch(html,/<script>bad|<img>|<svg>|<iframe>/);assert.match(html,/&lt;iframe&gt;/);assert.match(html,/terminal unmet/);});
test('archival requires the server capability, not just a terminal status',()=>{const s=setup();s.lab.detail=copy(campaign);s.lab.detail.archive.available=false;assert.match(s.lab.detailView(),/data-accuracy-action="archive" disabled/);s.lab.detail.archive.available=true;assert.doesNotMatch(s.lab.detailView(),/data-accuracy-action="archive" disabled/);});

function downloadSetup({corrupt=false,foreign=false,deferredMetadata=null}={}){
  const bytes=new TextEncoder().encode('{"synthetic":true}'),sha=createHash('sha256').update(bytes).digest('hex');
  const artifact={id:'a'.repeat(32),name:'accuracy-case-scores.json',size_bytes:bytes.length,sha256:sha,media_type:'application/json',kind:'numerical'};
  const s=setup(async()=>deferredMetadata?deferredMetadata.promise:{artifact,url:foreign?'https://foreign.example.test/data':'https://files.example.test/data'});
  s.lab.detail=copy(campaign);s.lab.artifacts=[artifact];let fetchCalls=0,options;
  s.lab.fetcher=async(url,init)=>{fetchCalls++;options=init;const data=corrupt?new Uint8Array(bytes.length).fill(1):bytes;return new Response(data,{status:200,headers:{'content-type':'application/json','content-length':String(data.length)}});};
  return {...s,artifact,bytes,getFetchCalls:()=>fetchCalls,getFetchOptions:()=>options};
}
test('numerical download verifies metadata, exact bytes and checksum before saving',async()=>{const s=downloadSetup();await s.lab.downloadArtifact(s.artifact.id);assert.equal(s.downloads.length,1);assert.equal(await s.downloads[0][1].text(),'{"synthetic":true}');assert.equal(s.getFetchOptions().credentials,'omit');assert.equal(s.getFetchOptions().redirect,'error');});
test('corrupt numerical bytes are rejected',async()=>{const s=downloadSetup({corrupt:true});await s.lab.downloadArtifact(s.artifact.id);assert.equal(s.downloads.length,0);assert.match(s.lab.error,/checksum|size/);});
test('foreign private-download hosts are rejected before any file request',async()=>{const s=downloadSetup({foreign:true});await s.lab.downloadArtifact(s.artifact.id);assert.equal(s.getFetchCalls(),0);assert.equal(s.downloads.length,0);});
test('artifact descriptor cannot exceed the published source limit',async()=>{const s=downloadSetup();s.lab.capabilities.max_artifact_bytes=1;await s.lab.downloadArtifact(s.artifact.id);assert.equal(s.calls.length,0);assert.equal(s.downloads.length,0);});
test('late artifact metadata after project reset cannot initiate a file fetch',async()=>{const gate=deferred(),s=downloadSetup({deferredMetadata:gate});const pending=s.lab.downloadArtifact(s.artifact.id);s.lab.reset();s.setContext({api:s.api,project:'other',valid:()=>true});gate.resolve({artifact:s.artifact,url:'https://files.example.test/data'});await pending;assert.equal(s.getFetchCalls(),0);assert.equal(s.downloads.length,0);});
test('source scientific results and figure access use only the accuracy route',async()=>{const png=Uint8Array.from(Buffer.from('iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+aL1kAAAAASUVORK5CYII=','base64')),sha=createHash('sha256').update(png).digest('hex');const artifact={id:'b'.repeat(32),name:'metrics-posterior-marginals.png',size_bytes:png.length,sha256:sha,media_type:'image/png'};
  const s=setup(async url=>url.endsWith('/results')?{result:{scientific_status:'terminal_unmet',qualification:{source_only:true}},accounting:copy(campaign.accounting),raw_result_artifact:{id:'c'.repeat(32),name:'comparison.json',size_bytes:2,sha256:'d'.repeat(64),media_type:'application/json'},artifacts:[artifact]}:{artifact,url:'https://files.example.test/figure'});
  s.lab.detail=copy(campaign);s.lab.fetcher=async()=>new Response(png,{status:200,headers:{'content-type':'image/png','content-length':String(png.length)}});
  await s.lab.loadResults();assert.equal(s.lab.result.scientific_status,'terminal_unmet');assert.equal(s.lab.figures[0].status,'ready');assert.match(s.lab.figures[0].src,/^data:image\/png;base64,/);assert.ok(s.calls.every(([url])=>url.includes('/benchmark-accuracy/')));
});

test('mode controls fence disabled entry and switch only the selected component',async()=>{
  let entered=0,left=0,changes=0;
  const workspace=new Workspace({context:()=>({api:{},project:'mesouq',valid:()=>true}),onChange:()=>changes++,download:()=>{}});
  workspace.fixed.mount=()=>{};workspace.fixed.leave=()=>left++;workspace.accuracy.enter=async()=>entered++;
  const buttons={fixed_budget:{},accuracy_target:{}};
  const container={querySelector:selector=>selector.includes('accuracy_target')?buttons.accuracy_target:selector.includes('fixed_budget')?buttons.fixed_budget:null};
  workspace.mount(container,null);await buttons.accuracy_target.onclick();assert.equal(workspace.mode,'fixed_budget');assert.equal(entered,0);
  workspace.accuracy.capabilities=caps;await buttons.accuracy_target.onclick();
  assert.equal(workspace.mode,'accuracy_target');assert.equal(left,1);assert.equal(entered,1);assert.equal(changes,1);
});


test('project change during campaign refresh prevents the old follow-up detail request',async()=>{
  const gate=deferred(),s=setup(()=>gate.promise);s.lab.detail=copy(campaign);
  const pending=s.lab.refresh();s.lab.reset();s.setContext({api:s.api,project:'other',valid:()=>true});
  gate.resolve({campaigns:[campaign]});await pending;
  assert.equal(s.calls.length,1);assert.deepEqual(s.lab.campaigns,[]);assert.equal(s.lab.detail,null);
});
test('returning to the list fences a late detail refresh',async()=>{
  const gate=deferred();let requests=0;
  const s=setup(async()=>++requests===1?{campaigns:[]}:requests===2?gate.promise:{campaigns:[]});
  s.lab.detail=copy(campaign);const pending=s.lab.refresh();await new Promise(resolve=>setImmediate(resolve));
  s.lab.backToList();gate.resolve({campaign});await pending;await new Promise(resolve=>setImmediate(resolve));
  assert.equal(s.lab.detail,null);assert.equal(s.lab.result,null);
});
test('task progress displays only supplied verified updates and losses, including zero',()=>{
  const s=setup();s.lab.detail=copy(campaign);s.lab.detail.tasks=[{kind:'hnpe_advance',factor:'acoustic_1',attempt:1,status:'succeeded',progress:{optimizer_updates:0,validation_nll:-2.25,best_validation_nll:-2.5,stop_reason:'<script>'}}];
  const html=s.lab.detailView();assert.match(html,/Last verified progress/);assert.match(html,/Optimizer updates: 0/);assert.match(html,/Best validation NLL: -2.5/);assert.match(html,/&lt;script&gt;/);assert.doesNotMatch(html,/<script>/);
});


test('campaign and task pagination preserve supplied ordering, escape cursors and remove duplicates',async()=>{
  const next={...copy(campaign),id:'e'.repeat(32)};
  const s=setup(async url=>url.includes('/tasks?')?{tasks:[{id:'one',kind:'plan'},{id:'two',kind:'hnpe_advance'}],next_cursor:null}:{campaigns:[campaign,next],next_cursor:null});
  s.lab.campaigns=[copy(campaign)];s.lab.campaignCursor='a+b/&';
  await s.lab.loadMoreCampaigns();assert.deepEqual(s.lab.campaigns.map(item=>item.id),[cid,next.id]);assert.match(s.calls[0][0],/cursor=a%2Bb%2F%26/);assert.equal(s.lab.campaignCursor,null);
  s.lab.detail={...copy(campaign),tasks:[{id:'one',kind:'plan'}],next_task_cursor:'c+d'};
  await s.lab.loadMoreTasks();assert.deepEqual(s.lab.detail.tasks.map(item=>item.id),['one','two']);assert.equal(s.lab.detail.next_task_cursor,null);
});
test('campaign criterion, budget outcome and population confidence are shown as distinct source claims',()=>{
  const s=setup();s.lab.detail=copy(campaign);s.lab.result={methods:{
    hnpe:{accuracy_target_within_budget:true,verdict:{status:'attained',observed_attainment:true,population_qualified:false,observed_pass_fraction:0.96}},
    tmcmc:{accuracy_target_within_budget:false,verdict:{status:'unmet',observed_attainment:false,population_qualified:false}},
  },paired_comparison:{paired_case_count:250,requested_case_count:256,mean_score_difference_hnpe_minus_tmcmc:0.02,scope:'descriptive paired comparison'}};
  const html=s.lab.detailView();assert.match(html,/Observed campaign criterion/);assert.match(html,/Met within budget/);assert.match(html,/Population confidence claim/);assert.match(html,/Not established within budget/);assert.match(html,/Paired cases: 250 \/ 256/);assert.doesNotMatch(html,/p-value|significant/);
});


test('known experimental budget overrun stays visible beside the unmodified source outcome',()=>{
  const s=setup();s.lab.detail=copy(campaign);s.lab.detail.budget_exceeded=true;
  Object.assign(s.lab.detail.accounting.methods.hnpe,{limit_gpu_seconds:14400,allocated_gpu_seconds:14401,budget_exceeded:true});
  s.lab.result={methods:{hnpe:{within_method_ceiling:false,within_factor_ceilings:true,accuracy_target_within_budget:false,verdict:{status:'unmet',observed_attainment:true,population_qualified:false}}}};
  const html=s.lab.detailView();assert.match(html,/experimental method budget was exceeded/);assert.match(html,/Method GPU s limit/);assert.match(html,/>14,400</);assert.match(html,/>14,401</);assert.match(html,/>Exceeded</);assert.match(html,/Outside ceiling/);assert.match(html,/Not established within budget/);assert.equal(s.lab.result.methods.hnpe.verdict.observed_attainment,true);
});

after(async()=>{if(process.env.ACCURACY_UI_REQUEST_CAPTURE)await writeFile(process.env.ACCURACY_UI_REQUEST_CAPTURE,JSON.stringify(captured,null,2)+'\n',{mode:0o600});});


function sourceResultSetup({rawInList=false}={}){
  const s=downloadSetup(),original=s.api.request;
  const summary={schema:'source-display-summary.v1',scientific_status:'terminal_unmet',methods:{hnpe:{verdict:{status:'unmet',observed_attainment:false}}}};
  const response={result:summary,accounting:copy(campaign.accounting),raw_result_artifact:copy(s.artifact),artifacts:rawInList?[copy(s.artifact)]:[]};
  s.api.request=async(...args)=>{if(args[0].endsWith('/results')){s.calls.push(args);return copy(response);}return original(...args);};
  return {...s,summary,response};
}
test('bounded source summary and full raw result remain distinct through verified download',async()=>{
  const s=sourceResultSetup();await s.lab.loadResults();assert.deepEqual(s.lab.result,s.summary);
  assert.deepEqual(s.lab.rawResultArtifact,s.artifact);assert.deepEqual(s.lab.artifacts,[]);
  assert.match(s.lab.detailView(),/Download full result JSON/);await s.lab.downloadResult();
  assert.equal(s.downloads.length,1);assert.equal(await s.downloads[0][1].text(),'{"synthetic":true}');
  assert.notEqual(await s.downloads[0][1].text(),JSON.stringify(s.summary));
  assert.match(s.calls.at(-1)[0],new RegExp('/artifacts/'+s.artifact.id+'$'));
});
test('missing full result descriptor fails closed instead of exporting the summary',async()=>{
  const s=sourceResultSetup();delete s.response.raw_result_artifact;await s.lab.loadResults();await s.lab.downloadResult();
  assert.equal(s.lab.result,null);assert.equal(s.lab.rawResultArtifact,null);assert.equal(s.downloads.length,0);
  assert.match(s.lab.error,/full scientific result.*metadata/i);assert.doesNotMatch(s.lab.detailView(),/data-accuracy-action="result-json"/);
});
test('full result descriptor must be JSON within the published byte limit',async()=>{
  for(const mutate of [r=>r.media_type='image/png',r=>r.size_bytes=caps.max_artifact_bytes+1,r=>r.sha256='not-a-digest']){
    const s=sourceResultSetup();mutate(s.response.raw_result_artifact);await s.lab.loadResults();
    assert.equal(s.lab.rawResultArtifact,null);assert.equal(s.lab.result,null);assert.equal(s.getFetchCalls(),0);assert.match(s.lab.error,/metadata/i);
  }
});
test('conflicting curated and full result descriptors are rejected',async()=>{
  const s=sourceResultSetup({rawInList:true});s.response.artifacts[0].sha256='f'.repeat(64);await s.lab.loadResults();
  assert.equal(s.lab.result,null);assert.equal(s.lab.rawResultArtifact,null);assert.match(s.lab.error,/conflicting/i);
});
test('a late source-summary response cannot restore a previous project full download',async()=>{
  const gate=deferred(),s=setup(()=>gate.promise);s.lab.detail=copy(campaign);
  const pending=s.lab.loadResults();s.lab.reset();s.setContext({api:s.api,project:'other',valid:()=>true});
  gate.resolve({result:{schema:'summary'},accounting:copy(campaign.accounting),raw_result_artifact:sourceResultSetup().artifact,artifacts:[]});await pending;
  assert.equal(s.lab.result,null);assert.equal(s.lab.rawResultArtifact,null);assert.deepEqual(s.downloads,[]);
});
test('full result action calls the checked downloader and respects busy state',async()=>{
  const s=sourceResultSetup();await s.lab.loadResults();let selected=0;s.lab.downloadResult=async()=>selected++;
  const button={dataset:{accuracyAction:'result-json'}};
  s.lab.mount({querySelector:()=>null,querySelectorAll:q=>q==='[data-accuracy-action]'?[button]:[]},null);
  button.onclick();assert.equal(selected,1);assert.equal(s.downloads.length,0);
  s.lab.loading=true;assert.match(s.lab.detailView(),/data-accuracy-action="result-json" disabled/);
});
test('full download metadata clears when returning to the campaign list',async()=>{
  const s=sourceResultSetup();await s.lab.loadResults();s.lab.backToList();
  assert.equal(s.lab.rawResultArtifact,null);assert.equal(s.lab.result,null);
});


function partialArtifactSetup(){
  const s=downloadSetup(),original=s.api.request;
  const partial={...copy(campaign),status:'cancelled',result:null,verified_artifacts:[copy(s.artifact)]};
  s.api.request=async(...args)=>{
    if(args[0].endsWith('/campaigns/'+cid)){s.calls.push(args);return {campaign:copy(partial)};}
    if(args[0].includes('/campaigns?')){s.calls.push(args);return {campaigns:[]};}
    return original(...args);
  };
  return {...s,partial};
}
test('cancelled partial files are discoverable without a completed scientific result',async()=>{
  const s=partialArtifactSetup();await s.lab.open(cid);
  assert.deepEqual(s.lab.artifacts,[s.artifact]);assert.equal(s.lab.result,null);assert.equal(s.lab.rawResultArtifact,null);
  const html=s.lab.detailView();assert.match(html,/Available files/);assert.match(html,/no completed scientific result/);
  assert.doesNotMatch(html,/Scientific assessment|data-accuracy-action="result-json"/);assert.deepEqual(s.lab.figures,[]);
});
test('cancelled partial download still verifies original bytes and checksum',async()=>{
  const s=partialArtifactSetup();await s.lab.open(cid);assert.equal(s.getFetchCalls(),0);
  await s.lab.downloadArtifact(s.artifact.id);assert.equal(s.downloads.length,1);
  assert.equal(await s.downloads[0][1].text(),'{"synthetic":true}');
  assert.equal(s.lab.result,null);assert.equal(s.lab.rawResultArtifact,null);
});
test('campaign refresh discovers newly verified partial artifacts',async()=>{
  const s=partialArtifactSetup();s.lab.detail={...copy(s.partial),verified_artifacts:[]};s.lab.artifacts=[];
  await s.lab.refresh();assert.deepEqual(s.lab.artifacts,[s.artifact]);assert.equal(s.lab.result,null);
});
test('opening an incomplete campaign clears the previous summary and full-result handle',async()=>{
  const s=partialArtifactSetup();s.lab.result={schema:'previous-summary'};s.lab.rawResultArtifact=copy(s.artifact);
  await s.lab.open(cid);assert.equal(s.lab.result,null);assert.equal(s.lab.rawResultArtifact,null);
  assert.deepEqual(s.lab.artifacts,[s.artifact]);assert.deepEqual(s.lab.figures,[]);
});

test('final authoritative accounting replaces the detail snapshot when results load',async()=>{
  const final=copy(campaign.accounting);final.methods.hnpe={allocated_gpu_seconds:321,cpu_seconds:5136,reserved_gpu_seconds:0,unknown_allocations:0,budget_exceeded:true};
  const raw={id:'c'.repeat(32),name:'comparison.json',size_bytes:2,sha256:'d'.repeat(64),media_type:'application/json'};
  const s=setup(async()=>({result:{scientific_status:'terminal_unmet'},raw_result_artifact:raw,artifacts:[],accounting:final}));
  s.lab.detail=copy(campaign);await s.lab.loadResults();
  assert.deepEqual(s.lab.detail.accounting,final);assert.ok(s.lab.detailView().includes(accuracyAccounting(final)));
  assert.equal(s.lab.detail.budget_exceeded,undefined);assert.equal(s.lab.result.scientific_status,'terminal_unmet');
});
test('invalid final accounting cannot expose a completed result with stale costs',async()=>{
  for(const accounting of [undefined,null,[],7]){
    const raw={id:'c'.repeat(32),name:'comparison.json',size_bytes:2,sha256:'d'.repeat(64),media_type:'application/json'};
    const s=setup(async()=>({result:{scientific_status:'terminal_unmet'},raw_result_artifact:raw,artifacts:[],accounting}));
    s.lab.detail=copy(campaign);await s.lab.loadResults();
    assert.match(s.lab.error,/final authoritative accounting/i);assert.equal(s.lab.result,null);assert.equal(s.lab.rawResultArtifact,null);
    assert.deepEqual(s.lab.detail.accounting,campaign.accounting);
  }
});
test('late final accounting cannot change another campaign context',async()=>{
  const gate=deferred(),s=setup(()=>gate.promise),raw={id:'c'.repeat(32),name:'comparison.json',size_bytes:2,sha256:'d'.repeat(64),media_type:'application/json'};
  s.lab.detail=copy(campaign);const pending=s.lab.loadResults();s.lab.reset();s.setContext({api:s.api,project:'other',valid:()=>true});
  gate.resolve({result:{scientific_status:'terminal_unmet'},raw_result_artifact:raw,artifacts:[],accounting:campaign.accounting});await pending;
  assert.equal(s.lab.detail,null);assert.equal(s.lab.result,null);
});
