"""Offline OAuth and CloudFront cryptographic boundary regression tests."""

import base64
import json
import re
from urllib.parse import parse_qs, urlsplit, urlencode

import jwt
import pytest
from cryptography.exceptions import InvalidSignature
from cryptography.fernet import InvalidToken
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa

from aws_publishing.site_auth import SiteAuth, Cognito, FLOW, SESSION, EDGE_COOKIES, INTENT, REMEMBERED


class FakeCognito:
    client_id = "client"
    domain = "https://invited.auth.eu-central-1.amazoncognito.com"

    def __init__(self):
        self.nonce = ""
        self.calls = []

    def exchange(self, code, verifier, callback):
        self.calls.append((code, verifier, callback))
        return {
            "id_token": "identity",
            "access_token": "access-secret",
            "refresh_token": "never-retained",
        }

    def verify(self, token, *, id_token=False):
        return {"sub": "invited", "nonce": self.nonce, "exp": 2000}


@pytest.fixture
def auth():
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    pem = key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    )
    return SiteAuth(
        base_url="https://private.cloudfront.net",
        key_id="KPUBLIC",
        private_key=pem,
        cognito=FakeCognito(),
        invited=lambda subject: subject == "invited",
        clock=lambda: 1000,
    )


def event(path, method="GET", *, cookies=(), query="", headers=None):
    return {
        "rawPath": path,
        "rawQueryString": query,
        "requestContext": {"http": {"method": method}},
        "cookies": list(cookies),
        "headers": headers or {},
    }


def login(auth):
    landing = auth.handle(event("/auth/login"))
    assert landing["statusCode"] == 200
    nonce = re.search('name="csrf" value="([^"]+)"',landing["body"])[1]
    intent = next(c.split(";",1)[0] for c in landing["cookies"] if c.startswith(INTENT+"="))
    request = event("/auth/login","POST",cookies=[intent],
        headers={"Origin":auth.base,"Content-Type":"application/x-www-form-urlencoded"})
    request["body"] = urlencode({"csrf":nonce})
    response = auth.handle(request)
    assert response["statusCode"] == 302
    query = parse_qs(urlsplit(response["headers"]["location"]).query)
    auth.cognito.nonce = query["nonce"][0]
    flow = response["cookies"][0].split(";", 1)[0]
    return query, flow


def callback(auth):
    query, flow = login(auth)
    return auth.handle(
        event(
            "/auth/callback",
            cookies=[flow],
            query="code=one-use-code&state=" + query["state"][0],
        )
    )


def test_callback_signatures_exact_resource_and_encrypted_token_custody(auth):
    response = callback(auth)
    assert response["statusCode"] == 302
    assert response["headers"]["location"] == auth.base + "/"
    assert "access-secret" not in json.dumps(response)
    assert "never-retained" not in json.dumps(response)
    values = dict(item.split(";", 1)[0].split("=", 1) for item in response["cookies"])
    decode = lambda value: base64.b64decode(
        value.translate(str.maketrans("-_~", "+=/"))
    )
    policy = decode(values["CloudFront-Policy"])
    auth.key.public_key().verify(
        decode(values["CloudFront-Signature"]),
        policy,
        padding.PKCS1v15(),
        hashes.SHA256(),
    )
    with pytest.raises(InvalidSignature):
        auth.key.public_key().verify(
            decode(values["CloudFront-Signature"]),
            policy.replace(b"private.cloudfront.net", b"other.cloudfront.net"),
            padding.PKCS1v15(),
            hashes.SHA256(),
        )
    assert json.loads(policy) == {
        "Statement": [
            {
                "Resource": auth.base + "/*",
                "Condition": {"DateLessThan": {"AWS:EpochTime": 1300}},
            }
        ]
    }
    assert values["CloudFront-Hash-Algorithm"] == "SHA256"
    for item in response["cookies"]:
        assert (
            "Path=/;" in item
            and "Secure; HttpOnly; SameSite=Lax" in item
            and "Domain=" not in item
        )
    assert all(
        "Max-Age=" not in value and "Expires=" not in value
        for value in response["cookies"]
        if not value.startswith(FLOW + "=") and "Max-Age=0" not in value
    )
    session = auth.handle(
        event("/auth/session", cookies=[SESSION + "=" + values[SESSION]])
    )
    assert json.loads(session["body"]) == {
        "access_token": "access-secret",
        "expires_at": 1300,
    }
    assert session["headers"]["cache-control"] == "no-store, private"
    assert "access-control-allow-origin" not in session["headers"]
    assert auth.cognito.calls[0][2] == auth.base + "/auth/callback"


