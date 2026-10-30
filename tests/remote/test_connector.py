from __future__ import annotations

from datetime import datetime, timedelta, timezone
import hashlib
import json
from pathlib import Path
import subprocess
import sys
from types import SimpleNamespace

import pytest

from aws_mesouq.connector.connector import Connector, LeaseExpired, StaleEvent
from aws_mesouq.connector.journal import Journal
from aws_mesouq.connector.slurm import SUBMISSION_HELPER, Slurm
from aws_mesouq.connector.ssh_session import SSHSessionError
from aws_mesouq.connector.transport import Transport, absolute_path, safe_id
from aws_mesouq.connector.api import API, NoRedirect


class FakeTransport:
    private_root_probe = Transport.private_root_probe
    def __init__(self):
        self.commands = []
        self.files = {}
        self.records = []

    def identity(self):
        return "ownuser"

    def run(self, argv, **kwargs):
        self.commands.append(argv)
        if argv[0] in {"sacct", "squeue"}:
            return "\n".join(self.records)
        return ""

    def write(self, path, data):
        assert path not in self.files
        self.files[path] = data

    def read(self, path):
        return self.files[path]

    def archive_prepare(self, root, token):
        self.commands.append(['archive_prepare', root, token])

    def archive_verify(self, root, token, item):
        blob = self.files[root + '/' + token + '/' + item['destination']]
        assert hashlib.sha256(blob).hexdigest() == item['sha256']
        assert len(blob) == item['size_bytes']

    def archive_copy(self, root, token, item):
        path = root + '/' + token + '/' + item['destination']
        if path not in self.files:
            blob = self.files[item['source_root'] + '/' + item['source']]
            assert hashlib.sha256(blob).hexdigest() == item['sha256']
            assert len(blob) == item['size_bytes']
            self.files[path] = blob
        self.archive_verify(root, token, item)

    def check_files(self, root, names):
        for name in names:
            if root + '/' + name not in self.files:
                raise FileNotFoundError(name)

    def read_beneath(self, root, name):
        return self.read(root + '/' + name)


class FakeAPI:
    def __init__(self, job):
        self.job = job
        self.events = []
        self.fail_once = False

    def request(self, method, path, payload=None):
        if path == "/v1/connector/claim":
            return {"job": self.job, "lease_token": "secret-lease", "lease_expires_at": (datetime.now(timezone.utc) + timedelta(minutes=5)).isoformat()}
        if path == "/v1/me":
            return {"subject": "owner"}
        if path == "/v1/projects/project-1/jobs/job-1":
            return self.job
        if path.endswith("/events"):
            self.events.append(payload)
            if self.fail_once:
                self.fail_once = False
                raise OSError("lost acknowledgement")
            if payload["type"] == "submitted":
                self.job["status"] = "queued"
            elif payload["type"] in {"running", "succeeded", "failed", "cancelled", "submission_unknown", "submitting"}:
                self.job["status"] = payload["type"]
            return {"status": self.job["status"]}
        raise AssertionError(path)


@pytest.fixture
def session(tmp_path):
    job = {"id": "job-1", "project_id": "project-1", "owner_sub": "owner", "kind": "prediction",
           "status": "claimed", "model_version": "model-1", "diameter_um": 3.2,
           "approved_model": {"version": "model-1", "approved_adapter_sha256": hashlib.sha256((Path(__file__).resolve().parents[2] / "src/aws_mesouq/adapters/indentation.py").read_bytes()).hexdigest(), "sha256": {"3.2": "a" * 64}}, "input_artifacts": [], "configuration": {}}
    api = FakeAPI(job)
    transport = FakeTransport()
    slurm = Slurm(transport)
    slurm.submit_count = 0

    def submit(path):
        slurm.submit_count += 1
        return "123"

    slurm.submit = submit
    journal = Journal(tmp_path / "journal")
    connector = Connector(api, slurm, journal, project="project-1", site="karolina", archive_root="/private/archive", durable_archive_root=True, run_root="/private/runs", repository="/trusted/repo", model_registry={"model-1": {"version": "model-1", "trained_dirs": {}, "sha256": {"3.2": "a" * 64}}})
    yield SimpleNamespace(connector=connector, api=api, slurm=slurm, transport=transport, journal=journal, job=job)
    journal.close()


def test_submission_manifest_and_no_duplicate_on_reconnect(session):
    s = session
    s.connector.tick()
    entry = s.journal.data["jobs"]["job-1"]
    manifest = json.loads(s.transport.files[entry["run_dir"] + "/manifest.json"])
    assert manifest["repository_root"] == "/trusted/repo"
    assert manifest["model"]["version"] == "model-1"
    script = s.transport.files[entry["run_dir"] + "/submit.sh"].decode()
    assert "/usr/bin/python3.11 " + entry["run_dir"] + "/aws-mesouq-adapter.py" in script
    assert "MESOUQ_SITE=karolina" in script
    s.transport.records = [f'123|{entry["marker"]}|ownuser|RUNNING']
    s.connector.tick()
    assert s.slurm.submit_count == 1
    assert s.api.events[-1]["type"] == "running"
    assert "secret-lease" not in s.journal.path.read_text()


