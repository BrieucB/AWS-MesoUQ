import test from 'node:test';
import assert from 'node:assert/strict';
import {SiteSession} from '../src/site-session.js';
import {Api} from '../src/client.js';

const response=(data,status=200,headers={})=>({ok:status>=200&&status<300,status,json:async()=>data,headers:{get:name=>headers[name]??null}});
const settle=async()=>{for(let i=0;i<20;i++)await Promise.resolve();};
class Events{
  constructor(){this.listeners=new Map();this.hidden=false;this.focused=true;}
  addEventListener(name,fn){if(!this.listeners.has(name))this.listeners.set(name,new Set());this.listeners.get(name).add(fn);}
  removeEventListener(name,fn){this.listeners.get(name)?.delete(fn);}
  emit(name,event={}){for(const fn of [...(this.listeners.get(name)||[])])fn(event);}
  hasFocus(){return this.focused;}
}
class Clock{
  constructor(){this.time=1800000000000;this.next=0;this.tasks=new Map();}
  set(fn,ms){const id=++this.next;this.tasks.set(id,{fn,at:this.time+Math.max(0,ms)});return id;}
  clear(id){this.tasks.delete(id);}
  async advance(ms){
    const target=this.time+ms;let count=0;
    for(;;){
      const due=[...this.tasks].filter(([,task])=>task.at<=target).sort((a,b)=>a[1].at-b[1].at)[0];
      if(!due)break;
      assert.ok(++count<200,'Timer execution stays bounded');
      this.time=due[1].at;this.tasks.delete(due[0]);due[1].fn();await settle();
    }
    this.time=target;await settle();
  }
}
function harness({cold=false,legacy=false,refresh=null,generation=0,limit=null,channel=null}={}){
  const clock=new Clock(),doc=new Events(),win=new Events(),calls=[],updates=[],ends=[],transient=[];
  const birth=clock.time,absolute=birth+(limit??30*86400000);
  let idle=Math.min(absolute,birth+7*86400000),version=generation;
  const grant=(g=version,token='synthetic-'+g)=>({
    access_token:token,expires_at:Math.min(clock.time+300000,idle,absolute)/1000,
    ...(legacy?{}:{renewable:true,absolute_expires_at:absolute/1000,idle_expires_at:idle/1000,generation:g}),
  });
  const fetcher=async(path,options)=>{
    calls.push({path,options});
    if(path==='/auth/session')return cold?response({error:{code:'refresh_required'}},401):response(grant());
    assert.equal(path,'/auth/refresh');
    assert.deepEqual(Object.keys(JSON.parse(options.body)),['activity']);
    const activity=JSON.parse(options.body).activity;
    if(refresh)return refresh(activity,calls.filter(x=>x.path==='/auth/refresh').length,{grant,clock});
    if(activity)idle=Math.min(absolute,clock.time+7*86400000);
    return response(grant(++version));
  };
  const manager=new SiteSession({fetcher,now:()=>clock.time,setTimer:(fn,ms)=>clock.set(fn,ms),
    clearTimer:id=>clock.clear(id),document:doc,window:win,channelFactory:()=>channel,
    onSession:s=>updates.push(s),onEnd:(reason,error)=>ends.push({reason,error}),onTransient:e=>transient.push(e)});
  return {manager,clock,doc,win,calls,updates,ends,transient,grant,absolute,get idle(){return idle;}};
}
const refreshes=h=>h.calls.filter(x=>x.path==='/auth/refresh');

