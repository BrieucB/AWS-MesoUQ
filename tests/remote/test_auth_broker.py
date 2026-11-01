"""Security and real-process evidence for the memory-only auth controller."""
from __future__ import annotations

import dataclasses
import hashlib
import json
import os
from pathlib import Path
import shutil
import socket
import stat
import subprocess
import sys
import threading
import time
import uuid

import pytest

from aws_mesouq.connector import auth
from aws_mesouq.connector.auth import SessionExpired, TokenSession
from aws_mesouq.connector.auth_broker import (
    AuthBroker, BrokerError, BrokerSecurityError, MAX_RESPONSE_BYTES,
    MAX_SOCKET_PATH_BYTES, _read_message,
)
from aws_mesouq.connector.benchmark_supervisor import (
    SourceWorkerSpec, WorkerSupervisor, SPEC_SCHEMA, source_binding,
)

ROOT = Path(__file__).resolve().parents[2]
SUPPORTED = sys.platform.startswith("linux") or sys.platform == "darwin"
pytestmark = pytest.mark.skipif(not SUPPORTED, reason="requires kernel Unix peer PID support")


class Clock:
    def __init__(self):
        self.value = 1000.0
        self.lock = threading.Lock()

    def __call__(self):
        with self.lock:
            return self.value

    def advance(self, value):
        with self.lock:
            self.value += value


class Endpoint:
    """Real TokenSession, synthetic in-memory login and refresh endpoint."""
    def __init__(self, clock):
        self.clock = clock
        self.login_calls = 0
        self.refresh_calls = 0
        self.active = 0
        self.peak = 0
        self.secrets = []
        self.lock = threading.Lock()
        self.revoke = False
        self.entered = threading.Event()
        self.release = None
        self.current_refresh = None

    def response(self):
        access = "qa-access-" + uuid.uuid4().hex
        refresh = "qa-refresh-" + uuid.uuid4().hex
        self.secrets.extend((access, refresh))
        self.current_refresh = refresh
        return {
            "access_token": access, "refresh_token": refresh,
            "expires_in": 3600, "token_type": "Bearer",
        }

    def login(self):
        self.login_calls += 1
        return TokenSession(
            "https://issuer.example.test", "qa-client", self.response(),
            clock=self.clock,
        )

    def request(self, issuer, fields):
        with self.lock:
            if (
                issuer != "https://issuer.example.test"
                or fields.get("grant_type") != "refresh_token"
                or fields.get("client_id") != "qa-client"
                or fields.get("refresh_token") != self.current_refresh
            ):
                raise RuntimeError("synthetic_refresh_contract_mismatch")
            self.refresh_calls += 1
            self.active += 1
            self.peak = max(self.peak, self.active)
        self.entered.set()
        try:
            if self.release is not None and not self.release.wait(10):
                raise RuntimeError("synthetic_refresh_release_timeout")
            time.sleep(0.03)
            if self.revoke:
                raise SessionExpired("synthetic revoked " + self.current_refresh)
            return self.response()
        finally:
            with self.lock:
                self.active -= 1


def stage_package(tmp_path):
    root = tmp_path / "approved-source"
    root.mkdir(mode=0o700)
    shutil.copytree(
        ROOT / "src/aws_mesouq", root / "aws_mesouq",
        ignore=shutil.ignore_patterns("__pycache__", "*.pyc", "*.pyo"),
    )
    shutil.copy2(ROOT / "tests/fixtures/auth_broker_worker.py", root / "worker_entry.py")
    for directory, dirs, files in os.walk(root):
        Path(directory).chmod(0o700)
        for name in files:
            (Path(directory) / name).chmod(0o600)
    hashes = {
        p.relative_to(root).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest()
        for p in root.rglob("*") if p.is_file()
    }
    binding = source_binding("worker_entry.py", hashes)
    manifest = {
        "schema": SPEC_SCHEMA, "entrypoint": "worker_entry.py",
        "files": hashes, "binding_sha256": binding,
    }
    return root, manifest


def specification(package, tmp_path, result_name, mode="stay", control=None):
    root, manifest = package
    args = (
        "--journal", str(tmp_path / "journal"),
        "--result", str(tmp_path / result_name), "--mode", mode,
    )
    if control is not None:
        args += ("--control", str(control))
    return SourceWorkerSpec.from_manifest(
        root, manifest, manifest["binding_sha256"], arguments=args
    )


