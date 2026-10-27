"""Invoke a locally built Lambda image through its real Runtime Interface Emulator.

For a published local port, run from the repository root with the default port9000.
For a container on an internal no-egress network, copy this file into /tmp and run
it with the image's Python via docker exec and --base-url http://127.0.0.1:8080.
The HTTP client must share the emulator's network namespace in that case.
Only loopback destinations are accepted. This client never supplies an authorizer
or accesses AWS; preflight is empty, /health is public, and actual API methods
must retain their authentication guards.
"""

from __future__ import annotations

import argparse
import json
import time
from urllib.error import URLError
from urllib.parse import urlsplit
from urllib.request import Request, HTTPRedirectHandler, ProxyHandler, build_opener


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise ValueError("The local Lambda emulator must not redirect requests")


# Environment proxies and redirects must not turn a loopback smoke into egress.
LOCAL_OPENER = build_opener(ProxyHandler({}), NoRedirect()).open


def invocation_url(base_url: str) -> str:
    parsed = urlsplit(base_url)
    if (
        parsed.scheme != "http"
        or parsed.hostname not in ("127.0.0.1", "localhost")
        or parsed.username
        or parsed.password
        or parsed.path not in ("", "/")
        or parsed.query
        or parsed.fragment
    ):
        raise ValueError(
            "The container smoke destination must be an HTTP loopback origin"
        )
    return base_url.rstrip("/") + "/2015-03-31/functions/function/invocations"


def event(path: str, method: str = "GET") -> dict:
    route = method + " " + path
    if path.startswith("/v1/"):
        route = ("OPTIONS" if method == "OPTIONS" else "ANY") + " /v1/{proxy+}"
    return {
        "version": "2.0",
        "routeKey": route,
        "rawPath": path,
        "rawQueryString": "",
        "headers": {},
        "isBase64Encoded": False,
        "requestContext": {
            "requestId": "local-container-smoke",
            "stage": "$default",
            "http": {
                "method": method,
                "path": path,
                "protocol": "HTTP/1.1",
                "sourceIp": "127.0.0.1",
                "userAgent": "local-container-smoke",
            },
        },
    }


def invoke(
    url: str,
    payload: dict,
    *,
    opener=LOCAL_OPENER,
    clock=time.monotonic,
    pause=time.sleep,
    wait_seconds=45,
) -> dict:
    request = Request(
        url,
        data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    deadline = clock() + wait_seconds
    while True:
        try:
            with opener(request, timeout=2) as response:
                if response.status != 200:
                    raise AssertionError(
                        "Lambda emulator did not accept the invocation"
                    )
                result = json.loads(response.read(100_000))
            if not isinstance(result, dict) or not isinstance(result.get("body"), str):
                raise AssertionError(
                    "Lambda image returned an invocation error instead of an HTTP API response"
                )
            return result
        except (URLError, TimeoutError, ConnectionError) as exc:
            if clock() >= deadline:
                raise TimeoutError(
                    "Lambda emulator did not become ready before the deadline"
                ) from exc
            pause(0.25)


def smoke(base_url: str, *, invoker=invoke) -> None:
    url = invocation_url(base_url)
    # Invoke preflight first, before health initializes the application clients.
    preflight = event("/v1/me", "OPTIONS")
    preflight["headers"] = {
        "origin": "https://portal.example.invalid",
        "access-control-request-method": "GET",
        "access-control-request-headers": "authorization",
    }
    empty = invoker(url, preflight)
    assert empty.get("statusCode") == 204 and empty["body"] == "", "Image rejected preflight"
    assert not any(key.lower().startswith("access-control-") for key in empty.get("headers", {})), "Gateway must own CORS headers"
    healthy = invoker(url, event("/health"))
    assert (
        healthy.get("statusCode") == 200
    ), "Real image /health returned an unsuccessful status"
    assert json.loads(healthy["body"]) == {"status": "ok"}, "Unexpected /health payload"
    assert healthy.get("headers", {}).get("cache-control") == "no-store"
    denied = invoker(url, event("/v1/me"))
    assert denied.get("statusCode") == 401, "Image bypassed authentication for /v1/me"
    assert json.loads(denied["body"]).get("error", {}).get("code") == "unauthenticated"
    denied_post = invoker(url, event("/v1/projects/synthetic/jobs", "POST"))
    assert denied_post.get("statusCode") == 401, "Image bypassed authentication for POST"
    assert json.loads(denied_post["body"]).get("error", {}).get("code") == "unauthenticated"


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", default="http://127.0.0.1:9000")
    smoke(parser.parse_args().base_url)
    print("Real Lambda image preflight, health and authentication smoke passed")
