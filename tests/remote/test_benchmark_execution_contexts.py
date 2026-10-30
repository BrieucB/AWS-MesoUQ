"""Synthetic same-journal migration and multi-context recovery boundaries."""
import copy
import hashlib
import json
import os
from pathlib import Path

import pytest

import aws_mesouq.adapters as adapter_catalog
from aws_mesouq.benchmark import DEFAULT_CONFIGURATION, operator_execution_context_id
from aws_mesouq.benchmark_contexts import DEADLINE_POLICY, JOURNAL_SCHEMA, REGISTRY_ENV, REGISTRY_SCHEMA
from aws_mesouq.benchmark_worker import BenchmarkWorker
from test_benchmark_worker import entries, session


def registry_fixture(session, tmp_path, monkeypatch):
    operator = copy.deepcopy(session.operator)
    operator["science_revision"] = "b" * 40
    repository = tmp_path / "new-science"
    repository.mkdir(mode=0o700)
    operator["repository"] = str(repository)
    operator["adapter_sha256"] = hashlib.sha256(
        Path(adapter_catalog.__file__).with_name("benchmark.py").read_bytes()).hexdigest()
    document = {"schema": REGISTRY_SCHEMA, "legacy_operator": copy.deepcopy(session.operator),
                "contexts": [{"operator": operator, "runtime_clock_policy": DEADLINE_POLICY}]}
    path = tmp_path / "trusted-contexts.json"
    path.write_text(json.dumps(document))
    path.chmod(0o600)
    monkeypatch.setenv(REGISTRY_ENV, str(path))
    return operator, document, path


def restart(session):
    worker = BenchmarkWorker(session.api, session.slurm, session.journal, project="p",
                             operator=session.operator, clock=lambda: session.clock[0])
    session.worker = worker
    return worker


def new_campaign(session, operator):
    session.store.state["projects"]["p"]["benchmark_policy"].update(
        science_revision=operator["science_revision"], adapter_sha256=operator["adapter_sha256"],
        execution_context_id=operator_execution_context_id(operator))
    return session.app.handle("POST", "/v1/projects/p/benchmarks", {"sub": "alice"},
                              {**copy.deepcopy(DEFAULT_CONFIGURATION),
                               "idempotency_key": "new-context"})[1]["campaign"]


def test_migration_preserves_identity_markers_payloads_and_flat_global_state(session, tmp_path, monkeypatch):
    session.worker.sweep()
    entry = entries(session)[0]
    entry.update(refresh_request_id="c" * 32,
                 pending_event={"id": "d" * 32, "type": "heartbeat", "fields": {}},
                 upload_reservation={"id": "e" * 32, "type": "artifact_upload", "fields": {}},
                 upload_plan=[{"fixture": "unchanged"}])
    record = session.worker.state["campaigns"][session.campaign["id"]]
    record["archive_event_id"] = "f" * 32
    session.worker.state["claim_request_id"] = "a" * 32
    session.journal.save()
    before = copy.deepcopy(session.journal.data)
    registry_fixture(session, tmp_path, monkeypatch)
    worker = restart(session)
    after = session.journal.data
    assert after["archive_device"] == before["archive_device"]
    assert after["connector_id"] == before["connector_id"]
    assert after["jobs"] == before["jobs"]
    assert worker.state["context"]["schema"] == JOURNAL_SCHEMA
    assert worker.state["legacy_context"] == before["benchmark_lab"]["context"]
    assert worker.state["stages"] == before["benchmark_lab"]["stages"]
    assert worker.state["campaigns"] == before["benchmark_lab"]["campaigns"]
    assert worker.state["claim_request_id"] == before["benchmark_lab"]["claim_request_id"]
    assert len(worker.context_registry.contexts) == 2
    assert worker.maximum_active == 16


def test_registry_is_required_after_migration_without_mutating_journal(session, tmp_path, monkeypatch):
    registry_fixture(session, tmp_path, monkeypatch)
    restart(session)
    before = copy.deepcopy(session.journal.data)
    monkeypatch.delenv(REGISTRY_ENV)
    with pytest.raises(ValueError, match="registry"):
        restart(session)
    assert session.journal.data == before


