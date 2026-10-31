"""Finite remembered-browser policy, encrypted custody and concurrent revocation."""
from copy import deepcopy
import json
import re
import threading
from urllib.parse import parse_qs, urlencode, urlsplit

from cryptography.fernet import Fernet
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
import pytest

from aws_publishing.browser_sessions import (
    BrowserSessions, MemorySessionRecords, DynamoSessionRecords, SessionExpired,
    RefreshBusy, RefreshUnavailable, MAXIMUM_SECONDS, IDLE_SECONDS)
from aws_publishing.site_auth import SiteAuth, INTENT, FLOW, REMEMBERED, SESSION, EDGE_COOKIES


class Provider:
    client_id = "remembered-web-only"
    domain = "https://identity.example"
    def __init__(self, clock):
        self.clock = clock
        self.nonce = ""
        self.refresh_calls = []
        self.revoked = []
        self.failure = None
        self.hook = None
    def exchange(self, *args):
        return {"id_token":"identity","access_token":"access-initial","refresh_token":"refresh-private"}
    def verify(self, token, *, id_token=False):
        return {"sub":"owner","exp":int(self.clock())+900,"nonce":self.nonce}
    def refresh(self, token):
        self.refresh_calls.append(token)
        if self.hook:self.hook()
        if self.failure:raise self.failure
        return {"access_token":"access-renewed"}
    def revoke(self, token):
        self.revoked.append(token)


@pytest.fixture
def family():
    now = [1000]
    provider = Provider(lambda:now[0])
    records = MemorySessionRecords()
    member = [True]
    service = BrowserSessions(records,Fernet(Fernet.generate_key()),provider,
        lambda subject:member[0] and subject=="owner",clock=lambda:now[0])
    sid, initial = service.create(provider.exchange(),"owner")
    return service,records,provider,now,member,sid,initial


def test_credentials_are_encrypted_and_sid_is_only_an_opaque_lookup(family):
    service,records,provider,now,member,sid,initial = family
    saved = json.dumps(records.records)
    assert len(sid)==43 and sid not in saved
    assert "refresh-private" not in saved and "access-initial" not in saved
    assert set(initial)=={"access_token","expires_at","renewable","absolute_expires_at","idle_expires_at","generation"}
    assert initial["absolute_expires_at"]==1000+MAXIMUM_SECONDS
    assert initial["idle_expires_at"]==1000+IDLE_SECONDS
    assert initial["expires_at"]==1300


def test_background_refresh_renews_grants_without_extending_idle_or_absolute(family):
    service,_,provider,now,_,sid,initial = family
    now[0]+=240
    result = service.refresh(sid,activity=False)
    assert result["expires_at"]==1540
    assert result["idle_expires_at"]==initial["idle_expires_at"]
    assert result["absolute_expires_at"]==initial["absolute_expires_at"]
    assert result["generation"]>initial["generation"]
    assert provider.refresh_calls==[]


def test_real_activity_extends_idle_but_never_the_original_thirty_day_end(family):
    service,_,_,now,_,sid,initial = family
    now[0]+=86400
    result = service.refresh(sid,activity=True)
    assert result["idle_expires_at"]==now[0]+IDLE_SECONDS
    assert result["absolute_expires_at"]==initial["absolute_expires_at"]
    for day in range(2,30):
        now[0]=1000+day*86400
        result=service.refresh(sid,activity=True)
    assert result["idle_expires_at"]==initial["absolute_expires_at"]
    now[0]=initial["absolute_expires_at"]
    with pytest.raises(SessionExpired):service.refresh(sid,activity=True)


@pytest.mark.parametrize("activity",[False,True])
def test_seven_day_idle_expiry_cannot_be_resurrected_by_refresh(family,activity):
    service,_,_,now,_,sid,initial = family
    now[0]=initial["idle_expires_at"]
    with pytest.raises(SessionExpired):service.refresh(sid,activity=activity)


def test_expired_short_grant_requires_refresh_but_not_fresh_mfa(family):
    service,_,provider,now,_,sid,_ = family
    now[0]+=300
    with pytest.raises(RefreshBusy,match="refresh_required"):service.current(sid)
    renewed=service.refresh(sid,activity=False)
    assert renewed["expires_at"]==now[0]+300
    assert provider.refresh_calls==[]


