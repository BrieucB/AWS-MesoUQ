"""Allocation time must include startup and terminate the whole science process tree."""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import time
from types import SimpleNamespace

import pytest

from aws_mesouq.adapters import benchmark as adapter


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True, allow_nan=False).encode()


@pytest.fixture
def prepared(tmp_path, monkeypatch):
    repository = tmp_path / "repository"
    repository.mkdir()
    environment = tmp_path / "environment.sh"
    environment.write_text("# approved fixture\n")
    request = tmp_path / "request.json"
    request.write_bytes(canonical({"fixture": "independent request"}))
    manifest = {
        "repository": str(repository),
        "science_revision": "a" * 40,
        "python_bin": sys.executable,
        "environment_script": str(environment),
        "environment_sha256": hashlib.sha256(environment.read_bytes()).hexdigest(),
        "adapter_sha256": hashlib.sha256(Path(adapter.__file__).read_bytes()).hexdigest(),
        "request": str(request),
        "campaign_root": str(tmp_path / "campaign"),
        "campaign_instance_id": "b" * 32,
        "configuration_hash": hashlib.sha256(request.read_bytes()).hexdigest(),
        "stage": "train_acoustic_1",
        "internal_seconds": 1770,
        "device": "cuda",
        "runtime_clock_policy": "allocation_deadline_v1",
        "allocated_seconds": 1800,
    }
    path = tmp_path / "manifest.json"
    path.write_bytes(canonical(manifest))
    for key, value in {
        "SLURM_JOB_ID": "77",
        "SLURM_JOB_START_TIME": "1000",
        "SLURM_JOB_END_TIME": "2800",
        "AWS_MESOUQ_ALLOCATION_JOB_ID": "77",
        "AWS_MESOUQ_ALLOCATION_START_EPOCH": "1000",
        "AWS_MESOUQ_ALLOCATION_DEADLINE_EPOCH": "2800",
    }.items():
        monkeypatch.setenv(key, value)
    monkeypatch.setattr(adapter.time if hasattr(adapter, "time") else time, "time", lambda: 1060.0)
    calls = []
    def check_output(command, **kwargs):
        return "a" * 40 + "\n" if command[-2:] == ["rev-parse", "HEAD"] else ""
    monkeypatch.setattr(adapter.subprocess, "check_output", check_output)
    def legacy_run(command, **kwargs):
        calls.append((command, kwargs))
        return SimpleNamespace(returncode=0)
    monkeypatch.setattr(adapter.subprocess, "run", legacy_run)
    class Process:
        pid = 999999999
        returncode = 0
        def wait(self, timeout=None):
            return 0
    def popen(command, **kwargs):
        calls.append((command, kwargs))
        return Process()
    monkeypatch.setattr(adapter.subprocess, "Popen", popen)
    return path, manifest, calls


def test_sixty_seconds_startup_cannot_move_scientific_deadline(prepared):
    path, manifest, calls = prepared
    assert adapter.execute(path) == 0
    command = calls[-1][0]
    assert command[command.index("--allocation-start-epoch") + 1] == "1000"
    assert command[command.index("--allocation-deadline-epoch") + 1] == "2800"
    assert command[command.index("--allocation-job-id") + 1] == "77"
    assert command[command.index("--reserved-seconds") + 1] == "1770"
    assert calls[-1][1]["start_new_session"] is True
    record = json.loads(path.with_name("allocation-clock.json").read_text())
    assert record["allocation_deadline_epoch"] == 2800
    assert record["watchdog_deadline_epoch"] == 2795
    assert record["manifest_sha256"] == hashlib.sha256(path.read_bytes()).hexdigest()


def test_expired_allocation_never_starts_a_scientific_child(prepared, monkeypatch):
    path, _, calls = prepared
    monkeypatch.setattr(adapter.time if hasattr(adapter, "time") else time, "time", lambda: 2801.0)
    with pytest.raises(RuntimeError, match="deadline|allocation"):
        adapter.execute(path)
    assert calls == []


def test_captured_job_cannot_be_replaced_after_environment_loading(prepared, monkeypatch):
    path, _, calls = prepared
    monkeypatch.setenv("SLURM_JOB_ID", "78")
    with pytest.raises(RuntimeError, match="allocation|identity"):
        adapter.execute(path)
    assert calls == []


def test_projected_end_never_expands_the_funded_allocation(prepared, monkeypatch):
    path, manifest, calls = prepared
    manifest["allocated_seconds"] = 600
    manifest["internal_seconds"] = 570
    path.write_bytes(canonical(manifest))
    monkeypatch.setenv("SLURM_JOB_END_TIME", "999999")
    monkeypatch.setenv("AWS_MESOUQ_ALLOCATION_DEADLINE_EPOCH", "999999")
    assert adapter.execute(path) == 0
    command = calls[-1][0]
    assert command[command.index("--allocation-deadline-epoch") + 1] == "1600"


@pytest.mark.parametrize("key,value", [
    ("AWS_MESOUQ_ALLOCATION_START_EPOCH", "nan"),
    ("AWS_MESOUQ_ALLOCATION_START_EPOCH", "1000.0"),
    ("AWS_MESOUQ_ALLOCATION_DEADLINE_EPOCH", "999"),
    ("AWS_MESOUQ_ALLOCATION_JOB_ID", ""),
])
def test_invalid_native_clock_fails_before_science(prepared, monkeypatch, key, value):
    path, _, calls = prepared
    monkeypatch.setenv(key, value)
    with pytest.raises((RuntimeError, ValueError), match="allocation|clock|identity"):
        adapter.execute(path)
    assert calls == []


