"""Adversarial metadata graph tests with a scripted trusted-native producer.

No scientific algorithm or AWS service runs here. Real SCI validation/receipts
and performance require the separate source-bound Karolina acceptance gate.
"""
from copy import deepcopy
import base64
import hashlib
import itertools
import json

import pytest
from test_benchmark import lab

from aws_mesouq import benchmark_accuracy_protocol as protocol
from test_accuracy import (
    accuracy_lab, call, claim_accuracy, create_accuracy, event_body, send_event, spec,
)

_jobs = itertools.count(50000)
_requests = itertools.count(1)


def claim_kind(suite, cid, kind, method=None):
    campaign = suite[1].state["accuracy_campaigns"]["p:alice:" + cid]
    tasks = [suite[1].state["accuracy_tasks"][cid + ":" + t] for t in campaign["active_tasks"]]
    selected = next(t for t in tasks if t["kind"] == kind and (method is None or t["method"] == method))
    excluded = [t["id"] for t in tasks if t["id"] != selected["id"]]
    claimed, _ = claim_accuracy(suite, f"graph-claim-{next(_requests)}", cid, excluded)
    assert claimed["task"]["id"] == selected["id"]
    return claimed


def begin(suite, claimed, native_state="COMPLETED", *, elapsed=1):
    job = str(next(_jobs))
    status, _, _ = send_event(suite, claimed, "submit-intent", "submitting", {
        "marker": claimed["task"]["marker"], "script_sha256": "f"*64, "intent_sha256": "1"*64})
    assert status == 200
    status, _, _ = send_event(suite, claimed, "submit-ack", "submitted", {"slurm_job_id": job})
    assert status == 200
    status, _, _ = send_event(suite, claimed, "native-closed", "allocation_closed", {
        "scheduler_accounting": {"source": "sacct", "site": "karolina", "slurm_job_id": job,
            "state": native_state, "elapsed_seconds": elapsed, "allocated_gpus": 1, "cpu_seconds": 16 * elapsed}})
    assert status == 200
    return job


def receipt(suite, claimed, role="wrapper"):
    task = claimed["task"]
    return {"directory": suite[5]["native_root"] + "/" + claimed["campaign"]["id"] + "/" + task["id"] + "/" + role,
            "sha256": protocol.checksum({"task": task["id"], "role": role})}


def native_result(suite, claimed, *, outputs=None, pins=None, proposal=None,
                  scientific_status=None, qualification=None, artifacts=None):
    task, manifest = claimed["task"], claimed["task"]["manifest"]
    desc = receipt(suite, claimed)
    refs = artifacts or []
    curated = [{k: v for k, v in item.items() if k != "id"} for item in refs]
    result = {
        "schema": protocol.RESULT_SCHEMA, "campaign_instance_id": claimed["campaign"]["id"],
        "task_id": task["id"], "attempt": task["attempt"], "kind": task["kind"],
        "context_id": manifest["context_id"], "source_binding_sha256": manifest["source_binding_sha256"],
        "scientific_request_hash": manifest["scientific_request_hash"],
        "transport_request_hash": manifest["transport_request_hash"],
        "manifest_sha256": task["manifest_sha256"], "slurm_job_id": task["slurm_job_id"],
        "input_ledger_sha256": manifest["input_ledger_sha256"],
        "native_receipt": desc, "input_pins": deepcopy(manifest["pins"]),
        "output_pins": {**deepcopy(manifest["pins"]), **(pins or {})},
        "outputs": deepcopy(outputs or {}),
        "source_validation": {
            "schema": "aws_mesouq.benchmark_accuracy_native_validation.v1", "verified": True,
            "producer_job_id": task["slurm_job_id"], "receipt_sha256": desc["sha256"],
            "manifest_sha256": task["manifest_sha256"],
            "verified_inputs_sha256": protocol.checksum(manifest["inputs"]),
            "artifact_manifest_sha256": protocol.checksum(sorted(curated, key=lambda a: a["name"])),
            "checks": sorted(protocol.required_validation_checks(task["kind"]))},
        "proposal": deepcopy(proposal), "scientific_status": scientific_status,
        "qualification": deepcopy(qualification), "artifacts": refs}
    return result


def finish(suite, claimed, **kwargs):
    begin(suite, claimed)
    result = native_result(suite, claimed, **kwargs)
    status, response, frozen = send_event(suite, claimed, "native-result", "result", {"result": result})
    assert status == 200, response
    return response, frozen