def test_access_expiry_refresh_uses_server_token_only_and_keeps_original_family(family):
    service,_,provider,now,_,sid,initial = family
    now[0]+=850
    result=service.refresh(sid,activity=False)
    assert provider.refresh_calls==["refresh-private"]
    assert result["access_token"]=="access-renewed"
    assert result["absolute_expires_at"]==initial["absolute_expires_at"]
    assert "refresh_token" not in result and "sid" not in result


def test_provider_transport_failure_releases_lease_without_revoking_family(family):
    service,_,provider,now,_,sid,initial = family
    now[0]+=850;provider.failure=OSError("simulated provider timeout")
    with pytest.raises(RefreshUnavailable):service.refresh(sid,activity=False)
    provider.failure=None
    assert service.refresh(sid,activity=False)["renewable"] is True
    assert service._read(sid)["state"]=="active"


def test_definitive_provider_denial_tombstones_family(family):
    service,records,provider,now,_,sid,_ = family
    now[0]+=850;provider.failure=ValueError("invalid_grant")
    with pytest.raises(SessionExpired):service.refresh(sid,activity=False)
    with pytest.raises(SessionExpired):service.refresh(sid,activity=True)
    assert "refresh-private" not in json.dumps(records.records)


def test_current_invitation_is_required_on_hydration_and_refresh(family):
    service,_,_,_,member,sid,_ = family
    member[0]=False
    for operation in (lambda:service.current(sid),lambda:service.refresh(sid,activity=True)):
        with pytest.raises(Exception) as error:operation()
        assert getattr(error.value,"status",None)==403


def test_two_tabs_cannot_refresh_the_same_provider_token_concurrently(family):
    service,_,provider,now,_,sid,_ = family
    now[0]+=850
    entered=threading.Event();release=threading.Event();outcome=[]
    provider.hook=lambda:(entered.set(),release.wait(timeout=3))
    def first():
        try:outcome.append(service.refresh(sid,activity=False))
        except Exception as error:outcome.append(error)
    thread=threading.Thread(target=first);thread.start()
    try:
        assert entered.wait(timeout=2)
        with pytest.raises(RefreshBusy,match="refresh_in_progress"):
            service.refresh(sid,activity=False)
    finally:
        release.set();thread.join(timeout=3)
    assert len(provider.refresh_calls)==1
    assert len(outcome)==1 and isinstance(outcome[0],dict)


def test_logout_tombstone_wins_over_an_inflight_provider_response(family):
    service,_,provider,now,_,sid,_ = family
    now[0]+=850
    provider.hook=lambda:service.revoke(sid)
    with pytest.raises(SessionExpired):service.refresh(sid,activity=False)
    assert provider.revoked==["refresh-private"]
    with pytest.raises(SessionExpired):service.current(sid)
    with pytest.raises(SessionExpired):service.refresh(sid,activity=True)


@pytest.fixture
def portal():
    now=[1000]
    provider=Provider(lambda:now[0])
    legacy=Provider(lambda:now[0]);legacy.client_id="original-native-and-legacy"
    key=rsa.generate_private_key(public_exponent=65537,key_size=2048)
    pem=key.private_bytes(serialization.Encoding.PEM,serialization.PrivateFormat.PKCS8,serialization.NoEncryption())
    auth=SiteAuth(base_url="https://portal.example",key_id="public",private_key=pem,
        cognito=legacy,remembered_cognito=provider,session_records=MemorySessionRecords(),
        invited=lambda sub:sub=="owner",clock=lambda:now[0])
    return auth,provider,legacy,now


def event(path,method="GET",cookies=(),body=None,origin="https://portal.example",content="application/json"):
    return {"rawPath":path,"requestContext":{"http":{"method":method}},
        "headers":{"Origin":origin,"Content-Type":content},"cookies":list(cookies),
        **({"body":body} if body is not None else {})}


def values(response):
    return {item.split("=",1)[0]:item.split(";",1)[0].split("=",1)[1]
            for item in response.get("cookies",[]) if "Max-Age=0" not in item}


