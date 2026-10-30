"""One owned SSH master, fail-closed borrowers and a durable attempt budget."""
from __future__ import annotations
from contextlib import contextmanager
import argparse
import atexit
import fcntl
import ipaddress
import json
import math
import os
from pathlib import Path
import re
import shlex
import signal
import socket
import stat
import subprocess
import tempfile
import threading
import time
import uuid

WINDOW_SECONDS = 3600
MAX_ATTEMPTS = 3
_CATEGORIES = {"connection_failed", "connect_timeout", "authentication_failed", "host_key_error", "connection_lost", "command_timeout", "client_error"}

class SSHSessionError(RuntimeError):
    """Sanitized session failure; callers must not automatically retry it."""
    def __init__(self, category):
        self.category = category
        super().__init__(f"SSH session blocked: {category}; controlled operator recovery is required")

def _private(fd):
    info = os.fstat(fd)
    if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) != 0o600 or info.st_nlink != 1:
        raise SSHSessionError("unsafe_state")

def _prepare_directory(path):
    path = Path(path)
    if not path.is_absolute() or Path(os.path.abspath(path)) != path.resolve():
        raise SSHSessionError("unsafe_state")
    try:
        path.mkdir(mode=0o700)
    except FileExistsError:
        pass
    info = path.lstat()
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) != 0o700:
        raise SSHSessionError("unsafe_state")
    if len(os.fsencode(path / "master.sock")) > 100:
        raise SSHSessionError("control_path_too_long")
    return path

def _directory(path):
    try:
        return _prepare_directory(path)
    except OSError as error:
        raise SSHSessionError("unsafe_state") from error