@pytest.mark.parametrize(
    "query",
    [
        "code=x&state=bad",
        "code=x&state=a&state=b",
        "code=x&state=a&redirect_uri=https://evil.example",
        "error=denied",
        "code=&state=a",
    ],
)
def test_callback_csrf_rejects_before_token_exchange(auth, query):
    _, flow = login(auth)
    assert (
        auth.handle(event("/auth/callback", cookies=[flow], query=query))["statusCode"]
        == 401
    )
    assert not auth.cognito.calls


def test_missing_tampered_expired_flow_and_nonce(auth):
    query, flow = login(auth)
    callback_event = event("/auth/callback", query="code=x&state=" + query["state"][0])
    assert auth.handle(callback_event)["statusCode"] == 401
    callback_event["cookies"] = [flow + "tamper"]
    assert auth.handle(callback_event)["statusCode"] == 401
    callback_event["cookies"] = [flow]
    auth.cognito.nonce = "different"
    assert auth.handle(callback_event)["statusCode"] == 401
    auth.clock = lambda: 1300
    assert auth.handle(callback_event)["statusCode"] == 401


def test_membership_checked_callback_and_session_and_expiration(auth):
    auth.invited = lambda subject: False
    assert callback(auth)["statusCode"] == 403
    auth.invited = lambda subject: True
    response = callback(auth)
    session = next(
        c.split(";", 1)[0] for c in response["cookies"] if c.startswith(SESSION + "=")
    )
    auth.invited = lambda subject: False
    assert auth.handle(event("/auth/session", cookies=[session]))["statusCode"] == 403
    auth.invited = lambda subject: True
    auth.clock = lambda: 1300
    assert auth.handle(event("/auth/session", cookies=[session]))["statusCode"] == 401


def test_logout_origin_and_complete_cookie_clear(auth):
    assert auth.handle(event("/auth/logout", "POST"))["statusCode"] == 403
    assert (
        auth.handle(
            event("/auth/logout", "POST", headers={"Origin": "https://evil.example"})
        )["statusCode"]
        == 403
    )
    response = auth.handle(event("/auth/logout", "POST", headers={"Origin": auth.base}))
    assert response["statusCode"] == 200
    assert len(response["cookies"]) == len(EDGE_COOKIES) + 4
    assert all("Max-Age=0" in value for value in response["cookies"])
    target = parse_qs(urlsplit(json.loads(response["body"])["logout_url"]).query)
    assert target["logout_uri"] == [auth.base + "/auth/login"]
    assert auth.handle(event("/auth/index.html"))["statusCode"] == 404


def test_real_cognito_jwt_verifies_signature_issuer_client_use(auth):
    key = auth.key
    provider = Cognito("https://login.example", "https://issuer.example", "client")

    class Keys:
        def get_signing_key_from_jwt(self, token):
            class Key:
                pass

            result = Key()
            result.key = key.public_key()
            return result

    provider.jwks = Keys()
    import time

    claims = {
        "iss": provider.issuer,
        "sub": "invited",
        "iat": int(time.time()),
        "exp": int(time.time()) + 60,
        "token_use": "access",
        "client_id": "client",
    }
    encode = lambda value: jwt.encode(value, key, algorithm="RS256")
    assert provider.verify(encode(claims))["sub"] == "invited"
    for changes in (
        {"iss": "https://evil.example"},
        {"client_id": "other"},
        {"token_use": "id"},
        {"exp": 0},
    ):
        with pytest.raises((jwt.PyJWTError, ValueError)):
            provider.verify(encode({**claims, **changes}))