def prepared(suite, nonce="graph"):
    campaign = create_accuracy(suite, nonce)
    claimed = claim_kind(suite, campaign["id"], "guide_preparation")
    finish(suite, claimed, outputs={"guide_preparation": receipt(suite, claimed, "guide"),
        "confirmation_commitment": "5"*64}, pins={
        "case_plan_sha256": "1"*64, "guide_case_set_sha256": "2"*64,
        "normalization_sha256": "3"*64, "validation_sha256": "4"*64})
    return campaign["id"]


def after_reference(suite, nonce="graph"):
    cid = prepared(suite, nonce)
    claimed = claim_kind(suite, cid, "guide_reference")
    finish(suite, claimed)
    return cid


def proposal(method, action, *, target=None, candidate=None, eligible=None, maximum=0,
             status=None, reason=None):
    return {"method": method, "action": action, "effort_target": target,
        "candidate_sha256": candidate, "eligible_factors": eligible or [],
        "maximum_reserved_gpu_seconds": maximum, "status": status, "reason": reason}


def methods_frozen(suite):
    cid = after_reference(suite)
    for method in ("hnpe", "tmcmc"):
        claimed = claim_kind(suite, cid, "plan", method)
        finish(suite, claimed, outputs={"method_states": {
            method: receipt(suite, claimed, method + "-method-state")}},
            proposal=proposal(method, "freeze", status="no_candidate", reason="source_terminal_fixture"))
    return cid


def seal_result(suite, claimed, *, anchor=None):
    anchor = protocol.bind_current_allocation(claimed["task"]["manifest"],
        claimed["task"]["slurm_job_id"]) if anchor is None else anchor
    desc = receipt(suite, claimed, "method-seal")
    return native_result(suite, claimed,
        outputs={"method_seal": desc, "sealed_allocation_anchor": anchor},
        pins={"method_seal_sha256": desc["sha256"],
              "sealed_allocation_anchor_sha256": protocol.checksum(anchor)})


def committed_seal(suite):
    cid = methods_frozen(suite)
    claimed = claim_kind(suite, cid, "method_seal")
    begin(suite, claimed)
    result = seal_result(suite, claimed)
    status, response, _ = send_event(suite, claimed, "seal-result", "result", {"result": result})
    assert status == 200, response
    return cid


@pytest.mark.parametrize("cleanup_overrun,inconsistent_budget_flag,capsule_fault",
    [(False, False, None), (True, False, None), (True, True, None)] +
    [(False, False, fault) for fault in ("foreign_producer", "prior_anchor", "membership",
        "cost_total", "case_denominator", "bool_revision", "curation_digest", "display_verdict",
        "source_extra", "raw_secret", "oversized_summary")])
