import {escapeHtml as h} from './plots.js';
import {formatTimestamp} from './client.js';

// These helpers only format server-authored values and draw returned plot series.
const colours=['#17695a','#b5773d','#6673ad','#a34e65'];
const terminal=new Set(['completed','succeeded','failed','cancelled','archived']);
const fmt=value=>typeof value==='number'&&Number.isFinite(value)?new Intl.NumberFormat(undefined,{maximumSignificantDigits:5}).format(value):'Not reported';
const text=value=>value===null||value===undefined?'Not reported':typeof value==='object'?JSON.stringify(value):String(value);
const button=(action,label,extra='',disabled=false)=>`<button type="button" class="secondary" data-lab-action="${h(action)}" ${extra} ${disabled?'disabled':''}>${h(label)}</button>`;
export function benchmarkPlot(plot){
  if(!plot||!Array.isArray(plot.series))return '<p class="lab-note">Plot data not reported.</p>';
  const series=plot.series.filter(s=>Array.isArray(s.x)&&Array.isArray(s.y)&&s.x.length===s.y.length&&s.x.length>0&&s.x.length<=6000&&s.x.every(Number.isFinite)&&s.y.every(Number.isFinite));
  if(!series.length)return '<p class="lab-note">Plot data not reported.</p>';
  let xmin=Infinity,xmax=-Infinity,ymin=Infinity,ymax=-Infinity;
  for(const s of series)for(let i=0;i<s.x.length;i++){xmin=Math.min(xmin,s.x[i]);xmax=Math.max(xmax,s.x[i]);ymin=Math.min(ymin,s.y[i]);ymax=Math.max(ymax,s.y[i]);}
  const xspan=xmax-xmin||1,yspan=ymax-ymin||1;
  if(!Number.isFinite(xspan)||!Number.isFinite(yspan))return '<p class="lab-note">Plot scale cannot be displayed.</p>';
  const x=v=>60+500*((v-xmin)/xspan),y=v=>225-180*((v-ymin)/yspan);
  const lines=series.map((s,n)=>`<path class="lab-chart-line" stroke="${colours[n%colours.length]}" d="${s.x.map((v,i)=>`${i?'L':'M'}${x(v).toFixed(2)},${y(s.y[i]).toFixed(2)}`).join(' ')}"/>`).join('');
  return `<figure><figcaption>${h(plot.title||'Benchmark comparison')}</figcaption><svg class="lab-chart" viewBox="0 0 600 290" role="img" aria-label="${h(plot.title||'Benchmark comparison')}"><path d="M60,35V225H565" fill="none" stroke="#afc3b8"/><text x="60" y="244">${h(fmt(xmin))}</text><text x="550" y="244" text-anchor="end">${h(fmt(xmax))}</text><text x="50" y="45" text-anchor="end">${h(fmt(ymax))}</text><text x="50" y="225" text-anchor="end">${h(fmt(ymin))}</text><text x="310" y="275" text-anchor="middle">${h(plot.x_label||'')}</text><text x="60" y="22">${h(plot.y_label||'')}</text>${lines}</svg><div class="lab-legend">${series.map(s=>`<span>${h(s.name||'Series')}</span>`).join('')}</div></figure>`;
}
function metricTable(rows){
  if(!rows||typeof rows!=='object')return '<p class="lab-note">Metrics not reported.</p>';
  const entries=Array.isArray(rows)?rows.map(r=>[r.name||r.metric,r.value]):Object.entries(rows);
  return `<div class="lab-table-wrap"><table><thead><tr><th>Reported metric</th><th>Value</th></tr></thead><tbody>${entries.map(([key,value])=>`<tr><td>${h(key)}</td><td>${h(typeof value==='number'?fmt(value):text(value))}</td></tr>`).join('')}</tbody></table></div>`;
}
export function benchmarkResults(result){
  if(!result)return '<p class="lab-note">Results become available after the campaign publishes verified evidence.</p>';
  const costs=result.costs||{};
  const methods=Array.isArray(costs.methods)?costs.methods:[];
  const costRows=methods.map(m=>`<tr><td>${h(m.method)}</td><td>${h(fmt(m.preparation_seconds))}</td><td>${h(fmt(m.training_seconds))}</td><td>${h(fmt(m.inference_seconds))}</td><td><strong>${h(fmt(m.total_seconds))}</strong></td></tr>`).join('');
  const plots=Array.isArray(result.plots)?result.plots:[];
  const failures=Array.isArray(result.failures)?result.failures:[];
  return `<section class="card"><h2>Accuracy and total method cost</h2><p class="lab-note">HNPE preparation, training and inference belong to the method cost. Numerical-reference generation is reported separately. Compare methods on the same cases and declared budgets.</p>${costRows?`<div class="lab-table-wrap"><table><thead><tr><th>Method</th><th>Preparation (s)</th><th>Training (s)</th><th>Inference (s)</th><th>Total (s)</th></tr></thead><tbody>${costRows}</tbody></table></div>`:'<p>Total method timing not reported.</p>'}<p class="lab-note">Numerical-reference cost: ${h(fmt(costs.reference_seconds))} seconds. Allocation and elapsed times have different scopes; see the evidence manifests.</p><details><summary>Reported CPU, GPU and wall-clock accounting</summary><div class="lab-table-wrap"><table><thead><tr><th>Method</th><th>Wall (s)</th><th>CPU (s)</th><th>GPU (s)</th></tr></thead><tbody>${methods.map(m=>`<tr><td>${h(m.method)}</td><td>${h(fmt(m.resource_cost?.wall_seconds))}</td><td>${h(fmt(m.resource_cost?.cpu_seconds))}</td><td>${h(fmt(m.resource_cost?.gpu_seconds))}</td></tr>`).join('')}<tr><td>Numerical reference</td><td>${h(fmt(costs.reference_resource_cost?.wall_seconds))}</td><td>${h(fmt(costs.reference_resource_cost?.cpu_seconds))}</td><td>${h(fmt(costs.reference_resource_cost?.gpu_seconds))}</td></tr></tbody></table></div><p class="lab-note">Concurrent process clocks, allocated GPU seconds and wall-clock duration are distinct; unreported accounting is not zero.</p></details><h3>Posterior accuracy, tails and calibration</h3>${metricTable(result.metrics)}<div class="lab-plot-grid">${plots.map(benchmarkPlot).join('')}</div><h3>Training histories</h3>${Array.isArray(result.training_histories)&&result.training_histories.length?result.training_histories.map(benchmarkPlot).join(''):'<p class="lab-note">Training history not reported.</p>'}<h3>Failures and incomplete stages</h3>${failures.length?failures.map(f=>`<div class="lab-failure"><strong>${h(f.stage||'Campaign stage')}</strong><p>${h(f.message||f.code||'Failed or incomplete; inspect the evidence.')}</p></div>`).join(''):'<p class="lab-note">No failures reported in this result.</p>'}</section>`;
}