def _lock_file(path, *, nonblocking=False):
    try:
        fd = os.open(path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    except OSError as error:
        raise SSHSessionError("unsafe_state") from error
    try:
        _private(fd)
        deadline = time.monotonic() + 95
        while True:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if nonblocking:
                    raise
                if time.monotonic() >= deadline:
                    raise SSHSessionError("state_busy")
                time.sleep(0.05)
        original = os.fstat(fd)
        current = os.stat(path, follow_symlinks=False)
        if (original.st_dev, original.st_ino) != (current.st_dev, current.st_ino):
            raise SSHSessionError("unsafe_state")
        return fd
    except BaseException:
        os.close(fd)
        raise

def _target(value):
    if not isinstance(value, dict) or set(value) != {"hostname", "user", "port"}:
        raise SSHSessionError("unsafe_state")
    if not isinstance(value["hostname"], str) or not re.fullmatch(r"[A-Za-z0-9_][A-Za-z0-9_.:-]*", value["hostname"]):
        raise SSHSessionError("unsafe_state")
    if not isinstance(value["user"], str) or not re.fullmatch(r"[A-Za-z0-9_][A-Za-z0-9_.-]*", value["user"]):
        raise SSHSessionError("unsafe_state")
    if type(value["port"]) is not int or not 1 <= value["port"] <= 65535:
        raise SSHSessionError("unsafe_state")
    return value

class _Store:
    def __init__(self, directory, clock=time.time):
        self.directory = _directory(directory)
        self.path = self.directory / "attempts.json"
        self.socket = self.directory / "master.sock"
        self.clock = clock
        bootstrap = _lock_file(self.directory / "bootstrap.lock")
        try:
            if not os.path.lexists(self.path):
                if os.path.lexists(self.directory / "state.lock") or os.path.lexists(self.directory / "owner.lock"):
                    raise SSHSessionError("unsafe_state")
                self.save({"schema": 2, "attempts": [], "watermark": 0.0, "active": None, "circuit": None, "counters": {"admitted": 0, "denied": 0, "rpc_requests": 0}}, 0.0)
        finally:
            os.close(bootstrap)

    def current_time(self, state, *, minimum=0.0):
        now = self.clock()
        if type(now) not in (int, float) or not math.isfinite(now) or now < max(state["watermark"], minimum):
            raise SSHSessionError("clock_rollback")
        return float(now)

    @contextmanager
    def locked(self):
        fd = _lock_file(self.directory / "state.lock")
        try:
            state = self.read()
            yield state, self.current_time(state)
        finally:
            os.close(fd)

    def read(self):
        try:
            fd = os.open(self.path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        except OSError as error:
            raise SSHSessionError("unsafe_state") from error
        try:
            _private(fd)
            if os.fstat(fd).st_size > 65536:
                raise SSHSessionError("unsafe_state")
            with os.fdopen(fd) as stream:
                fd = None
                encoded = stream.read(65537)
                if len(encoded) > 65536:
                    raise SSHSessionError("unsafe_state")
                state = json.loads(encoded)
        except (ValueError, OSError, TypeError) as error:
            raise SSHSessionError("unsafe_state") from error
        finally:
            if fd is not None:
                os.close(fd)
        if not isinstance(state, dict) or set(state) != {"schema", "attempts", "watermark", "active", "circuit", "counters"} or type(state["schema"]) is not int or state["schema"] != 2:
            raise SSHSessionError("unsafe_state")
        if type(state["watermark"]) not in (int, float) or not math.isfinite(state["watermark"]) or state["watermark"] < 0:
            raise SSHSessionError("unsafe_state")
        attempts = state["attempts"]
        if not isinstance(attempts, list) or len(attempts) > MAX_ATTEMPTS or any(type(x) not in (int, float) or not math.isfinite(x) or x < 0 or x > state["watermark"] for x in attempts) or attempts != sorted(attempts):
            raise SSHSessionError("unsafe_state")
        if state["circuit"] is not None and (not isinstance(state["circuit"], str) or state["circuit"] not in _CATEGORIES):
            raise SSHSessionError("unsafe_state")
        counters = state["counters"]
        if not isinstance(counters, dict) or set(counters) != {"admitted", "denied", "rpc_requests"} or any(type(x) is not int or x < 0 for x in counters.values()):
            raise SSHSessionError("unsafe_state")
        active = state["active"]
        if active is not None:
            required = {"id", "host_alias", "owner_pid", "master_pid", "ready", "target", "endpoint"}
            if not isinstance(active, dict) or set(active) != required or not isinstance(active["id"], str) or not re.fullmatch(r"[a-f0-9]{32}", active["id"]):
                raise SSHSessionError("unsafe_state")
            if not isinstance(active["host_alias"], str) or not re.fullmatch(r"[A-Za-z0-9_][A-Za-z0-9_.@-]*", active["host_alias"]) or type(active["owner_pid"]) is not int or active["owner_pid"] < 1 or type(active["ready"]) is not bool:
                raise SSHSessionError("unsafe_state")
            if active["master_pid"] is not None and (type(active["master_pid"]) is not int or active["master_pid"] < 1):
                raise SSHSessionError("unsafe_state")
            if active["ready"] and active["master_pid"] is None:
                raise SSHSessionError("unsafe_state")
            _target(active["target"])
            _endpoint(active["endpoint"])
        return state

    def save(self, state, now):
        state["watermark"] = now
        fd, temporary = tempfile.mkstemp(prefix=".attempts-", dir=self.directory)
        try:
            with os.fdopen(fd, "w") as stream:
                json.dump(state, stream, allow_nan=False, sort_keys=True)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, self.path)
            parent = os.open(self.directory, os.O_RDONLY | os.O_DIRECTORY)
            try:
                os.fsync(parent)
            finally:
                os.close(parent)
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)

def _settle_attempt(state, now):
    # Active reservation stays last: external imports are forbidden while active.
    if not state["attempts"]:
        raise SSHSessionError("unsafe_state")
    state["attempts"][-1] = now


def _endpoint(value):
    if type(value) is not dict or set(value) != {'address', 'family'}:
        raise SSHSessionError('unsafe_state')
    address, family = value['address'], value['family']
    if type(address) is not str or type(family) is not str or '%' in address or family not in {'inet', 'inet6'}:
        raise SSHSessionError('unsafe_state')
    try:
        parsed = ipaddress.ip_address(address)
    except ValueError as error:
        raise SSHSessionError('unsafe_state') from error
    if parsed.version != (4 if family == 'inet' else 6):
        raise SSHSessionError('unsafe_state')
    return {'address': str(parsed), 'family': family}


