"""Regional OAuth bootstrap; CloudFront verifies signatures before serving assets.

No refresh tokens are retained. Encrypted host-only cookies expire in five minutes.
The signing private key exists only in regional memory and owner-managed SSM.
"""

import base64
import hashlib
import hmac
import json
import secrets
import time
from http.cookies import SimpleCookie
from urllib.error import HTTPError
from urllib.parse import parse_qs, urlencode
from urllib.request import Request, build_opener, HTTPRedirectHandler

import jwt
from cryptography.fernet import Fernet, InvalidToken
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa

from .api import APIError

TTL = 300
SESSION = "__Host-mesouq-session"
FLOW = "__Host-mesouq-oauth"
EDGE_COOKIES = (
    "CloudFront-Policy",
    "CloudFront-Signature",
    "CloudFront-Key-Pair-Id",
    "CloudFront-Hash-Algorithm",
)


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


class Cognito:
    def __init__(self, domain, issuer, client_id):
        self.domain, self.issuer, self.client_id = domain, issuer, client_id
        self.jwks = jwt.PyJWKClient(issuer + "/.well-known/jwks.json", timeout=4)

    def exchange(self, code, verifier, callback):
        request = Request(
            self.domain + "/oauth2/token",
            data=urlencode(
                {
                    "grant_type": "authorization_code",
                    "client_id": self.client_id,
                    "code": code,
                    "code_verifier": verifier,
                    "redirect_uri": callback,
                }
            ).encode(),
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        )
        # Never follow a redirect containing a code/verifier to another endpoint.
        try:
            with build_opener(NoRedirect).open(request, timeout=5) as response:
                return json.loads(response.read(32_769))
        except HTTPError as exc:
            # Expired/reused/rejected authorization codes require a new login,
            # not an infrastructure retry; never log the provider response body.
            if exc.code in (400, 401):
                raise ValueError("Authorization code rejected") from None
            raise

    def verify(self, token, *, id_token=False):
        key = self.jwks.get_signing_key_from_jwt(token).key
        claims = jwt.decode(
            token,
            key,
            algorithms=["RS256"],
            issuer=self.issuer,
            audience=self.client_id if id_token else None,
            options={
                "verify_aud": id_token,
                "require": ["exp", "iat", "sub", "token_use"],
            },
        )
        if claims["token_use"] != ("id" if id_token else "access") or (
            not id_token and claims.get("client_id") != self.client_id
        ):
            raise ValueError("Unexpected Cognito token")
        return claims


def cookie(name, value, age=TTL):
    deletion = "; Max-Age=0" if age == 0 else ""
    return f"{name}={value}; Path=/{deletion}; Secure; HttpOnly; SameSite=Lax"


def cf64(value):
    return base64.b64encode(value).decode().translate(str.maketrans("+=/", "-_~"))


