// Only reviewed scientific figure names may be displayed inline.
const TITLES={
  'posterior-marginals.png':'Posterior marginals against the numerical reference',
  'compute-comparison.png':'Measured method cost and comparison',
  'metrics-posterior-marginals.png':'Posterior marginals against the numerical reference',
  'metrics-compute-comparison.png':'Measured method cost and comparison',
};
const LIMIT=10*1024*1024, MAGIC=[137,80,78,71,13,10,26,10];
const keys=['id','name','sha256','size_bytes','media_type'];
function checked(value){
  if(!value||!Object.hasOwn(TITLES,value.name)||value.media_type!=='image/png'||typeof value.id!=='string'||!value.id.length||value.id.length>256||/[\u0000-\u001f\u007f]/.test(value.id)||typeof value.sha256!=='string'||!/^[a-f0-9]{64}$/.test(value.sha256)||!Number.isSafeInteger(value.size_bytes)||value.size_bytes<8||value.size_bytes>LIMIT)throw Error('Invalid benchmark figure reference.');
  return Object.freeze(Object.fromEntries(keys.map(key=>[key,value[key]])));
}
export function benchmarkFigureArtifacts(result){
  if(result?.artifacts===undefined)return [];
  if(!Array.isArray(result.artifacts))throw Error('Invalid benchmark figure references.');
  const seen=new Set(),ids=new Set();
  return result.artifacts.filter(value=>Object.hasOwn(TITLES,value?.name)).map(value=>{
    const artifact=checked(value),title=TITLES[artifact.name];
    if(seen.has(title)||ids.has(artifact.id))throw Error('Ambiguous benchmark figure references.');
    seen.add(title);ids.add(artifact.id);return {artifact,title};
  });
}
function abort(signal){if(signal?.aborted)throw new DOMException('Figure loading cancelled.','AbortError');}
async function cancellable(promise,signal){
  abort(signal);if(!signal)return promise;
  let listener;
  const cancelled=new Promise((_,reject)=>{listener=()=>reject(new DOMException('Figure loading cancelled.','AbortError'));signal.addEventListener('abort',listener,{once:true});});
  try{return await Promise.race([promise,cancelled]);}finally{signal.removeEventListener('abort',listener);}
}
export async function loadBenchmarkFigure({api,project,campaign,artifact,fetcher=fetch,signal}){
  let reader;
  try{
    abort(signal);const trusted=checked(artifact);
    const metadata=await cancellable(api.request(api.project(project,'/benchmarks/'+encodeURIComponent(campaign)+'/artifacts/'+encodeURIComponent(trusted.id))),signal);
    const returned=checked(metadata?.artifact);
    if(keys.some(key=>trusted[key]!==returned[key]))throw Error();
    api.validateStorageUrl(metadata.download_url);abort(signal);
    const response=await cancellable(fetcher.call(globalThis,metadata.download_url,{method:'GET',credentials:'omit',redirect:'error',cache:'no-store',signal}),signal);
    reader=response.body?.getReader?.();
    if(!response.ok||response.headers.get('content-type')!=='image/png'||!reader)throw Error();
    const length=response.headers.get('content-length');
    if(length!==null&&(!/^[0-9]+$/.test(length)||Number(length)!==trusted.size_bytes))throw Error();
    const bytes=new Uint8Array(trusted.size_bytes);let offset=0;
    for(;;){const {done,value}=await cancellable(reader.read(),signal);if(done)break;if(!(value instanceof Uint8Array)||value.length>bytes.length-offset)throw Error();bytes.set(value,offset);offset+=value.length;}
    if(offset!==bytes.length||MAGIC.some((value,index)=>bytes[index]!==value))throw Error();
    const digest=Array.from(new Uint8Array(await cancellable(crypto.subtle.digest('SHA-256',bytes),signal)),value=>value.toString(16).padStart(2,'0')).join('');
    if(digest!==trusted.sha256)throw Error();abort(signal);
    const chunks=[];for(let i=0;i<bytes.length;i+=32768)chunks.push(String.fromCharCode(...bytes.subarray(i,i+32768)));
    return 'data:image/png;base64,'+btoa(chunks.join(''));
  }catch(error){
    if(signal?.aborted)throw new DOMException('Figure loading cancelled.','AbortError');
    throw Error('The benchmark figure could not be loaded or verified.');
  }finally{if(reader)try{await reader.cancel();}catch{}}
}
