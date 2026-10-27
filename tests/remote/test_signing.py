"""Application device signing and public enrollment material remain local."""
import base64
import copy
import json
import os
import stat

import pytest
from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

from aws_mesouq.connector import cli
from aws_mesouq.connector.journal import Journal
from aws_mesouq.connector.signing import archive_signing_bytes, device_identity, sign_archive


def test_private_device_key_persists_with_owner_only_permissions(tmp_path):
    directory = tmp_path / 'private'
    journal = Journal(directory)
    identity = device_identity(journal)
    private = journal.data['archive_device']['private_key']
    journal.close()
    assert stat.S_IMODE(directory.stat().st_mode) == 0o700
    assert stat.S_IMODE((directory / 'journal.json').stat().st_mode) == 0o600
    journal = Journal(directory)
    try:
        assert device_identity(journal) == identity
        assert journal.data['archive_device']['private_key'] == private
    finally:
        journal.close()


def test_archive_signature_binds_identity_event_and_receipt_but_not_lease(tmp_path):
    journal = Journal(tmp_path / 'private')
    try:
        identity = device_identity(journal)
        payload = {'project_id': 'p', 'connector_id': identity['connector_id'], 'event_id': 'event-1',
                   'type': 'archive_verified', 'archive': {'site': 'karolina', 'path': '/project/archive/job',
                    'verified_at': '2026-10-27T12:00:00+00:00', 'artifacts': [{'id': 'input-1', 'sha256': 'a'*64, 'size_bytes': 4}]}}
        signature = base64.b64decode(sign_archive(journal, 'job-1', 'owner', payload), validate=True)
        key = Ed25519PublicKey.from_public_bytes(base64.b64decode(identity['archive_public_key'], validate=True))
        key.verify(signature, archive_signing_bytes('job-1', 'owner', payload))
        assert archive_signing_bytes('job-1', 'owner', {**payload, 'lease_token': 'rotated'}) == archive_signing_bytes('job-1', 'owner', payload)
        for field in ('project_id', 'connector_id', 'event_id'):
            tampered = {**payload, field: 'different'}
            with pytest.raises(InvalidSignature):
                key.verify(signature, archive_signing_bytes('job-1', 'owner', tampered))
        for job, owner in [('different', 'owner'), ('job-1', 'different')]:
            with pytest.raises(InvalidSignature):
                key.verify(signature, archive_signing_bytes(job, owner, payload))
        tampered = copy.deepcopy(payload)
        tampered['archive']['path'] = '/scratch/pretend-archive'
        with pytest.raises(InvalidSignature):
            key.verify(signature, archive_signing_bytes('job-1', 'owner', tampered))
    finally:
        journal.close()


def test_show_public_identity_is_offline_and_never_prints_private_key(tmp_path, capsys, monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError('enrollment display must not contact application or HPC')
    monkeypatch.setattr(cli, 'Transport', forbidden)
    monkeypatch.setattr(cli, 'browser_login', forbidden)
    directory = tmp_path / 'private'
    assert cli.main(['--show-archive-public-key', '--journal', str(directory)]) == 0
    output = capsys.readouterr().out
    identity = json.loads(output)
    assert set(identity) == {'connector_id', 'archive_public_key', 'archive_signature_version'}
    assert len(base64.b64decode(identity['archive_public_key'], validate=True)) == 32
    journal = Journal(directory)
    try:
        assert journal.data['archive_device']['private_key'] not in output
    finally:
        journal.close()


@pytest.mark.parametrize('mode', [0o644, 0o660])
def test_journal_rejects_exposed_private_device_file(tmp_path, mode):
    directory = tmp_path / 'private'
    journal = Journal(directory)
    device_identity(journal)
    journal.close()
    (directory / 'journal.json').chmod(mode)
    with pytest.raises(ValueError, match='private'):
        Journal(directory)


def test_journal_rejects_hardlinked_private_device_file(tmp_path):
    directory = tmp_path / 'private'
    journal = Journal(directory)
    device_identity(journal)
    journal.close()
    os.link(directory / 'journal.json', tmp_path / 'another-name')
    with pytest.raises(ValueError, match='private'):
        Journal(directory)


def test_cloud_verifier_matches_connector_envelopes_for_both_signed_events(tmp_path, monkeypatch):
    from pathlib import Path
    from aws_mesouq.connector.signing import result_signing_bytes, sign_result
    monkeypatch.syspath_prepend(str(Path(__file__).resolve().parents[2] / 'services/aws_publishing'))
    from aws_publishing.attestation import archive_payload, result_payload, verify_result, verify_signature
    journal = Journal(tmp_path / 'private')
    try:
        identity = device_identity(journal)
        job = {'id': 'job-1', 'owner_sub': 'owner'}
        enrolled = {'connector_public_keys': {identity['connector_id']: identity['archive_public_key']}}
        base = {'project_id': 'p', 'connector_id': identity['connector_id'], 'event_id': 'event-1'}
        result = {**base, 'type': 'succeeded', 'slurm_job_id': '123', 'result': {'parameters': [{'name': 'ka', 'median': 5}]}}
        result['result_signature'] = sign_result(journal, job['id'], job['owner_sub'], result)
        assert result_signing_bytes('job-1', 'owner', result) == result_payload(job, result)
        verify_result(job, enrolled, result)
        tampered = copy.deepcopy(result)
        tampered['result']['parameters'][0]['median'] = 500
        with pytest.raises(ValueError, match='signature'):
            verify_result(job, enrolled, tampered)
        with pytest.raises(ValueError, match='signature'):
            verify_result(job, enrolled, {**result, 'slurm_job_id': '456'})
        with pytest.raises(ValueError, match='enrolled'):
            verify_result(job, {}, result)
        archive = {**base, 'type': 'archive_verified', 'archive': {'site': 'karolina', 'path': '/project/archive'}}
        archive['archive_signature'] = sign_archive(journal, 'job-1', 'owner', archive)
        assert archive_signing_bytes('job-1', 'owner', archive) == archive_payload(job, archive)
        verify_signature(job, enrolled, archive, field='archive_signature', payload=archive_payload)
    finally:
        journal.close()
