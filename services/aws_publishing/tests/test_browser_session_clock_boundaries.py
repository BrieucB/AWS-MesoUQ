"""Clock boundaries after blocking identity work must guard grant publication."""
from copy import deepcopy
import pytest

from aws_publishing.browser_sessions import SessionExpired, RefreshBusy
from test_browser_sessions import family, portal


def near_boundary(family, boundary):
    service,records,provider,clock,member,sid,initial=family
    if boundary=="absolute":
        for day in range(1,30):
            clock[0]=1000+day*86400
            service.refresh(sid,activity=True)
        end=initial["absolute_expires_at"]
    else:
        end=initial["idle_expires_at"]
    clock[0]=end-2
    service.refresh(sid,activity=False)
    return service,records,provider,clock,sid,end


@pytest.mark.parametrize("boundary",["idle","absolute"])
@pytest.mark.parametrize("operation",["current","cached_passive","cached_activity"])
def test_delayed_verification_cannot_publish_expired_grant(family,monkeypatch,boundary,operation):
    service,records,provider,clock,sid,end=near_boundary(family,boundary)
    before=deepcopy(records.records)
    verify=provider.verify
    def delayed(token,*args,**kwargs):
        clock[0]+=3
        return verify(token,*args,**kwargs)
    monkeypatch.setattr(provider,"verify",delayed)
    with pytest.raises((SessionExpired,RefreshBusy)):
        if operation=="current":
            service.current(sid)
        else:
            service.refresh(sid,activity=operation=="cached_activity")
    assert clock[0]>end
    assert records.records==before, "Late verification cannot touch/resurrect an expired family"


def test_current_grant_clock_crossing_requires_revalidation(family,monkeypatch):
    service,records,provider,clock,member,sid,initial=family
    clock[0]=initial["expires_at"]-2
    verify=provider.verify
    def delayed(token,*args,**kwargs):
        clock[0]+=3
        return verify(token,*args,**kwargs)
    monkeypatch.setattr(provider,"verify",delayed)
    with pytest.raises(RefreshBusy,match="refresh_required"):
        service.current(sid)
    assert clock[0]>initial["expires_at"]


@pytest.mark.parametrize("boundary",["idle","absolute"])
@pytest.mark.parametrize("operation",["current","cached_passive","cached_activity"])
def test_delayed_invitation_check_cannot_publish_or_touch_after_boundary(family,monkeypatch,boundary,operation):
    service,records,provider,clock,sid,end=near_boundary(family,boundary)
    before=deepcopy(records.records)
    def delayed(subject):
        clock[0]+=3
        return subject=="owner"
    monkeypatch.setattr(service,"invited",delayed)
    with pytest.raises((SessionExpired,RefreshBusy)):
        if operation=="current":
            service.current(sid)
        else:
            service.refresh(sid,activity=operation=="cached_activity")
    assert clock[0]>end
    assert records.records==before


@pytest.mark.parametrize("boundary",["idle","absolute"])
def test_cached_passive_refresh_rechecks_after_record_write(family,monkeypatch,boundary):
    service,records,provider,clock,sid,end=near_boundary(family,boundary)
    put=records.put
    def delayed(key,value,expected=None):
        written=put(key,value,expected)
        if expected is not None:
            clock[0]+=3
        return written
    monkeypatch.setattr(records,"put",delayed)
    with pytest.raises((SessionExpired,RefreshBusy)):
        service.refresh(sid,activity=False)
    assert clock[0]>end


@pytest.mark.parametrize("mode",["signature","claims"])
def test_current_jwt_expiry_during_verify_preserves_sid_for_one_authoritative_refresh(
        portal,monkeypatch,mode):
    from jwt.exceptions import ExpiredSignatureError
    from test_browser_sessions import sign_in,event,values
    from aws_publishing.site_auth import REMEMBERED
    import json
    auth,provider,legacy,clock=portal
    response,_=sign_in(portal,True)
    sid=values(response)[REMEMBERED]
    clock[0]=1780
    refreshed=auth.handle(event("/auth/refresh","POST",[REMEMBERED+"="+sid],'{"activity":false}'))
    assert refreshed["statusCode"]==200
    clock[0]=1898
    verify=provider.verify
    def expired(token,*args,**kwargs):
        clock[0]+=3
        if mode=="signature":
            raise ExpiredSignatureError("synthetic cached JWT expiry")
        return {"sub":"owner","exp":clock[0]}
    monkeypatch.setattr(provider,"verify",expired)
    current=auth.handle(event("/auth/session",cookies=[REMEMBERED+"="+sid]))
    assert current["statusCode"]==401
    assert json.loads(current["body"])["error"]["code"]=="refresh_required"
    assert not current["cookies"], "GET revalidation must not delete the eligible SID"
    monkeypatch.setattr(provider,"verify",verify)
    renewed=auth.handle(event("/auth/refresh","POST",[REMEMBERED+"="+sid],'{"activity":false}'))
    assert renewed["statusCode"]==200
    assert json.loads(renewed["body"])["renewable"] is True
    assert provider.refresh_calls==["refresh-private"]


@pytest.mark.parametrize("mode",["signature","claims"])
def test_cached_jwt_expiry_during_verify_renews_only_an_eligible_family(family,monkeypatch,mode):
    from jwt.exceptions import ExpiredSignatureError
    service,records,provider,clock,member,sid,initial=family
    clock[0]=1839
    service.refresh(sid,activity=False)  # Cached token has 61 seconds remaining.
    verify=provider.verify
    def expired(token,*args,**kwargs):
        if token=="access-initial":
            clock[0]=1901
            if mode=="signature":
                raise ExpiredSignatureError("synthetic cached JWT expiry")
            return {"sub":"owner","exp":clock[0]}
        return verify(token,*args,**kwargs)
    monkeypatch.setattr(provider,"verify",expired)
    renewed=service.refresh(sid,activity=False)
    assert renewed["access_token"]=="access-renewed"
    assert renewed["expires_at"]>clock[0]
    assert renewed["idle_expires_at"]==initial["idle_expires_at"]
    assert renewed["absolute_expires_at"]==initial["absolute_expires_at"]
    assert provider.refresh_calls==["refresh-private"]


@pytest.mark.parametrize("operation",["current","cached"])
def test_wrong_cached_identity_is_definitive_and_never_used_for_refresh(family,monkeypatch,operation):
    service,records,provider,clock,member,sid,initial=family
    monkeypatch.setattr(provider,"verify",lambda *a,**k:{"sub":"different-owner","exp":clock[0]+900})
    with pytest.raises(SessionExpired,match="identity"):
        if operation=="current":
            service.current(sid)
        else:
            service.refresh(sid,activity=False)
    assert provider.refresh_calls==[]