def _master_command(host, control_path, target=None, endpoint=None, host_key_alias=None):
    command = [
        'ssh', '-T', '-N', '-M', '-S', str(control_path),
        '-oControlMaster=yes', '-oControlPersist=no',
        '-oForkAfterAuthentication=no', '-oPermitLocalCommand=no',
        '-oRemoteCommand=none', '-oBatchMode=yes', '-oForwardAgent=no',
        '-oStrictHostKeyChecking=yes', '-oUpdateHostKeys=no',
        '-oClearAllForwardings=yes', '-oConnectionAttempts=1',
        '-oConnectTimeout=12', '-oServerAliveInterval=30',
        '-oServerAliveCountMax=2', '-oProxyCommand=none', '-oProxyJump=none',
    ]
    if target is not None:
        command += [
            '-oCanonicalizeHostname=no',
            '-oHostname=' + endpoint['address'],
            '-oAddressFamily=' + endpoint['family'],
            '-oHostKeyAlias=' + host_key_alias,
            '-l', target['user'], '-p', str(target['port']),
        ]
    return command + [host]


def _configuration(command):
    try:
        result = subprocess.run(
            [command[0], '-G', *command[1:]], stdout=subprocess.PIPE,
            stderr=subprocess.PIPE, timeout=5, check=True,
        )
        lines = result.stdout.decode().splitlines()
    except (OSError, subprocess.SubprocessError, UnicodeError) as error:
        raise SSHSessionError('configuration') from error
    values = {}
    for line in lines:
        key, separator, value = line.partition(' ')
        key = key.lower()
        if not separator or not re.fullmatch(r'[a-z][a-z0-9]*', key):
            raise SSHSessionError('configuration')
        values.setdefault(key, []).append(value)
    return values


def _option(values, key, default=None):
    items = values.get(key)
    if items is None and default is not None:
        return default
    if items is None or len(items) != 1:
        raise SSHSessionError('configuration')
    return items[0]


def _safe_configuration(values):
    for key in ('canonicalizehostname', 'checkhostip', 'verifyhostkeydns'):
        if _option(values, key).lower() not in {'no', 'false'}:
            raise SSHSessionError('configuration')
    if _option(values, 'gssapiauthentication', 'no').lower() not in {'no', 'false'}:
        raise SSHSessionError('configuration')
    for key in ('canonicalizepermittedcnames', 'knownhostscommand', 'localcommand', 'remotecommand'):
        if _option(values, key, 'none') != 'none':
            raise SSHSessionError('configuration')
    for key in (
        'identityfile', 'certificatefile', 'identityagent', 'userknownhostsfile',
        'globalknownhostsfile', 'revokedhostkeys', 'pkcs11provider', 'securitykeyprovider',
    ):
        if any('%' in value for value in values.get(key, [])):
            raise SSHSessionError('configuration')


