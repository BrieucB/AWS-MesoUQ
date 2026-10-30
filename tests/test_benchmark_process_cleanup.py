"""Owned process-group cleanup on Linux/Darwin; synthetic subprocesses only."""
import json
import os
from pathlib import Path
import select
import signal
import subprocess
import sys
from types import SimpleNamespace

import pytest

from aws_mesouq.adapters import benchmark as adapter


@pytest.fixture
def cleanup(monkeypatch):
    clock = SimpleNamespace(now=0.)
    events = []
    process = SimpleNamespace(pid=4242, returncode=None)
    def wait(timeout=None):
        events.append("reap")
        process.returncode = -signal.SIGTERM
        return process.returncode
    process.wait = wait
    def send(pgid, signum):
        assert pgid == process.pid
        events.append(signum)
        if signum == signal.SIGKILL and state.eperm:
            raise PermissionError(state.permission_errno, "synthetic denied group")
        if signum == 0 and state.absent:
            raise ProcessLookupError()
    def probe(pid, flags):
        assert pid == process.pid and flags == os.WNOHANG
        events.append("ownership")
        return 0, 0
    def ps(command, **kwargs):
        state.ps_calls.append((command, kwargs))
        timeout = kwargs["timeout"]
        if state.ps_elapsed > timeout:
            clock.now += timeout
            raise subprocess.TimeoutExpired(command, timeout)
        clock.now += state.ps_elapsed
        value = state.outputs.pop(0) if len(state.outputs) > 1 else state.outputs[0]
        if isinstance(value, BaseException):
            raise value
        return value
    state = SimpleNamespace(clock=clock, events=events, process=process, eperm=True,
                            absent=False, permission_errno=1, ps_calls=[], ps_elapsed=.01,
                            outputs=[SimpleNamespace(returncode=0, stdout=b"4242 4242 Z 1000\n", stderr=b"")])
    monkeypatch.setattr(adapter.time, "monotonic", lambda: clock.now)
    monkeypatch.setattr(adapter.time, "sleep", lambda seconds: setattr(clock, "now", clock.now + seconds))
    monkeypatch.setattr(adapter.os, "waitpid", probe)
    monkeypatch.setattr(adapter.os, "getpgid", lambda pid: pid)
    monkeypatch.setattr(adapter.os, "getsid", lambda pid: pid)
    monkeypatch.setattr(adapter.os, "geteuid", lambda: 1000)
    monkeypatch.setattr(adapter.os, "killpg", send)
    monkeypatch.setattr(adapter.signal, "getsignal", lambda signum: signal.SIG_DFL)
    monkeypatch.setattr(adapter, "sys", SimpleNamespace(platform="darwin"), raising=False)
    monkeypatch.setattr(adapter, "_capture_group_metadata", ps, raising=False)
    return state


def test_zombie_only_eperm_is_a_completed_cleanup(cleanup):
    # Old 599e1bb raises the historical PermissionError after prematurely reaping.
    adapter._stop_group(cleanup.process)
    assert cleanup.events[0] == "ownership"
    assert cleanup.events[-1] == "reap"
    assert signal.SIGKILL in cleanup.events
    assert cleanup.ps_calls


def test_live_permission_denied_is_not_suppressed(cleanup):
    cleanup.outputs = [SimpleNamespace(returncode=0, stdout=b"4242 4242 S 1000\n", stderr=b"")]
    with pytest.raises(PermissionError):
        adapter._stop_group(cleanup.process)
    assert "reap" not in cleanup.events


@pytest.mark.parametrize("output", [
    b"bad metadata\n", b"4242 9999 Z 1000\n", b"4242 4242 Q 1000\n",
    b"4242 4242 Z? 1000\n", b"4242 4242 Z 2000\n",
    b"4242 4242 Z 1000\n4242 4242 Z 1000\n",
    b"x" * 65537,
    b"4242 4242 Z 1000\n" * 1025,
])
def test_unknown_foreign_or_unbounded_group_proof_fails_closed(cleanup, output):
    cleanup.outputs = [SimpleNamespace(returncode=0, stdout=output, stderr=b"")]
    with pytest.raises(RuntimeError):
        adapter._stop_group(cleanup.process)
    assert "reap" not in cleanup.events


