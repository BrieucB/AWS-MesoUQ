"""Synthetic control-plane fault tests; no scientific or AWS execution."""
from copy import deepcopy
import base64
import json

import pytest
from fastapi import FastAPI
from fastapi.responses import JSONResponse
from fastapi.testclient import TestClient

from aws_mesouq import benchmark_accuracy_protocol as protocol
from aws_publishing.accuracy import AccuracyApplication, defaults
from aws_publishing.api import APIError
from aws_publishing.accuracy_http import attach_accuracy_routes
from aws_publishing.storage import Conflict
from test_benchmark import lab, request as legacy_request


@pytest.fixture
def accuracy_lab(lab):
    now = [1700000000]
    context = {
        "schema": protocol.CONTEXT_SCHEMA, "id": "0" * 64, "enabled": True,
        "owner_sub": "alice", "project_id": "p", "site": "karolina",
        "science_revision": "a" * 40, "source_binding_sha256": "b" * 64,
        "adapter_sha256": "c" * 64, "policy_sha256": "d" * 64,
        "runtime_profile_sha256": "e" * 64, "native_root": "/scratch/accuracy",
        "archive_root": "/project/accuracy", "native_user": "test-user",
        "native_cpus": 16, "native_gpus": 1, "maximum_artifact_bytes": 10_000_000,
        "maximum_total_artifact_bytes": 100_000_000, "maximum_artifact_files": 64,
        "capabilities": sorted(protocol.REQUIRED_CAPABILITIES),
        "allowed_artifacts": {kind: ["stage-receipt.json", "comparison.json", "allocation-accounting-final.json", "display-summary.json", "validation-capsule.json"] for kind in protocol.KINDS},
    }
    context["id"] = protocol.context_id(context)
    app = AccuracyApplication(lab[1], lab[2], clock=lambda: now[0],
        accuracy_policies={"p": [context]})
    return app, lab[1], lab[2], now, lab[4], context


def spec(nonce="native-control-test"):
    return {**defaults(), "idempotency_key": nonce}


def call(suite, method, path, body=None, sub="alice"):
    try:
        return legacy_request(suite, method, path, body, sub)
    except APIError as error:
        return error.status, {"code": error.code, "message": error.message}


def create_accuracy(suite, nonce="native-control-test"):
    status, response = call(suite, "POST", "/v1/projects/p/benchmark-accuracy/campaigns", spec(nonce))
    assert status == 202
    return response["campaign"]


def claim_accuracy(suite, request_id="claim-1", campaign=None, exclude=None):
    body = {"project_id": "p", "connector_id": "c1", "site": "karolina",
        "request_id": request_id, "supported_contexts": [suite[5]["id"]]}
    if campaign is not None:
        body["campaign_id"] = campaign
    if exclude is not None:
        body["exclude_task_ids"] = sorted(exclude)
    body["signature"] = base64.b64encode(suite[4].sign(protocol.claim_signing_bytes("alice", body))).decode()
    status, response = call(suite, "POST", "/v1/benchmark-connector/accuracy/claim", body)
    assert status == 200
    return response, body


def event_body(suite, claimed, event_id, kind, payload):
    task = claimed["task"]
    body = {"project_id": "p", "connector_id": "c1",
        "campaign_id": claimed["campaign"]["id"], "task_id": task["id"],
        "attempt": task["attempt"], "lease_epoch": task["lease_epoch"],
        "lease_token": task["lease"]["token"], "event_id": event_id,
        "expected_task_revision": task["revision"], "manifest_sha256": task["manifest_sha256"],
        "type": kind, "payload": deepcopy(payload)}
    body["signature"] = base64.b64encode(suite[4].sign(protocol.event_signing_bytes("alice", body))).decode()
    return body


def send_event(suite, claimed, event_id, kind, payload):
    body = event_body(suite, claimed, event_id, kind, payload)
    status, response = call(suite, "POST",
        f"/v1/benchmark-connector/accuracy/campaigns/{body['campaign_id']}/tasks/{body['task_id']}/events", body)
    if status == 200:
        claimed["task"].update(response["task"])
        if claimed["task"].get("lease") and response["task"]["lease_expires_at"] is not None:
            claimed["task"]["lease"]["expires_at"] = response["task"]["lease_expires_at"]
    return status, response, body