def _resolve(host, control_path):
    baseline = _configuration(_master_command(host, control_path))
    _safe_configuration(baseline)
    try:
        target = _target({
            'hostname': _option(baseline, 'hostname'),
            'user': _option(baseline, 'user'),
            'port': int(_option(baseline, 'port')),
        })
    except (ValueError, SSHSessionError) as error:
        raise SSHSessionError('configuration') from error
    if target['port'] != 22:
        raise SSHSessionError('configuration')
    family_name = _option(baseline, 'addressfamily')
    families = {'any': socket.AF_UNSPEC, 'inet': socket.AF_INET, 'inet6': socket.AF_INET6}
    if family_name not in families:
        raise SSHSessionError('configuration')
    host_key_alias = _option(baseline, 'hostkeyalias', target['hostname'])
    if not re.fullmatch(r'[A-Za-z0-9_][A-Za-z0-9_.:-]*', host_key_alias):
        raise SSHSessionError('configuration')
    try:
        candidates = socket.getaddrinfo(
            target['hostname'], target['port'], families[family_name],
            socket.SOCK_STREAM, socket.IPPROTO_TCP,
        )
        family, kind, protocol, _, address = candidates[0]
        if kind != socket.SOCK_STREAM or protocol not in (0, socket.IPPROTO_TCP):
            raise ValueError('unsupported address result')
        if families[family_name] != socket.AF_UNSPEC and family != families[family_name]:
            raise ValueError('address family mismatch')
        if family == socket.AF_INET:
            if len(address) != 2:
                raise ValueError('unsupported IPv4 address')
            selected_family = 'inet'
        elif family == socket.AF_INET6:
            if len(address) != 4 or address[2] != 0 or address[3] != 0:
                raise ValueError('scoped IPv6 is unsupported')
            selected_family = 'inet6'
        else:
            raise ValueError('unsupported address family')
        if type(address[1]) is not int or address[1] != target['port']:
            raise ValueError('address port mismatch')
        endpoint = _endpoint({'address': address[0], 'family': selected_family})
    except (OSError, ValueError, TypeError, IndexError, SSHSessionError) as error:
        raise SSHSessionError('configuration') from error
    command = _master_command(host, control_path, target, endpoint, host_key_alias)
    effective = _configuration(command)
    _safe_configuration(effective)
    if (
        _option(effective, 'hostname') != endpoint['address']
        or _option(effective, 'addressfamily') != endpoint['family']
        or _option(effective, 'hostkeyalias') != host_key_alias
    ):
        raise SSHSessionError('configuration')
    changed = {'hostname', 'addressfamily', 'hostkeyalias'}
    if (
        {key: value for key, value in baseline.items() if key not in changed}
        != {key: value for key, value in effective.items() if key not in changed}
    ):
        raise SSHSessionError('configuration')
    return target, endpoint, command

def _socket_private(path):
    try:
        info = path.lstat()
    except FileNotFoundError as error:
        raise SSHSessionError("connection_lost") from error
    if not stat.S_ISSOCK(info.st_mode) or info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) != 0o600:
        raise SSHSessionError("unsafe_state")

def _client_command(store, target, *, control=None, argv=None):
    # Dead mux executes only false; never permit normal direct-TCP fallback.
    command = ["ssh", "-T", "-F", "/dev/null", "-S", str(store.socket), "-oControlMaster=no", "-oProxyCommand=/usr/bin/false", "-oProxyJump=none", "-oBatchMode=yes", "-oForwardAgent=no", "-oClearAllForwardings=yes", "-oConnectionAttempts=1", "-oConnectTimeout=1", "-l", target["user"], "-p", str(target["port"])]
    if control:
        command += ["-O", control]
    command += [target["hostname"]]
    if argv is not None:
        command.append(shlex.join(argv))
    return command

def _check(store, target):
    try:
        _socket_private(store.socket)
        return subprocess.run(_client_command(store, target, control="check"), stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=3).returncode == 0
    except (OSError, subprocess.SubprocessError, SSHSessionError):
        return False

def _category(stderr):
    text = stderr.lower()
    if b"host key verification failed" in text or b"host identification has changed" in text:
        return "host_key_error"
    if b"permission denied" in text:
        return "authentication_failed"
    if b"timed out" in text or b"timeout" in text:
        return "connect_timeout"
    return "connection_failed"

