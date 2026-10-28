"""Benchmark control-plane security, recovery and budget conservation; no scientific jobs."""
import base64
import copy
import hashlib
import json
from concurrent.futures import ThreadPoolExecutor

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat

from aws_mesouq.benchmark import (
    DEFAULT_CONFIGURATION, SCHEMA, claim_signing_bytes, stage_signing_bytes,
    validate_create, archive_signing_bytes,
)
from aws_publishing.api import APIError, Application
from aws_publishing.storage import MemoryObjects, MemoryStore


@pytest.fixture
def lab():
    store, objects, clock = MemoryStore(), MemoryObjects(), [1000]
    key = Ed25519PrivateKey.generate()
    public = base64.b64encode(key.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw)).decode()
    store.state["projects"] = {"p": {"name": "Lab", "benchmark_policy": {"adapter_sha256": "a" * 64, "science_revision": "b" * 40, "scenario_families": ["baseline", "higher_noise", "broader_priors"]}}}
    store.state["identities"] = {
        "alice": {"projects": {"p": "maintainer"}, "connector_public_keys": {"c1": public}},
        "bob": {"projects": {"p": "member"}, "connector_public_keys": {"c1": public}},
    }
    return Application(store, objects, clock=lambda: clock[0]), store, objects, clock, key


def request(lab, method, path, body=None, sub="alice"):
    return lab[0].handle(method, path, {"sub": sub}, body)


def specification(key="campaign"):
    return {**copy.deepcopy(DEFAULT_CONFIGURATION), "idempotency_key": key}


def create(lab, key="campaign", **updates):
    body = specification(key)
    body.update(updates)
    return request(lab, "POST", "/v1/projects/p/benchmarks", body)[1]["campaign"]


def claim(lab, request_id, campaign=None, sub="alice"):
    body = {"project_id": "p", "connector_id": "c1", "site": "karolina", "request_id": request_id}
    if campaign:
        body["campaign_id"] = campaign
    body["signature"] = base64.b64encode(lab[4].sign(claim_signing_bytes(sub, body))).decode()
    return request(lab, "POST", "/v1/benchmark-connector/claim", body, sub)[1]


def event_body(lab, claimed, event_id, kind, **fields):
    body = {"project_id": "p", "connector_id": "c1", "lease_token": claimed["lease_token"], "event_id": event_id, "type": kind, **fields}
    body["signature"] = base64.b64encode(lab[4].sign(stage_signing_bytes(claimed["campaign"], claimed["stage"], body))).decode()
    return body


def send(lab, claimed, event_id, kind, **fields):
    campaign, stage = claimed["campaign"]["id"], claimed["stage"]["id"]
    body = event_body(lab, claimed, event_id, kind, **fields)
    return request(lab, "POST", f"/v1/benchmark-connector/campaigns/{campaign}/stages/{stage}/events", body)


def result(claimed, *, gpu=0, wall=10, cpu=20, case_hash="d" * 64, summary=None):
    campaign = claimed["campaign"]
    payload = {"schema": SCHEMA, "provenance": {"campaign_id": campaign["id"], "configuration_hash": campaign["configuration_hash"], "adapter_sha256": "a" * 64, "science_revision": "b" * 40, "site": "karolina", "case_set_sha256": case_hash}, "timing": {"wall_seconds": wall, "cpu_seconds": cpu, "gpu_seconds": gpu}, "artifacts": []}
    if claimed["stage"]["id"] == "cases":
        payload["metrics"] = {"method_preparation_costs": {"hnpe": {"wall_seconds": 3, "cpu_seconds": 6, "gpu_seconds": 0}}, "common_case_generation_costs": {"wall_seconds": 7, "cpu_seconds": 14, "gpu_seconds": 0}}
    if summary is not None:
        payload["summary"] = summary
    return payload


def accounting(job="12", seconds=10, state="COMPLETED"):
    return {"source": "sacct", "site": "karolina", "slurm_job_id": job, "state": state, "elapsed_seconds": seconds, "allocated_gpus": 1, "cpu_seconds": seconds * 2}


def start(lab, claimed, job="12"):
    send(lab, claimed, "submitting", "submitting")
    send(lab, claimed, "submitted", "submitted", slurm_job_id=job)