def started(suite, claimed):
    status, _, _ = send_event(suite, claimed, "intent", "submitting",
        {"script_sha256": "f" * 64, "intent_sha256": "1" * 64, "marker": claimed["task"]["marker"]})
    assert status == 200
    status, _, _ = send_event(suite, claimed, "submitted", "submitted", {"slurm_job_id": "101"})
    assert status == 200


def accounting(state="COMPLETED", seconds=7):
    return {"source": "sacct", "site": "karolina", "slurm_job_id": "101",
        "state": state, "elapsed_seconds": seconds, "allocated_gpus": 1,
        "cpu_seconds": seconds * 16}


def test_capabilities_disabled_without_exact_context(accuracy_lab):
    suite = accuracy_lab
    suite[0].accuracy_policies = {}
    status, body = call(suite, "GET", "/v1/projects/p/benchmark-accuracy/capabilities")
    assert status == 200 and body["enabled"] is False
    status, _ = call(suite, "POST", "/v1/projects/p/benchmark-accuracy/campaigns", spec())
    assert status == 409
    assert suite[1].state["accuracy_campaigns"] == {}


def test_capabilities_and_download_limit_match_server(accuracy_lab):
    status, body = call(accuracy_lab, "GET", "/v1/projects/p/benchmark-accuracy/capabilities")
    assert status == 200 and body["enabled"] is True
    assert body["max_artifact_bytes"] == accuracy_lab[5]["maximum_artifact_bytes"]
    assert body["defaults"]["evaluation"]["confirmation_case_count"] == 256
    assert "idempotency_key" not in body["defaults"]
    assert "native_root" not in json.dumps(body)


def test_same_nonce_replays_one_create_and_changed_payload_conflicts(accuracy_lab):
    one = create_accuracy(accuracy_lab)
    two = create_accuracy(accuracy_lab)
    assert one["id"] == two["id"]
    assert len(accuracy_lab[1].state["accuracy_tasks"]) == 1
    changed = spec()
    changed["seed"] += 1
    status, _ = call(accuracy_lab, "POST", "/v1/projects/p/benchmark-accuracy/campaigns", changed)
    assert status == 409
    assert len(accuracy_lab[1].state["accuracy_allocations"]) == 1


def test_opaque_nonce_never_enters_native_identity_or_path(accuracy_lab):
    campaign = create_accuracy(accuracy_lab, "../\nµ opaque")
    claimed, _ = claim_accuracy(accuracy_lab)
    assert claimed["task"]["marker"].startswith("mqacc-" + campaign["id"])
    assert "../" not in claimed["task"]["marker"]
    assert claimed["task"]["manifest"]["request"]["idempotency_key"] == "../\nµ opaque"
    assert "idempotency_key" not in campaign["configuration"]


def test_other_owner_cannot_read_or_enable_context(accuracy_lab):
    campaign = create_accuracy(accuracy_lab)
    status, _ = call(accuracy_lab, "GET",
        "/v1/projects/p/benchmark-accuracy/campaigns/" + campaign["id"], sub="bob")
    assert status == 404
    status, body = call(accuracy_lab, "GET", "/v1/projects/p/benchmark-accuracy/capabilities", sub="bob")
    assert status == 200 and body["enabled"] is False


def test_three_campaigns_have_independent_method_funding_not_global_two_gpu_cap(accuracy_lab):
    campaigns = [create_accuracy(accuracy_lab, f"campaign-{i}") for i in range(3)]
    assert len({c["id"] for c in campaigns}) == 3
    assert len(accuracy_lab[1].state["accuracy_tasks"]) == 3
    for campaign in campaigns:
        account = campaign["accounting"]["methods"]["hnpe"]
        assert account["reserved_gpu_seconds"] == 1800
        assert account["unknown_allocations"] == 1
        assert account["allocated_gpu_seconds"] is None


def test_cas_retry_does_not_duplicate_task_or_reservation(accuracy_lab, monkeypatch):
    store = accuracy_lab[1]
    original = store.save
    count = [0]
    def racing(revision, before, after):
        count[0] += 1
        if count[0] == 1:
            raise Conflict()
        return original(revision, before, after)
    monkeypatch.setattr(store, "save", racing)
    create_accuracy(accuracy_lab)
    assert count[0] >= 2
    assert len(store.state["accuracy_tasks"]) == 1
    assert len(store.state["accuracy_allocations"]) == 1


