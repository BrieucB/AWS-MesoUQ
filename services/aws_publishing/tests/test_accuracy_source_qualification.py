"""Synthetic source-policy admission tests; no AWS/scientific execution."""
from copy import deepcopy
import json

import pytest

from aws_mesouq import benchmark_accuracy_protocol as protocol
from aws_publishing.accuracy import AccuracyApplication
from test_benchmark import lab
from test_accuracy import (
    accuracy_lab, call, claim_accuracy, create_accuracy, send_event, spec,
)
from test_accuracy_graph import begin, native_result, receipt


def policy(suite):
    return suite[0].accuracy_policies["p"][0]


def revoke(suite):
    # Simulate removal of trusted qualification after a task was allocated.
    policy(suite).pop("source_qualification")


def test_legacy_absence_does_not_change_context_bytes_or_identity(accuracy_lab):
    value = deepcopy(accuracy_lab[5])
    value.pop("source_qualification")
    value["id"] = protocol.context_id(value)
    before = protocol.canonical(value)
    validated = protocol.validate_context(value)
    assert "source_qualification" not in validated
    assert protocol.canonical(validated) == before
    assert protocol.context_id(validated) == value["id"]
    assert not protocol.source_policy_qualified(validated)
    qualified = deepcopy(value)
    qualified["source_qualification"] = deepcopy(accuracy_lab[5]["source_qualification"])
    assert protocol.context_id(qualified) != value["id"]


@pytest.mark.parametrize("field,value", [
    ("science_revision", "0" * 40),
    ("source_binding_sha256", "0" * 64),
    ("semantic_policy_sha256", "0" * 64),
])
def test_record_requires_exact_source_tuple_and_semantic_policy(accuracy_lab, field, value):
    context = deepcopy(accuracy_lab[5])
    assert protocol.source_policy_qualified(context)
    context["source_qualification"][field] = value
    context["id"] = protocol.context_id(context)
    assert protocol.validate_context(context) == context
    assert not protocol.source_policy_qualified(context)


@pytest.mark.parametrize("mutation", ["missing", "null", "extra", "bad_schema", "bad_commit", "bad_sha"])
def test_qualification_schema_is_closed(accuracy_lab, mutation):
    context = deepcopy(accuracy_lab[5])
    record = context["source_qualification"]
    if mutation == "missing":
        del record["semantic_policy_sha256"]
    elif mutation == "null":
        context["source_qualification"] = None
    elif mutation == "extra":
        record["expected_hash"] = "d" * 64
    elif mutation == "bad_schema":
        record["schema"] = "other"
    elif mutation == "bad_commit":
        record["science_revision"] = "a" * 39
    else:
        record["semantic_policy_sha256"] = True
    context["id"] = protocol.context_id(context)
    with pytest.raises(ValueError):
        protocol.validate_context(context)
    assert not protocol.source_policy_qualified(context)


@pytest.mark.parametrize("mutation", ["absent", "revision", "closure", "policy"])
def test_create_rejects_before_any_campaign_task_or_reservation_write(accuracy_lab, mutation):
    suite = accuracy_lab
    current = policy(suite)
    if mutation == "absent":
        current.pop("source_qualification")
    else:
        key = {"revision": "science_revision", "closure": "source_binding_sha256",
               "policy": "semantic_policy_sha256"}[mutation]
        current["source_qualification"][key] = "0" * (40 if mutation == "revision" else 64)
    before = deepcopy(suite[1].state)
    status, response = call(suite, "POST", "/v1/projects/p/benchmark-accuracy/campaigns", spec())
    assert (status, response["code"]) == (409, "accuracy_source_unqualified")
    assert suite[1].state == before
    assert not suite[1].state["accuracy_campaigns"]
    assert not suite[1].state["accuracy_tasks"]
    assert not suite[1].state["accuracy_allocations"]


def test_client_cannot_supply_qualification_or_expected_pin(accuracy_lab):
    before = deepcopy(accuracy_lab[1].state)
    request = spec()
    request["source_qualification"] = deepcopy(accuracy_lab[5]["source_qualification"])
    status, _ = call(accuracy_lab, "POST", "/v1/projects/p/benchmark-accuracy/campaigns", request)
    assert status == 422
    assert accuracy_lab[1].state == before