@pytest.mark.parametrize("failure", [
    SimpleNamespace(returncode=1, stdout=b"", stderr=b"permission denied"),
    subprocess.TimeoutExpired("ps", .25),
    OSError("ps unavailable"),
])
def test_unavailable_or_denied_group_proof_fails_closed(cleanup, failure):
    cleanup.outputs = [failure]
    with pytest.raises(RuntimeError):
        adapter._stop_group(cleanup.process)


def test_empty_ps_requires_independent_no_group_proof(cleanup):
    cleanup.outputs = [SimpleNamespace(returncode=1, stdout=b"", stderr=b"")]
    with pytest.raises(RuntimeError):
        adapter._stop_group(cleanup.process)
    assert 0 in cleanup.events and "reap" not in cleanup.events


def test_empty_ps_and_esrch_prove_absent_group(cleanup):
    cleanup.outputs = [SimpleNamespace(returncode=1, stdout=b"", stderr=b"")]
    cleanup.absent = True
    adapter._stop_group(cleanup.process)
    assert cleanup.events[-1] == "reap"


def test_no_reap_occurs_between_owned_probe_and_final_group_signal(cleanup):
    original = cleanup.process.wait
    def wait(timeout=None):
        assert signal.SIGKILL in cleanup.events
        assert cleanup.events.count("ownership") == 1
        return original(timeout)
    cleanup.process.wait = wait
    adapter._stop_group(cleanup.process)
    assert cleanup.events.index("ownership") < cleanup.events.index(signal.SIGTERM)
    assert cleanup.events.index(signal.SIGKILL) < cleanup.events.index("reap")


@pytest.mark.parametrize("kind", ["reaped", "exited", "not-child", "changed-group", "changed-session"])
def test_unowned_or_recycled_group_is_never_signalled(cleanup, monkeypatch, kind):
    if kind == "reaped":
        cleanup.process.returncode = 0
    elif kind == "exited":
        monkeypatch.setattr(adapter.os, "waitpid", lambda *args: (cleanup.process.pid, 0))
    elif kind == "not-child":
        def not_child(*args):
            raise ChildProcessError()
        monkeypatch.setattr(adapter.os, "waitpid", not_child)
    elif kind == "changed-group":
        monkeypatch.setattr(adapter.os, "getpgid", lambda pid: pid + 1)
    else:
        monkeypatch.setattr(adapter.os, "getsid", lambda pid: pid + 1)
    with pytest.raises(RuntimeError):
        adapter._stop_group(cleanup.process)
    assert not any(isinstance(value, int) for value in cleanup.events)


@pytest.mark.parametrize("capability", ["SIGCHLD", "waitpid"])
def test_unsupported_ownership_supervision_fails_before_launch(cleanup, monkeypatch, tmp_path, capability):
    if capability == "SIGCHLD":
        monkeypatch.setattr(adapter.signal, "getsignal", lambda signum: signal.SIG_IGN)
    else:
        monkeypatch.setattr(adapter.os, "waitpid", None)
    launches = []
    monkeypatch.setattr(adapter.subprocess, "Popen", lambda *args, **kwargs: launches.append(args))
    with pytest.raises(RuntimeError):
        adapter.run_science(["never"], tmp_path, adapter._Deadline(100000000000.))
    assert not launches


def test_successful_kill_waits_for_live_to_zombie_transition(cleanup):
    cleanup.eperm = False
    cleanup.outputs = [
        SimpleNamespace(returncode=0, stdout=b"4242 4242 R 1000\n", stderr=b""),
        SimpleNamespace(returncode=0, stdout=b"4242 4242 Z 1000\n", stderr=b""),
    ]
    adapter._stop_group(cleanup.process)
    assert len(cleanup.ps_calls) == 2
    assert cleanup.events[-1] == "reap"