def wait_result(path, supervisor, phase=None):
    deadline = time.monotonic() + 12
    while time.monotonic() < deadline:
        if path.exists():
            value = json.loads(path.read_text())
            if phase is None or value.get("phase") == phase:
                return value
        if supervisor._process is not None and supervisor._process.poll() is not None:
            pytest.fail("owned test worker exited before its safe result")
        time.sleep(0.02)
    pytest.fail("bounded test worker result timeout")


def assert_no_secrets(tmp_path, endpoint, capsys):
    leaked = []
    for path in tmp_path.rglob("*"):
        if path.is_file() and not path.is_symlink():
            data = path.read_bytes()
            if any(value.encode() in data for value in endpoint.secrets):
                leaked.append(path.name)
    captured = capsys.readouterr()
    assert not leaked, "credential material reached an owned QA file"
    assert not any(value in captured.out + captured.err for value in endpoint.secrets), "credential material reached captured output"


@pytest.fixture
def rig(tmp_path, monkeypatch):
    clock = Clock()
    endpoint = Endpoint(clock)
    monkeypatch.setattr(auth, "token_request", endpoint.request)
    package = stage_package(tmp_path)
    broker = AuthBroker(endpoint.login, clock=clock)
    supervisor = WorkerSupervisor(
        broker, specification(package, tmp_path, "result0.json")
    )
    yield clock, endpoint, package, broker, supervisor
    if endpoint.release is not None:
        endpoint.release.set()
    supervisor.close(graceful_timeout=2)