def test_bad_old_context_does_not_block_one_qualified_context(accuracy_lab):
    bad = deepcopy(accuracy_lab[5])
    bad.pop("source_qualification")
    bad["id"] = protocol.context_id(bad)
    accuracy_lab[0].accuracy_policies["p"].append(protocol.validate_context(bad))
    created = create_accuracy(accuracy_lab)
    assert created["execution_context_id"] == accuracy_lab[5]["id"]
    assert len(accuracy_lab[1].state["accuracy_allocations"]) == 1


@pytest.mark.parametrize("mode", ["missing_record", "disabled", "no_contexts"])
def test_idempotent_replay_does_not_requalify_or_allocate(accuracy_lab, mode):
    one = create_accuracy(accuracy_lab)
    if mode == "missing_record":
        revoke(accuracy_lab)
    elif mode == "disabled":
        policy(accuracy_lab)["enabled"] = False
    else:
        accuracy_lab[0].accuracy_policies = {}
    before = deepcopy(accuracy_lab[1].state)
    replay = create_accuracy(accuracy_lab)
    assert replay == one
    assert accuracy_lab[1].state == before
    changed = spec()
    changed["seed"] += 1
    status, response = call(accuracy_lab, "POST", "/v1/projects/p/benchmark-accuracy/campaigns", changed)
    assert (status, response["code"]) == (409, "idempotency_conflict")


def test_new_task_block_does_not_touch_existing_ledger_or_history(accuracy_lab):
    created = create_accuracy(accuracy_lab)
    state = accuracy_lab[1].state
    campaign = state["accuracy_campaigns"]["p:alice:" + created["id"]]
    revoke(accuracy_lab)
    before = deepcopy(state)
    assert accuracy_lab[0].accuracy._new_task(state, campaign, "plan", method="hnpe") is None
    expected = deepcopy(before)
    expected["accuracy_campaigns"]["p:alice:" + created["id"]]["status"] = "blocked"
    assert state == expected


def test_result_and_terminal_costs_settle_but_no_unqualified_child_is_reserved(accuracy_lab):
    suite = accuracy_lab
    created = create_accuracy(suite)
    claimed, _ = claim_accuracy(suite)
    begin(suite, claimed, elapsed=7)
    revoke(suite)
    result = native_result(suite, claimed,
        outputs={"guide_preparation": receipt(suite, claimed, "guide"),
                 "confirmation_commitment": "5" * 64},
        pins={"case_plan_sha256": "1" * 64, "guide_case_set_sha256": "2" * 64,
              "normalization_sha256": "3" * 64, "validation_sha256": "4" * 64})
    status, response, event = send_event(suite, claimed, "settled-result", "result", {"result": result})
    assert status == 200, response
    state = suite[1].state
    campaign = state["accuracy_campaigns"]["p:alice:" + created["id"]]
    assert campaign["accounting"]["hnpe"]["settled_gpu_seconds"] == 7
    assert campaign["accounting"]["hnpe"]["settled_cpu_seconds"] == 112
    assert campaign["accounting"]["hnpe"]["reserved_gpu_seconds"] == 0
    assert campaign["accounting"]["hnpe"]["unknown_allocations"] == 0
    assert len(state["accuracy_tasks"]) == len(state["accuracy_allocations"]) == 1
    assert response["task"]["status"] == "succeeded"
    assert campaign["phase_receipts"]["guide_preparation"] == result["outputs"]["guide_preparation"]
    before = deepcopy(state)
    # Exact accepted event replay survives missing qualification and cannot charge twice.
    status, _ = call(suite, "POST",
        "/v1/benchmark-connector/accuracy/campaigns/" + created["id"]
        + "/tasks/" + claimed["task"]["id"] + "/events", event)
    assert status == 200
    assert suite[1].state == before