def test_claim_ack_loss_replays_identical_fenced_claim(accuracy_lab):
    create_accuracy(accuracy_lab)
    one, body = claim_accuracy(accuracy_lab)
    status, two = call(accuracy_lab, "POST", "/v1/benchmark-connector/accuracy/claim", body)
    assert status == 200 and one == two
    another = deepcopy(body)
    another["campaign_id"] = one["campaign"]["id"]
    another["signature"] = base64.b64encode(accuracy_lab[4].sign(protocol.claim_signing_bytes("alice", another))).decode()
    status, _ = call(accuracy_lab, "POST", "/v1/benchmark-connector/accuracy/claim", another)
    assert status == 409


def test_event_ack_loss_replays_original_without_touching_revision(accuracy_lab):
    create_accuracy(accuracy_lab)
    claimed, _ = claim_accuracy(accuracy_lab)
    status, first, frozen = send_event(accuracy_lab, claimed, "heartbeat", "heartbeat", {})
    assert status == 200
    accuracy_lab[3][0] += 2
    path = f"/v1/benchmark-connector/accuracy/campaigns/{frozen['campaign_id']}/tasks/{frozen['task_id']}/events"
    status, replay = call(accuracy_lab, "POST", path, frozen)
    assert status == 200 and replay == first
    different = deepcopy(frozen)
    different["payload"] = {"optimizer_updates": 100}
    different["signature"] = base64.b64encode(accuracy_lab[4].sign(protocol.event_signing_bytes("alice", different))).decode()
    status, _ = call(accuracy_lab, "POST", path, different)
    assert status == 422


def test_ambiguous_submission_new_lease_is_reconcile_only(accuracy_lab):
    campaign = create_accuracy(accuracy_lab)
    claimed, _ = claim_accuracy(accuracy_lab)
    old = deepcopy(claimed)
    status, _, _ = send_event(accuracy_lab, claimed, "intent", "submitting",
        {"script_sha256": "f"*64, "intent_sha256": "1"*64, "marker": claimed["task"]["marker"]})
    assert status == 200
    accuracy_lab[3][0] = claimed["task"]["lease"]["expires_at"] + 1
    recovered, _ = claim_accuracy(accuracy_lab, "recover", campaign["id"])
    assert recovered["task"]["id"] == old["task"]["id"]
    assert recovered["task"]["reconcile_only"] is True
    assert recovered["task"]["lease_epoch"] == old["task"]["lease_epoch"] + 1
    status, _, _ = send_event(accuracy_lab, recovered, "new-intent", "submitting",
        {"script_sha256": "f"*64, "intent_sha256": "1"*64, "marker": recovered["task"]["marker"]})
    assert status == 409
    assert len(accuracy_lab[1].state["accuracy_allocations"]) == 1
    row = next(iter(accuracy_lab[1].state["accuracy_allocations"].values()))["row"]
    assert row["allocated_gpu_seconds"] is None and row["reserved_gpu_seconds"] == 1800


def test_old_fence_cannot_mutate_recovered_task(accuracy_lab):
    campaign = create_accuracy(accuracy_lab)
    claimed, _ = claim_accuracy(accuracy_lab)
    frozen = event_body(accuracy_lab, claimed, "old", "heartbeat", {})
    accuracy_lab[3][0] = claimed["task"]["lease"]["expires_at"] + 1
    recovered, _ = claim_accuracy(accuracy_lab, "recover", campaign["id"])
    assert recovered["task"]["lease_epoch"] == 2
    status, _ = call(accuracy_lab, "POST",
        f"/v1/benchmark-connector/accuracy/campaigns/{campaign['id']}/tasks/{claimed['task']['id']}/events", frozen)
    assert status == 409


def test_native_job_identity_cannot_change(accuracy_lab):
    create_accuracy(accuracy_lab)
    claimed, _ = claim_accuracy(accuracy_lab)
    started(accuracy_lab, claimed)
    status, _, _ = send_event(accuracy_lab, claimed, "different-job", "queued", {"slurm_job_id": "102"})
    assert status == 409


def test_unknown_cost_cannot_be_failed_as_zero(accuracy_lab):
    create_accuracy(accuracy_lab)
    claimed, _ = claim_accuracy(accuracy_lab)
    started(accuracy_lab, claimed)
    status, _, _ = send_event(accuracy_lab, claimed, "failed", "failed",
        {"not_submitted": False, "failure": {"code": "unknown", "message": "Retain unknown cost"}})
    assert status == 409
    row = next(iter(accuracy_lab[1].state["accuracy_allocations"].values()))["row"]
    assert row["allocated_gpu_seconds"] is None