def test_full_source_unmet_metadata_graph_has_all256_and_closed_final_costs(accuracy_lab, monkeypatch,
        cleanup_overrun, inconsistent_budget_flag, capsule_fault):
    suite = accuracy_lab
    if cleanup_overrun:
        import sys
        import test_accuracy
        original_spec = test_accuracy.spec
        original_begin = begin
        def bounded_spec(nonce="native-control-test"):
            value = original_spec(nonce)
            value["budget"]["method_gpu_seconds"] = 60
            return value
        def cleanup_begin(suite, claimed, native_state="COMPLETED", *, elapsed=1):
            return original_begin(suite, claimed, native_state,
                elapsed=61 if claimed["task"]["kind"] == "guide_preparation" else elapsed)
        monkeypatch.setattr(test_accuracy, "spec", bounded_spec)
        monkeypatch.setattr(sys.modules[__name__], "begin", cleanup_begin)
    cid = committed_seal(suite)
    bank = claim_kind(suite, cid, "confirmation_preparation")
    finish(suite, bank, outputs={"confirmation_bank": receipt(suite, bank, "confirmation-bank")},
        pins={"opening_id": "opening-one"})
    reference = claim_kind(suite, cid, "confirmation_reference")
    finish(suite, reference)
    for method in ("hnpe", "tmcmc"):
        outcome = claim_kind(suite, cid, "record_confirmation_outcome", method)
        assert outcome["task"]["manifest"]["inputs"]["outcome_reason"] == "no_candidate"
        finish(suite, outcome, outputs={"confirmation_outcomes": {
            method: receipt(suite, outcome, "first-" + method)}})
    final = claim_kind(suite, cid, "confirmation_compile")
    begin(suite, final)
    status, ledger = call(suite, "GET", f"/v1/projects/p/benchmark-accuracy/campaigns/{cid}/accounting")
    assert status == 200
    current = ledger["snapshot"]
    manifest = final["task"]["manifest"]
    prior = protocol.bind_current_allocation(manifest, final["task"]["slurm_job_id"])
    binding = {name: current[name] for name in ("campaign_instance_id", "case_plan_sha256",
        "guide_case_set_sha256", "scientific_request_hash", "source_binding_sha256", "transport_request_hash")}
    binding.update(policy_sha256=suite[5]["policy_sha256"], target_fingerprint="f"*64)
    comparison = {"schema": "synthetic-source-comparison", "binding": binding,
        "evaluation": deepcopy(manifest["request"]["evaluation"]),
        "scientific_status": "completed_with_target_unmet",
        "methods": {method: {"verdict": {
            "counts": {"passed": 256, "failed": 0} if cleanup_overrun else {"passed": 0, "failed": 256},
            "status": "attained" if cleanup_overrun else "unmet",
            "observed_attainment": cleanup_overrun, "population_qualified": cleanup_overrun},
            "accuracy_target_within_budget": bool(inconsistent_budget_flag and method == "hnpe"),
            "within_method_ceiling": not (cleanup_overrun and method == "hnpe") or inconsistent_budget_flag,
            "within_factor_ceilings": True, "guide_status": "no_candidate"} for method in ("hnpe", "tmcmc")},
        "allocation_costs": {"snapshot": prior, "snapshot_sha256": protocol.checksum(prior), "complete": False}}
    qualification = protocol.source_qualification(comparison)
    content = protocol.canonical(comparison)
    spec = {"name": "comparison.json", "sha256": hashlib.sha256(content).hexdigest(),
        "size_bytes": len(content), "media_type": "application/json", "kind": "numerical-result"}
    appendix = {"schema": protocol.POSTEXIT_ACCOUNTING_SCHEMA, "binding": deepcopy(binding),
        "comparison_sha256": spec["sha256"], "prior_snapshot_sha256": protocol.checksum(prior),
        "final_allocation_costs": {"snapshot": current, "snapshot_sha256": protocol.checksum(current), "complete": True},
        "closed_numerical_prefix_preserved": True, "numerical_results_recomputed": False}
    appendix_content = protocol.canonical(appendix)
    appendix_spec = {"name": "allocation-accounting-final.json",
        "sha256": hashlib.sha256(appendix_content).hexdigest(), "size_bytes": len(appendix_content),
        "media_type": "application/json", "kind": "allocation_accounting_final"}
    totals = protocol.closed_resource_totals(current, manifest["request"])
    def source_desc(value):
        return {"file": value["name"], "sha256": value["sha256"],
            "size_bytes": value["size_bytes"], "content_type": value["media_type"], "kind": value["kind"]}
    summary = {"schema": protocol.DISPLAY_SCHEMA, "binding": binding,
        "comparison": source_desc(spec), "accounting": source_desc(appendix_spec),
        "scientific_status": comparison["scientific_status"], "scenario": manifest["request"]["scenario"],
        "evaluation": comparison["evaluation"], "methods": comparison["methods"],
        "downloads": [source_desc(spec), source_desc(appendix_spec)],
        "allocation_costs": {"schema": protocol.RESOURCE_SUMMARY_SCHEMA,
            "snapshot_sha256": protocol.checksum(current), "snapshot_revision": current["revision"],
            **totals, "complete": True, "reference_cost_included_in_method_budget": False,
            "measurement": "controller-pinned cumulative native allocated GPU/CPU seconds",
            "process_clocks_are_budget_authority": False},
        "numerical_statistics_recomputed": False}
    summary_content = protocol.canonical(summary) + b"\n"
    summary_spec = {"name": "display-summary.json", "sha256": hashlib.sha256(summary_content).hexdigest(),
        "size_bytes": len(summary_content), "media_type": "application/json", "kind": "display_summary"}
    curated = [*summary["downloads"], source_desc(summary_spec)]
    membership = [{key: row[key] for key in ("allocation_id", "attempt", "slurm_job_id", "method", "kind", "factor")}
        for row in sorted(current["allocations"], key=lambda row: (row["allocation_id"], row["attempt"]))]
    capsule = {"schema": protocol.CAPSULE_SCHEMA, "binding": binding,
        "comparison": source_desc(spec), "accounting": source_desc(appendix_spec),
        "summary": source_desc(summary_spec), "prior_snapshot_sha256": protocol.checksum(prior),
        "final_snapshot_sha256": protocol.checksum(current), "final_snapshot_revision": current["revision"],
        "allocation_membership_sha256": protocol.checksum(membership), "allocation_count": len(membership),
        "comparison_producer": {"allocation_id": final["task"]["allocation_id"],
            "attempt": final["task"]["attempt"], "slurm_job_id": final["task"]["slurm_job_id"]},
        "allocation_totals": totals, "source_assessments": comparison["methods"],
        "scientific_status": comparison["scientific_status"], "confirmation_case_count": 256,
        "curation": {"excludes_validation_capsule_itself": True,
            "artifacts_sha256": protocol.checksum(curated), "file_count": len(curated),
            "size_bytes": sum(item["size_bytes"] for item in curated)},
        "closed_numerical_prefix_preserved": True, "numerical_results_recomputed": False}
    if capsule_fault == "foreign_producer":
        capsule["comparison_producer"]["slurm_job_id"] = "99999999"
    elif capsule_fault == "prior_anchor":
        capsule["prior_snapshot_sha256"] = "0"*64
    elif capsule_fault == "membership":
        capsule["allocation_membership_sha256"] = "0"*64
    elif capsule_fault == "cost_total":
        capsule["allocation_totals"] = deepcopy(capsule["allocation_totals"])
        capsule["allocation_totals"]["methods"]["hnpe"]["allocated_gpu_seconds"] += 1
    elif capsule_fault == "case_denominator":
        capsule["confirmation_case_count"] = 255
    elif capsule_fault == "bool_revision":
        capsule["final_snapshot_revision"] = True
    elif capsule_fault == "curation_digest":
        capsule["curation"]["artifacts_sha256"] = "0"*64
    elif capsule_fault == "display_verdict":
        capsule["source_assessments"] = deepcopy(capsule["source_assessments"])
        capsule["source_assessments"]["hnpe"]["verdict"]["status"] = "forged"
    elif capsule_fault == "source_extra":
        capsule["caller_approved"] = True
    elif capsule_fault == "raw_secret":
        capsule["source_assessments"]["hnpe"]["verdict"]["raw_nonce"] = "not-a-real-secret"
    elif capsule_fault == "oversized_summary":
        summary["limitations"] = ["x"*protocol.DISPLAY_BYTES]
        summary_content = protocol.canonical(summary) + b"\n"
        summary_spec.update(sha256=hashlib.sha256(summary_content).hexdigest(), size_bytes=len(summary_content))
        capsule["summary"] = source_desc(summary_spec)
    capsule_content = protocol.canonical(capsule) + b"\n"
    capsule_spec = {"name": "validation-capsule.json", "sha256": hashlib.sha256(capsule_content).hexdigest(),
        "size_bytes": len(capsule_content), "media_type": "application/json", "kind": "validation_capsule"}
    specs = [spec, appendix_spec, summary_spec, capsule_spec]
    contents = [content, appendix_content, summary_content, capsule_content]
    blobs = {}
    monkeypatch.setattr(suite[2], "verify", lambda key, artifact: key in blobs and
        len(blobs[key]) == artifact["size_bytes"] and hashlib.sha256(blobs[key]).hexdigest() == artifact["sha256"])
    reads = []
    def bounded_source_metadata_only(key, artifact, maximum):
        assert artifact["name"] in {"display-summary.json", "validation-capsule.json"}
        assert maximum == protocol.DISPLAY_BYTES
        if len(blobs[key]) > maximum:
            raise ValueError("Bounded source metadata exceeds read limit")
        reads.append(artifact["name"])
        return json.loads(blobs[key])
    monkeypatch.setattr(suite[2], "read_json", bounded_source_metadata_only)
    status, uploaded, _ = send_event(suite, final, "upload", "artifact_upload", {"artifacts": specs})
    assert status == 200
    ids = [value["artifact_id"] for value in uploaded["uploads"]]
    aid, accounting_id, summary_id, capsule_id = ids
    record = suite[1].state["artifacts"][aid]
    for artifact_id, blob in zip(ids, contents, strict=True):
        blobs[suite[1].state["artifacts"][artifact_id]["storage_key"]] = blob
    status, _, _ = send_event(suite, final, "verify", "artifact_status", {"artifact_ids": ids})
    assert status == 200
    final_receipt = {"directory": receipt(suite, final)["directory"], "sha256": appendix_spec["sha256"]}
    result = native_result(suite, final, outputs={"result_artifact_id": aid,
        "accounting_artifact_id": accounting_id, "summary_artifact_id": summary_id,
        "validation_capsule_artifact_id": capsule_id, "final_accounting_receipt": final_receipt},
        scientific_status="completed_with_target_unmet", qualification=qualification,
        artifacts=[{"id": artifact_id, **artifact_spec} for artifact_id, artifact_spec in zip(ids, specs, strict=True)])
    status, response, frozen = send_event(suite, final, "final-result", "result", {"result": result})
    if capsule_fault:
        assert status == 422, response
        assert suite[1].state["accuracy_campaigns"]["p:alice:" + cid]["result"] is None
        assert len(reads) <= 2
        return
    if inconsistent_budget_flag:
        assert status == 422 and "overrun" in response["message"]
        retained = suite[1].state["accuracy_campaigns"]["p:alice:" + cid]
        assert retained["result"] is None and retained["budget_exceeded"] is True
        assert retained["accounting"]["hnpe"]["settled_gpu_seconds"] == 61
        return
    assert status == 200, response
    assert response["campaign"]["budget_exceeded"] is cleanup_overrun
    assert response["campaign"]["result"]["budget_exceeded"] is cleanup_overrun
    assert response["campaign"]["status"] == "completed"
    assert response["campaign"]["scientific_status"] == "completed_with_target_unmet"
    assert response["campaign"]["result"]["qualification"] == qualification
    assert response["campaign"]["result"]["qualification"]["hnpe"]["verdict"]["observed_attainment"] is cleanup_overrun
    assert response["campaign"]["result"]["qualification"]["hnpe"]["verdict"]["population_qualified"] is cleanup_overrun
    assert response["campaign"]["result"]["qualification"]["hnpe"]["accuracy_target_within_budget"] is False
    assert response["campaign"]["archive"]["available"] is True
    assert all(a["unknown_allocations"] == 0 for a in response["campaign"]["accounting"]["methods"].values())
    status, ledger = call(suite, "GET", f"/v1/projects/p/benchmark-accuracy/campaigns/{cid}/accounting")
    assert status == 200 and ledger["sha256"] == protocol.checksum(ledger["snapshot"])
    assert all(row["allocated_gpu_seconds"] is not None for row in ledger["snapshot"]["allocations"])
    assert any(row["kind"] == "metrics" for row in ledger["snapshot"]["allocations"])
    status, public = call(suite, "GET", f"/v1/projects/p/benchmark-accuracy/campaigns/{cid}/results")
    assert status == 200 and protocol.source_qualification(public["result"]) == qualification
    assert public["accounting"]["methods"]["hnpe"]["budget_exceeded"] is cleanup_overrun
    assert public["raw_result_artifact"]["id"] == aid
    assert "comparison.json" not in reads and "allocation-accounting-final.json" not in reads
    # Same immutable outcome cannot be executed/published under a new event identity.
    status, _, _ = send_event(suite, final, "different-first-outcome", "result", {"result": result})
    assert status == 409
    status, download = call(suite, "GET", f"/v1/projects/p/benchmark-accuracy/campaigns/{cid}/artifacts/{aid}")
    assert status == 200 and download["artifact"]["sha256"] == spec["sha256"]
    blobs[record["storage_key"]] = b"wrong-object"
    status, _ = call(suite, "GET", f"/v1/projects/p/benchmark-accuracy/campaigns/{cid}/artifacts/{aid}")
    assert status == 409
    blobs[record["storage_key"]] = content
    status, _ = call(suite, "POST", f"/v1/projects/p/benchmark-accuracy/campaigns/{cid}/archive", {})
    assert status == 200
    status, current = call(suite, "GET", f"/v1/projects/p/benchmark-accuracy/campaigns/{cid}/accounting")
    assert status == 200
    campaign = suite[1].state["accuracy_campaigns"]["p:alice:" + cid]
    archived = []
    def tag_after_commit(key):
        assert suite[1].state["accuracy_campaigns"]["p:alice:" + cid]["archive"]["status"] == "verified"
        archived.append(key)
    monkeypatch.setattr(suite[2], "archived", tag_after_commit)
    archive_receipt = {
        "schema": "aws_mesouq.benchmark_accuracy_archive.v1", "campaign_id": cid,
        "context_id": suite[5]["id"], "source_binding_sha256": suite[5]["source_binding_sha256"],
        "scientific_request_hash": campaign["scientific_request_hash"],
        "transport_request_hash": campaign["configuration_hash"],
        "allocation_snapshot_sha256": current["sha256"],
        "method_seal_sha256": campaign["pins"]["method_seal_sha256"],
        "manifest_sha256": "b"*64, "source_snapshot_sha256": "c"*64,
        "copy_snapshot_sha256": "c"*64, "file_count": 100, "total_bytes": 10000,
        "closed_pair_count": 10, "independent_copy": True, "no_hardlinks": True, "aliases_closed": True,
        "first_comparison_sha256": spec["sha256"], "final_accounting_sha256": appendix_spec["sha256"]}
    def archive_body(event, receipt):
        body = {"project_id": "p", "campaign_id": cid, "connector_id": "c1",
                "event_id": event, "receipt": receipt}
        body["signature"] = base64.b64encode(suite[4].sign(protocol.archive_signing_bytes("alice", body))).decode()
        return body
    path = f"/v1/benchmark-connector/accuracy/campaigns/{cid}/archive"
    forged = deepcopy(archive_receipt)
    forged["allocation_snapshot_sha256"] = "f"*64
    status, _ = call(suite, "POST", path, archive_body("wrong-ledger", forged))
    assert status == 422
    good = archive_body("verified-copy", archive_receipt)
    status, response = call(suite, "POST", path, good)
    assert status == 200 and response["campaign"]["archive"]["status"] == "verified"
    status, replay = call(suite, "POST", path, good)
    assert status == 200 and replay == response
    assert len(archived) <= 1