def test_uncertain_submission_never_retries_even_if_scheduler_empty(session):
    s = session
    def uncertain(path):
        s.slurm.submit_count += 1
        raise subprocess.TimeoutExpired("sbatch", 90)
    s.slurm.submit = uncertain
    s.connector.tick()
    s.connector.tick()
    assert s.slurm.submit_count == 1
    assert s.api.events[-1]["type"] == "heartbeat"


def test_event_replay_reuses_id_after_disconnect(session):
    s = session
    s.connector.tick()
    entry = s.journal.data["jobs"]["job-1"]
    s.transport.records = [f'123|{entry["marker"]}|ownuser|RUNNING']
    s.api.fail_once = True
    with pytest.raises(OSError):
        s.connector.tick()
    event_id = s.api.events[-1]["event_id"]
    s.connector.tick()
    assert s.api.events[-2]["event_id"] == event_id
    assert s.slurm.submit_count == 1


def test_expired_lease_has_no_scheduler_mutation(session):
    s = session
    original = s.api.request
    def request(method, path, payload=None):
        response = original(method, path, payload)
        if path == "/v1/connector/claim":
            response["lease_expires_at"] = "2000-01-01T00:00:00Z"
        return response
    s.api.request = request
    with pytest.raises(LeaseExpired):
        s.connector.tick()
    assert not s.transport.commands
    assert s.slurm.submit_count == 0


def test_owner_mismatch_rejected_before_staging(session):
    session.job["owner_sub"] = "someone-else"
    with pytest.raises(ValueError, match="owner"):
        session.connector.tick()
    assert not session.transport.commands


def test_cancellation_only_verifies_recorded_own_job(session):
    s = session
    s.connector.tick()
    entry = s.journal.data["jobs"]["job-1"]
    s.job["status"] = "cancelling"
    s.transport.records = [f'123|{entry["marker"]}|someone-else|RUNNING']
    s.connector.tick()
    assert not any(command[0] == "scancel" for command in s.transport.commands)
    s.transport.records = [f'123|{entry["marker"]}|ownuser|RUNNING']
    s.connector.tick()
    assert ["scancel", "--user", "ownuser", "123"] in s.transport.commands


def test_cancel_before_submission(session):
    session.journal.data["jobs"]["job-1"] = {"marker": "mesouq-own", "run_dir": "/private/runs/own", "phase": "staged"}
    session.job["status"] = "cancelling"
    session.connector.tick()
    assert session.slurm.submit_count == 0
    assert session.api.events[-1]["type"] == "cancelled"


@pytest.mark.parametrize("value", ["../escape", "hello;id", "$(id)", "a\nb", "", "a/b"])
def test_untrusted_identifier_rejected(value):
    with pytest.raises(ValueError):
        safe_id(value)


def test_transport_quotes_ssh_argv(tmp_path, monkeypatch):
    from aws_mesouq.connector import transport as module
    from aws_mesouq.connector.ssh_session import _client_command
    calls, closed = [], []
    class Borrowed:
        def __init__(self, host, directory, *, borrow_only=False):
            assert host == "ownuser@cluster" and borrow_only is True
            assert Path(directory) == tmp_path / "guard"
        def run_bytes(self, argv, *, data=None):
            command = _client_command(SimpleNamespace(socket=tmp_path / "unused.sock"),
                {"hostname": "fixture.invalid", "user": "ownuser", "port": 22}, argv=argv)
            calls.append((command, data))
            return b""
        def close(self):
            closed.append(True)
    monkeypatch.setattr(module, "SSHSession", Borrowed)
    monkeypatch.setenv("AWS_MESOUQ_SSH_STATE_DIR", str(tmp_path / "guard"))
    transport = Transport(host="ownuser@cluster")
    try:
        transport.run(["cat", "/path/$(touch stolen);x"])
    finally:
        transport.close()
    command, data = calls[0]
    assert command[-1] == "cat '/path/$(touch stolen);x'"
    assert "-oBatchMode=yes" in command and "-oForwardAgent=no" in command
    assert "-oProxyCommand=/usr/bin/false" in command
    assert command[command.index("-F")+1] == "/dev/null"
    assert data is None and closed == [True]


@pytest.mark.parametrize("value", ["relative", "/a/../b", "/a\nb"])
def test_traversal_rejected(value):
    with pytest.raises(ValueError):
        absolute_path(value)


