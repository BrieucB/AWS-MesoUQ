"""Context capabilities select work before any native budget or lease mutation."""
import base64
import copy

import pytest

from aws_mesouq import benchmark as contract
from test_benchmark import (
    lab, assert_error, claim, create, request, specification,
)


def context_claim(lab, request_id, contexts=None, *, campaign=None):
    body = {"project_id": "p", "connector_id": "c1", "site": "karolina", "request_id": request_id}
    if contexts is not None:
        body["supported_contexts"] = contexts
    if campaign is not None:
        body["campaign_id"] = campaign
    body["signature"] = base64.b64encode(lab[4].sign(contract.claim_signing_bytes("alice", body))).decode()
    return request(lab, "POST", "/v1/benchmark-connector/claim", body)[1]


def stored_new_campaign(lab, key="new"):
    # Stored future campaigns must be safe even for an old connector that cannot
    # parse their optional policy field. This fixture also runs on old source.
    campaign = create(lab, key)
    saved = lab[1].state["benchmark_campaigns"][campaign["id"]]
    saved["approved_adapter"]["execution_context_id"] = "c" * 64
    return saved


def test_legacy_connector_does_not_mutate_new_context(lab):
    campaign = stored_new_campaign(lab)
    # A stale derived DAG is intentional: an unsupported claim must not refresh it.
    campaign["stages"][0]["status"] = "failed"
    before = copy.deepcopy(campaign)
    claimed = claim(lab, "old-client")
    assert claimed["stage"] is None
    assert lab[1].state["benchmark_campaigns"][campaign["id"]] == before


def test_legacy_connector_does_not_reconcile_expired_new_lease(lab):
    campaign = create(lab)
    claimed = claim(lab, "first")
    saved = lab[1].state["benchmark_campaigns"][campaign["id"]]
    saved["approved_adapter"]["execution_context_id"] = "c" * 64
    saved["stages"][0]["status"] = "submitting"
    lab[3][0] += 121
    before = copy.deepcopy(saved)
    assert claim(lab, "old-recovery")["stage"] is None
    assert lab[1].state["benchmark_campaigns"][campaign["id"]] == before
    assert claimed["stage"]["attempt"] == 1


def test_new_policy_context_is_optional_operator_only_and_frozen(lab):
    legacy = create(lab, "legacy")
    assert "execution_context_id" not in legacy["approved_adapter"]
    policy = lab[1].state["projects"]["p"]["benchmark_policy"]
    policy["execution_context_id"] = "c" * 64
    current = create(lab, "new")
    assert current["approved_adapter"]["execution_context_id"] == "c" * 64
    policy["execution_context_id"] = "d" * 64
    assert create(lab, "new")["approved_adapter"] == current["approved_adapter"]
    assert lab[1].state["benchmark_campaigns"][legacy["id"]]["approved_adapter"] == legacy["approved_adapter"]
    bad = specification("browser-context")
    bad["execution_context_id"] = "e" * 64
    assert_error("invalid_benchmark", lambda: request(lab, "POST", "/v1/projects/p/benchmarks", bad))


@pytest.mark.parametrize("value", [None, False, 0, "", "a" * 63, "A" * 64, "g" * 64])
def test_policy_rejects_invalid_context_ids(lab, value):
    lab[1].state["projects"]["p"]["benchmark_policy"]["execution_context_id"] = value
    assert_error("invalid_benchmark", lambda: create(lab))
    assert not lab[1].state["benchmark_campaigns"]


def test_supported_context_skips_other_campaign_without_refresh_or_budget(lab):
    legacy = create(lab, "legacy")
    current = stored_new_campaign(lab)
    before = copy.deepcopy(lab[1].state["benchmark_campaigns"][legacy["id"]])
    wrong_before = copy.deepcopy(current)
    assert context_claim(lab, "unsupported", ["d" * 64])["stage"] is None
    assert lab[1].state["benchmark_campaigns"][current["id"]] == wrong_before
    claimed = context_claim(lab, "new-client", ["c" * 64])
    assert claimed["campaign"]["id"] == current["id"]
    assert claimed["stage"]["id"] == "cases"
    assert lab[1].state["benchmark_campaigns"][legacy["id"]] == before
    assert claimed["campaign"]["budget_ledger"]["hnpe"]["reservations"] == {
        "cases": {"gpu_seconds": 300, "allocated_gpus": 1}
    }


def test_supported_context_reconciles_same_attempt_without_second_reservation(lab):
    current = stored_new_campaign(lab)
    first = context_claim(lab, "first", ["c" * 64])
    saved = lab[1].state["benchmark_campaigns"][current["id"]]
    saved["stages"][0]["status"] = "submitting"
    reservation = copy.deepcopy(saved["budget_ledger"])
    lab[3][0] += 121
    before = copy.deepcopy(saved)
    assert context_claim(lab, "wrong", ["d" * 64])["stage"] is None
    assert lab[1].state["benchmark_campaigns"][current["id"]] == before
    recovered = context_claim(lab, "recover", ["c" * 64])
    assert recovered["stage"]["attempt"] == first["stage"]["attempt"] == 1
    assert recovered["stage"]["status"] == "submission_unknown"
    assert recovered["reconcile_required"] is True
    assert lab[1].state["benchmark_campaigns"][current["id"]]["budget_ledger"] == reservation