def test_unqualified_ready_task_not_leased_but_submitted_recovery_is_leased(accuracy_lab):
    suite = accuracy_lab
    created = create_accuracy(suite)
    revoke(suite)
    empty, _ = claim_accuracy(suite)
    assert empty["task"] is None
    # Put back the independently supplied synthetic record to establish a prior submission.
    policy(suite)["source_qualification"] = deepcopy(suite[5]["source_qualification"])
    claimed, _ = claim_accuracy(suite, "qualified")
    from test_accuracy import started
    started(suite, claimed)
    revoke(suite)
    suite[3][0] += 1000
    recovered, _ = claim_accuracy(suite, "reconcile")
    assert recovered["task"]["id"] == claimed["task"]["id"]
    assert recovered["task"]["reconcile_only"] is True
    assert recovered["task"]["slurm_job_id"] == "101"
    # Metadata reads and cancellation remain available for legacy history.
    status, _ = call(suite, "GET", "/v1/projects/p/benchmark-accuracy/campaigns/" + created["id"])
    assert status == 200
    status, _ = call(suite, "POST", "/v1/projects/p/benchmark-accuracy/campaigns/" + created["id"] + "/cancel", {})
    assert status == 200


def test_codec_preserves_legacy_absence_and_qualified_record(accuracy_lab):
    from aws_publishing.accuracy_policy_env import encode_policies, decode_policies
    qualified = deepcopy(accuracy_lab[5])
    legacy = deepcopy(qualified)
    legacy.pop("source_qualification")
    legacy["enabled"] = False
    legacy["id"] = protocol.context_id(legacy)
    original = {"p": [legacy, qualified]}
    decoded = decode_policies(encode_policies(original))
    assert protocol.canonical(decoded) == protocol.canonical(original)
    assert "source_qualification" not in decoded["p"][0]
    validated = AccuracyApplication(None, None, accuracy_policies=decoded)
    assert protocol.canonical(validated.accuracy_policies["p"]) == protocol.canonical(
        sorted(original["p"], key=lambda c: c["id"]))


def test_valid_absent_record_history_recovers_settles_and_archives(accuracy_lab, monkeypatch):
    suite = accuracy_lab
    legacy = suite[5]
    legacy.pop("source_qualification")
    legacy["id"] = protocol.context_id(legacy)
    assert protocol.validate_context(legacy) == legacy
    suite[0].accuracy_policies["p"] = [deepcopy(legacy)]
    # Seed exactly the pre-admission legacy behavior, without changing any
    # manifest/context fields after creation. Restore the real guard immediately.
    with monkeypatch.context() as seed:
        seed.setattr(protocol, "source_policy_qualified", lambda context: True)
        created = create_accuracy(suite, "valid-historical-context")
        claimed, _ = claim_accuracy(suite, "historical-claim")
        from test_accuracy import started
        started(suite, claimed)
    assert not protocol.source_policy_qualified(legacy)
    suite[3][0] += 1000
    recovered, _ = claim_accuracy(suite, "historical-recovery")
    assert recovered["task"]["reconcile_only"] is True
    assert recovered["task"]["manifest"]["context_id"] == legacy["id"]
    from test_accuracy import accounting
    status, _, _ = send_event(suite, recovered, "historical-closed", "allocation_closed",
                             {"scheduler_accounting": accounting(seconds=7)})
    assert status == 200
    result = native_result(suite, recovered,
        outputs={"guide_preparation": receipt(suite, recovered, "guide"),
                 "confirmation_commitment": "5" * 64},
        pins={"case_plan_sha256": "1" * 64, "guide_case_set_sha256": "2" * 64,
              "normalization_sha256": "3" * 64, "validation_sha256": "4" * 64})
    status, response, _ = send_event(suite, recovered, "historical-result", "result", {"result": result})
    assert status == 200, response
    state = suite[1].state
    campaign = state["accuracy_campaigns"]["p:alice:" + created["id"]]
    assert len(state["accuracy_tasks"]) == len(state["accuracy_allocations"]) == 1
    assert campaign["accounting"]["hnpe"]["settled_gpu_seconds"] == 7
    assert campaign["accounting"]["hnpe"]["settled_cpu_seconds"] == 112
    assert campaign["accounting"]["hnpe"]["unknown_allocations"] == 0
    assert protocol.validate_context(suite[0].accuracy._context(campaign)) == legacy
    path = "/v1/projects/p/benchmark-accuracy/campaigns/" + created["id"]
    status, _ = call(suite, "GET", path)
    assert status == 200
    status, _ = call(suite, "POST", path + "/cancel", {})
    assert status == 200
    status, response = call(suite, "POST", path + "/archive", {})
    assert status == 200, response
    assert response["campaign"]["archive"]["status"] == "requested"
    assert "source_qualification" not in suite[0].accuracy._context(campaign)
