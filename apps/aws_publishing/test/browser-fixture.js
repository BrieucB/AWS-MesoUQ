// SYNTHETIC DATA, test-only route. This module is excluded from dist/.
import {configurationFor} from '../src/defaults.js';
const preset=await (await fetch('/presets/indentation-v1.json')).json();
const CANONICAL_PROFILE=preset.scientific_profile;
const fixtureModel={model_version:'test-sonovue-canonical-v1',status:'released',description:'SYNTHETIC canonical registry fixture; no live validation claim',provenance:{test_fixture:true,pipeline_binding:{id:CANONICAL_PROFILE,verification_scope:'SYNTHETIC fixture; no artifact verification'}}};
const requestedView=new URL(location.href).searchParams.get('view');
const rememberedFixture=new URL(location.href).searchParams.get('remembered')==='1';
const coldFixture=new URL(location.href).searchParams.get('cold')==='1';
const fixtureBirth=Date.now();let fixtureNow=null,fixtureGeneration=0;
let fixtureIdle=fixtureBirth+7*86400000;
const fixtureAbsolute=fixtureBirth+30*86400000;
if(rememberedFixture){
  const originalNow=Date.now;Date.now=()=>fixtureNow??originalNow();
  window.__mesouqSessionFixture={calls:[],limits(){return {idle:fixtureIdle,absolute:fixtureAbsolute,generation:fixtureGeneration};},advance(ms){fixtureNow=Date.now()+ms;},
    resume(){window.dispatchEvent(new Event('pageshow'));}};
}
const fixtureSession=()=>({access_token:'TEST-ONLY-NONFUNCTIONAL-TOKEN-'+fixtureGeneration,
  expires_at:Math.min(Date.now()+300000,fixtureIdle,fixtureAbsolute)/1000,
  ...(rememberedFixture?{renewable:true,absolute_expires_at:fixtureAbsolute/1000,
    idle_expires_at:fixtureIdle/1000,generation:fixtureGeneration}:{})});
const banner=document.createElement('p');banner.className='visual-test-banner';banner.textContent='TEST FIXTURE · Synthetic API/auth/scientific data · No live AWS or HPC execution';document.body.prepend(banner);
const css=document.createElement('link');css.rel='stylesheet';css.href='/test/browser-fixture.css';document.head.append(css);
const sampleForce=Array.from({length:32},(_,i)=>i*0.5);
const median=sampleForce.map(f=>0.35*f+0.025*f*f);
const result={schema_version:1,kind:'calibration',interval_probability:0.9,
  parameters:[{name:'ka',unit:'DPD stiffness',median:9500,lower:8500,upper:10500},{name:'kb',unit:'DPD energy',median:20000,lower:17000,upper:23000},{name:'d0',unit:'DPD length',median:0.1,lower:0.05,upper:0.15},{name:'sigma',unit:'relative noise',median:0.08,lower:0.05,upper:0.1}],
  curves:[{dataset_id:'Synthetic curve · 3.2 μm',force:sampleForce,observed_displacement:median.map((d,i)=>d+Math.sin(i)*0.18),median,lower:median.map(d=>d*0.85),upper:median.map(d=>d*1.15),force_unit:'nN',displacement_unit:'nm'}],
  resonance:{status:'available',by_dataset:[{dataset_id:'Synthetic curve A',diameter_um:3.2,status:'available',median_mhz:2.4,lower_mhz:2.1,upper_mhz:2.8,conditional_fixed_kb_dpd:7850.288865935088,kb_uncertainty_propagated:false,assumptions:['SYNTHETIC frequency interval, not a bank evaluation or experimental validation.','Canonical bank conditions frequency on fixed kb; inferred kb uncertainty is not propagated.']},{dataset_id:'Synthetic curve B',diameter_um:5.8,status:'unavailable',reason:'TEST ONLY unsupported posterior sample fixture'}]},
  diagnostics:{test_fixture:true,effective_sample_size:284,uncertainty_includes:['parameter uncertainty','synthetic relative measurement noise'],unquantified:['model discrepancy','surrogate approximation error']},provenance:{test_fixture:true,code_hash:'synthetic-code-hash',model_hash:'synthetic-model-hash',config_hash:'synthetic-config-hash',runtime:{site:'TEST ONLY'}}};
