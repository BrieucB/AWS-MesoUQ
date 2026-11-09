"""Capability-gated factor waves and CPU-reference joins, using synthetic receipts."""
from copy import deepcopy

import pytest
from test_benchmark import lab
from test_accuracy import accuracy_lab, call, claim_accuracy, send_event, spec
from test_accuracy_graph import (
    after_reference, begin, claim_kind, finish, native_result, prepared, proposal, receipt,
)
from aws_mesouq import benchmark_accuracy_protocol as protocol


OPTIMIZATIONS = {
    "fused_factor_lifecycle_v1", "bounded_factor_parallelism_v1",
    "independent_guide_reference_v1", "cpu_reference_resources_v1",
}


def optimized(suite, capabilities=OPTIMIZATIONS):
    context = suite[5]
    context["capabilities"] = sorted(set(context["capabilities"]) | set(capabilities))
    if "cpu_reference_resources_v1" in capabilities:
        context["reference_resources"] = {"native_cpus": 16, "native_gpus": 0}
    context["id"] = protocol.context_id(context)
    suite[0].accuracy_policies["p"] = [protocol.validate_context(context)]
    return suite


def tasks(suite, cid, kind=None, method=None):
    campaign = suite[1].state["accuracy_campaigns"]["p:alice:" + cid]
    values = [suite[1].state["accuracy_tasks"][cid + ":" + ident] for ident in campaign["active_tasks"]]
    return [t for t in values if (kind is None or t["kind"] == kind)
            and (method is None or t["method"] == method)]


def eligible(count=7, operation="initialize_advance"):
    return [{"factor": factor, "operation": operation, "to_update": 1920 if operation != "initialize" else None,
             "expected_state_sha256": None, "generation": 0}
            for factor in protocol.FACTORS[:count]]



@pytest.mark.parametrize("budget,maximum,expected", [
    (14400, 1800, [1800, 1800, 1800]),
    (1200, 500, [500, 500, 199]),
    (1000, 500, [500, 499]),
])
def test_parallel_wave_atomically_clips_each_independent_hold(accuracy_lab, monkeypatch, budget, maximum, expected):
    import test_accuracy
    original = spec
    monkeypatch.setattr(test_accuracy, "spec", lambda nonce: {
        **original(nonce), "budget": {**original(nonce)["budget"], "method_gpu_seconds": budget}})
    suite = optimized(accuracy_lab, {"fused_factor_lifecycle_v1", "bounded_factor_parallelism_v1"})
    cid = after_reference(suite)
    planned = claim_kind(suite, cid, "plan", "hnpe")
    finish(suite, planned, proposal=proposal("hnpe", "train_factor",
        target=1920, eligible=eligible(), maximum=maximum))
    children = tasks(suite, cid, "hnpe_initialize_advance")
    assert len(children) == len(expected) <= 3
    assert len({t["factor"] for t in children}) == len(children)
    assert [t["manifest"]["reservation"]["gpu_seconds"] for t in children] == expected
    campaign = suite[1].state["accuracy_campaigns"]["p:alice:" + cid]
    assert campaign["accounting"]["hnpe"]["reserved_gpu_seconds"] == sum(expected)
    assert sum(expected) + campaign["accounting"]["hnpe"]["settled_gpu_seconds"] <= budget
    assert all(t["manifest"]["reservation"]["native_gpus"] == 1 for t in children)


def test_wave_waits_for_every_child_publication_before_replanning(accuracy_lab):
    suite = optimized(accuracy_lab, {"fused_factor_lifecycle_v1", "bounded_factor_parallelism_v1"})
    cid = after_reference(suite)
    finish(suite, claim_kind(suite, cid, "plan", "hnpe"),
        proposal=proposal("hnpe", "train_factor", target=1920, eligible=eligible(), maximum=1800))
    for index in range(3):
        child = claim_kind(suite, cid, "hnpe_initialize_advance")
        finish(suite, child, outputs={"progress": {
            "factor": child["task"]["factor"], "state_sha256": str(index + 1) * 64,
            "generation": 1, "optimizer_updates": 1920, "candidate_sha256": "a" * 64,
            "native_receipt": receipt(suite, child, "generation1")}})
        assert len(tasks(suite, cid, "plan", "hnpe")) == (1 if index == 2 else 0)
    rows = suite[0].accuracy._rows(suite[1].state,
        suite[1].state["accuracy_campaigns"]["p:alice:" + cid])
    assert len([row for row in rows if row["kind"] == "hnpe_initialize_advance"]) == 3
    assert all(row["allocated_gpu_seconds"] == 1 for row in rows
               if row["kind"] == "hnpe_initialize_advance")


