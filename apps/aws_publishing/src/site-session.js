import {readSiteSession,refreshSiteSession} from './client.js';

const endedError=()=>Object.assign(Error('Your site session expired. Sign in again.'),{authEnded:true});
const defaultChannel=()=>{
  const Channel=globalThis.window?.BroadcastChannel;
  try{return typeof Channel==='function'?new Channel('mesouq-site-session'):null;}catch{return null;}
};

// Credentials stay in the BFF. This object owns only the current short-lived
// access token in memory and nonsecret expiry/version metadata.
export class SiteSession {
  constructor({fetcher=globalThis.fetch,now=()=>Date.now(),setTimer=(fn,ms)=>globalThis.setTimeout(fn,ms),
    clearTimer=id=>globalThis.clearTimeout(id),document=globalThis.document,window=globalThis.window,
    channelFactory=defaultChannel,onSession=()=>{},onEnd=()=>{},onTransient=()=>{}}={}){
    Object.assign(this,{fetcher,now,setTimer,clearTimer,document,window,onSession,onEnd,onTransient});
    this.fetcher=fetcher.bind(globalThis);
    this.session=null;this.epoch=0;this.closed=false;this.flight=null;this.controller=null;
    this.refreshTimer=null;this.activityTimer=null;this.pendingActivity=null;this.activitySequence=0;
    this.lastActivitySentAt=-Infinity;this.retryCount=0;this.nextRetryAt=0;this.lastError=null;
    this.channel=channelFactory?.()||null;
    this.onChannel=event=>{if(event.data?.type==='logout')this.end({reason:'signed_out'});};
    this.channel?.addEventListener?.('message',this.onChannel);
    if(this.channel&&!this.channel.addEventListener)this.channel.onmessage=this.onChannel;
    this.onActivity=event=>this.noteActivity(event);
    this.onResume=()=>{if(!this.document?.hidden&&!this.closed)void this.ensure().catch(()=>{});};
    this.onVisibility=()=>{if(!this.document?.hidden)this.onResume();};
    for(const name of ['pointerdown','keydown','input','change','wheel'])this.document?.addEventListener?.(name,this.onActivity,{capture:true,passive:true});
    this.document?.addEventListener?.('visibilitychange',this.onVisibility);
    this.window?.addEventListener?.('pageshow',this.onResume);
    this.window?.addEventListener?.('focus',this.onResume);
  }
  isActive(){
    if(this.closed||!this.session)return false;
    const limit=this.session.renewable?Math.min(this.session.absoluteExpiresAt,this.session.idleExpiresAt):this.session.expiresAt;
    return this.now()<limit;
  }
  foreground(){return !this.document?.hidden&&this.document?.hasFocus?.()!==false;}
  check(epoch=this.epoch){
    if(this.closed||epoch!==this.epoch)throw endedError();
    if(this.session&&!this.isActive()){this.end({reason:'expired'});throw endedError();}
  }
  accept(session,epoch){
    this.check(epoch);
    if(!session||session.expiresAt<=this.now())throw endedError();
    if(this.session?.renewable&&session.renewable&&session.generation<this.session.generation){
      if(this.session.expiresAt>this.now())return this.session;
      throw Object.assign(Error('A newer browser session is already active. Retry the request.'),{status:409,retryAfterMs:1000});
    }
    this.session=session;this.retryCount=0;this.nextRetryAt=0;this.lastError=null;
    this.onSession(session);this.schedule();
    return session;
  }
  run(work){
    if(this.flight)return this.flight;
    this.check();
    const epoch=this.epoch,controller=new AbortController();
    this.controller=controller;
    const fetcher=(input,options={})=>this.fetcher(input,{...options,signal:controller.signal});
    const operation=(async()=>{
      try{return await work(epoch,fetcher,controller.signal);}
      catch(error){
        if(this.closed||epoch!==this.epoch)throw endedError();
        if(error.status===403){this.end({reason:'forbidden',error});throw error;}
        if(!error.authEnded){this.lastError=error;this.onTransient(error);this.scheduleRetry(error);}
        throw error;
      }
    })();
    this.flight=operation;
    const done=()=>{
      if(this.flight===operation){this.flight=null;this.controller=null;}
      if(!this.closed)this.queueActivity();
    };
    operation.then(done,done);
    return operation;
  }
  async wait(ms,signal,epoch){
    this.check(epoch);
    await new Promise((resolve,reject)=>{
      const abort=()=>{this.clearTimer(timer);reject(endedError());};
      const timer=this.setTimer(()=>{signal.removeEventListener('abort',abort);resolve();},ms);
      signal.addEventListener('abort',abort,{once:true});
      if(signal.aborted)abort();
    });
    this.check(epoch);
  }
  async requestRefresh(activity,fetcher,signal,epoch){
    for(let attempt=0;attempt<2;attempt++){
      try{return await refreshSiteSession(activity,fetcher,this.now());}
      catch(error){
        if(attempt!==0||error.status!==409||error.code!=='refresh_in_progress')throw error;
        await this.wait(error.retryAfterMs,signal,epoch);
      }
    }
  }
  bootstrap(){
    if(this.session)return this.ensure();
    return this.run(async(epoch,fetcher,signal)=>{
      let session=await readSiteSession(fetcher,this.now());
      this.check(epoch);
      if(!session)session=await this.requestRefresh(false,fetcher,signal,epoch);
      this.check(epoch);
      if(!session){this.end({reason:'expired'});return null;}
      return this.accept(session,epoch);
    });
  }
  async ensure({force=false,rejectedToken=null}={}){
    this.check();
    if(this.flight)return this.flight;
    if(!this.session)return this.bootstrap();
    if(rejectedToken!==null&&this.session.token!==rejectedToken&&this.session.expiresAt>this.now())return Promise.resolve(this.session);
    if(!this.session.renewable)return Promise.resolve(this.session);
    if(!force&&this.session.expiresAt-this.now()>60000)return Promise.resolve(this.session);
    if(this.now()<this.nextRetryAt){
      if(!force&&this.session.expiresAt>this.now())return Promise.resolve(this.session);
      return Promise.reject(this.lastError||Error('Sign-in renewal is temporarily unavailable.'));
    }
    return this.renew(null);
  }
  async renew(ticket){
    return this.run(async(epoch,fetcher,signal)=>{
      const activity=!!ticket&&this.foreground()&&this.now()-ticket.at<=60000;
      const session=await this.requestRefresh(activity,fetcher,signal,epoch);
      this.check(epoch);
      if(!session){this.end({reason:'expired'});throw endedError();}
      const accepted=this.accept(session,epoch);
      if(activity){
        this.lastActivitySentAt=this.now();
        if(this.pendingActivity?.sequence<=ticket.sequence)this.pendingActivity=null;
      }
      return accepted;
    });
  }
  schedule(){
    this.clearTimer(this.refreshTimer);this.refreshTimer=null;
    if(this.closed||!this.session)return;
    const remaining=this.session.expiresAt-this.now();
    if(!this.session.renewable){
      this.refreshTimer=this.setTimer(()=>this.end({reason:'expired'}),Math.max(0,remaining));
      return;
    }
    // A grant capped by the idle/absolute boundary cannot be extended by a
    // background request. Wait for that boundary instead of spinning near it.
    const delay=remaining<=60000?Math.max(1000,remaining):Math.min(240000,remaining-60000);
    this.refreshTimer=this.setTimer(()=>{this.refreshTimer=null;void this.ensure({force:true}).catch(()=>{});},delay);
  }
  scheduleRetry(error){
    if(this.closed||!this.session?.renewable)return;
    this.retryCount=Math.min(6,this.retryCount+1);
    const base=error.status===409?error.retryAfterMs||1000:5000;
    const delay=Math.min(60000,base*2**(this.retryCount-1));
    this.nextRetryAt=this.now()+delay;
    this.clearTimer(this.refreshTimer);
    this.refreshTimer=this.setTimer(()=>{this.refreshTimer=null;void this.ensure({force:true}).catch(()=>{});},delay);
  }
  noteActivity(event){
    if(event?.isTrusted!==true||!this.foreground()||this.closed||!this.session?.renewable)return;
    this.pendingActivity={sequence:++this.activitySequence,at:this.now()};
    this.queueActivity();
  }
  queueActivity(){
    if(this.closed||!this.pendingActivity||!this.session?.renewable||this.activityTimer!==null||this.flight)return;
    if(!this.foreground()||this.now()-this.pendingActivity.at>60000){this.pendingActivity=null;return;}
    const delay=Math.max(250,this.lastActivitySentAt+60000-this.now(),this.nextRetryAt-this.now());
    this.activityTimer=this.setTimer(()=>{
      this.activityTimer=null;
      if(this.closed||!this.pendingActivity||!this.foreground()||this.now()-this.pendingActivity.at>60000)return;
      if(this.flight){this.queueActivity();return;}
      void this.renew(this.pendingActivity).catch(()=>{});
    },delay);
  }
  stop(){
    if(this.closed)return false;
    this.closed=true;this.epoch++;
    this.clearTimer(this.refreshTimer);this.clearTimer(this.activityTimer);
    this.refreshTimer=null;this.activityTimer=null;this.pendingActivity=null;
    this.controller?.abort();this.session=null;
    for(const name of ['pointerdown','keydown','input','change','wheel'])this.document?.removeEventListener?.(name,this.onActivity,{capture:true});
    this.document?.removeEventListener?.('visibilitychange',this.onVisibility);
    this.window?.removeEventListener?.('pageshow',this.onResume);
    this.window?.removeEventListener?.('focus',this.onResume);
    this.channel?.removeEventListener?.('message',this.onChannel);
    this.channel?.close?.();
    return true;
  }
  end({reason='expired',error=null,broadcast=false}={}){
    if(this.closed)return;
    if(broadcast){try{this.channel?.postMessage?.({type:'logout'});}catch{/* Server revocation remains authoritative. */}}
    this.stop();this.onEnd(reason,error);
  }
  dispose(){this.stop();}
}
