"""Durable PROJECT transfer and receipt recovery without live HPC/storage writes."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
import subprocess

import pytest

from aws_mesouq.connector.connector import Connector
from aws_mesouq.connector.transport import Transport
from test_connector import session, publication_session


@pytest.fixture
def archive_files(tmp_path):
    scratch, project = tmp_path / 'scratch', tmp_path / 'project'
    scratch.mkdir(mode=0o700)
    project.mkdir(mode=0o700)
    (scratch / 'output').mkdir(mode=0o700)
    source = scratch / 'output' / 'posterior.csv'
    source.write_bytes(b'x,y\n1,2\n')
    source.chmod(0o400)
    item = {'source_root': str(scratch), 'source': 'output/posterior.csv',
            'destination': 'output/posterior.csv', 'sha256': hashlib.sha256(source.read_bytes()).hexdigest(),
            'size_bytes': source.stat().st_size}
    return Transport(local=True), scratch, project, item


def test_real_archive_verified_copy_is_idempotent_and_private(archive_files):
    transport, scratch, project, item = archive_files
    transport.archive_prepare(str(project), 'job-1')
    transport.archive_copy(str(project), 'job-1', item)
    target = project / 'job-1' / item['destination']
    inode = target.stat().st_ino
    transport.archive_copy(str(project), 'job-1', item)
    assert target.read_bytes() == (scratch / item['source']).read_bytes()
    assert target.stat().st_ino == inode
    assert target.stat().st_mode & 0o777 == 0o400
    assert target.parent.stat().st_mode & 0o777 == 0o700
    assert not list(target.parent.glob('*.partial'))


@pytest.mark.parametrize('corruption', ['size', 'hash', 'existing', 'source-symlink', 'target-symlink', 'intermediate-symlink', 'readonly-root', 'root-symlink', 'public-root'])
def test_real_archive_rejects_corruption_and_unsafe_storage(archive_files, corruption):
    transport, scratch, project, item = archive_files
    if corruption == 'size':
        item['size_bytes'] += 1
    elif corruption == 'hash':
        item['sha256'] = 'a' * 64
    elif corruption == 'existing':
        transport.archive_copy(str(project), 'job-1', item)
        target = project / 'job-1' / item['destination']
        target.chmod(0o600)
        target.write_bytes(b'incorrect')
    elif corruption == 'source-symlink':
        source = scratch / item['source']
        source.rename(source.with_suffix('.private'))
        source.symlink_to(source.with_suffix('.private'))
    elif corruption in {'target-symlink', 'intermediate-symlink'}:
        transport.archive_prepare(str(project), 'job-1')
        outside = project.parent / 'outside'
        outside.mkdir(mode=0o700)
        if corruption == 'intermediate-symlink':
            (project / 'job-1' / 'output').symlink_to(outside, target_is_directory=True)
        else:
            (project / 'job-1' / 'output').mkdir(mode=0o700)
            (project / 'job-1' / item['destination']).symlink_to(scratch / item['source'])
    elif corruption == 'readonly-root':
        project.chmod(0o500)
    elif corruption == 'public-root':
        project.chmod(0o755)
    elif corruption == 'root-symlink':
        alias = project.with_name('alias')
        alias.symlink_to(project, target_is_directory=True)
        project = alias
    with pytest.raises(subprocess.CalledProcessError):
        transport.archive_copy(str(project), 'job-1', item)


@pytest.mark.parametrize('linked', [False, True])
def test_real_archive_recovers_interrupted_temporary_install(archive_files, linked):
    transport, _, project, item = archive_files
    transport.archive_prepare(str(project), 'job-1')
    parent = project / 'job-1' / 'output'
    parent.mkdir(mode=0o700)
    partial = parent / '.posterior.csv.partial'
    partial.write_bytes(b'x,y\n1,2\n' if linked else b'incomplete')
    partial.chmod(0o400)
    if linked:
        import os
        os.link(partial, parent / 'posterior.csv')
    transport.archive_copy(str(project), 'job-1', item)
    assert not partial.exists()
    assert (parent / 'posterior.csv').read_bytes() == b'x,y\n1,2\n'


@pytest.mark.parametrize('run,archive', [('/private/runs', '/private/runs'), ('/private/runs', '/private/runs/archive'),
                                         ('/private/runs/sub', '/private/runs'), ('/', '/project/archive')])
def test_archive_roots_must_be_distinct_and_nonnested(session, run, archive):
    with pytest.raises(ValueError, match='separate'):
        Connector(session.api, session.slurm, session.journal, project='project-1', site='karolina',
                  run_root=run, archive_root=archive, durable_archive_root=True,
                  repository='/trusted/repo', model_registry={})


def test_archive_attestation_requires_separate_root(session):
    with pytest.raises(ValueError, match='attestation'):
        Connector(session.api, session.slurm, session.journal, project='project-1', site='karolina',
                  run_root='/scratch/runs', durable_archive_root=True, repository='/trusted/repo', model_registry={})


def ready_publication(publication_session):
    s, output, result = publication_session
    s.transport.files[output + '/result.json'] = json.dumps(result).encode()
    original = s.api.request
    def request(method, path, payload=None):
        if path == '/v1/projects/project-1/artifacts':
            aid = f'artifact-{len(s.journal.data["jobs"]["job-1"].get("uploaded", {}))}'
            return {'artifact': {'id': aid, **payload}, 'upload_url': 'https://upload.example'}
        if path.endswith('/complete'):
            return {}
        return original(method, path, payload)
    s.api.request = request
    s.api.transfer = lambda *args, **kwargs: b''
    return s, output, result


def test_interrupted_archive_retries_same_bytes_before_immutable_receipt(publication_session):
    s, _, _ = ready_publication(publication_session)
    original = s.transport.archive_copy
    calls = []
    def interrupted(root, token, item):
        original(root, token, item)
        calls.append(item['destination'])
        if len(calls) == 2:
            raise OSError('lost remote archive copy acknowledgement')
    s.transport.archive_copy = interrupted
    with pytest.raises(OSError):
        s.connector.tick()
    entry = s.journal.data['jobs']['job-1']
    assert entry['archive_copy_pending']
    assert not entry.get('archive_receipt')
    assert not any(e['type'] == 'archive_verified' for e in s.api.events)
    s.transport.archive_copy = original
    s.connector.tick()
    receipt = next(e['archive'] for e in s.api.events if e['type'] == 'archive_verified')
    assert receipt['path'].startswith('/private/archive/')
    assert not receipt['path'].startswith('/private/runs/')
    assert {a['id'] for a in receipt['artifacts']} == {a['id'] for a in entry['uploaded'].values()}
    assert entry['archive_receipt'] == receipt
    assert s.slurm.submit_count == 1
    assert len(entry['archive_copied']) == 5
    assert not entry.get('archive_copy_pending')


def test_archive_changed_bytes_fail_closed_on_retry(publication_session):
    s, _, _ = ready_publication(publication_session)
    original = s.transport.archive_copy
    def interrupted(root, token, item):
        original(root, token, item)
        raise OSError('disconnect')
    s.transport.archive_copy = interrupted
    with pytest.raises(OSError):
        s.connector.tick()
    entry = s.journal.data['jobs']['job-1']
    first = entry['archive_binding']['files'][1]
    # Preserve valid JSON while changing the retained bytes.
    s.transport.files[first['source_root'] + '/' + first['source']] += b' '
    s.transport.archive_copy = original
    with pytest.raises(AssertionError):
        s.connector.tick()
    assert not entry.get('archive_receipt')
    assert not any(e['type'] == 'archive_verified' for e in s.api.events)


def test_adapter_snapshot_is_hash_bound_and_executed(session):
    session.connector.tick()
    entry = session.journal.data['jobs']['job-1']
    run = entry['run_dir']
    snapshot = session.transport.files[run + '/aws-mesouq-adapter.py']
    manifest = json.loads(session.transport.files[run + '/manifest.json'])
    assert hashlib.sha256(snapshot).hexdigest() == entry['adapter_sha256']
    assert manifest['runtime']['adapter_sha256'] == entry['adapter_sha256']
    assert run + '/aws-mesouq-adapter.py --manifest ' in session.transport.files[run + '/submit.sh'].decode()
    private_key = session.journal.data['archive_device']['private_key'].encode()
    assert all(private_key not in blob for blob in session.transport.files.values())


def test_archive_lost_receipt_ack_replays_identical_receipt(publication_session):
    s, _, _ = ready_publication(publication_session)
    original = s.api.request
    dropped = []
    def request(method, path, payload=None):
        response = original(method, path, payload)
        if payload and payload.get('type') == 'archive_verified' and not dropped:
            dropped.append(payload)
            raise OSError('receipt committed, acknowledgement lost')
        return response
    s.api.request = request
    with pytest.raises(OSError):
        s.connector.tick()
    entry = s.journal.data['jobs']['job-1']
    saved = json.loads(json.dumps(entry['archive_receipt']))
    assert entry['pending_event']['type'] == 'archive_verified'
    # Retention recovery must be independent of SCRATCH after the verified copy.
    for name in list(s.transport.files):
        if name.startswith(entry['run_dir'] + '/'):
            del s.transport.files[name]
    s.connector.tick()
    receipts = [e for e in s.api.events if e['type'] == 'archive_verified']
    assert len(receipts) == 2
    assert receipts[0]['event_id'] == receipts[1]['event_id']
    assert receipts[0]['archive'] == receipts[1]['archive'] == saved
    assert entry['terminal']
    assert s.slurm.submit_count == 1


@pytest.mark.parametrize('site,attested', [('vega', True), ('karolina', False)])
def test_unattested_or_vega_cannot_claim_karolina_archive(publication_session, site, attested):
    s, _, _ = ready_publication(publication_session)
    s.connector.site = site
    s.connector.durable_archive_root = attested
    s.connector.tick()
    assert s.api.events[-1]['type'] == 'succeeded'
    assert not any(e['type'] == 'archive_verified' for e in s.api.events)
    assert not any(c[0] == 'archive_prepare' for c in s.transport.commands)


def test_private_adapter_corruption_cannot_authorize_archive(publication_session):
    s, _, _ = ready_publication(publication_session)
    entry = s.journal.data['jobs']['job-1']
    s.transport.files[entry['run_dir'] + '/aws-mesouq-adapter.py'] += b'\n# changed\n'
    with pytest.raises(ValueError, match='adapter snapshot checksum'):
        s.connector.tick()
    assert not entry.get('archive_receipt')
    assert not any(e['type'] == 'archive_verified' for e in s.api.events)


@pytest.mark.parametrize('gpus', ['2', 'gpu:2', '1,2'])
def test_actual_gpu_allocation_guard_precedes_science(session, gpus):
    session.connector.tick()
    entry = session.journal.data['jobs']['job-1']
    script = session.transport.files[entry['run_dir'] + '/submit.sh'].decode()
    result = subprocess.run(['/bin/bash'], input=script, text=True, capture_output=True,
                            env={'SLURM_JOB_CPUS_PER_NODE': '16', 'SLURM_GPUS_ON_NODE': gpus})
    assert result.returncode == 2
    assert 'GPU allocation' in result.stdout
    assert not result.stderr


def test_pending_receipt_rechecks_durable_bytes_after_disconnect(publication_session):
    s, _, _ = ready_publication(publication_session)
    original = s.api.request
    def request(method, path, payload=None):
        if payload and payload.get('type') == 'archive_verified':
            raise OSError('disconnected before sending receipt')
        return original(method, path, payload)
    s.api.request = request
    with pytest.raises(OSError):
        s.connector.tick()
    entry = s.journal.data['jobs']['job-1']
    binding = entry['archive_binding']
    target = binding['path'] + '/' + binding['files'][0]['destination']
    s.transport.files[target] += b'damaged'
    s.api.request = original
    with pytest.raises(AssertionError):
        s.connector.tick()
    assert entry['pending_event']['type'] == 'archive_verified'
    assert not any(e['type'] == 'archive_verified' for e in s.api.events)
    assert s.slurm.submit_count == 1


def test_real_durable_reverification_needs_no_scratch_and_rejects_damage(archive_files):
    transport, scratch, project, item = archive_files
    transport.archive_copy(str(project), 'job-1', item)
    (scratch / item['source']).unlink()
    transport.archive_verify(str(project), 'job-1', item)
    target = project / 'job-1' / item['destination']
    target.chmod(0o600)
    target.write_bytes(b'badbytes')
    with pytest.raises(subprocess.CalledProcessError):
        transport.archive_verify(str(project), 'job-1', item)


@pytest.mark.parametrize('crash_stage', ['succeeded_ack', 'before_succeeded_event', 'before_archive_event', 'last_copy_ack'])
def test_every_postcopy_stage_recovers_without_scratch(publication_session, crash_stage):
    s, _, _ = ready_publication(publication_session)
    entry = s.journal.data['jobs']['job-1']
    original_request, original_event, original_copy = s.api.request, s.connector.event, s.transport.archive_copy
    if crash_stage == 'succeeded_ack':
        def request(method, path, payload=None):
            response = original_request(method, path, payload)
            if payload and payload.get('type') == 'succeeded':
                raise OSError('succeeded committed, ACK lost')
            return response
        s.api.request = request
    elif crash_stage in {'before_succeeded_event', 'before_archive_event'}:
        def event(job_id, event_type, **fields):
            if event_type == ('succeeded' if crash_stage == 'before_succeeded_event' else 'archive_verified'):
                raise OSError('process interrupted before event journaling')
            return original_event(job_id, event_type, **fields)
        s.connector.event = event
    else:
        def archive_copy(root, token, item):
            original_copy(root, token, item)
            if item['destination'] == 'execution/aws-mesouq-adapter.py':
                raise OSError('final copy committed, ACK lost')
        s.transport.archive_copy = archive_copy
    with pytest.raises(OSError):
        s.connector.tick()
    for path in list(s.transport.files):
        if path.startswith(entry['run_dir'] + '/'):
            del s.transport.files[path]
    # No scheduler lookup is required once immutable archive bindings exist.
    s.transport.records = []
    s.api.request, s.connector.event, s.transport.archive_copy = original_request, original_event, original_copy
    s.connector.tick()
    assert entry['succeeded_sent'] and entry['archive_verified_sent'] and entry['terminal']
    assert s.api.events[-1]['type'] == 'archive_verified'
    assert s.api.events[-1]['archive_signature']
    assert s.slurm.submit_count == 1


@pytest.mark.parametrize('pin', [None, 'f'*64, 'bad'])
def test_missing_or_wrong_approved_adapter_pin_fails_before_remote_staging(session, pin):
    session.job['approved_model']['approved_adapter_sha256'] = pin
    with pytest.raises(ValueError, match='Approved adapter'):
        session.connector.tick()
    assert not session.transport.commands
    assert not session.transport.files
    assert session.slurm.submit_count == 0


def test_private_registry_adapter_pin_cannot_override_operator_approval(session):
    session.connector.models['model-1']['approved_adapter_sha256'] = 'f'*64
    with pytest.raises(ValueError, match='Approved adapter'):
        session.connector.tick()
    assert not session.transport.commands
    assert session.slurm.submit_count == 0


def test_initial_receipt_rechecks_durable_bytes_after_success_event(publication_session):
    s, _, _ = ready_publication(publication_session)
    original = s.api.request
    def request(method, path, payload=None):
        response = original(method, path, payload)
        if payload and payload.get('type') == 'succeeded':
            entry = s.journal.data['jobs']['job-1']
            binding = entry['archive_binding']
            target = binding['path'] + '/' + binding['files'][0]['destination']
            s.transport.files[target] += b'damaged'
        return response
    s.api.request = request
    with pytest.raises(AssertionError):
        s.connector.tick()
    assert not any(e['type'] == 'archive_verified' for e in s.api.events)
    assert s.journal.data['jobs']['job-1']['pending_event']['type'] == 'archive_verified'
