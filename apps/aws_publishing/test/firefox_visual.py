"""Local Firefox/Marionette visual check; requires the explicit visual fixture server.
Invoke with /usr/bin/python3.11 on Karolina. Uses no cloud credentials or jobs.
"""
import base64, json, os, pathlib, socket, subprocess, tempfile, time
root=pathlib.Path(__file__).resolve().parents[3]
output=pathlib.Path(os.environ.get('MESOUQ_VISUAL_OUTPUT', '/tmp/aws-mesouq-browser-visual'))
output.mkdir(parents=True,exist_ok=True)
temporary_profile=tempfile.TemporaryDirectory(prefix='mesouq-firefox-visual-')
profile=pathlib.Path(temporary_profile.name)
(profile/'user.js').write_text('user_pref("marionette.port", 2828);\nuser_pref("browser.shell.checkDefaultBrowser", false);\nuser_pref("datareporting.healthreport.uploadEnabled", false);\nuser_pref("toolkit.telemetry.enabled", false);\n')
log=open(output/'firefox-headless.log','w')
process=subprocess.Popen(['/usr/bin/firefox','--headless','--no-remote','--profile',str(profile),'--marionette','--remote-allow-system-access'],stdout=log,stderr=log)
sock=None
try:
    for _ in range(150):
        try:
            sock=socket.create_connection(('127.0.0.1',2828),timeout=1);sock.settimeout(20);break
        except OSError:time.sleep(.1)
    if sock is None:raise RuntimeError('Marionette did not start')
    pending=b''
    def receive():
        global pending
        while b':' not in pending:pending+=sock.recv(65536)
        prefix,tail=pending.split(b':',1);size=int(prefix)
        while len(tail)<size:tail+=sock.recv(65536)
        body,pending=tail[:size],tail[size:]
        return json.loads(body)
    handshake=receive();command_id=0
    def command(name,args=None):
        global command_id
        command_id+=1;message=json.dumps([0,command_id,name,args or {}]).encode();sock.sendall(str(len(message)).encode()+b':'+message)
        result=receive()
        if result[2]:raise RuntimeError(str(result[2]))
        return result[3]
    session=command('WebDriver:NewSession',{'capabilities':{'alwaysMatch':{}}})
    def execute(script):
        result=command('WebDriver:ExecuteScript',{'script':script,'args':[],'newSandbox':False,'sandbox':'default'})
        return result.get('value',result)
    records=[]
    for label,width,height,path,condition in [
        ('workspace-desktop',1440,1100,'/__fixtures__/workspace','!!document.querySelector("#diameter")'),
        ('workspace-mobile',390,844,'/__fixtures__/workspace','!!document.querySelector("#diameter")'),
        ('results-desktop',1440,1100,'/__fixtures__/workspace?view=result','!!document.querySelector("[data-action=download-result]")'),
        ('results-mobile',390,844,'/__fixtures__/workspace?view=result','!!document.querySelector("[data-action=download-result]")'),
    ]:
        command('WebDriver:SetWindowRect',{'width':width,'height':height,'x':0,'y':0})
        if width<450:
            command('Marionette:SetContext',{'value':'chrome'})
            execute('window.document.documentElement.style.minWidth="0px";window.document.documentElement.getBoundingClientRect();window.resizeTo('+str(width)+','+str(height)+');return true;')
            time.sleep(.3)
            execute('window.resizeTo('+str(width)+','+str(height)+');return true;')
            command('Marionette:SetContext',{'value':'content'})
        command('WebDriver:Navigate',{'url':'http://127.0.0.1:4173'+path})
        for _ in range(100):
            if execute('return '+condition):break
            time.sleep(.1)
        else:raise RuntimeError('Readiness timed out: '+label+' '+str(execute('return document.body.innerText;')))
        time.sleep(.2)
        metrics=execute('return {width:innerWidth,height:innerHeight,scrollWidth:document.documentElement.scrollWidth,scrollHeight:document.documentElement.scrollHeight,title:document.title,alerts:Array.from(document.querySelectorAll("[role=alert]")).map(x=>x.textContent),fixture:!!document.querySelector(".visual-test-banner"),inlineStyles:document.querySelectorAll("[style]").length,inlineScripts:document.querySelectorAll("script:not([src])").length};')
        screenshot=command('WebDriver:TakeScreenshot',{'id':None,'highlights':[],'full':True})
        (output/(label+'.png')).write_bytes(base64.b64decode(screenshot.get('value',screenshot)))
        assert metrics['fixture'], 'Synthetic fixture banner missing'
        assert metrics['scrollWidth'] <= metrics['width'], 'Horizontal page overflow'
        assert metrics['inlineStyles'] == 0 and metrics['inlineScripts'] == 0, 'Self-only CSP regression'
        assert not metrics['alerts'], 'Unexpected application alert'
        records.append({'name':label,'url':'http://127.0.0.1:4173'+path,**metrics})
        print(label,json.dumps(metrics),flush=True)
    execute('document.querySelector("figure").scrollIntoView();return true;')
    time.sleep(.2)
    screenshot=command('WebDriver:TakeScreenshot',{'id':None,'highlights':[],'full':False})
    (output/'results-mobile-chart-viewport.png').write_bytes(base64.b64decode(screenshot.get('value',screenshot)))
    (output/'firefox-visual-evidence.json').write_text(json.dumps({'browser':session,'handshake':handshake,'checks':records,'test_only_synthetic_data':True,'live_aws_hpc_verified':False,'protected_site_authentication_verified':False,'source_sha256':{name:__import__('hashlib').sha256((root/name).read_bytes()).hexdigest() for name in ['apps/aws_publishing/src/app.js','apps/aws_publishing/src/client.js','apps/aws_publishing/src/style.css','apps/aws_publishing/test/browser-fixture.js','apps/aws_publishing/test/visual-server.mjs']}},indent=2))
    command('Marionette:Quit',{'flags':['eForceQuit']})
finally:
    if sock:sock.close()
    try:process.wait(timeout=10)
    except subprocess.TimeoutExpired:process.terminate();process.wait(timeout=10)
    log.close()
    temporary_profile.cleanup()
