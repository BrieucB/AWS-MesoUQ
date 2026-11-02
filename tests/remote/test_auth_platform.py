"""Regression checks for native Darwin peer IDs and bounded source validation."""
from __future__ import annotations
import ctypes
import hashlib
import os
from pathlib import Path
import struct
import subprocess
import sys
from types import SimpleNamespace
import pytest
from aws_mesouq.connector import auth_broker as broker
from aws_mesouq.connector import benchmark_supervisor as supervisor

class NativeGetPeer:
    def __init__(self, *, status=0, set_ids=True):
        self.status, self.set_ids = status, set_ids
        self.calls = []
    def __call__(self, fd, uid, gid):
        self.calls.append(fd)
        if self.set_ids:
            uid._obj.value, gid._obj.value = 501, 20
        return self.status

def fake_darwin(monkeypatch, *, failure=None):
    native = NativeGetPeer(status=-1 if failure == "native" else 0,
                           set_ids=failure != "unset")
    monkeypatch.setattr(sys, "platform", "darwin")
    def load(name, *, use_errno):
        assert name is None and use_errno is True
        if failure == "loader":
            raise OSError("native-error-must-not-leak")
        return SimpleNamespace() if failure == "symbol" else SimpleNamespace(getpeereid=native)
    monkeypatch.setattr(ctypes, "CDLL", load)
    def getsockopt(level, option, size):
        assert (level, option, size) == (0, 2, 4)
        if failure == "short_pid":
            return b"\0"
        return struct.pack("i", 0 if failure == "pid" else 12345)
    return SimpleNamespace(fileno=lambda: 42, getsockopt=getsockopt), native

def test_darwin_uid_and_pid_are_kernel_derived_without_socket_getpeereid(monkeypatch):
    peer, native = fake_darwin(monkeypatch)
    assert not hasattr(peer, "getpeereid")
    assert broker._peer_identity(peer) == (12345, 501)
    assert native.calls == [42]
    assert native.restype is ctypes.c_int
    assert native.argtypes == [ctypes.c_int, ctypes.POINTER(ctypes.c_uint32),
                               ctypes.POINTER(ctypes.c_uint32)]

@pytest.mark.parametrize("failure", ["loader", "symbol", "native", "unset", "short_pid", "pid"])
def test_darwin_peer_lookup_fails_closed_and_redacts_native_errors(monkeypatch, failure):
    peer, _ = fake_darwin(monkeypatch, failure=failure)
    with pytest.raises(broker.BrokerSecurityError, match="peer_identity") as caught:
        broker._peer_identity(peer)
    assert "native-error-must-not-leak" not in str(caught.value)

@pytest.mark.parametrize("validation", ["parent", "bootstrap"])
def test_fifo_source_validation_is_bounded(tmp_path, validation):
    package = tmp_path.resolve() / "package"
    package.mkdir(mode=0o700)
    os.mkfifo(package / "worker.py", 0o600)
    files = {"worker.py": hashlib.sha256(b"").hexdigest()}
    spec = supervisor.SourceWorkerSpec(
        package, "worker.py", tuple(files.items()),
        supervisor.source_binding("worker.py", files))
    if validation == "parent":
        source_root = str(Path(supervisor.__file__).resolve().parents[2])
        code = (
            "import sys;sys.path.insert(0,"+repr(source_root)+");"
            "from pathlib import Path;"
            "from aws_mesouq.connector.benchmark_supervisor import SourceWorkerSpec,source_binding;"
            "from aws_mesouq.connector.auth_broker import BrokerSecurityError;"
            "files="+repr(files)+";"
            "spec=SourceWorkerSpec(Path("+repr(str(package))+"),'worker.py',tuple(files.items()),source_binding('worker.py',files))\n"
            "try: spec.validate()\n"
            "except BrokerSecurityError as e:\n"
            " assert str(e)=='unsafe_package_file'\n"
            " raise SystemExit(78)\n"
            "raise SystemExit(2)\n")
        data = None
    else:
        code, data = supervisor._BOOTSTRAP, spec.bootstrap_payload()
    try:
        result = subprocess.run(
            [sys.executable, "-I", "-B", "-c", code], input=data,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=3,
            env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"})
    except subprocess.TimeoutExpired:
        pytest.fail("FIFO source validation blocked instead of rejecting the non-regular file")
    assert result.returncode == 78, result.stderr.decode()[-500:]
