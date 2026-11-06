"""Private managed-worker startup diagnostics, with synthetic identities only."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import runpy
import socket
import stat
import sys
import tempfile
import time

import pytest

from aws_mesouq import benchmark_cli as cli
from aws_mesouq.connector.api import APIError
from aws_mesouq.connector.auth import SessionExpired
from aws_mesouq.connector.broker_client import CONTROLLER_ENV, GENERATION_ENV, SOCKET_ENV

ROOT = Path(__file__).resolve().parents[2]
SECRET = "PRIVATE_PAYLOAD_CANARY_7d3f"


@pytest.fixture
def broker_directory(monkeypatch):
    temporary_root = "/private/tmp" if sys.platform == "darwin" else "/tmp"
    with tempfile.TemporaryDirectory(prefix="mqsd-", dir=temporary_root) as temporary:
        root = Path(temporary).resolve()
        root.chmod(0o700)
        path = root / "a.sock"
        listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        listener.bind(str(path))
        path.chmod(0o600)
        monkeypatch.setenv(SOCKET_ENV, str(path))
        monkeypatch.setenv(CONTROLLER_ENV, str(os.getppid()))
        monkeypatch.setenv(GENERATION_ENV, "7")
        try:
            yield root
        finally:
            listener.close()


def diagnostic(root):
    return root / f"worker-startup-failure-{os.getpid()}-7.json"


def arguments(tmp_path, operator=None):
    journal = tmp_path / "journal"
    journal.mkdir(exist_ok=True)
    (journal / "journal.json").write_text("{}")
    operator = operator or tmp_path / "operator.json"
    if not operator.exists():
        operator.write_text("{" + SECRET)
    return ["managed-worker", "--api-url", "https://api.example.invalid",
            "--project", "p", "--operator-config", str(operator),
            "--journal", str(journal), "--local-cluster", "--once"]


def test_fixed_private_record_contains_only_approved_metadata(broker_directory):
    start = time.time_ns()
    cli._record_startup_failure("legacy_worker", APIError(403, SECRET, SECRET))
    path = diagnostic(broker_directory)
    data = path.read_bytes()
    record = json.loads(data)
    assert len(data) <= cli.MAX_STARTUP_DIAGNOSTIC_BYTES
    assert stat.S_IMODE(path.stat().st_mode) == 0o600 and path.stat().st_nlink == 1
    assert record == {
        "schema": cli.STARTUP_DIAGNOSTIC_SCHEMA, "phase": "legacy_worker",
        "exception_class": "APIError", "http_status": 403, "source_frame": None,
        "controller_pid": os.getppid(), "worker_pid": os.getpid(),
        "worker_generation": 7, "created_at_unix_ns": record["created_at_unix_ns"],
        "cli_source_sha256": hashlib.sha256(Path(cli.__file__).read_bytes()).hexdigest(),
    }
    assert start <= record["created_at_unix_ns"] <= time.time_ns()
    assert SECRET.encode() not in data


def test_last_sdk_frame_has_no_absolute_path_source_or_locals(broker_directory):
    try:
        cli._json("{" + SECRET)
    except Exception as error:
        cli._record_startup_failure("operator", error)
    record = json.loads(diagnostic(broker_directory).read_bytes())
    assert record["source_frame"]["path"] == "aws_mesouq/benchmark_cli.py"
    assert record["source_frame"]["function"] == "_json"
    assert type(record["source_frame"]["line"]) is int
    assert set(record["source_frame"]) == {"path", "line", "function"}
    assert SECRET not in json.dumps(record)
    assert str(ROOT) not in json.dumps(record)


def test_untrusted_exception_class_and_status_are_not_projected(broker_directory):
    kind = type(SECRET, (Exception,), {"__module__": __name__})
    error = kind(SECRET)
    error.status = 401
    cli._record_startup_failure("operator", error)
    record = json.loads(diagnostic(broker_directory).read_bytes())
    assert record["exception_class"] == "Exception"
    assert "http_status" not in record
    assert SECRET not in json.dumps(record)


@pytest.mark.parametrize("status", [True, 99, 600, SECRET])
def test_http_status_requires_exact_safe_integer(broker_directory, status):
    cli._record_startup_failure("legacy_worker", APIError(status, SECRET, SECRET))
    assert "http_status" not in json.loads(diagnostic(broker_directory).read_bytes())


def test_existing_record_is_preserved(broker_directory):
    path = diagnostic(broker_directory)
    path.write_bytes(b"retained")
    path.chmod(0o600)
    cli._record_startup_failure("operator", ValueError(SECRET))
    assert path.read_bytes() == b"retained"


def test_existing_symlink_is_preserved_without_touching_target(broker_directory, tmp_path):
    target = tmp_path / "synthetic-key"
    target.write_bytes(SECRET.encode())
    path = diagnostic(broker_directory)
    path.symlink_to(target)
    cli._record_startup_failure("operator", ValueError(SECRET))
    assert path.is_symlink()
    assert target.read_bytes() == SECRET.encode()


@pytest.mark.parametrize("unsafe", ["directory_mode", "socket_mode", "socket_type",
                                  "directory_symlink", "controller", "generation", "phase"])
def test_unsafe_destination_or_registration_writes_nothing(
    broker_directory, tmp_path, monkeypatch, unsafe
):
    phase = "operator"
    if unsafe == "directory_mode":
        broker_directory.chmod(0o750)
    elif unsafe == "socket_mode":
        (broker_directory / "a.sock").chmod(0o660)
    elif unsafe == "socket_type":
        (broker_directory / "a.sock").unlink()
        (broker_directory / "a.sock").write_bytes(SECRET.encode())
        (broker_directory / "a.sock").chmod(0o600)
    elif unsafe == "directory_symlink":
        alias = tmp_path / "broker-alias"
        alias.symlink_to(broker_directory, target_is_directory=True)
        monkeypatch.setenv(SOCKET_ENV, str(alias / "a.sock"))
    elif unsafe == "controller":
        monkeypatch.setenv(CONTROLLER_ENV, str(os.getppid() + 1))
    elif unsafe == "generation":
        monkeypatch.setenv(GENERATION_ENV, "0")
    else:
        phase = SECRET
    cli._record_startup_failure(phase, ValueError(SECRET))
    assert not diagnostic(broker_directory).exists()


def test_failed_write_is_tolerated_and_owned_partial_file_removed(broker_directory, monkeypatch):
    def fail(_fd):
        raise OSError(SECRET)
    monkeypatch.setattr(cli.os, "fsync", fail)
    cli._record_startup_failure("operator", ValueError(SECRET))
    assert not diagnostic(broker_directory).exists()


def test_permissive_umask_still_creates_private_record(broker_directory):
    previous = os.umask(0)
    try:
        cli._record_startup_failure("operator", ValueError(SECRET))
    finally:
        os.umask(previous)
    assert stat.S_IMODE(diagnostic(broker_directory).stat().st_mode) == 0o600


def test_full_main_records_setup_failure_and_retains_exit2(
    broker_directory, tmp_path, capsys
):
    argv = arguments(tmp_path)
    original = (tmp_path / "journal/journal.json").read_bytes()
    assert cli.main(argv) == 2
    record = json.loads(diagnostic(broker_directory).read_bytes())
    assert record["phase"] == "operator" and record["exception_class"] == "JSONDecodeError"
    assert record["source_frame"]["function"] == "run_managed_worker"
    assert (tmp_path / "journal/journal.json").read_bytes() == original
    assert SECRET not in capsys.readouterr().out
    assert SECRET not in json.dumps(record)


def test_direct_setup_failure_keeps_propagation_contract(broker_directory, tmp_path):
    parser = argparse.ArgumentParser()
    cli._runtime_parser(parser)
    args = parser.parse_args(arguments(tmp_path)[1:])
    with pytest.raises(json.JSONDecodeError):
        cli.run_managed_worker(args)
    assert json.loads(diagnostic(broker_directory).read_bytes())["phase"] == "operator"


def test_full_main_exit2_survives_diagnostic_io_failure(
    broker_directory, tmp_path, monkeypatch, capsys
):
    def fail(_fd):
        raise OSError(SECRET)
    monkeypatch.setattr(cli.os, "fsync", fail)
    assert cli.main(arguments(tmp_path)) == 2
    assert not diagnostic(broker_directory).exists()
    assert SECRET not in capsys.readouterr().out


@pytest.fixture
def enrolled(tmp_path):
    namespace = runpy.run_path(str(ROOT / "tests/remote/test_benchmark_worker.py"))
    generator = namespace["session"].__wrapped__(tmp_path)
    fixture = next(generator)
    try:
        next(generator)
    except StopIteration:
        pass
    return fixture


@pytest.mark.parametrize("expired", [False, True])
def test_real_benchmark_initialization_preserves_auth_exit_and_cleanup(
    broker_directory, tmp_path, monkeypatch, enrolled, expired
):
    from aws_mesouq.connector import api as api_module, transport as transport_module
    operator = tmp_path / "operator.json"
    operator.write_text(json.dumps(enrolled.operator))
    journal = tmp_path / "journal/journal.json"
    before = journal.read_bytes()
    calls = []
    class DeniedAPI:
        base = "https://api.example.invalid"
        def request(self, method, path, payload=None):
            calls.append((method, path))
            if expired:
                raise SessionExpired(SECRET)
            raise APIError(403, SECRET, SECRET)
    monkeypatch.setattr(api_module, "API", lambda *_args: DeniedAPI())
    monkeypatch.setattr(transport_module, "Transport", lambda **_kwargs: enrolled.cluster)
    argv = ["managed-worker", "--api-url", "https://api.example.invalid", "--project", "p",
            "--operator-config", str(operator), "--journal", str(tmp_path / "journal"),
            "--local-cluster", "--once"]
    result = cli.main(argv)
    observed = json.loads(diagnostic(broker_directory).read_bytes()) if diagnostic(broker_directory).exists() else {}
    safe_failure = (observed.get("phase"), observed.get("exception_class"), observed.get("source_frame"))
    assert result == (3 if expired else 2), safe_failure
    assert calls == [("GET", "/v1/me")], safe_failure
    assert enrolled.cluster._closed is True
    assert journal.read_bytes() == before
    if expired:
        assert not diagnostic(broker_directory).exists()
    else:
        record = json.loads(diagnostic(broker_directory).read_bytes())
        assert record["phase"] == "legacy_worker"
        assert record["exception_class"] == "APIError" and record["http_status"] == 403
        assert record["source_frame"]["path"] == "aws_mesouq/benchmark_worker.py"
        assert record["source_frame"]["function"] == "__init__"
        assert SECRET not in json.dumps(record)


def test_polling_failure_keeps_exit2_without_startup_record(
    broker_directory, tmp_path, monkeypatch, enrolled
):
    from aws_mesouq import benchmark_worker
    from aws_mesouq.connector import transport as transport_module
    operator = tmp_path / "operator.json"
    operator.write_text(json.dumps(enrolled.operator))
    class PollingFailure:
        def sweep(self):
            raise RuntimeError(SECRET)
    monkeypatch.setattr(benchmark_worker, "BenchmarkWorker", lambda *_a, **_k: PollingFailure())
    monkeypatch.setattr(transport_module, "Transport", lambda **_kwargs: enrolled.cluster)
    argv = ["managed-worker", "--api-url", "https://api.example.invalid", "--project", "p",
            "--operator-config", str(operator), "--journal", str(tmp_path / "journal"),
            "--local-cluster", "--once"]
    assert cli.main(argv) == 2
    assert not diagnostic(broker_directory).exists()
    assert enrolled.cluster._closed is True


def test_deep_traceback_omits_frame_instead_of_scanning_without_bound(broker_directory):
    def nested(depth):
        if depth:
            return nested(depth - 1)
        return cli._json("{" + SECRET)
    try:
        nested(200)
    except Exception as error:
        cli._record_startup_failure("operator", error)
    record = json.loads(diagnostic(broker_directory).read_bytes())
    assert record["source_frame"] is None
    assert SECRET not in json.dumps(record)


def test_failed_fchmod_removes_only_its_owned_empty_record(broker_directory, monkeypatch):
    def fail(_fd, _mode):
        raise OSError(SECRET)
    monkeypatch.setattr(cli.os, "fchmod", fail)
    cli._record_startup_failure("operator", ValueError(SECRET))
    assert not diagnostic(broker_directory).exists()
