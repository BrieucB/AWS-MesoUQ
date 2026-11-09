"""Fresh isolated RPC admission for pinned native metadata implementations."""
from __future__ import annotations

import ast
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys

import pytest

SOURCE = Path(__file__).parents[1] / "src/aws_mesouq/adapters/benchmark_accuracy.py"
SPEC = importlib.util.spec_from_file_location("rpc_admission_under_test", SOURCE)
native = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(native)
TREE = ast.parse(SOURCE.read_text())
REMOTE_REQUEST = next(
    ast.get_source_segment(SOURCE.read_text(), node)
    for node in TREE.body
    if isinstance(node, ast.FunctionDef) and node.name == "remote_request"
)
METADATA = ("preflight", "observe", "publication", "read_artifact")


def private(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.parent.chmod(0o700)
    path.write_bytes(data)
    path.chmod(0o600)
    return path


def legacy_fixture(tmp_path, *, operation, staging=True, failure=None, enabled=True):
    """Pin the real old remote_request lifecycle with synthetic external boundaries.

    The native source, context and payload stay fixed while the coordinator
    supplies import admission. Native package code is metadata-only.
    """
    root = tmp_path.resolve()
    root.chmod(0o700)
    package = root / "old-native-src"
    private(package / "aws_mesouq/__init__.py", b"")
    private(package / "aws_mesouq/source_staging.py", b"STAGED = True\n")
    protocol = private(
        package / "aws_mesouq/benchmark_accuracy_protocol.py",
        b"def validate_metadata(value): return value\n",
    )
    trace = root / "trace.json"
    lease = root / "lease"
    boundary = {
        "root": str(package),
        "trace": str(trace),
        "lease": str(lease),
        "staging": staging,
        "failure": failure,
        "protocol_sha256": hashlib.sha256(protocol.read_bytes()).hexdigest(),
    }
    source = """
import hashlib,importlib.abc,json,sys
from pathlib import Path
class NoScientificImports(importlib.abc.MetaPathFinder):
 def find_spec(self,fullname,path=None,target=None):
  if fullname.split('.')[0] in {'meso_uq','jax','torch','numpy','scipy','bayesflow','keras'}:
   raise AssertionError('scientific_import_forbidden')
  return None
sys.meta_path.insert(0,NoScientificImports())
BOUNDARY = __BOUNDARY__
def record(value):
 path=Path(BOUNDARY['trace'])
 values=json.loads(path.read_text()) if path.exists() else []
 path.write_text(json.dumps(values+[value]))
class Guard:
 def __init__(self):
  self.installed=False
 def install(self):
  if self.installed:
   record('install_idempotent')
   return self
  if any(name=='aws_mesouq' or name.startswith('aws_mesouq.') for name in sys.modules):
   raise RuntimeError('own_package_already_imported')
  record('install')
  Path(BOUNDARY['lease']).write_text('synthetic lease')
  if BOUNDARY['failure']=='install':
   raise RuntimeError('synthetic_install_failure')
  if BOUNDARY['failure']=='install_interrupt':
   raise KeyboardInterrupt('synthetic_install_interrupt')
  sys.path.insert(0,BOUNDARY['root'])
  if BOUNDARY['staging']:
   from aws_mesouq import source_staging
   assert source_staging.STAGED is True
   record('staging_import')
  self.installed=True
  return self
 def check(self):
  record('check')
  path=Path(BOUNDARY['root'])/'aws_mesouq/benchmark_accuracy_protocol.py'
  assert hashlib.sha256(path.read_bytes()).hexdigest()==BOUNDARY['protocol_sha256']
  if BOUNDARY['failure']=='check':
   raise RuntimeError('synthetic_check_failure')
 def close(self):
  record('close')
  Path(BOUNDARY['lease']).unlink(missing_ok=True)
def _verify_profile(*args,**kwargs):
 record('verify')
 if BOUNDARY['failure']=='verify':
  raise RuntimeError('synthetic_verification_failure')
 return Guard()
def _keys(value,required,optional=()):
 if not required<=set(value) or set(value)-required-set(optional):
  raise ValueError('unexpected_fields')
class NativeContractError(Exception):
 pass
def _home_preflight(context):
 return None
def metadata(payload):
 from aws_mesouq import benchmark_accuracy_protocol as protocol
 record('operation')
 protocol.validate_metadata(payload)
 if BOUNDARY['failure']=='operation':
  raise RuntimeError('synthetic_operation_failure')
 return {'verified':True}
def _require_new_work_qualification(context,guard):
 assert guard.installed is True
 if not context['enabled']:
  raise NativeContractError('disabled_new_work')
def _prepared_preflight(payload,guard):
 if not payload['context']['enabled']:
  raise NativeContractError('disabled_new_work')
 if BOUNDARY['failure']=='quota':
  error=NativeContractError('synthetic_quota_denial')
  error.preflight={'eligible':False}
  raise error
 return metadata(payload)
def _observe(payload):
 return metadata(payload)
def _publication(payload,guard):
 result=metadata(payload)
 guard.install()
 return result
def _read_artifact(payload):
 metadata(payload)
 return b'synthetic-artifact'
def _prepare(payload):
 guard=_verify_profile(payload['context'],payload['operator'])
 try:
  guard.install()
  return metadata(payload)
 finally:
  try: guard.check()
  finally: guard.close()
""".replace("__BOUNDARY__", repr(boundary))
    source += "\n" + REMOTE_REQUEST + "\n"
    entry = private(root / "old-native-adapter.py", source.encode())
    request = {
        "operation": operation,
        "context": {
            "adapter_sha256": hashlib.sha256(entry.read_bytes()).hexdigest(),
            "enabled": enabled,
        },
        "operator": {"adapter_path": str(entry)},
    }
    return entry, request, trace, lease


def invoke(request):
    return subprocess.run(
        [sys.executable, "-I", "-S", "-B", "-c", native._BOOTSTRAP],
        input=native.canonical(request),
        capture_output=True,
        timeout=20,
    )


def events(trace):
    return json.loads(trace.read_text()) if trace.exists() else []


@pytest.mark.parametrize("operation", METADATA)
@pytest.mark.parametrize("staging", [False, True])
def test_metadata_rpc_admits_same_guard_in_fresh_process(tmp_path, operation, staging):
    entry, request, trace, lease = legacy_fixture(
        tmp_path, operation=operation, staging=staging
    )
    original_source = entry.read_bytes()
    original_request = native.canonical(request)
    result = invoke(request)
    assert result.returncode == 0, result.stderr.decode()
    if operation == "read_artifact":
        assert result.stdout == b"synthetic-artifact"
    else:
        assert json.loads(result.stdout) == {"verified": True}
    observed = events(trace)
    assert observed.count("verify") == 1
    assert observed.count("install") == 1
    assert observed.count("operation") == 1
    assert observed[-2:] == ["check", "close"]
    assert ("staging_import" in observed) == staging
    assert ("install_idempotent" in observed) == (operation == "publication")
    assert not lease.exists()
    assert entry.read_bytes() == original_source
    assert native.canonical(request) == original_request


@pytest.mark.parametrize("operation", ("observe", "publication", "read_artifact"))
def test_disabled_historical_metadata_keeps_recovery_access(tmp_path, operation):
    _, request, trace, lease = legacy_fixture(
        tmp_path, operation=operation, enabled=False
    )
    result = invoke(request)
    assert result.returncode == 0, result.stderr.decode()
    assert events(trace)[-2:] == ["check", "close"]
    assert not lease.exists()


def test_disabled_context_still_refuses_new_submission_preflight(tmp_path):
    _, request, trace, lease = legacy_fixture(
        tmp_path, operation="preflight", enabled=False
    )
    result = invoke(request)
    assert result.returncode != 0
    assert b"disabled_new_work" in result.stderr
    assert "operation" not in events(trace)
    assert events(trace)[-2:] == ["check", "close"]
    assert not lease.exists()


@pytest.mark.parametrize("failure", ("verify", "install", "install_interrupt", "operation", "check"))
def test_metadata_failure_closes_owned_guard_and_staging_lease(tmp_path, failure):
    _, request, trace, lease = legacy_fixture(
        tmp_path, operation="publication", failure=failure
    )
    result = invoke(request)
    assert result.returncode != 0
    observed = events(trace)
    if failure == "verify":
        assert observed == ["verify"]
    else:
        assert observed[-1] == "close"
        assert observed.count("close") == 1
    assert not lease.exists()


@pytest.mark.parametrize("operation", ("prepare", "verify_profile"))
def test_existing_prepare_and_profile_routes_keep_native_admission(tmp_path, operation):
    _, request, trace, lease = legacy_fixture(tmp_path, operation=operation)
    result = invoke(request)
    assert result.returncode == 0, result.stderr.decode()
    observed = events(trace)
    assert observed.count("verify") == 1
    assert observed.count("install") == 1
    assert observed.count("close") == 1
    assert not lease.exists()


def test_changed_native_entrypoint_is_rejected_before_admission(tmp_path):
    entry, request, trace, lease = legacy_fixture(tmp_path, operation="preflight")
    entry.write_bytes(entry.read_bytes() + b"\nCHANGED=True\n")
    result = invoke(request)
    assert result.returncode != 0
    assert not trace.exists()
    assert not lease.exists()


def test_unknown_operation_is_rejected_before_admission(tmp_path):
    _, request, trace, lease = legacy_fixture(tmp_path, operation="unknown")
    result = invoke(request)
    assert result.returncode != 0
    assert not trace.exists()
    assert not lease.exists()


def test_quota_denial_keeps_the_native_typed_response_and_cleanup(tmp_path):
    _, request, trace, lease = legacy_fixture(
        tmp_path, operation="preflight", failure="quota"
    )
    result = invoke(request)
    assert result.returncode == 0, result.stderr.decode()
    assert json.loads(result.stdout) == {
        "schema": "aws_mesouq.benchmark_accuracy_native_error.v1",
        "code": "home_quota_unhealthy",
        "preflight": {"eligible": False},
    }
    assert events(trace)[-2:] == ["check", "close"]
    assert not lease.exists()