class SSHSession:
    def __init__(self, host, directory, *, borrow_only=False, clock=time.time):
        if not isinstance(host, str) or not re.fullmatch(r"[A-Za-z0-9_][A-Za-z0-9_.@-]*", host):
            raise SSHSessionError("configuration")
        self.host = host
        self.store = _Store(directory, clock)
        self.id = None
        self.target = None
        self._owner_fd = None
        self._master = None
        self._closed = False
        self._tail = bytearray()
        self._tail_lock = threading.Lock()
        self._drainer = None
        if borrow_only:
            self._attach()
            return
        try:
            self._owner_fd = _lock_file(self.store.directory / "owner.lock", nonblocking=True)
        except BlockingIOError:
            self._attach()
            return
        try:
            self._start()
            atexit.register(self.close)
        except BaseException:
            self.close()
            raise

    def _attach(self):
        with self.store.locked() as (state, now):
            if state["circuit"]:
                raise SSHSessionError(state["circuit"])
            active = state["active"]
            if active is None or not active["ready"] or active["host_alias"] != self.host:
                raise SSHSessionError("no_ready_session")
            if not _alive(active["owner_pid"]) or not _alive(active["master_pid"]):
                raise SSHSessionError("unclean_session")
            _socket_private(self.store.socket)
            self.id, self.target, self.endpoint = active["id"], active["target"], active["endpoint"]

    def _drain(self):
        try:
            while True:
                block = self._master.stderr.read(4096)
                if not block:
                    return
                with self._tail_lock:
                    self._tail.extend(block)
                    del self._tail[:-16384]
        except (OSError, ValueError):
            return

    def _tail_bytes(self):
        if self._drainer is not None:
            self._drainer.join(timeout=1)
        with self._tail_lock:
            return bytes(self._tail)

    def _trip(self, category):
        with self.store.locked() as (state, now):
            if state["active"] is not None and state["active"]["id"] == self.id:
                state["circuit"] = category
                self.store.save(state, now)

    def _start(self):
        with self.store.locked() as (state, now):
            if state["circuit"]:
                raise SSHSessionError(state["circuit"])
            if state["active"] is not None:
                raise SSHSessionError("unclean_session")
            recent = [x for x in state["attempts"] if x > now - WINDOW_SECONDS]
            if len(recent) >= MAX_ATTEMPTS:
                state["counters"]["denied"] += 1
                self.store.save(state, now)
                raise SSHSessionError("attempt_budget_exhausted")
            self.target, self.endpoint, command = _resolve(self.host, self.store.socket)
            if os.path.lexists(self.store.socket):
                raise SSHSessionError("orphaned_control_socket")
            now = self.store.current_time(state, minimum=now)
            recent = [x for x in state["attempts"] if x > now - WINDOW_SECONDS]
            if len(recent) >= MAX_ATTEMPTS:
                state["counters"]["denied"] += 1
                self.store.save(state, now)
                raise SSHSessionError("attempt_budget_exhausted")
            self.id = uuid.uuid4().hex
            state["attempts"] = recent + [now]
            state["counters"]["admitted"] += 1
            state["active"] = {"id": self.id, "host_alias": self.host, "owner_pid": os.getpid(), "master_pid": None, "ready": False, "target": self.target, "endpoint": self.endpoint}
            self.store.save(state, now)
        try:
            self._master = subprocess.Popen(command, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, umask=0o077, start_new_session=True)
            self._drainer = threading.Thread(target=self._drain, name="ssh-stderr", daemon=True)
            self._drainer.start()
            with self.store.locked() as (state, now):
                state["active"]["master_pid"] = self._master.pid
                self.store.save(state, now)
            deadline = time.monotonic() + 16
            while time.monotonic() < deadline:
                if self._master.poll() is not None:
                    category = _category(self._tail_bytes())
                    self._trip(category)
                    raise SSHSessionError(category)
                if _check(self.store, self.target):
                    with self.store.locked() as (state, now):
                        _settle_attempt(state, now)
                        state["active"]["ready"] = True
                        self.store.save(state, now)
                    return
                time.sleep(0.05)
            self._trip("connect_timeout")
            raise SSHSessionError("connect_timeout")
        except OSError as error:
            self._trip("client_error")
            raise SSHSessionError("client_error") from error

    def run_bytes(self, argv, *, data=None):
        if self._closed:
            raise SSHSessionError("closed_session")
        if not isinstance(argv, list) or not argv or any(not isinstance(x, str) or "\x00" in x for x in argv):
            raise ValueError("Expected a nonempty remote argv of strings")
        with self.store.locked() as (state, now):
            if state["circuit"]:
                raise SSHSessionError(state["circuit"])
            active = state["active"]
            if active is None or active["id"] != self.id or not active["ready"]:
                raise SSHSessionError("session_changed")
            try:
                _socket_private(self.store.socket)
            except SSHSessionError as error:
                if error.category == "connection_lost":
                    state["circuit"] = "connection_lost"
                    self.store.save(state, now)
                raise
            state["counters"]["rpc_requests"] += 1
            self.store.save(state, now)
            command = _client_command(self.store, self.target, argv=argv)
            try:
                result = subprocess.run(command, input=data, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=90)
            except subprocess.TimeoutExpired as error:
                state["circuit"] = "command_timeout"
                self.store.save(state, max(now, self.store.clock()))
                raise SSHSessionError("command_timeout") from error
            except OSError as error:
                state["circuit"] = "client_error"
                self.store.save(state, max(now, self.store.clock()))
                raise SSHSessionError("client_error") from error
            if result.returncode < 0:
                state["circuit"] = "client_error"
                self.store.save(state, max(now, self.store.clock()))
                raise SSHSessionError("client_error")
            if result.returncode == 255:
                state["circuit"] = "connection_lost"
                self.store.save(state, max(now, self.store.clock()))
                raise SSHSessionError("connection_lost")
            if result.returncode:
                raise subprocess.CalledProcessError(result.returncode, command, output=result.stdout, stderr=result.stderr)
            return result.stdout

    def snapshot(self):
        with self.store.locked() as (state, now):
            return {"observed_at": now, "attempts_in_rolling_hour": sum(x > now - WINDOW_SECONDS for x in state["attempts"]), "maximum_attempts": MAX_ATTEMPTS, "maximum_live_connections": 1, "session_record_active": state["active"] is not None, "session_owner": self._owner_fd is not None, "circuit": state["circuit"], "counters": dict(state["counters"])}

    def _stop_master(self):
        if self._master is None:
            return
        if self._master.poll() is None:
            self._master.terminate()
            try:
                self._master.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self._master.kill()
                self._master.wait(timeout=5)
        else:
            self._master.wait(timeout=1)
        if self._drainer is not None:
            self._drainer.join(timeout=1)
        if self._master.stderr is not None and not self._master.stderr.closed:
            self._master.stderr.close()

    def close(self):
        if self._closed:
            return
        self._closed = True
        if self._owner_fd is None:
            return
        try:
            with self.store.locked() as (state, now):
                was_dead = self._master is not None and self._master.poll() is not None
                self._stop_master()
                active = state["active"]
                if active is not None and active["id"] == self.id:
                    if self._master is None and state["circuit"] is None:
                        state["circuit"] = "client_error"
                    if was_dead and state["circuit"] is None:
                        state["circuit"] = "connection_lost"
                    if state["circuit"] is None:
                        if os.path.lexists(self.store.socket):
                            _socket_private(self.store.socket)
                            self.store.socket.unlink()
                        if not active["ready"]:
                            now = self.store.current_time(state, minimum=now)
                            _settle_attempt(state, now)
                        state["active"] = None
                    self.store.save(state, now)
        finally:
            try:
                self._stop_master()
            finally:
                os.close(self._owner_fd)
                self._owner_fd = None

    def __enter__(self):
        return self
    def __exit__(self, *unused):
        self.close()

