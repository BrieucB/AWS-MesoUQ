"""Memory-only connector authentication broker for one registered worker process.

No OAuth operation is initiated by a worker. The controller supplies the normal
TokenSession factory. Refresh credentials never cross this Unix socket.
"""
from __future__ import annotations

import ctypes
import errno
import json
import math
import os
from pathlib import Path
import socket
import stat
import struct
import subprocess
import sys
import tempfile
import threading
import time
from typing import Callable, Protocol

from .auth import SessionExpired

PROTOCOL_VERSION = 1
MAX_REQUEST_BYTES = 4096
MAX_RESPONSE_BYTES = 32768
MAX_TOKEN_BYTES = 16384
MAX_SOCKET_PATH_BYTES = 103
MAX_CONNECTIONS = 8
READ_TIMEOUT = 3.0


class BrokerError(RuntimeError):
    """A credential-free, controlled broker error code."""


class BrokerSecurityError(BrokerError):
    pass


class BrokerProtocolError(BrokerError):
    pass


class TokenProvider(Protocol):
    def token(self, *, force_refresh: bool = False) -> str: ...


def _canonical_path(value: str | Path) -> Path:
    text = os.fspath(value)
    if not isinstance(text, str) or "\0" in text:
        raise BrokerSecurityError("unsafe_path")
    path = Path(text)
    if not path.is_absolute() or str(path) != os.path.abspath(text):
        raise BrokerSecurityError("unsafe_path")
    return path


def _open_directory(path: str | Path, *, private: bool = False) -> int:
    """Walk directory descriptors without following any symlink."""
    path = _canonical_path(path)
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
    fd = os.open("/", flags)
    try:
        for part in path.parts[1:]:
            child = os.open(part, flags, dir_fd=fd)
            os.close(fd)
            fd = child
            info = os.fstat(fd)
            mode = stat.S_IMODE(info.st_mode)
            if info.st_uid not in (0, os.geteuid()):
                raise BrokerSecurityError("unsafe_ancestor_owner")
            # A root-owned sticky temporary directory is the sole shared-write
            # exception. Its freshly created child must still be private.
            if mode & 0o022 and not (info.st_uid == 0 and mode & stat.S_ISVTX):
                raise BrokerSecurityError("unsafe_ancestor_permissions")
        info = os.fstat(fd)
        if private and (
            info.st_uid != os.geteuid() or stat.S_IMODE(info.st_mode) != 0o700
        ):
            raise BrokerSecurityError("unsafe_private_directory")
        return fd
    except BaseException:
        os.close(fd)
        raise


def _socket_metadata(dirfd: int, name: str) -> os.stat_result:
    info = os.stat(name, dir_fd=dirfd, follow_symlinks=False)
    if (
        not stat.S_ISSOCK(info.st_mode)
        or info.st_uid != os.geteuid()
        or stat.S_IMODE(info.st_mode) != 0o600
        or info.st_nlink != 1
    ):
        raise BrokerSecurityError("unsafe_socket")
    return info


def _darwin_peer_ids(fd: int) -> tuple[int, int]:
    """Ask libc for the effective peer IDs on CPython, which lacks getpeereid.

    Apple's uid_t/gid_t are uint32; LOCAL_PEERPID separately authenticates PID.
    Failure is always closed: there is no caller-supplied or UID-only fallback.
    """
    if type(fd) is not int or fd < 0:
        raise BrokerSecurityError("peer_identity_unavailable")
    native = ctypes.CDLL(None, use_errno=True)
    getpeereid = native.getpeereid
    getpeereid.argtypes = [
        ctypes.c_int, ctypes.POINTER(ctypes.c_uint32),
        ctypes.POINTER(ctypes.c_uint32),
    ]
    getpeereid.restype = ctypes.c_int
    uid, gid = ctypes.c_uint32(0xFFFFFFFF), ctypes.c_uint32(0xFFFFFFFF)
    if getpeereid(fd, ctypes.byref(uid), ctypes.byref(gid)) != 0:
        raise OSError(ctypes.get_errno() or errno.EIO, "native peer lookup failed")
    if uid.value == 0xFFFFFFFF or gid.value == 0xFFFFFFFF:
        raise OSError(errno.EINVAL, "native peer lookup returned no identity")
    return uid.value, gid.value


