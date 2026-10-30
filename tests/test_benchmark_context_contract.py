"""Portable execution identity and exact signed-claim replay; synthetic keys only."""
import copy
from types import SimpleNamespace

import pytest

from aws_mesouq import benchmark as contract
from aws_mesouq.benchmark_client import BenchmarkClient
from aws_mesouq.connector.journal import Journal
from aws_mesouq.connector.signing import device_identity


def test_operator_context_digest_binds_complete_trusted_runtime():
    operator = {
        "repository": "/trusted/source", "science_revision": "a" * 40,
        "adapter_sha256": "b" * 64, "python_bin": "/trusted/bin/python",
        "environment_script": "/trusted/env.sh", "environment_sha256": "c" * 64,
        "run_root": "/private/run", "archive_root": "/private/archive",
        "expected_user": "owner", "expected_host": "node.example",
        "scheduler": {"partition": "qgpu", "account": "approved", "qos": "approved", "cpus-per-task": 4, "mem": "16G"},
        "stage_seconds": {"reference": 300, "metrics": 300},
        "allowed_upload_origins": ["https://upload.example"],
    }
    original = copy.deepcopy(operator)
    expected = contract.checksum({
        "schema": "aws_mesouq.benchmark_execution_context.v1", "operator": operator,
    })
    assert contract.operator_execution_context_id(operator) == expected
    for name, value in operator.items():
        changed = copy.deepcopy(operator)
        if isinstance(value, dict):
            first = next(iter(value))
            changed[name][first] = 600 if isinstance(value[first], int) else "other"
        elif isinstance(value, list):
            changed[name] = ["https://other.example"]
        else:
            changed[name] = value + "-changed"
        assert contract.operator_execution_context_id(changed) != expected, name
    assert operator == original


def test_legacy_context_uses_only_frozen_project_source_and_archive():
    campaign = {"project_id": "p", "approved_adapter": {
        "science_revision": "a" * 40, "adapter_sha256": "b" * 64,
        "archive_root": "/private/archive", "scenario_families": ["baseline"],
    }}
    baseline = contract.campaign_execution_context_id(campaign)
    same = copy.deepcopy(campaign)
    same["approved_adapter"]["max_concurrent_stages"] = 2
    assert contract.campaign_execution_context_id(same) == baseline
    for field in ("science_revision", "adapter_sha256", "archive_root"):
        changed = copy.deepcopy(campaign)
        changed["approved_adapter"][field] = {"science_revision": "c" * 40, "adapter_sha256": "d" * 64, "archive_root": "/private/other"}[field]
        assert contract.campaign_execution_context_id(changed) != baseline
    current = copy.deepcopy(campaign)
    current["approved_adapter"]["execution_context_id"] = "e" * 64
    assert contract.campaign_execution_context_id(current) == "e" * 64


@pytest.fixture
def client(tmp_path):
    journal = Journal(tmp_path / "synthetic-journal")
    device_identity(journal)
    calls = []
    def request(method, path, body=None):
        calls.append((method, path, copy.deepcopy(body)))
        return {"stage": None, "campaign": None}
    value = SimpleNamespace(client=BenchmarkClient(SimpleNamespace(request=request), journal, "alice"),
                            calls=calls, journal=journal)
    yield value
    journal.close()


def test_frozen_unsigned_body_survives_signing_and_replays_exactly(client):
    exclusions = ["metrics"]
    contexts = ["c" * 64, "d" * 64]
    body = client.client.build_claim_body("p", "request", campaign_id="campaign",
                                         exclude_stage_ids=exclusions, supported_contexts=contexts)
    before = copy.deepcopy(body)
    exclusions.append("cases")
    contexts.append("e" * 64)
    assert body == before
    identity = copy.deepcopy(client.journal.data["archive_device"])
    client.client.claim_frozen(body)
    client.client.claim_frozen(body)
    assert body == before and "signature" not in body
    assert client.calls[0] == client.calls[1]
    assert client.calls[0][2]["supported_contexts"] == before["supported_contexts"]
    assert client.journal.data["archive_device"] == identity


def test_legacy_claim_wrapper_preserves_original_body(client):
    client.client.claim("p", "legacy", campaign_id="campaign", exclude_stage_ids=["metrics"])
    body = client.calls[0][2]
    assert set(body) == {"project_id", "connector_id", "site", "request_id", "campaign_id", "exclude_stage_ids", "signature"}
    assert "supported_contexts" not in body


@pytest.mark.parametrize("alteration", [
    {"connector_id": "other"}, {"site": "vega"}, {"signature": "pre-signed"},
    {"unsupported": True}, {"supported_contexts": ["d" * 64, "c" * 64]},
])
def test_frozen_claim_rejects_changed_device_or_invalid_body_before_http(client, alteration):
    body = client.client.build_claim_body("p", "request")
    body.update(alteration)
    with pytest.raises(ValueError):
        client.client.claim_frozen(body)
    assert not client.calls