def test_legacy_context_still_schedules_one_initialize(accuracy_lab):
    cid = after_reference(accuracy_lab)
    finish(accuracy_lab, claim_kind(accuracy_lab, cid, "plan", "hnpe"),
        proposal=proposal("hnpe", "train_factor", target=1920,
                          eligible=eligible(operation="initialize"), maximum=1800))
    assert len(tasks(accuracy_lab, cid, "hnpe_initialize")) == 1


def test_unapproved_fused_action_is_rejected_without_a_child(accuracy_lab):
    cid = after_reference(accuracy_lab)
    planned = claim_kind(accuracy_lab, cid, "plan", "hnpe")
    begin(accuracy_lab, planned)
    result = native_result(accuracy_lab, planned,
        proposal=proposal("hnpe", "train_factor", target=1920, eligible=eligible(1), maximum=1800))
    status, _, _ = send_event(accuracy_lab, planned, "unapproved-fusion", "result", {"result": result})
    assert status == 422
    assert not tasks(accuracy_lab, cid, "hnpe_initialize_advance")


def test_reference_and_training_start_from_frozen_common_banks(accuracy_lab):
    suite = optimized(accuracy_lab)
    cid = prepared(suite)
    assert len(tasks(suite, cid, "guide_reference")) == 1
    assert len(tasks(suite, cid, "plan")) == 2
    reference = tasks(suite, cid, "guide_reference")[0]
    assert reference["manifest"]["reservation"] == {
        "allocation_id": reference["allocation_id"], "native_cpus": 16,
        "native_gpus": 0, "gpu_seconds": 0, "allocated_seconds": 14400}
    finish(suite, claim_kind(suite, cid, "plan", "hnpe"),
        proposal=proposal("hnpe", "train_factor", target=1920, eligible=eligible(), maximum=1800))
    assert len(tasks(suite, cid, "hnpe_initialize_advance")) == 3
    assert len(tasks(suite, cid, "guide_reference")) == 1


def test_cpu_reference_closes_with_zero_gpu_cost_and_joins_pending_guide(accuracy_lab):
    suite = optimized(accuracy_lab)
    cid = prepared(suite)
    finish(suite, claim_kind(suite, cid, "plan", "tmcmc"),
        proposal=proposal("tmcmc", "fresh_tmcmc_attempt", target=2048, maximum=600))
    sampler = claim_kind(suite, cid, "guide_tmcmc_attempt")
    finish(suite, sampler, pins={"candidate_sha256": "c" * 64})
    assert not tasks(suite, cid, "guide_compile")
    campaign = suite[1].state["accuracy_campaigns"]["p:alice:" + cid]
    assert campaign["methods"]["tmcmc"]["status"] == "awaiting_reference"
    reference = claim_kind(suite, cid, "guide_reference")
    for kind, payload in [
        ("submitting", {"marker": reference["task"]["marker"], "script_sha256": "f" * 64, "intent_sha256": "1" * 64}),
        ("submitted", {"slurm_job_id": "9000001"}),
        ("allocation_closed", {"scheduler_accounting": {"source": "sacct", "site": "karolina",
            "slurm_job_id": "9000001", "state": "COMPLETED", "elapsed_seconds": 90,
            "allocated_gpus": 0, "allocated_cpus": 16, "cpu_seconds": 16 * 90}}),
    ]:
        status, response, _ = send_event(suite, reference, "cpu-" + kind, kind, payload)
        assert status == 200, response
    result = native_result(suite, reference)
    status, response, _ = send_event(suite, reference, "cpu-result", "result", {"result": result})
    assert status == 200, response
    compiled = tasks(suite, cid, "guide_compile", "tmcmc")
    assert len(compiled) == 1
    assert compiled[0]["manifest"]["inputs"]["phase_receipts"]["guide_reference"] == result["native_receipt"]
    assert compiled[0]["manifest"]["inputs"]["method_result"] == receipt(suite, sampler)
    campaign = suite[1].state["accuracy_campaigns"]["p:alice:" + cid]
    assert campaign["accounting"]["reference"]["settled_gpu_seconds"] == 0
    assert campaign["accounting"]["reference"]["settled_cpu_seconds"] == 1440