def test_new_connector_can_select_unchanged_legacy_campaign(lab):
    campaign = create(lab)
    frozen = copy.deepcopy(campaign["approved_adapter"])
    context = contract.campaign_execution_context_id(campaign)
    assert context == contract.checksum({
        "schema": "aws_mesouq.benchmark_context.v1", "project_id": "p",
        "science_revision": "b" * 40, "adapter_sha256": "a" * 64,
        "archive_root": None,
    })
    claimed = context_claim(lab, "legacy-context", [context])
    assert claimed["campaign"]["id"] == campaign["id"]
    assert claimed["campaign"]["approved_adapter"] == frozen


@pytest.mark.parametrize("contexts", [
    [], "c" * 64, [None], ["c" * 63], ["C" * 64],
    ["c" * 64, "c" * 64], ["d" * 64, "c" * 64],
    [f"{i:064x}" for i in range(17)],
])
def test_invalid_selectors_cannot_mutate_campaign(lab, contexts):
    campaign = create(lab)
    before = copy.deepcopy(lab[1].state["benchmark_campaigns"][campaign["id"]])
    assert_error("invalid_benchmark", lambda: context_claim(lab, "invalid", contexts))
    assert lab[1].state["benchmark_campaigns"][campaign["id"]] == before


def test_context_selector_is_signed_and_replay_body_is_exact(lab):
    current = stored_new_campaign(lab)
    body = {"project_id": "p", "connector_id": "c1", "site": "karolina",
            "request_id": "frozen", "supported_contexts": ["c" * 64]}
    body["signature"] = base64.b64encode(lab[4].sign(contract.claim_signing_bytes("alice", body))).decode()
    tampered = dict(body, supported_contexts=["d" * 64])
    before = copy.deepcopy(lab[1].state["benchmark_campaigns"][current["id"]])
    assert_error("invalid_signature", lambda: request(lab, "POST", "/v1/benchmark-connector/claim", tampered))
    assert lab[1].state["benchmark_campaigns"][current["id"]] == before
    first = request(lab, "POST", "/v1/benchmark-connector/claim", body)[1]
    assert request(lab, "POST", "/v1/benchmark-connector/claim", body)[1] == first
    assert_error("idempotency_conflict", lambda: context_claim(lab, "frozen", ["d" * 64]))
    assert first["stage"]["attempt"] == 1


def test_legacy_pending_request_replays_after_new_default_is_published(lab):
    original = create(lab)
    first = claim(lab, "legacy-pending")
    lab[1].state["projects"]["p"]["benchmark_policy"]["execution_context_id"] = "c" * 64
    create(lab, "new-default")
    assert claim(lab, "legacy-pending") == first
    assert first["campaign"]["id"] == original["id"]
    assert "execution_context_id" not in first["campaign"]["approved_adapter"]

def test_typed_gateway_context_claim_preserves_signature_and_validation(lab, monkeypatch):
    pytest.importorskip("fastapi")
    pytest.importorskip("mangum")
    import json
    from aws_publishing import handler
    from container_smoke import event
    monkeypatch.setenv("COGNITO_ISSUER", "https://issuer.invalid/pool")
    monkeypatch.setenv("COGNITO_CLIENT_ID", "application-client")
    monkeypatch.setattr(handler, "application", lambda: lab[0])
    monkeypatch.setattr(handler, "_benchmark_adapter", None)
    current = stored_new_campaign(lab)
    def invoke(contexts, request_id):
        body = {"project_id": "p", "connector_id": "c1", "site": "karolina",
                "request_id": request_id, "supported_contexts": contexts}
        body["signature"] = base64.b64encode(lab[4].sign(contract.claim_signing_bytes("alice", body))).decode()
        payload = event("/v1/benchmark-connector/claim", "POST")
        payload["headers"] = {"content-type": "application/json"}
        payload["body"] = json.dumps(body)
        payload["requestContext"]["authorizer"] = {"jwt": {"claims": {
            "sub": "alice", "iss": "https://issuer.invalid/pool",
            "client_id": "application-client", "token_use": "access",
        }}}
        return handler.handler(payload, None)
    for index, contexts in enumerate([None, [], ["c" * 64, "c" * 64], ["d" * 64, "c" * 64]]):
        before = copy.deepcopy(lab[1].state["benchmark_campaigns"][current["id"]])
        response = invoke(contexts, f"typed-invalid-{index}")
        assert response["statusCode"] == 400, response
        assert lab[1].state["benchmark_campaigns"][current["id"]] == before
    response = invoke(["c" * 64], "typed-valid")
    assert response["statusCode"] == 200, response
    assert json.loads(response["body"])["campaign"]["id"] == current["id"]

def test_stage_signature_binds_the_frozen_execution_context(lab):
    from test_benchmark import event_body
    current = stored_new_campaign(lab)
    claimed = context_claim(lab, "signed-context", ["c" * 64])
    altered = copy.deepcopy(claimed)
    altered["campaign"]["approved_adapter"]["execution_context_id"] = "d" * 64
    body = event_body(lab, altered, "changed-context", "submitting")
    before = copy.deepcopy(lab[1].state["benchmark_campaigns"][current["id"]])
    assert_error("invalid_signature", lambda: request(
        lab, "POST", f"/v1/benchmark-connector/campaigns/{current['id']}/stages/cases/events", body))
    assert lab[1].state["benchmark_campaigns"][current["id"]] == before