const controlFields=[
  ['controls.hnpe.width','Hidden width','Units per hidden layer.'],
  ['controls.hnpe.depth','Hidden depth','Number of hidden layers.'],
  ['controls.hnpe.learning_rate','Learning rate','Optimizer step size.'],
  ['controls.hnpe.batch_size','Batch size','Training examples per optimizer update.'],
  ['controls.hnpe.max_updates','Maximum optimizer updates','Fixed update limit for each fresh factor fit.'],
  ['controls.hnpe.training_budget','Maximum fit time per factor (s)','The fit stops at this declared method budget; unfinished fits remain explicit.'],
  ['controls.tmcmc.population_size','Particle population','Particles in each TMCMC population.'],
  ['controls.tmcmc.target_cov','Target coefficient of variation','Tempering control; reported with the method configuration.'],
  ['controls.tmcmc.proposal_scale','Proposal scale','Proposal-width multiplier.'],
  ['controls.tmcmc.burn_in','Burn-in exploration','Declared exploration length.'],
  ['controls.tmcmc.max_generations','Maximum generations','Declared TMCMC generation limit.'],
];
const get=(object,path)=>path.split('.').reduce((value,key)=>value?.[key],object);
function put(object,path,value){const keys=path.split('.');let target=object;for(const key of keys.slice(0,-1))target=target[key]??={};target[keys.at(-1)]=value;}
function campaignStatus(campaign){return String(campaign?.status||'unknown');}
function archiveStatus(campaign){return String(campaign?.archive?.status||campaign?.archive_status||'not_requested');}
function campaignName(campaign){return campaign?.name||campaign?.id||'Campaign';}
function numericField(path,label,note,draft,limits,disabled){
  const rawSpec=get(limits,path)||limits?.[path]||{};
  const spec={...rawSpec,min:rawSpec.minimum??rawSpec.min,max:rawSpec.maximum??rawSpec.max};
  const value=get(draft,path);
  if(Array.isArray(spec.choices))return `<label>${h(label)}<select data-lab-field="${h(path)}" ${disabled?'disabled':''}>${spec.choices.map(choice=>`<option value="${h(choice)}" ${choice===value?'selected':''}>${h(choice)}</option>`).join('')}</select><small>${h(note)}</small></label>`;
  return `<label>${h(label)}<input type="number" data-lab-field="${h(path)}" value="${h(value??'')}" ${spec.min!==undefined?`min="${h(spec.min)}"`:''} ${spec.max!==undefined?`max="${h(spec.max)}"`:''} step="${h(spec.step??(path.includes('learning_rate')||path.includes('target_cov')||path.includes('proposal_scale')?'any':1))}" required ${disabled?'disabled':''}><small>${h(note)}</small></label>`;
}

