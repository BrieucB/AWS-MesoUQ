"""Execute the production helper against a fake sbatch, never a real scheduler."""
from __future__ import annotations

import json
import os
from pathlib import Path
import shlex
import subprocess
import sys
from types import SimpleNamespace

import pytest

from aws_mesouq.connector.slurm import SUBMISSION_HELPER, Slurm
from aws_mesouq.connector.transport import Transport


@pytest.mark.parametrize('mode', ['local', 'ssh'])
def test_actual_helper_strips_submission_overrides_and_forces_one_job(tmp_path, monkeypatch, mode):
    capture = tmp_path / 'fake-submission.json'
    binary = tmp_path / 'bin/sbatch'
    binary.parent.mkdir()
    # This stub only writes its argv/environment. It never communicates with
    # Slurm or executes the supplied batch script.
    binary.write_text(f'#!{sys.executable}\nimport json,os,sys\nfrom pathlib import Path\n'
        + f'Path({str(capture)!r}).write_text(json.dumps({{"argv":sys.argv,"environment":dict(os.environ)}}))\n'
        + 'print("12345;unit-test")\n')
    binary.chmod(0o700)
    batch = tmp_path / 'literal;$(touch not-executed).sbatch'
    batch.write_text('#!/bin/bash\n#SBATCH --time=00:10:00\n#SBATCH --gres=gpu:1\nexit 99\n')
    hostile = {'SBATCH_ARRAY': '0-999', 'SBATCH_ARRAY_INX': '0-999', 'SBATCH_TIMELIMIT': '4-00:00:00',
        'SBATCH_GRES': 'gpu:8', 'SBATCH_NTASKS': '128', 'SBATCH_NODES': '20', 'SBATCH_EXPORT': 'ALL',
        'SLURM_NTASKS': '128', 'SLURM_NNODES': '20', 'SLURM_CLUSTERS': 'other', 'SLURM_EXPORT_ENV': 'ALL',
        'BASH_ENV': '/not/approved/shell-hook', 'ENV': '/not/approved/shell-hook',
        'LD_PRELOAD': '/not/approved/loader-hook.so', 'LD_LIBRARY_PATH': '/not/approved/libraries',
        'PYTHONPATH': '/not/approved/modules', 'PYTHONSTARTUP': '/not/approved/startup',
        'SPANK_OPTION_SECRET': 'must-not-be-exported', 'UNRELATED_CREDENTIAL': 'must-not-be-exported',
        'BASH_FUNC_sbatch%%': '() { exit 99; }'}
    for key, value in hostile.items():
        monkeypatch.setenv(key, value)
    monkeypatch.setenv('PATH', str(binary.parent) + os.pathsep + os.defpath)
    calls = []
    original_run = subprocess.run
    if mode == 'ssh':
        from aws_mesouq.connector import transport as module
        from aws_mesouq.connector.ssh_session import _client_command
        class Borrowed:
            def __init__(self, host, directory, *, borrow_only=False):
                assert host == "ownuser@fake-cluster" and borrow_only is True
                assert Path(directory) == tmp_path / "guard"
            def run_bytes(self, argv, *, data=None):
                command = _client_command(SimpleNamespace(socket=tmp_path / "unused.sock"),
                    {"hostname": "fake-cluster", "user": "ownuser", "port": 22}, argv=argv)
                calls.append(command)
                assert command[0] == "ssh"
                assert "-oBatchMode=yes" in command and "-oForwardAgent=no" in command
                assert "-oProxyCommand=/usr/bin/false" in command
                remote = shlex.split(command[-1])
                if remote[0] == "/usr/bin/python3.11":
                    remote[0] = sys.executable
                else:
                    assert remote == ["id", "-un"]
                result = original_run(remote, input=data, stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE, check=True, timeout=90)
                return result.stdout
            def close(self):
                pass
        monkeypatch.setattr(module, "SSHSession", Borrowed)
        monkeypatch.setenv("AWS_MESOUQ_SSH_STATE_DIR", str(tmp_path / "guard"))
        transport = Transport(host="ownuser@fake-cluster")
    else:
        transport = Transport(local=True)
    scheduler = Slurm(transport)
    assert scheduler.submit(str(batch)) == '12345'
    recorded = json.loads(capture.read_text())
    assert recorded['argv'] == [str(binary), '--parsable', '--nodes=1', '--ntasks=1', '--no-requeue', '--export=NIL', str(batch)]
    # The stub observes its Python process after startup, when CPython may add
    # LC_CTYPE and macOS may add exactly __CF_USER_TEXT_ENCODING. These are
    # observation exceptions; the production helper's allowlist stays unchanged.
    observed_allowed = {'PATH', 'HOME', 'USER', 'LOGNAME', 'LANG', 'LC_ALL', 'TZ', 'LC_CTYPE'}
    if sys.platform == 'darwin':
        observed_allowed.add('__CF_USER_TEXT_ENCODING')
    assert set(recorded['environment']) <= observed_allowed
    assert not any(key.startswith(('SBATCH_', 'SLURM_')) for key in recorded['environment'])
    assert not set(hostile) & set(recorded['environment'])
    assert recorded['environment']['PATH'] == str(binary.parent) + os.pathsep + os.defpath
    assert not (tmp_path / 'not-executed').exists()
    assert batch.read_text().endswith('exit 99\n')
    if mode == 'ssh':
        helper = shlex.split(calls[-1][-1])
        assert helper[1:4] == ['-I', '-c', SUBMISSION_HELPER]
        assert helper[-1] == str(batch)


def test_missing_sbatch_fails_closed_without_fallback(tmp_path, monkeypatch):
    monkeypatch.setenv('PATH', str(tmp_path))
    # Avoid identity's PATH lookup here; this test targets helper resolution.
    transport = Transport(local=True)
    scheduler = Slurm.__new__(Slurm)
    scheduler.transport = transport
    scheduler.user = 'ownuser'
    with pytest.raises(subprocess.CalledProcessError):
        scheduler.submit(str(tmp_path / 'never-submitted.sbatch'))