def test_requested_queued_cancel_exact_zero_settles_once(accuracy_lab):
    campaign = create_accuracy(accuracy_lab)
    claimed, _ = claim_accuracy(accuracy_lab)
    started(accuracy_lab, claimed)
    status, _ = call(accuracy_lab, "POST",
        f"/v1/projects/p/benchmark-accuracy/campaigns/{campaign['id']}/cancel", {})
    assert status == 200
    claimed, _ = claim_accuracy(accuracy_lab, "cancel-refresh", campaign["id"])
    proof = {"source": "sacct", "site": "karolina", "slurm_job_id": "101", "state": "CANCELLED",
        "elapsed_seconds": 0, "allocated_gpus": 0, "cpu_seconds": 0,
        "preallocation_cancellation": {"start": None, "allocated_cpus": 0, "allocated_tres": ""}}
    status, _, _ = send_event(accuracy_lab, claimed, "closed", "allocation_closed", {"scheduler_accounting": proof})
    assert status == 200
    status, response, frozen = send_event(accuracy_lab, claimed, "cancelled", "cancelled",
        {"not_submitted": False, "scheduler_accounting": proof})
    assert status == 200 and response["campaign"]["status"] == "cancelled"
    status, again = call(accuracy_lab, "POST",
        f"/v1/benchmark-connector/accuracy/campaigns/{campaign['id']}/tasks/{claimed['task']['id']}/events", frozen)
    assert status == 200 and again == response
    row = next(iter(accuracy_lab[1].state["accuracy_allocations"].values()))["row"]
    assert row["allocated_gpu_seconds"] == 0 and row["allocated_cpu_seconds"] == 0
    assert row["reserved_gpu_seconds"] == 0


@pytest.mark.parametrize("replacement", [True, 0.0, "0", None])
def test_queued_zero_proof_cannot_coerce_cpu_type(accuracy_lab, replacement):
    proof = {"source": "sacct", "site": "karolina", "slurm_job_id": "101", "state": "CANCELLED",
        "elapsed_seconds": 0, "allocated_gpus": 0, "cpu_seconds": 0,
        "preallocation_cancellation": {"start": None, "allocated_cpus": replacement, "allocated_tres": ""}}
    with pytest.raises(ValueError):
        protocol.validate_accounting(proof, job_id="101", task_status="cancelling",
            expected_cpus=16, allow_preallocation=True)


def test_allocated_one_gpu_path_never_accepts_zero_allocation_proof():
    proof = accounting("CANCELLED", 0)
    proof["preallocation_cancellation"] = {"start": None, "allocated_cpus": 0, "allocated_tres": ""}
    with pytest.raises(ValueError):
        protocol.validate_accounting(proof, job_id="101", task_status="cancelling",
            expected_cpus=16, allow_preallocation=True)


def test_browser_cancel_before_intent_proves_never_submitted(accuracy_lab):
    campaign = create_accuracy(accuracy_lab)
    status, body = call(accuracy_lab, "POST",
        f"/v1/projects/p/benchmark-accuracy/campaigns/{campaign['id']}/cancel", {})
    assert status == 200 and body["campaign"]["status"] == "cancelled"
    assert body["campaign"]["archive"]["available"] is False
    row = next(iter(accuracy_lab[1].state["accuracy_allocations"].values()))["row"]
    assert row["not_submitted"] is True and row["allocated_gpu_seconds"] == 0


def test_typed_http_preserves_opaque_nonce_and_rejects_raw_integer_coercion():
    app = FastAPI()
    def dispatch(request, body):
        return JSONResponse({"captured": body}, status_code=202)
    attach_accuracy_routes(app, dispatch)
    client = TestClient(app)
    payload = spec("line\nµ")
    path = "/v1/projects/p/benchmark-accuracy/campaigns"
    response = client.post(path, json=payload)
    assert response.status_code == 202 and response.json()["captured"]["idempotency_key"] == "line\nµ"
    for field in ("guide_case_count", "confirmation_case_count", "posterior_draws"):
        bad = deepcopy(payload)
        bad["evaluation"][field] = float(bad["evaluation"][field])
        assert client.post(path, json=bad).status_code == 422
    bad = deepcopy(payload)
    bad["controls"]["hnpe"]["depth"] = True
    assert client.post(path, json=bad).status_code == 422