def _peer_identity(conn: socket.socket) -> tuple[int, int]:
    """Return kernel PID and UID; never fall back to UID-only authorization."""
    try:
        if sys.platform.startswith("linux") and hasattr(socket, "SO_PEERCRED"):
            size = struct.calcsize("3i")
            pid, uid, _gid = struct.unpack(
                "3i", conn.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, size)
            )
        elif sys.platform == "darwin":
            # Apple's public bsd/sys/un.h defines SOL_LOCAL=0, LOCAL_PEERPID=2.
            uid, _gid = _darwin_peer_ids(conn.fileno())
            pid = struct.unpack(
                "i", conn.getsockopt(0, 2, struct.calcsize("i")))[0]
        else:
            raise BrokerSecurityError("peer_identity_unavailable")
    except (AttributeError, OSError, TypeError, ValueError, struct.error, ctypes.ArgumentError):
        raise BrokerSecurityError("peer_identity_unavailable") from None
    if pid <= 0 or uid < 0:
        raise BrokerSecurityError("invalid_peer_identity")
    return pid, uid


def _unique_object(pairs: list[tuple[str, object]]) -> dict:
    result = {}
    for key, value in pairs:
        if key in result:
            raise BrokerProtocolError("invalid_rpc")
        result[key] = value
    return result


def _invalid_constant(_value: str) -> None:
    raise BrokerProtocolError("invalid_rpc")


def _read_exact(conn: socket.socket, count: int) -> bytes:
    parts = []
    while count:
        part = conn.recv(count)
        if not part:
            raise BrokerProtocolError("invalid_rpc")
        parts.append(part)
        count -= len(part)
    return b"".join(parts)


def _read_message(conn: socket.socket, limit: int) -> dict:
    length = struct.unpack("!I", _read_exact(conn, 4))[0]
    if not 0 < length <= limit:
        raise BrokerProtocolError("invalid_rpc")
    try:
        value = json.loads(
            _read_exact(conn, length).decode("utf-8"),
            object_pairs_hook=_unique_object,
            parse_constant=_invalid_constant,
        )
    except (ValueError, UnicodeError, RecursionError):
        raise BrokerProtocolError("invalid_rpc") from None
    if not isinstance(value, dict):
        raise BrokerProtocolError("invalid_rpc")
    return value


def _send_message(conn: socket.socket, value: dict, limit: int) -> None:
    data = json.dumps(value, allow_nan=False, separators=(",", ":")).encode("utf-8")
    if not 0 < len(data) <= limit:
        raise BrokerProtocolError("invalid_rpc")
    conn.sendall(struct.pack("!I", len(data)) + data)


def _integer(value: object, minimum: int = 0) -> bool:
    return type(value) is int and minimum <= value <= 2**63 - 1


def _valid_token(value: object) -> bool:
    return (
        isinstance(value, str)
        and 0 < len(value.encode("utf-8")) <= MAX_TOKEN_BYTES
        and all(33 <= ord(char) <= 126 for char in value)
    )


class _TokenVault:
    def __init__(
        self,
        session: TokenProvider,
        *,
        clock: Callable[[], float],
        force_interval: float,
    ):
        self._session: TokenProvider | None = session
        self._clock = clock
        self._force_interval = force_interval
        self._lock = threading.Lock()
        self._cached: str | None = None
        self._epoch = 0
        self._last_force = -math.inf
        self._auth_required = False
        self._provider_calls = 0
        self._forced_calls = 0
        self._last_error: str | None = None

    def get(self, *, force_refresh: bool, observed_epoch: int) -> tuple[str, int]:
        with self._lock:
            if self._auth_required or self._session is None:
                raise SessionExpired("controller_authentication_required")
            force = (
                force_refresh
                and observed_epoch == self._epoch
                and self._clock() - self._last_force >= self._force_interval
            )
            self._provider_calls += 1
            if force:
                self._forced_calls += 1
                self._last_force = self._clock()
            try:
                token = self._session.token(force_refresh=force)
            except SessionExpired:
                self._auth_required = True
                self._cached = None
                self._last_error = "authentication_required"
                raise SessionExpired("controller_authentication_required") from None
            except Exception:
                self._last_error = "token_provider_unavailable"
                raise BrokerError("token_provider_unavailable") from None
            if not _valid_token(token):
                self._last_error = "invalid_provider_token"
                raise BrokerError("invalid_provider_token")
            if token != self._cached or force:
                self._epoch += 1
            self._cached = token
            self._last_error = None
            return token, self._epoch

    def status(self) -> dict:
        with self._lock:
            return {
                "authenticated": self._cached is not None and not self._auth_required,
                "authentication_required": self._auth_required,
                "token_epoch": self._epoch,
                "provider_calls": self._provider_calls,
                "forced_refresh_calls": self._forced_calls,
                "last_error_code": self._last_error,
            }

    def clear(self) -> None:
        with self._lock:
            self._cached = None
            self._session = None