@pytest.mark.parametrize("change", ["permissions", "symlink", "hardlink", "relative"])
def test_registry_file_safety_precedes_migration(session, tmp_path, monkeypatch, change):
    _, _, path = registry_fixture(session, tmp_path, monkeypatch)
    if change == "permissions":
        path.chmod(0o644)
    elif change == "symlink":
        link = tmp_path / "link.json"
        link.symlink_to(path)
        monkeypatch.setenv(REGISTRY_ENV, str(link))
    elif change == "hardlink":
        os.link(path, tmp_path / "other-link.json")
    else:
        monkeypatch.setenv(REGISTRY_ENV, path.name)
    before = copy.deepcopy(session.journal.data)
    with pytest.raises(ValueError):
        restart(session)
    assert session.journal.data == before


def test_new_clock_policy_cannot_use_legacy_adapter(session, tmp_path, monkeypatch):
    _, document, path = registry_fixture(session, tmp_path, monkeypatch)
    document["contexts"][0]["operator"]["adapter_sha256"] = session.operator["adapter_sha256"]
    path.write_text(json.dumps(document))
    before = copy.deepcopy(session.journal.data)
    with pytest.raises(ValueError, match="legacy adapter"):
        restart(session)
    assert session.journal.data == before


def test_migration_rejects_untrusted_stored_campaign_before_atomic_save(session, tmp_path, monkeypatch):
    session.worker.sweep()
    entries(session)[0]["claim"]["campaign"]["approved_adapter"]["science_revision"] = "f" * 40
    session.journal.save()
    before = copy.deepcopy(session.journal.data)
    registry_fixture(session, tmp_path, monkeypatch)
    with pytest.raises(ValueError, match="context"):
        restart(session)
    assert session.journal.data == before


def test_preupgrade_pending_claim_keeps_exact_legacy_body(session, tmp_path, monkeypatch):
    session.worker.state["claim_request_id"] = "a" * 32
    session.journal.save()
    registry_fixture(session, tmp_path, monkeypatch)
    worker = restart(session)
    observed = []

    def ambiguous(body):
        observed.append(copy.deepcopy(body))
        raise OSError("fixture lost response")

    monkeypatch.setattr(worker.client, "claim_frozen", ambiguous)
    with pytest.raises(OSError):
        worker._claim()
    expected = {"project_id": "p", "connector_id": session.journal.data["connector_id"],
                "site": "karolina", "request_id": "a" * 32}
    assert observed == [expected]
    assert worker.state["claim_request_body"] == expected
    assert "supported_contexts" not in expected


def test_preupgrade_pending_refresh_keeps_campaign_and_exclusion_order(session, tmp_path, monkeypatch):
    session.worker.sweep()
    old = entries(session)[0]
    old["refresh_request_id"] = "a" * 32
    original = copy.deepcopy(old["claim"])
    session.journal.save()
    registry_fixture(session, tmp_path, monkeypatch)
    worker = restart(session)
    entry = entries(session)[0]
    observed = []

    def ambiguous(body):
        observed.append(copy.deepcopy(body))
        raise OSError("fixture lost response")

    monkeypatch.setattr(worker.client, "claim_frozen", ambiguous)
    with pytest.raises(OSError):
        worker._refresh(entry)
    expected = {"project_id": "p", "connector_id": session.journal.data["connector_id"],
                "site": "karolina", "request_id": "a" * 32,
                "campaign_id": original["campaign"]["id"],
                "exclude_stage_ids": [s["id"] for s in original["campaign"]["stages"]
                                      if s["id"] != original["stage"]["id"]]}
    assert observed == [expected]
    assert entry["refresh_request_body"] == expected
    assert entry["claim"] == original


