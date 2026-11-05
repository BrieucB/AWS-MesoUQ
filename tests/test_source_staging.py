"""Source staging is an immutable byte store with explicit admission."""
import fcntl
import json
import os
import subprocess
import sys
from types import SimpleNamespace

import pytest

from aws_mesouq import source_staging as S


@pytest.fixture
def source_case(tmp_path):
    return SimpleNamespace(root=tmp_path/"sources", qualification="a"*64,
        files={"meso_uq/__init__.py": b"", "meso_uq/module.py": b"answer = 42\n",
               "meso_uq/data/settings.json": b'{"value":1}\n'})


def stage(case, **kwargs):
    options = dict(max_bytes=1048576, max_namespaces=3)
    options.update(kwargs)
    return S.stage_sources(case.root, case.qualification, case.files, **options)


def test_stages_python_and_declared_resources_readonly(source_case):
    c = source_case
    with stage(c) as lease:
        assert lease.source_root == c.root/c.qualification/"src"
        for name, value in c.files.items():
            path = lease.source_root/name
            assert path.read_bytes() == value
            assert path.stat().st_mode & 0o777 == 0o400
        assert lease.source_root.stat().st_mode & 0o777 == 0o500
        report = lease.verify()
        assert report["source_files"] == 3
        assert report["qualification_sha256"] == c.qualification
    with pytest.raises(S.SourceStagingError, match="active process-owned"):
        lease.verify()


def test_two_leases_hold_shared_locks_outside_immutable_namespace(source_case):
    first = stage(source_case)
    second = stage(source_case)
    lock = source_case.root/".leases"/(source_case.qualification+".lock")
    descriptor = os.open(lock, os.O_RDWR)
    try:
        for lease in (first, second):
            with pytest.raises(BlockingIOError):
                fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
            lease.close()
        fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        fcntl.flock(descriptor, fcntl.LOCK_UN)
    finally:
        os.close(descriptor)
        first.close()
        second.close()


@pytest.mark.parametrize("name", [
    "../outside.py", "/meso_uq/outside.py", "other/module.py",
    "meso_uq/../outside.py", "meso_uq//bad.py", "meso_uq\\bad.py",
    "meso_uq/__pycache__/bad.py", "meso_uq/bad.pyc",
])
def test_invalid_source_names_do_not_create_staging(source_case, name):
    source_case.files[name] = b""
    with pytest.raises(S.SourceStagingError):
        stage(source_case)
    assert not source_case.root.exists()


def test_package_initialization_and_immutable_bytes_are_required(source_case):
    source_case.files.pop("meso_uq/__init__.py")
    with pytest.raises(S.SourceStagingError):
        stage(source_case)
    source_case.files["meso_uq/__init__.py"] = bytearray()
    with pytest.raises(S.SourceStagingError):
        stage(source_case)


def test_a_source_file_cannot_shadow_a_package_directory(source_case):
    source_case.files["meso_uq/module.py/other.py"] = b""
    with pytest.raises(S.SourceStagingError, match="shadow"):
        stage(source_case)
    assert not source_case.root.exists()


def test_namespace_capacity_has_no_fallback_or_deletion(source_case):
    c = source_case
    first = stage(c, max_namespaces=1)
    c.qualification = "b"*64
    with pytest.raises(S.SourceStagingError, match="capacity exhausted"):
        stage(c, max_namespaces=1)
    assert first.verify()["source_files"] == 3
    assert not (c.root/c.qualification).exists()
    first.close()


def test_byte_limit_is_total_across_namespace_payloads_and_manifests(source_case):
    c = source_case
    expected = S._manifest(c.qualification, c.files, 1048576)
    one_size = len(S._canonical(expected))+sum(len(value) for value in c.files.values())
    first = stage(c, max_bytes=one_size+10)
    c.qualification = "b"*64
    with pytest.raises(S.SourceStagingError, match="capacity exhausted"):
        stage(c, max_bytes=one_size+10)
    assert first.verify()["logical_bytes"] == one_size
    first.close()


