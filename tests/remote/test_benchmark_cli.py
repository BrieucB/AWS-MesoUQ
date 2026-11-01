"""Credential-free public CLI/controller integration with real supervised processes."""
from __future__ import annotations

import argparse
import hashlib
import io
import json
import os
from pathlib import Path
import runpy
import signal
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from aws_mesouq import benchmark_cli as cli
from aws_mesouq.connector.auth import TokenSession
from aws_mesouq.connector.auth_broker import AuthBroker, BrokerError
from aws_mesouq.connector.benchmark_supervisor import SourceWorkerSpec, WorkerSupervisor, source_binding

ROOT = Path(__file__).resolve().parents[2]


def wait_for(test, timeout=10):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        value = test()
        if value:
            return value
        time.sleep(0.025)
    pytest.fail("Bounded synthetic CLI observation did not arrive")


@pytest.fixture
def cli_rig(tmp_path):
    # Existing public service/worker fixture supplies a genuine isolated enrollment.
    namespace = runpy.run_path(str(ROOT / "tests/remote/test_benchmark_worker.py"))
    generator = namespace["session"].__wrapped__(tmp_path)
    fixture = next(generator)
    fixture.store.state["benchmark_campaigns"].clear()
    identity_before = (tmp_path / "journal/journal.json").read_bytes()
    key_path = tmp_path / "journal/archive_ed25519.pem"
    key_before = hashlib.sha256(key_path.read_bytes()).hexdigest() if key_path.exists() else None
    try:
        next(generator)
    except StopIteration:
        pass
    failures = []
    requests = []

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            self.dispatch("GET")

        def do_POST(self):
            self.dispatch("POST")

        def dispatch(self, method):
            try:
                assert self.headers.get("Authorization") == "Bearer fixture-access-token"
                count = int(self.headers.get("Content-Length", "0"))
                assert 0 <= count <= 131072
                payload = json.loads(self.rfile.read(count)) if count else None
                status, response = fixture.app.handle(method, self.path, {"sub": "alice"}, payload)
                requests.append((method, self.path))
                data = json.dumps(response).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)
            except Exception as error:
                failures.append(type(error).__name__)
                self.send_error(500)

        def log_message(self, *_args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    staging = runpy.run_path(str(ROOT / "tests/remote/test_auth_broker.py"))
    package, manifest = staging["stage_package"](tmp_path)
    target = package / "worker_entry.py"
    target.write_bytes((ROOT / "tests/fixtures/benchmark_cli_worker.py").read_bytes())
    target.chmod(0o600)
    hashes = {p.relative_to(package).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest()
              for p in package.rglob("*") if p.is_file()}
    binding = source_binding("worker_entry.py", hashes)
    manifest = {"schema": "aws_mesouq.approved_source_worker.v1",
                "entrypoint": "worker_entry.py", "files": hashes, "binding_sha256": binding}
    operator_path = tmp_path / "synthetic-operator.json"
    operator_path.write_text(json.dumps(fixture.operator))
    operator_path.chmod(0o600)
    observations = tmp_path / "worker-observations.jsonl"
    fail_transport = tmp_path / "fail-transport"
    control_path = tmp_path / "synthetic-boundaries.json"
    control_path.write_text(json.dumps({"endpoint": f"http://127.0.0.1:{server.server_port}",
                                     "observations": str(observations),
                                     "fail_transport": str(fail_transport)}))
    control_path.chmod(0o600)
    arguments = (str(control_path), "--api-url", "https://api.example.invalid",
                 "--project", "p", "--operator-config", str(operator_path),
                 "--journal", str(tmp_path / "journal"), "--local-cluster",
                 "--poll-seconds", "1")
    specification = SourceWorkerSpec.from_manifest(package, manifest, binding, arguments=arguments)
    login = []
    def factory():
        login.append("login")
        return TokenSession("https://issuer.invalid", "fixture-client",
                            {"access_token": "fixture-access-token", "refresh_token": "fixture-refresh-secret",
                             "expires_in": 3600})
    broker = AuthBroker(factory)
    supervisor = WorkerSupervisor(broker, specification)
    controller = cli.Controller(supervisor)
    value = argparse.Namespace(**locals())
    try:
        yield value
    finally:
        controller.close()
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def observations(rig):
    if not rig.observations.exists():
        return []
    return [json.loads(line) for line in rig.observations.read_text().splitlines()]


def worker_ready(rig, pid):
    return any(value["pid"] == pid and value.get("path") == "/v1/projects/p/benchmarks"
               for value in observations(rig))


def test_public_cli_process_restarts_preserve_normal_enrollment_and_auth(cli_rig, tmp_path):
    rig = cli_rig
    sentinel = subprocess.Popen([sys.executable, "-c", "import time;time.sleep(30)"])
    pids = []
    try:
        first = rig.controller.start()
        pids.append(first["worker_pid"])
        wait_for(lambda: worker_ready(rig, pids[-1]))
        for _ in range(3):
            status = rig.controller.command({"operation": "restart"})
            assert status["worker_state"] == "running"
            pids.append(status["worker_pid"])
            wait_for(lambda: worker_ready(rig, pids[-1]))
        assert len(set(pids)) == 4
        assert rig.login == ["login"]
        assert not rig.failures
        state = json.loads((tmp_path / "journal/journal.json").read_text())
        original = json.loads(rig.identity_before)
        assert state["archive_device"] == original["archive_device"]
        assert state["owner_sub"] == original["owner_sub"] == "alice"
        assert state["jobs"]["existing-calibration"] == {"retained": True}
        assert state["benchmark_lab"]["context"] == original["benchmark_lab"]["context"]
        assert rig.fixture.cluster.submit_count == 0
        assert sentinel.poll() is None
        if rig.key_before is not None:
            assert hashlib.sha256(rig.key_path.read_bytes()).hexdigest() == rig.key_before
        proof = {"actual_platform": sys.platform, "darwin_actual_run": sys.platform == "darwin",
                 "controller_pid": os.getpid(), "worker_pids": pids, "login_calls": len(rig.login),
                 "normal_enrollment_unchanged": True, "native_submissions": 0,
                 "unrelated_sentinel_alive": True, "source_binding": rig.binding}
        evidence = os.environ.get("AWS_MESOUQ_CLI_QA_EVIDENCE")
        if evidence:
            Path(evidence).write_text(json.dumps(proof, sort_keys=True, indent=2) + "\n")
    finally:
        sentinel.terminate()
        sentinel.wait(timeout=5)


def test_worker_ssh_failure_pauses_without_losing_auth_and_owner_restarts(cli_rig):
    rig = cli_rig
    first = rig.controller.start()["worker_pid"]
    wait_for(lambda: worker_ready(rig, first))
    rig.fail_transport.write_text("synthetic failure")
    wait_for(lambda: rig.supervisor._process.poll() is not None)
    status = rig.controller.command({"operation": "status"})
    assert status["worker_state"] == "paused"
    assert status["worker_exit_code"] == 2
    assert status["controller_running"] is True
    rig.fail_transport.unlink()
    status = rig.controller.command({"operation": "restart"})
    assert status["worker_pid"] != first
    wait_for(lambda: worker_ready(rig, status["worker_pid"]))
    assert rig.login == ["login"]
    assert rig.fixture.cluster.submit_count == 0


def test_startup_rejection_retains_auth_and_guarded_retry(cli_rig, monkeypatch):
    rig = cli_rig
    original = rig.supervisor.start_worker
    monkeypatch.setattr(rig.supervisor, "start_worker",
                        lambda: (_ for _ in ()).throw(RuntimeError("sensitive-provider-text")))
    status = rig.controller.start()
    assert status["worker_state"] == "paused"
    assert status["controller_error"] == "worker_start_failed"
    assert status["controller_running"] is True
    assert "sensitive" not in json.dumps(status)
    monkeypatch.setattr(rig.supervisor, "start_worker", original)
    restarted = rig.controller.command({"operation": "restart"})
    wait_for(lambda: worker_ready(rig, restarted["worker_pid"]))
    assert rig.login == ["login"]


@pytest.mark.parametrize("value", [
    {}, {"operation": "shell"}, {"operation": "restart", "argv": ["bad"]},
    {"operation": "status", "token": "secret"}, {"operation": "use_spec"},
    [], {"operation": 1},
])
def test_owner_commands_have_no_shell_or_auth_override(value):
    controller = cli.Controller(None)
    with pytest.raises(BrokerError):
        controller.command(value)


def test_owner_command_loop_bounds_json_and_eof_detaches():
    readfd, writefd = os.pipe()
    stop = threading.Event()
    output = io.StringIO()
    class FakeController:
        def start(self):
            return {"controller_running": True}
        def command(self, value):
            assert set(value) == {"operation"}
            return {"stopping": True} if value["operation"] == "stop" else {"ok": True}
    thread = threading.Thread(target=cli.run_controller_loop,
                              args=(FakeController(),),
                              kwargs={"input_fd": readfd, "output": output, "stop": stop})
    thread.start()
    try:
        os.write(writefd, b"x" * (cli.MAX_COMMAND_BYTES + 1) + b"\n")
        os.write(writefd, b'{"operation":"status","operation":"stop"}\n')
        os.write(writefd, b'{"operation":"status"}\n')
        wait_for(lambda: '"ok":true' in output.getvalue())
        os.close(writefd)
        writefd = None
        time.sleep(0.3)
        assert thread.is_alive()
        assert "owner_command_too_large" in output.getvalue()
        assert "invalid_owner_command" in output.getvalue()
    finally:
        stop.set()
        thread.join(timeout=5)
        if writefd is not None:
            os.close(writefd)
        os.close(readfd)
    assert not thread.is_alive()


def test_registration_is_kind_and_whole_package_pinned(tmp_path):
    path = tmp_path / "manifest.json"
    path.write_text(json.dumps({"entrypoint": "arbitrary.py"}))
    path.chmod(0o600)
    with pytest.raises(BrokerError):
        cli.approved_worker(tmp_path, path, "a" * 64, (), kind="untrusted")
    with pytest.raises(BrokerError):
        cli.approved_worker(tmp_path, path, "a" * 64, ())
    linked = tmp_path / "symlink.json"
    linked.symlink_to(path)
    with pytest.raises(BrokerError):
        cli._manifest(linked)
    with pytest.raises(BrokerError):
        cli._json('{"operation":"status","operation":"stop"}')


def test_managed_worker_has_no_browser_login_fallback(tmp_path, monkeypatch):
    parser = argparse.ArgumentParser()
    cli._runtime_parser(parser)
    args = parser.parse_args(["--api-url", "https://api.invalid", "--project", "p",
                              "--operator-config", str(tmp_path / "not-read"),
                              "--journal", str(tmp_path), "--local-cluster"])
    for key in ("AWS_MESOUQ_AUTH_SOCKET", "AWS_MESOUQ_AUTH_CONTROLLER_PID",
                "AWS_MESOUQ_AUTH_GENERATION"):
        monkeypatch.delenv(key, raising=False)
    with pytest.raises(BrokerError):
        cli.run_managed_worker(args)


def test_legacy_flags_delegate_unchanged(monkeypatch):
    import aws_mesouq.benchmark_worker as worker
    seen = []
    monkeypatch.setattr(worker, "main", lambda argv: seen.append(argv) or 17)
    assert cli.main(["--api-url", "https://api.invalid"]) == 17
    assert seen == [["--api-url", "https://api.invalid"]]


@pytest.mark.parametrize("value", ["nan", "inf", "-1", "0"])
def test_poll_interval_rejects_unbounded_values(value, tmp_path):
    parser = argparse.ArgumentParser()
    cli._runtime_parser(parser)
    args = parser.parse_args(["--api-url", "https://api.invalid", "--project", "p",
                              "--operator-config", str(tmp_path / "operator"),
                              "--journal", str(tmp_path), "--local-cluster",
                              "--poll-seconds", value])
    with pytest.raises(SystemExit):
        cli._validate_runtime(parser, args)

def test_invalid_owner_upgrade_preserves_live_worker_and_auth(cli_rig):
    rig = cli_rig
    first = rig.controller.start()["worker_pid"]
    wait_for(lambda: worker_ready(rig, first))
    def reject(_value):
        raise BrokerError("invalid_source_specification")
    rig.controller.specification_loader = reject
    status = rig.controller.command({"operation": "use_spec", "worker_kind": "benchmark-v1",
                                     "package_root": "/bad", "manifest": "/bad", "binding": "a" * 64})
    assert status["worker_pid"] == first
    assert status["worker_state"] == "running"
    assert status["controller_error"] == "worker_restart_failed"
    assert rig.login == ["login"]