def sign_in(portal,remember):
    auth,provider,legacy,_=portal
    landing=auth.handle(event("/auth/login"));jar=values(landing)
    nonce=re.search('name="csrf" value="([^"]+)"',landing["body"])[1]
    body=urlencode({"csrf":nonce,**({"remember":"1"} if remember else {})})
    started=auth.handle(event("/auth/login","POST",[INTENT+"="+jar[INTENT]],body,
        content="application/x-www-form-urlencoded"))
    assert started["statusCode"]==302
    flow=values(started)[FLOW];query=parse_qs(urlsplit(started["headers"]["location"]).query)
    (provider if remember else legacy).nonce=query["nonce"][0]
    callback=event("/auth/callback",cookies=[FLOW+"="+flow])
    callback["rawQueryString"]=urlencode({"code":"one-use","state":query["state"][0]})
    result=auth.handle(callback)
    assert result["statusCode"]==302
    assert result["headers"]["location"]==auth.base+"/app"
    return result,query


def test_explicit_remember_choice_is_bound_to_dedicated_client_and_opaque_cookie(portal):
    auth,provider,legacy,_=portal
    result,query=sign_in(portal,True)
    jar=values(result)
    assert query["client_id"]==[provider.client_id]
    assert REMEMBERED in jar and SESSION not in jar
    assert "refresh-private" not in json.dumps(result) and "access-initial" not in json.dumps(result)
    token_cookie=next(c for c in result["cookies"] if c.startswith(REMEMBERED+"="))
    assert "Secure; HttpOnly; SameSite=Lax" in token_cookie and "Domain=" not in token_cookie
    assert "Max-Age="+str(IDLE_SECONDS) in token_cookie
    assert not legacy.refresh_calls


def test_unchecked_choice_keeps_original_five_minute_policy(portal):
    auth,_,legacy,_=portal
    result,query=sign_in(portal,False);jar=values(result)
    assert query["client_id"]==[legacy.client_id]
    assert SESSION in jar and REMEMBERED not in jar
    hydrated=auth.handle(event("/auth/session",cookies=[SESSION+"="+jar[SESSION]]))
    assert json.loads(hydrated["body"])=={"access_token":"access-initial","expires_at":1300}


def test_cold_entry_resumes_without_oauth_or_mfa_and_refresh_response_hides_sid(portal):
    auth,provider,_,now=portal
    result,_=sign_in(portal,True);sid=values(result)[REMEMBERED];now[0]+=850
    resumed=auth.handle(event("/auth/login",cookies=[REMEMBERED+"="+sid]))
    assert resumed["statusCode"]==302 and resumed["headers"]["location"]==auth.base+"/app"
    assert provider.refresh_calls==["refresh-private"]
    refreshed=auth.handle(event("/auth/refresh","POST",[REMEMBERED+"="+sid],json.dumps({"activity":False})))
    assert refreshed["statusCode"]==200
    body=json.loads(refreshed["body"])
    assert body["renewable"] is True and sid not in refreshed["body"]
    assert body["expires_at"]<=now[0]+300
    assert refreshed["headers"]["cache-control"]=="no-store, private"


@pytest.mark.parametrize("body",['{}','{"activity":1}','{"activity":null}','{"activity":false,"extra":1}','[]','{"activity":NaN}','{"activity":false,"activity":true}'])
def test_refresh_rejects_nonexact_body_without_provider_call(portal,body):
    auth,provider,_,_=portal
    result,_=sign_in(portal,True);sid=values(result)[REMEMBERED]
    reply=auth.handle(event("/auth/refresh","POST",[REMEMBERED+"="+sid],body))
    assert reply["statusCode"] in (400,401)
    assert provider.refresh_calls==[]


def test_logout_clears_all_browser_cookies_and_replay_cannot_resume(portal):
    auth,provider,_,_=portal
    result,_=sign_in(portal,True);sid=values(result)[REMEMBERED]
    reply=auth.handle(event("/auth/logout","POST",[REMEMBERED+"="+sid]))
    assert reply["statusCode"]==200
    assert all("Max-Age=0" in value for value in reply["cookies"])
    assert {cookie.split("=",1)[0] for cookie in reply["cookies"]}=={SESSION,FLOW,INTENT,REMEMBERED,*EDGE_COOKIES}
    assert provider.revoked==["refresh-private"]
    assert auth.handle(event("/auth/refresh","POST",[REMEMBERED+"="+sid],'{"activity":false}'))["statusCode"]==401