def test_sealing_waits_for_reference_even_when_both_methods_have_no_candidate(accuracy_lab):
    suite = optimized(accuracy_lab, {"independent_guide_reference_v1"})
    cid = prepared(suite)
    for method in ("hnpe", "tmcmc"):
        planned = claim_kind(suite, cid, "plan", method)
        finish(suite, planned, outputs={"method_states": {method: receipt(suite, planned, "state")}},
            proposal=proposal(method, "freeze", status="no_candidate", reason="source_terminal_fixture"))
    assert not tasks(suite, cid, "method_seal")
    finish(suite, claim_kind(suite, cid, "guide_reference"))
    assert len(tasks(suite, cid, "method_seal")) == 1


@pytest.mark.parametrize("gpus", [0, 1])
def test_exact_expected_gpu_count_is_required_for_cpu_reference_accounting(gpus):
    value = {"source": "sacct", "site": "karolina", "slurm_job_id": "123",
             "state": "COMPLETED", "elapsed_seconds": 60, "allocated_gpus": gpus, "allocated_cpus": 16, "cpu_seconds": 960}
    if gpus == 0:
        assert protocol.validate_accounting(value, job_id="123", task_status="running",
            expected_cpus=16, expected_gpus=0, allow_preallocation=False) == (0, 960)
    else:
        with pytest.raises(ValueError):
            protocol.validate_accounting(value, job_id="123", task_status="running",
                expected_cpus=16, expected_gpus=0, allow_preallocation=False)


@pytest.mark.parametrize("evidence", [{}, {"allocated_cpus": 0}, {"allocated_cpus": True}])
def test_zero_elapsed_cpu_task_still_requires_real_allocated_cpu_evidence(evidence):
    value = {"source": "sacct", "site": "karolina", "slurm_job_id": "123",
             "state": "COMPLETED", "elapsed_seconds": 0, "allocated_gpus": 0, "cpu_seconds": 0, **evidence}
    with pytest.raises(ValueError):
        protocol.validate_accounting(value, job_id="123", task_status="running",
            expected_cpus=16, expected_gpus=0, allow_preallocation=False)


def test_worker_cpu_reference_funds_walltime_without_fabricating_a_gpu_hold():
    from types import SimpleNamespace
    from aws_mesouq.benchmark_accuracy_worker import AccuracyWorker
    observed = []
    def prepare(claim, window):
        observed.append(window)
        return {"script_path": "/scratch/native/campaign/task/run.sh",
                "script_sha256": "a" * 64, "intent_sha256": "b" * 64,
                "marker": "marker", "window": window}
    worker = SimpleNamespace(_validate_existing=lambda entry: {"native_root": "/scratch/native"},
        _reconcile_before_new_work=lambda entry: False,
        contexts=SimpleNamespace(require_new_work=lambda context_id: None),
        executor=SimpleNamespace(prepare=prepare), save=lambda: None)
    entry = {"context_id": "synthetic-context", "claim": {"campaign": {"id": "campaign"}, "task": {
        "marker": "marker", "manifest": {"reservation": {
            "native_gpus": 0, "native_cpus": 16, "gpu_seconds": 0, "allocated_seconds": 120}}}}}
    AccuracyWorker._prepare(worker, entry)
    assert entry["phase"] == "prepared"
    assert observed[0]["allocated_seconds"] == 120
    assert entry["claim"]["task"]["manifest"]["reservation"]["gpu_seconds"] == 0


def test_independent_reference_keeps_actual_gpu_cost_under_legacy_allocation_policy(accuracy_lab):
    suite = optimized(accuracy_lab, {"independent_guide_reference_v1"})
    cid = prepared(suite)
    assert len(tasks(suite, cid, "plan")) == 2
    reference = claim_kind(suite, cid, "guide_reference")
    reservation = reference["task"]["manifest"]["reservation"]
    assert reservation["native_gpus"] == 1
    assert reservation["gpu_seconds"] == 14400
    begin(suite, reference, elapsed=90)
    result = native_result(suite, reference)
    status, response, _ = send_event(suite, reference, "reference-truthful-cost", "result", {"result": result})
    assert status == 200, response
    campaign = suite[1].state["accuracy_campaigns"]["p:alice:" + cid]
    assert campaign["accounting"]["reference"]["settled_gpu_seconds"] == 90
    assert campaign["accounting"]["reference"]["settled_cpu_seconds"] == 1440
    assert campaign["accounting"]["hnpe"]["settled_gpu_seconds"] == 1