def test_method_seal_rejects_bare_terminal_declarations(accuracy_lab):
    cid = methods_frozen(accuracy_lab)
    claimed = claim_kind(accuracy_lab, cid, "method_seal")
    begin(accuracy_lab, claimed)
    raw = native_result(accuracy_lab, claimed, outputs={"method_states": {
        "hnpe": {"status": "frozen"}, "tmcmc": {"status": "frozen"}}})
    status, _, _ = send_event(accuracy_lab, claimed, "bare", "result", {"result": raw})
    assert status == 422
    campaign = accuracy_lab[1].state["accuracy_campaigns"]["p:alice:" + cid]
    assert campaign["confirmation_authorization"] is None
    assert not any(t["kind"] == "confirmation_preparation" for t in accuracy_lab[1].state["accuracy_tasks"].values())


def test_seal_cannot_drop_any_original_allocation(accuracy_lab):
    cid = methods_frozen(accuracy_lab)
    claimed = claim_kind(accuracy_lab, cid, "method_seal")
    begin(accuracy_lab, claimed)
    anchor = deepcopy(claimed["task"]["manifest"]["accounting"])
    omitted = next(row for row in anchor["allocations"] if row["method"] == "hnpe")
    anchor["allocations"].remove(omitted)
    raw = seal_result(accuracy_lab, claimed, anchor=anchor)
    status, _, _ = send_event(accuracy_lab, claimed, "omit", "result", {"result": raw})
    assert status == 422
    assert accuracy_lab[1].state["accuracy_campaigns"]["p:alice:" + cid]["confirmation_authorization"] is None