def test_journal_lock_and_permissions(tmp_path):
    j = Journal(tmp_path / "private")
    try:
        with pytest.raises(BlockingIOError):
            Journal(tmp_path / "private")
        assert (tmp_path / "private").stat().st_mode & 0o077 == 0
    finally:
        j.close()


def test_archive_cannot_claim_success_without_scientific_result(session):
    s = session
    s.connector.tick()
    entry = s.journal.data["jobs"]["job-1"]
    s.transport.records = [f'123|{entry["marker"]}|ownuser|COMPLETED']
    with pytest.raises(KeyError):
        s.connector.tick()
    assert not any(event["type"] == "succeeded" for event in s.api.events)


def test_vega_interface(session):
    session.connector.site = "vega"
    session.connector.tick()
    script = next(data for name, data in session.transport.files.items() if name.endswith("submit.sh"))
    assert b"MESOUQ_SITE=vega" in script


def test_api_requires_tls_and_rejects_redirects():
    with pytest.raises(ValueError):
        API("http://cloud", "token")
    with pytest.raises(ValueError):
        NoRedirect().redirect_request(None, None, 302, "", {}, "https://attacker")


def test_cancelling_without_journal_cannot_assert_no_job(session):
    session.job["status"] = "cancelling"
    session.connector.tick()
    assert session.api.events[-1]["type"] == "heartbeat"
    assert session.slurm.submit_count == 0


def test_cloud_uncertain_with_staged_journal_never_submits(session):
    session.journal.data["jobs"]["job-1"] = {"marker": "mesouq-own", "run_dir": "/private/runs/own", "phase": "staged"}
    session.job["status"] = "submission_unknown"
    session.connector.tick()
    assert session.slurm.submit_count == 0
    assert session.api.events[-1]["type"] == "heartbeat"


def test_multiple_scheduler_matches_quarantined(session):
    session.connector.tick()
    entry = session.journal.data["jobs"]["job-1"]
    session.transport.records = [f'{jid}|{entry["marker"]}|ownuser|RUNNING' for jid in ["123", "124"]]
    session.connector.tick()
    assert session.api.events[-1]["type"] == "submission_unknown"


def test_corrupt_input_not_submitted(session):
    artifact = {"id": "a1", "name": "../bad.csv", "kind": "curve", "sha256": "0" * 64, "size_bytes": 3}
    session.job["input_artifacts"] = [artifact]
    original = session.api.request
    def request(method, path, payload=None):
        if path.endswith("/artifacts/a1"):
            return {"download_url": "https://object.example/a1"}
        return original(method, path, payload)
    session.api.request = request
    session.api.transfer = lambda url: b"bad"
    with pytest.raises(ValueError, match="checksum"):
        session.connector.tick()
    assert session.slurm.submit_count == 0
    assert session.api.events[-1]["type"] == "failed"