def test_immutable_clock_record_detects_changed_allocation(prepared, monkeypatch):
    path, _, _ = prepared
    assert adapter.execute(path) == 0
    monkeypatch.setenv("SLURM_JOB_END_TIME", "2700")
    monkeypatch.setenv("AWS_MESOUQ_ALLOCATION_DEADLINE_EPOCH", "2700")
    with pytest.raises(ValueError, match="clock|Immutable"):
        adapter.execute(path)


def test_deadline_kills_scientific_process_group(tmp_path):
    if not hasattr(adapter, "run_science"):
        pytest.skip("process supervisor introduced with allocation clock")
    pid_path = tmp_path / "descendant.pid"
    marker = tmp_path / "descendant-survived"
    child_code = (
        "import time,pathlib;time.sleep(5);"
        f"pathlib.Path({str(marker)!r}).write_text('orphaned')"
    )
    parent_code = (
        "import subprocess,sys,time,pathlib;"
        f"p=subprocess.Popen([sys.executable,'-c',{child_code!r}]);"
        f"pathlib.Path({str(pid_path)!r}).write_text(str(p.pid));"
        "time.sleep(30)"
    )
    started = time.monotonic()
    with pytest.raises(adapter.AllocationDeadlineExceeded):
        adapter.run_science([sys.executable, "-c", parent_code], tmp_path, time.time() + 2)
    assert time.monotonic() - started < 6
    assert pid_path.exists()
    pid = int(pid_path.read_text())
    # A killed but not-yet-reaped grandchild may briefly remain a zombie.
    try:
        process_state = subprocess.check_output(["ps", "-o", "stat=", "-p", str(pid)], text=True).strip()
    except subprocess.CalledProcessError:
        process_state = ""
    assert not process_state or process_state.startswith("Z")
    assert not marker.exists()


def test_watchdog_does_not_extend_after_wall_clock_jump(monkeypatch):
    if not hasattr(adapter, "_Deadline"):
        pytest.skip("allocation-wide supervisor introduced with deadline policy")
    epoch = [1000.0]
    monotonic = [10.0]
    monkeypatch.setattr(adapter.time, "time", lambda: epoch[0])
    monkeypatch.setattr(adapter.time, "monotonic", lambda: monotonic[0])
    deadline = adapter._Deadline(1010.0)
    assert deadline.remaining() == 10.0
    epoch[0] -= 3600
    monotonic[0] += 7
    assert deadline.remaining() == 3.0


def test_expired_supervisor_never_launches_child(tmp_path, monkeypatch):
    if not hasattr(adapter, "run_science"):
        pytest.skip("allocation-wide supervisor introduced with deadline policy")
    calls = []
    monkeypatch.setattr(adapter.subprocess, "Popen", lambda *a, **k: calls.append(a))
    with pytest.raises(adapter.AllocationDeadlineExceeded):
        adapter.run_science(["unused"], tmp_path, time.time() - 1)
    assert calls == []


def test_exact_legacy_adapter_remains_bundled():
    from aws_mesouq.adapters import adapter_snapshot, LEGACY_BENCHMARK_ADAPTER_SHA256
    blob = adapter_snapshot(LEGACY_BENCHMARK_ADAPTER_SHA256)
    assert hashlib.sha256(blob).hexdigest() == "4740f1d4af27e754ba3d4fe4f80d5309e9e2ce1bc9bd70945ef10ae06bcf75e9"
    assert b"--allocation-deadline-epoch" not in blob
    current = hashlib.sha256(Path(adapter.__file__).read_bytes()).hexdigest()
    assert adapter_snapshot(current) == Path(adapter.__file__).read_bytes()
    with pytest.raises(ValueError, match="unsupported"):
        adapter_snapshot("0" * 64)


def test_supervisor_sigterm_stops_its_separate_scientific_group(tmp_path):
    if not hasattr(adapter, "run_science"):
        pytest.skip("allocation supervisor introduced with clock policy")
    pid_path = tmp_path / "scientific-processes.json"
    child_code = "import time;time.sleep(30)"
    science_code = (
        "import subprocess,sys,time,pathlib,json,os;"
        f"p=subprocess.Popen([sys.executable,'-c',{child_code!r}]);"
        f"pathlib.Path({str(pid_path)!r}).write_text(json.dumps({{'science':os.getpid(),'descendant':p.pid}}));"
        "time.sleep(30)"
    )
    supervisor_code = (
        "import sys,time;from aws_mesouq.adapters import benchmark as a;"
        f"command=[sys.executable,'-c',{science_code!r}];"
        "\ntry:\n"
        f" a.run_science(command,{str(tmp_path)!r},time.time()+20)\n"
        "except BaseException as error:\n"
        " raise SystemExit(128+getattr(error,'signum',0))\n"
    )
    supervisor = subprocess.Popen([sys.executable, "-c", supervisor_code], start_new_session=True)
    pids = {}
    states = []
    try:
        until = time.monotonic() + 5
        while not pid_path.exists() and time.monotonic() < until:
            if supervisor.poll() is not None:
                break
            time.sleep(0.02)
        assert pid_path.exists(), "scientific subprocess did not start"
        pids = json.loads(pid_path.read_text())
        os.killpg(supervisor.pid, signal.SIGTERM)
        returncode = supervisor.wait(timeout=5)
        for pid in pids.values():
            try:
                state = subprocess.check_output(["ps", "-o", "stat=", "-p", str(pid)], text=True).strip()
            except subprocess.CalledProcessError:
                state = ""
            states.append(state)
        assert returncode == 128 + signal.SIGTERM
        assert all(not value or value.startswith("Z") for value in states)
    finally:
        # The old-source regression intentionally leaves descendants alive.
        if pids:
            try:
                os.killpg(pids["science"], signal.SIGKILL)
            except ProcessLookupError:
                pass
        if supervisor.poll() is None:
            try:
                os.killpg(supervisor.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            supervisor.wait(timeout=2)
