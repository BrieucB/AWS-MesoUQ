import test from 'node:test';
import assert from 'node:assert/strict';
import {readFile} from 'node:fs/promises';
import {setTimeout as pause} from 'node:timers/promises';
const preset=JSON.parse(await readFile(new URL('../../../src/aws_mesouq/presets/indentation-v1.json',import.meta.url)));
test('synthetic minimal upload submits exact locked preset, supports diagnostics/cancellation and authorized output downloads',async()=>{
  const listeners={},requests=[],downloads=[];
  const element={innerHTML:'',addEventListener:(type,fn)=>listeners[type]=fn};
  const config={apiBaseUrl:'https://api.example.test',cognitoDomain:'https://identity.example.test',clientId:'public',redirectUri:'https://portal.example.test/auth/callback',logoutUri:'https://portal.example.test/auth/login',uploadOrigins:['https://artifacts.example.test']};
  let job={id:'job-1',project_id:'p',owner_sub:'researcher',kind:'calibration',diameter_um:3.2,model_version:'approved-v1',created_at:1767225600,status:'running',configuration:preset.configuration,input_artifacts:[]};
  const outputs=['posterior.csv','fitted-curve.csv','resonance.csv','fitted-curve.png','resonance.pdf'].map((name,i)=>({id:`out-${i}`,name,sha256:'synthetic-sha',kind:'output'}));
  const result={kind:'calibration',interval_probability:.9,parameters:[{name:'ka',unit:'DPD stiffness',lower:1,median:2,upper:3},{name:'kb',unit:'DPD energy',lower:4,median:5,upper:6},{name:'d0',unit:'DPD length',lower:0,median:.1,upper:.2},{name:'sigma',unit:'noise',lower:0,median:.1,upper:.2}],curves:[{force:[1,2],observed_displacement:[3,4],median:[3,4],lower:[2,3],upper:[4,5],force_unit:'nN',displacement_unit:'nm'}],resonance:{status:'available',lower_mhz:1,median_mhz:2,upper_mhz:3,conditional_fixed_kb_dpd:7850.288865935088,kb_uncertainty_propagated:false},diagnostics:{effective_sample_size:200},publication_files:outputs,private_path:'/never/fetch/this'};
  globalThis.document={querySelector:selector=>selector==='#app'?element:null,hidden:false,createElement:()=>({click(){downloads.push(this.download);}})};
  globalThis.location={origin:'https://portal.example.test',search:'',pathname:'/',assign:()=>{}};
  globalThis.history={replaceState:()=>{}};
  globalThis.sessionStorage={removeItem:()=>{},getItem:()=>{throw Error('No token storage');},setItem:()=>{throw Error('No token storage');}};
  const originalTimeout=globalThis.setTimeout,originalInterval=globalThis.setInterval;
  globalThis.setTimeout=()=>0;globalThis.setInterval=()=>0;
  let staged,count=0,submitFails=true;
  globalThis.fetch=async(input,options={})=>{
    const path=String(input);requests.push({path,options});let data;
    if(path==='/auth/session')data={access_token:'memory-token',expires_at:Math.floor(Date.now()/1000)+300};
    else if(path==='runtime-config.json')data=config;
    else if(path==='presets/indentation-v1.json')data=preset;
    else if(path.endsWith('/v1/me'))data={subject:'researcher',email:'synthetic@example.test'};
    else if(path.endsWith('/v1/projects'))data={projects:[{id:'p',name:'Synthetic project',site:'karolina'}]};
    else if(path.endsWith('/models'))data={models:[{model_version:'approved-v1',status:'released',provenance:{pipeline_binding:{id:preset.scientific_profile}}}]};
    else if(path==='https://artifacts.example.test/upload')data={};
    else if(path.endsWith('/artifacts')&&options.method==='POST'){staged={...JSON.parse(options.body),id:`curve-${++count}`};data={artifact:staged,upload_url:'https://artifacts.example.test/upload'};}
    else if(/\/artifacts\/curve-\d+\/complete$/.test(path))data={artifact:staged};
    else if(path.endsWith('/jobs')&&options.method==='POST'){
      if(submitFails){submitFails=false;return {ok:false,status:503,json:async()=>({error:{message:'Synthetic retryable admission failure'}})};}
      job={...job,...JSON.parse(options.body),status:'pending'};data=job;
    }
    else if(path.endsWith('/jobs'))data={jobs:[job]};
    else if(path.endsWith('/jobs/job-1/cancel')){job={...job,status:'cancel_requested'};data=job;}
    else if(path.endsWith('/jobs/job-1/result'))data=result;
    else if(path.endsWith('/jobs/job-1'))data=job;
    else if(path.endsWith('/artifacts/out-0'))data={artifact:outputs[0],download_url:'https://artifacts.example.test/download'};
    else if(path==='https://artifacts.example.test/download')return {ok:true,status:200,blob:async()=>new Blob(['synthetic numerical data'])};
    else throw Error(`Unexpected synthetic request ${path}`);
    return {ok:true,status:200,json:async()=>data};
  };
  const settle=async pattern=>{for(let i=0;i<300&&!pattern.test(element.innerHTML);i++)await pause(2);assert.match(element.innerHTML,pattern);};
  const click=(action,extra={})=>listeners.click({target:{closest:()=>({dataset:{action,...extra},disabled:false})}});
  const change=(id,value,files)=>listeners.change({target:{id,value,files}});
  try{
    await import('../src/app.js');await settle(/Configure your analysis/);
    assert.match(element.innerHTML,/approved-v1/);assert.match(element.innerHTML,/karolina/);
    assert.doesNotMatch(element.innerHTML,/id="(?:kind|mode|model|configuration|calibrationData)"|Retrain|Model registry|density|correlation/);
    change('order','displacement-force');change('forceUnit','nN');change('displacementUnit','nm');change('diameter','5.8');
    change('file',undefined,[new File(['d,f\n1,2\n3,4'],'synthetic.csv',{type:'text/csv'})]);await settle(/Upload &amp; verify curve/);
    click('upload');await settle(/Curve uploaded and verified/);
    click('submit');await settle(/Synthetic retryable admission failure/);
    click('submit');await settle(/Calculation accepted/);
    const submitted=requests.filter(r=>r.path.endsWith('/jobs')&&r.options.method==='POST').map(r=>JSON.parse(r.options.body));
    assert.equal(submitted.length,2);assert.equal(submitted[0].idempotency_key,submitted[1].idempotency_key);
    assert.equal(submitted[1].kind,'calibration');assert.equal(submitted[1].model_version,'approved-v1');
    assert.deepEqual(submitted[1].configuration,{...preset.configuration,emb_diameters:[5.8]});
    assert.equal(submitted[1].input_artifacts.length,1);
    assert.deepEqual(submitted[1].input_artifacts[0].metadata,{column_order:'displacement-force',force_unit:'nN',displacement_unit:'nm',nominal_diameter_um:5.8});
    assert.match(element.innerHTML,/Request cancellation/);click('cancel');await settle(/Cancellation requested/);assert.match(element.innerHTML,/cancel requested/);
    job={...job,status:'succeeded'};click('refresh-detail');await settle(/Load scientific result/);assert.doesNotMatch(element.innerHTML,/Request cancellation/);
    click('result');await settle(/Numerical data &amp; ready figures/);
    assert.match(element.innerHTML,/90% credible intervals/);assert.match(element.innerHTML,/DPD stiffness/);assert.match(element.innerHTML,/Force \(nN\)/);assert.match(element.innerHTML,/Displacement \(nm\)/);
    assert.match(element.innerHTML,/fixed kb = 7850.288865935088 DPD/);assert.match(element.innerHTML,/Only ka uncertainty is propagated/);assert.match(element.innerHTML,/effective_sample_size/);
    for(const output of outputs)assert.ok(element.innerHTML.includes(output.name));
    click('download-artifact',{id:'out-0'});for(let i=0;i<100&&!downloads.length;i++)await pause(2);
    assert.deepEqual(downloads,['posterior.csv']);const storageRequest=requests.find(r=>r.path==='https://artifacts.example.test/download');assert.equal(storageRequest.options.credentials,'omit');assert.equal(storageRequest.options.redirect,'error');assert.equal(storageRequest.options.headers,undefined);assert.ok(!requests.some(r=>r.path.includes('/never/fetch/')));
    click('tab',{tab:'workspace'});change('diameter','3.4');assert.doesNotMatch(element.innerHTML,/synthetic.csv/);click('submit');await settle(/Exactly one verified curve/);
  }finally{globalThis.setTimeout=originalTimeout;globalThis.setInterval=originalInterval;}
});