def finish_cases(lab):
    claimed = claim(lab, "cases")
    assert claimed["stage"]["id"] == "cases"
    start(lab, claimed)
    send(lab, claimed, "done", "succeeded", result=result(claimed, gpu=10), scheduler_accounting=accounting())
    return claimed["campaign"]["id"]


def assert_error(code, action):
    with pytest.raises(APIError) as exc:
        action()
    assert exc.value.code == code


def test_frozen_contract_and_capabilities(lab):
    validate_create(specification())
    status, caps = request(lab, "GET", "/v1/projects/p/benchmarks/capabilities")
    assert status == 200 and caps["defaults"]["scenario"]["scale"] == 1
    assert caps["limits"]["controls.hnpe.width"]["choices"] == [64, 128, 256]
    assert caps["actions"]["archive"] is True
    for path, value in (("scale", 0), ("scale", 1.1), ("family", "custom")):
        bad = specification()
        bad["scenario"][path] = value
        assert_error("invalid_benchmark", lambda: request(lab, "POST", "/v1/projects/p/benchmarks", bad))


def test_baseline_scale_and_unknown_configuration_rejected(lab):
    bad = specification()
    bad["scenario"]["scale"] = 2
    assert_error("invalid_benchmark", lambda: request(lab, "POST", "/v1/projects/p/benchmarks", bad))
    bad = specification()
    bad["controls"]["hnpe"]["checkpoint_path"] = "/private/model"
    assert_error("invalid_benchmark", lambda: request(lab, "POST", "/v1/projects/p/benchmarks", bad))


def test_create_idempotency_owner_privacy_and_operator_pin(lab):
    one = create(lab)
    assert create(lab)["id"] == one["id"]
    changed = specification()
    changed["seed"] += 1
    assert_error("idempotency_conflict", lambda: request(lab, "POST", "/v1/projects/p/benchmarks", changed))
    assert request(lab, "GET", "/v1/projects/p/benchmarks", sub="bob")[1] == {"campaigns": []}
    assert_error("not_found", lambda: request(lab, "GET", f"/v1/projects/p/benchmarks/{one['id']}", sub="bob"))
    lab[1].state["projects"]["p"].pop("benchmark_policy")
    assert_error("benchmark_not_enrolled", lambda: create(lab, "two"))
    assert lab[1].state["jobs"] == {}  # Existing calibration namespace stays untouched.


def test_claim_requires_enrolled_signed_owner_identity(lab):
    create(lab)
    body = {"project_id": "p", "connector_id": "c1", "site": "karolina", "request_id": "forged", "signature": "not-a-signature"}
    assert_error("invalid_signature", lambda: request(lab, "POST", "/v1/benchmark-connector/claim", body))
    assert claim(lab, "bob", sub="bob")["stage"] is None


def test_claim_replay_and_expired_submission_reconciles_without_new_attempt(lab):
    create(lab)
    first = claim(lab, "one")
    assert claim(lab, "one")["lease_token"] == first["lease_token"]
    send(lab, first, "submit", "submitting")
    lab[3][0] += 121
    assert_error("claim_expired", lambda: claim(lab, "one"))
    recovered = claim(lab, "reconcile")
    assert recovered["stage"]["id"] == "cases"
    assert recovered["stage"]["status"] == "submission_unknown"
    assert recovered["stage"]["attempt"] == 1
    assert recovered["reconcile_required"] is True
    assert_error("invalid_lease", lambda: send(lab, first, "old", "heartbeat"))


def test_parallel_claims_are_distinct_and_budget_conserved(lab):
    campaign = create(lab)
    finish_cases(lab)
    with ThreadPoolExecutor(max_workers=6) as pool:
        claims = list(pool.map(lambda index: claim(lab, f"parallel-{index}"), range(9)))
    ids = [c["stage"]["id"] for c in claims if c["stage"]]
    assert len(ids) == len(set(ids)) == 9
    assert "hnpe_infer" not in ids and "metrics" not in ids
    ledger = lab[1].state["benchmark_campaigns"][campaign["id"]]["budget_ledger"]
    assert sum(r["gpu_seconds"] for r in ledger["hnpe"]["reservations"].values()) == 7 * 1800
    assert sum(r["gpu_seconds"] for r in ledger["tmcmc"]["reservations"].values()) == 14400
    for value in ledger.values():
        assert value["settled_gpu_seconds"] + sum(r["gpu_seconds"] for r in value["reservations"].values()) <= value["limit_gpu_seconds"]


