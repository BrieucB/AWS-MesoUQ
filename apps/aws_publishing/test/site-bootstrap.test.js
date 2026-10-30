import test from 'node:test';
import {readFile} from 'node:fs/promises';
const preset=JSON.parse(await readFile(new URL('../../../src/aws_mesouq/presets/indentation-v1.json',import.meta.url)));
import assert from 'node:assert/strict';
import {setTimeout as pause} from 'node:timers/promises';
const config={apiBaseUrl:'https://api.example.test',cognitoDomain:'https://identity.example.test',clientId:'public-client',redirectUri:'https://portal.example.test/auth/callback',logoutUri:'https://portal.example.test/auth/login',uploadOrigins:[]};
const response=(data,status=200)=>({ok:status>=200&&status<300,status,json:async()=>data});
let sequence=0;
async function launch({sessionStatus=200,logoutFailure=false,remembered=false,refreshStatus=200,delayRefresh=false}={}){
  const listeners={},requests=[],redirects=[],timers=[],storageWrites=[];
  const element={innerHTML:'',addEventListener:(name,handler)=>listeners[name]=handler};
  const originals={setTimeout:globalThis.setTimeout,setInterval:globalThis.setInterval};
  let generation=0,finishRefresh;
  const born=Math.floor(Date.now()/1000),absolute=born+30*86400,idle=born+7*86400;
  const session=()=>({access_token:generation?'renewed-memory-token':'memory-only-session-token',expires_at:Math.min(Math.floor(Date.now()/1000)+300,idle,absolute),
    ...(remembered?{renewable:true,absolute_expires_at:absolute,idle_expires_at:idle,generation}:{})});
  globalThis.document={querySelector:selector=>selector==='#app'?element:null,hidden:false};
  globalThis.location={origin:'https://portal.example.test',search:'',pathname:'/',assign:url=>redirects.push(String(url))};
  globalThis.history={replaceState:()=>{}};
  globalThis.sessionStorage={removeItem:()=>{},setItem:(...args)=>storageWrites.push(args),getItem:()=>{throw Error('Protected bootstrap must not read OAuth/browser-token storage');}};
  globalThis.setTimeout=(callback,duration)=>{timers.push({callback,duration});return 123;};globalThis.setInterval=()=>124;
  globalThis.fetch=async(path,options={})=>{
    requests.push({path:String(path),options});
    if(path==='/auth/session')return response(sessionStatus===200?session():{error:{message:'Active invitation required',code:'refresh_required'}},sessionStatus);
    if(path==='/auth/refresh'){
      if(delayRefresh)await new Promise(resolve=>finishRefresh=resolve);
      if((sessionStatus===401&&!remembered)||refreshStatus!==200)return response({error:{message:'Synthetic renewal failure'}},refreshStatus!==200?refreshStatus:401);
      generation++;return response(session());
    }
    if(path==='runtime-config.json')return response(config);
    if(path==='presets/indentation-v1.json')return response(preset);
    if(path==='/auth/logout'){if(logoutFailure)throw Error('Synthetic network failure');return response({logout_url:config.cognitoDomain+'/logout?'+new URLSearchParams({client_id:config.clientId,logout_uri:location.origin+'/auth/login'})});}
    if(String(path).endsWith('/v1/me'))return response({subject:'researcher',email:'session@example.test',roles:[]});
    if(String(path).endsWith('/v1/projects'))return response({projects:[{id:'p',name:'Research project',role:'member'}]});
    if(String(path).endsWith('/jobs'))return response({jobs:[]});
    if(String(path).endsWith('/models'))return response({models:[]});
    throw Error('Unexpected test request');
  };
  await import(new URL('../src/app.js?bootstrap-test='+sequence++,import.meta.url));
  const wait=async predicate=>{for(let i=0;i<100&&!predicate();i++)await pause(2);assert.ok(predicate());};
  return {element,requests,redirects,timers,storageWrites,wait,
    click:action=>listeners.click({target:{closest:()=>({dataset:{action},disabled:false})}}),
    change:(id,value,files)=>listeners.change({target:{id,value,files}}),
    finishRefresh:()=>finishRefresh?.(),
    restore:()=>{globalThis.setTimeout=originals.setTimeout;globalThis.setInterval=originals.setInterval;}};
}
test('missing auth cookie redirects to public bootstrap before loading protected runtime/API data',async()=>{
  const harness=await launch({sessionStatus:401});try{await harness.wait(()=>harness.redirects.length>0);assert.deepEqual(harness.redirects,['/auth/login']);assert.deepEqual(harness.requests.map(r=>r.path),['/auth/session','/auth/refresh']);assert.equal(harness.storageWrites.length,0);}finally{harness.restore();}
});
test('revoked invitation displays denial without automatic repeated Cognito redirects',async()=>{
  const harness=await launch({sessionStatus:403});try{await harness.wait(()=>harness.element.innerHTML.includes('Active invitation required'));assert.equal(harness.redirects.length,0);assert.deepEqual(harness.requests.map(r=>r.path),['/auth/session']);}finally{harness.restore();}
});
test('session hydrates memory-only bearer, then cookie expiry clears project view and reauthenticates',async()=>{
  const harness=await launch();try{
    await harness.wait(()=>harness.element.innerHTML.includes('Configure your analysis'));
    assert.equal(harness.requests[0].path,'/auth/session');assert.equal(harness.requests[1].options.credentials,'same-origin');assert.equal(harness.requests[1].options.redirect,'error');
    const me=harness.requests.find(r=>r.path.endsWith('/v1/me'));assert.equal(me.options.headers.Authorization,'Bearer memory-only-session-token');assert.equal(me.options.credentials,'omit');assert.equal(harness.storageWrites.length,0);
    assert.ok(harness.timers[0].duration>290000&&harness.timers[0].duration<=300000);
    harness.timers[0].callback();assert.equal(harness.redirects.at(-1),'/auth/login');assert.doesNotMatch(harness.element.innerHTML,/Configure your analysis|session@example.test/);
  }finally{harness.restore();}
});
test('sign-out clears visible research state and calls server cookie logout before hosted redirect',async()=>{
  const harness=await launch();try{
    await harness.wait(()=>harness.element.innerHTML.includes('Configure your analysis'));harness.click('logout');
    assert.doesNotMatch(harness.element.innerHTML,/Configure your analysis|session@example.test/);
    await harness.wait(()=>harness.redirects.length>0);
    const request=harness.requests.find(r=>r.path==='/auth/logout');assert.equal(request.options.method,'POST');assert.equal(request.options.credentials,'include');assert.equal(request.options.headers.Authorization,undefined);assert.match(harness.redirects.at(-1),/^https:\/\/identity\.example\.test\/logout\?/);
  }finally{harness.restore();}
});
test('failed cookie logout does not claim completion and offers an explicit retry',async()=>{
  const harness=await launch({logoutFailure:true});try{await harness.wait(()=>harness.element.innerHTML.includes('Configure your analysis'));harness.click('logout');await harness.wait(()=>harness.element.innerHTML.includes('Retry sign-out'));assert.equal(harness.redirects.length,0);assert.match(harness.element.innerHTML,/Site cookies could not be cleared/);assert.doesNotMatch(harness.element.innerHTML,/session@example.test/);}finally{harness.restore();}
});


