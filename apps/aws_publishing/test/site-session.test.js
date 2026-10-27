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