def test_signed_terminal_settles_actual_gpu_allocation_and_unused_reservation(lab):
    campaign = create(lab)
    finish_cases(lab)
    trained = claim(lab, "train")
    start(lab, trained)
    send(lab, trained, "done", "succeeded", result=result(trained, gpu=10), scheduler_accounting=accounting())
    ledger = lab[1].state["benchmark_campaigns"][campaign["id"]]["budget_ledger"]["hnpe"]
    assert ledger["settled_gpu_seconds"] == 20 and ledger["reservations"] == {}


def test_unknown_execution_holds_reservation_and_reconcile_keeps_it(lab):
    campaign = create(lab)
    finish_cases(lab)
    trained = claim(lab, "train")
    send(lab, trained, "submit", "submitting")
    lab[3][0] += 121
    recovered = claim(lab, "recover")
    assert recovered["stage"]["id"] == trained["stage"]["id"]
    ledger = lab[1].state["benchmark_campaigns"][campaign["id"]]["budget_ledger"]["hnpe"]
    assert ledger["reservations"][trained["stage"]["id"]]["gpu_seconds"] == 1800
    assert_error("invalid_benchmark", lambda: send(lab, recovered, "failed", "failed", failure={"code": "unknown", "message": "Cannot find scheduler receipt"}))
    assert trained["stage"]["id"] in lab[1].state["benchmark_campaigns"][campaign["id"]]["budget_ledger"]["hnpe"]["reservations"]


def test_terminal_receipt_provenance_and_native_accounting_must_match(lab):
    create(lab)
    finish_cases(lab)
    trained = claim(lab, "train")
    start(lab, trained)
    assert_error("invalid_benchmark", lambda: send(lab, trained, "wrong-cases", "succeeded", result=result(trained, gpu=10, case_hash="e" * 64), scheduler_accounting=accounting()))
    wrong = accounting(job="99")
    assert_error("invalid_benchmark", lambda: send(lab, trained, "wrong-scheduler", "succeeded", result=result(trained, gpu=10), scheduler_accounting=wrong))
    wrong = accounting()
    wrong["allocated_gpus"] = 2
    assert_error("invalid_benchmark", lambda: send(lab, trained, "wrong-resources", "succeeded", result=result(trained, gpu=20), scheduler_accounting=wrong))


def test_real_overrun_is_recorded_and_stops_future_method_admissions(lab):
    campaign = create(lab)
    finish_cases(lab)
    trained = claim(lab, "train")
    start(lab, trained)
    send(lab, trained, "overrun", "succeeded", result=result(trained, gpu=1801, wall=1801, cpu=3602), scheduler_accounting=accounting(seconds=1801))
    saved = lab[1].state["benchmark_campaigns"][campaign["id"]]
    ledger = saved["budget_ledger"]["hnpe"]
    assert ledger["settled_gpu_seconds"] == 1811 and ledger["cost_exceeded"]
    assert ledger["reservations"] == {}
    assert next(s for s in saved["stages"] if s["id"] == trained["stage"]["id"])["failure"]["code"] == "budget_overrun"
    subsequent = claim(lab, "next")
    assert subsequent["stage"]["id"] in ("tmcmc", "reference")


def test_cancel_pending_and_active_work_without_forgetting_native_submission(lab):
    campaign = create(lab)
    first = claim(lab, "first")
    start(lab, first)
    cancelled = request(lab, "POST", f"/v1/projects/p/benchmarks/{campaign['id']}/cancel", {})[1]["campaign"]
    assert cancelled["status"] == "cancelling"
    assert next(s for s in cancelled["stages"] if s["id"] == "cases")["slurm_job_id"] == "12"
    send(lab, first, "cancelled", "cancelled", timing={"wall_seconds": 10, "cpu_seconds": 20, "gpu_seconds": 10}, scheduler_accounting=accounting(state="CANCELLED"))
    assert lab[1].state["benchmark_campaigns"][campaign["id"]]["status"] == "cancelled"


