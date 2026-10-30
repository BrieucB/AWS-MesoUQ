"""Clock boundaries after blocking identity work must guard grant publication."""
from copy import deepcopy
import pytest

from aws_publishing.browser_sessions import SessionExpired, RefreshBusy
from test_browser_sessions import family


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
