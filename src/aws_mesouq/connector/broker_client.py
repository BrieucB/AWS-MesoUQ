"""Worker-side, memory-only access-token provider for the auth controller."""
from __future__ import annotations

import os
from pathlib import Path
import socket
import threading

from .auth import SessionExpired
from .auth_broker import (
    BrokerError,
    BrokerProtocolError,
    BrokerSecurityError,
    MAX_REQUEST_BYTES,
    MAX_RESPONSE_BYTES,
    MAX_SOCKET_PATH_BYTES,
    _canonical_path,
    _integer,
    _open_directory,
    _peer_identity,
    _read_message,
    _send_message,
    _socket_metadata,
    _valid_token,
)

SOCKET_ENV = "AWS_MESOUQ_AUTH_SOCKET"
CONTROLLER_ENV = "AWS_MESOUQ_AUTH_CONTROLLER_PID"
GENERATION_ENV = "AWS_MESOUQ_AUTH_GENERATION"


class BrokerClient:
    """Drop-in token-session object for API(base_url, token_session).

    Only the token epoch is cached here. Refresh credentials and login
    operations are never available to a worker.
    """

    def __init__(
        self,
        socket_path: str | Path,
        controller_pid: int,
        generation: int,
        *,
        timeout: float = 45.0,
    ):
        self._path = _canonical_path(socket_path)
        if len(os.fsencode(self._path)) > MAX_SOCKET_PATH_BYTES:
            raise BrokerSecurityError("socket_path_too_long")
        if not _integer(controller_pid, 1) or not _integer(generation, 1):
            raise BrokerSecurityError("invalid_controller_registration")
        if not 0 < timeout <= 60:
            raise BrokerError("invalid_rpc_timeout")
        self._controller_pid = controller_pid
        self._generation = generation
        self._timeout = timeout
        self._epoch = 0
        self._epoch_lock = threading.Lock()

    @classmethod
    def from_environment(cls) -> "BrokerClient":
        """Read coordination metadata only; no token is placed in environment."""
        try:
            path = os.environ[SOCKET_ENV]
            pid = int(os.environ[CONTROLLER_ENV])
            generation = int(os.environ[GENERATION_ENV])
        except (KeyError, ValueError):
            raise BrokerSecurityError("invalid_controller_registration") from None
        return cls(path, pid, generation)

    def _request(self, operation: str, **fields: object) -> dict:
        conn = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        conn.set_inheritable(False)
        conn.settimeout(self._timeout)
        dirfd = None
        try:
            dirfd = _open_directory(self._path.parent, private=True)
            original = _socket_metadata(dirfd, self._path.name)
            conn.connect(str(self._path))
            current = _socket_metadata(dirfd, self._path.name)
            if (original.st_dev, original.st_ino) != (current.st_dev, current.st_ino):
                raise BrokerSecurityError("socket_changed")
            pid, uid = _peer_identity(conn)
            if uid != os.geteuid() or pid != self._controller_pid:
                raise BrokerSecurityError("controller_not_authorized")
            _send_message(
                conn,
                {
                    "version": 1,
                    "operation": operation,
                    "generation": self._generation,
                    **fields,
                },
                MAX_REQUEST_BYTES,
            )
            result = _read_message(conn, MAX_RESPONSE_BYTES)
        except BrokerError:
            raise
        except (OSError, ValueError):
            raise BrokerError("controller_unavailable") from None
        finally:
            conn.close()
            if dirfd is not None:
                os.close(dirfd)
        if (
            type(result.get("version")) is not int
            or result["version"] != 1 or type(result.get("ok")) is not bool
        ):
            raise BrokerProtocolError("invalid_rpc_response")
        if not result["ok"]:
            if set(result) != {"version", "ok", "error"}:
                raise BrokerProtocolError("invalid_rpc_response")
            code = result["error"]
            if code == "authentication_required":
                raise SessionExpired("controller_authentication_required")
            allowed = {
                "busy", "invalid_rpc", "worker_not_authorized",
                "worker_generation_fenced", "controller_not_running",
                "token_provider_unavailable", "invalid_provider_token",
                "peer_identity_unavailable", "invalid_peer_identity",
            }
            if not isinstance(code, str) or code not in allowed:
                raise BrokerProtocolError("invalid_rpc_response")
            raise BrokerError(code)
        return result

    def token(self, *, force_refresh: bool = False) -> str:
        if type(force_refresh) is not bool:
            raise BrokerProtocolError("invalid_rpc")
        with self._epoch_lock:
            epoch = self._epoch
        if force_refresh and epoch == 0:
            self.token()
            with self._epoch_lock:
                epoch = self._epoch
        result = self._request(
            "token", force_refresh=force_refresh, observed_epoch=epoch
        )
        if (
            set(result) != {"version", "ok", "token", "epoch"}
            or not _valid_token(result["token"])
            or not _integer(result["epoch"], 1)
        ):
            raise BrokerProtocolError("invalid_rpc_response")
        with self._epoch_lock:
            self._epoch = max(self._epoch, result["epoch"])
        return result["token"]

    def safe_status(self) -> dict:
        result = self._request("status")
        if set(result) != {"version", "ok", "status"}:
            raise BrokerProtocolError("invalid_rpc_response")
        status = result["status"]
        keys = {
            "controller_running", "login_attempts", "worker_active", "worker_pid",
            "worker_generation", "worker_source_binding", "authenticated",
            "authentication_required", "token_epoch", "provider_calls",
            "forced_refresh_calls", "last_error_code",
        }
        if not isinstance(status, dict) or set(status) != keys:
            raise BrokerProtocolError("invalid_rpc_response")
        return status