def test_existing_policy_and_qualification_cannot_change(source_case):
    c = source_case
    lease = stage(c)
    with pytest.raises(S.SourceStagingError, match="policy changed"):
        stage(c, max_bytes=2097152)
    c.files["meso_uq/module.py"] = b"answer = 43\n"
    with pytest.raises(S.SourceStagingError, match="manifest bytes changed"):
        stage(c)
    lease.close()


@pytest.mark.parametrize("mutation", ["bytes", "resource", "extra", "missing", "symlink"])
def test_full_byte_and_membership_verification(source_case, tmp_path, mutation):
    lease = stage(source_case)
    module = lease.source_root/"meso_uq/module.py"
    if mutation in ("bytes", "resource"):
        target = module if mutation == "bytes" else lease.source_root/"meso_uq/data/settings.json"
        target.chmod(0o600)
        target.write_bytes(b"answer = 43\n" if mutation == "bytes" else b'{"value":2}\n')
        target.chmod(0o400)
    else:
        parent = module.parent
        parent.chmod(0o700)
        if mutation == "extra":
            target = parent/"injected.py"
            target.write_bytes(b"")
            target.chmod(0o400)
        else:
            module.unlink()
            if mutation == "symlink":
                outside = tmp_path/"outside"
                outside.write_bytes(b"answer = 42\n")
                module.symlink_to(outside)
        parent.chmod(0o500)
    try:
        with pytest.raises(S.SourceStagingError):
            lease.verify()
    finally:
        lease.close()


def test_root_symlink_and_foreign_access_fail_closed(source_case, tmp_path):
    actual = tmp_path/"actual"
    actual.mkdir(mode=0o700)
    source_case.root.symlink_to(actual, target_is_directory=True)
    with pytest.raises(S.SourceStagingError):
        stage(source_case)
    source_case.root.unlink()
    source_case.root.mkdir(mode=0o770)
    source_case.root.chmod(0o770)
    with pytest.raises(S.SourceStagingError):
        stage(source_case)


def test_prepublication_failure_cleans_only_own_pending_and_allows_retry(
        source_case, monkeypatch):
    original_rename = S.os.rename
    def fail(*args):
        raise RuntimeError("injected prepublication failure")
    monkeypatch.setattr(S.os, "rename", fail)
    with pytest.raises(RuntimeError, match="prepublication"):
        stage(source_case)
    assert not (source_case.root/source_case.qualification).exists()
    assert not list(source_case.root.glob(".pending-*"))
    monkeypatch.setattr(S.os, "rename", original_rename)
    with stage(source_case) as lease:
        assert lease.verify()["source_files"] == 3


def test_crash_left_pending_requires_explicit_recovery_and_is_preserved(source_case):
    lease = stage(source_case)
    pending = source_case.root/(".pending-"+"b"*64+"-123-"+"c"*32)
    pending.mkdir(mode=0o700)
    marker = pending/"partial"
    marker.write_bytes(b"unpublished")
    with pytest.raises(S.SourceStagingError, match="explicit recovery"):
        stage(source_case)
    assert marker.read_bytes() == b"unpublished"
    assert lease.verify()["source_files"] == 3
    lease.close()


def test_concurrent_processes_publish_one_identical_namespace(source_case):
    code = """
import importlib.util,json,sys,time
from pathlib import Path
spec=importlib.util.spec_from_file_location("staging",sys.argv[1])
module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)
files={"meso_uq/__init__.py":b"","meso_uq/module.py":b"answer = 42\\n"}
lease=module.stage_sources(Path(sys.argv[2]),"a"*64,files,max_bytes=1048576,max_namespaces=3)
print(json.dumps(lease.verify()),flush=True)
time.sleep(.1);lease.close()
"""
    command = [sys.executable, "-I", "-B", "-c", code, S.__file__, str(source_case.root)]
    children = [subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
                for _ in range(2)]
    results = [child.communicate(timeout=15) for child in children]
    assert all(child.returncode == 0 for child in children)
    receipts = [json.loads(stdout) for stdout, _ in results]
    assert receipts[0] == receipts[1]
    namespaces = [path for path in source_case.root.iterdir()
                  if path.name == source_case.qualification]
    assert len(namespaces) == 1
    assert not list(source_case.root.glob(".pending-*"))
