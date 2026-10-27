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
  let staged,count=0,submitFails=true,releaseUpload;
  globalThis.fetch=async(input,options={})=>{
    const path=String(input);requests.push({path,options});let data;
    if(path==='/auth/session')data={access_token:'memory-token',expires_at:Math.floor(Date.now()/1000)+300};
    else if(path==='runtime-config.json')data=config;
    else if(path==='presets/indentation-v1.json')data=preset;
    else if(path.endsWith('/v1/me'))data={subject:'researcher',email:'synthetic@example.test'};
    else if(path.endsWith('/v1/projects'))data={projects:[{id:'p',name:'Synthetic project',site:'karolina'}]};
    else if(path.endsWith('/models'))data={models:[{model_version:'approved-v1',status:'released',provenance:{pipeline_binding:{id:preset.scientific_profile}}}]};
    else if(path==='https://artifacts.example.test/upload'){await new Promise(resolve=>releaseUpload=resolve);data={};}
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
    change('file',undefined,[new File(['1,NaN\n3,4'],'invalid.csv',{type:'text/csv'})]);await settle(/numeric CSV/);
    assert.doesNotMatch(element.innerHTML,/synthetic.csv|Upload &amp; verify curve/);
    assert.match(element.innerHTML,/data-action="submit" disabled/);
    assert.equal(requests.filter(r=>r.path.endsWith('/artifacts')).length,0);
    change('file',undefined,[new File(['d,f\n1,2\n3,4'],'synthetic.csv',{type:'text/csv'})]);await settle(/Upload &amp; verify curve/);
    click('upload');
    for(let i=0;i<100&&!releaseUpload;i++)await pause(2);
    assert.equal(typeof releaseUpload,'function');
    for(const id of ['diameter','forceUnit','displacementUnit','order'])assert.match(element.innerHTML,new RegExp(`<select id="${id}" disabled>`));
    // Stale/programmatic events must not change the captured upload metadata.
    change('diameter','3.2');change('forceUnit','N');change('displacementUnit','m');change('order','force-displacement');click('remove');
    releaseUpload();await settle(/Curve uploaded and verified/);
    for(const value of ['5.8','nN','nm','displacement-force'])assert.ok(element.innerHTML.includes(`<option value="${value}" selected>`));
    change('file',undefined,[new File(['1,2\n3,4'],'replacement.txt',{type:'text/plain'})]);await settle(/Choose one CSV curve/);
    assert.doesNotMatch(element.innerHTML,/synthetic.csv|Upload &amp; verify curve|Remove curve/);
    assert.match(element.innerHTML,/data-action="submit" disabled/);
    assert.equal(count,1); // Previously verified server object is retained, never deleted.
    change('file',undefined,[new File(['d,f\n1,2\n3,4'],'synthetic.csv',{type:'text/csv'})]);await settle(/Upload &amp; verify curve/);
    releaseUpload=undefined;click('upload');
    for(let i=0;i<100&&!releaseUpload;i++)await pause(2);
    assert.equal(typeof releaseUpload,'function');releaseUpload();await settle(/Curve uploaded and verified/);
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

test('both drawing modes use the existing CSV upload/submission flow and reset on metadata/project/logout',async(t)=>{
  const listeners={},requests=[],csvDownloads=[],redirects=[];let widget,staged,releasePut,jobCount=0;
  const config={apiBaseUrl:'https://api.example.test',cognitoDomain:'https://identity.example.test',clientId:'public',redirectUri:'https://portal.example.test/auth/callback',logoutUri:'https://portal.example.test/auth/login',uploadOrigins:['https://artifacts.example.test']};
  function editorDom(){
    const nodes=new Map();
    const find=key=>{
      if(!nodes.has(key))nodes.set(key,{value:key.includes('Max')?'1':'0',innerHTML:'',textContent:'',disabled:false,dataset:{},handlers:{},addEventListener(name,fn){this.handlers[name]=fn;},removeEventListener(name,fn){if(this.handlers[name]===fn)delete this.handlers[name];},setAttribute(){},matches(selector){return key.startsWith(selector.replace(']','='))||key===selector;}});
      return nodes.get(key);
    };
    const root=find('root');root.querySelector=find;root.querySelectorAll=()=>['apply-ranges','add-point','undo','clear','download','use'].map(action=>{const button=find(`[data-editor-action="${action}"]`);button.dataset.editorAction=action;return button;});
    const svg=find('[data-editor-canvas]');svg.getBoundingClientRect=()=>({left:0,top:0,width:640,height:360});svg.focus=()=>{};svg.setPointerCapture=id=>svg.pointer=id;svg.hasPointerCapture=id=>id===svg.pointer;svg.releasePointerCapture=()=>svg.pointer=null;
    return {root,find,svg,action(name,force=false){const button=find(`[data-editor-action="${name}"]`);if(force)button.disabled=false;root.handlers.click({target:{closest:()=>button},preventDefault(){}});},pointer(type,x,y){svg.handlers[type]?.({pointerId:1,button:0,isPrimary:true,clientX:x,clientY:y,preventDefault(){}});},mode(value){const input=find('[data-editor-mode]');input.value=value;root.handlers.change({target:input});}};
  }
  const element={innerHTML:'',addEventListener:(name,fn)=>listeners[name]=fn,querySelector(selector){
    if(selector==='[data-curve-editor]'){if(!this.innerHTML.includes('data-curve-editor'))return null;widget=editorDom();return widget.root;}
    return {replaceChildren(){},disabled:false,value:''};
  }};
  globalThis.document={querySelector:selector=>selector==='#app'?element:null,hidden:false,createElement:()=>({click(){}})};
  globalThis.location={origin:'https://portal.example.test',pathname:'/',search:'',assign:value=>redirects.push(String(value))};globalThis.history={replaceState(){}};globalThis.sessionStorage={removeItem(){}};
  const oldTimeout=globalThis.setTimeout,oldInterval=globalThis.setInterval;globalThis.setTimeout=()=>0;globalThis.setInterval=()=>0;
  t.mock.method(URL,'createObjectURL',blob=>{csvDownloads.push(blob);return 'blob:synthetic';});
  globalThis.fetch=async(input,options={})=>{
    const path=String(input);requests.push({path,options});let data;
    if(path==='/auth/session')data={access_token:'memory-only-synthetic',expires_at:Math.floor(Date.now()/1000)+300};
    else if(path==='runtime-config.json')data=config;
    else if(path==='presets/indentation-v1.json')data=preset;
    else if(path.endsWith('/v1/me'))data={subject:'researcher',email:'synthetic@example.test'};
    else if(path.endsWith('/v1/projects'))data={projects:[{id:'p',name:'Synthetic P'},{id:'q',name:'Synthetic Q'}]};
    else if(path.endsWith('/models'))data={models:[{model_version:'approved-v1',status:'released',provenance:{pipeline_binding:{id:preset.scientific_profile}}}]};
    else if(path.endsWith('/artifacts')&&options.method==='POST'){staged={...JSON.parse(options.body),id:'drawn-'+requests.length};data={artifact:staged,upload_url:'https://artifacts.example.test/upload'};}
    else if(path==='https://artifacts.example.test/upload'){await new Promise(resolve=>releasePut=resolve);data={};}
    else if(path.endsWith('/complete'))data={artifact:staged};
    else if(path.endsWith('/jobs')&&options.method==='POST')data={...JSON.parse(options.body),id:'job-'+(++jobCount),owner_sub:'researcher',status:'pending'};
    else if(path.endsWith('/jobs'))data={jobs:[]};
    else if(path==='/auth/logout')data={logout_url:config.cognitoDomain+'/logout?'+new URLSearchParams({client_id:config.clientId,logout_uri:location.origin+'/auth/login'})};
    else throw Error('Unrecognized synthetic drawing request');
    return {ok:true,status:200,json:async()=>data};
  };
  const settle=async predicate=>{for(let i=0;i<300&&!predicate();i++)await pause(2);assert.ok(predicate());};
  const click=(action,extra={})=>listeners.click({target:{closest:()=>({dataset:{action,...extra},disabled:false})}});
  const change=(id,value)=>listeners.change({target:{id,value}});
  try{
    await import('../src/app.js?drawing-workspace');await settle(()=>element.innerHTML.includes('Configure your analysis'));
    for(const [order,mode] of [['force-displacement','freehand'],['displacement-force','points']]){
      change('order',order);assert.equal(widget.find('[data-editor-count]').textContent,'0');widget.mode(mode);
      widget.pointer('pointerdown',202,93);
      if(mode==='freehand'){widget.pointer('pointermove',478,231);widget.pointer('pointerup',478,231);}
      else{widget.pointer('pointerup',202,93);widget.pointer('pointerdown',478,231);widget.pointer('pointerup',478,231);}
      assert.equal(widget.find('[data-editor-count]').textContent,'2');
      widget.action('download');const csv=await csvDownloads.at(-1).text();assert.equal(csv.split('\n')[0],order==='force-displacement'?'force,displacement':'displacement,force');
      assert.doesNotMatch(csv,/memory-only-synthetic/);
      widget.action('use');await settle(()=>element.innerHTML.includes('Drawn curve · converted to CSV'));
      assert.match(element.innerHTML,/drawn-curve.csv/);releasePut=undefined;click('upload');await settle(()=>typeof releasePut==='function');
      const before=widget.find('[data-editor-count]').textContent;
      for(const name of ['clear','undo','add-point','apply-ranges','download','use'])widget.action(name,true);
      widget.pointer('pointerdown',340,162);widget.mode(mode==='points'?'freehand':'points');
      assert.equal(widget.find('[data-editor-count]').textContent,before);
      releasePut();await settle(()=>element.innerHTML.includes('Curve uploaded and verified'));
      const put=requests.filter(r=>r.path==='https://artifacts.example.test/upload').at(-1);
      assert.equal(new TextDecoder().decode(put.options.body),csv);assert.equal(put.options.headers.Authorization,undefined);
      const original=JSON.parse(requests.filter(r=>r.path.endsWith('/artifacts')&&r.options.method==='POST').at(-1).options.body);
      assert.deepEqual(original.metadata,{force_unit:'uN',displacement_unit:'um',column_order:order,nominal_diameter_um:3.2});
      assert.equal(original.size_bytes,new TextEncoder().encode(csv).length);
      click('submit');await settle(()=>element.innerHTML.includes('Calculation accepted'));
      const submitted=JSON.parse(requests.filter(r=>r.path.endsWith('/jobs')&&r.options.method==='POST').at(-1).options.body);
      assert.equal(submitted.input_artifacts.length,1);assert.equal(submitted.kind,'calibration');assert.deepEqual(submitted.configuration,preset.configuration);
      click('tab',{tab:'workspace'});
    }
    change('forceUnit','N');assert.equal(widget.find('[data-editor-count]').textContent,'0');assert.doesNotMatch(element.innerHTML,/drawn-curve.csv/);
    widget.find('[data-editor-coordinate="displacement"]').value='.2';widget.find('[data-editor-coordinate="force"]').value='.3';widget.action('add-point');
    change('project','q');await settle(()=>element.innerHTML.includes('Configure your analysis')&&!/<select id="project" disabled>/.test(element.innerHTML));
    assert.equal(widget.find('[data-editor-count]').textContent,'0');
    widget.find('[data-editor-coordinate="displacement"]').value='.2';widget.find('[data-editor-coordinate="force"]').value='.3';widget.action('add-point');
    click('logout');await settle(()=>redirects.length>0);assert.doesNotMatch(element.innerHTML,/data-curve-editor|drawn-curve.csv/);
    assert.equal(jobCount,2);
  }finally{globalThis.setTimeout=oldTimeout;globalThis.setInterval=oldInterval;}
});
