"""Trusted adapter with an allocation-wide native deadline and bounded child cleanup."""
from __future__ import annotations

import argparse
import errno
import hashlib
import json
import math
import os
from pathlib import Path
import re
import selectors
import signal
import stat
import subprocess
import sys
import threading
import time

CLOCK_POLICY = "allocation_deadline_v1"
WATCHDOG_MARGIN_SECONDS = 5


class AllocationDeadlineExceeded(RuntimeError):
    """The funded native allocation cannot support more scientific work."""


def digest(path):
    value = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1048576), b""):
            value.update(block)
    return value.hexdigest()


def _native_integer(name):
    value = os.environ.get(name, "")
    if not re.fullmatch(r"[1-9][0-9]{0,15}", value):
        raise RuntimeError("Invalid or missing native allocation clock: " + name)
    return int(value)


def allocation_clock(manifest):
    if manifest.get("runtime_clock_policy") != CLOCK_POLICY:
        raise ValueError("An explicit supported allocation clock policy is required")
    allocated = manifest.get("allocated_seconds")
    internal = manifest.get("internal_seconds")
    if (type(allocated) is not int or not 60 <= allocated <= 172800 or allocated % 60
            or type(internal) not in (int, float) or not math.isfinite(internal)
            or not 0 < internal <= allocated):
        raise ValueError("Invalid funded native allocation clock window")
    job = _native_integer("AWS_MESOUQ_ALLOCATION_JOB_ID")
    start = _native_integer("AWS_MESOUQ_ALLOCATION_START_EPOCH")
    projected_end = _native_integer("AWS_MESOUQ_ALLOCATION_DEADLINE_EPOCH")
    if (str(job) != os.environ.get("SLURM_JOB_ID")
            or str(start) != os.environ.get("SLURM_JOB_START_TIME")
            or str(projected_end) != os.environ.get("SLURM_JOB_END_TIME")):
        raise RuntimeError("Native allocation identity changed after environment loading")
    now = time.time()
    if not math.isfinite(now) or projected_end <= start or start > now + 5:
        raise RuntimeError("Invalid native allocation clock bounds")
    end = min(projected_end, start + allocated)
    if end - WATCHDOG_MARGIN_SECONDS <= now:
        raise AllocationDeadlineExceeded("Native allocation deadline is already exhausted")
    return {
        "schema_version": 1,
        "policy": CLOCK_POLICY,
        "slurm_job_id": str(job),
        "allocation_start_epoch": start,
        "projected_end_epoch": projected_end,
        "allocated_seconds": allocated,
        "allocation_deadline_epoch": end,
        "watchdog_deadline_epoch": end - WATCHDOG_MARGIN_SECONDS,
    }


class _Deadline:
    def __init__(self, epoch):
        self.monotonic_end = time.monotonic() + (epoch - time.time())

    def remaining(self):
        remaining = self.monotonic_end - time.monotonic()
        if remaining <= 0:
            raise AllocationDeadlineExceeded("Native allocation deadline is exhausted")
        return remaining


def _remaining(deadline):
    return deadline.remaining()


def _clock_record(manifest_path, clock):
    record = {**clock, "manifest_sha256": digest(manifest_path)}
    blob = json.dumps(record, sort_keys=True, separators=(",", ":"), allow_nan=False).encode() + b"\n"
    path = manifest_path.with_name("allocation-clock.json")
    try:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    except FileExistsError:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        with os.fdopen(fd, "rb") as stream:
            info = os.fstat(stream.fileno())
            if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid()
                    or stat.S_IMODE(info.st_mode) != 0o600 or info.st_nlink != 1
                    or stream.read() != blob):
                raise ValueError("Immutable native allocation clock differs")
    else:
        with os.fdopen(fd, "wb") as stream:
            stream.write(blob)
            stream.flush()
            os.fsync(stream.fileno())
        parent = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(parent)
        finally:
            os.close(parent)


CLEANUP_SECONDS = 4.
TERM_GRACE_SECONDS = 1.
GROUP_PROOF_SECONDS = .25
GROUP_PROOF_BYTES = 65536
GROUP_PROOF_ROWS = 1024


class ProcessGroupCleanupError(RuntimeError):
    """An owned group could not be safely proved stopped within its deadline."""