def test_cleanup_has_one_four_second_budget_including_metadata(cleanup):
    cleanup.eperm = False
    cleanup.ps_elapsed = .2
    cleanup.outputs = [SimpleNamespace(returncode=0, stdout=b"4242 4242 D 1000\n", stderr=b"")]
    with pytest.raises(RuntimeError):
        adapter._stop_group(cleanup.process)
    assert cleanup.clock.now <= 4.01
    assert all(0 < kwargs["timeout"] <= .25 for _, kwargs in cleanup.ps_calls)
    assert "reap" not in cleanup.events


@pytest.mark.parametrize("platform,selector", [("linux", "-4242"), ("darwin", "-g")])
def test_exact_platform_group_selector_never_requests_commands(cleanup, monkeypatch, platform, selector):
    cleanup.eperm = False
    monkeypatch.setattr(adapter, "sys", SimpleNamespace(platform=platform), raising=False)
    adapter._stop_group(cleanup.process)
    command, kwargs = cleanup.ps_calls[0]
    assert selector in command
    assert all("command" not in value and "args" not in value for value in command)
    assert kwargs["env"]["LC_ALL"] == "C"


@pytest.fixture
def real_group(tmp_path):
    # Two pipes provide failure cleanup and an unrecycled liveness observation.
    guard_read, guard_write = os.pipe()
    alive_read, alive_write = os.pipe()
    ready = tmp_path / "ready.json"
    child = (
        "import os,signal,pathlib,json;"
        "signal.signal(signal.SIGTERM,signal.SIG_IGN);"
        f"p=pathlib.Path({str(ready)!r});t=p.with_suffix('.tmp');"
        "t.write_text(json.dumps({'pid':os.getpid(),'group':os.getpgrp()}));"
        "os.replace(t,p);"
        f"os.read({guard_read},1)"
    )
    leader = (
        "import subprocess,sys,os,time;"
        f"subprocess.Popen([sys.executable,'-c',{child!r}],pass_fds=({guard_read},{alive_write}));"
        f"os.close({guard_read});os.close({alive_write});time.sleep(30)"
    )
    process = subprocess.Popen([sys.executable, "-c", leader], start_new_session=True,
                               pass_fds=(guard_read, alive_write))
    os.close(guard_read)
    os.close(alive_write)
    try:
        until = adapter.time.monotonic() + 5
        while not ready.exists() and adapter.time.monotonic() < until:
            adapter.time.sleep(.01)
        assert ready.exists(), "synthetic child did not complete its atomic ready handshake"
        identity = json.loads(ready.read_text())
        assert identity["group"] == process.pid
        yield SimpleNamespace(process=process, identity=identity, alive_read=alive_read)
    finally:
        # Closing the guard lets an orphaned fixture exit without signalling a
        # potentially recycled descendant PID or process-group number.
        os.close(guard_write)
        if process.returncode is None:
            process.kill()
            process.wait(timeout=2)
        os.close(alive_read)

@pytest.mark.parametrize("platform,error_number", [
    ("linux", 1), ("darwin", 13), ("darwin", None),
])
def test_only_darwin_eperm_can_use_quiescent_proof(cleanup, monkeypatch, platform, error_number):
    monkeypatch.setattr(adapter, "sys", SimpleNamespace(platform=platform), raising=False)
    cleanup.permission_errno = error_number
    with pytest.raises(PermissionError):
        adapter._stop_group(cleanup.process)
    assert not cleanup.ps_calls


@pytest.mark.parametrize("kind", ["overflow", "stderr", "timeout"])
def test_metadata_reader_is_hard_bounded_and_reaps_its_child(tmp_path, monkeypatch, kind):
    real_popen = subprocess.Popen
    real_read = os.read
    children = []
    observed = {"stdout_bytes": 0}
    descriptors = set()
    def popen(*args, **kwargs):
        process = real_popen(*args, **kwargs)
        children.append(process)
        descriptors.add(process.stdout.fileno())
        return process
    def read(fd, size):
        block = real_read(fd, size)
        if fd in descriptors:
            observed["stdout_bytes"] += len(block)
        return block
    monkeypatch.setattr(adapter.subprocess, "Popen", popen)
    monkeypatch.setattr(adapter.os, "read", read)
    code = {
        "overflow": "import os,time;os.write(1,b'x'*100000);time.sleep(30)",
        "stderr": "import os,time;os.write(2,b'!');time.sleep(30)",
        "timeout": "import time;time.sleep(30)",
    }[kind]
    started = adapter.time.monotonic()
    with pytest.raises((RuntimeError, subprocess.TimeoutExpired)):
        adapter._capture_group_metadata([sys.executable, "-c", code], timeout=.25,
                                        env=dict(os.environ))
    assert adapter.time.monotonic() - started < .5
    assert len(children) == 1 and children[0].returncode is not None
    assert observed["stdout_bytes"] <= 65537
    if kind == "overflow":
        assert observed["stdout_bytes"] == 65537