def test_confirmation_opening_pin_cannot_appear_before_sealing(accuracy_lab):
    campaign = create_accuracy(accuracy_lab)
    claimed = claim_kind(accuracy_lab, campaign["id"], "guide_preparation")
    begin(accuracy_lab, claimed)
    raw = native_result(accuracy_lab, claimed,
        outputs={"guide_preparation": receipt(accuracy_lab, claimed, "guide"),
                 "confirmation_commitment": "5"*64},
        pins={"case_plan_sha256": "1"*64, "guide_case_set_sha256": "2"*64,
              "normalization_sha256": "3"*64, "validation_sha256": "4"*64,
              "opening_id": "premature"})
    status, _, _ = send_event(accuracy_lab, claimed, "premature-open", "result", {"result": raw})
    assert status == 422


def test_foreign_native_receipt_path_and_missing_role_validation_rejected(accuracy_lab):
    cid = prepared(accuracy_lab)
    claimed = claim_kind(accuracy_lab, cid, "guide_reference")
    begin(accuracy_lab, claimed)
    raw = native_result(accuracy_lab, claimed)
    raw["native_receipt"]["directory"] = "/scratch/accuracy/another-campaign/receipt"
    status, _, _ = send_event(accuracy_lab, claimed, "foreign", "result", {"result": raw})
    assert status == 422
    raw = native_result(accuracy_lab, claimed)
    raw["source_validation"]["checks"].remove("reference")
    status, _, _ = send_event(accuracy_lab, claimed, "no-reference-proof", "result", {"result": raw})
    assert status == 422