def _alive(pid):
    if pid is None:
        return False
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True

def recover(directory, *, clock=time.time):
    store = _Store(directory, clock)
    try:
        owner = _lock_file(store.directory / "owner.lock", nonblocking=True)
    except BlockingIOError as error:
        raise SSHSessionError("session_still_owned") from error
    try:
        with store.locked() as (state, now):
            active = state["active"]
            if active is not None:
                if active["master_pid"] is None:
                    raise SSHSessionError("uncertain_master_identity")
                if _alive(active["owner_pid"]) or _alive(active["master_pid"]):
                    raise SSHSessionError("old_process_still_alive")
                if _check(store, active["target"]):
                    raise SSHSessionError("master_still_alive")
            if active is None and os.path.lexists(store.socket):
                raise SSHSessionError("orphaned_control_socket")
            if os.path.lexists(store.socket):
                _socket_private(store.socket)
                store.socket.unlink()
            if active is not None:
                now = store.current_time(state, minimum=now)
                _settle_attempt(state, now)
            state["active"] = None
            state["circuit"] = None
            store.save(state, now)
    finally:
        os.close(owner)

def record_external_attempt(directory, timestamp, *, clock=time.time):
    store = _Store(directory, clock)
    with store.locked() as (state, now):
        if state["active"] is not None:
            raise SSHSessionError("session_still_owned")
        if type(timestamp) not in (int, float) or not math.isfinite(timestamp) or timestamp < 0 or timestamp > now:
            raise ValueError("External attempt timestamp must be finite and in the past")
        recent = [x for x in state["attempts"] if x > now - WINDOW_SECONDS]
        if timestamp > now - WINDOW_SECONDS:
            if len(recent) >= MAX_ATTEMPTS:
                raise SSHSessionError("attempt_budget_exhausted")
            recent.append(timestamp)
            recent.sort()
            state["counters"]["admitted"] += 1
        state["attempts"] = recent
        store.save(state, now)