def test_token_exchange_rejects_expired_code_without_provider_body(monkeypatch):
    from urllib.error import HTTPError
    from aws_publishing import site_auth

    provider = Cognito("https://login.example", "https://issuer.example", "client")

    class Opener:
        def open(self, request, timeout):
            assert request.full_url == provider.domain + "/oauth2/token"
            assert timeout == 5
            raise HTTPError(request.full_url, 400, "sensitive upstream error", {}, None)

    monkeypatch.setattr(site_auth, "build_opener", lambda *args: Opener())
    with pytest.raises(ValueError, match="Authorization code rejected") as caught:
        provider.exchange(
            "one-use-code", "verifier", "https://private.cloudfront.net/auth/callback"
        )
    assert "sensitive" not in str(caught.value)


def test_sealed_cookie_rejects_appended_data_for_every_base64_padding_length(auth):
    tokens=[auth.seal({"expires_at":2000,"padding":"x"*size}) for size in (0,16,32)]
    assert len({len(value)-len(value.rstrip("=")) for value in tokens})==3
    for value in tokens:
        assert auth.unseal(value)["expires_at"]==2000
        for suffix in ("tamper","=","A","AA"):
            with pytest.raises((ValueError,InvalidToken)):
                auth.unseal(value+suffix)
    for value in ("é"*20,"x"*8193):
        with pytest.raises(ValueError):
            auth.unseal(value)


@pytest.mark.parametrize("origin", [None, "null", "https://other.example"])
def test_login_rejects_non_exact_origin_before_form_intent(auth, origin):
    headers = {"Content-Type": "application/x-www-form-urlencoded"}
    if origin is not None:
        headers["Origin"] = origin
    request = event("/auth/login", "POST", headers=headers)
    request["body"] = "csrf=synthetic"
    response = auth.handle(request)
    assert response["statusCode"] == 403
    assert json.loads(response["body"])["error"]["code"] == "invalid_origin"
    assert not auth.cognito.calls


