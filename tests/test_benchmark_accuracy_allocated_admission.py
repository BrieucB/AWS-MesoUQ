"""Fresh allocated-entry admission and private scheduler-log regressions."""
from __future__ import annotations

from copy import deepcopy
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import stat
import subprocess
import sys
import time

import pytest

SOURCE = Path(__file__).parents[1] / "src/aws_mesouq/adapters/benchmark_accuracy.py"
SPEC = importlib.util.spec_from_file_location("allocated_native_under_test", SOURCE)
native = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(native)
FIXTURE_SPEC = importlib.util.spec_from_file_location(
    "allocated_existing_fixtures", Path(__file__).with_name("test_benchmark_accuracy_adapter.py")
)
fixtures = importlib.util.module_from_spec(FIXTURE_SPEC)
FIXTURE_SPEC.loader.exec_module(fixtures)


def private(path, raw):
    return fixtures.private(path, raw)


def sha(raw):
    return hashlib.sha256(raw).hexdigest()


def source_module(name):
    return (SOURCE.parents[1] / name).read_bytes()


def allocated_fixture(tmp_path, monkeypatch, *, failure=None):
    root = tmp_path.resolve()
    root.chmod(0o700)
    protocol, manifest, control, path, _, _ = fixtures.real_protocol_native_control(
        root, monkeypatch, "allocated-é", portable_record_sink=True
    )
    manifest = control["manifest"]
    package = root / "admitted-src"
    for name in ("__init__.py", "benchmark_accuracy_protocol.py", "benchmark_accuracy.py", "benchmark.py"):
        private(package / "aws_mesouq" / name, source_module(name))
    private(package / "aws_mesouq/adapters/__init__.py", b"")
    private(package / "aws_mesouq/source_staging.py", b"STAGED = True\n")
    trace = root / "events.json"
    boundary = {"package": str(package), "trace": str(trace), "failure": failure}
    benchmark = (SOURCE.parent / "benchmark.py").read_text()
    benchmark += """
def run_science(command, directory, deadline):
 import json
 from pathlib import Path
 path=Path(__TRACE__)
 values=json.loads(path.read_text())
 path.write_text(json.dumps(values+[[os.getpid(),'child']]))
 if __FAILURE__=='child':
  raise RuntimeError('synthetic_child_failure')
 result=subprocess.run(command,cwd=directory,capture_output=True,check=False,timeout=20)
 if result.returncode:
  raise RuntimeError('synthetic_child_rejected')
 return result.returncode
""".replace("__TRACE__", repr(str(trace))).replace("__FAILURE__", repr(failure))
    private(package / "aws_mesouq/adapters/benchmark.py", benchmark.encode())
    injections = """
import importlib.abc
UNIT = __BOUNDARY__
class NoScientificImports(importlib.abc.MetaPathFinder):
 def find_spec(self,fullname,path=None,target=None):
  if fullname.split('.')[0] in {'meso_uq','jax','numpy','torch','scipy','bayesflow','keras'}:
   raise AssertionError('scientific_import_forbidden')
  return None
sys.meta_path.insert(0,NoScientificImports())
def event(value):
 path=Path(UNIT['trace'])
 values=json.loads(path.read_text()) if path.exists() else []
 path.write_text(json.dumps(values+[[os.getpid(),value]]))
class Guard:
 def __init__(self):
  self.installed=False
  self.lease=Path(UNIT['trace']).parent/('lease-'+str(os.getpid()))
 def install(self):
  if self.installed:
   raise AssertionError('guard_installed_twice')
  if any(name=='aws_mesouq' or name.startswith('aws_mesouq.') for name in sys.modules):
   raise AssertionError('own_import_before_admission')
  event('install')
  self.lease.write_text('synthetic')
  if UNIT['failure']=='install':
   raise KeyboardInterrupt('synthetic_install_failure')
  sys.path.insert(0,UNIT['package'])
  from aws_mesouq import source_staging
  assert source_staging.STAGED is True
  self.installed=True
  return self
 def check(self):
  event('check')
 def close(self):
  event('close')
  self.lease.unlink(missing_ok=True)
def _verify_profile(context,operator,*,allocated=False,dependencies=True):
 assert allocated is True
 event('verify')
 if UNIT['failure']=='verify':
  raise RuntimeError('synthetic_verification_failure')
 return Guard()
def _source_qualification(context,guard):
 # Explicit synthetic science authority; startup tests forbid model imports.
 assert guard.installed is True
 from aws_mesouq import benchmark_accuracy_protocol as protocol
 return protocol.validate_source_qualification({'schema':protocol.SOURCE_QUALIFICATION_SCHEMA,
  'science_revision':'f'*40,'source_binding_sha256':'d'*64,'semantic_policy_sha256':'e'*64})
def _require_scratch(path):
 return Path(path)
def _home_preflight(context,*,manifest_sha256=None):
 event('quota')
 if UNIT['failure']=='quota':
  raise RuntimeError('synthetic_quota_failure')
 return {'observed_at':time.time(),'eligible':True}
def immutable_json(path,value):
 event('output')
 path=Path(path)
 raw=canonical(value)
 if path.exists():
  if path.read_bytes()!=raw:
   raise RuntimeError('synthetic_immutable_conflict')
 else:
  path.write_bytes(raw)
  path.chmod(0o600)
 return str(path)
def _dispatch(control,clock):
 event('dispatch')
 if UNIT['failure'] in ('dispatch','nested_dispatch'):
  raise RuntimeError('synthetic_dispatch_failure')
 return 0
""".replace("__BOUNDARY__", repr(boundary))
    entry = private(root / "candidate-native.py", SOURCE.read_bytes() + injections.encode())
    environment = private(root / "environment.sh", b":\n")
    operator = control["operator"]
    operator.update(
        adapter_path=str(entry), python_bin=sys.executable,
        environment_script=str(environment), environment_sha256=sha(environment.read_bytes())
    )
    context = control["context"]
    context["adapter_sha256"] = sha(entry.read_bytes())
    context["runtime_profile_sha256"] = native.checksum(operator)
    context["id"] = protocol.context_id(context)
    manifest["context_id"] = context["id"]
    control["manifest_sha256"] = protocol.checksum(manifest)
    private(path, native.canonical(control))
    start = int(time.time()) - 2
    env = {
        **os.environ, "SLURM_JOB_ID": "123", "SLURM_JOB_START_TIME": str(start),
        "SLURM_JOB_END_TIME": str(start + 120),
        "AWS_MESOUQ_ALLOCATION_JOB_ID": "123",
        "AWS_MESOUQ_ALLOCATION_START_EPOCH": str(start),
        "AWS_MESOUQ_ALLOCATION_DEADLINE_EPOCH": str(start + 120),
    }
    return protocol, control, path, entry, trace, env


