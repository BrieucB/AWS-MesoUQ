export const terminal = new Set(['succeeded','failed','cancelled']);
export function validateRuntime(config, location = globalThis.location) {
  for (const key of ['apiBaseUrl','cognitoDomain','redirectUri','logoutUri']) {
    if (!config[key]) throw new Error('Deployment configuration is incomplete. Ask the workspace maintainer to configure runtime-config.json.');
    const u=new URL(config[key]);
    if(u.protocol!=='https:' && !(u.protocol==='http:' && ['localhost','127.0.0.1'].includes(u.hostname))) throw Error(`${key} must use HTTPS.`);
    if(u.username||u.password||u.search||u.hash) throw Error(`${key} must not contain credentials, queries, or fragments.`);
  }
  if (!config.clientId) throw Error('Cognito client ID is required.');
  if (location && new URL(config.redirectUri).origin !== location.origin) throw Error('The OAuth redirect must use this portal’s origin.');
  return config;
}
export class Api {
  constructor(config,token,fetcher=fetch,{expiresAt=Infinity,onUnauthorized=()=>{},ensureSession=null,renewable=false}={}){
    this.config=config;this.token=token;this.fetcher=fetcher.bind(globalThis);this.expiresAt=expiresAt;
    this.onUnauthorized=onUnauthorized;this.ensureSession=ensureSession;this.renewable=renewable;this.authFailed=false;this.authVersion=0;
  }
  invalidate(){if(!this.authFailed){this.authFailed=true;this.authVersion++;this.token='';this.onUnauthorized();}}
  applySession(session){
    if(this.authFailed)return;
    this.token=session.token;this.expiresAt=session.expiresAt;this.renewable=!!session.renewable;
  }
  async ready(options={}){
    if(this.authFailed)throw Error('Your site session expired. Sign in again.');
    if(this.ensureSession){
      const version=this.authVersion;
      let session;
      try{session=await this.ensureSession(options);}
      catch(error){if(error.authEnded||error.status===401||error.status===403)this.invalidate();throw error;}
      if(this.authFailed||version!==this.authVersion)throw Error('Your site session expired. Sign in again.');
      if(!session){this.invalidate();throw Error('Your site session expired. Sign in again.');}
      this.applySession(session);
    }
    if(this.authFailed||Date.now()>=this.expiresAt){this.invalidate();throw Error('Your site session expired. Sign in again.');}
  }
  async request(path,body,method=body===undefined?'GET':'POST'){
    method=method.toUpperCase();
    await this.ready();
    const version=this.authVersion;
    for(let attempt=0;attempt<2;attempt++){
      if(this.authFailed||version!==this.authVersion)throw Error('Your site session expired. Sign in again.');
      const rejectedToken=this.token;
      const response=await this.fetcher(this.config.apiBaseUrl.replace(/\/$/,'')+path,{method,headers:{Authorization:'Bearer '+this.token,...(body===undefined?{}:{'Content-Type':'application/json'})},...(body===undefined?{}:{body:JSON.stringify(body)}),cache:'no-store',credentials:'omit',redirect:'error'});
      const data=await response.json().catch(()=>null);
      if(this.authFailed||version!==this.authVersion)throw Error('Your site session expired. Sign in again.');
      if(response.status===401){
        if(this.renewable&&this.ensureSession&&attempt===0){
          await this.ready({force:true,rejectedToken});
          if(method==='GET')continue;
          throw Error('Sign-in was renewed. This action was not retried; check its status before trying again.');
        }
        this.invalidate();throw Error('Your site session expired. Sign in again.');
      }
      if(!this.ensureSession&&Date.now()>=this.expiresAt){this.invalidate();throw Error('Your site session expired. Sign in again.');}
      if(!response.ok)throw Error(data?.error?.message||'Request failed ('+response.status+').');
      return data;
    }
  }
  project(id,suffix){return `/v1/projects/${encodeURIComponent(id)}${suffix}`;}
  async upload(project,file,kind,metadata){
    if(kind!=='curve')throw Error('Browser v1 accepts only CSV curve artifacts.');
    if(!Number.isSafeInteger(file.size)||file.size<1||file.size>1024*1024)throw Error('CSV uploads are limited to 1 MiB.');
    const bytes=await file.arrayBuffer();
    if(bytes.byteLength!==file.size)throw Error('The selected curve changed while reading it.');
    const sha256=Array.from(new Uint8Array(await crypto.subtle.digest('SHA-256',bytes)),x=>x.toString(16).padStart(2,'0')).join('');
    const staged=await this.request(this.project(project,'/artifacts'),{name:file.name,kind,sha256,size_bytes:file.size,media_type:file.type||'text/csv',metadata});
    this.validateStorageUrl(staged.upload_url);
    const headers={...(staged.required_headers||{})};
    for(const key of Object.keys(headers))if(key.toLowerCase()==='content-length'){
      if(headers[key]!==String(bytes.byteLength))throw Error('Storage upload length does not match the selected curve.');
      // Fetch sets this forbidden request header from the exact ArrayBuffer body.
      delete headers[key];
    }
    const response=await this.fetcher(staged.upload_url,{method:'PUT',headers,body:bytes,credentials:'omit',redirect:'error'});
    if(!response.ok)throw Error(`Artifact upload failed (${response.status}).`);
    const completed=await this.request(this.project(project,`/artifacts/${encodeURIComponent(staged.artifact.id)}/complete`),{});
    return completed.artifact||completed;
  }
  validateStorageUrl(value){const u=new URL(value);if(u.protocol!=='https:'||!(this.config.uploadOrigins||[]).includes(u.origin))throw Error('Storage URL is outside the configured private artifact origins.');return u;}
}
export function parseCurve(text,order){
  if(!['force-displacement','displacement-force'].includes(order))throw Error('Choose an explicit canonical CSV column order.');
  const lines=text.replace(/^\uFEFF/,'').trim().split(/\r?\n/).filter(x=>x.trim());
  if(lines.length<2)throw Error('A curve needs at least two data rows.');
  const rows=lines.map(x=>x.split(',').map(y=>y.trim()));
  const first=rows[0];
  const header=first.length===2&&first.every(x=>/^[A-Za-z]/.test(x)&&!Number.isFinite(Number(x))&&!/^(nan|[-+]?inf(?:inity)?)$/i.test(x));
  if(header)rows.shift();
  if(rows.length>1000)throw Error('Release 1 accepts at most 1,000 curve points.');
  const decimal=/^[+-]?(?:\d+\.?\d*|\.\d+)(?:[eE][+-]?\d+)?$/;
  if(rows.length<2||rows.some(r=>r.length!==2||r.some(x=>!decimal.test(x)||!Number.isFinite(Number(x)))))throw Error('Use an unquoted, two-column numeric CSV with an optional header; all values must be finite.');
  return rows.map(r=>({force:Number(r[order==='force-displacement'?0:1]),displacement:Number(r[order==='force-displacement'?1:0])}));
}
export function createJob({kind,model,diameter,configuration,artifacts,key}){
  if(kind!=='calibration')throw Error('Browser v1 supports only individual calibration.');
  if(!model)throw Error('Select an immutable model version.');
  if(![3.2,3.4,5.8].includes(Number(diameter)))throw Error('Select a supported nominal diameter.');
  if(artifacts.length!==1)throw Error('Upload and verify exactly one curve before submitting.');
  return {schema_version:1,kind,model_version:model,diameter_um:Number(diameter),configuration,input_artifacts:artifacts,idempotency_key:key};
}
const base64url=bytes=>btoa(String.fromCharCode(...bytes)).replace(/\+/g,'-').replace(/\//g,'_').replace(/=+$/,'');
export async function beginLogin(config,storage=sessionStorage,go=url=>location.assign(url)){
  const verifier=base64url(crypto.getRandomValues(new Uint8Array(48))),state=base64url(crypto.getRandomValues(new Uint8Array(24)));
  storage.setItem('mesouq.oauth',JSON.stringify({verifier,state,created:Date.now()}));
  const challenge=base64url(new Uint8Array(await crypto.subtle.digest('SHA-256',new TextEncoder().encode(verifier))));
  const url=new URL('/oauth2/authorize',config.cognitoDomain);url.search=new URLSearchParams({response_type:'code',client_id:config.clientId,redirect_uri:config.redirectUri,scope:(config.scopes||['openid','email']).join(' '),state,code_challenge:challenge,code_challenge_method:'S256'});go(url.href);
}
export async function finishLogin(config,params,storage=sessionStorage,fetcher=fetch){
  const saved=storage.getItem('mesouq.oauth');storage.removeItem('mesouq.oauth');
  if(!saved)throw Error('The sign-in session is missing. Sign in again.');
  const pending=JSON.parse(saved);
  if(params.get('state')!==pending.state||Date.now()-pending.created>600000)throw Error('Sign-in state is invalid or expired.');
  if(params.has('error'))throw Error('Sign-in was not completed. Please try again.');
  if(!params.get('code'))throw Error('Missing authorization code.');
  const response=await fetcher(new URL('/oauth2/token',config.cognitoDomain),{method:'POST',headers:{'Content-Type':'application/x-www-form-urlencoded'},body:new URLSearchParams({grant_type:'authorization_code',client_id:config.clientId,redirect_uri:config.redirectUri,code:params.get('code'),code_verifier:pending.verifier}),credentials:'omit',redirect:'error',cache:'no-store'});
  const data=await response.json();if(!response.ok||!data.access_token||!Number.isFinite(data.expires_in))throw Error('The identity service could not complete sign-in.');
  return {token:data.access_token,expiresAt:Date.now()+data.expires_in*1000};
}

export function formatTimestamp(value){
  if(value===null||value===undefined||value==='')return 'Not reported';
  const timestamp=new Date(typeof value==='number'?value*1000:value);
  return Number.isFinite(timestamp.getTime())?timestamp.toLocaleString():'Not reported';
}

// Protected-portal session bootstrap. Only the regional /auth/* service handles
// OAuth callbacks/cookies; the research application receives a memory-only token.
function siteSession(data,now){
  const allowed=data?.renewable===true?['access_token','expires_at','renewable','absolute_expires_at','idle_expires_at','generation']:['access_token','expires_at'];
  if(!data||Object.keys(data).some(key=>!allowed.includes(key)))throw Error('Invalid site session response fields.');
  if(typeof data?.access_token!=='string'||!data.access_token||!Number.isFinite(data.expires_at))throw Error('Invalid site session response.');
  const expiresAt=data.expires_at*1000;
  if(!Number.isFinite(expiresAt))throw Error('Invalid site session expiry.');
  if(expiresAt<=now)return null;
  const session={token:data.access_token,expiresAt};
  if(data.renewable!==undefined){
    if(data.renewable!==true||!Number.isSafeInteger(data.generation)||data.generation<0
       ||!Number.isFinite(data.absolute_expires_at)||!Number.isFinite(data.idle_expires_at))throw Error('Invalid remembered site session response.');
    const absoluteExpiresAt=data.absolute_expires_at*1000,idleExpiresAt=data.idle_expires_at*1000;
    if(!Number.isFinite(absoluteExpiresAt)||!Number.isFinite(idleExpiresAt)||idleExpiresAt>absoluteExpiresAt
       ||expiresAt>Math.min(absoluteExpiresAt,idleExpiresAt))throw Error('Invalid remembered site session limits.');
    Object.assign(session,{renewable:true,absoluteExpiresAt,idleExpiresAt,generation:data.generation});
  }
  return session;
}
function siteSessionError(response,data){
  const error=Error(data?.error?.message||'Site session request failed ('+response.status+').');
  error.status=response.status;error.code=data?.error?.code;
  const retry=Number(response.headers?.get?.('Retry-After'));
  error.retryAfterMs=Number.isFinite(retry)&&retry>0?Math.min(5000,Math.max(1000,retry*1000)):1000;
  return error;
}
export async function readSiteSession(fetcher=fetch,now=Date.now()){
  const response=await fetcher('/auth/session',{method:'GET',headers:{Accept:'application/json'},credentials:'include',cache:'no-store',redirect:'error'});
  const data=await response.json().catch(()=>null);
  if(response.status===401)return null;
  if(!response.ok)throw siteSessionError(response,data);
  return siteSession(data,now);
}
export async function refreshSiteSession(activity=false,fetcher=fetch,now=Date.now()){
  if(typeof activity!=='boolean')throw Error('Session activity must be explicit.');
  const response=await fetcher('/auth/refresh',{method:'POST',headers:{Accept:'application/json','Content-Type':'application/json'},body:JSON.stringify({activity}),credentials:'include',cache:'no-store',redirect:'error'});
  const data=await response.json().catch(()=>null);
  if(response.status===401)return null;
  if(!response.ok)throw siteSessionError(response,data);
  return siteSession(data,now);
}
export async function endSiteSession(config,fetcher=fetch,origin=globalThis.location.origin){
  const response=await fetcher('/auth/logout',{method:'POST',headers:{Accept:'application/json'},credentials:'include',cache:'no-store',redirect:'error'});
  const data=await response.json().catch(()=>null);
  if(!response.ok)throw Error(data?.error?.message||`Site sign-out failed (${response.status}).`);
  if(typeof data?.logout_url!=='string')throw Error('The sign-out response has no redirect.');
  const url=new URL(data.logout_url),domain=new URL(config.cognitoDomain);
  const target=new URL(url.searchParams.get('logout_uri')||'',origin);
  if([...url.searchParams.keys()].sort().join(',')!=='client_id,logout_uri'||url.searchParams.get('client_id')!==config.clientId||url.origin!==domain.origin||url.pathname!=='/logout'||url.username||url.password||url.hash||target.origin!==origin||target.pathname!=='/auth/login'||target.search||target.hash)throw Error('The sign-out redirect is outside the configured identity flow.');
  return url.href;
}
