import test from 'node:test';
import assert from 'node:assert/strict';
import {createHash} from 'node:crypto';
import {benchmarkFigureArtifacts,loadBenchmarkFigure} from '../src/benchmark-figures.js';
import {BenchmarkLab,benchmarkResults} from '../src/benchmark-lab.js';
const bytes=new Uint8Array(Buffer.from('iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+a0V8AAAAASUVORK5CYII=','base64'));
const hash=value=>createHash('sha256').update(value).digest('hex');
const artifact={id:'figure-1',name:'metrics-posterior-marginals.png',media_type:'image/png',sha256:hash(bytes),size_bytes:bytes.length};
function fixture({metadata=artifact,body=bytes,mime='image/png',headers={},fetcher}={}){
 const calls=[];
 const api={project:(project,path)=>'/v1/projects/'+project+path,request:async path=>{calls.push(['api',path]);return {artifact:metadata,download_url:'https://storage.example.test/private?signature=synthetic-secret'};},validateStorageUrl:url=>{calls.push(['validate']);assert.equal(new URL(url).origin,'https://storage.example.test');}};
 return {api,calls,fetcher:fetcher||async function(url,options){calls.push(['storage',options]);assert.equal(this,globalThis);return new Response(body,{headers:{'Content-Type':mime,...headers}});}};
}
test('only explicit reviewed PNG figure names are selected, with alias ambiguity rejected',()=>{
 const entries=benchmarkFigureArtifacts({artifacts:[artifact,{...artifact,id:'figure-2',name:'metrics-compute-comparison.png'}]});
 assert.equal(entries.length,2);
 assert.match(entries[0].title,/numerical reference/);
 for(const name of ['arbitrary.png','posterior.svg','private/path/posterior-marginals.png'])assert.deepEqual(benchmarkFigureArtifacts({artifacts:[{...artifact,name}]}),[]);
 assert.throws(()=>benchmarkFigureArtifacts({artifacts:[artifact,{...artifact,id:'alias',name:'posterior-marginals.png'}]}),/Ambiguous/);
 assert.throws(()=>benchmarkFigureArtifacts({artifacts:[{...artifact,size_bytes:10*1024*1024+1}]}),/Invalid/);
 assert.throws(()=>benchmarkFigureArtifacts({artifacts:[{...artifact,media_type:'image/svg+xml'}]}),/Invalid/);
});
test('verified figure uses owner-scoped metadata and credential-free origin-validated fetch',async()=>{
 const f=fixture();
 const src=await loadBenchmarkFigure({...f,project:'p',campaign:'c /',artifact});
 assert.equal(src,'data:image/png;base64,'+Buffer.from(bytes).toString('base64'));
 assert.deepEqual(f.calls.map(c=>c[0]),['api','validate','storage']);
 assert.equal(f.calls[0][1],'/v1/projects/p/benchmarks/c%20%2F/artifacts/figure-1');
 assert.equal(f.calls[2][1].credentials,'omit');
 assert.equal(f.calls[2][1].redirect,'error');
 assert.equal(f.calls[2][1].cache,'no-store');
 assert.equal(f.calls[2][1].headers,undefined);
});
test('figure metadata, MIME, size, signature and checksum mismatches fail privately',async()=>{
 for(const patch of [{id:'other'},{name:'metrics-compute-comparison.png'},{size_bytes:bytes.length+1},{sha256:'0'.repeat(64)}]){
  const f=fixture({metadata:{...artifact,...patch}});
  await assert.rejects(loadBenchmarkFigure({...f,project:'p',campaign:'c',artifact}),/could not be loaded or verified/);
  assert.equal(f.calls.some(call=>call[0]==='storage'),false);
 }
 const modified=bytes.slice();modified[20]^=1;
 for(const options of [{mime:'image/svg+xml'},{mime:'image/png; charset=utf-8'},{body:bytes.slice(0,-1)},{body:new Uint8Array(bytes.length+1)},{body:new Uint8Array(bytes.length)},{body:modified},{headers:{'Content-Length':'999999999'}}]){
  await assert.rejects(loadBenchmarkFigure({...fixture(options),project:'p',campaign:'c',artifact}),/could not be loaded or verified/);
 }
 const f=fixture();f.api.validateStorageUrl=()=>{throw Error('https://private.example.test/?signature=secret');};
 await assert.rejects(loadBenchmarkFigure({...f,project:'p',campaign:'c',artifact}),error=>!error.message.includes('https:')&&!error.message.includes('secret'));
 assert.equal(f.calls.some(c=>c[0]==='storage'),false);
});
test('figure loads can abort before work and while metadata is pending',async()=>{
 const before=new AbortController();before.abort();
 const f=fixture();
 await assert.rejects(loadBenchmarkFigure({...f,project:'p',campaign:'c',artifact,signal:before.signal}),{name:'AbortError'});
 assert.equal(f.calls.length,0);
 f.api.request=()=>new Promise(()=>{});
 const controller=new AbortController();
 const pending=loadBenchmarkFigure({...f,project:'p',campaign:'c',artifact,signal:controller.signal});
 controller.abort();await assert.rejects(pending,{name:'AbortError'});
});
test('figures remain independent of operation busy state and clear on navigation',async()=>{
 let release;
 const f=fixture();
 const result={plots:[],artifacts:[artifact]};
 f.api.request=async path=>path.endsWith('/results')?{result}:new Promise(resolve=>release=resolve);
 const lab=new BenchmarkLab({context:()=>({api:f.api,project:'p',valid:()=>true}),onChange:()=>{},download:()=>{},fetcher:f.fetcher});
 lab.sync();lab.active=true;lab.detail={id:'c',status:'completed'};
 await lab.loadResults();
 assert.equal(lab.busy,false);
 assert.equal(lab.figures[0].status,'loading');
 assert.equal(lab.figures[0].src,'');
 lab.leave();
 release({artifact,download_url:'https://storage.example.test/private'});
 await new Promise(resolve=>setTimeout(resolve,0));
 assert.deepEqual(lab.figures,[]);
 assert.equal(f.calls.some(c=>c[0]==='storage'),false);
});
test('actual nested scientific counts preserve missing scores, histories and limitations',()=>{
 const markup=benchmarkResults({metrics:{hnpe:{counts:{total:4,scored:0,method_failed:1,reference_unresolved:2,budget_exhausted:1},mean_score_on_scored_cases:null,median_score_on_scored_cases:null}},plots:[],training_histories:[{factor:'global',history:[{update:1,validation_nll:4},{update:2,validation_nll:3}]}],limitations:['Budget-limited candidate, not converged.']});
 assert.match(markup,/Unresolved references/);
 assert.match(markup,/Mean score on scored cases only<\/td><td>Not reported/);
 assert.match(markup,/Budget-limited candidate, not converged/);
 assert.match(markup,/global validation history/);
 assert.match(markup,/Missing scores are unreported, not zero/);
});