def test_real_browser_login_form_preserves_origin_and_cross_origin_privacy(auth, tmp_path):
    """Actual form navigation under production no-referrer headers; local fixtures only."""
    import base64
    import hashlib
    import os
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
    from pathlib import Path
    import socket
    import subprocess
    import threading
    import time

    binary = os.environ.get("AWS_MESOUQ_LOGIN_TEST_FIREFOX")
    if binary is None:
        pytest.skip("Allocated QA supplies the exact Firefox executable for real form navigation")
    firefox = Path(binary)
    assert firefox == Path("/apps/all/Firefox/143.0.4/firefox")
    assert hashlib.sha256(firefox.read_bytes()).hexdigest() == (
        "a63284a9bf941faf503cb589234c6c3913a35293fcbb6411e327395824cc5584")
    output = Path(os.environ.get("AWS_MESOUQ_LOGIN_BROWSER_OUTPUT", str(tmp_path / "browser")))
    output.mkdir(parents=True, mode=0o700)
    observed = {"synthetic_fixture_only": True, "oauth_or_business_requests": False,
                "user_credentials_or_session_state_inspected": False}
    deadline = time.monotonic() + 90

    class QuietHandler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def reply(self, body, headers=None):
            raw = body.encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(raw)))
            self.send_header("Referrer-Policy", "no-referrer")
            self.send_header("Content-Security-Policy", "default-src 'self'; script-src 'self'; "
                             "style-src 'self'; frame-ancestors 'none'; base-uri 'self'; object-src 'none'")
            self.end_headers()
            self.wfile.write(raw)
            self.close_connection = True

    class ExternalHandler(QuietHandler):
        def do_GET(self):
            observed["cross_origin_referer_absent"] = self.headers.get("Referer") is None
            self.reply("<!doctype html><title>External synthetic fixture</title><h1>Complete</h1>")

    external = ThreadingHTTPServer(("127.0.0.1", 0), ExternalHandler)
    external_url = "http://127.0.0.1:" + str(external.server_port) + "/landing"

    class LoginHandler(QuietHandler):
        def do_GET(self):
            # Actual generated product HTML. No cookies or sealed intent are exported
            # or supplied to Firefox; this fixture exercises the Origin boundary only.
            response = auth.handle(event("/auth/login"))
            assert response["statusCode"] == 200
            assert response["headers"]["referrer-policy"] == "no-referrer"
            body = response["body"] + '<a id="external-fixture" href="' + external_url + '">External fixture</a>'
            self.reply(body)

        def do_POST(self):
            origin = self.headers.get("Origin")
            observed["form_origin_class"] = (
                "exact" if origin == auth.base else "null" if origin == "null"
                else "missing" if origin is None else "other")
            try:
                # Exercise the unchanged product guard, not an imitation of its rule.
                auth._origin({"origin": origin})
            except Exception as error:
                observed["origin_guard_allowed"] = False
                observed["origin_guard_error_code"] = getattr(error, "code", "unexpected")
            else:
                observed["origin_guard_allowed"] = True
            self.reply("<!doctype html><title>Synthetic form result</title><h1 id='done'>Observed</h1>")

    server = ThreadingHTTPServer(("127.0.0.1", 0), LoginHandler)
    auth.base = "http://127.0.0.1:" + str(server.server_port)
    login_url = auth.base + "/auth/login"
    threads = [threading.Thread(target=item.serve_forever, daemon=True) for item in (server, external)]
    for thread in threads:
        thread.start()
    profile = tmp_path / "fresh-firefox-profile"
    profile.mkdir(mode=0o700)
    with socket.socket() as reservation:
        reservation.bind(("127.0.0.1", 0))
        port = reservation.getsockname()[1]
    (profile / "user.js").write_text(
        'user_pref("marionette.port", ' + str(port) + ');\n'
        'user_pref("browser.shell.checkDefaultBrowser", false);\n'
        'user_pref("datareporting.healthreport.uploadEnabled", false);\n'
        'user_pref("toolkit.telemetry.enabled", false);\n')
    environment = {"PATH": "/usr/bin:/bin", "LD_LIBRARY_PATH": "/apps/all/GCCcore/14.3.0/lib64",
                   "LANG": "C.UTF-8", "MOZ_HEADLESS": "1", "MOZ_CRASHREPORTER_DISABLE": "1",
                   "XDG_CACHE_HOME": str(profile / "cache"), "XDG_CONFIG_HOME": str(profile / "config")}
    sock = process = None
    logfile = (output / "firefox.log").open("xb")
    os.chmod(output / "firefox.log", 0o600)
    try:
        process = subprocess.Popen(
            [str(firefox), "--headless", "--no-remote", "--profile", str(profile),
             "--marionette", "--remote-allow-system-access"],
            env=environment, stdout=logfile, stderr=logfile, start_new_session=True)
        for _ in range(150):
            assert process.poll() is None, "The synthetic Firefox exited before readiness"
            try:
                sock = socket.create_connection(("127.0.0.1", port), timeout=.5)
                break
            except OSError:
                time.sleep(.1)
        assert sock is not None, "The synthetic Firefox readiness deadline expired"
        pending = b""
        sequence = 0

        def receive():
            nonlocal pending
            while b":" not in pending:
                block = sock.recv(65536)
                assert block and len(pending) <= 32, "Invalid bounded Marionette framing"
                pending += block
            prefix, tail = pending.split(b":", 1)
            assert prefix.isdigit() and len(prefix) <= 10
            size = int(prefix)
            assert 0 < size <= 20 * 1024 * 1024
            while len(tail) < size:
                block = sock.recv(min(65536, size - len(tail)))
                assert block, "Incomplete Marionette frame"
                tail += block
            body, pending = tail[:size], tail[size:]
            return json.loads(body)

        def command(name, args=None):
            nonlocal sequence
            remaining = deadline - time.monotonic()
            assert remaining > 0, "Synthetic browser proof exceeded its observation budget"
            sock.settimeout(min(15, remaining))
            sequence += 1
            raw = json.dumps([0, sequence, name, args or {}]).encode()
            sock.sendall(str(len(raw)).encode() + b":" + raw)
            response = receive()
            assert response[:2] == [1, sequence] and not response[2], "Synthetic browser command failed"
            result = response[3]
            return result.get("value", result) if isinstance(result, dict) else result

        def execute(script):
            return command("WebDriver:ExecuteScript", {"script": script, "args": []})

        def click(selector):
            found = command("WebDriver:FindElement", {"using": "css selector", "value": selector})
            identifier = found.get("element-6066-11e4-a52e-4f735466cecf", found.get("ELEMENT"))
            assert identifier
            command("WebDriver:ElementClick", {"id": identifier})

        def wait_for(key):
            for _ in range(100):
                if key in observed:
                    return
                time.sleep(.05)
            raise AssertionError("Synthetic browser did not produce the expected observation: " + key)

        sock.settimeout(5)
        receive()
        command("WebDriver:NewSession", {"capabilities": {"alwaysMatch": {}}})
        command("WebDriver:SetWindowRect", {"width": 1440, "height": 1000, "x": 0, "y": 0})
        command("WebDriver:Navigate", {"url": login_url})
        observed["effective_document_referrer_policy"] = execute("return document.referrerPolicy")
        observed["response_header_referrer_policy"] = "no-referrer"
        png = base64.b64decode(command("WebDriver:TakeScreenshot",
                               {"id": None, "highlights": [], "full": True}), validate=True)
        assert png.startswith(b"\x89PNG\r\n\x1a\n")
        (output / "synthetic-login.png").write_bytes(png)
        observed["screenshot_sha256"] = hashlib.sha256(png).hexdigest()
        click('form[action="/auth/login"] button[type="submit"]')
        wait_for("form_origin_class")
        command("WebDriver:Navigate", {"url": login_url})
        click("#external-fixture")
        wait_for("cross_origin_referer_absent")
        observed["cognito_exchange_calls"] = len(auth.cognito.calls)
        observed["source_sha256"] = hashlib.sha256(Path(__import__(
            "aws_publishing.site_auth", fromlist=["__file__"]).__file__).read_bytes()).hexdigest()
        (output / "browser-evidence.json").write_text(json.dumps(observed, indent=2, sort_keys=True) + "\n")
        (output / "browser-evidence.md").write_text(
            "# Synthetic login-form browser proof\n\n"
            "Actual Firefox ordinary form navigation under the production no-referrer response header. "
            "The fixture exercises the unchanged Origin guard, never OAuth or a business endpoint.\n\n"
            "![Generated login page served by the local synthetic fixture](synthetic-login.png)\n\n"
            "Observed form Origin class: " + observed["form_origin_class"] + ". "
            "Cross-origin Referer absent: " + str(observed["cross_origin_referer_absent"]) + ".\n")
        assert observed["form_origin_class"] == "exact", (
            "Ordinary form POST did not preserve exact Origin; observed " + observed["form_origin_class"])
        assert observed["origin_guard_allowed"] is True
        assert observed["cross_origin_referer_absent"] is True
        assert observed["cognito_exchange_calls"] == 0
    finally:
        if sock is not None:
            try:
                command("Marionette:Quit", {"flags": ["eForceQuit"]})
            except Exception:
                pass
            sock.close()
        if process is not None:
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.terminate()
                try:
                    process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=5)
        logfile.close()
        for item in (server, external):
            item.shutdown()
            item.server_close()
        for thread in threads:
            thread.join(timeout=2)
