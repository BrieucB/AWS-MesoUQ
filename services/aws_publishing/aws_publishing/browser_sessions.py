"""Remembered-browser credentials: encrypted records outside business STATE.

The shared Lambda role is trusted for both business data and this namespace.
No browser, normal metadata snapshot or connector receives these credentials.
"""
from __future__ import annotations
import copy
import hashlib
import json
import secrets
import threading
import time

from jwt import PyJWTError
from jwt.exceptions import PyJWKClientConnectionError

MAXIMUM_SECONDS = 30 * 86400
IDLE_SECONDS = 7 * 86400
GRANT_SECONDS = 300
REFRESH_LEASE_SECONDS = 15
NAMESPACE = "AUTH_SESSION#"


class SessionExpired(ValueError):
    pass


class RefreshBusy(RuntimeError):
    pass


class RefreshUnavailable(RuntimeError):
    pass


class MemorySessionRecords:
    """Encrypted-record fixture with the same conditional-write contract."""
    def __init__(self):
        self.records = {}
        self.lock = threading.Lock()

    def get(self, key):
        with self.lock:
            return copy.deepcopy(self.records.get(key))

    def put(self, key, value, expected=None):
        with self.lock:
            previous = self.records.get(key)
            if (expected is None and previous is not None) or (
                    expected is not None and (previous is None or previous["version"] != expected)):
                return False
            self.records[key] = copy.deepcopy(value)
            return True


class DynamoSessionRecords:
    """Point reads and single-record CAS; no scans or business revision changes."""
    def __init__(self, client, table):
        self.client, self.table = client, table

    def get(self, key):
        item = self.client.get_item(TableName=self.table,
            Key={"pk":{"S":NAMESPACE+key},"sk":{"S":"SESSION"}},
            ConsistentRead=True).get("Item")
        if item is None:
            return None
        return {"version":int(item["version"]["N"]),
                "payload":item["payload"]["S"],
                "auth_expires_at":int(item["auth_expires_at"]["N"])}

    def put(self, key, value, expected=None):
        arguments = {"TableName":self.table,"Item":{
            "pk":{"S":NAMESPACE+key},"sk":{"S":"SESSION"},
            "version":{"N":str(value["version"])},
            "payload":{"S":value["payload"]},
            "auth_expires_at":{"N":str(value["auth_expires_at"])}}}
        if expected is None:
            arguments["ConditionExpression"] = "attribute_not_exists(pk)"
        else:
            arguments.update(ConditionExpression="#version = :expected",
                ExpressionAttributeNames={"#version":"version"},
                ExpressionAttributeValues={":expected":{"N":str(expected)}})
        try:
            self.client.put_item(**arguments)
            return True
        except Exception as error:
            if getattr(error,"response",{}).get("Error",{}).get("Code") == "ConditionalCheckFailedException":
                return False
            raise


