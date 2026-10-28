"""Standalone trusted adapter executed inside a native Slurm allocation."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import subprocess


def digest(path):
    value = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1048576), b""):
            value.update(block)
    return value.hexdigest()


def execute(manifest_path):
    manifest_path = Path(manifest_path)
    manifest = json.loads(manifest_path.read_text())
    if not os.environ.get("SLURM_JOB_ID", "").isdecimal():
        raise RuntimeError("Benchmark adapter requires a native Slurm allocation")
    if digest(Path(__file__)) != manifest["adapter_sha256"]:
        raise ValueError("Adapter snapshot differs from operator pin")
    repository = Path(manifest["repository"])
    revision = subprocess.check_output(["git", "-C", str(repository), "rev-parse", "HEAD"], text=True).strip()
    dirty = subprocess.check_output(["git", "-C", str(repository), "status", "--porcelain", "--untracked-files=all", "--", "src/meso_uq"], text=True).strip()
    if revision != manifest["science_revision"] or dirty:
        raise ValueError("Scientific source differs from the immutable approved revision")
    if digest(Path(manifest["environment_script"])) != manifest["environment_sha256"]:
        raise ValueError("Scientific environment script differs from operator pin")
    request = json.loads(Path(manifest["request"]).read_text())
    fingerprint = hashlib.sha256(json.dumps(request, sort_keys=True, separators=(",", ":"), ensure_ascii=True, allow_nan=False).encode()).hexdigest()
    if fingerprint != manifest["configuration_hash"]:
        raise ValueError("Frozen scientific request differs from campaign hash")
    command = [manifest["python_bin"], "-m", "meso_uq.benchmark_lab.cli", "run-stage",
               "--request", manifest["request"], "--campaign", manifest["campaign_root"],
               "--stage", manifest["stage"], "--reserved-seconds", str(manifest["internal_seconds"]),
               "--device", manifest["device"]]
    if manifest["stage"] == "tmcmc":
        command = ["mpirun", "-np", "1", *command]
    return subprocess.run(command, cwd=repository, timeout=manifest["internal_seconds"] + 5).returncode


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", required=True)
    args = parser.parse_args(argv)
    return execute(args.manifest)


if __name__ == "__main__":
    raise SystemExit(main())
