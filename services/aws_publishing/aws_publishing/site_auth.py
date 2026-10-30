"""Regional OAuth bootstrap; CloudFront verifies signatures before serving assets.

Legacy sessions expire in five minutes. Explicit remembered browsers retain only
opaque host-only cookies; encrypted server credentials have finite idle/absolute caps.
The signing private key exists only in regional memory and owner-managed SSM.
"""

import base64
import hashlib
import hmac
import html
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
from .browser_sessions import (BrowserSessions, SessionExpired, RefreshBusy, RefreshUnavailable)

TTL = 300
SESSION = "__Host-mesouq-session"
FLOW = "__Host-mesouq-oauth"
INTENT = "__Host-mesouq-login-intent"
REMEMBERED = "__Host-mesouq-remembered"
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

    def _token_post(self, path, fields, *, empty=False):
        request = Request(self.domain+path,data=urlencode(fields).encode(),
            headers={"Content-Type":"application/x-www-form-urlencoded"})
        try:
            with build_opener(NoRedirect).open(request,timeout=5) as response:
                raw = response.read(32769)
                if len(raw)>32768:
                    raise ValueError("Identity response exceeded limit")
                return {} if empty and not raw else json.loads(raw)
        except HTTPError as error:
            if error.code in (400,401):
                if empty:
                    return {}
                raise ValueError("Session renewal rejected") from None
            raise

    def refresh(self, refresh_token):
        return self._token_post("/oauth2/token",{"grant_type":"refresh_token",
            "client_id":self.client_id,"refresh_token":refresh_token})

    def revoke(self, refresh_token):
        self._token_post("/oauth2/revoke",{"client_id":self.client_id,
            "token":refresh_token},empty=True)

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


def strict_object(pairs):
    result = {}
    for key,value in pairs:
        if key in result:
            raise ValueError("Duplicate authentication body field")
        result[key] = value
    return result


def cf64(value):
    return base64.b64encode(value).decode().translate(str.maketrans("+=/", "-_~"))


