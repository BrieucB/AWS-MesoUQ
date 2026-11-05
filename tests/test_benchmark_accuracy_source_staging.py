"""Authenticated source-staging integration; synthetic code and data only."""
import hashlib
import importlib.util
import json
from pathlib import Path
import subprocess
import sys

import pytest

SOURCE = Path(__file__).parents[1] / "src/aws_mesouq/adapters/benchmark_accuracy.py"
HELPER = SOURCE.parents[1] / "source_staging.py"
SPEC = importlib.util.spec_from_file_location("staged_adapter_test", SOURCE)
native = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(native)


def fixture(tmp_path):
    tmp_path.chmod(0o700)
    aws = tmp_path / "aws/src/aws_mesouq"
    sci = tmp_path / "science/src/meso_uq"
    content = {
        aws / "__init__.py": b"",
        aws / "adapters/benchmark_accuracy.py": b"MARKER = 'fixture'\n",
        aws / "source_staging.py": HELPER.read_bytes(),
        sci / "__init__.py": b"",
        sci / "known.py": b"VALUE = 'authenticated'\n",
        sci / "references/example.csv": b"1,2\n",
    }
    for path, raw in content.items():
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        for parent in path.parents:
            if parent == tmp_path:
                break
            parent.chmod(0o700)
        path.write_bytes(raw)
        path.chmod(0o600)
    sha = lambda raw: hashlib.sha256(raw).hexdigest()
    sources = {str(path): sha(raw) for path, raw in content.items() if path.suffix == ".py"}
    resources = {str(path): sha(raw) for path, raw in content.items() if path.suffix != ".py"}
    operator = {
        "adapter_path": str(aws / "adapters/benchmark_accuracy.py"),
        "repository": str(tmp_path / "science"),
        "native_python_sources": sources,
        "native_package_resources": resources,
        "source_staging": {
            "root": str(tmp_path / "node-local"),
            "max_bytes": 1048576, "max_namespaces": 2,
        },
    }
    binding = {str(Path(path).relative_to(sci)): expected
               for path, expected in sources.items() if sci in Path(path).parents}
    context = {
        "capabilities": ["verified_source_staging_v1"],
        "runtime_profile_sha256": native.checksum(operator),
        "adapter_sha256": sources[operator["adapter_path"]],
        "source_binding_sha256": native.checksum(binding),
    }
    return context, operator


def rebind(context, operator):
    context["runtime_profile_sha256"] = native.checksum(operator)


@pytest.mark.parametrize("field", ["source_staging", "native_package_resources"])
def test_staging_requires_both_policy_and_resource_closure(tmp_path, field):
    context, operator = fixture(tmp_path)
    del operator[field]
    rebind(context, operator)
    with pytest.raises(native.NativeContractError, match="agree"):
        native.SourceGuard(context, operator)


def test_staging_requires_explicit_capability(tmp_path):
    context, operator = fixture(tmp_path)
    context["capabilities"] = []
    with pytest.raises(native.NativeContractError, match="agree"):
        native.SourceGuard(context, operator)


def test_all_non_python_package_resources_are_bound(tmp_path):
    context, operator = fixture(tmp_path)
    operator["native_package_resources"] = {}
    rebind(context, operator)
    with pytest.raises(native.NativeContractError, match="resource closure"):
        native.SourceGuard(context, operator)


def test_resource_mutation_is_detected_before_staging(tmp_path):
    context, operator = fixture(tmp_path)
    Path(next(iter(operator["native_package_resources"]))).write_bytes(b"3,4\n")
    with pytest.raises(native.NativeContractError, match="resource checksum"):
        native.SourceGuard(context, operator)


def test_staging_helper_must_belong_to_verified_aws_source_closure(tmp_path):
    context, operator = fixture(tmp_path)
    del operator["native_python_sources"][str(tmp_path / "aws/src/aws_mesouq/source_staging.py")]
    rebind(context, operator)
    with pytest.raises(native.NativeContractError, match="helper"):
        native.SourceGuard(context, operator)


CHILD = r"""
import importlib.util,json,pathlib,sys
spec=importlib.util.spec_from_file_location('isolated_native_adapter',sys.argv[1])
native=importlib.util.module_from_spec(spec);spec.loader.exec_module(native)
context,operator=json.loads(sys.argv[2]);action=sys.argv[3]
guard=native.SourceGuard(context,operator)
guard.search_path=list(sys.path)
try:
 guard.install()
 import meso_uq.known
 assert meso_uq.known.VALUE=='authenticated'
 assert pathlib.Path(meso_uq.known.__file__).is_relative_to(guard.science_root)
 assert guard.science_root==pathlib.Path(operator['source_staging']['root'])/context['runtime_profile_sha256']/'src'
 assert (guard.science_root/'meso_uq/references/example.csv').read_bytes()==b'1,2\n'
 guard.check()
 if action=='original':
  target=pathlib.Path(operator['repository'])/'src/meso_uq/known.py'
  target.write_bytes(b"VALUE = 'unauthenticated'\n")
 elif action=='staged':
  target=guard.science_root/'meso_uq/known.py'
  target.chmod(0o600);target.write_bytes(b"VALUE = 'unauthenticated'\n")
 elif action=='membership':
  target=guard.science_root/'meso_uq/extra.py'
  target.parent.chmod(0o700)
  target.write_bytes(b'EXTRA=True\n');target.chmod(0o400)
 if action!='normal':
  try: guard.check()
  except (ValueError,RuntimeError,OSError): pass
  else: raise AssertionError('post-install mutation admitted')
 print('verified')
finally:
 guard.close()
 assert guard.staging_lease is None
"""


@pytest.mark.parametrize("action", ["normal", "original", "staged", "membership"])
def test_staged_origins_resources_and_final_integrity_in_fresh_process(tmp_path, action):
    context, operator = fixture(tmp_path)
    result = subprocess.run(
        [sys.executable, "-I", "-B", "-c", CHILD, str(SOURCE),
         json.dumps([context, operator]), action],
        capture_output=True, text=True, timeout=20)
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "verified"