def _require_process_supervision():
    # Explicit SIG_IGN/custom reapers can release a PID behind this supervisor.
    required = ("waitpid", "waitstatus_to_exitcode", "killpg", "getpgid", "getsid")
    if (sys.platform not in ("linux", "darwin") or
            any(not callable(getattr(os, name, None)) for name in required) or
            not getattr(os, "WNOHANG", 0) or
            threading.current_thread() is not threading.main_thread() or
            signal.getsignal(signal.SIGCHLD) != signal.SIG_DFL):
        raise ProcessGroupCleanupError("Owned process supervision requires Linux/macOS, waitpid and default SIGCHLD")


def _cleanup_remaining(deadline):
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise ProcessGroupCleanupError("Scientific process-group cleanup exceeded its four-second limit")
    return remaining


def _owned_live_group(process):
    _require_process_supervision()
    pid = process.pid
    if type(pid) is not int or pid <= 1 or process.returncode is not None:
        raise ProcessGroupCleanupError("Cannot signal a process group after its owned leader was reaped")
    try:
        found, status = os.waitpid(pid, os.WNOHANG)
        if found == pid:
            process.returncode = os.waitstatus_to_exitcode(status)
            raise ProcessGroupCleanupError("Owned leader already exited; group identity is ambiguous")
        if (found, status) != (0, 0) or os.getpgid(pid) != pid or os.getsid(pid) != pid:
            raise ProcessGroupCleanupError("Owned child does not identify its private process group and session")
    except OSError as error:
        raise ProcessGroupCleanupError("Cannot prove live child ownership before a group signal") from error


def _capture_group_metadata(command, *, timeout, env):
    """Read at most 64 KiB plus one overflow byte; bound and reap the ps child."""
    deadline = time.monotonic() + timeout
    read_deadline = deadline - min(.05, timeout / 3)
    process = None
    selected = None
    output = bytearray()
    try:
        process = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                   env=env, start_new_session=True)
        selected = selectors.DefaultSelector()
        selected.register(process.stdout, selectors.EVENT_READ, "output")
        selected.register(process.stderr, selectors.EVENT_READ, "error")
        while selected.get_map():
            remaining = read_deadline - time.monotonic()
            if remaining <= 0:
                raise subprocess.TimeoutExpired(command, timeout)
            events = selected.select(remaining)
            if not events:
                raise subprocess.TimeoutExpired(command, timeout)
            for key, _ in events:
                maximum = min(4096, GROUP_PROOF_BYTES + 1 - len(output)) if key.data == "output" else 1
                block = os.read(key.fileobj.fileno(), maximum)
                if not block:
                    selected.unregister(key.fileobj)
                    continue
                if key.data == "error":
                    raise ProcessGroupCleanupError("Process-group metadata reported an error")
                output.extend(block)
                if len(output) > GROUP_PROOF_BYTES:
                    raise ProcessGroupCleanupError("Process-group metadata exceeded its hard read limit")
        remaining = read_deadline - time.monotonic()
        if remaining <= 0:
            raise subprocess.TimeoutExpired(command, timeout)
        code = process.wait(timeout=remaining)
        return subprocess.CompletedProcess(command, code, bytes(output), b"")
    finally:
        if selected is not None:
            selected.close()
        if process is not None:
            for stream in (process.stdout, process.stderr):
                stream.close()
            if process.returncode is None:
                process.kill()
                try:
                    process.wait(timeout=max(0., deadline - time.monotonic()))
                except subprocess.TimeoutExpired as error:
                    raise ProcessGroupCleanupError("Metadata child did not stop within its bounded query") from error