def test_one_native_producer_cannot_back_two_campaigns(accuracy_lab):
    one = create_accuracy(accuracy_lab, "one")
    two = create_accuracy(accuracy_lab, "two")
    first = claim_kind(accuracy_lab, one["id"], "guide_preparation")
    second = claim_kind(accuracy_lab, two["id"], "guide_preparation")
    for claimed in (first, second):
        status, _, _ = send_event(accuracy_lab, claimed, "intent", "submitting",
            {"script_sha256": "f"*64, "intent_sha256": "1"*64, "marker": claimed["task"]["marker"]})
        assert status == 200
    status, _, _ = send_event(accuracy_lab, first, "job", "submitted", {"slurm_job_id": "90001"})
    assert status == 200
    status, _, _ = send_event(accuracy_lab, second, "wrong-job", "submitted", {"slurm_job_id": "90001"})
    assert status == 409
    assert accuracy_lab[1].state["accuracy_tasks"][two["id"] + ":" + second["task"]["id"]]["slurm_job_id"] is None


def test_partial_native_advance_retains_requested_and_actual_progress(accuracy_lab):
    suite = accuracy_lab
    cid = after_reference(suite)
    planned = claim_kind(suite, cid, "plan", "hnpe")
    init = {"factor": "global", "operation": "initialize", "to_update": None,
            "expected_state_sha256": None, "generation": 0}
    finish(suite, planned, proposal=proposal("hnpe", "train_factor",
        target=128, eligible=[init], maximum=1800))
    initialized = claim_kind(suite, cid, "hnpe_initialize")
    state0 = "8"*64
    finish(suite, initialized, outputs={"progress": {
        "factor": "global", "state_sha256": state0, "generation": 0, "optimizer_updates": 0,
        "candidate_sha256": None, "native_receipt": receipt(suite, initialized, "state0")}})
    planned = claim_kind(suite, cid, "plan", "hnpe")
    advance = {"factor": "global", "operation": "advance", "to_update": 2048,
               "expected_state_sha256": state0, "generation": 0}
    finish(suite, planned, proposal=proposal("hnpe", "train_factor",
        target=2048, eligible=[advance], maximum=1799))
    advanced = claim_kind(suite, cid, "hnpe_advance")
    begin(suite, advanced)
    progress = {"factor": "global", "state_sha256": "9"*64, "generation": 1,
        "optimizer_updates": 1920, "candidate_sha256": "a"*64,
        "native_receipt": receipt(suite, advanced, "state1"),
        "best_validation_nll": 0.1, "validation_nll": 0.12}
    bad = native_result(suite, advanced, outputs={"progress": progress})
    status, _, _ = send_event(suite, advanced, "no-stop-proof", "result", {"result": bad})
    assert status == 422
    progress["stop_reason"] = "validation_stopped"
    good = native_result(suite, advanced, outputs={"progress": progress})
    status, response, _ = send_event(suite, advanced, "native-stop", "result", {"result": good})
    assert status == 200, response
    assert response["task"]["effort_target"] == 2048
    assert response["task"]["progress"]["optimizer_updates"] == 1920
    assert response["task"]["progress"]["stop_reason"] == "validation_stopped"