@pytest.mark.parametrize("repeat", range(20))
def test_term_resistant_descendant_dies_even_after_leader_exits(real_group, repeat):
    started = adapter.time.monotonic()
    adapter._stop_group(real_group.process)
    assert adapter.time.monotonic() - started < 4.2
    assert real_group.process.returncode == -signal.SIGTERM
    readable, _, _ = select.select([real_group.alive_read], [], [], .5)
    assert readable and os.read(real_group.alive_read, 1) == b""

def test_darwin_metadata_overrides_poisoned_command_mode(cleanup, monkeypatch):
    monkeypatch.setenv("COMMAND_MODE", "legacy")
    adapter._stop_group(cleanup.process)
    assert cleanup.ps_calls[0][1]["env"]["COMMAND_MODE"] == "unix2003"
    assert os.environ["COMMAND_MODE"] == "legacy"


@pytest.mark.skipif(sys.platform != "darwin", reason="Actual Apple ps compatibility-mode regression")
def test_actual_darwin_selector_sees_descendant_despite_legacy_parent_mode(real_group, monkeypatch):
    monkeypatch.setenv("COMMAND_MODE", "legacy")
    capture = adapter._capture_group_metadata
    observed = []
    def record(command, **kwargs):
        assert kwargs["env"]["COMMAND_MODE"] == "unix2003"
        result = capture(command, **kwargs)
        observed.extend(result.stdout.decode("ascii").splitlines())
        return result
    monkeypatch.setattr(adapter, "_capture_group_metadata", record)
    assert adapter._group_quiescent(real_group.process.pid, adapter.time.monotonic() + 2) is False
    members = {int(row.split()[0]): row.split() for row in observed}
    child = members[real_group.identity["pid"]]
    assert int(child[1]) == real_group.process.pid and not child[2].startswith("Z")
    assert int(child[3]) == os.geteuid()
    adapter._stop_group(real_group.process)
    readable, _, _ = select.select([real_group.alive_read], [], [], .5)
    assert readable and os.read(real_group.alive_read, 1) == b""

def test_old_procps_selector_comes_after_all_format_options(cleanup, monkeypatch):
    monkeypatch.setattr(adapter, "sys", SimpleNamespace(platform="linux"), raising=False)
    cleanup.eperm = False
    metadata = adapter._capture_group_metadata
    def old_procps(command, **kwargs):
        # procps3.3.15 parse_trailing_pids treats ALL remaining tokens as IDs.
        numeric = command.index("-4242")
        if numeric != len(command) - 1:
            return SimpleNamespace(returncode=1, stdout=b"",
                                   stderr=b"error: process ID list syntax error\n")
        return metadata(command, **kwargs)
    monkeypatch.setattr(adapter, "_capture_group_metadata", old_procps)
    adapter._stop_group(cleanup.process)
    assert cleanup.events[-1] == "reap"


def test_linux_group_proof_rejects_foreign_pgid_instead_of_filtering_it(cleanup, monkeypatch):
    monkeypatch.setattr(adapter, "sys", SimpleNamespace(platform="linux"), raising=False)
    cleanup.eperm = False
    cleanup.outputs = [SimpleNamespace(returncode=0, stdout=b"4242 4242 Z 1000\n4243 5000 R 1000\n", stderr=b"")]
    with pytest.raises(RuntimeError):
        adapter._stop_group(cleanup.process)
    assert "reap" not in cleanup.events