class SiteAuth:
    def __init__(
        self, *, base_url, key_id, private_key, cognito, invited, clock=time.time,
        session_records=None, remembered_cognito=None, remembered_enabled=True
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
        if (session_records is None)!=(remembered_cognito is None):
            raise ValueError("Remembered provider and record store must be configured together")
        if type(remembered_enabled) is not bool:
            raise ValueError("Literal remembered-browser feature flag required")
        self.remembered_enabled = remembered_enabled
        self.remembered = None
        if session_records is not None:
            cipher = Fernet(base64.urlsafe_b64encode(
                hashlib.sha256(b"mesouq-remembered-browser-v1\0"+secret).digest()))
            self.remembered = BrowserSessions(session_records,cipher,remembered_cognito,
                                              invited,clock=clock)

    def seal(self, value):
        return self.cipher.encrypt(
            json.dumps(value, separators=(",", ":")).encode()
        ).decode()

    def unseal(self, value):
        payload = json.loads(self.cipher.decrypt(value.encode()))
        if payload["expires_at"] <= self.clock():
            raise ValueError("Expired site session")
        return payload

    def edge(self, expires, *, remembered=False):
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
        values = [
            cookie("CloudFront-Policy", cf64(policy)),
            cookie("CloudFront-Signature", cf64(signature)),
            cookie("CloudFront-Key-Pair-Id", self.key_id),
            cookie("CloudFront-Hash-Algorithm", "SHA256"),
        ]
        if remembered:
            age = int(expires-self.clock())
            if not 0 < age <= TTL:
                raise SessionExpired("Finite remembered asset grant required")
            # The entry hook checks presence only. Expired remembered grants must
            # disappear from the browser; CloudFront still verifies every signature.
            values = [value+"; Max-Age="+str(age) for value in values]
        return values

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

    def _provider(self, remember):
        if remember:
            if self.remembered is None or not self.remembered_enabled:
                raise APIError(503,"remembered_unavailable","Remembered browser sign-in is not enabled")
            return self.remembered.provider
        return self.cognito

    def _begin(self, remember):
        provider = self._provider(remember)
        now = int(self.clock())
        state,nonce,verifier = (secrets.token_urlsafe(32) for _ in range(3))
        flow = self.seal({"state":state,"nonce":nonce,"verifier":verifier,
            "remember":remember,"client_id":provider.client_id,"expires_at":now+TTL})
        query = urlencode({"response_type":"code","client_id":provider.client_id,
            "scope":"openid email profile","redirect_uri":self.base+"/auth/callback",
            "state":state,"nonce":nonce,"code_challenge_method":"S256",
            "code_challenge":base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).decode().rstrip("=")})
        return self.response(302,cookies=[cookie(FLOW,flow),cookie(INTENT,"",0)],
            location=provider.domain+"/oauth2/authorize?"+query)

    def _login_page(self):
        nonce = secrets.token_urlsafe(32)
        intent = self.seal({"intent_nonce":nonce,"expires_at":int(self.clock())+TTL})
        disabled = "" if self.remembered is not None and self.remembered_enabled else " disabled"
        document = (
            '<!doctype html><html lang="en"><meta charset="utf-8">'
            '<meta name="viewport" content="width=device-width,initial-scale=1">'
            '<title>Sign in · MesoUQ</title><main><h1>Sign in to MesoUQ</h1>'
            '<p>Private project · Invited MFA accounts</p>'
            '<form method="post" action="/auth/login">'
            '<input type="hidden" name="csrf" value="'+html.escape(nonce,quote=True)+'">'
            '<label><input type="checkbox" name="remember" value="1"'+disabled+'>'
            ' Keep me signed in on this browser</label>'
            '<p>Optional: up to 30 days, ending after 7 days without activity. '
            'Use only a browser you control. MFA remains required for sign-in.</p>'
            '<button type="submit">Continue to secure sign-in</button></form></main></html>')
        result = self.response(200,cookies=[cookie(INTENT,intent)])
        result["headers"]["content-type"] = "text/html; charset=utf-8"
        result["body"] = document
        return result

    def _origin(self, headers):
        if headers.get("origin")!=self.base:
            raise APIError(403,"invalid_origin","Exact same-origin request required")

    def _body(self, event, *, json_body):
        raw = event.get("body") or ""
        if event.get("isBase64Encoded"):
            raw = base64.b64decode(raw,validate=True).decode()
        if not isinstance(raw,str) or len(raw.encode())>4096:
            raise ValueError("Bounded authentication request required")
        headers = {k.lower():v for k,v in event.get("headers",{}).items()}
        content = headers.get("content-type","").split(";",1)[0].strip().lower()
        if json_body:
            if content!="application/json":
                raise ValueError("JSON authentication request required")
            return json.loads(raw,object_pairs_hook=strict_object,
                parse_constant=lambda value:(_ for _ in ()).throw(ValueError("Nonfinite body")))
        if content!="application/x-www-form-urlencoded":
            raise ValueError("Encoded login form required")
        values = parse_qs(raw,strict_parsing=True,keep_blank_values=True)
        if any(len(v)!=1 for v in values.values()):
            raise ValueError("Unique login fields required")
        return {key:value[0] for key,value in values.items()}

    def _remember_cookie(self, sid, session):
        age = int(min(session["idle_expires_at"],session["absolute_expires_at"])-self.clock())
        if age<=0:
            raise SessionExpired("Remembered browser session expired")
        return cookie(REMEMBERED,sid)+"; Max-Age="+str(age)

    def _legacy_session(self, jar):
        session = self.unseal(jar[SESSION].value)
        access = self.cognito.verify(session["access_token"])
        if not self.invited(access["sub"]):
            raise APIError(403,"not_invited","An active invitation is required")
        return session

    def _clear(self):
        return [cookie(name,"",0) for name in (SESSION,FLOW,INTENT,REMEMBERED,*EDGE_COOKIES)]

    def handle(self, event):
        path = event.get("rawPath")
        method = event.get("requestContext", {}).get("http", {}).get("method")
        headers = {k.lower(): v for k, v in event.get("headers", {}).items()}
        jar = SimpleCookie()
        try:
            jar.load("; ".join(event.get("cookies", [])))
            now = int(self.clock())
            if path == "/auth/login" and method == "GET":
                if event.get("rawQueryString"):
                    raise ValueError("Login accepts no query parameters")
                if REMEMBERED in jar and self.remembered is not None and self.remembered_enabled:
                    try:
                        session = self.remembered.refresh(jar[REMEMBERED].value,activity=False)
                        return self.response(302,cookies=[self._remember_cookie(jar[REMEMBERED].value,session),
                            *self.edge(session["expires_at"],remembered=True)],location=self.base+"/")
                    except SessionExpired:
                        pass
                return self._login_page()
            if path == "/auth/login" and method == "POST":
                self._origin(headers)
                form = self._body(event,json_body=False)
                if (set(form) not in ({"csrf"},{"csrf","remember"}) or
                    not isinstance(form["csrf"],str) or not form["csrf"] or
                    ("remember" in form and form["remember"]!="1")):
                    raise ValueError("Exact login intent fields required")
                intent = self.unseal(jar[INTENT].value)
                if set(intent)!={"intent_nonce","expires_at"} or not hmac.compare_digest(
                        intent["intent_nonce"],form["csrf"]):
                    raise ValueError("Invalid login intent")
                return self._begin("remember" in form)
            if path == "/auth/callback" and method == "GET":
                query = parse_qs(event.get("rawQueryString", ""), strict_parsing=True)
                if set(query) != {"code", "state"} or any(
                    len(v) != 1 or not v[0] or len(v[0]) > 4096 for v in query.values()
                ):
                    raise ValueError("Invalid OAuth callback")
                flow = self.unseal(jar[FLOW].value)
                if not hmac.compare_digest(flow["state"], query["state"][0]):
                    raise ValueError("Invalid OAuth state")
                remember = flow.get("remember",False)
                if type(remember) is not bool:
                    raise ValueError("Literal remembered intent required")
                provider = self._provider(remember)
                if flow.get("client_id",provider.client_id)!=provider.client_id:
                    raise ValueError("OAuth client differs from the chosen intent")
                tokens = provider.exchange(
                    query["code"][0], flow["verifier"], self.base + "/auth/callback")
                identity = provider.verify(tokens["id_token"],id_token=True)
                access = provider.verify(tokens["access_token"])
                if (
                    identity.get("nonce") != flow["nonce"]
                    or identity["sub"] != access["sub"]
                ):
                    raise ValueError("Invalid OAuth nonce/subject")
                if not self.invited(access["sub"]):
                    raise APIError(
                        403, "not_invited", "An active invitation is required"
                    )
                if REMEMBERED in jar and self.remembered is not None:
                    self.remembered.revoke(jar[REMEMBERED].value)
                if remember:
                    sid, remembered_session = self.remembered.create(tokens,access["sub"])
                    return self.response(302,cookies=[
                        cookie(FLOW,"",0),cookie(INTENT,"",0),cookie(SESSION,"",0),
                        self._remember_cookie(sid,remembered_session),
                        *self.edge(remembered_session["expires_at"],remembered=True)],location=self.base+"/")
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
                        cookie(INTENT,"",0),cookie(REMEMBERED,"",0),
                        cookie(SESSION, session, expires - now),
                        *self.edge(expires),
                    ],
                    location=self.base + "/",
                )
            if path == "/auth/session" and method == "GET":
                if REMEMBERED in jar:
                    if self.remembered is None or not self.remembered_enabled:
                        raise SessionExpired("Remembered sign-in is unavailable")
                    return self.response(200,self.remembered.current(jar[REMEMBERED].value))
                return self.response(200,self._legacy_session(jar))
            if path == "/auth/refresh" and method == "POST":
                self._origin(headers)
                try:
                    body = self._body(event,json_body=True)
                    if not isinstance(body,dict) or set(body)!={"activity"} or type(body["activity"]) is not bool:
                        raise ValueError()
                except (ValueError,TypeError,UnicodeError):
                    raise APIError(400,"invalid_request","Exact activity boolean JSON required") from None
                if REMEMBERED not in jar:
                    return self.response(200,self._legacy_session(jar))
                if self.remembered is None or not self.remembered_enabled:
                    raise SessionExpired("Remembered sign-in is unavailable")
                sid = jar[REMEMBERED].value
                session = self.remembered.refresh(sid,activity=body["activity"])
                return self.response(200,session,cookies=[self._remember_cookie(sid,session),
                    *self.edge(session["expires_at"],remembered=True)])
            if path == "/auth/logout" and method == "POST":
                if headers.get("origin") != self.base:
                    raise APIError(403, "invalid_origin", "Same-origin logout required")
                if REMEMBERED in jar and self.remembered is not None:
                    self.remembered.revoke(jar[REMEMBERED].value)
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
                    cookies=self._clear(),
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
        except RefreshBusy as exc:
            refresh_required = str(exc)=="refresh_required"
            result = self.response(401 if refresh_required else 409,
                {"error":{"code":"refresh_required" if refresh_required else "refresh_in_progress",
                          "message":"Renew the browser session" if refresh_required else "Session renewal is in progress"}})
            if not refresh_required:
                result["headers"]["retry-after"]="1"
            return result
        except RefreshUnavailable:
            return self.response(503,{"error":{"code":"identity_unavailable",
                "message":"Identity service is temporarily unavailable"}})
        except APIError as exc:
            return self.response(
                exc.status,
                {"error": {"code": exc.code, "message": exc.message}},
                cookies=(self._clear() if exc.code=="not_invited" and path in ("/auth/session","/auth/refresh")
                         else [] if exc.code=="invalid_origin" or exc.status==400
                         else [cookie(FLOW,"",0),cookie(INTENT,"",0)]),
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
                cookies=(self._clear() if path in ("/auth/session","/auth/refresh")
                         else [cookie(FLOW,"",0),cookie(INTENT,"",0)]),
            )