test('cold entry resumes a remembered BFF session once without storing credentials',async()=>{
  const h=harness({cold:true});try{
    const session=await h.manager.bootstrap();
    assert.equal(session.renewable,true);assert.equal(h.calls.length,2);
    assert.deepEqual(JSON.parse(h.calls[1].options.body),{activity:false});
    assert.equal(h.calls[1].options.credentials,'include');
    assert.equal(h.calls[1].options.headers.Authorization,undefined);
    assert.deepEqual(Object.keys(session).sort(),['absoluteExpiresAt','expiresAt','generation','idleExpiresAt','renewable','token']);
  }finally{h.manager.dispose();}
});
test('renewal is singleflight and does not reset the accepted memory session',async()=>{
  let finish;const pending=new Promise(resolve=>finish=resolve);
  const h=harness({refresh:()=>pending});try{
    const old=await h.manager.bootstrap();
    const a=h.manager.ensure({force:true}),b=h.manager.ensure({force:true});await settle();
    assert.equal(refreshes(h).length,1);assert.equal(h.manager.session,old);
    finish(response(h.grant(1,'new-short-grant')));
    assert.equal((await a).token,'new-short-grant');assert.equal((await b).token,'new-short-grant');
    assert.equal(h.updates.length,2);assert.equal(h.ends.length,0);
  }finally{h.manager.dispose();}
});
test('timer, background polling and tab resume never claim foreground activity',async()=>{
  const h=harness();try{
    await h.manager.bootstrap();const idle=h.idle;
    h.manager.noteActivity({isTrusted:false});h.doc.hidden=true;h.manager.noteActivity({isTrusted:true});
    await h.clock.advance(240000);
    assert.deepEqual(refreshes(h).map(x=>JSON.parse(x.options.body)),[{activity:false}]);
    assert.equal(h.idle,idle);
    h.doc.hidden=false;h.clock.time+=301000;h.win.emit('pageshow');await settle();
    assert.equal(refreshes(h).length,2);assert.equal(JSON.parse(refreshes(h)[1].options.body).activity,false);
    assert.equal(h.idle,idle);
  }finally{h.manager.dispose();}
});
test('only fresh trusted foreground input can extend the idle limit; signals are throttled',async()=>{
  const h=harness();try{
    await h.manager.bootstrap();const idle=h.idle;
    h.doc.emit('pointerdown',{isTrusted:true});h.doc.emit('input',{isTrusted:true});
    await h.clock.advance(250);
    assert.equal(refreshes(h).length,1);assert.equal(JSON.parse(refreshes(h)[0].options.body).activity,true);
    assert.ok(h.idle>idle);assert.equal(h.manager.session.absoluteExpiresAt,h.absolute);
    h.doc.emit('input',{isTrusted:true});await h.clock.advance(1000);
    assert.equal(refreshes(h).length,1);
    h.doc.hidden=true;await h.clock.advance(60000);
    assert.equal(refreshes(h).length,1,'A queued input cannot extend idle after the tab becomes hidden');
  }finally{h.manager.dispose();}
});
test('409 refresh contention retries once after Retry-After without forcing sign-in',async()=>{
  const h=harness({refresh:(_activity,n,{grant})=>n===1?
    response({error:{code:'refresh_in_progress'}},409,{'Retry-After':'1'}):response(grant(1))});try{
    await h.manager.bootstrap();const pending=h.manager.ensure({force:true});await settle();
    assert.equal(refreshes(h).length,1);await h.clock.advance(1000);await pending;
    assert.equal(refreshes(h).length,2);assert.equal(h.ends.length,0);
  }finally{h.manager.dispose();}
});
test('503 keeps the current work/session and uses bounded retry rather than sign-in',async()=>{
  let fail=true;const h=harness({refresh:(_a,_n,{grant})=>fail?
    response({error:{message:'Provider unavailable'}},503):response(grant(1))});try{
    const old=await h.manager.bootstrap();
    await assert.rejects(h.manager.ensure({force:true}),error=>error.status===503);
    assert.equal(h.manager.session,old);assert.equal(h.ends.length,0);assert.equal(h.transient.length,1);
    fail=false;await h.clock.advance(5000);
    assert.equal(h.manager.session.generation,1);assert.equal(h.ends.length,0);
  }finally{h.manager.dispose();}
});
test('definitive refresh rejection and local limits end renewal; timers cannot extend remembered duration',async()=>{
  for(const status of [401,403]){
    const h=harness({refresh:()=>response({error:{message:'Denied'}},status)});
    try{await h.manager.bootstrap();await assert.rejects(h.manager.ensure({force:true}));
      assert.equal(h.manager.closed,true);assert.equal(h.manager.session,null);assert.equal(h.ends.length,1);
    }finally{h.manager.dispose();}
  }
  const h=harness({limit:20000});await h.manager.bootstrap();await h.clock.advance(20000);
  assert.equal(h.ends[0].reason,'expired');assert.equal(refreshes(h).length,0);
});
test('legacy sessions keep their original five-minute expiry without background renewal',async()=>{
  const h=harness({legacy:true});await h.manager.bootstrap();
  await h.clock.advance(300000);
  assert.equal(refreshes(h).length,0);assert.equal(h.ends[0].reason,'expired');
});
test('logout aborts in-flight refresh and discards a late token response',async()=>{
  let finish;const pending=new Promise(resolve=>finish=resolve),messages=[];
  const channel={postMessage:message=>messages.push(message),close(){}};
  const h=harness({refresh:()=>pending,channel});
  await h.manager.bootstrap();const update=h.manager.ensure({force:true});await settle();
  const signal=refreshes(h)[0].options.signal;
  h.manager.end({reason:'signed_out',broadcast:true});
  assert.equal(signal.aborted,true);finish(response(h.grant(1,'late-credential')));
  await assert.rejects(update,error=>error.authEnded);
  assert.equal(h.manager.session,null);assert.equal(h.updates.length,1);
  assert.deepEqual(messages,[{type:'logout'}]);assert.equal(h.ends.length,1);
});
test('another tab logout closes memory state and blocks late refresh without broadcasting tokens',async()=>{
  const channels=new Set(),messages=[];
  const makeChannel=()=>{
    const channel={handler:null,addEventListener(_name,fn){this.handler=fn;},
      removeEventListener(){this.handler=null;},close(){channels.delete(this);},
      postMessage(data){messages.push(data);for(const other of [...channels])if(other!==this)other.handler?.({data});}};
    channels.add(channel);return channel;
  };
  const a=harness({channel:makeChannel()}),b=harness({channel:makeChannel()});
  await a.manager.bootstrap();await b.manager.bootstrap();a.manager.end({reason:'signed_out',broadcast:true});
  assert.equal(a.manager.closed,true);assert.equal(b.manager.closed,true);
  assert.equal(b.manager.session,null);assert.deepEqual(messages,[{type:'logout'}]);
});
test('an older server generation cannot replace a newer accepted token',async()=>{
  const h=harness({generation:10,refresh:(_a,_n,{grant})=>response(grant(9,'stale-token'))});try{
    const current=await h.manager.bootstrap();
    assert.equal(await h.manager.ensure({force:true}),current);
    assert.equal(h.manager.session.token,'synthetic-10');assert.equal(h.updates.length,1);
  }finally{h.manager.dispose();}
});

