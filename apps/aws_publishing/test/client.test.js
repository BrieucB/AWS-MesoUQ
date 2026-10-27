import test from 'node:test';
import assert from 'node:assert/strict';
import {Api,parseCurve,createJob,validateRuntime,beginLogin,finishLogin,formatTimestamp} from '../src/client.js';
import {curvePlot,intervalPlot,escapeHtml} from '../src/plots.js';
const config={apiBaseUrl:'https://api.example.org',cognitoDomain:'https://identity.example.org',clientId:'public-client',redirectUri:'https://research.example.org/',logoutUri:'https://research.example.org/',uploadOrigins:['https://private-bucket.example.org']};
const response=(data,status=200)=>({ok:status>=200&&status<300,status,json:async()=>data});
const memory=()=>{const data=new Map();return {setItem:(k,v)=>data.set(k,v),getItem:k=>data.get(k),removeItem:k=>data.delete(k)};};
test('runtime config requires HTTPS, same-origin callback and public client config',()=>{
  assert.equal(validateRuntime(config,{origin:'https://research.example.org'}),config);
  assert.throws(()=>validateRuntime({...config,apiBaseUrl:'http://public.example.org'}),/HTTPS/);
  assert.throws(()=>validateRuntime(config,{origin:'https://other.example.org'}),/origin/);
  assert.throws(()=>validateRuntime({...config,clientId:''}),/client ID/);
  assert.throws(()=>validateRuntime({...config,apiBaseUrl:'https://user:password@api.example.org'}),/credentials/);
});
test('CSV interprets explicit canonical order, BOM/header, and validates every numeric sample',()=>{
  assert.deepEqual(parseCurve('\uFEFFd,f\r\n1,2\r\n3,4','displacement-force'),[{force:2,displacement:1},{force:4,displacement:3}]);
  assert.deepEqual(parseCurve('1,2\n3,4','force-displacement'),[{force:1,displacement:2},{force:3,displacement:4}]);
  for(const input of ['NaN,2\n1,2\n3,4','NaN,Infinity\n1,2\n3,4','1,2','f,d\n1,2\nNaN,3','f,d\n1,2\n,3','f,d\n1,2\n1,2,3'])assert.throws(()=>parseCurve(input,'force-displacement'));
});
test('job request has canonical fields, no client identity and stable retry key',()=>{
  const input={kind:'calibration',model:'immutable-v1',diameter:'3.2',configuration:{seed:3},artifacts:[{id:'curve-1'}],key:'retry-key'};
  const job=createJob(input);assert.deepEqual(job,{schema_version:1,kind:'calibration',model_version:'immutable-v1',diameter_um:3.2,configuration:{seed:3},input_artifacts:[{id:'curve-1'}],idempotency_key:'retry-key'});
  for(const kind of ['prediction','training'])assert.throws(()=>createJob({...input,kind}),/only individual calibration/);
  assert.throws(()=>createJob({...input,artifacts:[{id:'a'},{id:'b'}]}),/exactly one/);
  assert.deepEqual(createJob(input),job);assert.throws(()=>createJob({...input,artifacts:[]}),/Upload/);assert.throws(()=>createJob({...input,diameter:4}),/diameter/);
});
test('CSV density is bounded before upload and uses scientific decimal numbers',()=>{
  assert.equal(parseCurve('force,d\n'+Array(1000).fill('1e-3,+.2').join('\n'),'force-displacement').length,1000);
  assert.throws(()=>parseCurve('force,d\n'+Array(1001).fill('1,2').join('\n'),'force-displacement'),/1,000/);
  for(const value of ['0xff','0b10','0o17'])assert.throws(()=>parseCurve(`${value},2\n3,4`,'force-displacement'),/numeric CSV/);
});
test('API sends bearer only to API with private cache, encoded IDs, and safe server errors',async()=>{
  let sent;const api=new Api(config,'private-token',async(...args)=>{sent=args;return response({error:{code:'denied',message:'Membership required.'}},403);});
  assert.equal(api.project('a/b','/jobs'),'/v1/projects/a%2Fb/jobs');await assert.rejects(api.request('/v1/me'),/Membership required/);
  assert.equal(sent[1].headers.Authorization,'Bearer private-token');assert.equal(sent[1].credentials,'omit');assert.equal(sent[1].redirect,'error');assert.equal(sent[1].cache,'no-store');
});
test('browser fetch keeps its global receiver for API requests and storage uploads',async(t)=>{
  const calls=[];
  function browserFetch(url,opts){
    if(this!==globalThis)throw new TypeError('Illegal invocation');
    calls.push({url,opts});
    if(url.endsWith('/v1/me'))return Promise.resolve(response({sub:'invited-user'}));
    if(url.endsWith('/artifacts'))return Promise.resolve(response({artifact:{id:'curve-1'},upload_url:'https://private-bucket.example.org/upload'}));
    if(url.endsWith('/complete'))return Promise.resolve(response({artifact:{id:'curve-1',verified:true}}));
    assert.equal(url,'https://private-bucket.example.org/upload');
    assert.equal(opts.method,'PUT');
    assert.equal(opts.headers.Authorization,undefined);
    return Promise.resolve(response({}));
  }
  t.mock.method(globalThis,'fetch',browserFetch);
  for(const injected of [false,true]){
    const api=injected?new Api(config,'private-token',browserFetch):new Api(config,'private-token');
    assert.deepEqual(await api.request('/v1/me'),{sub:'invited-user'});
    assert.deepEqual(await api.upload('p',new File(['1,2\n3,4'],'curve.csv',{type:'text/csv'}),'curve',{}),{id:'curve-1',verified:true});
  }
  assert.equal(calls.length,8);
  assert.equal(calls.filter(({opts})=>opts.method==='PUT').length,2);
});
test('artifact lifecycle hashes original bytes, preserves explicit metadata, PUT has no token, complete unwraps',async()=>{
  for(const wrapped of [true,false]){
    const calls=[];const file=new File(['force,d\n1,2\n3,4'],'curve.csv',{type:'text/csv'});
    const artifact={id:'artifact-1',name:'curve.csv',kind:'curve',metadata:{force_unit:'uN',displacement_unit:'um',column_order:'displacement-force',nominal_diameter_um:3.2,measured_diameter_um:3.19}};
    const api=new Api(config,'secret',async(url,opts)=>{calls.push({url,opts});if(calls.length===1)return response({artifact:{id:artifact.id},upload_url:'https://private-bucket.example.org/upload?signature=private',required_headers:{'Content-Type':'text/csv'}});if(calls.length===2)return response({});return response(wrapped?{artifact}:artifact);});
    const result=await api.upload('p',file,'curve',artifact.metadata);assert.deepEqual(result,artifact);assert.equal(calls.length,3);
    const metadata=JSON.parse(calls[0].opts.body);assert.match(metadata.sha256,/^[a-f0-9]{64}$/);assert.equal(metadata.size_bytes,file.size);assert.deepEqual(metadata.metadata,artifact.metadata);
    assert.equal(calls[1].opts.method,'PUT');assert.equal(calls[1].opts.headers.Authorization,undefined);assert.equal(calls[1].opts.credentials,'omit');assert.match(calls[2].url,/artifact-1\/complete$/);
  }
});
test('failed storage upload never completes; hostile presigned origins refused',async()=>{
  let count=0;const api=new Api(config,'secret',async()=>{count++;return count===1?response({artifact:{id:'a'},upload_url:'https://private-bucket.example.org/x'}):response({},500);});
  await assert.rejects(api.upload('p',new File(['a'],'x.csv'),'curve',{}),/upload failed/);assert.equal(count,2);
  assert.throws(()=>api.validateStorageUrl('https://attacker.example.org/x'),/outside/);assert.throws(()=>api.validateStorageUrl('http://private-bucket.example.org/x'),/outside/);
});
test('signed upload length is checked and left to Fetch; oversized curves never reserve',async()=>{
  const file=new File(['1,2\n3,4'],'curve.csv',{type:'text/csv'});
  let calls=[];
  const api=new Api(config,'secret',async(url,opts)=>{
    calls.push({url,opts});
    if(calls.length===1)return response({artifact:{id:'a'},upload_url:'https://private-bucket.example.org/x',required_headers:{'Content-Length':String(file.size),'Content-Type':'text/csv','If-None-Match':'*'}});
    return response({});
  });
  await api.upload('p',file,'curve',{});
  assert.equal(calls[1].opts.headers['Content-Length'],undefined);
  assert.equal(calls[1].opts.body.byteLength,file.size);
  assert.equal(calls[1].opts.headers['If-None-Match'],'*');
  calls=[];
  await assert.rejects(api.upload('p',new File([new Uint8Array(1024*1024+1)],'large.csv'),'curve',{}),/1 MiB/);
  assert.equal(calls.length,0);
  const wrong=new Api(config,'secret',async()=>response({artifact:{id:'a'},upload_url:'https://private-bucket.example.org/x',required_headers:{'content-length':'999'}}));
  await assert.rejects(wrong.upload('p',file,'curve',{}),/length does not match/);
});
test('result response remains normalized with server unit/provenance/diagnostic fields',async()=>{
  const result={schema_version:1,kind:'calibration',interval_probability:0.9,parameters:[],curves:[{dataset_id:'x',force:[1,2],observed_displacement:[1,2],median:[1,2],lower:[0,1],upper:[2,3],force_unit:'nN',displacement_unit:'nm'}],resonance:{status:'unavailable',reason:'Unsupported samples'},provenance:{model_sha256:'abc'},diagnostics:{}};
  const api=new Api(config,'t',async()=>response(result));const actual=await api.request('/v1/projects/p/jobs/j/result');assert.deepEqual(actual,result);assert.match(curvePlot(actual.curves[0]),/nN/);assert.match(curvePlot(actual.curves[0]),/nm/);
});
test('OAuth S256 PKCE verifies state once before authorization code exchange',async()=>{
  const storage=memory();let redirect;await beginLogin(config,storage,url=>redirect=new URL(url));
  assert.equal(redirect.searchParams.get('code_challenge_method'),'S256');assert.equal(redirect.searchParams.get('response_type'),'code');
  const pending=JSON.parse(storage.getItem('mesouq.oauth'));assert.equal(pending.state,redirect.searchParams.get('state'));assert.ok(pending.verifier.length>=43);
  let sent;const result=await finishLogin(config,new URLSearchParams({code:'one-use-code',state:pending.state}),storage,async(url,opts)=>{sent={url,opts};return response({access_token:'memory-only-token',expires_in:3600});});
  assert.equal(result.token,'memory-only-token');assert.equal(storage.getItem('mesouq.oauth'),undefined);assert.equal(sent.opts.body.get('code_verifier'),pending.verifier);assert.equal(sent.opts.body.get('client_secret'),null);assert.equal(sent.opts.redirect,'error');
});
test('OAuth rejects mismatched or expired state without exchange',async()=>{
  for(const pending of [{state:'correct',verifier:'v',created:Date.now()},{state:'wrong',verifier:'v',created:0}]){const storage=memory();storage.setItem('mesouq.oauth',JSON.stringify(pending));let called=false;await assert.rejects(finishLogin(config,new URLSearchParams({code:'c',state:'wrong'}),storage,async()=>{called=true;}),/invalid or expired/);assert.equal(called,false);}
});
test('curve component renders accessible observations and uncertainty without HTML injection',()=>{
  const svg=curvePlot({dataset_id:'<script>alert(1)</script>',force:[1,2],observed_displacement:[3,4],median:[3.1,4.1],lower:[2,3],upper:[4,5],force_unit:'uN',displacement_unit:'um'});
  assert.match(svg,/role="img"/);assert.match(svg,/<polygon/);assert.match(svg,/<polyline/);assert.equal((svg.match(/<circle/g)||[]).length,2);assert.ok(!svg.includes('<script>'));assert.match(svg,/uN/);
  assert.match(curvePlot({force:[1,2],median:[1]}),/mismatched/);assert.match(curvePlot({force:[1,NaN],median:[2,3]}),/invalid/);
});
test('interval component never fabricates unavailable data and escapes labels',()=>{
  assert.match(intervalPlot([{name:'ka',unit:'Pa',lower:1,median:2,upper:3}]),/lower 1, median 2, upper 3/);
  assert.match(intervalPlot([{name:'ka',lower:3,median:2,upper:1}]),/unavailable/);
  assert.equal(escapeHtml('<img onerror="x">'),'&lt;img onerror=&quot;x&quot;&gt;');
});

test('large curve display is bounded without modifying source samples',()=>{const force=Array.from({length:150000},(_,i)=>i);const median=force.map(v=>v/2);const svg=curvePlot({force,median});assert.match(svg,/sampled to 1,000/);assert.equal(force.length,150000);assert.ok(svg.length<100000);});

test('timestamps accept real backend epoch seconds and ISO without 1970 dates',()=>{
  const epoch=1767225600;
  assert.equal(formatTimestamp(epoch),new Date('2026-01-01T00:00:00Z').toLocaleString());
  assert.equal(formatTimestamp('2026-01-01T00:00:00Z'),formatTimestamp(epoch));
  assert.equal(formatTimestamp(null),'Not reported');
  assert.equal(formatTimestamp('not-a-time'),'Not reported');
});