def invoke(control, path, entry, env, action="--execute-stage"):
    return subprocess.run(
        [sys.executable, "-I", "-S", "-B", "-c", native._NATIVE_BOOTSTRAP,
         str(entry), sha(entry.read_bytes()), str(path), sha(path.read_bytes()), action],
        env=env, capture_output=True, timeout=30
    )


def events(trace):
    return json.loads(trace.read_text()) if trace.exists() else []


def process_events(trace):
    result = {}
    for pid, event in events(trace):
        result.setdefault(pid, []).append(event)
    return list(result.values())


@pytest.mark.parametrize("action,processes", [("--manifest", 2), ("--execute-stage", 1)])
def test_genuine_allocated_bootstrap_admits_before_identity_and_preserves_clock(
    tmp_path, monkeypatch, action, processes
):
    _, control, path, entry, trace, env = allocated_fixture(tmp_path, monkeypatch)
    original = path.read_bytes()
    result = invoke(control, path, entry, env, action)
    assert result.returncode == 0, result.stderr.decode()
    observed = process_events(trace)
    assert len(observed) == processes
    for sequence in observed:
        assert sequence[:2] == ["verify", "install"]
        assert sequence[-2:] == ["check", "close"]
        assert sequence.count("verify") == sequence.count("install") == 1
    clock = json.loads((path.parent / "allocation-clock.json").read_bytes())
    start = int(env["SLURM_JOB_START_TIME"])
    assert clock["allocation_start_epoch"] == start
    assert clock["projected_end_epoch"] == start + 120
    assert clock["allocation_deadline_epoch"] == start + 60
    assert path.read_bytes() == original
    assert not list(tmp_path.glob("lease-*"))


@pytest.mark.parametrize("raw_failure", ["schema", "placement"])
def test_raw_control_failure_precedes_guard_admission(tmp_path, monkeypatch, raw_failure):
    _, control, path, entry, trace, env = allocated_fixture(tmp_path, monkeypatch)
    if raw_failure == "schema":
        control["schema"] = "invalid"
    else:
        control["control_directory"] = str(path.parent / "relocated")
    private(path, native.canonical(control))
    result = invoke(control, path, entry, env)
    assert result.returncode != 0
    assert not trace.exists()


@pytest.mark.parametrize("action", ["--manifest", "--execute-stage"])
def test_invalid_control_identity_is_rejected_before_effects(tmp_path, monkeypatch, action):
    _, control, path, entry, trace, env = allocated_fixture(tmp_path, monkeypatch)
    control["marker"] = "wrong"
    private(path, native.canonical(control))
    result = invoke(control, path, entry, env, action)
    assert result.returncode != 0
    assert [event for _, event in events(trace)] == ["verify", "install", "check", "close"]
    assert not (path.parent / "allocation-clock.json").exists()
    assert not list(tmp_path.glob("lease-*"))


