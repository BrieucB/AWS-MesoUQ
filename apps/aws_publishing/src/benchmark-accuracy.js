import {escapeHtml as h} from './plots.js';
import {formatTimestamp} from './client.js';
import {benchmarkFigureArtifacts,loadBenchmarkFigure} from './benchmark-figures.js';

const terminal=new Set(['blocked','cancelled','failed','completed']);
const profiles=Object.freeze({profile_id:'w2-rms0p1-central90-2048-244of256-v1',effort_policy_id:'bounded-effort-ladders-v1',reference_profile_id:'independent-resolution-relative-reference-v1'});
const clone=value=>structuredClone(value);
const words=value=>String(value??'Awaiting assessment').replaceAll('_',' ');
const number=value=>typeof value==='number'&&Number.isFinite(value)?new Intl.NumberFormat('en',{maximumSignificantDigits:6}).format(value):'Incomplete';
const label=value=>h(words(value));
const path=(api,project,suffix)=>api.project(project,'/benchmark-accuracy'+suffix);
const fieldPaths=new Set(['seed','controls.hnpe.width','controls.hnpe.depth','controls.hnpe.learning_rate','controls.hnpe.batch_size','controls.hnpe.max_updates','controls.hnpe.training_budget','controls.tmcmc.population_size','controls.tmcmc.target_cov','controls.tmcmc.proposal_scale','controls.tmcmc.burn_in','controls.tmcmc.max_generations','budget.method_gpu_seconds']);
function get(object,key){return key.split('.').reduce((value,part)=>value?.[part],object);}
function put(object,key,value){const parts=key.split('.'),last=parts.pop();let target=object;for(const part of parts)target=target[part];target[last]=value;}
function sameContext(context,api,project){return context.api===api&&context.project===project&&(!context.valid||context.valid(api));}
function validRawResult(artifact,limit){
  return artifact&&typeof artifact==='object'&&!Array.isArray(artifact)&&
    typeof artifact.id==='string'&&artifact.id.length>0&&
    typeof artifact.name==='string'&&artifact.name.length>0&&
    artifact.media_type==='application/json'&&Number.isSafeInteger(limit)&&limit>0&&
    Number.isSafeInteger(artifact.size_bytes)&&artifact.size_bytes>0&&artifact.size_bytes<=limit&&
    typeof artifact.sha256==='string'&&/^[a-f0-9]{64}$/.test(artifact.sha256);
}

export function accuracyDraftError(draft,limits={}){
  if(!draft||draft.schema_version!==2||draft.budget?.mode!=='accuracy_target')return 'The accuracy profile is unavailable. Refresh this project.';
  if(!['baseline','higher_noise','broader_priors'].includes(draft.scenario?.family))return 'Choose a scientific scenario.';
  const scale=draft.scenario.scale;
  if(!Number.isFinite(scale)||scale<1||scale>3||(draft.scenario.family==='baseline'&&scale!==1))return 'Choose a scenario multiplier between 1 and 3.';
  if(!Number.isSafeInteger(draft.seed)||draft.seed<0)return 'Enter a non-negative integer seed.';
  for(const key of ['width','depth','learning_rate','batch_size'])if(limits.hnpe?.[key]&&!limits.hnpe[key].includes(draft.controls?.hnpe?.[key]))return 'Choose one of the published HNPE settings.';
  for(const key of ['population_size','target_cov','proposal_scale','burn_in'])if(limits.tmcmc?.[key]&&!limits.tmcmc[key].includes(draft.controls?.tmcmc?.[key]))return 'Choose one of the published TMCMC settings.';
  for(const [key,bounds] of [['controls.hnpe.max_updates',limits.hnpe?.max_updates],['controls.hnpe.training_budget',limits.hnpe?.training_budget],['controls.tmcmc.max_generations',limits.tmcmc?.max_generations],['budget.method_gpu_seconds',limits.method_gpu_seconds]]){
    const value=get(draft,key);
    if(!Number.isSafeInteger(value)||value<(bounds?.min??1)||value>(bounds?.max??2147483647))return 'Enter whole-number controls within the published limits.';
  }
  const evaluation=draft.evaluation;
  if(evaluation?.cohort!=='central_90'||evaluation.guide_case_count!==8||evaluation.confirmation_case_count!==256||evaluation.posterior_draws!==2048)return 'The locked evaluation profile changed. Refresh this project.';
  for(const [key,value] of Object.entries(profiles))if(draft.accuracy_target?.[key]!==value)return 'The approved accuracy profile changed. Refresh this project.';
  return null;
}