def _group_quiescent(pgid, deadline):
    remaining = _cleanup_remaining(deadline)
    if remaining <= .1:
        raise ProcessGroupCleanupError("Insufficient cleanup time for bounded group metadata")
    # Apple -g selects PGID; procps -g selects SID, so Linux uses its documented
    # negative numeric PGID selector. Never request command lines or environments.
    select_group = ["-g", str(pgid)] if sys.platform == "darwin" else ["-" + str(pgid)]
    command = ["/bin/ps", *select_group, "-o", "pid=", "-o", "pgid=", "-o", "stat=", "-o", "uid="]
    environment = dict(os.environ)
    for name in ("PS_FORMAT", "PS_PERSONALITY", "CMD_ENV", "POSIXLY_CORRECT", "UNIX95", "I_WANT_A_BROKEN_PS"):
        environment.pop(name, None)
    environment["LC_ALL"] = "C"
    if sys.platform == "linux":
        environment["PS_PERSONALITY"] = "linux"
    else:
        # Apple legacy mode ignores -g and can treat the PGID as a PID list.
        environment["COMMAND_MODE"] = "unix2003"
    try:
        result = _capture_group_metadata(command,
                                         timeout=min(GROUP_PROOF_SECONDS, remaining - .1),
                                         env=environment)
    except (OSError, subprocess.SubprocessError) as error:
        raise ProcessGroupCleanupError("Bounded process-group metadata is unavailable") from error
    _cleanup_remaining(deadline)
    output, errors = result.stdout, result.stderr
    if (not isinstance(output, bytes) or not isinstance(errors, bytes) or errors or
            len(output) > GROUP_PROOF_BYTES or result.returncode not in (0, 1)):
        raise ProcessGroupCleanupError("Unknown, denied or oversized process-group metadata")
    if not output.strip():
        # Empty/nonzero ps output alone is not a trusted absence proof.
        try:
            os.killpg(pgid, 0)
        except ProcessLookupError:
            return True
        except OSError as error:
            raise ProcessGroupCleanupError("Empty group metadata does not prove group absence") from error
        raise ProcessGroupCleanupError("Empty group metadata does not prove group absence")
    if result.returncode != 0:
        raise ProcessGroupCleanupError("Partial process-group metadata cannot prove cleanup")
    try:
        rows = output.decode("ascii").splitlines()
    except UnicodeError as error:
        raise ProcessGroupCleanupError("Invalid process-group metadata encoding") from error
    if not 1 <= len(rows) <= GROUP_PROOF_ROWS:
        raise ProcessGroupCleanupError("Unbounded process-group metadata rows")
    seen, quiescent = set(), True
    for row in rows:
        fields = row.split()
        if len(fields) != 4:
            raise ProcessGroupCleanupError("Unknown process-group metadata fields")
        pid, group, state, uid = fields
        if (not re.fullmatch(r"[1-9][0-9]{0,15}", pid) or
                not re.fullmatch(r"[1-9][0-9]{0,15}", group) or
                not re.fullmatch(r"0|[1-9][0-9]{0,15}", uid) or
                int(group) != pgid or int(uid) != os.geteuid() or pid in seen or
                not re.fullmatch(r"[DIRSTtWXZUI][+<=>AEILNOSTVWlrs]*", state)):
            raise ProcessGroupCleanupError("Unknown or foreign process-group metadata")
        seen.add(pid)
        quiescent = quiescent and state.startswith("Z")
    return quiescent


def _signal_owned_group(process, signum, deadline):
    _cleanup_remaining(deadline)
    if process.returncode is not None:
        raise ProcessGroupCleanupError("Owned leader was reaped before the last group signal")
    try:
        os.killpg(process.pid, signum)
    except ProcessLookupError:
        return False
    except PermissionError as error:
        if sys.platform != "darwin" or error.errno != errno.EPERM:
            raise
        # Darwin can return EPERM for an existing zombie-only group. A live or
        # unknown group must retain its failure; leader exit alone is no proof.
        if not _group_quiescent(process.pid, deadline):
            raise
        return False
    return True


def _stop_group(process):
    deadline = time.monotonic() + CLEANUP_SECONDS
    _owned_live_group(process)
    active = _signal_owned_group(process, signal.SIGTERM, deadline)
    if active:
        # Do not wait/poll here: an unreaped owned leader pins the PID/PGID even
        # if it exits while a TERM-resistant grandchild is still executing.
        time.sleep(min(TERM_GRACE_SECONDS, _cleanup_remaining(deadline)))
        active = _signal_owned_group(process, signal.SIGKILL, deadline)
        while active and not _group_quiescent(process.pid, deadline):
            time.sleep(min(.02, _cleanup_remaining(deadline)))
    # All group signals are finished before releasing the owned PID.
    try:
        process.wait(timeout=_cleanup_remaining(deadline))
    except subprocess.TimeoutExpired as error:
        raise ProcessGroupCleanupError("Owned leader could not be reaped within the cleanup deadline") from error
    _cleanup_remaining(deadline)


class AdapterInterrupted(RuntimeError):
    """A native termination signal must also stop the owned scientific group."""

    def __init__(self, signum):
        super().__init__("Benchmark adapter interrupted by signal " + str(signum))
        self.signum = signum


