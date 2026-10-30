"""Trusted adapter with an allocation-wide native deadline and bounded child cleanup."""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
from pathlib import Path
import re
import signal
import stat
import subprocess
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


def _stop_group(process):
    # The parent may have exited while a descendant still owns the group.
    try:
        os.killpg(process.pid, signal.SIGTERM)
    except ProcessLookupError:
        pass
    try:
        process.wait(timeout=2)
    except subprocess.TimeoutExpired:
        pass
    try:
        os.killpg(process.pid, signal.SIGKILL)
    except ProcessLookupError:
        pass
    process.wait(timeout=2)


def run_science(command, repository, watchdog_deadline):
    deadline = watchdog_deadline if isinstance(watchdog_deadline, _Deadline) else _Deadline(watchdog_deadline)
    _remaining(deadline)
    # A private process group also includes local MPI/scientific descendants.
    process = subprocess.Popen(command, cwd=repository, start_new_session=True)
    try:
        result = process.wait(timeout=_remaining(deadline))
        _remaining(deadline)
        return result
    except subprocess.TimeoutExpired as error:
        _stop_group(process)
        raise AllocationDeadlineExceeded("Scientific process reached its native allocation deadline") from error
    except BaseException:
        _stop_group(process)
        raise


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
    return execute(args.manifest)


if __name__ == "__main__":
    raise SystemExit(main())
