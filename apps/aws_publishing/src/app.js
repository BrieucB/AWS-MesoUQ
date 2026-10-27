import {Api,validateRuntime,readSiteSession,endSiteSession,parseCurve,createJob,terminal,formatTimestamp} from './client.js';
import {escapeHtml as h,curvePlot,intervalPlot} from './plots.js';
import {configurationFor,approvedModel,outputArtifacts} from './defaults.js';
import {CurveEditor,editorMarkup,mountEditor} from './curve-editor.js';
const app=document.querySelector('#app');
const state={config:null,preset:null,api:null,me:null,projects:[],project:'',model:'',jobs:[],artifacts:[],preview:null,result:null,detail:null,loading:false,error:'',notice:'',tab:'workspace',diameter:'3.2',forceUnit:'uN',displacementUnit:'um',order:'force-displacement',submitKey:null,logoutFailed:false};
const drawing=new CurveEditor();
let expiryTimer,pollTimer,editorController,inputRevision=0;
const option=(value,label,selected)=>`<option value="${h(value)}" ${String(value)===String(selected)?'selected':''}>${h(label)}</option>`;
function status(value){return `<span class="status ${h(value)}">${h(String(value).replaceAll('_',' '))}</span>`;}
function download(name,value,type='application/json'){const url=URL.createObjectURL(new Blob([value instanceof Blob||typeof value==='string'?value:JSON.stringify(value,null,2)],{type}));const a=document.createElement('a');a.href=url;a.download=name;a.click();setTimeout(()=>URL.revokeObjectURL(url),1000);}
function shell(content){return `<div class="layout"><aside><a class="brand" href="./"><span class="mark">μ</span><span>MesoUQ<small>INDENTATION</small></span></a><nav aria-label="Workspace"><button data-action="tab" data-tab="workspace">New analysis</button><button data-action="tab" data-tab="jobs">Calculations</button></nav><div class="side-bottom"><p>Private project · Invited MFA accounts</p><div class="account">${h(state.me?.email||'')}</div>${state.me?'<button class="quiet" data-action="logout">Sign out</button>':''}</div></aside><div class="main"><header>MesoUQ / Indentation analysis</header><main>${state.error?`<div role="alert" class="alert error" data-app-message>${h(state.error)}</div>`:''}${state.notice?`<div role="status" class="alert" data-app-message>${h(state.notice)}</div>`:''}${content}</main><footer>Approved indentation model · Own-account HPC session</footer></div></div>`;}
function intro(title,subtitle){return `<div class="page-heading"><div><h1>${h(title)}</h1><p>${h(subtitle)}</p></div></div>`;}
function loginView(){return shell(`${intro('Private research workspace','Sign in with your invited account. The hosted identity flow requires MFA.')}<button class="primary" data-action="login">Sign in securely</button>${state.logoutFailed?'<button class="secondary" data-action="logout">Retry sign-out</button>':''}`);}
function projectSelect(){return `<label>Project<select id="project" ${state.loading?'disabled':''}>${state.projects.map(p=>option(p.id,p.name,state.project)).join('')}</select></label>`;}
function jobsPanel(){return `<section class="card"><div class="section-heading"><h2>Calculations</h2><button data-action="refresh">Refresh</button></div>${state.jobs.map(j=>`<article class="artifact-row"><div><strong>${h(j.id)}</strong><p>${status(j.status)} · ${h(j.diameter_um)} μm · ${h(formatTimestamp(j.created_at))}</p></div><button class="secondary" data-action="detail" data-id="${h(j.id)}">View calculation</button></article>`).join('')||'<p>No calculations yet.</p>'}</section>`;}
function workspace(){return `${intro('Indentation analysis','One measured or drawn curve, one nominal diameter, and the approved scientific preset.')}${projectSelect()}<section class="card"><h2>HPC session connector</h2><p>Connection status is not reported by API v1. Start the local terminal connector using your own account at the project-configured HPC site. Waiting calculations require your active connector.</p><p>Execution site: ${h(state.projects.find(p=>p.id===state.project)?.site||'operator configured')}. Karolina and Vega use the same contract; live Vega validation is deferred.</p><p>No HPC password or private key is entered here. Follow the operator runbook for <code>scripts/platforms/hpc/run_publishing_connector.py</code>.</p></section><section class="card"><h2>Configure your analysis</h2><p>Approved model: ${h(state.model||'Unavailable — contact the operator')}. Curve-only individual calibration; scientific settings are locked.</p><div class="form-grid"><label>Nominal diameter<select id="diameter" ${state.loading?'disabled':''}>${[3.2,3.4,5.8].map(d=>option(d,`${d} μm`,state.diameter)).join('')}</select></label></div><h3>CSV metadata</h3><p>Two unquoted numeric columns with an optional header. Declare the units and column order before selecting your file.</p><div class="form-grid"><label>Force unit<select id="forceUnit" ${state.loading?'disabled':''}>${['N','nN','uN','µN'].map(v=>option(v,v,state.forceUnit)).join('')}</select></label><label>Displacement unit<select id="displacementUnit" ${state.loading?'disabled':''}>${['m','nm','um','µm'].map(v=>option(v,v,state.displacementUnit)).join('')}</select></label><label>Column order<select id="order" ${state.loading?'disabled':''}>${option('force-displacement','Force, displacement',state.order)}${option('displacement-force','Displacement, force',state.order)}</select></label></div>${editorMarkup(drawing,{forceUnit:state.forceUnit,displacementUnit:state.displacementUnit,disabled:state.loading})}<label class="drop-zone"><strong>Select one CSV curve</strong><input id="file" type="file" accept=".csv,text/csv" ${state.loading?'disabled':''}></label><div id="curve-preview">${state.preview?`<div class="preview"><strong>${h(state.preview.file.name)}</strong>${state.preview.source==='drawn'?'<p>Drawn curve · converted to CSV for the same validation and upload.</p>':''}${curvePlot({force:state.preview.rows.map(r=>r.force),observed_displacement:state.preview.rows.map(r=>r.displacement),force_unit:state.preview.metadata.force_unit,displacement_unit:state.preview.metadata.displacement_unit})}<button class="secondary" data-action="upload">Upload &amp; verify curve</button></div>`:''}</div><div id="curve-artifacts">${state.artifacts.map(a=>`<div class="artifact-row">${h(a.name)} · ${h(a.metadata?.nominal_diameter_um)} μm<button data-action="remove" ${state.loading?'disabled':''}>Remove curve</button></div>`).join('')}</div><p>Uploads remain private to project members. New measurements produce a derived analysis, not a replay of the historical run.</p><button class="primary" data-action="submit" ${state.loading||!state.model||state.artifacts.length!==1?'disabled':''}>Submit analysis</button></section>${jobsPanel()}`;}
function frequencyCondition(r){return `<p class="alert" role="note">Conditional resonance: fixed kb${Number.isFinite(r.conditional_fixed_kb_dpd)?` = ${h(r.conditional_fixed_kb_dpd)} DPD`: ' from the approved polynomial bank'}. Only ka uncertainty is propagated; inferred kb uncertainty is not propagated.</p>`;}
function resultPanel(result){if(!result)return '';const probability=result.interval_probability??state.detail?.configuration?.interval_probability;const parameters=(result.parameters||[]).filter(p=>['ka','kb','d0'].includes(p.name));const resonance=result.resonance||{};const datasets=Array.isArray(resonance.by_dataset)?resonance.by_dataset:[resonance];return `<section class="card"><h2>Parameter estimates &amp; fitted curve</h2><p>${Number.isFinite(probability)?h(probability*100)+'% credible intervals':'Interval probability not reported'}. Units are reported by the result. Completion alone does not prove sampling stability; inspect diagnostics.</p>${intervalPlot(parameters)}<h3>Fitted curve uncertainty</h3><p>Observed points, fitted median, and reported uncertainty band.</p>${(result.curves||[]).map(c=>`<figure><figcaption>${h(c.dataset_id)}</figcaption>${curvePlot(c)}</figure>`).join('')||'<p>No fitted curve reported.</p>'}<h3>Conditional resonance in liquid</h3>${datasets.map(r=>`${frequencyCondition(r)}${r.status==='available'?intervalPlot([{name:'Resonance frequency',unit:'MHz',lower:r.lower_mhz,median:r.median_mhz,upper:r.upper_mhz}]):`<p>${h(r.reason||'Resonance unavailable.')}</p>`}`).join('')}<details open><summary>Diagnostics &amp; sampling checks</summary><pre>${h(JSON.stringify(result.diagnostics??{},null,2))}</pre></details><details><summary>Provenance</summary><pre>${h(JSON.stringify(result.provenance??{},null,2))}</pre></details><h3>Numerical data &amp; ready figures</h3><p>Download posterior samples, fitted curves, conditional resonance data and figures returned by the worker.</p>${outputArtifacts(result).map(a=>`<div class="artifact-row"><span>${h(a.name)}</span><button data-action="download-artifact" data-id="${h(a.id)}">Download</button></div>`).join('')||'<p>No output artifacts reported.</p>'}<button data-action="download-result">Download result JSON</button></section>`;}
function detailPanel(){const j=state.detail;return `${intro('Calculation detail',j.id)}<button data-action="back">All calculations</button><section class="card"><h2>${h(j.diameter_um)} μm · ${status(j.status)}</h2><p>Model: ${h(j.model_version)} · Site: ${h(j.site||'Awaiting connector')} · Scheduler: ${h(j.slurm_job_id||'Not submitted')}</p>${j.error?`<pre role="alert">${h(JSON.stringify(j.error,null,2))}</pre>`:''}${j.status==='submission_unknown'?'<p class="alert error">Submission is uncertain. The connector must reconcile its journal with Slurm. Contact the operator before resubmitting.</p>':''}<div class="button-row"><button data-action="refresh-detail">Refresh detail</button>${j.status==='succeeded'?'<button data-action="result">Load scientific result</button>':''}${!terminal.has(j.status)&&j.owner_sub===state.me.subject?'<button data-action="cancel">Request cancellation</button>':''}</div><details><summary>Submitted preset and input provenance</summary><pre>${h(JSON.stringify({configuration:j.configuration,input_artifacts:j.input_artifacts},null,2))}</pre></details></section>${resultPanel(state.result)}`;}
function clearInput(){
  inputRevision++;state.artifacts=[];state.preview=null;state.submitKey=null;state.error='';state.notice='';
  for(const id of ['curve-preview','curve-artifacts'])app.querySelector?.('#'+id)?.replaceChildren();
  const submit=app.querySelector?.('[data-action="submit"]');if(submit)submit.disabled=true;
  const file=app.querySelector?.('#file');if(file)file.value='';
  for(const message of app.querySelectorAll?.('[data-app-message]')||[])message.remove();
}
async function prepareCurve(file,source='csv'){
  clearInput();if(source!=='drawn')drawing.reset();
  const revision=inputRevision,project=state.project;
  const metadata={force_unit:state.forceUnit,displacement_unit:state.displacementUnit,column_order:state.order,nominal_diameter_um:Number(state.diameter)};
  if(!/\.csv$/i.test(file.name))throw Error('Choose one CSV curve.');
  if(file.size>1024*1024)throw Error('CSV uploads are limited to 1 MiB.');
  const rows=parseCurve(await file.text(),metadata.column_order);
  if(revision!==inputRevision||project!==state.project||!state.me)return;
  state.preview={file,rows,metadata,source};
}
function render(){
  editorController?.destroy();editorController=null;
  app.innerHTML=state.me?shell(!state.project?intro('Your projects','No project membership was returned. Ask the operator for access.'):state.detail?detailPanel():state.tab==='jobs'?projectSelect()+jobsPanel():workspace()):loginView();
  const root=app.querySelector?.('[data-curve-editor]');
  if(root)editorController=mountEditor(root,drawing,{
    blocked:()=>state.loading||!state.me||!state.project,order:()=>state.order,onChange:clearInput,
    onUse:csv=>busy(()=>prepareCurve(new File([csv],'drawn-curve.csv',{type:'text/csv'}),'drawn')),
    onDownload:csv=>download('drawn-curve.csv',csv,'text/csv'),
  });
}
function capture(){}
async function busy(fn){if(state.loading)return;state.loading=true;state.error='';state.notice='';render();try{await fn();}catch(error){state.error=error.message||'The request could not be completed.';}finally{state.loading=false;render();}}
async function loadProject(){const project=state.project;const [jobs,models]=await Promise.all([state.api.request(state.api.project(project,'/jobs')),state.api.request(state.api.project(project,'/models'))]);if(project!==state.project)return;state.jobs=jobs.jobs||[];state.model='';try{state.model=approvedModel(models.models||[],state.preset,state.config.modelVersion);}catch(error){state.error=error.message;}}
async function loadDetail(id){state.detail=await state.api.request(state.api.project(state.project,`/jobs/${encodeURIComponent(id)}`));state.result=null;}
function resetProject(){clearInput();drawing.reset();state.detail=null;state.result=null;state.jobs=[];state.model='';}
app.addEventListener('change',event=>{const {id,value,files}=event.target;if(id==='project'){busy(async()=>{state.project=value;resetProject();await loadProject();});return;}if(['diameter','forceUnit','displacementUnit','order'].includes(id)){if(state.loading)return;state[id]=value;clearInput();drawing.reset();render();return;}if(id==='file'&&files?.[0])busy(()=>prepareCurve(files[0]));});
app.addEventListener('click',event=>{const button=event.target.closest('[data-action]');if(!button||button.disabled)return;const action=button.dataset.action;
if(action==='login'){location.assign('/auth/login');return;}
if(action==='logout'){const config=state.config;clearSiteMemory();state.loading=true;render();endSiteSession(config).then(url=>location.assign(url)).catch(error=>{state.logoutFailed=true;state.error=`Site cookies could not be cleared. Retry sign-out. ${error.message}`;}).finally(()=>{state.loading=false;render();});return;}
if(action==='tab'){state.tab=button.dataset.tab;state.detail=null;state.result=null;render();return;}
if(action==='back'){state.detail=null;state.result=null;state.tab='jobs';render();return;}
if(action==='remove'){if(state.loading)return;state.artifacts=[];state.submitKey=null;render();return;}
if(action==='download-result'){download(`result-${state.detail.id}.json`,state.result);return;}
busy(async()=>{
if(action==='refresh')await loadProject();
if(action==='detail')await loadDetail(button.dataset.id);
if(action==='refresh-detail')await loadDetail(state.detail.id);
if(action==='upload'){if(!state.preview)throw Error('Select a CSV first.');const p=state.preview;state.artifacts=[await state.api.upload(state.project,p.file,'curve',p.metadata)];state.preview=null;state.submitKey=null;state.notice='Curve uploaded and verified.';}
if(action==='submit'){if(state.artifacts.length!==1||state.artifacts[0].kind!=='curve')throw Error('Exactly one verified curve is required.');if(state.artifacts[0].metadata?.nominal_diameter_um!==Number(state.diameter))throw Error('Curve metadata must match the selected diameter.');state.submitKey ||= crypto.randomUUID();const job=createJob({kind:'calibration',model:state.model,diameter:state.diameter,configuration:configurationFor(state.preset,state.diameter),artifacts:state.artifacts,key:state.submitKey});state.detail=await state.api.request(state.api.project(state.project,'/jobs'),job);state.submitKey=null;state.result=null;await loadProject();state.notice='Calculation accepted. Your session connector will claim the work.';}
if(action==='cancel'){state.detail=await state.api.request(state.api.project(state.project,`/jobs/${encodeURIComponent(state.detail.id)}/cancel`),{});state.notice='Cancellation requested; refresh for the scheduler outcome.';}
if(action==='result')state.result=await state.api.request(state.api.project(state.project,`/jobs/${encodeURIComponent(state.detail.id)}/result`));
if(action==='download-artifact'){const data=await state.api.request(state.api.project(state.project,`/artifacts/${encodeURIComponent(button.dataset.id)}`));const url=state.api.validateStorageUrl(data.download_url);const response=await fetch(url,{credentials:'omit',redirect:'error',cache:'no-store'});if(!response.ok)throw Error('Artifact download failed.');download(data.artifact?.name||'artifact',await response.blob(),'application/octet-stream');}
});});
function clearSiteMemory(){clearTimeout(expiryTimer);clearInterval(pollTimer);if(state.api){state.api.token='';state.api.authFailed=true;}state.api=null;state.me=null;state.projects=[];state.project='';resetProject();state.notice='';state.error='';state.logoutFailed=false;state.diameter='3.2';state.preset=null;sessionStorage.removeItem('mesouq.oauth');}
function expireSiteSession(){
  clearSiteMemory();state.notice='Your private site session expired. Sign in again to continue.';render();location.assign('/auth/login');
}
async function boot(){
  render();
  try{
    const params=new URLSearchParams(location.search);
    if(params.has('code')||params.has('state')||params.has('error'))history.replaceState(null,'',location.pathname);
    const session=await readSiteSession();
    if(!session){expireSiteSession();return;}
    const response=await fetch('runtime-config.json',{cache:'no-store',credentials:'same-origin',redirect:'error'});
    if(response.status===401||response.status===403){expireSiteSession();return;}
    if(!response.ok)throw Error('Runtime deployment configuration is unavailable.');
    state.config=validateRuntime(await response.json());
    const presetResponse=await fetch('presets/indentation-v1.json',{cache:'no-store',credentials:'same-origin',redirect:'error'});
    if(!presetResponse.ok)throw Error('Approved preset is unavailable.');
    state.preset=await presetResponse.json();configurationFor(state.preset,state.diameter);
    state.api=new Api(state.config,session.token,fetch,{expiresAt:session.expiresAt,onUnauthorized:expireSiteSession});
    expiryTimer=setTimeout(expireSiteSession,Math.max(0,session.expiresAt-Date.now()));
    state.me=await state.api.request('/v1/me');
    state.projects=(await state.api.request('/v1/projects')).projects||[];
    state.project=state.projects[0]?.id||'';
    if(state.project)await loadProject();
    pollTimer=setInterval(async()=>{
      if(state.loading||document.hidden||!state.project||!state.api)return;
      try{
        const project=state.project;
        const response=await state.api.request(state.api.project(project,'/jobs'));
        if(project!==state.project)return;
        state.jobs=response.jobs||[];
        if(state.tab==='jobs'&&!state.detail){capture();render();}
      }catch(error){state.error=`Status refresh failed: ${error.message}`;if(state.tab==='jobs')render();}
    },15000);
  }catch(error){clearSiteMemory();state.error=error.message;}
  render();
}
boot();
