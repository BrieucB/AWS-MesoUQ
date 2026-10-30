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
