// SYNTHETIC DATA, test-only route. This module is excluded from dist/.
import {configurationFor} from '../src/defaults.js';
const preset=await (await fetch('/presets/indentation-v1.json')).json();
const CANONICAL_PROFILE=preset.scientific_profile;
const fixtureModel={model_version:'test-sonovue-canonical-v1',status:'released',description:'SYNTHETIC canonical registry fixture; no live validation claim',provenance:{test_fixture:true,pipeline_binding:{id:CANONICAL_PROFILE,verification_scope:'SYNTHETIC fixture; no artifact verification'}}};
const requestedView=new URL(location.href).searchParams.get('view');
const banner=document.createElement('p');banner.className='visual-test-banner';banner.textContent='TEST FIXTURE · Synthetic API/auth/scientific data · No live AWS or HPC execution';document.body.prepend(banner);
const css=document.createElement('link');css.rel='stylesheet';css.href='/test/browser-fixture.css';document.head.append(css);
const sampleForce=Array.from({length:32},(_,i)=>i*0.5);
const median=sampleForce.map(f=>0.35*f+0.025*f*f);
const result={schema_version:1,kind:'calibration',interval_probability:0.9,
  parameters:[{name:'ka',unit:'DPD stiffness',median:9500,lower:8500,upper:10500},{name:'kb',unit:'DPD energy',median:20000,lower:17000,upper:23000},{name:'d0',unit:'DPD length',median:0.1,lower:0.05,upper:0.15},{name:'sigma',unit:'relative noise',median:0.08,lower:0.05,upper:0.1}],
  curves:[{dataset_id:'Synthetic curve · 3.2 μm',force:sampleForce,observed_displacement:median.map((d,i)=>d+Math.sin(i)*0.18),median,lower:median.map(d=>d*0.85),upper:median.map(d=>d*1.15),force_unit:'nN',displacement_unit:'nm'}],
  resonance:{status:'available',by_dataset:[{dataset_id:'Synthetic curve A',diameter_um:3.2,status:'available',median_mhz:2.4,lower_mhz:2.1,upper_mhz:2.8,conditional_fixed_kb_dpd:7850.288865935088,kb_uncertainty_propagated:false,assumptions:['SYNTHETIC frequency interval, not a bank evaluation or experimental validation.','Canonical bank conditions frequency on fixed kb; inferred kb uncertainty is not propagated.']},{dataset_id:'Synthetic curve B',diameter_um:5.8,status:'unavailable',reason:'TEST ONLY unsupported posterior sample fixture'}]},
  diagnostics:{test_fixture:true,effective_sample_size:284,uncertainty_includes:['parameter uncertainty','synthetic relative measurement noise'],unquantified:['model discrepancy','surrogate approximation error']},provenance:{test_fixture:true,code_hash:'synthetic-code-hash',model_hash:'synthetic-model-hash',config_hash:'synthetic-config-hash',runtime:{site:'TEST ONLY'}}};
const baseJob={schema_version:1,project_id:'test-project',owner_sub:'test-researcher',kind:'calibration',diameter_um:3.2,model_version:fixtureModel.model_version,created_at:1792576800,updated_at:1792576800,revision:1,configuration:configurationFor(preset,3.2),input_artifacts:[]};
const jobs=[{...baseJob,id:'test-completed-calibration',status:'succeeded',site:'karolina',slurm_job_id:'TEST-SCHEDULER-ID',archive:{verified:false,test_fixture:true}},{...baseJob,id:'test-running-calibration',status:'running',owner_sub:'test-collaborator'},{...baseJob,id:'test-pending-calibration',status:'pending',diameter_um:5.8}];
const config={apiBaseUrl:location.origin+'/test-api',cognitoDomain:location.origin,clientId:'TEST-ONLY-public-client',redirectUri:location.origin+location.pathname,logoutUri:location.origin+'/',scopes:['openid','email'],uploadOrigins:[]};
const nativeFetch=window.fetch.bind(window);
window.fetch=async(input,options={})=>{
  const url=new URL(String(input),location.href);let data;
  if(url.pathname.endsWith('/runtime-config.json'))data=config;
  else if(url.pathname==='/auth/session')data={access_token:'TEST-ONLY-NONFUNCTIONAL-TOKEN',expires_at:Math.floor(Date.now()/1000)+300};
  else if(url.pathname==='/auth/logout')data={logout_url:location.origin+'/logout?client_id=TEST-ONLY-public-client&logout_uri='+encodeURIComponent(location.origin+'/auth/login')};
  else if(url.pathname.endsWith('/v1/me'))data={subject:'test-researcher',email:'researcher@example.test',roles:['maintainer']};
  else if(url.pathname.endsWith('/v1/projects'))data={projects:[{id:'test-project',name:'Synthetic indentation study',role:'maintainer'}]};
  else if(url.pathname.endsWith('/models'))data={models:[fixtureModel,{model_version:'test-indentation-dnn-candidate',status:'candidate',description:'SYNTHETIC candidate fixture; not evaluated'}]};
  else if(url.pathname.endsWith('/jobs'))data={jobs};
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
}
window.__mesouqVisualReady=true;