def test_frozen_claim_does_not_gain_contexts_after_registry_upgrade(session, tmp_path, monkeypatch):
    _, document, path = registry_fixture(session, tmp_path, monkeypatch)
    worker = restart(session)
    observed = []

    def ambiguous(body):
        observed.append(copy.deepcopy(body))
        raise OSError("fixture lost response")

    monkeypatch.setattr(worker.client, "claim_frozen", ambiguous)
    with pytest.raises(OSError):
        worker._claim()
    first = copy.deepcopy(worker.state["claim_request_body"])
    third = copy.deepcopy(document["contexts"][0])
    third["operator"]["science_revision"] = "c" * 40
    document["contexts"].append(third)
    path.write_text(json.dumps(document))
    worker = restart(session)
    monkeypatch.setattr(worker.client, "claim_frozen",
                        lambda body: (observed.append(copy.deepcopy(body)) or {"stage": None}))
    assert worker._claim() is None
    assert observed == [first, first]
    assert len(first["supported_contexts"]) == 2
    assert len(worker.context_registry.contexts) == 3
    assert "claim_request_id" not in worker.state
    assert "claim_request_body" not in worker.state


def test_new_refresh_advertises_only_bound_stage_context(session, tmp_path, monkeypatch):
    session.worker.sweep()
    registry_fixture(session, tmp_path, monkeypatch)
    worker = restart(session)
    entry = entries(session)[0]
    observed = []

    def ambiguous(body):
        observed.append(copy.deepcopy(body))
        raise OSError("fixture lost response")

    monkeypatch.setattr(worker.client, "claim_frozen", ambiguous)
    with pytest.raises(OSError):
        worker._refresh(entry)
    assert observed[0]["supported_contexts"] == [entry["context_id"]]


def test_legacy_and_new_stages_use_separate_pins_and_clock_manifests(session, tmp_path, monkeypatch):
    operator, _, _ = registry_fixture(session, tmp_path, monkeypatch)
    worker = restart(session)
    new = new_campaign(session, operator)
    claimed = [worker._claim(), worker._claim()]
    assert {e["claim"]["campaign"]["id"] for e in claimed} == {session.campaign["id"], new["id"]}
    assert len(worker.state["stages"]) == 2
    for entry in claimed:
        worker.stage(entry)
        manifest = json.loads((Path(entry["control"]) / "manifest.json").read_text())
        script = Path(entry["script"]).read_text()
        if entry["claim"]["campaign"]["id"] == new["id"]:
            assert manifest["science_revision"] == operator["science_revision"]
            assert manifest["repository"] == operator["repository"]
            assert manifest["adapter_sha256"] == operator["adapter_sha256"]
            assert manifest["runtime_clock_policy"] == DEADLINE_POLICY
            assert manifest["allocated_seconds"] == entry["window"]["allocated_seconds"] == 300
            capture = script.index("readonly AWS_MESOUQ_ALLOCATION_JOB_ID")
            assert capture < script.index("source ")
            assert "SLURM_JOB_START_TIME" in script and "SLURM_JOB_END_TIME" in script
        else:
            assert manifest["science_revision"] == session.operator["science_revision"]
            assert manifest["adapter_sha256"] == session.operator["adapter_sha256"]
            assert "runtime_clock_policy" not in manifest
            assert "allocated_seconds" not in manifest
            assert "AWS_MESOUQ_ALLOCATION_" not in script


def test_contexts_share_one_global_active_stage_cap(session, tmp_path, monkeypatch):
    operator, _, _ = registry_fixture(session, tmp_path, monkeypatch)
    worker = restart(session)
    new_campaign(session, operator)
    worker.maximum_active = 1
    worker.sweep()
    assert session.cluster.submit_count == 1
    assert len(worker.state["stages"]) == 1


def test_referenced_context_cannot_be_rewritten(session, tmp_path, monkeypatch):
    operator, document, path = registry_fixture(session, tmp_path, monkeypatch)
    worker = restart(session)
    new_campaign(session, operator)
    worker._claim()
    worker._claim()
    before = copy.deepcopy(session.journal.data)
    document["contexts"][0]["operator"]["python_bin"] = "/not-the-bound-python"
    path.write_text(json.dumps(document))
    with pytest.raises(ValueError, match="retain exact"):
        restart(session)
    assert session.journal.data == before