test('remembered cold entry restores the app through one passive refresh without a login redirect',async()=>{
  const harness=await launch({sessionStatus:401,remembered:true});try{
    await harness.wait(()=>harness.element.innerHTML.includes('Configure your analysis'));
    assert.deepEqual(harness.requests.slice(0,2).map(r=>r.path),['/auth/session','/auth/refresh']);
    assert.deepEqual(JSON.parse(harness.requests[1].options.body),{activity:false});
    assert.equal(harness.redirects.length,0);assert.equal(harness.storageWrites.length,0);
    assert.match(harness.element.innerHTML,/This browser is remembered/);
  }finally{harness.restore();}
});
test('automatic renewal preserves the selected CSV preview, units and current workspace',async()=>{
  const harness=await launch({remembered:true});try{
    await harness.wait(()=>harness.element.innerHTML.includes('Configure your analysis'));
    harness.change('forceUnit','nN');harness.change('displacementUnit','nm');
    harness.change('file',undefined,[new File(['1,2\n3,4'],'private-preview.csv',{type:'text/csv'})]);
    await harness.wait(()=>harness.element.innerHTML.includes('private-preview.csv'));
    const before=harness.element.innerHTML,apiCalls=harness.requests.filter(r=>r.path.startsWith('https://api.')).length;
    assert.ok(harness.timers[0].duration>230000&&harness.timers[0].duration<=240000);
    harness.timers[0].callback();
    await harness.wait(()=>harness.requests.some(r=>r.path==='/auth/refresh'));await pause(3);
    assert.equal(harness.element.innerHTML,before);
    assert.equal(harness.requests.filter(r=>r.path.startsWith('https://api.')).length,apiCalls);
    assert.equal(harness.redirects.length,0);assert.equal(harness.storageWrites.length,0);
    harness.click('refresh');await pause(3);
    const latest=harness.requests.filter(r=>r.path.endsWith('/jobs')).at(-1);
    assert.equal(latest.options.headers.Authorization,'Bearer renewed-memory-token');
    assert.match(harness.element.innerHTML,/private-preview.csv|Upload &amp; verify curve/);
  }finally{harness.restore();}
});
test('transient renewal failure keeps the preview and does not redirect or submit calculations',async()=>{
  const harness=await launch({remembered:true,refreshStatus:503});try{
    await harness.wait(()=>harness.element.innerHTML.includes('Configure your analysis'));
    harness.change('file',undefined,[new File(['1,2\n3,4'],'retained.csv',{type:'text/csv'})]);
    await harness.wait(()=>harness.element.innerHTML.includes('retained.csv'));
    harness.timers[0].callback();await harness.wait(()=>harness.requests.some(r=>r.path==='/auth/refresh'));await pause(3);
    assert.match(harness.element.innerHTML,/retained.csv/);assert.equal(harness.redirects.length,0);
    assert.ok(!harness.requests.some(r=>r.options.method==='POST'&&r.path.startsWith('https://api.')));
  }finally{harness.restore();}
});
test('logout wins over an in-flight remembered refresh and its late response cannot restore the form',async()=>{
  const harness=await launch({remembered:true,delayRefresh:true});try{
    await harness.wait(()=>harness.element.innerHTML.includes('Configure your analysis'));
    harness.timers[0].callback();await harness.wait(()=>harness.requests.some(r=>r.path==='/auth/refresh'));
    harness.click('logout');await harness.wait(()=>harness.redirects.length>0);
    harness.finishRefresh();await pause(5);
    assert.doesNotMatch(harness.element.innerHTML,/Configure your analysis|session@example.test/);
    assert.equal(harness.redirects.length,1);assert.equal(harness.storageWrites.length,0);
  }finally{harness.restore();}
});