def status(directory, *, clock=time.time):
    store = _Store(directory, clock)
    with store.locked() as (state, now):
        active = state["active"]
        return {"observed_at": now,
                "attempts_in_rolling_hour": sum(x > now-WINDOW_SECONDS for x in state["attempts"]),
                "maximum_attempts": MAX_ATTEMPTS, "maximum_live_connections": 1,
                "session_record_active": active is not None,
                "ready": bool(active and active["ready"]),
                "owner_process_alive": bool(active and _alive(active["owner_pid"])),
                "master_process_alive": bool(active and _alive(active["master_pid"])),
                "circuit": state["circuit"], "counters": dict(state["counters"]),
                "new_connections": 0}

def hold(host, directory):
    def terminate(signum, frame):
        raise SystemExit(128 + signum)
    previous = signal.signal(signal.SIGTERM, terminate)
    try:
        with SSHSession(host, directory) as session:
            if session._owner_fd is None:
                raise SSHSessionError("session_already_owned")
            print(json.dumps({"session_ready": True, **session.snapshot()}), flush=True)
            while True:
                if session._master.poll() is not None:
                    session._trip("connection_lost")
                    raise SSHSessionError("connection_lost")
                snapshot = session.snapshot()
                if snapshot["circuit"]:
                    raise SSHSessionError(snapshot["circuit"])
                time.sleep(1)
    finally:
        signal.signal(signal.SIGTERM, previous)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--state-dir", type=Path, required=True)
    modes = parser.add_subparsers(dest="mode", required=True)
    modes.add_parser("status", help="Read sanitized state without SSH")
    holder=modes.add_parser("hold", help="Own one foreground master; no automatic retries")
    holder.add_argument("--host", required=True)
    modes.add_parser("recover", help="Offline controlled recovery; never opens SSH")
    record = modes.add_parser("record-attempt", help="Append a known external attempt without resetting history")
    record.add_argument("--at", type=float, required=True)
    run = modes.add_parser("run", help="Borrow an existing master only; never opens a physical connection")
    run.add_argument("--host", required=True)
    run.add_argument("--stdin", action="store_true", help="Forward at most32MiB of standard input as bytes")
    run.add_argument("command", nargs=argparse.REMAINDER)
    args = parser.parse_args(argv)
    try:
        if args.mode == "status":
            print(json.dumps(status(args.state_dir)))
        elif args.mode == "hold":
            hold(args.host, args.state_dir)
        elif args.mode == "recover":
            recover(args.state_dir)
            print(json.dumps({"observed_at": time.time(),"reconciled": True, "new_connections": 0}))
        elif args.mode == "record-attempt":
            record_external_attempt(args.state_dir, args.at)
            print(json.dumps({"observed_at": time.time(),"recorded": True, "new_connections": 0}))
        else:
            command = args.command[1:] if args.command[:1] == ["--"] else args.command
            import sys
            data = sys.stdin.buffer.read(32*1024*1024+1) if args.stdin else None
            if data is not None and len(data)>32*1024*1024:
                raise SSHSessionError("input_too_large")
            with SSHSession(args.host, args.state_dir, borrow_only=True) as session:
                sys.stdout.buffer.write(session.run_bytes(command, data=data))
        return 0
    except KeyboardInterrupt:
        return 130
    except SSHSessionError as error:
        print(json.dumps({"observed_at": time.time(),"transport_blocked": True, "error_category": error.category, "physical_attempt_may_have_been_admitted": args.mode == "hold"}))
        return 2
    except subprocess.CalledProcessError as error:
        print(json.dumps({"observed_at": time.time(),"remote_command_failed": True, "returncode": error.returncode}))
        return error.returncode if 0 < error.returncode < 255 else 2

if __name__ == "__main__":
    raise SystemExit(main())