def test_tampered_event_cannot_mutate_stage_or_budget(lab):
    create(lab)
    first = claim(lab, "first")
    body = event_body(lab, first, "submit", "submitting")
    body["type"] = "failed"
    path = f"/v1/benchmark-connector/campaigns/{first['campaign']['id']}/stages/cases/events"
    assert_error("invalid_signature", lambda: request(lab, "POST", path, body))
    assert lab[1].state["benchmark_campaigns"][first["campaign"]["id"]]["stages"][0]["status"] == "claimed"


def test_stage_output_private_upload_checksums_and_owner_download(lab):
    campaign = create(lab)
    first = claim(lab, "first")
    payload = b'{"value": 1}'
    ref = {"name": "cases.json", "sha256": hashlib.sha256(payload).hexdigest(), "size_bytes": len(payload), "media_type": "application/json"}
    body = event_body(lab, first, "upload", "artifact_upload", artifacts=[ref])
    path = f"/v1/benchmark-connector/campaigns/{campaign['id']}/stages/cases/artifacts"
    _, response = request(lab, "POST", path, body)
    artifact = response["uploads"][0]["artifact"]
    saved = lab[1].state["benchmark_campaigns"][campaign["id"]]["artifacts"][artifact["id"]]
    lab[2].data[saved["storage_key"]] = payload
    start(lab, first)
    output = result(first, gpu=10)
    output["artifacts"] = [{**ref, "id": artifact["id"]}]
    send(lab, first, "done", "succeeded", result=output, scheduler_accounting=accounting())
    artifact_path = f"/v1/projects/p/benchmarks/{campaign['id']}/artifacts/{artifact['id']}"
    assert request(lab, "GET", artifact_path)[1]["download_url"]
    assert_error("not_found", lambda: request(lab, "GET", artifact_path, sub="bob"))


def test_fixed_budget_exhaustion_does_not_fabricate_success_or_global_quota(lab):
    campaign = create(lab, budget={"mode": "fixed", "method_gpu_seconds": 90})
    finish_cases(lab)
    trained = claim(lab, "train")
    assert trained["stage"]["reserved_seconds"] == 80
    # Other HNPE factors cannot be admitted while this reservation consumes its budget.
    other = claim(lab, "other")
    assert other["stage"]["id"] in ("tmcmc", "reference")
    assert lab[1].state["benchmark_campaigns"][campaign["id"]]["budget_ledger"]["hnpe"]["reservations"]


def test_method_failures_still_produce_an_honest_completed_comparison(lab):
    campaign = create(lab)
    finish_cases(lab)
    # Fail one factor before submission; dependent inference becomes blocked.
    trained = claim(lab, "train")
    send(lab, trained, "no-runtime", "failed", not_submitted=True, failure={"code": "training_failed", "message": "Fresh training could not initialize"})
    index = 0
    while True:
        next_claim = claim(lab, f"remaining-{index}")
        index += 1
        if next_claim["stage"] is None:
            break
        stage = next_claim["stage"]
        start(lab, next_claim, str(100 + index))
        if stage["id"] == "metrics":
            send(lab, next_claim, "report", "succeeded", result=result(next_claim, summary={"metrics": {"hnpe_pass": False}, "plots": [], "training_histories": []}))
            break
        if stage.get("method_group"):
            job = str(100 + index)
            send(lab, next_claim, "done", "succeeded", result=result(next_claim, gpu=10), scheduler_accounting=accounting(job=job))
        else:
            send(lab, next_claim, "done", "succeeded", result=result(next_claim))
    saved = lab[1].state["benchmark_campaigns"][campaign["id"]]
    assert saved["status"] == "completed"
    output = request(lab, "GET", f"/v1/projects/p/benchmarks/{campaign['id']}/results")[1]["result"]
    assert output["metrics"]["hnpe_pass"] is False
    assert any(item["code"] == "training_failed" for item in output["failures"])
    costs = output["costs"]
    hnpe = next(item for item in costs["methods"] if item["method"] == "hnpe")
    assert hnpe["preparation_seconds"] == 10
    assert hnpe["total_seconds"] == 70  # Six successful trains, failed unsubmitted train/inference have no GPU runtime.
    assert costs["reference_seconds"] == 10