export function accuracyAccounting(accounting){
  const rows=[['HNPE',accounting?.methods?.hnpe],['TMCMC',accounting?.methods?.tmcmc],['Numerical reference',accounting?.reference],['Orchestration & reporting',accounting?.overhead]];
  return '<table><thead><tr><th>Computation</th><th>Method GPU s limit</th><th>Allocated GPU s</th><th>Reserved GPU s</th><th>CPU core s</th><th>Unresolved allocations</th><th>Budget state</th></tr></thead><tbody>'+rows.map(([name,item])=>{
    const limit=typeof item?.limit_gpu_seconds==='number'?number(item.limit_gpu_seconds):'—';
    const state=item?.budget_exceeded===true?'Exceeded':item?.budget_exceeded===false?'No excess recorded':'—';
    return '<tr><th scope="row">'+h(name)+'</th><td>'+h(limit)+'</td><td>'+h(number(item?.allocated_gpu_seconds))+'</td><td>'+h(number(item?.reserved_gpu_seconds))+'</td><td>'+h(number(item?.cpu_seconds))+'</td><td>'+h(number(item?.unknown_allocations))+'</td><td>'+h(state)+'</td></tr>';
  }).join('')+'</tbody></table>';
}

function verifiedTaskProgress(progress){
  if(!progress)return '—';
  const values=[];
  if(Number.isSafeInteger(progress.optimizer_updates)&&progress.optimizer_updates>=0)values.push('Optimizer updates: '+number(progress.optimizer_updates));
  for(const [key,title] of [['validation_nll','Validation NLL'],['best_validation_nll','Best validation NLL']])if(typeof progress[key]==='number'&&Number.isFinite(progress[key]))values.push(title+': '+number(progress[key]));
  if(progress.stop_reason)values.push('Stop reason: '+words(progress.stop_reason));
  return values.length?values.map(value=>h(value)).join('<br>'):'—';
}

function sourceAssessment(result,detail){
  const status=result?.scientific_status??detail?.scientific_status;
  const qualification=result?.qualification??detail?.result?.qualification;
  const flag=(value,yes,no)=>value===true?yes:value===false?no:'Not reported';
  const methods=['hnpe','tmcmc'].filter(name=>result?.methods?.[name]?.verdict);
  const cards=methods.map(name=>{
    const method=result.methods[name],v=method.verdict;
    const details=[['Observed pass fraction',v.observed_pass_fraction],['Possible pass fraction',v.possible_pass_fraction],['Minimum observed passes',v.minimum_observed_passes]];
    return '<article><h4>'+name.toUpperCase()+'</h4><p><strong>'+label(v.status)+'</strong></p><dl><dt>Observed campaign criterion</dt><dd>'+h(flag(v.observed_attainment,'Met','Not met'))+'</dd><dt>Accuracy target within budget</dt><dd>'+h(flag(method.accuracy_target_within_budget,'Met within budget','Not established within budget'))+'</dd><dt>Method budget ceiling</dt><dd>'+h(flag(method.within_method_ceiling,'Within ceiling','Outside ceiling'))+'</dd><dt>Factor budget ceilings</dt><dd>'+h(flag(method.within_factor_ceilings,'Within ceilings','Outside ceilings'))+'</dd><dt>Population confidence claim</dt><dd>'+h(flag(v.population_qualified,'Supported','Not established'))+'</dd>'+details.filter(([,value])=>typeof value==='number'&&Number.isFinite(value)).map(([title,value])=>'<dt>'+h(title)+'</dt><dd>'+h(number(value))+'</dd>').join('')+'</dl>'+(v.exact_binomial_bounds?'<details><summary>Exact binomial confidence bounds</summary><pre>'+h(JSON.stringify(v.exact_binomial_bounds,null,2))+'</pre></details>':'')+'</article>';
  }).join('');
  const paired=result?.paired_comparison;
  return '<section class="panel"><h3>Scientific assessment</h3>'+(status?'<p>'+label(status)+'</p>':'')+'<p class="muted">The observed 256-case campaign criterion and the population confidence claim are separate source assessments. Failed and unresolved cases remain in the denominator.</p>'+(cards?'<div class="accuracy-grid">'+cards+'</div>':'')+(paired?'<h4>Paired comparison</h4><p>Paired cases: '+h(number(paired.paired_case_count))+' / '+h(number(paired.requested_case_count))+'</p><p>Mean score difference (HNPE − TMCMC): '+h(number(paired.mean_score_difference_hnpe_minus_tmcmc))+'</p><p>'+h(paired.scope??'')+'</p>':'')+(qualification?'<details><summary>Source assessment values</summary><pre>'+h(JSON.stringify(qualification,null,2))+'</pre></details>':'')+(!cards&&!qualification?'<p>No verified method assessment is available yet.</p>':'')+'</section>';
}