@pytest.mark.parametrize("durable", [True, False])
def test_result_uploaded_then_verified_archive(session, durable):
    s = session
    s.connector.durable_archive_root = durable
    secret_environment = b'export SECRET_RUNTIME_TOKEN=do-not-upload\n'
    operator_script = '/home/private-operator/runtime/env.sh'
    s.transport.files[operator_script] = secret_environment
    s.connector.runtime = {'python_bin': '/home/private-operator/venv/bin/python3.10',
                           'environment_script': operator_script,
                           'environment_sha256': hashlib.sha256(secret_environment).hexdigest()}
    s.connector.tick()
    entry = s.journal.data["jobs"]["job-1"]
    run = entry["run_dir"]
    s.transport.files[run + "/output/result.json"] = json.dumps({"schema_version": 1, "kind": "prediction", "provenance": {"job_id": s.job["id"], "model_version": s.job["model_version"],
        "code_sha256": hashlib.sha256(b"trusted fixture source").hexdigest(),
        "config_sha256": hashlib.sha256(json.dumps(s.job["configuration"], sort_keys=True, separators=(",", ":")).encode()).hexdigest(),
        "model_sha256": s.job["approved_model"]["sha256"], "data_sha256": {},
        "runtime": {"python": "3.11.2", "site": "karolina"}},
        "publication_files": ["result.json", "provenance.json"],
        "interval_probability": 0.95, "parameters": [], "diagnostics": {},
        "resonance": {"status": "unavailable", "reason": "test fixture"},
        "curves": [{"dataset_id": "curve", "force": [1], "observed_displacement": [2], "median": [2],
                    "lower": [1], "upper": [3], "force_unit": "N", "displacement_unit": "m"}]}).encode()
    s.transport.files[run + "/output/provenance.json"] = json.dumps(json.loads(s.transport.files[run + "/output/result.json"])["provenance"]).encode()
    s.transport.records = [f'123|{entry["marker"]}|ownuser|COMPLETED']
    # A transient generation could be gigabytes; it must never be opened,
    # hashed, enumerated, or uploaded merely because it exists under output.
    unlisted = run + "/output/inference/generation-050000.json"
    s.transport.files[unlisted] = object()
    original_read = s.transport.read
    reads = []
    def read(path):
        reads.append(path)
        assert path != unlisted
        return original_read(path)
    s.transport.read = read
    original_request = s.api.request
    uploads = []
    completed = []
    def request(method, path, payload=None):
        if path.endswith("/artifacts"):
            artifact = {**payload, "id": "artifact-" + str(len(uploads))}
            uploads.append(artifact)
            return {"artifact": artifact, "upload_url": "https://object.example/upload"}
        if path.endswith("/complete"):
            completed.append(path)
            return {}
        return original_request(method, path, payload)
    s.api.request = request
    transferred = []
    def transfer(url, *, data, headers):
        transferred.append(data)
        return b''
    s.api.transfer = transfer
    s.connector.tick()
    assert unlisted not in reads
    assert not any("rglob" in str(command) for command in s.transport.commands)
    assert len(uploads) == len(completed) == 2
    if durable:
        assert s.api.events[-2]["type"] == "succeeded"
        assert s.api.events[-1]["type"] == "archive_verified"
        assert len(s.api.events[-1]["archive"]["artifacts"]) == 2
    else:
        assert s.api.events[-1]["type"] == "succeeded"
        assert not any(event["type"] == "archive_verified" for event in s.api.events)
    assert s.transport.files[run + '/runtime-env.sh'] == secret_environment
    assert operator_script.encode() in s.transport.files[run + '/manifest.json']
    assert all(b'SECRET_RUNTIME_TOKEN' not in blob and b'/home/private-operator' not in blob for blob in transferred)
    assert all(s.journal.data['archive_device']['private_key'].encode() not in blob for blob in transferred)
    assert all(item['name'] not in {'manifest.json', 'runtime-env.sh'} for item in uploads)
    assert (run + '/runtime-env.sh' in reads) is durable
    assert (run + '/manifest.json' in reads) is durable
    if durable:
        archive_path = entry['archive_receipt']['path']
        assert s.transport.files[archive_path + '/execution/runtime-env.sh'] == secret_environment
        assert s.transport.files[archive_path + '/execution/manifest.json'] == s.transport.files[run + '/manifest.json']
    assert entry["terminal"]


def test_real_local_transport_binary_roundtrip_and_noclobber(tmp_path):
    transport = Transport(local=True)
    path = str(tmp_path / "literal;$(touch injected)")
    transport.write(path, b"\x00\xffpayload\n")
    assert transport.read(path) == b"\x00\xffpayload\n"
    with pytest.raises(subprocess.CalledProcessError):
        transport.write(path, b"overwrite")
    target = tmp_path / "link"
    target.symlink_to(path)
    with pytest.raises(subprocess.CalledProcessError):
        transport.read(str(target))


def test_numeric_epoch_lease_and_heartbeat_poll(session):
    original = session.api.request
    def request(method, path, payload=None):
        response = original(method, path, payload)
        if path == "/v1/connector/claim":
            response["lease_expires_at"] = (datetime.now(timezone.utc) + timedelta(minutes=5)).timestamp()
        return response
    session.api.request = request
    session.connector.tick()
    entry = session.journal.data["jobs"]["job-1"]
    session.transport.records = [f'123|{entry["marker"]}|ownuser|PENDING']
    session.connector.tick()
    assert session.api.events[-1]["type"] == "heartbeat"


def test_recovered_uncertain_job_records_id_before_running(session):
    session.connector.tick()
    entry = session.journal.data["jobs"]["job-1"]
    session.job["status"] = "submission_unknown"
    session.transport.records = [f'123|{entry["marker"]}|ownuser|RUNNING']
    session.connector.tick()
    assert [e["type"] for e in session.api.events[-2:]] == ["submitted", "running"]


def test_second_cloud_identity_cannot_reuse_journal(session):
    session.connector.tick()
    original = session.api.request
    def request(method, path, payload=None):
        if path == "/v1/me":
            return {"subject": "other"}
        return original(method, path, payload)
    session.api.request = request
    with pytest.raises(ValueError, match="another cloud identity"):
        session.connector.tick()


def test_cancelled_scheduler_reconciles_without_repeated_scancel(session):
    session.connector.tick()
    entry = session.journal.data["jobs"]["job-1"]
    session.job["status"] = "cancelling"
    session.transport.records = [f'123|{entry["marker"]}|ownuser|CANCELLED']
    session.connector.tick()
    assert session.api.events[-1]["type"] == "cancelled"
    assert not any(command[0] == "scancel" for command in session.transport.commands)


