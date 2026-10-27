"""Uncertain staging-heartbeat recovery against the actual conditional backend."""
from __future__ import annotations

import copy
from datetime import datetime
import hashlib
from pathlib import Path
import time

import pytest

from aws_mesouq.contracts import validate_job_configuration, validate_result
from aws_mesouq.connector import connector as connector_module
from aws_mesouq.connector.connector import Connector
from aws_mesouq.connector.journal import Journal
from aws_mesouq.connector.transport import Transport
from aws_mesouq.connector.slurm import SUBMISSION_HELPER, Slurm


@pytest.mark.parametrize('site', ['karolina', 'vega'])
@pytest.mark.parametrize('committed_before_disconnect', [True, False])
def test_lost_staging_heartbeat_replays_then_explicitly_fails_interrupted_stage(
    tmp_path, monkeypatch, site, committed_before_disconnect
):
    monkeypatch.syspath_prepend(str(Path(__file__).resolve().parents[2] / 'services/aws_publishing'))
    from aws_publishing.api import Application
    from aws_publishing.storage import MemoryObjects, MemoryStore
    clock = [time.time()]
    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return cls.fromtimestamp(clock[0], tz)
    monkeypatch.setattr(connector_module, 'datetime', Clock)
    store, objects = MemoryStore(), MemoryObjects()
    store.state['projects'] = {'p': {'name': 'Project'}}
    store.state['identities'] = {'alice': {'projects': {'p': 'maintainer'}}}
    store.state['models']['p:v1'] = {'project_id': 'p', 'version': 'v1', 'trusted': True,
                                  'status': 'released', 'approved_adapter_sha256': hashlib.sha256((Path(__file__).resolve().parents[2] / 'src/aws_mesouq/adapters/indentation.py').read_bytes()).hexdigest(), 'sha256': {'3.2': 'a' * 64}}
    app = Application(store, objects, release_policy='nonproduction_legacy', clock=lambda: clock[0], validate_configuration=validate_job_configuration,
                      validate_result=validate_result)
    class API:
        dropped = False
        events = []
        def request(self, method, path, payload=None):
            if payload and payload.get('type'):
                self.events.append(copy.deepcopy(payload))
            if payload and payload.get('type') == 'heartbeat' and not self.dropped:
                self.dropped = True
                if committed_before_disconnect:
                    app.handle(method, path, {'sub': 'alice'}, payload)
                raise OSError('Lost staging heartbeat acknowledgement')
            return app.handle(method, path, {'sub': 'alice'}, payload)[1]
        def transfer(self, url, *, data=None, headers=None):
            key = url.removeprefix('memory://')
            if data is None:
                return objects.data[key]
            objects.data[key] = data
            return b''
    api = API()
    raw = b'force,displacement\n1,2\n2,3\n3,4\n4,5\n5,6\n'
    uploaded = api.request('POST', '/v1/projects/p/artifacts', {
        'name': 'curve.csv', 'kind': 'curve', 'sha256': hashlib.sha256(raw).hexdigest(),
        'size_bytes': len(raw), 'media_type': 'text/csv', 'metadata': {'nominal_diameter_um': 3.2,
        'force_unit': 'N', 'displacement_unit': 'm', 'column_order': 'force-displacement'}})
    api.transfer(uploaded['upload_url'], data=raw)
    ref = uploaded['artifact']
    api.request('POST', f"/v1/projects/p/artifacts/{ref['id']}/complete", {})
    job = api.request('POST', '/v1/projects/p/jobs', {'schema_version': 1, 'kind': 'prediction',
        'model_version': 'v1', 'diameter_um': 3.2, 'configuration': {'parameters': {'Yt': 1, 'kb': 2}},
        'input_artifacts': [ref], 'idempotency_key': 'staging-recovery'})
    class Cluster:
        private_root_probe = Transport.private_root_probe
        host = 'owncluster'
        staging_python = '/usr/bin/python3.11'
        delayed = False
        submitted = 0
        def identity(self):
            return 'ownuser'
        def run(self, argv, **kwargs):
            if len(argv) >= 5 and argv[3] == SUBMISSION_HELPER:
                self.submitted += 1
                raise AssertionError('Interrupted staging must never submit')
            if not self.delayed:
                self.delayed = True
                clock[0] += 20  # Near-expiry renewal before the next slow operation.
            return ''
        def write(self, *args):
            raise AssertionError('Disconnect occurs before any staging write')
    cluster = Cluster()
    path = tmp_path / 'journal'
    journal = Journal(path)
    def worker(journal):
        return Connector(api, Slurm(cluster), journal, project='p', site=site,
            run_root='/private/runs', repository='/trusted/repo',
            model_registry={'v1': {'version': 'v1', 'sha256': {'3.2': 'a' * 64}, 'trained_dirs': {}}})
    try:
        session = worker(journal)
        with pytest.raises(OSError, match='staging heartbeat'):
            session.tick()
        entry = journal.data['jobs'][job['id']]
        pending = copy.deepcopy(entry['pending_event'])
        assert pending['type'] == 'heartbeat'
        assert entry['phase'] == 'staging'
        assert not entry.get('terminal')
        assert store.state['jobs'][job['id']]['status'] == 'claimed'
        assert [event['type'] for event in api.events] == ['heartbeat']
    finally:
        journal.close()
    # Restart with the on-disk journal and fresh owner-bound lease, rather than
    # replacing the uncertain heartbeat with a differently named failure event.
    journal = Journal(path)
    try:
        session = worker(journal)
        assert session.tick()
        entry = journal.data['jobs'][job['id']]
        assert [event['type'] for event in api.events] == ['heartbeat', 'heartbeat', 'failed']
        assert api.events[0]['event_id'] == api.events[1]['event_id'] == pending['event_id']
        assert api.events[0]['lease_token'] != api.events[1]['lease_token']
        assert store.state['jobs'][job['id']]['status'] == 'failed'
        assert 'Interrupted immutable staging' in store.state['jobs'][job['id']]['error']['reason']
        assert entry['terminal']
        assert 'pending_event' not in entry
        assert cluster.submitted == 0
        assert session.tick() is False
    finally:
        journal.close()