export class AccuracyLab{
  constructor({context,onChange,download,fetcher=globalThis.fetch,now=()=>performance.now()}){
    this.context=context;this.onChange=onChange;this.download=download;this.fetcher=fetcher;this.now=now;this.lastPoll=null;this.revision=0;this.abort=null;this.resetData();
  }
  resetData(){this.capabilities=null;this.draft=null;this.campaigns=[];this.campaignCursor=null;this.detail=null;this.result=null;this.rawResultArtifact=null;this.artifacts=[];this.figures=[];this.error='';this.notice='';this.loading=false;this.archived=false;this.intent=null;this.lastPoll=null;this.scaleValues={higher_noise:1.25,broader_priors:1.5};}
  reset(){this.leave();this.resetData();}
  leave(){this.revision++;this.abort?.abort();this.abort=null;this.loading=false;this.figures=[];}
  current(revision,api,project){return revision===this.revision&&sameContext(this.context(),api,project);}
  notify(){this.onChange();}
  setDetail(value){this.detail=value;if(!this.result)this.artifacts=Array.isArray(value?.verified_artifacts)?clone(value.verified_artifacts):[];}
  async loadCapabilities(){
    const {api,project}=this.context();if(!api||!project)return null;
    const revision=this.revision;
    try{const data=await api.request(path(api,project,'/capabilities'));if(!this.current(revision,api,project))return null;this.capabilities=data;if(!this.draft&&data.defaults)this.draft=clone(data.defaults);this.notify();return data;}
    catch{if(this.current(revision,api,project)){this.capabilities={enabled:false,reason:'Accuracy-target campaigns are awaiting scientific release activation.'};this.notify();}return null;}
  }
  async enter(){if(!this.capabilities)await this.loadCapabilities();if(this.capabilities?.enabled)await this.refresh();}
  async operation(callback){
    if(this.loading)return;
    const {api,project}=this.context();if(!api||!project||!sameContext(this.context(),api,project))return;
    const revision=this.revision;this.loading=true;this.error='';this.notify();
    try{await callback(api,project,()=>this.current(revision,api,project));}
    catch(error){if(this.current(revision,api,project))this.error=error?.message||'The request could not be completed. Refresh before trying again.';}
    finally{if(this.current(revision,api,project)){this.loading=false;this.notify();}}
  }
  async refresh(){
    await this.operation(async(api,project,current)=>{
      const data=await api.request(path(api,project,'/campaigns?archived='+(this.archived?'true':'false')+'&limit=50'));
      if(!current())return;this.campaigns=data.campaigns??[];this.campaignCursor=data.next_cursor??null;
      if(this.detail){const data=await api.request(path(api,project,'/campaigns/'+encodeURIComponent(this.detail.id)));if(current())this.setDetail(data.campaign);}
    });
  }
  async loadMoreCampaigns(){
    if(!this.campaignCursor)return;const cursor=this.campaignCursor;
    await this.operation(async(api,project,current)=>{
      const data=await api.request(path(api,project,'/campaigns?archived='+(this.archived?'true':'false')+'&limit=50&cursor='+encodeURIComponent(cursor)));
      if(!current())return;const seen=new Set(this.campaigns.map(item=>item.id));
      this.campaigns.push(...(data.campaigns??[]).filter(item=>!seen.has(item.id)&&seen.add(item.id)));this.campaignCursor=data.next_cursor??null;
    });
  }
  async loadMoreTasks(){
    if(!this.detail?.next_task_cursor)return;const id=this.detail.id,cursor=this.detail.next_task_cursor;
    await this.operation(async(api,project,current)=>{
      const data=await api.request(path(api,project,'/campaigns/'+encodeURIComponent(id)+'/tasks?limit=50&cursor='+encodeURIComponent(cursor)));
      if(!current()||this.detail?.id!==id)return;const seen=new Set((this.detail.tasks??[]).map(item=>item.id));
      this.detail.tasks=[...(this.detail.tasks??[]),...(data.tasks??[]).filter(item=>!seen.has(item.id)&&seen.add(item.id))];this.detail.next_task_cursor=data.next_cursor??null;
    });
  }
  async poll(){const now=this.now();if(this.lastPoll!==null&&now-this.lastPoll<15000)return;if(!this.loading&&this.detail&&(!terminal.has(this.detail.status)||['requested','verifying'].includes(this.detail.archive?.status))){this.lastPoll=now;await this.refresh();}}
  async open(id){await this.operation(async(api,project,current)=>{const data=await api.request(path(api,project,'/campaigns/'+encodeURIComponent(id)));if(current()){this.result=null;this.rawResultArtifact=null;this.setDetail(data.campaign);this.figures=[];this.notice='';}});}
  backToList(){this.leave();this.detail=null;this.result=null;this.rawResultArtifact=null;this.artifacts=[];this.notice='';this.notify();void this.refresh();}
  toggleArchiveView(){this.leave();this.archived=!this.archived;this.campaigns=[];this.campaignCursor=null;this.notify();void this.refresh();}
  async submit(){
    if(this.loading||!this.capabilities?.enabled||this.intent)return;
    const message=accuracyDraftError(this.draft,this.capabilities.limits);if(message){this.error=message;this.notify();return;}
    this.intent={...clone(this.draft),idempotency_key:crypto.randomUUID()};
    await this.reconcileSubmission();
  }
  async reconcileSubmission(){
    if(!this.intent)return;
    const request=clone(this.intent);
    await this.operation(async(api,project,current)=>{
      try{const data=await api.request(path(api,project,'/campaigns'),request);if(current()){this.result=null;this.rawResultArtifact=null;this.setDetail(data.campaign);this.intent=null;this.notice='Campaign accepted. Scientific computation runs on Karolina.';}}
      catch(error){throw Error('The submission could not be confirmed. Reconcile the original submission before creating another campaign.');}
    });
  }
  async change(action){
    if(!this.detail||!['cancel','archive'].includes(action))return;
    const id=this.detail.id;
    await this.operation(async(api,project,current)=>{const data=await api.request(path(api,project,'/campaigns/'+encodeURIComponent(id)+'/'+action),{});if(current()){this.setDetail(data.campaign);this.notice=action==='archive'?'Archival requested. Wait for independent PROJECT verification.':'Cancellation requested. Allocated work must reach a known terminal state.';}});
  }
  async loadResults(){
    if(!this.detail)return;
    const id=this.detail.id;
    await this.operation(async(api,project,current)=>{
      const data=await api.request(path(api,project,'/campaigns/'+encodeURIComponent(id)+'/results'));
      if(!current())return;
      const raw=data.raw_result_artifact,limit=this.detail.max_artifact_bytes??this.capabilities?.max_artifact_bytes;
      if(!validRawResult(raw,limit)||!data.result||typeof data.result!=='object'||Array.isArray(data.result)||!Array.isArray(data.artifacts))throw Error('The full scientific result download metadata is unavailable or invalid.');
      const listed=data.artifacts.find(item=>item.id===raw.id);
      if(listed&&['id','name','media_type','sha256','size_bytes','kind'].some(key=>listed[key]!==raw[key]))throw Error('Conflicting full scientific result download metadata.');
      if(!data.accounting||typeof data.accounting!=='object'||Array.isArray(data.accounting))throw Error('The final authoritative accounting is unavailable or invalid.');
      this.detail={...this.detail,accounting:clone(data.accounting)};
      this.result=data.result;this.rawResultArtifact=clone(raw);this.artifacts=data.artifacts;this.figures=[];
      const candidates=benchmarkFigureArtifacts({artifacts:this.artifacts},{profile:'accuracy'});
      this.abort?.abort();this.abort=new AbortController();const signal=this.abort.signal;
      const bridge={project:(p,suffix)=>api.project(p,suffix.replace(/^\/benchmarks\//,'/benchmark-accuracy/campaigns/')),request:async url=>{const data=await api.request(url);return {...data,download_url:data.url};},validateStorageUrl:value=>api.validateStorageUrl(value)};
      this.figures=candidates.map(({artifact,title})=>({artifact,title,status:'loading'}));this.notify();
      await Promise.all(this.figures.map(async figure=>{try{const src=await loadBenchmarkFigure({api:bridge,project,campaign:id,artifact:figure.artifact,fetcher:this.fetcher,signal,profile:'accuracy'});if(current()){figure.src=src;figure.status='ready';}}catch(error){if(current()&&!signal.aborted){figure.status='error';figure.error='This figure could not be loaded or verified.';}}}));
    });
  }
  async downloadResult(){
    if(!this.rawResultArtifact)return;
    await this.downloadArtifact(this.rawResultArtifact.id);
  }
  async downloadArtifact(id){
    const artifact=this.rawResultArtifact?.id===id?this.rawResultArtifact:this.artifacts.find(item=>item.id===id);if(!artifact||!this.detail)return;
    await this.operation(async(api,project,current)=>{
      const limit=this.detail.max_artifact_bytes??this.capabilities?.max_artifact_bytes;if(!Number.isSafeInteger(limit)||limit<1||!Number.isSafeInteger(artifact.size_bytes)||artifact.size_bytes<0||artifact.size_bytes>limit||!/^[a-f0-9]{64}$/.test(artifact.sha256))throw Error('This download descriptor is invalid.');
      const data=await api.request(path(api,project,'/campaigns/'+encodeURIComponent(this.detail.id)+'/artifacts/'+encodeURIComponent(id)));
      if(!current())return;for(const key of ['id','name','media_type','sha256','size_bytes',...(artifact.kind===undefined?[]:['kind'])])if(data.artifact?.[key]!==artifact[key])throw Error('The download metadata changed. Refresh the results.');
      api.validateStorageUrl(data.url);
      this.abort?.abort();this.abort=new AbortController();const signal=this.abort.signal;const response=await this.fetcher.call(globalThis,data.url,{method:'GET',credentials:'omit',cache:'no-store',redirect:'error',signal});
      if(!response.ok||response.headers.get('content-type')?.split(';')[0].trim()!==artifact.media_type)throw Error('The download could not be retrieved or its media type changed.');
      const reader=response.body?.getReader();if(!reader)throw Error('Streaming download is unavailable.');
      const bytes=new Uint8Array(artifact.size_bytes);let offset=0;
      try{for(;;){const {done,value}=await reader.read();if(done)break;if(!(value instanceof Uint8Array)||offset+value.length>bytes.length)throw Error('The downloaded size does not match.');bytes.set(value,offset);offset+=value.length;}}
      finally{try{await reader.cancel();}catch{}}
      const digest=Array.from(new Uint8Array(await crypto.subtle.digest('SHA-256',bytes)),v=>v.toString(16).padStart(2,'0')).join('');
      if(offset!==bytes.length||digest!==artifact.sha256)throw Error('The download checksum or size does not match.');
      if(current())this.download(artifact.name,new Blob([bytes],{type:artifact.media_type}),artifact.media_type);
    });
  }
  form(){
    if(!this.draft)return '<p>Loading the approved accuracy profile…</p>';
    const d=this.draft,l=this.capabilities?.limits??{},disabled=this.loading||!this.capabilities?.enabled||Boolean(this.intent),disable=disabled?' disabled':'';
    const control=(key,title,options,bounds,help='')=>'<label class="accuracy-field"><span>'+h(title)+'</span>'+(options?'<select data-accuracy-field="'+h(key)+'"'+disable+'>'+options.map(value=>'<option value="'+h(value)+'"'+(get(d,key)===value?' selected':'')+'>'+h(value)+'</option>').join('')+'</select>':'<input type="number" step="1" data-accuracy-field="'+h(key)+'" value="'+h(get(d,key))+'" min="'+h(bounds?.min??0)+'"'+(bounds?.max!==undefined?' max="'+h(bounds.max)+'"':'')+disable+'>')+(help?'<small>'+h(help)+'</small>':'')+'</label>';
    const error=accuracyDraftError(d,l);
    return '<section class="panel accuracy-card"><h2>Design an accuracy-target comparison</h2><p>Train a fresh seven-factor HNPE model and run TMCMC against an independent numerical reference. Each method freezes at its first passing guide result, or reports its terminal limitation.</p><div class="accuracy-grid"><label class="accuracy-field"><span>Scenario family</span><select data-accuracy-scenario'+disable+'>'+[['baseline','Baseline'],['higher_noise','Higher measurement noise'],['broader_priors','Broader physical priors']].map(([value,title])=>'<option value="'+value+'"'+(d.scenario.family===value?' selected':'')+'>'+title+'</option>').join('')+'</select></label><label class="accuracy-field"><span>Scenario multiplier · '+h(d.scenario.scale)+'×</span><input type="range" min="1" max="3" step="0.05" data-accuracy-scale value="'+h(d.scenario.scale)+'"'+(disabled||d.scenario.family==='baseline'?' disabled':'')+'><small>Only the selected noise or physical-prior family changes. Baseline stays at 1×.</small></label>'+control('seed','Campaign seed',null,{min:0})+'</div><h3>Fresh HNPE training</h3><div class="accuracy-grid">'+control('controls.hnpe.width','Hidden width',l.hnpe?.width??[64,128,256])+control('controls.hnpe.depth','Hidden depth',l.hnpe?.depth??[1,2,3])+control('controls.hnpe.learning_rate','Learning rate',l.hnpe?.learning_rate??[.0001,.0003])+control('controls.hnpe.batch_size','Batch size',l.hnpe?.batch_size??[256,512,1024,2048])+control('controls.hnpe.max_updates','Maximum optimizer updates',null,l.hnpe?.max_updates)+control('controls.hnpe.training_budget','Maximum fit time per factor (s)',null,l.hnpe?.training_budget)+'</div><h3>TMCMC sampler</h3><div class="accuracy-grid">'+control('controls.tmcmc.population_size','Initial particle population',l.tmcmc?.population_size??[2048,4096,8192])+control('controls.tmcmc.target_cov','Target coefficient of variation',l.tmcmc?.target_cov??[.4,.6,.8])+control('controls.tmcmc.proposal_scale','Proposal scale',l.tmcmc?.proposal_scale??[.25,.5,1])+control('controls.tmcmc.burn_in','Phase 1 burn-in',l.tmcmc?.burn_in??[1,7,10])+control('controls.tmcmc.max_generations','Maximum generations',null,l.tmcmc?.max_generations)+control('budget.method_gpu_seconds','Total method budget (GPU s)',null,l.method_gpu_seconds,'Preparation, training and inference count toward HNPE cost. Reference and reporting costs are shown separately.')+'</div><h3>Locked evaluation</h3><p><strong>8 guide cases · 256 independent confirmation cases · 2,048 draws</strong></p><p>The central 90% cohort, accuracy criterion and confidence assessment are fixed by the approved scientific profile. Confirmation opens after both methods are sealed.</p>'+(error?'<p class="error">'+h(error)+'</p>':'')+(this.intent?'<p role="status">An original submission is awaiting reconciliation. Its settings and identifier remain fixed.</p><button type="button" data-accuracy-action="reconcile"'+(this.loading?' disabled':'')+'>Reconcile original submission</button>':'<button type="button" data-accuracy-action="submit"'+(disabled||error?' disabled':'')+'>Start accuracy-target campaign</button>')+'</section>';
  }
  detailView(){
    const d=this.detail,archive=d.archive?.status??'not_requested',busy=this.loading?' disabled':'';
    return '<section class="panel accuracy-card"><button type="button" data-accuracy-action="back">All accuracy campaigns</button><button type="button" data-accuracy-action="refresh"'+busy+'>Refresh campaign</button><h2>'+label(d.configuration?.scenario?.family)+' · accuracy target</h2><p><strong>'+label(d.status)+'</strong> · PROJECT archive: '+label(archive)+'</p>'+(d.budget_exceeded===true?'<p class="error" role="status">An experimental method budget was exceeded. Actual costs remain recorded; exhausted methods cannot receive more work.</p>':'')+(!terminal.has(d.status)?'<button type="button" data-accuracy-action="cancel"'+busy+'>Request cancellation</button>':archive==='not_requested'?'<button type="button" data-accuracy-action="archive"'+(this.loading||!d.archive?.available?' disabled':'')+'>Request archival</button>':'')+'<button type="button" data-accuracy-action="campaign-json">Download campaign JSON</button><h3>Method progress</h3><div class="accuracy-grid">'+['hnpe','tmcmc'].map(method=>'<article><h4>'+method.toUpperCase()+'</h4><p>'+label(d.methods?.[method]?.status)+'</p><p>Guide: '+label(d.methods?.[method]?.guide_status)+'</p>'+(Number.isSafeInteger(d.methods?.[method]?.effort_target)?'<p>Requested effort: '+h(number(d.methods[method].effort_target))+'</p>':'')+'</article>').join('')+'</div><h3>Allocated cost and reservations</h3>'+accuracyAccounting(d.accounting)+'<h3>Tasks</h3><table><thead><tr><th>Task</th><th>Factor</th><th>Attempt</th><th>Status</th><th>Last verified progress</th></tr></thead><tbody>'+(d.tasks??[]).map(task=>'<tr><td>'+label(task.kind)+'</td><td>'+h(task.factor??'—')+'</td><td>'+h(task.attempt)+'</td><td>'+label(task.status)+'</td><td>'+verifiedTaskProgress(task.progress)+'</td></tr>').join('')+'</tbody></table>'+(d.next_task_cursor?'<button type="button" data-accuracy-action="more-tasks"'+busy+'>Load more tasks</button>':'')+'<button type="button" data-accuracy-action="results"'+(this.loading||!d.result?' disabled':'')+'>Load scientific result</button>'+(this.result&&this.rawResultArtifact?'<button type="button" data-accuracy-action="result-json"'+busy+'>Download full result JSON</button>':'')+'<details><summary>Scientific profile and reproducibility</summary><pre>'+h(JSON.stringify({campaign:d.id,configuration:d.configuration,scientific_request_hash:d.scientific_request_hash,execution_context_id:d.execution_context_id},null,2))+'</pre></details></section>'+(this.result?sourceAssessment(this.result,d):'')+(this.figures.length?'<section class="panel"><h3>Scientific figures</h3><div class="accuracy-figures">'+this.figures.map(item=>'<figure><figcaption>'+h(item.title??item.artifact.name)+'</figcaption>'+(item.status==='ready'?'<img src="'+h(item.src)+'" alt="'+h(item.artifact.name)+'">':item.status==='error'?'<p role="alert">'+h(item.error)+'</p>':'<p role="status">Loading and verifying figure…</p>')+'</figure>').join('')+'</div></section>':'')+(this.artifacts.length?'<section class="panel"><h3>'+(this.result?'Numerical data & figures':'Available files')+'</h3>'+(!this.result&&!d.result?'<p>This run has no completed scientific result. Available files are preserved for inspection.</p>':'')+'<ul>'+this.artifacts.map(artifact=>'<li>'+h(artifact.name)+' <button type="button" data-accuracy-artifact="'+h(artifact.id)+'"'+busy+'>Download</button></li>').join('')+'</ul></section>':'');
  }
  view(){
    const enabled=this.capabilities?.enabled;
    return '<div class="accuracy-lab"><p class="eyebrow">BENCHMARK LAB · ACCURACY TARGET</p><h1>Accuracy-target comparison</h1><p>Compare the effort each method needs to meet a declared scientific criterion. An honest unmet target remains a valid campaign outcome.</p>'+(this.error?'<p class="error" role="alert">'+h(this.error)+'</p>':'')+(this.notice?'<p role="status">'+h(this.notice)+'</p>':'')+(!enabled?'<section class="panel"><p>'+h(this.capabilities?.reason??'Loading scientific capabilities…')+'</p></section>':this.detail?this.detailView():this.form()+'<section class="panel"><h2>'+(this.archived?'Archived accuracy campaigns':'Accuracy campaigns')+'</h2><button type="button" data-accuracy-action="refresh"'+(this.loading?' disabled':'')+'>Refresh campaigns</button><button type="button" data-accuracy-action="archive-view">'+(this.archived?'Show current':'Show archive')+'</button>'+(this.campaigns.length?'<ul>'+this.campaigns.map(item=>'<li>'+label(item.configuration?.scenario?.family)+' · '+h(formatTimestamp(item.created_at))+' · '+label(item.status)+' <button type="button" data-accuracy-open="'+h(item.id)+'"'+(this.loading?' disabled':'')+'>View campaign</button></li>').join('')+'</ul>':'<p>No campaigns in this view.</p>')+(this.campaignCursor?'<button type="button" data-accuracy-action="more-campaigns"'+(this.loading?' disabled':'')+'>Load more campaigns</button>':'')+'</section>')+'</div>';
  }
  captureFocus(){const node=globalThis.document?.activeElement;if(!node?.id?.startsWith('accuracy-'))return null;return {id:node.id,start:node.selectionStart,end:node.selectionEnd};}
  mount(container,focus){
    for(const node of container.querySelectorAll('[data-accuracy-field]')){node.id='accuracy-'+node.dataset.accuracyField.replaceAll('.','-');node.onchange=()=>{if(this.loading||this.intent)return;const key=node.dataset.accuracyField;if(!fieldPaths.has(key))return;put(this.draft,key,node.value===''?NaN:Number(node.value));this.notify();};}
    const scenario=container.querySelector('[data-accuracy-scenario]');if(scenario){scenario.id='accuracy-scenario';scenario.onchange=()=>{if(this.loading||this.intent)return;this.draft.scenario={family:scenario.value,scale:scenario.value==='baseline'?1:this.scaleValues[scenario.value]};this.notify();};}
    const scale=container.querySelector('[data-accuracy-scale]');if(scale){scale.id='accuracy-scale';scale.oninput=()=>{if(this.loading||this.intent||this.draft.scenario.family==='baseline')return;this.draft.scenario.scale=Number(scale.value);this.scaleValues[this.draft.scenario.family]=Number(scale.value);this.notify();};}
    for(const button of container.querySelectorAll('[data-accuracy-action]'))button.onclick=()=>{const action=button.dataset.accuracyAction;if(action==='submit')void this.submit();else if(action==='reconcile')void this.reconcileSubmission();else if(action==='refresh')void this.refresh();else if(action==='more-campaigns')void this.loadMoreCampaigns();else if(action==='more-tasks')void this.loadMoreTasks();else if(action==='cancel'||action==='archive')void this.change(action);else if(action==='results')void this.loadResults();else if(action==='campaign-json')this.download('accuracy-'+this.detail.id+'.json',this.detail);else if(action==='result-json')void this.downloadResult();else if(action==='back')this.backToList();else if(action==='archive-view')this.toggleArchiveView();};
    for(const button of container.querySelectorAll('[data-accuracy-open]'))button.onclick=()=>void this.open(button.dataset.accuracyOpen);
    for(const button of container.querySelectorAll('[data-accuracy-artifact]'))button.onclick=()=>void this.downloadArtifact(button.dataset.accuracyArtifact);
    if(focus?.id){const node=[...container.querySelectorAll('[id]')].find(item=>item.id===focus.id);node?.focus();if(typeof node?.setSelectionRange==='function'&&focus.start!==null)try{node.setSelectionRange(focus.start,focus.end);}catch{}}
  }
}
