import test from 'node:test';
import assert from 'node:assert/strict';
import {createHash} from 'node:crypto';
import {Api} from '../src/client.js';
import {posteriorArtifact,loadPosteriorImage} from '../src/posterior.js';

// A synthetic one-pixel PNG, never a scientific result.
const bytes = new Uint8Array(Buffer.from('iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+a0V8AAAAASUVORK5CYII=', 'base64'));
const hash = value => createHash('sha256').update(value).digest('hex');
const artifact = {id:'output-1',name:'figures__posterior.png',kind:'result',media_type:'image/png',sha256:hash(bytes),size_bytes:bytes.length};
const signed = 'https://private.example.test/posterior?synthetic-signature=secret';
function fixture({metadata=artifact, body=bytes, type='image/png', headers={}, status=200, fetcher}={}) {
  const calls = [];
  const api = {
    project:(project,path)=>'/v1/projects/'+encodeURIComponent(project)+path,
    request:async path=>{calls.push(['api',path]);return {artifact:metadata,download_url:signed};},
    validateStorageUrl:url=>{calls.push(['validate',url]); assert.equal(new URL(url).origin,'https://private.example.test');}
  };
  return {api, calls, fetcher:fetcher || (async (url,options)=>{
    calls.push(['storage',url,options]);
    return new Response(body,{status,headers:{'Content-Type':type,...headers}});
  })};
}
test('selects only explicit exact release posterior PNG references',()=>{
  assert.deepEqual(posteriorArtifact({artifacts:[artifact]}),artifact);
  assert.equal(Object.isFrozen(posteriorArtifact({artifacts:[artifact]})),true);
  assert.equal(posteriorArtifact({nested:{artifacts:[artifact]}}),null);
  for(const name of ['posterior.png','figures/posterior.png','figures__posterior.svg','other__posterior.png','figures__posterior.pdf','posterior.csv']) {
    assert.equal(posteriorArtifact({artifacts:[{...artifact,name}]}),null);
  }
  assert.throws(()=>posteriorArtifact({artifacts:[artifact,{...artifact,id:'other'}]}),/ambiguous/);
  assert.throws(()=>posteriorArtifact({artifacts:{}}),/list/);
});
test('rejects malformed selected metadata and bounded sizes',()=>{
  for(const patch of [{id:''},{id:'bad\nvalue'},{kind:'input'},{media_type:'image/svg+xml'},{sha256:'x'},{sha256:undefined},{size_bytes:0},{size_bytes:Infinity},{size_bytes:1.5},{size_bytes:10*1024*1024+1}]) {
    assert.throws(()=>posteriorArtifact({artifacts:[{...artifact,...patch}]}),/metadata/);
  }
});
test('valid PNG is verified and encoded without placing signed URLs in the image',async()=>{
  const f=fixture();
  const result=await loadPosteriorImage({...f,project:'p',artifact});
  assert.equal(result,'data:image/png;base64,'+Buffer.from(bytes).toString('base64'));
  assert.deepEqual(f.calls.map(call=>call[0]),['api','validate','storage']);
  const options=f.calls[2][2];
  assert.equal(options.credentials,'omit');
  assert.equal(options.redirect,'error');
  assert.equal(options.cache,'no-store');
  assert.equal(options.headers,undefined);
});
test('actual API sends bearer only to API and validates private storage origin',async()=>{
  const calls=[];
  const api=new Api({apiBaseUrl:'https://api.example.test',uploadOrigins:['https://private.example.test']},'synthetic-token',async(url,options)=>{
    calls.push([url,options]);
    return new Response(JSON.stringify({artifact,download_url:signed}),{headers:{'Content-Type':'application/json'}});
  });
  await loadPosteriorImage({api,project:'project /',artifact,fetcher:async(url,options)=>{
    calls.push([url,options]);return new Response(bytes,{headers:{'Content-Type':'image/png'}});
  }});
  assert.equal(calls.length,2);
  assert.equal(calls[0][1].headers.Authorization,'Bearer synthetic-token');
  assert.equal(calls[1][1].headers,undefined);
  assert.equal(calls[1][1].credentials,'omit');
  const f=fixture();f.api.validateStorageUrl=()=>{throw Error(signed);};
  await assert.rejects(loadPosteriorImage({...f,project:'p',artifact}),error=>error.message==='Posterior image could not be loaded or verified.');
  assert.equal(f.calls.some(call=>call[0]==='storage'),false);
});
test('every returned identity and integrity field must match trusted reference',async()=>{
  for(const patch of [{id:'other'},{name:'posterior.png'},{sha256:'0'.repeat(64)},{size_bytes:bytes.length+1},{kind:'input'},{media_type:'image/jpeg'}]) {
    const f=fixture({metadata:{...artifact,...patch}});
    await assert.rejects(loadPosteriorImage({...f,project:'p',artifact}),/metadata/);
    assert.equal(f.calls.some(call=>call[0]==='storage'),false);
  }
});
test('rejects mismatched MIME, HTTP errors, lengths, signatures and hashes',async()=>{
  for(const options of [{type:'image/svg+xml'},{type:'image/png; charset=utf-8'},{status:403},{headers:{'Content-Length':String(bytes.length+1)}},{body:bytes.slice(0,-1)},{body:new Uint8Array(bytes.length+1)},{body:new Uint8Array(bytes.length)}]) {
    await assert.rejects(loadPosteriorImage({...fixture(options),project:'p',artifact}));
  }
  const modified=bytes.slice();modified[20]^=1;
  await assert.rejects(loadPosteriorImage({...fixture({body:modified}),project:'p',artifact}),/checksum/);
  const f=fixture({fetcher:async()=>{throw Error('network '+signed);}});
  await assert.rejects(loadPosteriorImage({...f,project:'p',artifact}),error=>!error.message.includes(signed)&&!error.message.includes('secret'));
});
test('oversized streaming response is cancelled before reading further chunks',async()=>{
  let cancelled=false,reads=0;
  const f=fixture({fetcher:async()=>({ok:true,headers:new Headers({'Content-Type':'image/png'}),body:{getReader:()=>({
    read:async()=>{reads++;return {done:false,value:new Uint8Array(bytes.length+1)};},
    cancel:async()=>{cancelled=true;}
  })}})});
  await assert.rejects(loadPosteriorImage({...f,project:'p',artifact}),/length/);
  assert.equal(reads,1);assert.equal(cancelled,true);
});
test('aborts before request, while API waits, and during body read',async()=>{
  const controller=new AbortController();controller.abort();
  const f=fixture();
  await assert.rejects(loadPosteriorImage({...f,project:'p',artifact,signal:controller.signal}),{name:'AbortError'});
  assert.equal(f.calls.length,0);
  const pendingController=new AbortController();
  f.api.request=()=>new Promise(()=>{});
  const pending=loadPosteriorImage({...f,project:'p',artifact,signal:pendingController.signal});
  pendingController.abort();
  await assert.rejects(pending,{name:'AbortError'});
  let cancelled=false;
  const streamController=new AbortController();
  const streamed=fixture({fetcher:async(url,options)=>{
    assert.equal(options.signal,streamController.signal);
    return {ok:true,headers:new Headers({'Content-Type':'image/png'}),body:{getReader:()=>({
      read:()=>{queueMicrotask(()=>streamController.abort());return new Promise(()=>{});},
      cancel:async()=>{cancelled=true;}
    })}};
  }});
  await assert.rejects(loadPosteriorImage({...streamed,project:'p',artifact,signal:streamController.signal}),{name:'AbortError'});
  assert.equal(cancelled,true);
});

test('storage fetch keeps the global browser receiver without auth headers',async()=>{
  let called=false;
  const f=fixture({fetcher:async function(url,options){
    assert.equal(this,globalThis);
    assert.equal(url,signed);
    assert.equal(options.headers,undefined);
    assert.equal(options.credentials,'omit');
    called=true;
    return new Response(bytes,{headers:{'Content-Type':'image/png'}});
  }});
  await loadPosteriorImage({...f,project:'p',artifact});
  assert.equal(called,true);
});