def test_connector_import_does_not_load_torch():
    result = subprocess.run([sys.executable, "-c", "import aws_mesouq.connector.connector,sys; assert 'torch' not in sys.modules"], check=True, capture_output=True)
    assert result.returncode == 0


def test_lease_rechecked_after_slow_cancellation_identity_query(session):
    session.connector.tick()
    entry = session.journal.data["jobs"]["job-1"]
    session.job["status"] = "cancelling"
    session.transport.records = [f'123|{entry["marker"]}|ownuser|RUNNING']
    original = session.slurm.lookup
    calls = []
    def lookup(marker):
        calls.append(marker)
        if len(calls) == 2:
            session.connector.lease["lease_expires_at"] = 0
        return original(marker)
    session.slurm.lookup = lookup
    with pytest.raises(LeaseExpired):
        session.connector.tick()
    assert not any(command[0] == "scancel" for command in session.transport.commands)


@pytest.mark.parametrize('mutation', ['hash', 'version', 'missing', 'grouped-missing', 'scalar-diameter'])
def test_cloud_model_descriptor_must_match_trusted_installation(session, mutation):
    s = session
    approved = s.job['approved_model']
    if mutation == 'hash':
        approved['sha256']['3.2'] = 'b' * 64
    elif mutation == 'version':
        approved['version'] = 'other'
    elif mutation == 'missing':
        s.job.pop('approved_model')
    elif mutation == 'grouped-missing':
        s.job['configuration']['emb_diameters'] = [3.2, 3.4]
    else:
        approved['sha256'] = 'a' * 64
        approved['diameter_um'] = 3.4
    with pytest.raises(ValueError, match='[Aa]pproved'):
        s.connector.tick()
    assert s.slurm.submit_count == 0
    assert not s.transport.files
    assert s.api.events[-1]['detail']['code'] == 'model_unavailable'


def test_runtime_profile_snapshot_and_verified_interpreter(session):
    s = session
    code = b'export APPROVED_ENV=1\n'
    s.transport.files['/trusted/site env.sh'] = code
    s.connector.runtime = {'python_bin': '/trusted/venv/bin/python3.10', 'environment_script': '/trusted/site env.sh',
                           'environment_sha256': hashlib.sha256(code).hexdigest(), 'site_runtime_root': '/trusted/runtime'}
    s.connector.tick()
    entry = s.journal.data['jobs']['job-1']
    run = entry['run_dir']
    assert s.transport.files[run + '/runtime-env.sh'] == code
    manifest = json.loads(s.transport.files[run + '/manifest.json'])
    assert manifest['runtime']['environment_sha256'] == hashlib.sha256(code).hexdigest()
    script = s.transport.files[run + '/submit.sh'].decode()
    assert script.startswith('#!/bin/bash')
    assert 'source ' + run + '/runtime-env.sh' in script
    assert script.index('export MESOUQ_SITE=karolina') < script.index('source ')
    assert script.index('export MESOUQ_SITE_RUNTIME_ROOT=/trusted/runtime') < script.index('source ')
    assert '/trusted/venv/bin/python3.10 ' + run + '/aws-mesouq-adapter.py' in script
    assert '${PYTHONPATH:+' in script


def test_modified_trusted_environment_fails_before_submission(session):
    s = session
    s.transport.files['/trusted/env.sh'] = b'changed'
    s.connector.runtime = {'python_bin': '/trusted/python3.10', 'environment_script': '/trusted/env.sh', 'environment_sha256': 'a' * 64}
    with pytest.raises(ValueError, match='checksum'):
        s.connector.tick()
    assert s.slurm.submit_count == 0


def test_trusted_runtime_rejects_relative_and_traversal_python(session):
    for value in ['relative', '/bad/../python', '/bad\npython']:
        with pytest.raises(ValueError):
            Connector(session.api, session.slurm, session.journal, project='project-1', site='karolina',
                run_root='/private/runs', repository='/trusted/repo', model_registry={}, runtime={'python_bin': value})


@pytest.fixture
def publication_session(session):
    s = session
    s.connector.tick()
    entry = s.journal.data['jobs']['job-1']
    root = entry['run_dir'] + '/output'
    result = {'schema_version': 1, 'kind': 'prediction', 'provenance': {
        'job_id': s.job['id'], 'model_version': s.job['model_version'],
        'code_sha256': hashlib.sha256(b'fixture source').hexdigest(),
        'config_sha256': hashlib.sha256(b'{}').hexdigest(),
        'model_sha256': s.job['approved_model']['sha256'], 'data_sha256': {},
        'runtime': {'python': '3.11.2'}}, 'interval_probability': .95, 'parameters': [],
        'diagnostics': {}, 'resonance': {'status': 'unavailable', 'reason': 'fixture'},
        'curves': [{'dataset_id': 'curve', 'force': [1], 'observed_displacement': [2],
                    'median': [2], 'lower': [1], 'upper': [3], 'force_unit': 'N', 'displacement_unit': 'm'}],
        'publication_files': ['result.json', 'provenance.json']}
    s.transport.files[root + '/provenance.json'] = json.dumps(result['provenance']).encode()
    s.transport.records = [f"123|{entry['marker']}|ownuser|COMPLETED"]
    return s, root, result