export class BenchmarkLab {
  constructor({context,onChange,download,fetcher=globalThis.fetch}){
    this.context=context;this.onChange=onChange;this.download=download;this.fetcher=fetcher;
    this.revision=0;this.active=false;this.api=null;this.project='';this.resetData();
  }
  resetData(){this.transferController?.abort();this.transferController=null;this.capabilities=null;this.draft=null;this.campaigns=[];this.detail=null;this.result=null;this.error='';this.notice='';this.busy=false;this.initialized=false;this.idempotencyKey=null;this.archiveView=false;}
  reset(){this.revision++;this.active=false;this.api=null;this.project='';this.resetData();}
  leave(){this.transferController?.abort();this.revision++;this.active=false;this.busy=false;}
  sync(){
    const {api,project}=this.context();
    if(api!==this.api||project!==this.project){this.revision++;this.api=api;this.project=project;this.resetData();}
  }
  async enter(){this.sync();this.active=true;if(!this.initialized)await this.refresh();}
  current(revision,api,project){
    const context=this.context();
    return this.active&&revision===this.revision&&api===context.api&&project===context.project&&context.valid(api);
  }
  async operation(callback){
    if(this.busy)return;
    this.sync();this.revision++;const revision=this.revision,api=this.api,project=this.project;
    if(!this.active||!api||!project||!this.context().valid(api))return;
    this.busy=true;this.error='';this.notice='';this.onChange();
    const current=()=>this.current(revision,api,project);
    try{await callback({api,project,current});}
    catch(error){if(current())this.error='BenchmarkLab could not complete the request. Refresh the campaign to check its state before retrying.';}
    finally{if(current()){this.busy=false;this.onChange();}}
  }
  async refresh(){
    await this.operation(async({api,project,current})=>{
      const [caps,list]=await Promise.all([
        api.request(api.project(project,'/benchmarks/capabilities')),
        api.request(api.project(project,'/benchmarks')),
      ]);
      if(!current())return;
      this.capabilities=caps.capabilities||caps;
      if(!this.draft&&this.capabilities.defaults)this.draft=structuredClone(this.capabilities.defaults);
      this.campaigns=list.campaigns||[];
      this.initialized=true;
    });
  }
  async poll(){
    if(this.busy||!this.active)return;
    const revision=this.revision,api=this.api,project=this.project,id=this.detail?.id;
    if(!this.current(revision,api,project))return;
    try{
      const data=await api.request(api.project(project,id?'/benchmarks/'+encodeURIComponent(id):'/benchmarks'));
      if(!this.current(revision,api,project)||id!==this.detail?.id)return;
      if(id){if(JSON.stringify(data.campaign)!==JSON.stringify(this.detail)){this.detail=data.campaign;this.onChange();}}
      else if(JSON.stringify(data.campaigns)!==JSON.stringify(this.campaigns)){this.campaigns=data.campaigns||[];this.onChange();}
    }catch{if(this.current(revision,api,project)){this.error='Campaign status refresh failed. Use Refresh campaigns to retry.';this.onChange();}}
  }
  async open(id){
    await this.operation(async({api,project,current})=>{
      const data=await api.request(api.project(project,'/benchmarks/'+encodeURIComponent(id)));
      if(!current())return;this.detail=data.campaign;this.result=null;
    });
  }
  async loadResults(){
    const id=this.detail?.id;if(!id)return;
    await this.operation(async({api,project,current})=>{
      const data=await api.request(api.project(project,'/benchmarks/'+encodeURIComponent(id)+'/results'));
      if(!current()||this.detail?.id!==id)return;this.result=data.result;
    });
  }
  async submit(){
    await this.operation(async({api,project,current})=>{
      if(!this.draft)throw Error('Capabilities unavailable');
      this.idempotencyKey??=crypto.randomUUID();
      const request={schema_version:1,...structuredClone(this.draft),idempotency_key:this.idempotencyKey};
      const data=await api.request(api.project(project,'/benchmarks'),request);
      if(!current())return;
      this.detail=data.campaign;this.result=null;this.idempotencyKey=null;
      this.notice='Campaign submitted. Each campaign trains a fresh HNPE bundle; Slurm controls resource admission.';
      this.campaigns=[data.campaign,...this.campaigns.filter(item=>item.id!==data.campaign.id)];
    });
  }
  async changeCampaign(action){
    if(action==='archive'&&this.capabilities?.actions?.archive!==true)return;
    const id=this.detail?.id;if(!id)return;
    await this.operation(async({api,project,current})=>{
      const data=await api.request(api.project(project,'/benchmarks/'+encodeURIComponent(id)+'/'+action),{});
      if(!current()||this.detail?.id!==id)return;
      this.detail=data.campaign;
      this.campaigns=this.campaigns.map(c=>c.id===id?data.campaign:c);
      this.notice=action==='archive'?'Archival requested. Evidence remains available while the PROJECT archive is verified.':'Cancellation requested. Slurm termination may still be pending.';
    });
  }
  artifactRefs(){
    const unique=new Map();
    for(const artifact of [...(this.detail?.artifacts||[]),...(this.result?.artifacts||[])])if(!unique.has(artifact.id))unique.set(artifact.id,artifact);
    return [...unique.values()];
  }
  async downloadArtifact(id){
    const artifacts=this.artifactRefs();
    const selected=artifacts.find(a=>a.id===id);if(!selected)return;
    await this.operation(async({api,project,current})=>{
      const data=await api.request(api.project(project,'/benchmarks/'+encodeURIComponent(this.detail.id)+'/artifacts/'+encodeURIComponent(id)));
      if(!current())return;
      if(data.artifact?.id!==selected.id||data.artifact?.name!==selected.name||data.artifact?.sha256!==selected.sha256||data.artifact?.size_bytes!==selected.size_bytes)throw Error('Artifact reference mismatch');
      const size=selected.size_bytes;
      if(!Number.isSafeInteger(size)||size<1||size>100*1024*1024||!/^[a-f0-9]{64}$/.test(selected.sha256))throw Error('Artifact exceeds browser limit');
      api.validateStorageUrl(data.download_url);
      this.transferController=new AbortController();
      const response=await this.fetcher.call(globalThis,data.download_url,{credentials:'omit',redirect:'error',cache:'no-store',signal:this.transferController.signal});
      if(!response.ok||!response.body?.getReader)throw Error('Download failed');
      const reader=response.body.getReader(),chunks=[];let length=0;
      try{for(;;){const {done,value}=await reader.read();if(done)break;length+=value.byteLength;if(length>size||!current())throw Error('Stale or oversized download');chunks.push(value);}}
      finally{try{await reader.cancel();}catch{}}
      if(length!==size)throw Error('Download length mismatch');
      const bytes=new Uint8Array(size);let offset=0;for(const chunk of chunks){bytes.set(chunk,offset);offset+=chunk.length;}
      const digest=Array.from(new Uint8Array(await crypto.subtle.digest('SHA-256',bytes)),b=>b.toString(16).padStart(2,'0')).join('');
      if(digest!==selected.sha256)throw Error('Download checksum mismatch');
      if(!current())return;
      this.download(selected.name,new Blob([bytes],{type:'application/octet-stream'}),'application/octet-stream');
    });
  }
  form(){
    const disabled=this.busy||!this.draft,limits=this.capabilities?.limits||{};
    if(!this.draft)return `<p class="lab-empty">Campaign controls are unavailable until the project returns BenchmarkLab capabilities.</p>${button('refresh','Load campaign controls','',this.busy)}`;
    const family=this.draft.scenario?.family||'baseline';
    const families=[['baseline','Baseline'],['higher_noise','Higher measurement noise'],['broader_priors','Broader population priors']];
    const rawScaleSpec=get(limits,'scenario.scale')||limits['scenario.scale']||{};
    const scaleSpec={...rawScaleSpec,min:rawScaleSpec.minimum??rawScaleSpec.min,max:rawScaleSpec.maximum??rawScaleSpec.max};
    const scale=this.draft.scenario?.scale??1;
    const options=families.map(([value,label])=>`<option value="${value}" ${family===value?'selected':''}>${label}</option>`).join('');
    return `<form data-lab-form><div class="lab-scenario"><label>Scenario family<select data-lab-field="scenario.family" ${disabled?'disabled':''}>${options}</select></label><p class="lab-note">Change one family at a time; all methods share the resulting cases and numerical reference.</p><div class="lab-scale-row"><label for="lab-scenario-scale">Scenario multiplier</label><output id="lab-scale-value" aria-live="polite" for="lab-scenario-scale">${h(fmt(scale))}×</output></div><input id="lab-scenario-scale" type="range" data-lab-field="scenario.scale" aria-describedby="lab-scenario-note" min="${h(scaleSpec.min??1)}" max="${h(scaleSpec.max??3)}" step="${h(scaleSpec.step??.25)}" value="${h(scale)}" ${disabled||family==='baseline'?'disabled':''}><p id="lab-scenario-note" class="lab-note">${family==='baseline'?'1× retains the sealed baseline.':family==='higher_noise'?'Scale the complete measurement-noise SD distribution; retain its declared shape.':'Scale joint prior widths around the declared centres; retain the registered joint prior semantics.'}</p></div><h3>Fresh HNPE training</h3><p class="lab-note">A new seven-factor HNPE bundle is trained for every campaign. Changing these controls creates a new comparison.</p><div class="lab-form-grid">${controlFields.filter(([p])=>p.includes('.hnpe.')).map(([p,label,note])=>numericField(p,label,note,this.draft,limits,disabled)).join('')}</div><h3>TMCMC budget</h3><div class="lab-form-grid">${controlFields.filter(([p])=>p.includes('.tmcmc.')).map(([p,label,note])=>numericField(p,label,note,this.draft,limits,disabled)).join('')}</div><h3>Matched evaluation and method budget</h3><div class="lab-form-grid">${numericField('evaluation.case_count','Shared test cases','HNPE, TMCMC and numerical reference use the same cases.',this.draft,limits,disabled)}${numericField('evaluation.posterior_draws','Posterior draws','Matched retained sample count per case.',this.draft,limits,disabled)}${numericField('seed','Campaign seed','Shared case generation is versioned and reproducible.',this.draft,limits,disabled)}${numericField('budget.method_gpu_seconds','Total method budget (GPU s)','Training plus inference for each method; numerical-reference cost is separate.',this.draft,limits,disabled)}</div><details><summary>Reference and immutable benchmark profile</summary><pre>${h(JSON.stringify({cohort:this.draft.evaluation?.cohort,reference:this.draft.reference,profile:this.capabilities?.profile||'sealed_21d'},null,2))}</pre></details><p class="lab-budget-note">First compare accuracy at declared method budgets. Accuracy-target stopping is a separate follow-on mode.</p><p class="lab-rule">Campaigns may run concurrently. Slurm account, partition and QoS rules admit compute work; BenchmarkLab adds no global GPU-hour quota.</p><button class="primary" type="submit" ${disabled?'disabled':''}>Start benchmark campaign</button></form>`;
  }
  detailView(){
    const c=this.detail,s=campaignStatus(c),stages=Array.isArray(c.stages)?c.stages:Object.entries(c.stages||{}).map(([id,value])=>({id,...value}));
    const artifacts=this.artifactRefs();
    return `<div class="lab-toolbar">${button('back','All campaigns')}${button('refresh-detail','Refresh campaign','',this.busy)}</div><section class="card"><div class="lab-detail-head"><div><p class="lab-kicker">Campaign</p><h2>${h(campaignName(c))}</h2><span class="lab-status lab-status-${h(s)}">${h(s.replaceAll('_',' '))}</span></div><div class="lab-toolbar">${!terminal.has(s)?button('cancel','Request cancellation','',this.busy):''}${this.capabilities?.actions?.archive===true&&terminal.has(s)&&archiveStatus(c)!=='verified'?button('archive',archiveStatus(c)==='failed'?'Retry archival':'Request archival','',this.busy||['pending','running','requested'].includes(archiveStatus(c))):''}</div></div><p class="lab-note">Family: ${h(c.scenario?.family||c.configuration?.scenario?.family||'Not reported')} · Execution: Karolina</p><p class="lab-note">PROJECT archive: <strong>${h(archiveStatus(c).replaceAll('_',' '))}</strong>. Archival is complete only after the connector verifies the copy.</p>${s==='failed'?'<p class="lab-failure" role="alert">The campaign did not complete. Inspect the reported stages and evidence for the failure or budget limit.</p>':''}<h3>Stages</h3>${stages.length?`<ol class="lab-stage-list">${stages.map(stage=>`<li><strong>${h(stage.name||stage.id)}</strong> · ${h(stage.status||'Not reported')}</li>`).join('')}</ol>`:'<p class="lab-note">Stage progress not yet reported.</p>'}<div class="lab-toolbar">${button('results','Load comparison results','',this.busy||!['completed','succeeded'].includes(s))}${button('configuration','Download campaign JSON')}</div><details><summary>Declared scenario and method budgets</summary><pre>${h(JSON.stringify(c.configuration||c.controls||c,null,2))}</pre></details></section>${this.result?benchmarkResults(this.result):''}<section class="card"><h2>Evidence downloads</h2><p class="lab-note">Numerical results, plots, histories and manifests remain attached to the campaign when archived.</p>${artifacts.length?`<ul class="lab-downloads">${artifacts.map(a=>`<li><span>${h(a.name)}</span>${button('artifact','Download',`data-lab-id="${h(a.id)}"`,this.busy)}</li>`).join('')}</ul>`:'<p class="lab-note">No evidence artifacts reported yet.</p>'}</section>`;
  }
  view(){
    this.sync();
    const list=this.campaigns.filter(c=>this.archiveView?archiveStatus(c)==='verified':archiveStatus(c)!=='verified');
    return `<div class="benchmark-lab" data-benchmark-lab><div class="lab-heading"><div><p class="lab-kicker">Research methods · Analytical hierarchy</p><h1>BenchmarkLab</h1><p class="lab-intro">Compare fresh HNPE training with TMCMC against the same numerical reference. Explore noise and prior assumptions while keeping every campaign reproducible.</p></div>${button('refresh','Refresh campaigns','',this.busy)}</div>${this.error?`<div class="alert error lab-error" role="alert">${h(this.error)}</div>`:''}${this.notice?`<p class="alert" role="status">${h(this.notice)}</p>`:''}${this.busy?'<p class="lab-spinner" role="status">Loading BenchmarkLab…</p>':''}${this.detail?this.detailView():`<div class="lab-columns"><section class="card"><h2>Design a comparison</h2>${this.form()}</section><section class="card"><div class="lab-section-title"><h2>${this.archiveView?'Archived campaigns':'Campaigns'}</h2>${this.capabilities?.actions?.archive===true?button('toggle-archive',this.archiveView?'Show current':'Show archive'):''}</div><p class="lab-note">Each row is an independent scenario, training run and matched comparison.</p>${list.length?list.map(c=>`<article class="lab-campaign"><div><strong>${h(campaignName(c))}</strong><p>${h(c.scenario?.family||c.configuration?.scenario?.family||'Scenario')} · ${h(formatTimestamp(c.created_at))}</p><span class="lab-status lab-status-${h(campaignStatus(c))}">${h(campaignStatus(c).replaceAll('_',' '))}</span></div>${button('open','View campaign',`data-lab-id="${h(c.id)}"`,this.busy)}</article>`).join(''):'<p class="lab-empty">No campaigns in this view.</p>'}<p class="lab-rule">Scientific execution stays on Karolina. AWS coordinates campaign state and stores approved evidence.</p></section></div>`}</div>`;
  }
  captureFocus(){
    const element=globalThis.document?.activeElement;
    if(!element?.closest?.('[data-benchmark-lab]')||!element.dataset?.labField)return null;
    return {path:element.dataset.labField,start:element.selectionStart,end:element.selectionEnd};
  }
  mount(container,focus){
    const root=container?.querySelector?.('[data-benchmark-lab]');
    if(!root?.addEventListener||!root.querySelector)return;
    if(focus&&/^[a-zA-Z_.]+$/.test(focus.path)){
      const field=root.querySelector('[data-lab-field="'+focus.path+'"]');
      field?.focus?.({preventScroll:true});
      if(typeof focus.start==='number')try{field?.setSelectionRange?.(focus.start,focus.end);}catch{}
    }
    root.addEventListener('input',event=>{
      const path=event.target.dataset?.labField;if(!path||!this.draft||this.busy)return;
      const value=path==='scenario.family'?event.target.value:Number(event.target.value);
      put(this.draft,path,value);this.idempotencyKey=null;
      if(path==='scenario.scale'){const output=root.querySelector('#lab-scale-value');if(output)output.textContent=fmt(value)+'×';}
    });
    root.addEventListener('change',event=>{
      if(event.target.dataset?.labField==='scenario.family'){
        put(this.draft,'scenario.family',event.target.value);put(this.draft,'scenario.scale',event.target.value==='baseline'?1:2);this.idempotencyKey=null;this.onChange();
      }
    });
    root.addEventListener('submit',event=>{if(!event.target.matches?.('[data-lab-form]'))return;event.preventDefault();void this.submit();});
    root.addEventListener('click',event=>{
      const target=event.target.closest?.('[data-lab-action]');if(!target||target.disabled)return;
      const action=target.dataset.labAction,id=target.dataset.labId;
      if(action==='refresh')void this.refresh();
      if(action==='open')void this.open(id);
      if(action==='refresh-detail')void this.open(this.detail.id);
      if(action==='results')void this.loadResults();
      if(action==='cancel'||action==='archive')void this.changeCampaign(action);
      if(action==='artifact')void this.downloadArtifact(id);
      if(action==='back'){this.revision++;this.detail=null;this.result=null;this.busy=false;this.onChange();void this.refresh();}
      if(action==='toggle-archive'){this.archiveView=!this.archiveView;this.onChange();}
      if(action==='configuration')this.download('benchmark-'+this.detail.id+'.json',this.detail);
    });
  }
}
