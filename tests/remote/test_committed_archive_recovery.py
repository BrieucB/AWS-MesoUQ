"""Real strict API recovery when accepted archived jobs no longer get leases."""
from __future__ import annotations

import copy
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import sys

import pytest

# Reuse the service's synthetic admission/result builders. All state is in memory.
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / 'services/aws_publishing/tests'))
from test_api import env, upload, call
from test_release_policy import production, locked_spec, complete_result
from test_connector import FakeTransport
from aws_mesouq.adapters import indentation
from aws_mesouq.connector.connector import Connector
from aws_mesouq.connector.journal import Journal
from aws_mesouq.connector.slurm import Slurm
from aws_mesouq.connector.signing import sign_archive
from aws_publishing.api import APIError


@pytest.mark.parametrize('committed,fault', [(True, None), (False, None), (True, 'damaged-durable-file'),
                                          (True, 'changed-receipt'), (True, 'unaccepted-event'), (True, 'revoked-key')])
def test_strict_archive_ack_loss_recovers_without_scheduler_or_scratch(production, tmp_path, committed, fault):
    assert production[0].release_policy == 'v1'
    production[3][0] = datetime.now(timezone.utc).timestamp()
    adapter = Path(indentation.__file__).read_bytes()
    adapter_digest = hashlib.sha256(adapter).hexdigest()
    production[1].state['models']['p:v1']['approved_adapter_sha256'] = adapter_digest
    input_ref = upload(production)
    job = call(production, 'POST', '/v1/projects/p/jobs', locked_spec(input_ref))[1]

    class API:
        drop_archive = True
        events = []
        requests = []
        def request(self, method, path, payload=None):
            self.requests.append((method, path))
            if payload and payload.get('type'):
                self.events.append(copy.deepcopy(payload))
            dropping = payload and payload.get('type') == 'archive_verified' and self.drop_archive
            if dropping and not committed:
                self.drop_archive = False
                raise OSError('archive event did not reach server')
            response = production[0].handle(method, path, {'sub': 'alice'}, payload)[1]
            if dropping:
                self.drop_archive = False
                raise OSError('archive event committed, acknowledgement lost')
            return response
        def transfer(self, url, *, data=None, headers=None):
            key = url.removeprefix('memory://')
            if data is None:
                return production[2].data[key]
            production[2].data[key] = data
            return b''

    api, transport = API(), FakeTransport()
    scheduler = Slurm(transport)
    journal_path = tmp_path / 'journal'
    journal = Journal(journal_path)
    args = dict(project='p', site='karolina', run_root='/private/runs', archive_root='/private/archive',
                durable_archive_root=True, repository='/trusted/repo', model_registry={})
    connector = Connector(api, scheduler, journal, **args)
    production[1].state['identities']['alice']['connector_public_keys'] = {
        connector.connector_id: journal.data['archive_device']['public_key']}
    journal.data['owner_sub'] = 'alice'
    run = '/private/runs/synthetic-run-1'
    entry = {'marker': 'mesouq-synthetic-1', 'run_dir': run, 'phase': 'submission_attempted',
             'slurm_job_id': '42', 'adapter_sha256': adapter_digest}
    journal.data['jobs'][job['id']] = entry
    journal.save()
    connector.lease = api.request('POST', '/v1/connector/claim', {
        'project_id': 'p', 'connector_id': connector.connector_id, 'site': 'karolina', 'archive_capable': True})
    connector.event(job['id'], 'submitting')
    connector.event(job['id'], 'submitted', slurm_job_id='42')
    connector.event(job['id'], 'running')
    scientific = complete_result(production, job, upload_outputs=False)
    scientific['provenance']['runtime']['adapter_sha256'] = adapter_digest
    transport.files[run + '/manifest.json'] = b'{}'
    transport.files[run + '/submit.sh'] = b'synthetic private provenance, never executed'
    transport.files[run + '/aws-mesouq-adapter.py'] = adapter
    reference = api.request('GET', f"/v1/projects/p/artifacts/{input_ref['id']}")
    transport.files[run + '/input-0'] = api.transfer(reference['download_url'])
    for name in scientific['publication_files']:
        if name == 'result.json':
            blob = json.dumps(scientific).encode()
        elif name == 'provenance.json':
            blob = json.dumps(scientific['provenance']).encode()
        elif name == 'settings_and_versions.json':
            blob = json.dumps({'preset_id': 'indentation-sonovue-v1', 'configuration': job['configuration'],
                               'provenance': scientific['provenance']}).encode()
        elif name.endswith('.csv'):
            blob = b'synthetic,fixture\n1,2\n'
        else:
            blob = b'synthetic figure fixture'
        transport.files[run + '/output/' + name] = blob
    with pytest.raises(OSError):
        connector.publish(job, entry)
    pending = copy.deepcopy(entry['pending_event'])
    assert pending['type'] == 'archive_verified'
    current = api.request('GET', f"/v1/projects/p/jobs/{job['id']}")
    assert bool(current.get('archive')) == committed
    if committed:
        before_archive = copy.deepcopy(current['archive'])
        # This is the real behavior missing from the unconditional FakeAPI claim.
        assert api.request('POST', '/v1/connector/claim', {'project_id': 'p', 'connector_id': connector.connector_id,
                           'site': 'karolina', 'archive_capable': True}) == {'job': None}
    for path in list(transport.files):
        if path.startswith(run + '/'):
            del transport.files[path]
    transport.records = []
    journal.close()
    journal = Journal(journal_path)
    try:
        restarted = Connector(api, scheduler, journal, **args)
        if fault == 'damaged-durable-file':
            saved = journal.data['jobs'][job['id']]['archive_binding']
            transport.files[saved['path'] + '/' + saved['files'][0]['destination']] += b'damaged'
        elif fault == 'changed-receipt':
            journal.data['jobs'][job['id']]['pending_event']['archive']['path'] += '-changed'
        elif fault == 'unaccepted-event':
            changed = journal.data['jobs'][job['id']]['pending_event']
            changed['event_id'] = 'signed-but-never-accepted'
            changed['archive_signature'] = sign_archive(journal, job['id'], 'alice', changed)
        elif fault == 'revoked-key':
            # Acknowledging a historically accepted event grants no new authority.
            production[1].state['identities']['alice']['connector_public_keys'] = {}
        before_requests = len(api.requests)
        if fault in {'damaged-durable-file', 'changed-receipt', 'unaccepted-event'}:
            before_events = len(production[1].state['events'])
            expected = AssertionError if fault == 'damaged-durable-file' else ValueError if fault == 'changed-receipt' else APIError
            with pytest.raises(expected) as rejected:
                restarted.tick()
            if fault == 'unaccepted-event':
                assert rejected.value.code == 'invalid_lease'
            assert journal.data['jobs'][job['id']]['pending_event']
            assert restarted.lease is None
            assert len(production[1].state['events']) == before_events
            assert api.request('GET', f"/v1/projects/p/jobs/{job['id']}")['archive'] == before_archive
            assert not any(command[0] in {'sbatch', 'squeue', 'sacct', 'scancel'} for command in transport.commands)
            return
        assert restarted.tick()
        recovered = journal.data['jobs'][job['id']]
        assert recovered['archive_verified_sent'] and recovered['terminal']
        assert not recovered.get('pending_event')
        archive_events = [event for event in api.events if event['type'] == 'archive_verified']
        assert archive_events[-1]['event_id'] == pending['event_id']
        assert archive_events[-1]['archive_signature'] == pending['archive_signature']
        assert {key: value for key, value in archive_events[-1].items() if key != 'lease_token'} == pending
        assert not any(command[0] in {'sbatch', 'squeue', 'sacct', 'scancel'} for command in transport.commands)
        if committed:
            assert restarted.lease is None
            assert not any(path == '/v1/connector/claim' for _, path in api.requests[before_requests:])
            assert archive_events[-1]['lease_token'] == 'idempotent-archive-replay-only'
            assert api.request('GET', f"/v1/projects/p/jobs/{job['id']}")['archive'] == before_archive
        else:
            assert restarted.lease is not None
            assert any(path == '/v1/connector/claim' for _, path in api.requests[before_requests:])
    finally:
        journal.close()