def test_one_login_across_three_real_worker_restarts_and_rotating_refresh(
    rig, tmp_path, capsys
):
    clock, endpoint, package, broker, supervisor = rig
    sentinel = subprocess.Popen(
        [sys.executable, "-I", "-c", "import time;time.sleep(60)"],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    pids = []
    results = []
    previous_exits = []
    try:
        supervisor.start()
        for index in range(4):
            result = wait_result(tmp_path / f"result{index}.json", supervisor)
            assert result["phase"] == "ready"
            assert result["normal_BenchmarkClient_used"] is True
            assert result["normal_API_used"] is True
            assert result["access_token_absent_from_environment"] is True
            assert result["access_token_absent_from_argv"] is True
            assert result["refresh_secret_not_available_on_provider"] is True
            assert result["controller_pid"] == os.getpid()
            pids.append(result["pid"])
            results.append(result)
            if index == 0:
                clock.advance(4000)
            if index < 3:
                previous = supervisor._process
                mode = "api401" if index + 1 == 2 else "stay"
                supervisor.restart_worker(
                    specification(package, tmp_path, f"result{index+1}.json", mode),
                    graceful_timeout=2,
                )
                previous_exits.append(previous.poll())
                assert sentinel.poll() is None
        assert len(set(pids)) == 4
        assert len({result["connector_id"] for result in results}) == 1
        assert [result["generation"] for result in results] == [1, 2, 3, 4]
        assert previous_exits == [0, 0, 0]
        assert endpoint.login_calls == 1
        assert endpoint.refresh_calls == 2
        assert results[2]["api_attempts"] == 2
        assert broker.safe_status()["login_attempts"] == 1
        assert broker.safe_status()["forced_refresh_calls"] == 1
        assert endpoint.peak == 1
        evidence_dir = os.environ.get("AWS_MESOUQ_QA_EVIDENCE_DIR")
        if evidence_dir:
            proof = {
                "scope": "synthetic authentication with real TokenSession/API/BenchmarkClient and kernel-authenticated worker processes; no real OAuth",
                "controller_PID": os.getpid(), "worker_PIDs": pids,
                "actual_worker_restarts": 3, "synthetic_login_factory_calls": 1,
                "normal_refresh_endpoint_calls": 2, "peak_concurrent_refresh": 1,
                "same_normal_SDK_enrollment_connector_id": results[0]["connector_id"],
                "worker_generations": [1, 2, 3, 4],
                "old_workers_exit0_before_next_worker": previous_exits,
                "unrelated_owned_sentinel_survives": sentinel.poll() is None,
                "package_binding_SHA256": package[1]["binding_sha256"],
                "complete_package_file_count": len(package[1]["files"]),
                "kernel_platform": sys.platform,
                "socket_path_bound_bytes": len(os.fsencode(broker.socket_path)),
                "portable_socket_limit": MAX_SOCKET_PATH_BYTES,
                "Darwin_actual_run": sys.platform == "darwin",
            }
            path = Path(evidence_dir) / "real-worker-restarts-proof.private.json"
            path.write_text(json.dumps(proof, sort_keys=True, indent=2) + "\n")
            path.chmod(0o600)
    finally:
        supervisor.stop_worker(graceful_timeout=2)
        sentinel.terminate()
        sentinel.wait(timeout=5)
    assert_no_secrets(tmp_path, endpoint, capsys)


def test_concurrent_force_refresh_is_serialized_and_debounced(rig, tmp_path, capsys):
    _, endpoint, package, broker, supervisor = rig
    supervisor._spec = specification(package, tmp_path, "concurrent.json", "concurrent")
    supervisor.start()
    result = wait_result(tmp_path / "concurrent.json", supervisor)
    assert result["concurrent_successes"] == 8
    assert result["concurrent_failures"] == []
    assert endpoint.refresh_calls == 1
    assert endpoint.peak == 1
    assert endpoint.login_calls == 1
    assert broker.safe_status()["forced_refresh_calls"] == 1
    assert_no_secrets(tmp_path, endpoint, capsys)


def test_revocation_requires_controller_auth_but_worker_restart_does_not_login(
    rig, tmp_path, capsys
):
    clock, endpoint, package, broker, supervisor = rig
    supervisor.start()
    wait_result(tmp_path / "result0.json", supervisor)
    endpoint.revoke = True
    clock.advance(4000)
    supervisor.restart_worker(
        specification(package, tmp_path, "revoked1.json"), graceful_timeout=2
    )
    one = wait_result(tmp_path / "revoked1.json", supervisor)
    assert one["phase"] == "error"
    assert one["error_type"] == "SessionExpired"
    assert broker.safe_status()["authentication_required"] is True
    supervisor.restart_worker(
        specification(package, tmp_path, "revoked2.json"), graceful_timeout=2
    )
    two = wait_result(tmp_path / "revoked2.json", supervisor)
    assert two["error_type"] == "SessionExpired"
    assert endpoint.login_calls == 1
    assert endpoint.refresh_calls == 1
    assert_no_secrets(tmp_path, endpoint, capsys)


def test_unknown_size_duplicate_type_and_generation_rpc_guards(
    rig, tmp_path, capsys
):
    _, endpoint, package, broker, supervisor = rig
    supervisor._spec = specification(package, tmp_path, "guards.json", "guards")
    supervisor.start()
    result = wait_result(tmp_path / "guards.json", supervisor)
    assert result["all_denied_without_tokens"] is True
    assert len(result["guard_errors"]) == 11
    assert set(result["guard_errors"]) == {"invalid_rpc", "worker_generation_fenced"}
    assert broker.safe_status()["provider_calls"] == 1
    assert endpoint.login_calls == 1
    assert endpoint.refresh_calls == 0
    assert_no_secrets(tmp_path, endpoint, capsys)


def test_actual_kernel_denies_unregistered_same_uid_process(rig, tmp_path):
    _, _, _, broker, supervisor = rig
    supervisor.start()
    wait_result(tmp_path / "result0.json", supervisor)
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as conn:
        conn.settimeout(3)
        conn.connect(str(broker.socket_path))
        reply = _read_message(conn, MAX_RESPONSE_BYTES)
    assert reply == {"version": 1, "ok": False, "error": "worker_not_authorized"}
    assert "token" not in reply
    assert broker.safe_status()["worker_pid"] != os.getpid()
    with pytest.raises(BrokerSecurityError):
        broker._authorize((supervisor._process.pid, os.geteuid() + 1), 1)


def test_fence_during_refresh_prevents_new_token_response(rig, tmp_path, capsys):
    _, endpoint, package, broker, supervisor = rig
    control = tmp_path / "force-now"
    supervisor._spec = specification(
        package, tmp_path, "fenced.json", "wait-force", control
    )
    supervisor.start()
    wait_result(tmp_path / "fenced.json", supervisor, "ready")
    endpoint.release = threading.Event()
    control.write_text("owned QA coordination only")
    assert endpoint.entered.wait(5)
    broker.fence_worker()
    endpoint.release.set()
    result = wait_result(tmp_path / "fenced.json", supervisor, "forced")
    assert result["new_token_denied"] is True
    assert result["controlled_error"] in ("worker_not_authorized", "worker_generation_fenced", "invalid_rpc")
    assert endpoint.login_calls == 1
    assert endpoint.refresh_calls == 1
    assert_no_secrets(tmp_path, endpoint, capsys)


def test_invalid_upgrade_keeps_current_worker_and_authentication(rig, tmp_path):
    _, endpoint, package, broker, supervisor = rig
    supervisor.start()
    wait_result(tmp_path / "result0.json", supervisor)
    before = supervisor._process.pid
    (package[0] / "unapproved.py").write_text("raise RuntimeError('must not run')\n")
    with pytest.raises(BrokerSecurityError):
        supervisor.apply_spec(specification(package, tmp_path, "unused.json"))
    assert supervisor._process.pid == before
    assert supervisor._process.poll() is None
    assert broker.safe_status()["worker_pid"] == before
    assert endpoint.login_calls == 1


def test_child_rechecks_whole_package_after_parent_registration(
    rig, tmp_path, monkeypatch
):
    _, endpoint, package, broker, supervisor = rig
    original = broker._bind_worker

    def mutate_after_registration(process, generation, binding):
        original(process, generation, binding)
        path = package[0] / "aws_mesouq/connector/api.py"
        path.write_bytes(path.read_bytes() + b"\n# synthetic post-registration mutation\n")

    monkeypatch.setattr(broker, "_bind_worker", mutate_after_registration)
    supervisor.start()
    deadline = time.monotonic() + 8
    while supervisor._process.poll() is None and time.monotonic() < deadline:
        time.sleep(0.02)
    assert supervisor._process.poll() == 78
    assert not (tmp_path / "result0.json").exists()
    assert endpoint.login_calls == 1
    assert broker.safe_status()["provider_calls"] == 1


def test_socket_modes_and_credential_free_status(rig, tmp_path, capsys):
    _, endpoint, _, broker, supervisor = rig
    supervisor.start()
    wait_result(tmp_path / "result0.json", supervisor)
    assert stat.S_IMODE(os.lstat(broker.socket_path.parent).st_mode) == 0o700
    assert stat.S_IMODE(os.lstat(broker.socket_path).st_mode) == 0o600
    status = json.dumps(supervisor.safe_status())
    assert not any(value in status for value in endpoint.secrets)
    assert "refresh_token" not in status and "access_token" not in status
    assert_no_secrets(tmp_path, endpoint, capsys)


@pytest.mark.parametrize("kind", ["file", "socket", "symlink"])
def test_existing_socket_path_is_refused_without_login_or_removal(tmp_path, kind):
    directory = tmp_path / "private"
    directory.mkdir(mode=0o700)
    path = directory / "a.sock"
    sock = None
    if kind == "file":
        path.write_text("preserve")
    elif kind == "socket":
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        sock.bind(str(path))
        path.chmod(0o600)
    else:
        other = tmp_path / "preserve"
        other.write_text("preserve")
        path.symlink_to(other)
    calls = []
    broker = AuthBroker(lambda: calls.append(True), private_socket_dir=directory)
    try:
        with pytest.raises(BrokerError):
            broker.start()
        assert calls == []
        assert os.path.lexists(path)
    finally:
        broker.close()
        if sock is not None:
            sock.close()


def test_unsafe_ancestor_and_symlink_directory_refused_before_login(tmp_path):
    unsafe = tmp_path / "unsafe"
    unsafe.mkdir(mode=0o700)
    private = unsafe / "private"
    private.mkdir(mode=0o700)
    unsafe.chmod(0o770)
    calls = []
    broker = AuthBroker(lambda: calls.append(True), private_socket_dir=private)
    with pytest.raises(BrokerError):
        broker.start()
    assert calls == []
    unsafe.chmod(0o700)
    link = tmp_path / "link"
    link.symlink_to(private, target_is_directory=True)
    broker2 = AuthBroker(lambda: calls.append(True), private_socket_dir=link)
    with pytest.raises(BrokerError):
        broker2.start()
    assert calls == []


def test_portable_socket_path_limit_before_login(tmp_path):
    directory = tmp_path / ("x" * 95)
    directory.mkdir(mode=0o700)
    assert len(os.fsencode(directory / "a.sock")) > 103
    calls = []
    broker = AuthBroker(lambda: calls.append(True), private_socket_dir=directory)
    with pytest.raises(BrokerSecurityError):
        broker.start()
    assert calls == []


def test_complete_manifest_rejects_wrong_approval_traversal_and_unbound_code(tmp_path):
    package = stage_package(tmp_path)
    root, manifest = package
    with pytest.raises(BrokerSecurityError):
        SourceWorkerSpec.from_manifest(root, manifest, "0" * 64)
    with pytest.raises(BrokerSecurityError):
        source_binding("../escape.py", {"../escape.py": "0" * 64})
    with pytest.raises(BrokerSecurityError):
        source_binding("worker_entry.py", {"worker_entry.py": "not-a-hash"})
    spec = specification(package, tmp_path, "unused.json")
    spec.validate()
    link = root / "foreign.py"
    link.symlink_to(root / "worker_entry.py")
    with pytest.raises(BrokerSecurityError):
        spec.validate()


def test_worker_start_requires_running_controller_and_never_logs_in(rig):
    _, endpoint, _, _, supervisor = rig
    with pytest.raises(BrokerError, match="controller_not_running"):
        supervisor.start_worker()
    assert endpoint.login_calls == 0


def test_same_spec_apply_is_noop_and_cold_controller_cannot_restart(rig, tmp_path):
    _, endpoint, _, broker, supervisor = rig
    pid = supervisor.start()
    wait_result(tmp_path / "result0.json", supervisor)
    assert supervisor.apply_spec(supervisor._spec) == pid
    assert endpoint.login_calls == 1
    supervisor.close(graceful_timeout=2)
    with pytest.raises(BrokerError, match="controller_already_started"):
        broker.start()
    with pytest.raises(BrokerError, match="controller_not_running"):
        supervisor.start_worker()
    assert endpoint.login_calls == 1


@pytest.mark.parametrize("error_type", [RuntimeError, BrokerError])
def test_login_factory_exception_text_is_not_exposed(tmp_path, capsys, error_type):
    secret = "qa-factory-secret-" + uuid.uuid4().hex

    def factory():
        raise error_type(secret)

    broker = AuthBroker(factory)
    try:
        with pytest.raises(BrokerError) as failure:
            broker.start()
        assert failure.value.args == ("authentication_start_failed",)
        assert secret not in str(failure.value)
        captured = capsys.readouterr()
        assert secret not in captured.out + captured.err
        for path in tmp_path.rglob("*"):
            if path.is_file():
                assert secret.encode() not in path.read_bytes()
    finally:
        broker.close()


def test_owner_interrupt_cleans_owned_controller_socket(tmp_path):
    def factory():
        raise KeyboardInterrupt()

    broker = AuthBroker(factory)
    with pytest.raises(KeyboardInterrupt):
        broker.start()
    assert broker._socket_path is not None
    assert not broker._socket_path.exists()
    assert broker._directory is not None
    assert not broker._directory.exists()
    broker.close()


def test_owner_interrupt_at_startup_gate_kills_only_owned_worker(rig, monkeypatch):
    _, endpoint, _, broker, supervisor = rig
    original = broker._bind_worker
    owned = []

    def interrupt_after_registration(process, generation, binding):
        original(process, generation, binding)
        owned.append(process)
        raise KeyboardInterrupt()

    monkeypatch.setattr(broker, "_bind_worker", interrupt_after_registration)
    with pytest.raises(KeyboardInterrupt):
        supervisor.start()
    assert len(owned) == 1
    assert owned[0].poll() is not None
    assert supervisor._process is None
    assert broker.safe_status()["worker_active"] is False
    assert endpoint.login_calls == 1