def test_sealed_original_rows_remain_exact_and_unknown_overhead_is_not_zero(accuracy_lab):
    cid = committed_seal(accuracy_lab)
    campaign = accuracy_lab[1].state["accuracy_campaigns"]["p:alice:" + cid]
    sealed = accuracy_lab[1].state["requests"]["accuracy_seal:" + cid]["anchor"]
    status, current = call(accuracy_lab, "GET", f"/v1/projects/p/benchmark-accuracy/campaigns/{cid}/accounting")
    assert status == 200
    protocol.validate_anchor(sealed, current["snapshot"])
    assert any(row["method"] == "overhead" and row["allocated_gpu_seconds"] is None
               for row in current["snapshot"]["allocations"])
    changed = deepcopy(current["snapshot"])
    target = next(row for row in changed["allocations"] if row["method"] == "hnpe")
    target["allocated_gpu_seconds"] += 1
    with pytest.raises(ValueError):
        protocol.validate_anchor(sealed, changed)


@pytest.mark.parametrize("method", ["hnpe", "tmcmc"])
@pytest.mark.parametrize("reservation", [59, 60, 61, 119, 120])
def test_source_method_proposal_never_stages_an_unfundable_native_hold(accuracy_lab, method, reservation):
    suite = accuracy_lab
    cid = after_reference(suite, "floor-" + method + "-" + str(reservation))
    claimed = claim_kind(suite, cid, "plan", method)
    begin(suite, claimed)
    if method == "hnpe":
        value = proposal(method, "train_factor", maximum=reservation, eligible=[{
            "factor": "global", "operation": "initialize", "expected_state_sha256": None,
            "generation": 0, "to_update": None}])
    else:
        value = proposal(method, "fresh_tmcmc_attempt", target=2048, maximum=reservation)
    result = native_result(suite, claimed, proposal=value)
    _, before = suite[1].load()
    status, response, frozen = send_event(suite, claimed, "source-funded-proposal", "result", {"result": result})
    _, after = suite[1].load()
    if reservation < 60:
        assert status == 422 and "60-second" in response["message"]
        assert len(after["accuracy_tasks"]) == len(before["accuracy_tasks"])
        assert len(after["accuracy_allocations"]) == len(before["accuracy_allocations"])
    else:
        assert status == 200
        campaign = after["accuracy_campaigns"]["p:alice:" + cid]
        tasks = [after["accuracy_tasks"][cid + ":" + ident] for ident in campaign["active_tasks"]]
        kind = "hnpe_initialize" if method == "hnpe" else "guide_tmcmc_attempt"
        native = next(task for task in tasks if task["kind"] == kind)
        assert native["manifest"]["reservation"]["gpu_seconds"] == reservation
        assert native["submission_intent"] is None and native["slurm_job_id"] is None


