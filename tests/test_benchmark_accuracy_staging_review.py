"""Independent closure regressions for verified scientific source staging."""
import json
import os
from pathlib import Path
import subprocess
import sys

import pytest

from test_benchmark_accuracy_source_staging import fixture, native, SOURCE


@pytest.mark.parametrize("kind", ["symlink_directory", "fifo"])
def test_source_inventory_rejects_nonregular_aliases_and_special_files(tmp_path, kind):
    context, operator = fixture(tmp_path)
    package = Path(operator["repository"]) / "src/meso_uq"
    if kind == "symlink_directory":
        target = tmp_path / "external"
        target.mkdir()
        (target / "outside.py").write_bytes(b"VALUE = 'outside'")
        (package / "external").symlink_to(target, target_is_directory=True)
    else:
        os.mkfifo(package / "special-input")
    with pytest.raises((native.NativeContractError, ValueError, OSError)):
        native.SourceGuard(context, operator)


CHILD = r"""
import importlib.util,json,pathlib,sys
spec=importlib.util.spec_from_file_location('closure_review_adapter',sys.argv[1])
native=importlib.util.module_from_spec(spec);spec.loader.exec_module(native)
context,operator=json.loads(sys.argv[2]);kind=sys.argv[3]
guard=native.SourceGuard(context,operator)
guard.search_path=list(sys.path)
try:
 guard.install()
 import meso_uq.known
 guard.check()
 root=pathlib.Path(operator['repository'])/'src/meso_uq'
 if kind=='python':
  (root/'added_after_freeze.py').write_bytes(b'ADDED=True')
 else:
  (root/'references/added_after_freeze.csv').write_bytes(b'1,2')
 try:guard.check()
 except (ValueError,RuntimeError,OSError):pass
 else:raise AssertionError('original package membership changed without detection')
finally:
 guard.close()
"""


@pytest.mark.parametrize("kind", ["python", "resource"])
def test_original_source_membership_is_rechecked_after_staging(tmp_path, kind):
    context, operator = fixture(tmp_path)
    result = subprocess.run([sys.executable, "-I", "-B", "-c", CHILD,
        str(SOURCE), json.dumps([context, operator]), kind],
        capture_output=True, text=True, timeout=20)
    assert result.returncode == 0, result.stderr