class BrowserSessions:
    """One finite family per opaque SID; refresh never changes its original end."""
    def __init__(self, records, cipher, provider, invited, *, clock=time.time):
        self.records, self.cipher, self.provider = records, cipher, provider
        self.invited, self.clock = invited, clock

    @staticmethod
    def key(sid):
        if (not isinstance(sid,str) or len(sid)!=43 or
                any(c not in "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_" for c in sid)):
            raise SessionExpired("Remembered browser session is unavailable")
        return hashlib.sha256(sid.encode()).hexdigest()

    def _pack(self, record):
        return {"version":record["version"],
            "payload":self.cipher.encrypt(json.dumps(record,sort_keys=True,
                separators=(",",":"),allow_nan=False).encode()).decode(),
            # TTL is cleanup only. Explicit idle/absolute checks are authoritative.
            "auth_expires_at":int(record["absolute_expires_at"])+86400}

    def _read(self, sid, *, expired=False):
        key = self.key(sid)
        raw = self.records.get(key)
        if raw is None:
            raise SessionExpired("Remembered browser session is unavailable")
        try:
            record = json.loads(self.cipher.decrypt(raw["payload"].encode()))
            if (record["schema"]!=1 or record["sid_hash"]!=key or
                type(record["version"]) is not int or record["version"]!=raw["version"] or
                record["client_id"]!=self.provider.client_id or
                record["absolute_expires_at"]!=record["created_at"]+MAXIMUM_SECONDS or
                not record["created_at"]<=record["idle_expires_at"]<=record["absolute_expires_at"]):
                raise ValueError()
        except Exception:
            raise SessionExpired("Remembered browser record is invalid") from None
        if record["state"]!="active" or (not expired and
                self.clock()>=min(record["idle_expires_at"],record["absolute_expires_at"])):
            raise SessionExpired("Remembered browser session expired")
        return record

    def _write(self, before, after):
        after["version"] = before["version"]+1
        return self.records.put(after["sid_hash"],self._pack(after),expected=before["version"])

    def _verify(self, token, subject):
        try:
            claims = self.provider.verify(token)
            if claims["sub"]!=subject or claims["exp"]<=self.clock():
                raise SessionExpired("Remembered token identity differs")
            return claims
        except PyJWKClientConnectionError:
            raise RefreshUnavailable("Identity verification is temporarily unavailable") from None
        except (ValueError,KeyError,TypeError,PyJWTError):
            raise SessionExpired("Remembered token identity differs") from None

    def _member(self, subject):
        if not self.invited(subject):
            from .api import APIError
            raise APIError(403,"not_invited","An active invitation is required")

    def create(self, tokens, subject):
        now = int(self.clock())
        access, refresh = tokens.get("access_token"),tokens.get("refresh_token")
        if (not isinstance(access,str) or not access or not isinstance(refresh,str) or
                not refresh or len(access)>16384 or len(refresh)>16384):
            raise SessionExpired("Remembered login did not return bounded credentials")
        claims = self._verify(access,subject)
        self._member(subject)
        sid = secrets.token_urlsafe(32)
        record = {"schema":1,"sid_hash":self.key(sid),"version":0,"state":"active",
            "client_id":self.provider.client_id,"subject":subject,"created_at":now,
            "absolute_expires_at":now+MAXIMUM_SECONDS,"idle_expires_at":now+IDLE_SECONDS,
            "last_activity_at":now,"grant_expires_at":min(now+GRANT_SECONDS,int(claims["exp"])),
            "access_expires_at":int(claims["exp"]),
            "tokens":{"access_token":access,"refresh_token":refresh},
            "refresh_owner":None,"refresh_until":0}
        if not self.records.put(record["sid_hash"],self._pack(record)):
            raise RefreshUnavailable("Remembered session could not be created")
        return sid, self._response(record)

    def _active_now(self, record):
        now = self.clock()
        if record["state"]!="active" or now>=min(
                record["idle_expires_at"],record["absolute_expires_at"]):
            raise SessionExpired("Remembered browser session expired")
        return now

    def _response(self, record):
        # Identity/JWKS and conditional storage calls can cross a deadline.
        # Re-read the live clock after that work, immediately before publishing.
        now = self._active_now(record)
        expires = min(record["grant_expires_at"],record["access_expires_at"],
            record["idle_expires_at"],record["absolute_expires_at"])
        if expires<=now:
            raise RefreshBusy("refresh_required")
        return {"access_token":record["tokens"]["access_token"],
            "expires_at":expires,
            "renewable":True,"absolute_expires_at":record["absolute_expires_at"],
            "idle_expires_at":record["idle_expires_at"],"generation":record["version"]}

    def current(self, sid):
        record = self._read(sid)
        self._member(record["subject"])
        if self.clock()>=min(record["grant_expires_at"],record["access_expires_at"]):
            raise RefreshBusy("refresh_required")
        self._verify(record["tokens"]["access_token"],record["subject"])
        try:
            return self._response(record)
        except SessionExpired:
            # This GET observed an older snapshot. Another tab may have extended
            # idle during verification; let one fresh POST decide authoritatively.
            raise RefreshBusy("refresh_required") from None

    def refresh(self, sid, *, activity):
        if type(activity) is not bool:
            raise ValueError("Literal activity boolean required")
        for _ in range(4):
            record = self._read(sid)
            self._member(record["subject"])
            now = int(self._active_now(record))
            if record["refresh_owner"] is not None and record["refresh_until"]>now:
                raise RefreshBusy("refresh_in_progress")
            updated = copy.deepcopy(record)
            updated.update(refresh_owner=None,refresh_until=0)
            if activity:
                updated["last_activity_at"] = now
                updated["idle_expires_at"] = min(now+IDLE_SECONDS,record["absolute_expires_at"])
            if record["access_expires_at"]>now+60:
                self._verify(record["tokens"]["access_token"],record["subject"])
                # Verify against the original bounds before allowing activity to
                # extend idle. Never use the pre-verification timestamp for CAS.
                now = int(self._active_now(record))
                if activity:
                    updated["last_activity_at"] = now
                    updated["idle_expires_at"] = min(now+IDLE_SECONDS,record["absolute_expires_at"])
                updated["grant_expires_at"] = min(now+GRANT_SECONDS,updated["idle_expires_at"],
                    record["absolute_expires_at"],record["access_expires_at"])
                if self._write(record,updated):
                    return self._response(updated)
                continue
            updated["refresh_owner"] = secrets.token_urlsafe(16)
            updated["refresh_until"] = now+REFRESH_LEASE_SECONDS
            if not self._write(record,updated):
                continue
            reserved = updated
            try:
                tokens = self.provider.refresh(record["tokens"]["refresh_token"])
                access = tokens.get("access_token")
                refresh = tokens.get("refresh_token",record["tokens"]["refresh_token"])
                if (not isinstance(access,str) or not access or len(access)>16384 or
                    not isinstance(refresh,str) or not refresh or len(refresh)>16384):
                    raise SessionExpired("Renewal returned invalid credentials")
                claims = self._verify(access,record["subject"])
                self._member(record["subject"])
            except (ValueError,KeyError,TypeError) as error:
                tombstone = copy.deepcopy(reserved)
                tombstone.update(state="revoked",tokens={},refresh_owner=None,refresh_until=0,
                                 grant_expires_at=0)
                if not self._write(reserved,tombstone):
                    raise RefreshBusy("refresh_in_progress") from None
                raise SessionExpired("Remembered renewal was rejected") from None
            except Exception as error:
                released = copy.deepcopy(reserved)
                released.update(refresh_owner=None,refresh_until=0)
                self._write(reserved,released)
                # Invitation denial is definitive; provider transport failures are transient.
                if getattr(error,"status",None)==403:
                    raise
                raise RefreshUnavailable("Identity service is temporarily unavailable") from None
            finished = copy.deepcopy(reserved)
            finished.update(tokens={"access_token":access,"refresh_token":refresh},
                access_expires_at=int(claims["exp"]),refresh_owner=None,refresh_until=0)
            finish_now = int(self.clock())
            if finish_now>=min(finished["idle_expires_at"],finished["absolute_expires_at"]):
                finished.update(state="revoked",tokens={},grant_expires_at=0)
                self._write(reserved,finished)
                raise SessionExpired("Remembered browser session expired during renewal")
            finished["grant_expires_at"] = min(finish_now+GRANT_SECONDS,int(claims["exp"]),
                finished["idle_expires_at"],finished["absolute_expires_at"])
            if not self._write(reserved,finished):
                # A concurrent logout tombstone always wins. Never publish fetched tokens.
                latest = self._read(sid)
                if latest["version"]!=reserved["version"]:
                    raise RefreshBusy("refresh_in_progress")
                raise RefreshUnavailable("Remembered session could not be saved")
            return self._response(finished)
        raise RefreshBusy("refresh_in_progress")

    def revoke(self, sid):
        for _ in range(4):
            try:
                record = self._read(sid,expired=True)
            except SessionExpired:
                return
            token = record["tokens"].get("refresh_token")
            revoked = copy.deepcopy(record)
            revoked.update(state="revoked",tokens={},grant_expires_at=0,
                           refresh_owner=None,refresh_until=0)
            if self._write(record,revoked):
                if token:
                    try:
                        self.provider.revoke(token)
                    except Exception:
                        # Local tombstone is already durable; no provider body is logged.
                        pass
                return
        raise RefreshBusy("refresh_in_progress")