def test_wave_honors_each_source_factor_reservation_bound(accuracy_lab):
    suite = optimized(accuracy_lab, {"fused_factor_lifecycle_v1", "bounded_factor_parallelism_v1"})
    cid = after_reference(suite)
    alternatives = eligible(3)
    for row, maximum in zip(alternatives, (600, 1200, 900)):
        row["maximum_reserved_gpu_seconds"] = maximum
    finish(suite, claim_kind(suite, cid, "plan", "hnpe"),
        proposal=proposal("hnpe", "train_factor", target=1920, eligible=alternatives, maximum=1200))
    assert [task["manifest"]["reservation"]["gpu_seconds"]
            for task in tasks(suite, cid, "hnpe_initialize_advance")] == [600, 1200, 900]


def test_one_failed_parallel_child_cancels_all_owned_siblings_without_releasing_unknown_costs(accuracy_lab):
    suite = optimized(accuracy_lab, {"fused_factor_lifecycle_v1", "bounded_factor_parallelism_v1"})
    cid = after_reference(suite)
    finish(suite, claim_kind(suite, cid, "plan", "hnpe"),
        proposal=proposal("hnpe", "train_factor", target=1920, eligible=eligible(3), maximum=1800))
    identities = [task["id"] for task in tasks(suite, cid, "hnpe_initialize_advance")]
    children = []
    for index, identity in enumerate(identities):
        excluded = [task["id"] for task in tasks(suite, cid) if task["id"] != identity]
        child, _ = claim_accuracy(suite, "wave-child-" + str(index), cid, excluded)
        for kind, payload in [
            ("submitting", {"marker": child["task"]["marker"], "script_sha256": "f" * 64, "intent_sha256": "1" * 64}),
            ("submitted", {"slurm_job_id": str(9100000 + index)}),
        ]:
            status, response, _ = send_event(suite, child, "wave-" + str(index) + "-" + kind, kind, payload)
            assert status == 200, response
        children.append(child)
    def account(index, state, elapsed):
        return {"source": "sacct", "site": "karolina", "slurm_job_id": str(9100000 + index),
            "state": state, "elapsed_seconds": elapsed, "allocated_gpus": 1, "cpu_seconds": 16 * elapsed}
    status, response, _ = send_event(suite, children[0], "one-child-failed", "failed",
        {"not_submitted": False, "scheduler_accounting": account(0, "FAILED", 10),
         "failure": {"code": "synthetic_native_failure", "message": "Synthetic owned child failure"}})
    assert status == 200, response
    assert response["campaign"]["status"] == "cancelling"
    for index in (1, 2):
        campaign = suite[1].state["accuracy_campaigns"]["p:alice:" + cid]
        assert campaign["accounting"]["hnpe"]["reserved_gpu_seconds"] == (3 - index) * 1800
        assert campaign["accounting"]["hnpe"]["unknown_allocations"] == 3 - index
        assert not response["campaign"]["archive"]["available"]
        excluded = [task["id"] for task in tasks(suite, cid) if task["id"] != identities[index]]
        refreshed, _ = claim_accuracy(suite, "wave-cancel-" + str(index), cid, excluded)
        status, response, _ = send_event(suite, refreshed, "child-cancelled-" + str(index), "cancelled",
            {"not_submitted": False, "scheduler_accounting": account(index, "CANCELLED", 11 + index)})
        assert status == 200, response
    campaign = suite[1].state["accuracy_campaigns"]["p:alice:" + cid]
    assert campaign["status"] == "failed"
    assert campaign["accounting"]["hnpe"]["unknown_allocations"] == 0
    assert campaign["accounting"]["hnpe"]["reserved_gpu_seconds"] == 0
    assert campaign["accounting"]["hnpe"]["settled_gpu_seconds"] == 36
    assert not tasks(suite, cid)
