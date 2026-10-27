"""Sequential multi-job session integration with the actual authenticated backend."""
from __future__ import annotations

import copy
from datetime import datetime, timedelta, timezone
import hashlib
from pathlib import Path
import re
import time

import pytest

from aws_mesouq.contracts import validate_job_configuration, validate_result
from aws_mesouq.connector.connector import Connector, LeaseExpired, StaleEvent
from aws_mesouq.connector.journal import Journal
from aws_mesouq.connector.transport import Transport
from aws_mesouq.connector.slurm import SUBMISSION_HELPER, Slurm


@pytest.mark.parametrize('site', ['karolina', 'vega'])
def test_ten_jobs_swept_with_recovery_monitoring_and_later_cancellation(tmp_path, monkeypatch, site):
    monkeypatch.syspath_prepend(str(Path(__file__).resolve().parents[2] / 'services/aws_publishing'))
    from aws_publishing.api import Application
    from aws_publishing.storage import MemoryObjects, MemoryStore
    store, objects = MemoryStore(), MemoryObjects()
    store.state['projects'] = {'p': {'name': 'Project'}}
    store.state['identities'] = {'alice': {'projects': {'p': 'maintainer'}}}
    store.state['models']['p:v1'] = {'project_id': 'p', 'version': 'v1', 'trusted': True,
                                  'status': 'released', 'approved_adapter_sha256': hashlib.sha256((Path(__file__).resolve().parents[2] / 'src/aws_mesouq/adapters/indentation.py').read_bytes()).hexdigest(), 'sha256': {'3.2': 'a' * 64}}
    app = Application(store, objects, release_policy='nonproduction_legacy', clock=time.time, validate_configuration=validate_job_configuration, validate_result=validate_result)

    class API:
        submitted_events = 0
        dropped = False
        claims = []
        def request(self, method, path, payload=None):
            result = app.handle(method, path, {'sub': 'alice'}, payload)[1]
            if path == '/v1/connector/claim':
                self.claims.append((copy.deepcopy(payload), result.get('job', {}).get('id') if result.get('job') else None))
            if payload and payload.get('type') == 'submitted':
                self.submitted_events += 1
                if self.submitted_events == 3 and not self.dropped:
                    self.dropped = True
                    raise OSError('Lost third submission event acknowledgment')
            return result
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
    jobs = [api.request('POST', '/v1/projects/p/jobs', {'schema_version': 1, 'kind': 'prediction',
        'model_version': 'v1', 'diameter_um': 3.2, 'configuration': {'parameters': {'Yt': 1, 'kb': 2}},
        'input_artifacts': [ref], 'idempotency_key': f'job-{index}'}) for index in range(10)]

    class Cluster:
        private_root_probe = Transport.private_root_probe
        host = 'owncluster'
        staging_python = '/usr/bin/python3.11'
        def __init__(self):
            self.files, self.jobs, self.submissions, self.cancelled = {}, {}, [], []
        def identity(self):
            return 'ownuser'
        def write(self, path, blob):
            assert path not in self.files
            self.files[path] = blob
        def run(self, argv, **kwargs):
            if len(argv) >= 5 and argv[3] == SUBMISSION_HELPER:
                marker = re.search(rb'#SBATCH --job-name=(\S+)', self.files[argv[-1]]).group(1).decode()
                ident = str(1000 + len(self.submissions))
                self.submissions.append((ident, marker))
                self.jobs[ident] = {'marker': marker, 'state': 'RUNNING'}
                return ident + '\n'
            if argv[0] in {'squeue', 'sacct'}:
                marker = argv[argv.index('--name') + 1]
                return '\n'.join(f"{ident}|{row['marker']}|ownuser|{row['state']}" for ident, row in self.jobs.items() if row['marker'] == marker)
            if argv[0] == 'scancel':
                self.cancelled.append(argv[-1])
                self.jobs[argv[-1]]['state'] = 'CANCELLED'
            return ''

    cluster = Cluster()
    journal = Journal(tmp_path / 'journal')
    try:
        worker = Connector(api, Slurm(cluster), journal, project='p', site=site,
            run_root='/private/runs', repository='/trusted/repo',
            model_registry={'v1': {'version': 'v1', 'sha256': {'3.2': 'a' * 64}, 'trained_dirs': {}}})
        with pytest.raises(OSError, match='third submission'):
            worker.sweep()
        assert len(cluster.submissions) == 3
        visited = worker.sweep()
        assert len(visited) == len(set(visited)) == 10
        assert set(visited) == {job['id'] for job in jobs}
        assert len(cluster.submissions) == 10
        assert len({marker for _, marker in cluster.submissions}) == 10
        assert set(worker.sweep()) == set(visited)
        assert all(store.state['jobs'][job['id']]['status'] == 'running' for job in jobs)
        assert set(worker.sweep()) == set(visited)
        assert len(cluster.submissions) == 10
        later = jobs[-1]
        api.request('POST', f"/v1/projects/p/jobs/{later['id']}/cancel", {})
        worker.sweep()
        assert cluster.cancelled == [store.state['jobs'][later['id']]['slurm_job_id']]
        worker.sweep()
        assert store.state['jobs'][later['id']]['status'] == 'cancelled'
        assert all(store.state['jobs'][job['id']]['status'] == 'running' for job in jobs[:-1])
        assert len(cluster.submissions) == 10
        assert all(len(body.get('exclude_job_ids', [])) <= 9 for body, _ in api.claims)
        assert all(body['site'] == site for body, _ in api.claims)
    finally:
        journal.close()
