from __future__ import annotations

from io import BytesIO
import json
from types import SimpleNamespace
import urllib.error
import urllib.parse

import pytest

from aws_mesouq.connector.api import API, APIError
from aws_mesouq.connector.auth import SessionExpired, TokenSession, browser_login


def test_preexpiry_refresh_keeps_credentials_only_in_memory(monkeypatch):
    now = [0]
    session = TokenSession("https://auth", "client", {"access_token": "old", "refresh_token": "private", "expires_in": 900}, clock=lambda: now[0])
    requests = []
    def exchange(domain, fields):
        requests.append(fields)
        return {"access_token": "new", "expires_in": 900}
    monkeypatch.setattr("aws_mesouq.connector.auth.token_request", exchange)
    assert session.token() == "old"
    assert not requests
    now[0] = 850
    assert session.token() == "new"
    assert requests == [{"grant_type": "refresh_token", "client_id": "client", "refresh_token": "private"}]
    assert session.refresh_token == "private"


def test_rejected_refresh_fails_closed_without_returning_old_access(monkeypatch):
    session = TokenSession("https://auth", "client", {"access_token": "old", "refresh_token": "private", "expires_in": 900})
    def reject(*args):
        raise RuntimeError("private token must not appear in surfaced error")
    monkeypatch.setattr("aws_mesouq.connector.auth.token_request", reject)
    with pytest.raises(SessionExpired, match="Session renewal failed") as error:
        session.token(force_refresh=True)
    assert "private" not in str(error.value)
    assert session.access_token == ""
    assert session.refresh_token is None
    with pytest.raises(SessionExpired):
        session.token()


def test_api_401_renews_once_then_retries_same_request(monkeypatch):
    session = TokenSession("https://auth", "client", {"access_token": "old", "refresh_token": "private", "expires_in": 900})
    monkeypatch.setattr("aws_mesouq.connector.auth.token_request", lambda *args: {"access_token": "new", "expires_in": 900})
    calls = []
    def open_request(request, **kwargs):
        calls.append((request.headers["Authorization"], request.data))
        if len(calls) == 1:
            raise urllib.error.HTTPError(request.full_url, 401, "Unauthorized", {}, BytesIO(b"{}"))
        return BytesIO(b'{"ok":true}')
    monkeypatch.setattr("urllib.request.build_opener", lambda *args: SimpleNamespace(open=open_request))
    assert API("https://api", session).request("POST", "/v1/connector/claim", {"project_id": "p"}) == {"ok": True}
    assert [call[0] for call in calls] == ["Bearer old", "Bearer new"]
    assert calls[0][1] == calls[1][1]


def test_api_401_retries_only_once(monkeypatch):
    session = TokenSession("https://auth", "client", {"access_token": "old", "refresh_token": "private", "expires_in": 900})
    monkeypatch.setattr("aws_mesouq.connector.auth.token_request", lambda *args: {"access_token": "new", "expires_in": 900})
    calls = []
    def reject(request, **kwargs):
        calls.append(request)
        raise urllib.error.HTTPError(request.full_url, 401, "Unauthorized", {}, BytesIO(b"{}"))
    monkeypatch.setattr("urllib.request.build_opener", lambda *args: SimpleNamespace(open=reject))
    with pytest.raises(APIError) as error:
        API("https://api", session).request("GET", "/v1/me")
    assert error.value.status == 401
    assert len(calls) == 2


def test_browser_callback_registered_localhost_with_ipv4_listener(monkeypatch):
    opened, bound, exchanged = [], [], []
    class Server:
        def __init__(self, address, handler):
            bound.append(address)
            self.handler = handler
        def __enter__(self):
            return self
        def __exit__(self, *_args):
            pass
        def handle_request(self):
            state = urllib.parse.parse_qs(urllib.parse.urlsplit(opened[0]).query)["state"][0]
            request = self.handler.__new__(self.handler)
            request.path = "/callback?" + urllib.parse.urlencode({"state": state, "code": "authorization-code"})
            request.send_response = lambda *_args: None
            request.end_headers = lambda: None
            request.wfile = BytesIO()
            request.do_GET()
    monkeypatch.setattr("aws_mesouq.connector.auth.HTTPServer", Server)
    monkeypatch.setattr("webbrowser.open", lambda url: opened.append(url))
    def exchange(domain, fields):
        exchanged.append(fields)
        return {"access_token": "access", "refresh_token": "refresh", "expires_in": 900}
    monkeypatch.setattr("aws_mesouq.connector.auth.token_request", exchange)
    assert isinstance(browser_login("https://auth", "client"), TokenSession)
    assert bound == [("127.0.0.1", 8765)]
    query = urllib.parse.parse_qs(urllib.parse.urlsplit(opened[0]).query)
    assert query["redirect_uri"] == ["http://localhost:8765/callback"]
    assert exchanged[0]["redirect_uri"] == "http://localhost:8765/callback"
    assert query["code_challenge_method"] == ["S256"]