def test_sealed_authenticated_candidate_with_59_seconds_routes_to_source_first_no_run(accuracy_lab):
    # Scripted source metadata only. The separate SCI oracle must independently
    # prove its immutable physically_unfundable outcome and all256 statuses.
    suite = accuracy_lab
    request = spec("sealed-positive-unfundable")
    request["budget"]["method_gpu_seconds"] = 120
    status, created = call(suite, "POST", "/v1/projects/p/benchmark-accuracy/campaigns", request)
    assert status == 202
    cid = created["campaign"]["id"]
    prep = claim_kind(suite, cid, "guide_preparation")
    begin(suite, prep, elapsed=61)
    raw = native_result(suite, prep, outputs={"guide_preparation": receipt(suite, prep, "guide"),
        "confirmation_commitment": "5"*64}, pins={"case_plan_sha256": "1"*64,
        "guide_case_set_sha256": "2"*64, "normalization_sha256": "3"*64, "validation_sha256": "4"*64})
    status, _, _ = send_event(suite, prep, "prepared-positive", "result", {"result": raw})
    assert status == 200
    finish(suite, claim_kind(suite, cid, "guide_reference"))
    for method in ("hnpe", "tmcmc"):
        claimed = claim_kind(suite, cid, "plan", method)
        finish(suite, claimed, outputs={"method_states": {method: receipt(suite, claimed, method+"-state")}},
            proposal=proposal(method, "freeze", candidate="c"*64 if method=="hnpe" else None,
                target=512 if method=="hnpe" else None, status="frozen" if method=="hnpe" else "no_candidate",
                reason="scripted-source-terminal"))
    seal = claim_kind(suite, cid, "method_seal")
    begin(suite, seal)
    status, _, _ = send_event(suite, seal, "sealed-positive", "result", {"result": seal_result(suite, seal)})
    assert status == 200
    bank = claim_kind(suite, cid, "confirmation_preparation")
    finish(suite, bank, outputs={"confirmation_bank": receipt(suite, bank, "bank")}, pins={"opening_id": "opened-once"})
    _, state = suite[1].load()
    campaign = state["accuracy_campaigns"]["p:alice:" + cid]
    assert campaign["methods"]["hnpe"]["candidate_sha256"] == "c"*64
    assert campaign["pins"]["method_seal_sha256"] is not None
    rows = [state["accuracy_allocations"][cid+":"+task["allocation_id"]]["row"]
        for task in state["accuracy_tasks"].values() if task["campaign_id"]==cid]
    assert not any(row["kind"]=="confirmation_hnpe_inference" for row in rows)
    no_run = [task for task in state["accuracy_tasks"].values()
        if task["campaign_id"]==cid and task["kind"]=="record_confirmation_outcome" and task["method"]=="hnpe"]
    assert len(no_run)==1 and no_run[0]["manifest"]["inputs"]["outcome_reason"]=="insufficient_method_budget"
    assert no_run[0]["manifest"]["reservation"]["gpu_seconds"]>=60
    own = [row for row in rows if row["allocation_id"]==no_run[0]["allocation_id"]][0]
    assert own["method"]=="overhead" and own["kind"]=="metrics"
    account = campaign["accounting"]["hnpe"]
    assert account["settled_gpu_seconds"]==61 and account["reserved_gpu_seconds"]==0
    assert campaign["request"]["budget"]["method_gpu_seconds"]-account["settled_gpu_seconds"]==59