test('cached idle snapshot is revalidated after another tab extended the server session',async()=>{
  const h=harness({refresh:(_activity,_n,{grant,clock})=>response({
    ...grant(1,'same-family-new-idle'),expires_at:(clock.time+300000)/1000,
    idle_expires_at:Math.min(h.absolute,clock.time+7*86400000)/1000,
  })});try{
    const old=await h.manager.bootstrap();h.clock.time=old.idleExpiresAt+1000;
    assert.equal(h.manager.isActive(),true,'A stale idle snapshot cannot clear the current form');
    const session=await h.manager.ensure();
    assert.equal(session.token,'same-family-new-idle');assert.ok(session.idleExpiresAt>old.idleExpiresAt);
    assert.deepEqual(refreshes(h).map(x=>JSON.parse(x.options.body)),[{activity:false}]);
    assert.equal(session.absoluteExpiresAt,old.absoluteExpiresAt);assert.equal(h.ends.length,0);
  }finally{h.manager.dispose();}
});
test('activity renewal crossing the superseded idle end accepts its successful same-epoch response',async()=>{
  let finish;const pending=new Promise(resolve=>finish=resolve);
  const h=harness({refresh:()=>pending});try{
    const old=await h.manager.bootstrap();h.clock.time=old.idleExpiresAt-1000;
    const request=h.manager.renew({sequence:1,at:h.clock.time});await settle();
    assert.equal(JSON.parse(refreshes(h)[0].options.body).activity,true);
    h.clock.time=old.idleExpiresAt+1000;
    finish(response({...h.grant(1,'activity-renewed'),expires_at:(h.clock.time+300000)/1000,
      idle_expires_at:Math.min(h.absolute,old.idleExpiresAt-1000+7*86400000)/1000}));
    const session=await request;
    assert.equal(session.token,'activity-renewed');assert.equal(h.manager.closed,false);
    assert.ok(session.idleExpiresAt>old.idleExpiresAt);assert.equal(session.absoluteExpiresAt,old.absoluteExpiresAt);
    assert.equal(h.ends.length,0);
  }finally{h.manager.dispose();}
});
test('server idle rejection remains definitive after passive stale-snapshot revalidation',async()=>{
  const h=harness({refresh:()=>response({error:{code:'session_expired'}},401)});try{
    const old=await h.manager.bootstrap();h.clock.time=old.idleExpiresAt+1000;
    await assert.rejects(h.manager.ensure(),error=>error.authEnded);
    assert.deepEqual(refreshes(h).map(x=>JSON.parse(x.options.body)),[{activity:false}]);
    assert.equal(h.manager.closed,true);assert.equal(h.manager.session,null);assert.equal(h.ends.length,1);
  }finally{h.manager.dispose();}
});
test('postflight remembered absolute cap blocks late data even when the expiry timer is delayed',async()=>{
  const originalNow=Date.now,h=harness({limit:120000});let reads=0,invalidations=0;
  Date.now=()=>h.clock.time;
  try{
    const session=await h.manager.bootstrap();
    const api=new Api({apiBaseUrl:'https://api.example.test'},session.token,async()=>{
      reads++;h.clock.time=h.absolute+1;return response({private:'late-result'});
    },{...session,onUnauthorized:()=>invalidations++,ensureSession:options=>h.manager.ensure(options)});
    await assert.rejects(api.request('/v1/results'),error=>error.authEnded||/expired/.test(error.message));
    assert.equal(reads,1);assert.equal(invalidations,1);assert.equal(api.authFailed,true);
    assert.equal(h.manager.closed,true);assert.equal(h.ends.length,1);assert.equal(refreshes(h).length,0);
  }finally{Date.now=originalNow;h.manager.dispose();}
});