// Deterministic, synthetic marginal-distribution figure for browser rendering only.
const posteriorCanvas=document.createElement('canvas');posteriorCanvas.width=1200;posteriorCanvas.height=360;
const ctx=posteriorCanvas.getContext('2d');ctx.fillStyle='#fff';ctx.fillRect(0,0,1200,360);
for(const [panel,label] of ['kA (synthetic units)','kB (synthetic units)','d0 (synthetic units)'].entries()){
  const x=panel*400;ctx.fillStyle='#173b38';ctx.font='bold 20px sans-serif';ctx.fillText(label,x+65,35);
  ctx.fillStyle='#e9eee9';ctx.fillRect(x+120,62,140,230);
  ctx.strokeStyle='#7c8c8a';ctx.beginPath();ctx.moveTo(x+45,60);ctx.lineTo(x+45,295);ctx.lineTo(x+370,295);ctx.stroke();
  ctx.fillStyle='#2563eb';
  for(let bin=0;bin<32;bin++){
    const z=(bin-(13+panel*2))/(4+panel);const height=200*Math.exp(-.5*z*z)*(1+.08*Math.sin(bin*2));
    ctx.fillRect(x+50+bin*9.7,294-height,8,height);
  }
  ctx.strokeStyle='#173b38';ctx.beginPath();ctx.moveTo(x+190,65);ctx.lineTo(x+190,294);ctx.stroke();
  ctx.fillStyle='#597752';ctx.font='16px sans-serif';ctx.fillText('Synthetic parameter value',x+88,330);
}
const posteriorBlob=await new Promise(resolve=>posteriorCanvas.toBlob(resolve,'image/png'));
const posteriorBytes=new Uint8Array(await posteriorBlob.arrayBuffer());
const posteriorHash=Array.from(new Uint8Array(await crypto.subtle.digest('SHA-256',posteriorBytes)),value=>value.toString(16).padStart(2,'0')).join('');
const posteriorArtifact={id:'test-posterior-figure',name:'figures__posterior.png',kind:'result',media_type:'image/png',sha256:posteriorHash,size_bytes:posteriorBytes.length};
result.artifacts=[posteriorArtifact];
const baseJob={schema_version:1,project_id:'test-project',owner_sub:'test-researcher',kind:'calibration',diameter_um:3.2,model_version:fixtureModel.model_version,created_at:1792576800,updated_at:1792576800,revision:1,configuration:configurationFor(preset,3.2),input_artifacts:[]};
const jobs=[{...baseJob,id:'test-completed-calibration',status:'succeeded',site:'karolina',slurm_job_id:'TEST-SCHEDULER-ID',archive:{verified:false,test_fixture:true}},{...baseJob,id:'test-running-calibration',status:'running',owner_sub:'test-collaborator'},{...baseJob,id:'test-pending-calibration',status:'pending',diameter_um:5.8}];
const config={apiBaseUrl:location.origin+'/test-api',cognitoDomain:location.origin,clientId:'TEST-ONLY-public-client',redirectUri:location.origin+location.pathname,logoutUri:location.origin+'/',scopes:['openid','email'],uploadOrigins:['https://posterior-figure.example.test']};
const nativeFetch=window.fetch.bind(window);
window.fetch=async(input,options={})=>{
  const url=new URL(String(input),location.href);let data;
  if(rememberedFixture)window.__mesouqSessionFixture.calls.push({path:url.pathname,method:options.method||'GET',...(url.pathname==='/auth/refresh'?{activity:JSON.parse(options.body).activity}:{})});
  if(url.pathname.endsWith('/runtime-config.json'))data=config;
  else if(url.pathname.endsWith('/presets/indentation-v1.json'))data=preset;
  else if(url.pathname==='/auth/session'){
    if(rememberedFixture&&coldFixture&&fixtureGeneration===0)return new Response(JSON.stringify({error:{code:'refresh_required'}}),{status:401,headers:{'Content-Type':'application/json'}});
    data=fixtureSession();
  }else if(url.pathname==='/auth/refresh'){
    const body=JSON.parse(options.body);
    if(Object.keys(body).join(',')!=='activity'||typeof body.activity!=='boolean')throw Error('Synthetic refresh contract mismatch');
    if(body.activity)fixtureIdle=Math.min(fixtureAbsolute,Date.now()+7*86400000);
    fixtureGeneration++;data=fixtureSession();
  }
  else if(url.pathname==='/auth/logout')data={logout_url:location.origin+'/logout?client_id=TEST-ONLY-public-client&logout_uri='+encodeURIComponent(location.origin+'/auth/login')};
  else if(url.pathname.endsWith('/v1/me'))data={subject:'test-researcher',email:'researcher@example.test',roles:['maintainer']};
  else if(url.pathname.endsWith('/v1/projects'))data={projects:[{id:'test-project',name:'Synthetic indentation study',role:'maintainer'}]};
  else if(url.pathname.endsWith('/models'))data={models:[fixtureModel,{model_version:'test-indentation-dnn-candidate',status:'candidate',description:'SYNTHETIC candidate fixture; not evaluated'}]};
  else if(url.pathname.endsWith('/jobs'))data={jobs};
  else if(url.pathname.endsWith('/artifacts/test-posterior-figure'))data={artifact:posteriorArtifact,download_url:'https://posterior-figure.example.test/posterior.png'};
  else if(url.href==='https://posterior-figure.example.test/posterior.png')return new Response(posteriorBytes,{status:200,headers:{'Content-Type':'image/png','Content-Length':String(posteriorBytes.length)}});
  else if(url.pathname.endsWith('/result'))data=result;
  else if(url.pathname.includes('/jobs/'))data=jobs.find(job=>url.pathname.endsWith('/'+job.id));
  else if(url.origin===location.origin&&!url.pathname.startsWith('/test-api'))return nativeFetch(input,options);
  else throw Error('Test fixture refused an unrecognized or external request.');
  return new Response(JSON.stringify(data),{status:data?200:404,headers:{'Content-Type':'application/json'}});
};
await import('../src/app.js');
function until(predicate){return new Promise((resolve,reject)=>{if(predicate())return resolve();const observer=new MutationObserver(()=>{if(predicate()){observer.disconnect();resolve();}});observer.observe(document.querySelector('#app'),{subtree:true,childList:true});setTimeout(()=>{observer.disconnect();reject(Error('Visual fixture timed out'));},10000);});}
await until(()=>document.querySelector('#diameter'));
if(requestedView==='result'){
  document.querySelector('[data-action="detail"][data-id="test-completed-calibration"]').click();
  await until(()=>document.querySelector('[data-action="result"]'));
  document.querySelector('[data-action="result"]').click();
  await until(()=>document.querySelector('[data-action="download-result"]'));
  await until(()=>document.querySelector('[data-posterior-image]'));
  let imageReady=false;
  for(let attempt=0;attempt<100;attempt++){
    const image=document.querySelector('[data-posterior-image]');
    if(image?.complete&&image.naturalWidth>0){imageReady=true;break;}
    await new Promise(resolve=>setTimeout(resolve,30));
  }
  if(!imageReady)throw Error('Synthetic posterior image did not decode.');
}
window.__mesouqVisualReady=true;