@pytest.mark.parametrize("failure", ["verify", "install", "quota", "dispatch", "child", "nested_dispatch"])
def test_allocated_failure_closes_single_guard(tmp_path, monkeypatch, failure):
    _, control, path, entry, trace, env = allocated_fixture(
        tmp_path, monkeypatch, failure=failure
    )
    action = "--manifest" if failure in ("child", "nested_dispatch") else "--execute-stage"
    result = invoke(control, path, entry, env, action)
    assert result.returncode != 0
    observed = process_events(trace)
    if failure == "verify":
        assert observed == [["verify"]]
    else:
        for sequence in observed:
            assert sequence[-2:] == ["check", "close"]
        if failure == "quota":
            assert "quota" in observed[0] and "output" not in observed[0]
        elif failure in ("dispatch", "nested_dispatch"):
            assert any("dispatch" in sequence for sequence in observed)
        elif failure == "child":
            assert "child" in observed[0]
    assert len(observed) == (2 if failure == "nested_dispatch" else 1)
    assert not list(tmp_path.glob("lease-*"))


@pytest.mark.parametrize("change", ["missing", "partial", "malformed", "mismatch", "exhausted"])
def test_strict_original_clock_rejects_invalid_environment_before_outputs(
    tmp_path, monkeypatch, change
):
    _, control, path, entry, trace, env = allocated_fixture(tmp_path, monkeypatch)
    if change == "missing":
        for key in list(env):
            if key.startswith("AWS_MESOUQ_ALLOCATION_"):
                del env[key]
    elif change == "partial":
        del env["AWS_MESOUQ_ALLOCATION_DEADLINE_EPOCH"]
    elif change == "malformed":
        env["AWS_MESOUQ_ALLOCATION_START_EPOCH"] = "not-an-integer"
    elif change == "mismatch":
        env["SLURM_JOB_ID"] = "124"
    else:
        start = int(time.time()) - 120
        env.update(
            SLURM_JOB_START_TIME=str(start), SLURM_JOB_END_TIME=str(start + 60),
            AWS_MESOUQ_ALLOCATION_START_EPOCH=str(start),
            AWS_MESOUQ_ALLOCATION_DEADLINE_EPOCH=str(start + 60),
        )
    result = invoke(control, path, entry, env)
    assert result.returncode != 0
    assert "quota" in [event for _, event in events(trace)]
    assert "output" not in [event for _, event in events(trace)]
    assert process_events(trace)[0][-2:] == ["check", "close"]
    assert not list(tmp_path.glob("lease-*"))


@pytest.mark.parametrize("missing", ["SLURM_JOB_START_TIME", "SLURM_JOB_END_TIME"])
def test_generated_script_rejects_missing_original_epoch_before_python(
    tmp_path, monkeypatch, missing
):
    _, control, path, entry, trace, env = allocated_fixture(tmp_path, monkeypatch)
    del env[missing]
    script = private(
        tmp_path / "run-production.sh",
        native._script(control, control["context"], control["operator"], control["window"]).encode()
    )
    result = subprocess.run(
        ["/bin/bash", "--noprofile", "--norc", str(script)],
        env=env, capture_output=True, timeout=20
    )
    assert result.returncode != 0
    assert missing.encode() in result.stderr
    assert not trace.exists()


def test_complete_generated_script_reaches_supervisor_and_child(tmp_path, monkeypatch):
    _, control, path, entry, trace, env = allocated_fixture(tmp_path, monkeypatch)
    script = private(
        tmp_path / "run-production.sh",
        native._script(control, control["context"], control["operator"], control["window"]).encode()
    )
    assert b"#SBATCH --open-mode=append\n" in script.read_bytes()
    result = subprocess.run(
        ["/bin/bash", "--noprofile", "--norc", str(script)],
        env=env, capture_output=True, timeout=30
    )
    assert result.returncode == 0, result.stderr.decode()
    assert len(process_events(trace)) == 2
    assert sum(event == "dispatch" for _, event in events(trace)) == 1


def test_existing_load_control_still_fully_validates(tmp_path, monkeypatch):
    _, control, path, _, _, _ = allocated_fixture(tmp_path, monkeypatch)
    assert native._load_control(str(path)) == control
    control["marker"] = "wrong"
    private(path, native.canonical(control))
    with pytest.raises(native.NativeContractError, match="source-bound"):
        native._load_control(str(path))


@pytest.mark.skipif(sys.platform != "linux", reason="Karolina native ACL checks require Linux")
def test_scheduler_log_creation_and_identical_retry_preserve_contents(tmp_path):
    directory = fixtures.private_fixture_directory(tmp_path / "private")
    native._prepare_scheduler_logs(directory)
    for name in ("stdout.log", "stderr.log"):
        path = directory / name
        assert stat.S_IMODE(path.stat().st_mode) == 0o600
        path.write_bytes(b"existing nonempty scheduler log")
    before = {name: (directory / name).read_bytes() for name in ("stdout.log", "stderr.log")}
    native._prepare_scheduler_logs(directory)
    assert {name: (directory / name).read_bytes() for name in before} == before