@pytest.mark.parametrize('names', [None, [], ['result.json'], ['result.json', 'provenance.json', '../escape'],
    ['result.json', 'provenance.json', '/absolute'], ['result.json', 'provenance.json', 'native//x'],
    ['result.json', 'provenance.json', 'native/../x'], ['result.json', 'provenance.json', 'result.json'],
    ['result.json', 'provenance.json', 'manifest.json'], ['result.json', 'provenance.json', 'runtime-env.sh'],
    ['result.json', 'provenance.json', 'nested/runtime-env.sh'], ['result.json', 'provenance.json', 'nested/manifest.json'],
    ['result.json', 'provenance.json', 'copied-environment.sh']])
def test_unsafe_or_missing_publication_list_fails_closed(publication_session, names):
    s, root, result = publication_session
    if names is None:
        result.pop('publication_files')
    else:
        result['publication_files'] = names
    s.transport.files[root + '/result.json'] = json.dumps(result).encode()
    with pytest.raises(ValueError):
        s.connector.tick()
    assert not any(event['type'] == 'succeeded' for event in s.api.events)


def test_required_listed_publication_missing_fails_before_upload(publication_session):
    s, root, result = publication_session
    result['publication_files'].append('models/3.2um/trained/checkpoint.pkl')
    s.transport.files[root + '/result.json'] = json.dumps(result).encode()
    with pytest.raises(FileNotFoundError):
        s.connector.tick()
    assert not any(event['type'] == 'succeeded' for event in s.api.events)
    assert not s.journal.data['jobs']['job-1'].get('uploaded')


def test_real_beneath_reader_rejects_intermediate_symlinks_and_traversal(tmp_path):
    output = tmp_path / 'output'
    output.mkdir()
    nested = output / 'nested'
    nested.mkdir()
    (nested / 'valid.csv').write_bytes(b'final posterior')
    outside = tmp_path / 'outside'
    outside.mkdir()
    (outside / 'secret').write_bytes(b'private')
    (output / 'link').symlink_to(outside, target_is_directory=True)
    transport = Transport(local=True)
    transport.check_files(str(output), ['nested/valid.csv'])
    assert transport.read_beneath(str(output), 'nested/valid.csv') == b'final posterior'
    with pytest.raises(subprocess.CalledProcessError):
        transport.check_files(str(output), ['link/secret'])
    with pytest.raises(subprocess.CalledProcessError):
        transport.read_beneath(str(output), 'link/secret')
    with pytest.raises(ValueError):
        transport.read_beneath(str(output), '../outside/secret')
    with pytest.raises(subprocess.CalledProcessError):
        transport.check_files(str(output), ['nested'])


@pytest.mark.parametrize('private_runtime', [
    {'python': '3.11.2', 'profile': {'environment_script': '/home/private-operator/env.sh'}},
    {'python': '3.11.2', 'korali_file': '/home/private-operator/korali/__init__.py'},
    {'python': '3.11.2', 'executable': '/home/private-operator/bin/python'},
    {'python': '3.11.2', 'profile': {'environment_sha256': 'not-a-hash'}},
])
def test_unsanitized_runtime_provenance_cannot_be_published(publication_session, private_runtime):
    s, root, result = publication_session
    result['provenance']['runtime'] = private_runtime
    s.transport.files[root + '/result.json'] = json.dumps(result).encode()
    with pytest.raises(ValueError, match='runtime'):
        s.connector.tick()
    assert not s.journal.data['jobs']['job-1'].get('uploaded')
    assert not any(event['type'] == 'succeeded' for event in s.api.events)


def test_standalone_provenance_cannot_hide_operator_secret(publication_session):
    s, root, result = publication_session
    s.transport.files[root + '/result.json'] = json.dumps(result).encode()
    private = {**result['provenance'], 'runtime': {'python': '3.11.2', 'secret_token': 'never-publish'}}
    s.transport.files[root + '/provenance.json'] = json.dumps(private).encode()
    with pytest.raises(ValueError, match='provenance file differs'):
        s.connector.tick()
    assert not s.journal.data['jobs']['job-1'].get('uploaded')