def test_dynamo_repository_uses_only_separate_point_key_and_conditional_put():
    calls=[]
    class Client:
        def get_item(self,**kwargs):calls.append(("get",kwargs));return {}
        def put_item(self,**kwargs):calls.append(("put",kwargs))
    store=DynamoSessionRecords(Client(),"existing-table")
    assert store.get("hash") is None
    assert store.put("hash",{"version":0,"payload":"encrypted","auth_expires_at":1000})
    assert store.put("hash",{"version":1,"payload":"encrypted","auth_expires_at":1000},expected=0)
    assert calls[0][1]["Key"]=={"pk":{"S":"AUTH_SESSION#hash"},"sk":{"S":"SESSION"}}
    assert calls[0][1]["ConsistentRead"] is True
    assert calls[1][1]["ConditionExpression"]=="attribute_not_exists(pk)"
    assert calls[2][1]["ExpressionAttributeValues"]=={":expected":{"N":"0"}}


def test_cold_navigation_does_not_extend_idle_and_bad_oauth_callback_cannot_logout(portal):
    auth,_,_,now=portal
    result,_=sign_in(portal,True);sid=values(result)[REMEMBERED]
    original=auth.remembered._read(sid)["idle_expires_at"]
    now[0]+=86400
    resumed=auth.handle(event("/auth/login",cookies=[REMEMBERED+"="+sid]))
    assert resumed["statusCode"]==302
    assert auth.remembered._read(sid)["idle_expires_at"]==original
    bad=event("/auth/callback",cookies=[REMEMBERED+"="+sid])
    bad["rawQueryString"]="code=forged&state=forged"
    rejected=auth.handle(bad)
    assert rejected["statusCode"]==401
    assert not any(c.startswith(REMEMBERED+"=") or c.startswith("CloudFront-") for c in rejected["cookies"])
    assert auth.remembered.current(sid)["renewable"] is True


def test_disabled_feature_still_tombstones_an_existing_family_on_logout(portal):
    auth,provider,_,_=portal
    result,_=sign_in(portal,True);sid=values(result)[REMEMBERED]
    auth.remembered_enabled=False
    assert auth.handle(event("/auth/session",cookies=[REMEMBERED+"="+sid]))["statusCode"]==401
    reply=auth.handle(event("/auth/logout","POST",[REMEMBERED+"="+sid]))
    assert reply["statusCode"]==200 and provider.revoked==["refresh-private"]
    auth.remembered_enabled=True
    with pytest.raises(SessionExpired):auth.remembered.current(sid)


def test_failed_cas_never_extends_idle_or_claims_logout_success(family,monkeypatch):
    service,records,provider,now,_,sid,initial=family
    now[0]+=240
    put=records.put
    monkeypatch.setattr(records,"put",lambda key,value,expected=None:
        False if expected is not None else put(key,value,expected))
    with pytest.raises(RefreshBusy):service.refresh(sid,activity=True)
    assert service._read(sid)["idle_expires_at"]==initial["idle_expires_at"]
    with pytest.raises(RefreshBusy):service.revoke(sid)
    assert provider.revoked==[]


def test_tampered_record_fails_closed_without_provider_use(family):
    service,records,provider,_,_,sid,_=family
    records.records[service.key(sid)]["payload"]="invalid-encrypted-payload"
    with pytest.raises(SessionExpired):service.refresh(sid,activity=True)
    assert provider.refresh_calls==[]