def archive_event(lab, campaign, receipt, key="archive"):
    body = {"project_id": "p", "campaign_id": campaign["id"], "connector_id": "c1", "event_id": key, "type": "archive_verified", "receipt": receipt}
    body["signature"] = base64.b64encode(lab[4].sign(archive_signing_bytes(campaign, body))).decode()
    return body


def test_archive_requires_owner_request_enrolled_signature_and_approved_durable_root(lab):
    lab[1].state["projects"]["p"]["benchmark_policy"]["archive_root"] = "/approved/durable"
    campaign = create(lab)
    first = claim(lab, "first")
    start(lab, first)
    request(lab, "POST", f"/v1/projects/p/benchmarks/{campaign['id']}/cancel", {})
    send(lab, first, "cancelled", "cancelled", timing={"wall_seconds": 10, "cpu_seconds": 20, "gpu_seconds": 10}, scheduler_accounting=accounting(state="CANCELLED"))
    receipt = {"site": "karolina", "path": "/approved/durable/campaign", "manifest_sha256": "f" * 64, "artifacts": [], "retained_outputs": [{"path": "manifest.json", "sha256": "e" * 64, "size_bytes": 100}]}
    payload = archive_event(lab, campaign, receipt)
    assert_error("archive_not_requested", lambda: request(lab, "POST", "/v1/benchmark-connector/archive", payload))
    assert_error("not_found", lambda: request(lab, "POST", f"/v1/projects/p/benchmarks/{campaign['id']}/archive", {}, sub="bob"))
    request(lab, "POST", f"/v1/projects/p/benchmarks/{campaign['id']}/archive", {})
    forged = copy.deepcopy(payload)
    forged["receipt"]["path"] = "/unapproved/elsewhere"
    assert_error("invalid_signature", lambda: request(lab, "POST", "/v1/benchmark-connector/archive", forged))
    wrong = copy.deepcopy(receipt)
    wrong["path"] = "/unapproved/elsewhere"
    wrong = archive_event(lab, campaign, wrong, "wrong")
    assert_error("invalid_benchmark", lambda: request(lab, "POST", "/v1/benchmark-connector/archive", wrong))
    _, response = request(lab, "POST", "/v1/benchmark-connector/archive", payload)
    verified_at = response["campaign"]["archive"]["verified_at"]
    assert response["campaign"]["archive"]["status"] == "verified"
    assert response["campaign"]["archive"]["cloud_retention"]["automatic_expiry"] is False
    lab[3][0] += 1000
    assert request(lab, "POST", "/v1/benchmark-connector/archive", payload)[1]["campaign"]["archive"]["verified_at"] == verified_at


def test_typed_gateway_dispatch_preserves_jwt_preflight(lab, monkeypatch):
    pytest.importorskip("fastapi")
    pytest.importorskip("mangum")
    from aws_publishing import handler
    from container_smoke import event
    monkeypatch.setenv("COGNITO_ISSUER", "https://issuer.invalid/pool")
    monkeypatch.setenv("COGNITO_CLIENT_ID", "application-client")
    monkeypatch.setattr(handler, "application", lambda: lab[0])
    monkeypatch.setattr(handler, "_benchmark_adapter", None)
    payload = event("/v1/projects/p/benchmarks", "POST")
    payload["body"] = json.dumps(specification())
    payload["headers"] = {"content-type": "application/json"}
    payload["requestContext"]["authorizer"] = {"jwt": {"claims": {"sub": "alice", "iss": "https://issuer.invalid/pool", "client_id": "application-client", "token_use": "access"}}}
    response = handler.handler(payload, None)
    assert response["statusCode"] == 202, response
    assert json.loads(response["body"])["campaign"]["owner_sub"] == "alice"
    for headers in ({}, {"content-type": "text/plain"}):
        malformed_media = copy.deepcopy(payload)
        malformed_media["headers"] = headers
        assert handler.handler(malformed_media, None)["statusCode"] == 400
    payload["requestContext"]["authorizer"]["jwt"]["claims"]["token_use"] = "id"
    assert handler.handler(payload, None)["statusCode"] == 401
    payload["requestContext"]["authorizer"]["jwt"]["claims"]["token_use"] = "access"
    bad = specification("bad")
    bad["controls"]["hnpe"]["width"] = 63
    payload["body"] = json.dumps(bad)
    assert handler.handler(payload, None)["statusCode"] == 400