@pytest.mark.parametrize('allocation', ['', '32', '16(x2)'])
def test_actual_allocation_guard_runs_before_environment_or_science(session, allocation):
    session.connector.tick()
    entry = session.journal.data['jobs']['job-1']
    script = session.transport.files[entry['run_dir'] + '/submit.sh'].decode()
    result = subprocess.run(['/bin/bash'], input=script, text=True, capture_output=True,
                            env={'SLURM_JOB_CPUS_PER_NODE': allocation})
    assert result.returncode == 2
    assert 'allocation' in result.stdout.lower()
    assert not result.stderr


def test_archive_configuration_is_bound_to_private_journal(session):
    args = dict(project='project-1', site='karolina', run_root='/private/runs',
                repository='/trusted/repo', model_registry={})
    for override in ({}, {'archive_root': '/different/archive', 'durable_archive_root': True},
                     {'archive_root': '/private/archive', 'durable_archive_root': False}):
        with pytest.raises(ValueError, match='durability policy'):
            Connector(session.api, session.slurm, session.journal, **args, **override)


def test_confirmed_durable_connector_resumes_missing_archive_for_local_terminal_job(session):
    session.connector.tick()
    entry = session.journal.data['jobs']['job-1']
    entry['terminal'] = True
    entry['succeeded_sent'] = True
    session.job['status'] = 'succeeded'
    invoked = []
    session.connector.publish = lambda job, saved: invoked.append((job['id'], saved))
    session.connector.tick()
    assert invoked == [('job-1', entry)]
    assert session.slurm.submit_count == 1


def test_sweep_fails_closed_if_server_ignores_exclusions(session):
    with pytest.raises(RuntimeError, match='ignored job exclusions'):
        session.connector.sweep()
    assert session.slurm.submit_count == 1
    assert session.connector.lease is None


def test_excluded_job_cannot_be_processed_even_with_new_lease(session):
    with pytest.raises(RuntimeError, match='ignored job exclusions'):
        session.connector.tick(exclude_job_ids=['job-1'])
    assert session.slurm.submit_count == 0
    assert not session.transport.commands
    assert not session.api.events


@pytest.mark.parametrize('excluded', [['job-1', 'job-1'], ['../escape'], [f'job-{i}' for i in range(11)]])
def test_invalid_exclusion_set_rejected_before_any_mutation(session, excluded):
    with pytest.raises(ValueError):
        session.connector.tick(exclude_job_ids=excluded)
    assert not session.transport.commands
    assert not session.api.events


def test_sweep_is_bounded_to_ten_distinct_claims(session):
    import copy
    original = session.api.request
    source = copy.deepcopy(session.job)
    claims = []
    def request(method, path, payload=None):
        if path == '/v1/connector/claim':
            claims.append(list(payload['exclude_job_ids']))
            session.api.job = {**copy.deepcopy(source), 'id': f'job-{len(claims)}'}
        return original(method, path, payload)
    session.api.request = request
    assert session.connector.sweep() == [f'job-{i}' for i in range(1, 11)]
    assert [len(values) for values in claims] == list(range(10))
    assert session.slurm.submit_count == 10


def test_slow_operation_renews_near_expiry_and_uses_returned_expiry(session):
    session.connector.tick()
    session.connector.lease['lease_expires_at'] = (datetime.now(timezone.utc) + timedelta(seconds=60)).timestamp()
    original = session.api.request
    expiry = (datetime.now(timezone.utc) + timedelta(seconds=120)).timestamp()
    def request(method, path, payload=None):
        response = original(method, path, payload)
        if payload and payload.get('type') == 'heartbeat':
            response['lease_expires_at'] = expiry
        return response
    session.api.request = request
    performed = []
    def operation():
        assert session.api.events[-1]['type'] == 'heartbeat'
        assert session.connector.lease['lease_expires_at'] == expiry
        performed.append(True)
        return 'verified'
    assert session.connector.slow('job-1', operation) == 'verified'
    assert performed == [True]


def test_slow_operation_cannot_renew_around_pending_uncertain_event(session):
    session.connector.tick()
    entry = session.journal.data['jobs']['job-1']
    entry['pending_event'] = {'type': 'submitted', 'event_id': 'uncertain'}
    performed = []
    with pytest.raises(RuntimeError, match='Pending event'):
        session.connector.slow('job-1', lambda: performed.append(True))
    assert not performed
    assert entry['pending_event']['event_id'] == 'uncertain'


def test_slow_operation_rejects_expired_or_unextended_lease(session):
    session.connector.tick()
    performed = []
    session.connector.lease['lease_expires_at'] = 0
    with pytest.raises(LeaseExpired):
        session.connector.slow('job-1', lambda: performed.append(True))
    session.connector.lease['lease_expires_at'] = (datetime.now(timezone.utc) + timedelta(seconds=60)).timestamp()
    with pytest.raises(LeaseExpired, match='enough lease time'):
        session.connector.slow('job-1', lambda: performed.append(True))
    assert not performed