class AuthBroker:
    """Long-lived controller: one normal login factory, memory-only credentials.

    Only WorkerSupervisor binds a live Popen instance. Socket RPC never accepts
    lifecycle commands or worker specifications.
    """

    def __init__(
        self,
        session_factory: Callable[[], TokenProvider],
        *,
        private_socket_dir: str | Path | None = None,
        clock: Callable[[], float] = time.monotonic,
        force_refresh_min_interval: float = 5.0,
    ):
        if not callable(session_factory):
            raise BrokerError("invalid_session_factory")
        if not 0.5 <= force_refresh_min_interval <= 60:
            raise BrokerError("invalid_refresh_interval")
        self._factory = session_factory
        self._requested_dir = private_socket_dir
        self._clock = clock
        self._force_interval = force_refresh_min_interval
        self._vault: _TokenVault | None = None
        self._listener: socket.socket | None = None
        self._dirfd: int | None = None
        self._directory: Path | None = None
        self._socket_path: Path | None = None
        self._socket_inode: tuple[int, int] | None = None
        self._owns_directory = False
        self._lease_lock = threading.RLock()
        self._lease: tuple[subprocess.Popen, int, str] | None = None
        self._lifecycle_lock = threading.RLock()
        self._threads_lock = threading.Lock()
        self._handlers: set[threading.Thread] = set()
        self._connections: set[socket.socket] = set()
        self._slots = threading.BoundedSemaphore(MAX_CONNECTIONS)
        self._stop = threading.Event()
        self._accept_thread: threading.Thread | None = None
        self._login_attempts = 0
        self._started = False
        self._running = False

    @property
    def socket_path(self) -> Path:
        if not self._running or self._socket_path is None:
            raise BrokerError("controller_not_running")
        return self._socket_path

    @property
    def running(self) -> bool:
        return self._running

    def start(self) -> "AuthBroker":
        with self._lifecycle_lock:
            if self._started:
                raise BrokerError("controller_already_started")
            self._started = True
            try:
                if self._requested_dir is None:
                    base = Path("/tmp").resolve()
                    basefd = _open_directory(base)
                    os.close(basefd)
                    self._directory = Path(tempfile.mkdtemp(prefix=".amq-", dir=base))
                    self._owns_directory = True
                else:
                    self._directory = _canonical_path(self._requested_dir)
                self._dirfd = _open_directory(self._directory, private=True)
                self._socket_path = self._directory / "a.sock"
                if len(os.fsencode(self._socket_path)) > MAX_SOCKET_PATH_BYTES:
                    raise BrokerSecurityError("socket_path_too_long")
                try:
                    os.stat("a.sock", dir_fd=self._dirfd, follow_symlinks=False)
                except FileNotFoundError:
                    pass
                else:
                    raise BrokerSecurityError("socket_already_exists")
                self._listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
                self._listener.set_inheritable(False)
                self._listener.bind(str(self._socket_path))
                # The leaf has been private before bind. No credential is
                # available before this socket is restricted and verified.
                os.chmod("a.sock", 0o600, dir_fd=self._dirfd)
                info = _socket_metadata(self._dirfd, "a.sock")
                self._socket_inode = (info.st_dev, info.st_ino)
                self._listener.listen(MAX_CONNECTIONS)
                self._listener.settimeout(0.2)
                self._login_attempts += 1
                try:
                    session = self._factory()
                except SessionExpired:
                    raise SessionExpired("controller_authentication_required") from None
                except Exception:
                    raise BrokerError("authentication_start_failed") from None
                if not callable(getattr(session, "token", None)):
                    raise BrokerError("invalid_token_provider")
                self._vault = _TokenVault(
                    session, clock=self._clock, force_interval=self._force_interval
                )
                self._vault.get(force_refresh=False, observed_epoch=0)
                self._running = True
                self._accept_thread = threading.Thread(
                    target=self._accept, name="mesouq-auth-controller", daemon=True
                )
                self._accept_thread.start()
                return self
            except SessionExpired:
                self.close()
                raise SessionExpired("controller_authentication_required") from None
            except BrokerError:
                self.close()
                raise
            except Exception:
                self.close()
                raise BrokerError("controller_start_failed") from None
            except BaseException:
                self.close()
                raise

    def _bind_worker(
        self, process: subprocess.Popen, generation: int, source_binding: str
    ) -> None:
        if not self._running or not _integer(generation, 1):
            raise BrokerError("invalid_worker_registration")
        if (
            not isinstance(process, subprocess.Popen)
            or process.pid == os.getpid()
            or process.poll() is not None
            or not isinstance(source_binding, str)
            or len(source_binding) != 64
            or any(char not in "0123456789abcdef" for char in source_binding)
        ):
            raise BrokerError("invalid_worker_registration")
        with self._lease_lock:
            if self._lease is not None and self._lease[0].poll() is None:
                raise BrokerError("worker_already_registered")
            self._lease = (process, generation, source_binding)

    def fence_worker(self) -> None:
        with self._lease_lock:
            self._lease = None

    def _authorize(self, peer: tuple[int, int], generation: int | None = None) -> None:
        pid, uid = peer
        with self._lease_lock:
            lease = self._lease
            if (
                uid != os.geteuid()
                or lease is None
                or lease[0].pid != pid
                or lease[0].poll() is not None
            ):
                raise BrokerSecurityError("worker_not_authorized")
            if generation is not None and lease[1] != generation:
                raise BrokerSecurityError("worker_generation_fenced")

    def safe_status(self) -> dict:
        with self._lease_lock:
            lease = self._lease
            active = lease is not None and lease[0].poll() is None
            worker = {
                "worker_active": active,
                "worker_pid": lease[0].pid if active else None,
                "worker_generation": lease[1] if active else None,
                "worker_source_binding": lease[2] if active else None,
            }
        auth = self._vault.status() if self._vault else {
            "authenticated": False,
            "authentication_required": False,
            "token_epoch": 0,
            "provider_calls": 0,
            "forced_refresh_calls": 0,
            "last_error_code": None,
        }
        return {
            "controller_running": self._running,
            "login_attempts": self._login_attempts,
            **worker,
            **auth,
        }

    def _dispatch(self, peer: tuple[int, int], request: dict) -> dict:
        if type(request.get("version")) is not int or request["version"] != 1:
            raise BrokerProtocolError("invalid_rpc")
        if not _integer(request.get("generation"), 1):
            raise BrokerProtocolError("invalid_rpc")
        generation = request["generation"]
        self._authorize(peer, generation)
        operation = request.get("operation")
        if operation == "status":
            if set(request) != {"version", "operation", "generation"}:
                raise BrokerProtocolError("invalid_rpc")
            return {"version": 1, "ok": True, "status": self.safe_status()}
        if operation != "token" or set(request) != {
            "version", "operation", "generation", "force_refresh", "observed_epoch"
        }:
            raise BrokerProtocolError("invalid_rpc")
        if type(request["force_refresh"]) is not bool or not _integer(
            request["observed_epoch"]
        ):
            raise BrokerProtocolError("invalid_rpc")
        if self._vault is None:
            raise BrokerError("controller_not_running")
        token, epoch = self._vault.get(
            force_refresh=request["force_refresh"],
            observed_epoch=request["observed_epoch"],
        )
        # Fencing also applies to an RPC that was waiting for serialized refresh.
        self._authorize(peer, generation)
        return {"version": 1, "ok": True, "token": token, "epoch": epoch}

    def _accept(self) -> None:
        while not self._stop.is_set():
            try:
                assert self._listener is not None
                conn, _ = self._listener.accept()
            except socket.timeout:
                continue
            except OSError:
                if self._stop.is_set():
                    return
                continue
            conn.settimeout(READ_TIMEOUT)
            conn.set_inheritable(False)
            if not self._slots.acquire(blocking=False):
                try:
                    _send_message(
                        conn, {"version": 1, "ok": False, "error": "busy"},
                        MAX_RESPONSE_BYTES,
                    )
                except OSError:
                    pass
                conn.close()
                continue
            thread = threading.Thread(
                target=self._handle, args=(conn,), name="mesouq-auth-rpc", daemon=True
            )
            with self._threads_lock:
                self._connections.add(conn)
                self._handlers.add(thread)
            thread.start()

    def _handle(self, conn: socket.socket) -> None:
        try:
            peer = _peer_identity(conn)
            self._authorize(peer)
            request = _read_message(conn, MAX_REQUEST_BYTES)
            response = self._dispatch(peer, request)
        except SessionExpired:
            response = {"version": 1, "ok": False, "error": "authentication_required"}
        except BrokerError as error:
            # All BrokerError arguments are controlled codes, never provider
            # exceptions or request/token text.
            response = {"version": 1, "ok": False, "error": error.args[0]}
        except Exception:
            response = {"version": 1, "ok": False, "error": "invalid_rpc"}
        try:
            if "token" in response:
                # Fence completion is ordered after any already-authorized
                # response send. No token response begins after fencing returns.
                with self._lease_lock:
                    self._authorize(peer, request["generation"])
                    _send_message(conn, response, MAX_RESPONSE_BYTES)
            else:
                _send_message(conn, response, MAX_RESPONSE_BYTES)
        except (OSError, BrokerError):
            pass
        finally:
            conn.close()
            with self._threads_lock:
                self._connections.discard(conn)
                self._handlers.discard(threading.current_thread())
            self._slots.release()

    def close(self, timeout: float = 5.0) -> None:
        with self._lifecycle_lock:
            self.fence_worker()
            self._running = False
            self._stop.set()
            if self._listener is not None:
                self._listener.close()
            with self._threads_lock:
                connections = list(self._connections)
                handlers = list(self._handlers)
            for conn in connections:
                try:
                    conn.shutdown(socket.SHUT_RDWR)
                except OSError:
                    pass
                conn.close()
            deadline = time.monotonic() + timeout
            join_failed = False
            for thread in [self._accept_thread, *handlers]:
                if (thread is not None and thread is not threading.current_thread()
                        and thread.ident is not None):
                    try:
                        thread.join(max(0, deadline - time.monotonic()))
                    except RuntimeError:
                        # Complete resource cleanup even if a thread cannot join.
                        join_failed = True
            if self._dirfd is not None:
                try:
                    info = os.stat("a.sock", dir_fd=self._dirfd, follow_symlinks=False)
                    if (
                        self._socket_inode == (info.st_dev, info.st_ino)
                        and stat.S_ISSOCK(info.st_mode)
                        and info.st_uid == os.geteuid()
                    ):
                        os.unlink("a.sock", dir_fd=self._dirfd)
                except FileNotFoundError:
                    pass
                if self._owns_directory and self._directory is not None:
                    try:
                        current = os.stat(self._directory, follow_symlinks=False)
                        held = os.fstat(self._dirfd)
                        if (current.st_dev, current.st_ino) == (held.st_dev, held.st_ino):
                            os.rmdir(self._directory)
                    except (FileNotFoundError, OSError):
                        pass
                os.close(self._dirfd)
                self._dirfd = None
            if self._vault is not None:
                self._vault.clear()
            if join_failed or any(thread.is_alive() for thread in handlers):
                raise BrokerError("controller_shutdown_incomplete")

    def __enter__(self) -> "AuthBroker":
        return self.start()

    def __exit__(self, *_exc: object) -> None:
        self.close()
