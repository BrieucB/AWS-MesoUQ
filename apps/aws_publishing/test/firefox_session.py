"""Synthetic remembered-session browser checks, using an isolated Firefox profile.

Run on Karolina inside an allocated QA job with the explicit fixture server.
This never signs in to AWS, Cognito, MesoUQ, or an HPC account.
"""
import ast, base64, json, os, pathlib, socket, subprocess, tempfile, time

output=pathlib.Path(os.environ.get("MESOUQ_SESSION_BROWSER_OUTPUT","/tmp/aws-mesouq-session-browser"))
output.mkdir(parents=True,exist_ok=True)
base=os.environ.get("MESOUQ_VISUAL_BASE_URL","http://127.0.0.1:4173")
firefox=pathlib.Path(os.environ.get("MESOUQ_SESSION_FIREFOX","/usr/bin/firefox"))
if not firefox.is_absolute() or not firefox.is_file() or not os.access(firefox,os.X_OK):
    raise RuntimeError("An absolute executable MESOUQ_SESSION_FIREFOX is required")
firefox=firefox.resolve(strict=True)
temporary=tempfile.TemporaryDirectory(prefix="mesouq-session-browser-")
profile=pathlib.Path(temporary.name)
(profile/"user.js").write_text('user_pref("marionette.port", 2830);\nuser_pref("browser.shell.checkDefaultBrowser", false);\nuser_pref("datareporting.healthreport.uploadEnabled", false);\nuser_pref("toolkit.telemetry.enabled", false);\n')
log=open(output/"firefox-session.log","w")
process=subprocess.Popen([str(firefox),"--headless","--no-remote","--profile",str(profile),"--marionette","--remote-allow-system-access"],stdout=log,stderr=log)
sock=None
try:
    for _ in range(150):
        try:
            sock=socket.create_connection(("127.0.0.1",2830),timeout=1);sock.settimeout(20);break
        except OSError:time.sleep(.1)
    if sock is None:raise RuntimeError("Marionette did not start")
    pending=b"";sequence=0
    def receive():
        global pending
        while b":" not in pending:pending+=sock.recv(65536)
        prefix,tail=pending.split(b":",1);size=int(prefix)
        while len(tail)<size:tail+=sock.recv(65536)
        body,pending=tail[:size],tail[size:]
        return json.loads(body)
    receive()
    def command(name,args=None):
        global sequence
        sequence+=1;data=json.dumps([0,sequence,name,args or {}]).encode()
        sock.sendall(str(len(data)).encode()+b":"+data);result=receive()
        if result[2]:raise RuntimeError(str(result[2]))
        return result[3]
    def value(result):return result.get("value",result) if isinstance(result,dict) else result
    def execute(js):return value(command("WebDriver:ExecuteScript",{"script":js,"args":[],"newSandbox":False,"sandbox":"default"}))
    def wait(js):
        for _ in range(100):
            result=execute("return "+js)
            if result:return result
            time.sleep(.05)
        raise AssertionError("Synthetic browser readiness failed: "+js+"; "+str(execute("return document.body.innerText")))
    def click(selector):
        result=value(command("WebDriver:FindElement",{"using":"css selector","value":selector}))
        identifier=result.get("element-6066-11e4-a52e-4f735466cecf",result.get("ELEMENT"))
        assert identifier
        command("WebDriver:ElementClick",{"id":identifier})
    command("WebDriver:NewSession",{"capabilities":{"alwaysMatch":{}}})
    command("WebDriver:SetWindowRect",{"width":1440,"height":1100,"x":0,"y":0})
    command("WebDriver:Navigate",{"url":base+"/__fixtures__/workspace?remembered=1"})
    wait("!!document.querySelector('#file') && !!window.__mesouqSessionFixture")
    first=value(command("WebDriver:GetWindowHandle"))
    execute("""for(const [id,v] of [['forceUnit','nN'],['displacementUnit','nm']]){
      const el=document.querySelector('#'+id);el.value=v;el.dispatchEvent(new Event('change',{bubbles:true}));
    }
    const input=document.querySelector('#file'),data=new DataTransfer();
    data.items.add(new File(['1,2\\n3,4'],'remembered-synthetic.csv',{type:'text/csv'}));
    input.files=data.files;input.dispatchEvent(new Event('change',{bubbles:true}));return true;""")
    wait("document.body.innerText.includes('remembered-synthetic.csv')")
    before=execute("return {force:document.querySelector('#forceUnit').value,displacement:document.querySelector('#displacementUnit').value,preview:document.body.innerText.includes('remembered-synthetic.csv'),limits:window.__mesouqSessionFixture.limits()}")
    execute("window.__mesouqSessionFixture.advance(301000);window.__mesouqSessionFixture.resume();return true;")
    wait("window.__mesouqSessionFixture.calls.some(c=>c.path==='/auth/refresh')")
    after=execute("return {force:document.querySelector('#forceUnit').value,displacement:document.querySelector('#displacementUnit').value,preview:document.body.innerText.includes('remembered-synthetic.csv'),limits:window.__mesouqSessionFixture.limits(),calls:window.__mesouqSessionFixture.calls.filter(c=>c.path==='/auth/refresh')}")
    assert after["force"]==before["force"]=="nN" and after["displacement"]==before["displacement"]=="nm" and after["preview"]
    assert after["limits"]["idle"]==before["limits"]["idle"] and after["limits"]["absolute"]==before["limits"]["absolute"]
    assert after["calls"] and all(c["activity"] is False for c in after["calls"])
    click('[data-action="tab"][data-tab="workspace"]')
    wait("window.__mesouqSessionFixture.calls.some(c=>c.path==='/auth/refresh' && c.activity===true)")
    activity=execute("return {limits:window.__mesouqSessionFixture.limits(),foreground:document.hasFocus(),preview:document.body.innerText.includes('remembered-synthetic.csv'),localStorageItems:localStorage.length,sessionStorageItems:sessionStorage.length}")
    assert activity["preview"] and activity["foreground"]
    assert activity["limits"]["idle"]>before["limits"]["idle"] and activity["limits"]["absolute"]==before["limits"]["absolute"]
    assert activity["localStorageItems"]==activity["sessionStorageItems"]==0
    screenshot=value(command("WebDriver:TakeScreenshot",{"id":None,"highlights":[],"full":False}))
    (output/"remembered-preview.png").write_bytes(base64.b64decode(screenshot))
    second=value(command("WebDriver:NewWindow",{"type":"tab"}))["handle"]
    command("WebDriver:SwitchToWindow",{"handle":second})
    command("WebDriver:Navigate",{"url":base+"/__fixtures__/workspace?remembered=1&cold=1"})
    wait("!!document.querySelector('#file') && !!window.__mesouqSessionFixture")
    cold=execute("return window.__mesouqSessionFixture.calls.filter(c=>c.path.startsWith('/auth/'))")
    assert [c["path"] for c in cold[:2]]==["/auth/session","/auth/refresh"] and cold[1]["activity"] is False
    command("WebDriver:SwitchToWindow",{"handle":first})
    click('[data-action="logout"]')
    command("WebDriver:SwitchToWindow",{"handle":second})
    wait("!document.querySelector('#file') && document.body.innerText.includes('signed out')")
    signed_out=execute("return {privateForm:!!document.querySelector('#file'),account:document.body.innerText.includes('researcher@example.test'),redirected:location.pathname!=='/__fixtures__/workspace'}")
    assert not signed_out["privateForm"] and not signed_out["account"] and not signed_out["redirected"]
    (output/"session-browser-evidence.json").write_text(json.dumps({
        "synthetic_fixture_only":True,"live_authentication_verified":False,"aws_or_hpc_calculation_submitted":False,
        "firefox_binary":str(firefox),
        "passive_refresh_preserves_units_and_file_preview":True,"passive_refresh_does_not_extend_idle":True,
        "trusted_browser_input_touches_idle_only":True,"no_browser_auth_storage":True,
        "cold_entry_resumes_with_passive_post":True,"cross_tab_logout_clears_private_view":True,
        "passive":after,"activity":activity,"cold":cold,"signed_out":signed_out,
    },indent=2))
    print(json.dumps({"synthetic_browser_checks":"passed","live_authentication_verified":False,"output":str(output)}),flush=True)
    command("Marionette:Quit",{"flags":["eForceQuit"]})
finally:
    if sock:sock.close()
    try:process.wait(timeout=10)
    except subprocess.TimeoutExpired:process.terminate();process.wait(timeout=10)
    log.close();temporary.cleanup()