def test_slow_operation_cannot_use_another_jobs_lease(session):
    session.connector.tick()
    with pytest.raises(LeaseExpired, match='another job'):
        session.connector.slow('job-other', lambda: pytest.fail('wrong-job operation'))
    with pytest.raises(LeaseExpired, match='another job'):
        session.connector.event('job-other', 'heartbeat')


def test_expiry_during_slow_operation_stops_followup(session):
    session.connector.tick()
    def slow_read():
        session.connector.lease['lease_expires_at'] = 0
        return b'data'
    with pytest.raises(LeaseExpired):
        session.connector.slow('job-1', slow_read)


def test_different_event_cannot_consume_pending_acknowledgment(session):
    session.connector.tick()
    entry = session.journal.data['jobs']['job-1']
    entry['pending_event'] = {'type': 'heartbeat', 'event_id': 'uncertain-heartbeat'}
    before = len(session.api.events)
    with pytest.raises(RuntimeError, match='different event type'):
        session.connector.event('job-1', 'failed', detail={'reason': 'unrelated failure'})
    assert len(session.api.events) == before
    assert entry['pending_event']['event_id'] == 'uncertain-heartbeat'
    assert not entry.get('terminal')


@pytest.mark.parametrize('notification_failure', [None, OSError, StaleEvent])
def test_sweep_stops_on_fatal_ssh_submission_even_when_unknown_notification_fails(session, monkeypatch, notification_failure):
    s = session
    fatal = SSHSessionError('connection_lost')
    failed, submissions, after_failure, claims, unknown = [False], [], [], [], []
    original_request = s.api.request
    def request(method, path, payload=None):
        if path == '/v1/connector/claim':
            claims.append(list(payload.get('exclude_job_ids', [])))
        if payload and payload.get('type') == 'submission_unknown':
            unknown.append(dict(payload))
            if notification_failure is not None:
                raise notification_failure('Unknown submission notification unavailable')
        return original_request(method, path, payload)
    monkeypatch.setattr(s.api, 'request', request)
    monkeypatch.setattr(s.slurm, 'submit', Slurm.submit.__get__(s.slurm, Slurm))
    for name in ('identity', 'run', 'write', 'read', 'private_root_probe',
                 'archive_prepare', 'archive_copy', 'archive_verify', 'check_files', 'read_beneath'):
        original = getattr(s.transport, name)
        def operation(*args, _name=name, _original=original, **kwargs):
            if failed[0]:
                after_failure.append(_name)
            if _name == 'run' and len(args[0]) >= 5 and args[0][3] == SUBMISSION_HELPER:
                submissions.append(list(args[0]))
                failed[0] = True
                raise fatal
            return _original(*args, **kwargs)
        monkeypatch.setattr(s.transport, name, operation)
    with pytest.raises(SSHSessionError) as error:
        s.connector.sweep()
    assert error.value is fatal
    assert claims == [[]]
    assert len(submissions) == 1 and after_failure == []
    assert len(unknown) == 1
    persisted = json.loads(s.journal.path.read_text())['jobs']['job-1']
    assert persisted['phase'] == 'submission_attempted'
    assert 'slurm_job_id' not in persisted and not persisted.get('terminal')
    if notification_failure is not None:
        assert persisted['pending_event']['type'] == 'submission_unknown'
        assert persisted['pending_event']['event_id'] == unknown[0]['event_id']
    else:
        assert 'pending_event' not in persisted
        assert s.job['status'] == 'submission_unknown'


def test_sweep_preserves_fatal_staging_ssh_error_without_cloud_failure_transition(session, monkeypatch):
    s = session
    fatal = SSHSessionError('connection_lost')
    claims, remote_calls, failed_notifications = [], [], []
    original_request = s.api.request
    def request(method, path, payload=None):
        if path == '/v1/connector/claim':
            claims.append(list(payload.get('exclude_job_ids', [])))
        if payload and payload.get('type') == 'failed':
            failed_notifications.append(dict(payload))
            raise StaleEvent('Cloud state changed while reporting staging failure')
        return original_request(method, path, payload)
    def fail_remote(argv, **kwargs):
        remote_calls.append(list(argv))
        raise fatal
    monkeypatch.setattr(s.api, 'request', request)
    monkeypatch.setattr(s.transport, 'run', fail_remote)
    with pytest.raises(SSHSessionError) as error:
        s.connector.sweep()
    assert error.value is fatal
    assert claims == [[]] and len(remote_calls) == 1
    assert failed_notifications == [] and s.slurm.submit_count == 0
    persisted = json.loads(s.journal.path.read_text())['jobs']['job-1']
    assert persisted['phase'] == 'staging'
    assert 'pending_event' not in persisted and not persisted.get('terminal')