@pytest.mark.skipif(sys.platform != "linux", reason="Karolina native ACL checks require Linux")
@pytest.mark.parametrize("unsafe", ["symlink", "hardlink", "fifo", "permissive"])
def test_scheduler_log_preparation_rejects_unsafe_existing_files(tmp_path, unsafe):
    directory = fixtures.private_fixture_directory(tmp_path / "private")
    other = private(tmp_path / "excluded", b"unrelated")
    path = directory / "stdout.log"
    if unsafe == "symlink":
        path.symlink_to(other)
    elif unsafe == "hardlink":
        os.link(other, path)
    elif unsafe == "fifo":
        os.mkfifo(path, 0o600)
    else:
        private(path, b"existing").chmod(0o664)
    before = path.lstat()
    with pytest.raises((OSError, native.NativeContractError)):
        native._prepare_scheduler_logs(directory)
    after = path.lstat()
    assert (after.st_dev, after.st_ino, after.st_mode, after.st_nlink, after.st_size) == (
        before.st_dev, before.st_ino, before.st_mode, before.st_nlink, before.st_size
    )
    assert other.read_bytes() == b"unrelated"
    if unsafe == "permissive":
        assert path.read_bytes() == b"existing"
        assert stat.S_IMODE(path.stat().st_mode) == 0o664


@pytest.mark.skipif(sys.platform != "linux", reason="Karolina native ACL checks require Linux")
def test_new_scheduler_log_chmod_failure_removes_only_its_created_file(tmp_path, monkeypatch):
    directory = fixtures.private_fixture_directory(tmp_path / "private")
    descriptors = []
    def denied(descriptor, mode):
        descriptors.append(descriptor)
        raise OSError("synthetic chmod failure")
    monkeypatch.setattr(native.os, "fchmod", denied)
    with pytest.raises(OSError, match="synthetic chmod"):
        native._prepare_scheduler_logs(directory)
    assert not (directory / "stdout.log").exists()
    assert not (directory / "stderr.log").exists()
    with pytest.raises(OSError):
        os.fstat(descriptors[0])


@pytest.mark.skipif(sys.platform != "linux", reason="Karolina native ACL checks require Linux")
def test_new_scheduler_log_sync_failure_cleans_its_file(tmp_path, monkeypatch):
    directory = fixtures.private_fixture_directory(tmp_path / "private")
    def denied(descriptor):
        raise OSError("synthetic sync failure")
    monkeypatch.setattr(native.os, "fsync", denied)
    with pytest.raises(OSError, match="synthetic sync"):
        native._prepare_scheduler_logs(directory)
    assert not (directory / "stdout.log").exists()
    assert not (directory / "stderr.log").exists()


@pytest.mark.skipif(sys.platform != "linux", reason="Karolina native ACL checks require Linux")
def test_scheduler_log_failure_preserves_a_replaced_leaf(tmp_path, monkeypatch):
    directory = fixtures.private_fixture_directory(tmp_path / "private")
    replacement_inodes = []
    def replaced(descriptor, mode):
        (directory / "stdout.log").rename(directory / "detached-created.log")
        private(directory / "stdout.log", b"unrelated replacement")
        replacement_inodes.append((directory / "stdout.log").stat().st_ino)
        raise OSError("synthetic replacement failure")
    monkeypatch.setattr(native.os, "fchmod", replaced)
    with pytest.raises(OSError, match="synthetic replacement"):
        native._prepare_scheduler_logs(directory)
    assert (directory / "stdout.log").read_bytes() == b"unrelated replacement"
    assert (directory / "stdout.log").stat().st_ino == replacement_inodes[0]
    assert (directory / "detached-created.log").read_bytes() == b""


@pytest.mark.skipif(sys.platform != "linux", reason="Karolina native ACL checks require Linux")
def test_normal_prepare_precreates_private_scheduler_logs_before_handoff(tmp_path, monkeypatch):
    _, _, control, path, _ = fixtures.real_protocol_native_control(
        tmp_path.resolve(), monkeypatch, "prepare-é", portable_record_sink=False
    )
    assert (path.parent / "run.sh").is_file()
    for name in ("stdout.log", "stderr.log"):
        info = (path.parent / name).lstat()
        assert stat.S_ISREG(info.st_mode)
        assert info.st_uid == os.getuid()
        assert info.st_nlink == 1
        assert stat.S_IMODE(info.st_mode) == 0o600
        assert info.st_size == 0
    assert control["control_directory"] == str(path.parent)
