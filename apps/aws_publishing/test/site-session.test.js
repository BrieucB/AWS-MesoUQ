import test from 'node:test';
import assert from 'node:assert/strict';
import {Api,readSiteSession,endSiteSession} from '../src/client.js';
const config={apiBaseUrl:'https://api.example.test',cognitoDomain:'https://identity.example.test',clientId:'public-client'};
const origin='https://portal.example.test';
const response=(data,status=200)=>({ok:status>=200&&status<300,status,json:async()=>data});
const logoutUrl=()=>config.cognitoDomain+'/logout?'+new URLSearchParams({client_id:config.clientId,logout_uri:origin+'/auth/login'});
test('site hydration uses credentialed no-store same-origin auth endpoint and epoch-second expiry',async()=>{
  const now=1800000000000;let request;
  const session=await readSiteSession(async(...args)=>{request=args;return response({access_token:'memory-only-token',expires_at:now/1000+300});},now);
  assert.deepEqual(session,{token:'memory-only-token',expiresAt:now+300000});
  assert.equal(request[0],'/auth/session');assert.equal(request[1].credentials,'include');assert.equal(request[1].cache,'no-store');assert.equal(request[1].redirect,'error');assert.equal(request[1].headers.Authorization,undefined);
});
test('missing/expired site cookie returns no token; revoked invitation remains a distinct forbidden error',async()=>{
  assert.equal(await readSiteSession(async()=>response({},401)),null);
  assert.equal(await readSiteSession(async()=>response({access_token:'expired',expires_at:1}),2000),null);
  await assert.rejects(readSiteSession(async()=>response({error:{message:'Active invitation required'}},403)),error=>error.status===403&&/invitation/.test(error.message));
  await assert.rejects(readSiteSession(async()=>response({access_token:'x',expires_at:'300'})),/Invalid site session/);
});
test('API stops sending bearer token at absolute site expiry, even before a delayed timer runs',async()=>{
  let calls=0,invalidations=0;
  const api=new Api(config,'private-token',async()=>{calls++;return response({});},{expiresAt:Date.now()-1,onUnauthorized:()=>invalidations++});
  await assert.rejects(api.request('/v1/me'),/expired/);await assert.rejects(api.request('/v1/me'),/expired/);
  assert.equal(calls,0);assert.equal(invalidations,1);assert.equal(api.token,'');
});
test('API 401 invalidates memory session; project-specific 403 does not trigger a sign-in loop',async()=>{
  for(const status of [401,403]){let invalidations=0;const api=new Api(config,'t',async()=>response({error:{message:'Denied'}},status),{onUnauthorized:()=>invalidations++});await assert.rejects(api.request('/v1/me'),status===401?/expired/:/Denied/);assert.equal(invalidations,status===401?1:0);}
});
test('logout sends credentialed POST to relative same-origin endpoint and validates returned identity redirect',async()=>{
  let request;
  assert.equal(await endSiteSession(config,async(...args)=>{request=args;return response({logout_url:logoutUrl()});},origin),logoutUrl());
  assert.equal(request[0],'/auth/logout');assert.equal(request[1].method,'POST');assert.equal(request[1].credentials,'include');assert.equal(request[1].cache,'no-store');assert.equal(request[1].redirect,'error');assert.equal(request[1].headers.Authorization,undefined);
  for(const url of ['https://attacker.example.test/logout',logoutUrl()+'&access_token=forbidden',config.cognitoDomain+'/logout?'+new URLSearchParams({client_id:'other',logout_uri:origin+'/auth/login'}),config.cognitoDomain+'/logout?'+new URLSearchParams({client_id:config.clientId,logout_uri:'https://attacker.example.test/auth/login'})])await assert.rejects(endSiteSession(config,async()=>response({logout_url:url}),origin),/outside/);
});

test('a response arriving after local session clear cannot repopulate research state',async()=>{
  let finish;const pending=new Promise(resolve=>finish=resolve);
  const api=new Api(config,'private-token',()=>pending);
  const request=api.request('/v1/me');api.token='';api.authFailed=true;
  finish(response({subject:'previous-user-private-data'}));
  await assert.rejects(request,/expired/);assert.equal(api.token,'');
});