class SiteAuth:
    def __init__(
        self, *, base_url, key_id, private_key, cognito, invited, clock=time.time
    ):
        self.base = base_url.rstrip("/")
        if not self.base.startswith("https://") or "/" in self.base[8:]:
            raise ValueError("Exact HTTPS site origin required")
        self.key_id, self.cognito, self.invited, self.clock = (
            key_id,
            cognito,
            invited,
            clock,
        )
        self.key = serialization.load_pem_private_key(private_key, password=None)
        if not isinstance(self.key, rsa.RSAPrivateKey) or self.key.key_size != 2048:
            raise ValueError("CloudFront requires the provisioned RSA2048 key")
        secret = self.key.private_bytes(
            serialization.Encoding.DER,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        )
        self.cipher = Fernet(
            base64.urlsafe_b64encode(
                hashlib.sha256(b"mesouq-site-session-v1\0" + secret).digest()
            )
        )

    def seal(self, value):
        return self.cipher.encrypt(
            json.dumps(value, separators=(",", ":")).encode()
        ).decode()

    def unseal(self, value):
        payload = json.loads(self.cipher.decrypt(value.encode()))
        if payload["expires_at"] <= self.clock():
            raise ValueError("Expired site session")
        return payload

    def edge(self, expires):
        policy = json.dumps(
            {
                "Statement": [
                    {
                        "Resource": self.base + "/*",
                        "Condition": {"DateLessThan": {"AWS:EpochTime": expires}},
                    }
                ]
            },
            separators=(",", ":"),
        ).encode()
        signature = self.key.sign(policy, padding.PKCS1v15(), hashes.SHA256())
        return [
            cookie("CloudFront-Policy", cf64(policy)),
            cookie("CloudFront-Signature", cf64(signature)),
            cookie("CloudFront-Key-Pair-Id", self.key_id),
            cookie("CloudFront-Hash-Algorithm", "SHA256"),
        ]

    def response(self, status, body=None, *, cookies=(), location=None):
        headers = {
            "content-type": "application/json",
            "cache-control": "no-store, private",
            "pragma": "no-cache",
            "referrer-policy": "no-referrer",
            "x-content-type-options": "nosniff",
        }
        if location:
            headers["location"] = location
        return {
            "statusCode": status,
            "headers": headers,
            "cookies": list(cookies),
            "body": json.dumps(body or {}),
        }

    def handle(self, event):
        path = event.get("rawPath")
        method = event.get("requestContext", {}).get("http", {}).get("method")
        headers = {k.lower(): v for k, v in event.get("headers", {}).items()}
        jar = SimpleCookie()
        try:
            jar.load("; ".join(event.get("cookies", [])))
            now = int(self.clock())
            if path == "/auth/login" and method == "GET":
                state, nonce, verifier = (secrets.token_urlsafe(32) for _ in range(3))
                flow = self.seal(
                    {
                        "state": state,
                        "nonce": nonce,
                        "verifier": verifier,
                        "expires_at": now + TTL,
                    }
                )
                query = urlencode(
                    {
                        "response_type": "code",
                        "client_id": self.cognito.client_id,
                        "scope": "openid email profile",
                        "redirect_uri": self.base + "/auth/callback",
                        "state": state,
                        "nonce": nonce,
                        "code_challenge_method": "S256",
                        "code_challenge": base64.urlsafe_b64encode(
                            hashlib.sha256(verifier.encode()).digest()
                        )
                        .decode()
                        .rstrip("="),
                    }
                )
                return self.response(
                    302,
                    cookies=[cookie(FLOW, flow)],
                    location=self.cognito.domain + "/oauth2/authorize?" + query,
                )
            if path == "/auth/callback" and method == "GET":
                query = parse_qs(event.get("rawQueryString", ""), strict_parsing=True)
                if set(query) != {"code", "state"} or any(
                    len(v) != 1 or not v[0] or len(v[0]) > 4096 for v in query.values()
                ):
                    raise ValueError("Invalid OAuth callback")
                flow = self.unseal(jar[FLOW].value)
                if not hmac.compare_digest(flow["state"], query["state"][0]):
                    raise ValueError("Invalid OAuth state")
                tokens = self.cognito.exchange(
                    query["code"][0], flow["verifier"], self.base + "/auth/callback"
                )
                identity = self.cognito.verify(tokens["id_token"], id_token=True)
                access = self.cognito.verify(tokens["access_token"])
                if (
                    identity.get("nonce") != flow["nonce"]
                    or identity["sub"] != access["sub"]
                ):
                    raise ValueError("Invalid OAuth nonce/subject")
                if not self.invited(access["sub"]):
                    raise APIError(
                        403, "not_invited", "An active invitation is required"
                    )
                expires = min(now + TTL, int(access["exp"]))
                if expires <= now:
                    raise ValueError("Expired token")
                session = self.seal(
                    {"access_token": tokens["access_token"], "expires_at": expires}
                )
                if len(session) > 3800:
                    raise ValueError("Session exceeds cookie limit")
                return self.response(
                    302,
                    cookies=[
                        cookie(FLOW, "", 0),
                        cookie(SESSION, session, expires - now),
                        *self.edge(expires),
                    ],
                    location=self.base + "/",
                )
            if path == "/auth/session" and method == "GET":
                session = self.unseal(jar[SESSION].value)
                access = self.cognito.verify(session["access_token"])
                if not self.invited(access["sub"]):
                    raise APIError(
                        403, "not_invited", "An active invitation is required"
                    )
                return self.response(200, session)
            if path == "/auth/logout" and method == "POST":
                if headers.get("origin") != self.base:
                    raise APIError(403, "invalid_origin", "Same-origin logout required")
                target = (
                    self.cognito.domain
                    + "/logout?"
                    + urlencode(
                        {
                            "client_id": self.cognito.client_id,
                            "logout_uri": self.base + "/auth/login",
                        }
                    )
                )
                return self.response(
                    200,
                    {"logout_url": target},
                    cookies=[
                        cookie(name, "", 0) for name in (SESSION, FLOW, *EDGE_COOKIES)
                    ],
                )
            return self.response(
                404,
                {
                    "error": {
                        "code": "not_found",
                        "message": "Unknown authentication endpoint",
                    }
                },
            )
        except APIError as exc:
            return self.response(
                exc.status,
                {"error": {"code": exc.code, "message": exc.message}},
                cookies=[cookie(FLOW, "", 0)],
            )
        except (ValueError, KeyError, TypeError, InvalidToken, jwt.PyJWTError):
            return self.response(
                401,
                {
                    "error": {
                        "code": "unauthenticated",
                        "message": "Start a new sign-in at /auth/login",
                    }
                },
                cookies=[cookie(FLOW, "", 0)],
            )
