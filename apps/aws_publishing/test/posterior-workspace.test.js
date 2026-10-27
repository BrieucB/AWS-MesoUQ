import test from 'node:test';
import assert from 'node:assert/strict';
import {readFile} from 'node:fs/promises';
import {createHash} from 'node:crypto';
import {setTimeout as pause} from 'node:timers/promises';
const preset=JSON.parse(await readFile(new URL('../../../src/aws_mesouq/presets/indentation-v1.json',import.meta.url)));
const bytes=new Uint8Array(Buffer.from('iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+a0V8AAAAASUVORK5CYII=','base64'));
const artifact={id:'posterior-test',name:'figures__posterior.png',kind:'result',media_type:'image/png',sha256:createHash('sha256').update(bytes).digest('hex'),size_bytes:bytes.length};
const config={apiBaseUrl:'https://api.example.test',cognitoDomain:'https://identity.example.test',clientId:'synthetic-public',redirectUri:'https://portal.example.test/auth/callback',logoutUri:'https://portal.example.test/auth/login',uploadOrigins:['https://artifacts.example.test']};
const result={kind:'calibration',interval_probability:.9,parameters:[{name:'ka',unit:'DPD',lower:1,median:2,upper:3}],curves:[],resonance:{status:'unavailable',reason:'Synthetic fixture'},diagnostics:{test_fixture:true},artifacts:[artifact]};
const imagePattern=/<img[^>]*data-posterior-image[^>]*src="data:image\/png;base64,/;
async function harness(key){
  const listeners={},requests=[],redirects=[],image={handlers:{},addEventListener(name,fn){this.handlers[name]=fn;}};
  const element={innerHTML:'',addEventListener:(name,fn)=>listeners[name]=fn,querySelector(selector){return selector==='[data-posterior-image]'&&this.innerHTML.includes('data-posterior-image')?image:null;}};
  let mode='good',missing=false,holdResult=false,releaseImage,releaseResult,unauthorized=false;
  const oldTimeout=globalThis.setTimeout,oldInterval=globalThis.setInterval;
  globalThis.setTimeout=()=>0;globalThis.setInterval=()=>0;
  globalThis.document={querySelector:selector=>selector==='#app'?element:null,hidden:false,createElement:()=>({click(){}})};
  globalThis.location={origin:'https://portal.example.test',search:'',pathname:'/',assign:value=>redirects.push(String(value))};
  globalThis.history={replaceState(){}};
  globalThis.sessionStorage={removeItem(){},getItem(){throw Error('No browser token storage');},setItem(){throw Error('No browser token storage');}};
  globalThis.fetch=async(input,options={})=>{
    const path=String(input);requests.push({path,options});let data;
    if(path==='/auth/session')data={access_token:'synthetic-memory-only',expires_at:Math.floor(Date.now()/1000)+300};
    else if(path==='runtime-config.json')data=config;
    else if(path==='presets/indentation-v1.json')data=preset;
    else if(path.endsWith('/v1/me'))data={subject:'synthetic-owner',email:'synthetic@example.test'};
    else if(path.endsWith('/v1/projects'))data={projects:[{id:'p',name:'Synthetic P'},{id:'q',name:'Synthetic Q'}]};
    else if(path.endsWith('/models'))data={models:[{model_version:'approved-test',status:'released',provenance:{pipeline_binding:{id:preset.scientific_profile}}}]};
    else if(path.endsWith('/jobs'))data={jobs:[{id:'finished',status:'succeeded',diameter_um:3.2}]};
    else if(path.endsWith('/jobs/finished/result')){
      if(holdResult){holdResult=false;await new Promise(resolve=>releaseResult=resolve);}
      data={...structuredClone(result),artifacts:missing?[]:[artifact]};
    }
    else if(path.endsWith('/jobs/finished'))data={id:'finished',status:'succeeded',diameter_um:3.2,owner_sub:'synthetic-owner',configuration:preset.configuration,input_artifacts:[]};
    else if(path.endsWith('/artifacts/posterior-test')){
      if(unauthorized)return new Response(JSON.stringify({error:{message:'Synthetic session expired'}}),{status:401,headers:{'Content-Type':'application/json'}});
      data={artifact,download_url:'https://artifacts.example.test/posterior'};
    }
    else if(path==='https://artifacts.example.test/posterior'){
      const captured=mode;
      if(captured==='hold')await new Promise(resolve=>releaseImage=resolve);
      const body=bytes.slice();if(captured==='corrupt')body[20]^=1;
      return new Response(body,{headers:{'Content-Type':'image/png'}});
    }
    else if(path==='/auth/logout')data={logout_url:config.cognitoDomain+'/logout?'+new URLSearchParams({client_id:config.clientId,logout_uri:location.origin+'/auth/login'})};
    else throw Error('Unexpected synthetic route');
    return new Response(JSON.stringify(data),{headers:{'Content-Type':'application/json'}});
  };
  const settle=async predicate=>{for(let i=0;i<300&&!predicate();i++)await pause(2);assert.ok(predicate(),'Expected UI state did not appear');await pause(0);};
  const click=(action,extra={})=>listeners.click({target:{closest:()=>({dataset:{action,...extra},disabled:false})}});
  const change=(id,value)=>listeners.change({target:{id,value}});
  await import('../src/app.js?posterior-workspace-'+key);await settle(()=>element.innerHTML.includes('Configure your analysis'));
  return {element,image,requests,redirects,click,change,settle,
    setMode:value=>{mode=value;releaseImage=undefined;},
    setMissing:value=>missing=value,
    holdNextResult:()=>{holdResult=true;releaseResult=undefined;},
    imageWaiting:()=>typeof releaseImage==='function',
    resultWaiting:()=>typeof releaseResult==='function',
    releaseImage:()=>releaseImage?.(),
    releaseResult:()=>releaseResult?.(),
    expire:()=>unauthorized=true,
    async open(){click('detail',{id:'finished'});await settle(()=>element.innerHTML.includes('Load scientific result'));},
    async ready(){click('result');await settle(()=>imagePattern.test(element.innerHTML));},
    cleanup(){releaseImage?.();releaseResult?.();globalThis.setTimeout=oldTimeout;globalThis.setInterval=oldInterval;}
  };
}
test('posterior UI displays verified bytes, retries failures and discards stale navigation results',async(t)=>{
  const h=await harness('navigation');t.after(()=>h.cleanup());
  await h.open();h.setMode('hold');h.click('result');await h.settle(h.imageWaiting);
  assert.match(h.element.innerHTML,/Loading posterior figure/);assert.doesNotMatch(h.element.innerHTML,imagePattern);
  h.releaseImage();await h.settle(()=>imagePattern.test(h.element.innerHTML));
  assert.match(h.element.innerHTML,/Posterior distributions of kA, kB and d0/);
  assert.doesNotMatch(h.element.innerHTML,/synthetic-memory-only|https:\/\/artifacts/);
  const storage=h.requests.find(r=>r.path==='https://artifacts.example.test/posterior');
  assert.equal(storage.options.credentials,'omit');assert.equal(storage.options.redirect,'error');assert.equal(storage.options.headers,undefined);
  h.image.handlers.error();await h.settle(()=>h.element.innerHTML.includes('Retry figure'));assert.doesNotMatch(h.element.innerHTML,imagePattern);
  h.setMode('good');h.click('retry-posterior');await h.settle(()=>imagePattern.test(h.element.innerHTML));
  h.setMode('corrupt');h.click('refresh-detail');await h.settle(()=>!h.element.innerHTML.includes('Posterior distributions'));
  h.click('result');await h.settle(()=>h.element.innerHTML.includes('Retry figure'));assert.doesNotMatch(h.element.innerHTML,imagePattern);
  h.setMode('good');h.click('retry-posterior');await h.settle(()=>imagePattern.test(h.element.innerHTML));
  h.setMode('hold');h.click('refresh-detail');await h.settle(()=>!h.element.innerHTML.includes('Posterior distributions'));
  h.click('result');await h.settle(h.imageWaiting);h.click('back');assert.doesNotMatch(h.element.innerHTML,imagePattern);
  assert.equal(h.requests.filter(r=>r.path==='https://artifacts.example.test/posterior').at(-1).options.signal.aborted,true);
  h.releaseImage();await pause(10);assert.doesNotMatch(h.element.innerHTML,/Posterior distributions|data-posterior-image/);
  await h.open();h.holdNextResult();h.click('result');await h.settle(h.resultWaiting);h.click('tab',{tab:'jobs'});h.releaseResult();await pause(10);
  assert.doesNotMatch(h.element.innerHTML,/Posterior distributions|data-posterior-image/);
  await h.open();h.setMode('hold');h.click('result');await h.settle(h.imageWaiting);h.change('project','q');h.releaseImage();await pause(10);
  assert.doesNotMatch(h.element.innerHTML,/Posterior distributions|data-posterior-image/);
  h.change('project','p');await h.settle(()=>h.element.innerHTML.includes('<option value="p" selected>'));
  h.setMissing(true);await h.open();h.click('result');await h.settle(()=>h.element.innerHTML.includes('Posterior figure unavailable'));
  assert.match(h.element.innerHTML,/Numerical data &amp; ready figures/);assert.doesNotMatch(h.element.innerHTML,imagePattern);
  h.setMissing(false);h.setMode('hold');h.click('refresh-detail');await h.settle(()=>!h.element.innerHTML.includes('Posterior distributions'));
  h.click('result');await h.settle(h.imageWaiting);h.click('logout');h.releaseImage();await h.settle(()=>h.redirects.length>0);
  assert.doesNotMatch(h.element.innerHTML,/Posterior distributions|data-posterior-image/);
});
test('session expiry during posterior metadata retrieval clears the result and returns to sign-in',async(t)=>{
  const h=await harness('expiry');t.after(()=>h.cleanup());await h.open();h.expire();h.click('result');
  await h.settle(()=>h.redirects.includes('/auth/login'));
  assert.doesNotMatch(h.element.innerHTML,/Posterior distributions|data-posterior-image|synthetic-memory-only/);
});
