"""Interactive OAuth authorization-code flow; credentials remain in memory."""
from __future__ import annotations

import base64
import hashlib
import json
import math
import secrets
import time
import urllib.parse
import urllib.request
import webbrowser
from http.server import BaseHTTPRequestHandler, HTTPServer


class SessionExpired(RuntimeError):
    """Interactive reauthentication is required; credentials are never logged."""


class TokenSession:
    """Refreshable OAuth credentials held exclusively in this process's memory."""
    def __init__(self, issuer, client_id, response, *, clock=time.monotonic):
        self.issuer = issuer.rstrip("/")
        self.client_id = client_id
        self.clock = clock
        self.access_token = ""
        self.refresh_token = None
        self.expires_at = 0
        self._accept(response)

    def _accept(self, response):
        token, lifetime = response.get("access_token"), response.get("expires_in")
        if not isinstance(token, str) or not token or not isinstance(lifetime, (int, float)) or isinstance(lifetime, bool) or not math.isfinite(lifetime) or lifetime <= 0:
            raise SessionExpired("OAuth returned invalid or expired credentials; authenticate again")
        self.access_token = token
        self.refresh_token = response.get("refresh_token", self.refresh_token)
        self.expires_at = self.clock() + lifetime

    def token(self, *, force_refresh=False):
        if force_refresh or self.clock() >= self.expires_at - 60:
            if not self.refresh_token:
                self.access_token = ""
                raise SessionExpired("Session cannot renew; authenticate again")
            try:
                self._accept(token_request(self.issuer, {"grant_type": "refresh_token", "client_id": self.client_id,
                                                          "refresh_token": self.refresh_token}))
            except Exception:
                self.access_token = ""
                self.refresh_token = None
                self.expires_at = 0
                raise SessionExpired("Session renewal failed; authenticate again") from None
        return self.access_token


def token_request(issuer, fields):
    from .api import NoRedirect
    request = urllib.request.Request(issuer.rstrip("/") + "/oauth2/token",
                                     data=urllib.parse.urlencode(fields).encode())
    with urllib.request.build_opener(NoRedirect).open(request, timeout=30) as response:
        return json.load(response)


def browser_login(issuer: str, client_id: str, port: int = 8765) -> TokenSession:
    """Cognito hosted-domain PKCE login with an explicitly registered loopback URI."""
    if not issuer.startswith("https://"):
        raise ValueError("OAuth domain must use HTTPS")
    verifier = secrets.token_urlsafe(64)
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    state = secrets.token_urlsafe(32)
    redirect = f"http://localhost:{port}/callback"
    outcome = {}

    class Callback(BaseHTTPRequestHandler):
        def do_GET(self):
            parsed = urllib.parse.urlsplit(self.path)
            query = urllib.parse.parse_qs(parsed.query)
            valid = parsed.path == "/callback" and secrets.compare_digest(query.get("state", [""])[0], state)
            if valid:
                outcome.update(query)
            self.send_response(200 if valid else 400)
            self.end_headers()
            self.wfile.write(b"Authentication received. Return to the connector." if valid else b"Invalid callback.")

        def log_message(self, *_args):
            pass  # Authorization codes must never enter logs.

    url = issuer.rstrip("/") + "/oauth2/authorize?" + urllib.parse.urlencode({
        "response_type": "code", "client_id": client_id, "redirect_uri": redirect,
        "scope": "openid email", "state": state, "code_challenge": challenge,
        "code_challenge_method": "S256",
    })
    with HTTPServer(("127.0.0.1", port), Callback) as server:
        server.timeout = 1
        webbrowser.open(url)
        deadline = time.monotonic() + 180
        while not outcome and time.monotonic() < deadline:
            server.handle_request()
    if "code" not in outcome:
        raise RuntimeError("Browser authentication failed or timed out")
    response = token_request(issuer, {"grant_type": "authorization_code", "client_id": client_id,
                                     "code": outcome["code"][0], "redirect_uri": redirect,
                                     "code_verifier": verifier})
    return TokenSession(issuer, client_id, response)
