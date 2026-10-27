"""Reject effective inherited ACL permissions before creating jobs or sbatch."""
from __future__ import annotations

import os
from pathlib import Path
import shutil
import stat
import subprocess
import sys

import pytest

from aws_mesouq.connector.transport import PRIVATE_ROOT_PROBE, Transport
from test_connector import session


def test_private_root_probe_creates_only_a_temporary_private_file(tmp_path):
    root = tmp_path / 'storage'
    root.mkdir(mode=0o700)
    sentinel = root / 'existing'
    sentinel.write_bytes(b'leave existing content alone')
    before = (sentinel.stat().st_ino, sentinel.stat().st_mode, sentinel.read_bytes(), root.stat().st_mode)
    Transport(local=True).private_root_probe(str(root))
    assert list(root.iterdir()) == [sentinel]
    assert (sentinel.stat().st_ino, sentinel.stat().st_mode, sentinel.read_bytes(), root.stat().st_mode) == before


@pytest.mark.parametrize('kind', ['symlink', 'public', 'readonly'])
def test_private_root_probe_rejects_unsafe_roots_without_entries(tmp_path, kind):
    root = tmp_path / 'storage'
    root.mkdir(mode=0o700)
    selected = root
    if kind == 'symlink':
        selected = tmp_path / 'alias'
        selected.symlink_to(root, target_is_directory=True)
    else:
        root.chmod(0o755 if kind == 'public' else 0o500)
    with pytest.raises(subprocess.CalledProcessError):
        Transport(local=True).private_root_probe(str(selected))
    assert list(root.iterdir()) == []


def test_effective_acl_mode_emulation_rejects_and_cleans_probe(tmp_path, monkeypatch):
    """Portable fallback: emulate ACL-created mode0660 despite child umask077."""
    root = tmp_path / 'storage'
    root.mkdir(mode=0o700)
    original_open = os.open
    observed = []
    def inherited_acl_open(path, flags, mode=0o777, *, dir_fd=None):
        fd = original_open(path, flags, mode, dir_fd=dir_fd)
        if flags & os.O_CREAT:
            observed.append((str(path), mode, stat.S_IMODE(os.fstat(fd).st_mode)))
            os.fchmod(fd, 0o660)
        return fd
    monkeypatch.setattr(os, 'open', inherited_acl_open)
    monkeypatch.setattr(sys, 'argv', ['private-root-probe', str(root)])
    with pytest.raises(ValueError, match='inherited default ACLs'):
        exec(PRIVATE_ROOT_PROBE, {})
    assert len(observed) == 1
    assert observed[0][0].startswith('.aws-mesouq-private-probe-')
    assert observed[0][1:] == (0o666, 0o600)
    assert root.stat().st_mode & 0o777 == 0o700
    assert list(root.iterdir()) == []


def test_real_default_acl_rejected_even_when_root_is_mode0700(tmp_path):
    if not shutil.which('setfacl') or not shutil.which('getfacl'):
        pytest.skip('ACL tools unavailable; portable effective-mode emulation runs separately')
    root = tmp_path / 'storage'
    root.mkdir(mode=0o700)
    configured = subprocess.run(['setfacl', '-m', 'd:u::rwx,d:g::rw-,d:o::---', str(root)], capture_output=True)
    if configured.returncode:
        pytest.skip('Temporary filesystem does not support default POSIX ACLs')
    assert root.stat().st_mode & 0o777 == 0o700
    original_acl = os.getxattr(root, 'system.posix_acl_default') if hasattr(os, 'getxattr') else None
    with pytest.raises(subprocess.CalledProcessError) as caught:
        Transport(local=True).private_root_probe(str(root))
    assert b'inherited default ACLs' in caught.value.stderr
    assert list(root.iterdir()) == []
    assert root.stat().st_mode & 0o777 == 0o700
    if original_acl is not None:
        assert os.getxattr(root, 'system.posix_acl_default') == original_acl


@pytest.mark.parametrize('site', ['karolina', 'vega'])
@pytest.mark.parametrize('failed_root', ['/private/runs', '/private/archive'])
def test_storage_preflight_failure_blocks_staging_and_sbatch(session, site, failed_root):
    session.connector.site = site
    checked = []
    def probe(root):
        checked.append(root)
        if root == failed_root:
            raise ValueError('Storage root creates nonprivate files despite umask077')
    session.transport.private_root_probe = probe
    with pytest.raises(ValueError, match='nonprivate'):
        session.connector.tick()
    assert failed_root in checked
    assert not session.transport.files
    assert not session.transport.commands
    assert session.slurm.submit_count == 0
    assert session.api.events[-1]['detail']['code'] == 'staging_failed'


def test_storage_policy_is_rechecked_after_staging_before_submission(session):
    checked = []
    def probe(root):
        checked.append(root)
        if len(checked) == 4:
            raise ValueError('archive root became permissive before submission')
    session.transport.private_root_probe = probe
    with pytest.raises(ValueError, match='before submission'):
        session.connector.tick()
    entry = session.journal.data['jobs']['job-1']
    assert checked == ['/private/runs', '/private/archive', '/private/runs', '/private/archive']
    assert entry['phase'] == 'staged'
    assert session.transport.files[entry['run_dir'] + '/submit.sh']
    assert session.slurm.submit_count == 0
    assert not any(event['type'] == 'submitting' for event in session.api.events)
