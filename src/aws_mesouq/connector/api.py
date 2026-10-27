"""Small bearer-authenticated v1 client, with origin-safe artifact transfers."""
from __future__ import annotations

import json
import urllib.parse
import urllib.error
import urllib.request


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise ValueError("Redirects are forbidden for authenticated and signed requests")


class APIError(RuntimeError):
    def __init__(self, status, code, message):
        self.status, self.code = status, code
        super().__init__(message)


class API:
    def __init__(self, base_url: str, token: str):
        if not base_url.startswith("https://"):
            raise ValueError("Cloud API must use HTTPS")
        self.base = base_url.rstrip("/")
        self.token = token

    def request(self, method: str, path: str, payload=None):
        if not path.startswith("/v1/"):
            raise ValueError("Expected canonical v1 route")
        body = None if payload is None else json.dumps(payload, allow_nan=False).encode()
        for attempt in range(2):
            token = self.token.token() if hasattr(self.token, "token") else self.token
            request = urllib.request.Request(self.base + path, data=body, method=method,
                                             headers={"Authorization": "Bearer " + token, "Content-Type": "application/json"})
            try:
                with urllib.request.build_opener(NoRedirect).open(request, timeout=30) as response:
                    return json.load(response)
            except urllib.error.HTTPError as exc:
                if exc.code == 401 and attempt == 0 and hasattr(self.token, "token"):
                    self.token.token(force_refresh=True)
                    continue
                try:
                    error = json.loads(exc.read()).get("error", {})
                except (ValueError, AttributeError):
                    error = {}
                raise APIError(exc.code, error.get("code", "http_error"),
                               error.get("message", "Cloud API rejected the request")) from None

    @staticmethod
    def transfer(url: str, *, data: bytes | None = None, headers=None) -> bytes:
        if urllib.parse.urlsplit(url).scheme != "https":
            raise ValueError("Artifact transfer requires HTTPS")
        # No bearer token is ever forwarded to the object-storage URL.
        request = urllib.request.Request(url, data=data, method="GET" if data is None else "PUT", headers=headers or {})
        with urllib.request.build_opener(NoRedirect).open(request, timeout=90) as response:
            return response.read()