def test_regional_factory_point_reads_invitation_without_business_initialization(monkeypatch):
    import sys
    from types import SimpleNamespace
    from aws_publishing import handler
    key=rsa.generate_private_key(public_exponent=65537,key_size=2048)
    pem=key.private_bytes(serialization.Encoding.PEM,serialization.PrivateFormat.PKCS8,serialization.NoEncryption()).decode()
    calls=[]
    class SSM:
        def get_parameter(self,**kwargs):
            return {"Parameter":{"Value":pem}}
    class Dynamo:
        def get_item(self,**kwargs):
            calls.append(kwargs)
            return {"Item":{"payload":{"S":json.dumps({"projects":{"fixture":"member"}})}}}
    dynamo=Dynamo()
    monkeypatch.setitem(sys.modules,"boto3",SimpleNamespace(client=lambda name:
        SSM() if name=="ssm" else dynamo if name=="dynamodb" else pytest.fail("Unexpected AWS service")))
    for name,value in {"SITE_SIGNING_KEY_PARAMETER":"/synthetic/key",
        "SITE_BASE_URL":"https://portal.example","SITE_PUBLIC_KEY_ID":"synthetic",
        "COGNITO_DOMAIN":"https://identity.example","COGNITO_ISSUER":"https://issuer.example/pool",
        "COGNITO_CLIENT_ID":"original","COGNITO_REMEMBERED_CLIENT_ID":"remembered",
        "SITE_REMEMBERED_SESSIONS_ENABLED":"false","STATE_TABLE":"existing-table"}.items():
        monkeypatch.setenv(name,value)
    monkeypatch.setattr(handler,"_site_auth",None)
    monkeypatch.setattr(handler,"application",lambda:pytest.fail("Auth cannot initialize business snapshots"))
    auth=handler.site_auth()
    assert auth.remembered is not None and auth.remembered_enabled is False
    assert auth.invited("owner") is True
    assert calls==[{"TableName":"existing-table",
        "Key":{"pk":{"S":"STATE"},"sk":{"S":"identities:owner"}},"ConsistentRead":True}]


def test_remembered_asset_cookies_expire_in_browser_at_exact_short_grant(portal):
    auth,_,_,now=portal
    result,_=sign_in(portal,True);sid=values(result)[REMEMBERED]
    for response in (result,
        auth.handle(event("/auth/login",cookies=[REMEMBERED+"="+sid])),
        auth.handle(event("/auth/refresh","POST",[REMEMBERED+"="+sid],'{"activity":false}'))):
        grants=[cookie for cookie in response["cookies"] if cookie.startswith("CloudFront-")]
        assert len(grants)==4
        assert all("; Max-Age=300" in cookie for cookie in grants)
        assert all("Secure; HttpOnly; SameSite=Lax" in cookie for cookie in grants)


def test_transient_jwks_transport_failure_does_not_revoke_family(family,monkeypatch):
    from jwt.exceptions import PyJWKClientConnectionError
    service,_,provider,now,_,sid,_=family
    now[0]+=240
    verify=provider.verify
    monkeypatch.setattr(provider,"verify",lambda *a,**k:(_ for _ in ()).throw(
        PyJWKClientConnectionError("synthetic JWKS timeout")))
    with pytest.raises(RefreshUnavailable):service.refresh(sid,activity=False)
    monkeypatch.setattr(provider,"verify",verify)
    assert service.refresh(sid,activity=False)["renewable"] is True


@pytest.mark.parametrize("sid_state", ["invalid", "revoked", "disabled"])
def test_present_bad_remembered_sid_never_downgrades_to_valid_legacy(portal, monkeypatch, sid_state):
    auth, provider, legacy, _ = portal
    remembered, _ = sign_in(portal, True)
    sid = values(remembered)[REMEMBERED]
    unchecked, _ = sign_in(portal, False)
    sealed = values(unchecked)[SESSION]
    if sid_state == "invalid":
        sid = "invalid-remembered-lookup"
    elif sid_state == "revoked":
        auth.remembered.revoke(sid)
    else:
        auth.remembered_enabled = False
    def no_legacy_validation(*args, **kwargs):
        pytest.fail("An existing remembered SID must never downgrade to legacy")
    monkeypatch.setattr(legacy, "verify", no_legacy_validation)
    result = auth.handle(event("/auth/login", cookies=[
        REMEMBERED + "=" + sid, SESSION + "=" + sealed]))
    assert result["statusCode"] == 200
    assert "location" not in result["headers"]
    assert not any(c.startswith("CloudFront-") or c.startswith(SESSION + "=")
                   for c in result["cookies"])
    assert not provider.refresh_calls
    assert not legacy.refresh_calls