test('remembered hydration accepts only short-grant metadata and refresh uses explicit activity JSON',async()=>{
  const {refreshSiteSession}=await import('../src/client.js');
  const now=1800000000000,data={access_token:'short-token',expires_at:now/1000+300,renewable:true,
    absolute_expires_at:now/1000+30*86400,idle_expires_at:now/1000+7*86400,generation:4};
  const session=await readSiteSession(async()=>response(data),now);
  assert.equal(session.renewable,true);assert.equal(session.generation,4);
  assert.equal(session.absoluteExpiresAt,data.absolute_expires_at*1000);
  let request;
  assert.deepEqual(await refreshSiteSession(false,async(...args)=>{request=args;return response(data);},now),session);
  assert.equal(request[0],'/auth/refresh');assert.equal(request[1].method,'POST');
  assert.deepEqual(JSON.parse(request[1].body),{activity:false});
  assert.equal(request[1].credentials,'include');assert.equal(request[1].headers.Authorization,undefined);
  for(const changed of [
    {...data,generation:-1},{...data,generation:1.5},{...data,idle_expires_at:data.absolute_expires_at+1},
    {...data,absolute_expires_at:now/1000+100},{...data,refresh_token:'forbidden'},{...data,sid:'forbidden'},
  ])await assert.rejects(readSiteSession(async()=>response(changed),now),/Invalid/);
});
test('safe GET can renew once after 401 without replacing the API instance',async()=>{
  let reads=0,renewals=0,invalidations=0;
  const original={token:'old',expiresAt:Date.now()+300000,renewable:true};
  const updated={...original,token:'renewed'};
  const api=new Api(config,original.token,async(_path,options)=>{
    reads++;assert.equal(options.headers.Authorization,'Bearer '+(reads===1?'old':'renewed'));
    return response(reads===1?{}:{value:'research-result'},reads===1?401:200);
  },{...original,onUnauthorized:()=>invalidations++,ensureSession:async options=>{
    if(options.force){renewals++;assert.equal(options.rejectedToken,'old');return updated;}
    return original;
  }});
  assert.deepEqual(await api.request('/v1/results'),{value:'research-result'});
  assert.equal(reads,2);assert.equal(renewals,1);assert.equal(invalidations,0);assert.equal(api.token,'renewed');
});
test('mutation 401 renews future access but never replays the business POST or its idempotency key',async()=>{
  let writes=0,renewals=0,invalidations=0,submitted;
  const original={token:'old',expiresAt:Date.now()+300000,renewable:true};
  const api=new Api(config,original.token,async(_path,options)=>{
    writes++;submitted=JSON.parse(options.body);return response({},401);
  },{...original,onUnauthorized:()=>invalidations++,ensureSession:async options=>{
    if(options.force){renewals++;return {...original,token:'renewed'};}return original;
  }});
  await assert.rejects(api.request('/v1/campaigns',{idempotency_key:'one-intent'}),/not retried/);
  assert.equal(writes,1);assert.equal(renewals,1);assert.deepEqual(submitted,{idempotency_key:'one-intent'});
  assert.equal(invalidations,0);assert.equal(api.token,'renewed');
});
test('transient renewal error preserves the API token and does not send a business mutation',async()=>{
  let writes=0,invalidations=0;
  const api=new Api(config,'current',async()=>{writes++;return response({});},{
    expiresAt:Date.now()+300000,renewable:true,onUnauthorized:()=>invalidations++,
    ensureSession:async()=>{throw Object.assign(Error('Provider unavailable'),{status:503});},
  });
  await assert.rejects(api.request('/v1/campaigns',{idempotency_key:'unchanged'}),/Provider unavailable/);
  assert.equal(writes,0);assert.equal(invalidations,0);assert.equal(api.token,'current');assert.equal(api.authFailed,false);
});
test('logout during a preflight renewal cannot restore bearer memory or send the waiting mutation',async()=>{
  let resolve,requests=0;
  const pending=new Promise(finish=>resolve=finish);
  const api=new Api(config,'old',async()=>{requests++;return response({});},{
    expiresAt:Date.now()+300000,renewable:true,ensureSession:()=>pending,
  });
  const request=api.request('/v1/campaigns',{idempotency_key:'not-sent'});
  api.invalidate();resolve({token:'late',expiresAt:Date.now()+300000,renewable:true});
  await assert.rejects(request,/expired/);
  assert.equal(requests,0);assert.equal(api.token,'');assert.equal(api.authFailed,true);
});

test('postflight legacy expiry rejects a delayed result even with an ensure callback',async()=>{
  const originalNow=Date.now;let now=1800000000000,reads=0,ensures=0,invalidations=0;
  Date.now=()=>now;
  const session={token:'legacy-memory-token',expiresAt:now+1000};
  const api=new Api(config,session.token,async()=>{
    reads++;now=session.expiresAt+1;return response({private:'late-result'});
  },{...session,ensureSession:async()=>{ensures++;return session;},onUnauthorized:()=>invalidations++});
  try{
    await assert.rejects(api.request('/v1/results'),/expired/);
    assert.equal(reads,1);assert.equal(ensures,2);assert.equal(invalidations,1);assert.equal(api.token,'');
  }finally{Date.now=originalNow;}
});
test('postflight expired short grant renews authentication without replaying a successful mutation',async()=>{
  const originalNow=Date.now;let now=1800000000000,writes=0,ensures=0;
  Date.now=()=>now;
  const initial={token:'initial',expiresAt:now+1000,renewable:true};
  const renewed={...initial,token:'renewed',expiresAt:now+300000};
  const api=new Api(config,initial.token,async()=>{
    writes++;now=initial.expiresAt+1;return response({id:'already-created'});
  },{...initial,ensureSession:async()=>++ensures===1?initial:renewed});
  try{
    assert.deepEqual(await api.request('/v1/campaigns',{idempotency_key:'single-intent'}),{id:'already-created'});
    assert.equal(writes,1);assert.equal(ensures,2);assert.equal(api.token,'renewed');assert.equal(api.authFailed,false);
  }finally{Date.now=originalNow;}
});
test('postflight transient auth failure preserves memory and never replays the completed mutation',async()=>{
  const originalNow=Date.now;let now=1800000000000,writes=0,ensures=0,invalidations=0;
  Date.now=()=>now;
  const session={token:'retained',expiresAt:now+1000,renewable:true};
  const api=new Api(config,session.token,async()=>{
    writes++;now=session.expiresAt+1;return response({id:'already-created'});
  },{...session,onUnauthorized:()=>invalidations++,ensureSession:async()=>{
    if(++ensures===1)return session;
    throw Object.assign(Error('Provider temporarily unavailable'),{status:503});
  }});
  try{
    await assert.rejects(api.request('/v1/campaigns',{idempotency_key:'single-intent'}),error=>error.status===503);
    assert.equal(writes,1);assert.equal(ensures,2);assert.equal(invalidations,0);
    assert.equal(api.token,'retained');assert.equal(api.authFailed,false);
  }finally{Date.now=originalNow;}
});