def run_science(command, repository, watchdog_deadline):
    deadline = watchdog_deadline if isinstance(watchdog_deadline, _Deadline) else _Deadline(watchdog_deadline)
    _remaining(deadline)
    _require_process_supervision()
    process = None
    launching = True
    stopping = False
    pending_signal = None
    previous = {}

    def interrupted(signum, frame):
        nonlocal pending_signal, stopping
        if launching or stopping:
            # Finish acquiring the child's PID before cleanup; repeated signals
            # cannot interrupt the bounded TERM/KILL/reap sequence.
            pending_signal = signum
            return
        stopping = True
        raise AdapterInterrupted(signum)

    try:
        for signum in (signal.SIGTERM, signal.SIGINT):
            previous[signum] = signal.getsignal(signum)
            signal.signal(signum, interrupted)
        # A private group contains the local scientific/MPI descendants.
        process = subprocess.Popen(command, cwd=repository, start_new_session=True)
        launching = False
        if pending_signal is not None:
            stopping = True
            raise AdapterInterrupted(pending_signal)
        result = process.wait(timeout=_remaining(deadline))
        _remaining(deadline)
        return result
    except subprocess.TimeoutExpired as error:
        stopping = True
        if process is not None:
            _stop_group(process)
        raise AllocationDeadlineExceeded("Scientific process reached its native allocation deadline") from error
    except BaseException:
        stopping = True
        if process is not None:
            _stop_group(process)
        raise
    finally:
        stopping = True
        for signum, handler in previous.items():
            signal.signal(signum, handler)


def execute(manifest_path):
    manifest_path = Path(manifest_path)
    manifest = json.loads(manifest_path.read_text())
    required = {
        "repository", "science_revision", "python_bin", "environment_script",
        "environment_sha256", "adapter_sha256", "request", "campaign_root",
        "campaign_instance_id", "configuration_hash", "stage", "internal_seconds",
        "device", "runtime_clock_policy", "allocated_seconds",
    }
    if not isinstance(manifest, dict) or not required <= set(manifest) or set(manifest) - required - {"allocation_costs", "allocation_costs_sha256"}:
        raise ValueError("Unexpected or missing trusted allocation manifest fields")
    clock = allocation_clock(manifest)
    deadline = _Deadline(clock["watchdog_deadline_epoch"])
    _clock_record(manifest_path, clock)
    if digest(Path(__file__)) != manifest["adapter_sha256"]:
        raise ValueError("Adapter snapshot differs from operator pin")
    repository = Path(manifest["repository"])
    revision = subprocess.check_output(
        ["git", "-C", str(repository), "rev-parse", "HEAD"],
        text=True, timeout=_remaining(deadline),
    ).strip()
    dirty = subprocess.check_output(
        ["git", "-C", str(repository), "status", "--porcelain", "--untracked-files=all", "--", "src/meso_uq"],
        text=True, timeout=_remaining(deadline),
    ).strip()
    if revision != manifest["science_revision"] or dirty:
        raise ValueError("Scientific source differs from the immutable approved revision")
    if digest(Path(manifest["environment_script"])) != manifest["environment_sha256"]:
        raise ValueError("Scientific environment script differs from operator pin")
    request = json.loads(Path(manifest["request"]).read_text())
    fingerprint = hashlib.sha256(json.dumps(request, sort_keys=True, separators=(",", ":"), ensure_ascii=True, allow_nan=False).encode()).hexdigest()
    if fingerprint != manifest["configuration_hash"]:
        raise ValueError("Frozen scientific request differs from campaign hash")
    command = [
        manifest["python_bin"], "-m", "meso_uq.benchmark_lab.cli", "run-stage",
        "--request", manifest["request"], "--campaign", manifest["campaign_root"],
        "--campaign-instance-id", manifest["campaign_instance_id"],
        "--stage", manifest["stage"], "--reserved-seconds", str(manifest["internal_seconds"]),
        "--device", manifest["device"], "--allocation-job-id", clock["slurm_job_id"],
        "--allocation-start-epoch", str(clock["allocation_start_epoch"]),
        "--allocation-deadline-epoch", str(clock["allocation_deadline_epoch"]),
    ]
    if manifest["stage"] == "metrics":
        if not manifest.get("allocation_costs") or digest(Path(manifest["allocation_costs"])) != manifest.get("allocation_costs_sha256"):
            raise ValueError("Metrics requires a pinned authoritative allocation-cost snapshot")
        command += ["--allocation-costs", manifest["allocation_costs"], "--allocation-costs-sha256", manifest["allocation_costs_sha256"]]
    elif "allocation_costs" in manifest or "allocation_costs_sha256" in manifest:
        raise ValueError("Allocation-cost snapshots are metrics-only")
    if manifest["stage"] == "tmcmc":
        command = ["mpirun", "-np", "1", *command]
    _remaining(deadline)
    return run_science(command, repository, deadline)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", required=True)
    args = parser.parse_args(argv)
    try:
        return execute(args.manifest)
    except AdapterInterrupted as error:
        return 128 + error.signum


if __name__ == "__main__":
    raise SystemExit(main())
