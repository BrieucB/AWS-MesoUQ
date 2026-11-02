"""Protocol adversarial cases independent of native numerical algorithms."""
from copy import deepcopy
import json

import pytest
from test_benchmark import lab

from aws_mesouq import benchmark_accuracy_protocol as p
from aws_publishing.storage import DynamoStore, LazyRecords
from test_accuracy import accuracy_lab
from test_accuracy_graph import (
    after_reference, begin, claim_kind, committed_seal, native_result, prepared,
)
from test_accuracy import call, create_accuracy


def test_new_history_buckets_are_lazy_and_owner_prefix_paged():
    class Table:
        def __init__(self):
            self.calls = []
            self.items = {
                "accuracy_tasks:one:t01": {"id": "t01"},
                "accuracy_tasks:one:t02": {"id": "t02"},
                "accuracy_tasks:one:t03": {"id": "t03"},
                "accuracy_tasks:other:foreign": {"id": "foreign"},
            }
        def get_item(self, **kw):
            if kw["Key"]["sk"]["S"] == "!revision":
                return {"Item": {"version": {"N": "0"}}}
            value = self.items.get(kw["Key"]["sk"]["S"])
            return {} if value is None else {"Item": {"payload": {"S": json.dumps(value)}}}
        def query(self, **kw):
            self.calls.append(kw)
            assert kw["ConsistentRead"] is True
            prefix = kw["ExpressionAttributeValues"][":prefix"]["S"]
            after = kw.get("ExclusiveStartKey", {}).get("sk", {}).get("S")
            reverse = not kw.get("ScanIndexForward", True)
            names = sorted((k for k in self.items if k.startswith(prefix) and (after is None or (k < after if reverse else k > after))), reverse=reverse)
            limit = kw.get("Limit", 1000)
            selected = names[:limit]
            response = {"Items": [{"pk": {"S": "STATE"}, "sk": {"S": k},
                "payload": {"S": json.dumps(self.items[k])}} for k in selected]}
            if len(names) > limit:
                response["LastEvaluatedKey"] = {"pk": {"S": "STATE"}, "sk": {"S": selected[-1]}}
            return response
    table = Table()
    store = DynamoStore(table, "table")
    loaded = store.load()
    state = next(v for v in loaded if isinstance(v, dict))
    for bucket in ("accuracy_campaigns", "accuracy_tasks", "accuracy_allocations"):
        assert isinstance(state[bucket], LazyRecords)
    assert not any(call["ExpressionAttributeValues"][":prefix"]["S"].startswith("accuracy_") for call in table.calls)
    rows, cursor = store.records("accuracy_tasks", "one:", limit=2)
    assert [v["id"] for _, v in rows] == ["t01", "t02"] and cursor == "one:t02"
    rows, cursor = store.records("accuracy_tasks", "one:", limit=2, after=cursor)
    assert [v["id"] for _, v in rows] == ["t03"] and cursor is None
    rows, cursor = store.records("accuracy_tasks", "one:", limit=2, reverse=True)
    assert [v["id"] for _, v in rows] == ["t03", "t02"] and cursor == "one:t02"
    rows, cursor = store.records("accuracy_tasks", "one:", limit=2, after=cursor, reverse=True)
    assert [v["id"] for _, v in rows] == ["t01"] and cursor is None
    assert table.calls[-1]["ScanIndexForward"] is False
    with pytest.raises(ValueError):
        store.records("accuracy_tasks", "one:", after="other:foreign")


@pytest.mark.parametrize("change", [
    lambda row: row.update(allocated_gpu_seconds=0),
    lambda row: row.update(allocated_cpu_seconds=0),
    lambda row: row.update(status="failed"),
    lambda row: row.update(slurm_job_id="99999"),
    lambda row: row.update(not_submitted=True),
])
def test_closed_original_allocations_cannot_change(accuracy_lab, change):
    cid = committed_seal(accuracy_lab)
    anchor = accuracy_lab[1].state["requests"]["accuracy_seal:" + cid]["anchor"]
    status, response = call(accuracy_lab, "GET", f"/v1/projects/p/benchmark-accuracy/campaigns/{cid}/accounting")
    assert status == 200
    current = deepcopy(response["snapshot"])
    row = next(r for r in current["allocations"] if r["method"] == "hnpe")
    change(row)
    with pytest.raises(ValueError):
        p.validate_anchor(anchor, current)


def test_unknown_overhead_may_only_close_monotonically(accuracy_lab):
    cid = committed_seal(accuracy_lab)
    anchor = accuracy_lab[1].state["requests"]["accuracy_seal:" + cid]["anchor"]
    status, response = call(accuracy_lab, "GET", f"/v1/projects/p/benchmark-accuracy/campaigns/{cid}/accounting")
    assert status == 200
    current = response["snapshot"]
    p.validate_anchor(anchor, current)
    old = next(r for r in anchor["allocations"] if r["method"] == "overhead" and r["status"] not in p.TERMINAL)
    damaged = deepcopy(current)
    row = next(r for r in damaged["allocations"] if r["allocation_id"] == old["allocation_id"])
    row["reserved_gpu_seconds"] = old["reserved_gpu_seconds"] + 1
    with pytest.raises(ValueError):
        p.validate_anchor(anchor, damaged)


def test_published_curated_bytes_must_match_native_manifest(accuracy_lab):
    cid = prepared(accuracy_lab)
    claimed = claim_kind(accuracy_lab, cid, "guide_reference")
    begin(accuracy_lab, claimed)
    raw = native_result(accuracy_lab, claimed)
    artifact = {"id": "object1", "name": "stage-receipt.json", "sha256": "a"*64,
        "size_bytes": 10, "media_type": "application/json", "kind": "native-receipt"}
    raw["artifacts"] = [artifact]
    with pytest.raises(ValueError, match="Curated object bytes"):
        p.validate_result(raw, claimed["task"]["manifest"],
            native_root=accuracy_lab[5]["native_root"] + "/" + cid)


@pytest.mark.parametrize("field", ["source_binding_sha256", "scientific_request_hash",
    "transport_request_hash", "context_id", "manifest_sha256", "input_ledger_sha256"])
def test_native_result_identity_chain_cannot_be_transplanted(accuracy_lab, field):
    cid = prepared(accuracy_lab)
    claimed = claim_kind(accuracy_lab, cid, "guide_reference")
    begin(accuracy_lab, claimed)
    raw = native_result(accuracy_lab, claimed)
    raw[field] = "f"*64
    with pytest.raises(ValueError):
        p.validate_result(raw, claimed["task"]["manifest"],
            native_root=accuracy_lab[5]["native_root"] + "/" + cid)


def test_unfunded_or_active_method_cannot_reserve_another_action(accuracy_lab):
    suite = accuracy_lab
    cid = after_reference(suite)
    camp = suite[1].state["accuracy_campaigns"]["p:alice:" + cid]
    # The actual public production path admits from validated planner receipts.
    # This direct controller boundary test isolates authoritative funding.
    task = suite[0].accuracy._new_task(suite[1].state, camp,
        "hnpe_initialize", method="hnpe", factor="global")
    assert task is not None
    with pytest.raises(Exception) as error:
        suite[0].accuracy._new_task(suite[1].state, camp,
            "hnpe_initialize", method="hnpe", factor="mechanical_1")
    assert getattr(error.value, "code", None) == "accuracy_method_allocation_active"


def test_reference_window_does_not_depend_on_experimental_method_allowance(accuracy_lab):
    cid = prepared(accuracy_lab)
    camp = accuracy_lab[1].state["accuracy_campaigns"]["p:alice:" + cid]
    first = accuracy_lab[0].accuracy._window(camp, "confirmation_reference")
    camp["request"]["budget"]["method_gpu_seconds"] = 1
    assert accuracy_lab[0].accuracy._window(camp, "confirmation_reference") == first


@pytest.mark.parametrize("secret", ["nonce", "raw_nonce", "confirmation_nonce", "private_key", "lease_token"])
def test_raw_custody_or_auth_secrets_never_become_public_outputs(accuracy_lab, secret):
    cid = prepared(accuracy_lab)
    claimed = claim_kind(accuracy_lab, cid, "guide_reference")
    begin(accuracy_lab, claimed)
    raw = native_result(accuracy_lab, claimed)
    raw["outputs"] = {"guide_reference": {"directory": "/scratch/x", "sha256": "0"*64,
                                           secret: "do-not-publish"}}
    with pytest.raises(ValueError):
        p.validate_result(raw, claimed["task"]["manifest"],
            native_root=accuracy_lab[5]["native_root"] + "/" + cid)


def test_manifest_and_requested_effort_are_not_browser_defined(accuracy_lab):
    from test_accuracy import spec
    value = spec()
    value["executor"] = {"argv": ["arbitrary-code"]}
    status, _ = call(accuracy_lab, "POST", "/v1/projects/p/benchmark-accuracy/campaigns", value)
    assert status == 422


def test_context_stage_windows_are_bound_by_context_checksum(accuracy_lab):
    context = deepcopy(accuracy_lab[5])
    context["stage_reservations"] = {"confirmation_reference": 7200}
    with pytest.raises(ValueError, match="checksum"):
        p.validate_context(context)
    context["id"] = p.context_id(context)
    assert p.validate_context(context)["stage_reservations"]["confirmation_reference"] == 7200

def test_ambiguous_enabled_sources_do_not_choose_an_arbitrary_release(accuracy_lab):
    from test_accuracy import spec
    one = deepcopy(accuracy_lab[5])
    two = deepcopy(one)
    two["source_binding_sha256"] = "f"*64
    two["id"] = p.context_id(two)
    accuracy_lab[0].accuracy_policies["p"] = [one, two]
    status, response = call(accuracy_lab, "GET", "/v1/projects/p/benchmark-accuracy/capabilities")
    assert status == 200 and response["enabled"] is False
    status, response = call(accuracy_lab, "POST", "/v1/projects/p/benchmark-accuracy/campaigns", spec())
    assert status == 409


def test_lifecycle_cancellation_does_not_mutate_closed_native_costs(accuracy_lab):
    from test_accuracy import create_accuracy, claim_accuracy, started, send_event, accounting
    suite = accuracy_lab
    campaign = create_accuracy(suite)
    claimed, _ = claim_accuracy(suite)
    started(suite, claimed)
    status, _, _ = send_event(suite, claimed, "closed", "allocation_closed",
        {"scheduler_accounting": accounting()})
    assert status == 200
    row = deepcopy(next(iter(suite[1].state["accuracy_allocations"].values()))["row"])
    status, _ = call(suite, "POST",
        "/v1/projects/p/benchmark-accuracy/campaigns/" + campaign["id"] + "/cancel", {})
    assert status == 200
    assert next(iter(suite[1].state["accuracy_allocations"].values()))["row"] == row


@pytest.mark.parametrize("field,value", [
    ("maximum_artifact_bytes", True), ("maximum_artifact_bytes", 67_108_865),
    ("maximum_total_artifact_bytes", 536_870_913), ("maximum_total_artifact_bytes", 1),
    ("maximum_artifact_files", True), ("maximum_artifact_files", 65)])
def test_curated_envelope_is_strict_and_context_bound(accuracy_lab, field, value):
    context = deepcopy(accuracy_lab[5])
    context[field] = value
    context["id"] = p.context_id(context)
    with pytest.raises(ValueError):
        p.validate_context(context)


def test_latest_tasks_remain_visible_after_fifty_with_complete_paging(accuracy_lab):
    campaign = create_accuracy(accuracy_lab)
    cid = campaign["id"]
    state = accuracy_lab[1].state
    prototype = next(v for v in state["accuracy_tasks"].values() if v["campaign_id"] == cid)
    state["accuracy_tasks"] = {}
    for number in range(61):
        task = deepcopy(prototype)
        task["id"] = f"t{number:08d}-fixture"
        state["accuracy_tasks"][cid + ":" + task["id"]] = task
    status, detail = call(accuracy_lab, "GET", f"/v1/projects/p/benchmark-accuracy/campaigns/{cid}")
    assert status == 200
    initial = detail["campaign"]["tasks"]
    assert len(initial) == 50 and initial[0]["id"] == "t00000060-fixture"
    cursor = detail["campaign"]["next_task_cursor"]
    status, remainder = call(accuracy_lab, "GET",
        f"/v1/projects/p/benchmark-accuracy/campaigns/{cid}/tasks", {"cursor": cursor})
    assert status == 200 and remainder["next_cursor"] is None
    ids = [task["id"] for task in initial + remainder["tasks"]]
    assert len(ids) == len(set(ids)) == 61 and ids[-1] == "t00000000-fixture"


def test_artifact_file_total_count_and_shared_project_limits_are_independent(accuracy_lab, monkeypatch):
    from aws_publishing.api import APIError
    campaign = create_accuracy(accuracy_lab)
    state = accuracy_lab[1].state
    campaign = state["accuracy_campaigns"]["p:alice:" + campaign["id"]]
    task = next(v for v in state["accuracy_tasks"].values() if v["campaign_id"] == campaign["id"])
    context = deepcopy(accuracy_lab[5])
    context.update(maximum_artifact_bytes=8, maximum_total_artifact_bytes=12, maximum_artifact_files=2)
    context["allowed_artifacts"][task["kind"]] = ["one.npz", "two.npz", "three.npz", "notes.md", "bad.zip"]
    monkeypatch.setattr(accuracy_lab[0].accuracy, "_context", lambda _: context)
    def spec(name, size=4, media="application/octet-stream"):
        return {"name": name, "sha256": "a"*64, "size_bytes": size, "media_type": media, "kind": "posterior"}
    with pytest.raises(ValueError, match="artifact bytes"):
        accuracy_lab[0].accuracy._uploads(state, campaign, task, [spec("one.npz", 9)])
    with pytest.raises(ValueError, match="media type"):
        accuracy_lab[0].accuracy._uploads(state, campaign, task, [spec("bad.zip", media="application/zip")])
    first = accuracy_lab[0].accuracy._uploads(state, campaign, task, [spec("one.npz", 8)])
    assert len(first) == 1
    with pytest.raises(APIError) as failure:
        accuracy_lab[0].accuracy._uploads(state, campaign, task, [spec("two.npz", 5)])
    assert failure.value.code == "accuracy_storage_quota_exceeded"
    accuracy_lab[0].accuracy._uploads(state, campaign, task, [spec("two.npz", 4)])
    with pytest.raises(APIError):
        accuracy_lab[0].accuracy._uploads(state, campaign, task, [spec("three.npz", 1)])
    assert len(campaign["artifacts"]) == 2  # Capacity failures retain existing private objects.
    context.update(maximum_total_artifact_bytes=100, maximum_artifact_files=10)
    state["artifacts"]["other-campaign"] = {"project_id": "p", "campaign_id": "other",
        "size_bytes": 1_073_741_824 - 12, "reservation_released": False}
    with pytest.raises(APIError):
        accuracy_lab[0].accuracy._uploads(state, campaign, task, [spec("three.npz", 1)])
    del state["artifacts"]["other-campaign"]
    notes = accuracy_lab[0].accuracy._uploads(state, campaign, task, [spec("notes.md", media="text/markdown")])
    assert notes[0]["artifact"]["media_type"] == "text/markdown"


@pytest.mark.parametrize("mutation", ["append", "omit", "numerical_cost", "producer",
    "wrong_comparison", "recomputed", "incomplete", "foreign_binding", "foreign_current"])
def test_postexit_appendix_cannot_change_first_comparison_or_allocation_membership(accuracy_lab, mutation):
    cid = committed_seal(accuracy_lab)
    campaign = accuracy_lab[1].state["accuracy_campaigns"]["p:alice:" + cid]
    current = accuracy_lab[0].accuracy._snapshot(accuracy_lab[1].state, campaign)
    for row in current["allocations"]:
        if row["status"] not in p.TERMINAL:
            assert row["method"] == "overhead"
            row.update(status="succeeded", slurm_job_id="123456789",
                allocated_gpu_seconds=2, allocated_cpu_seconds=32, reserved_gpu_seconds=0)
    prior = deepcopy(current)
    overhead = next(row for row in prior["allocations"] if row["method"] == "overhead")
    overhead.update(status="running", allocated_gpu_seconds=None, allocated_cpu_seconds=None,
                    reserved_gpu_seconds=2)
    binding = {key: current[key] for key in ("campaign_instance_id", "scientific_request_hash",
        "transport_request_hash", "source_binding_sha256", "case_plan_sha256", "guide_case_set_sha256")}
    binding.update(policy_sha256="d"*64, target_fingerprint="e"*64)
    comparison = {"binding": binding,
        "allocation_costs": {"snapshot": prior, "snapshot_sha256": p.checksum(prior)}}
    appendix = {"schema": p.POSTEXIT_ACCOUNTING_SCHEMA, "binding": deepcopy(binding),
        "comparison_sha256": "a"*64, "prior_snapshot_sha256": p.checksum(prior),
        "final_allocation_costs": {"snapshot": deepcopy(current), "snapshot_sha256": p.checksum(current), "complete": True},
        "closed_numerical_prefix_preserved": True, "numerical_results_recomputed": False}
    assert p.validate_final_accounting(appendix, comparison, comparison_sha256="a"*64, current=current) == appendix
    final = appendix["final_allocation_costs"]["snapshot"]
    if mutation == "append":
        extra = deepcopy(final["allocations"][-1])
        extra["allocation_id"] = "foreign-allocation"
        extra["slurm_job_id"] = "987654321"
        final["allocations"].append(extra)
    elif mutation == "omit":
        final["allocations"].pop()
    elif mutation == "numerical_cost":
        row = next(row for row in final["allocations"] if row["method"] != "overhead")
        row["allocated_gpu_seconds"] += 1
        row["allocated_cpu_seconds"] += 16
    elif mutation == "producer":
        row = next(row for row in final["allocations"] if row["method"] == "overhead")
        row["slurm_job_id"] = "987654321"
    elif mutation == "wrong_comparison":
        appendix["comparison_sha256"] = "b"*64
    elif mutation == "recomputed":
        appendix["numerical_results_recomputed"] = True
    elif mutation == "incomplete":
        appendix["final_allocation_costs"]["complete"] = False
    elif mutation == "foreign_binding":
        appendix["binding"]["campaign_instance_id"] = "foreign"
    else:
        current = deepcopy(current)
        current["revision"] += 1
    appendix["final_allocation_costs"]["snapshot_sha256"] = p.checksum(final)
    with pytest.raises(ValueError):
        p.validate_final_accounting(appendix, comparison, comparison_sha256="a"*64, current=current)


def test_current_native_producer_binding_is_exact_idempotent_and_nonmutating(accuracy_lab):
    from test_accuracy import create_accuracy, claim_accuracy
    cid = create_accuracy(accuracy_lab)["id"]
    claimed, _ = claim_accuracy(accuracy_lab, campaign=cid)
    manifest = claimed["task"]["manifest"]
    original = deepcopy(manifest)
    bound = p.bind_current_allocation(manifest, "12345")
    assert manifest == original
    assert bound["revision"] == manifest["accounting"]["revision"]
    before = manifest["accounting"]["allocations"]
    after = bound["allocations"]
    assert len(before) == len(after)
    current_id = manifest["reservation"]["allocation_id"]
    for old, new in zip(before, after, strict=True):
        if old["allocation_id"] == current_id:
            assert new == {**old, "status": "running", "slurm_job_id": "12345"}
        else:
            assert p.canonical(new) == p.canonical(old)
    assert p.bind_current_allocation(manifest, "12345", snapshot=bound) == bound


@pytest.mark.parametrize("mutation", ["producer", "cost", "reservation", "status", "revision", "bool"])
def test_current_native_producer_binding_rejects_foreign_changes(accuracy_lab, mutation):
    from test_accuracy import create_accuracy, claim_accuracy
    cid = create_accuracy(accuracy_lab)["id"]
    claimed, _ = claim_accuracy(accuracy_lab, campaign=cid)
    manifest = claimed["task"]["manifest"]
    bound = p.bind_current_allocation(manifest, "12345")
    row = next(row for row in bound["allocations"]
               if row["allocation_id"] == manifest["reservation"]["allocation_id"])
    if mutation == "producer":
        row["slurm_job_id"] = "99999"
    elif mutation == "cost":
        row["allocated_gpu_seconds"] = 1
        row["allocated_cpu_seconds"] = 16
    elif mutation == "reservation":
        row["reserved_gpu_seconds"] -= 1
    elif mutation == "status":
        row["status"] = "submitted"
    elif mutation == "revision":
        bound["revision"] += 1
    else:
        row["attempt"] = True
    with pytest.raises(ValueError):
        p.bind_current_allocation(manifest, "12345", snapshot=bound)


def test_planner_method_state_is_a_thin_approved_native_descriptor(accuracy_lab):
    cid = after_reference(accuracy_lab)
    claimed = claim_kind(accuracy_lab, cid, "plan", "hnpe")
    begin(accuracy_lab, claimed)
    for state in ({"status": "no_candidate", "history": []},
                  {"directory": "/foreign/method", "sha256": "a"*64},
                  {"directory": accuracy_lab[5]["native_root"] + "/" + cid,
                   "sha256": "a"*64, "unbounded_history": []}):
        value = native_result(accuracy_lab, claimed,
            outputs={"method_states": {"hnpe": state}},
            proposal={"method": "hnpe", "action": "freeze", "effort_target": None,
                "candidate_sha256": None, "eligible_factors": [],
                "maximum_reserved_gpu_seconds": 0, "status": "no_candidate",
                "reason": "source_fixture"})
        with pytest.raises(ValueError):
            p.validate_result(value, claimed["task"]["manifest"],
                native_root=accuracy_lab[5]["native_root"] + "/" + cid)


def test_whole_accounting_response_fails_closed_without_silently_truncating_legal_history(accuracy_lab):
    cid = create_accuracy(accuracy_lab)["id"]
    store = accuracy_lab[1]
    revision, state = store.load()
    before = deepcopy(state)
    for index in range(9999):
        ident = "bounded-history-" + str(index).zfill(5) + "-" + "x"*100
        state["accuracy_allocations"][cid + ":" + ident] = {"task_id": "retained-task",
            "row": {"allocation_id": ident, "attempt": 1, "method": "overhead",
                "kind": "metrics", "factor": None, "status": "cancelled",
                "slurm_job_id": None, "allocated_gpu_seconds": 0, "allocated_cpu_seconds": 0,
                "reserved_gpu_seconds": 0, "not_submitted": True}}
    store.save(revision, before, state)
    status, response = call(accuracy_lab, "GET",
        f"/v1/projects/p/benchmark-accuracy/campaigns/{cid}/accounting")
    assert status == 413 and response["code"] == "accuracy_response_too_large"
    _, retained = store.load()
    assert len(retained["accuracy_allocations"]) == 10000


def test_campaign_pages_keep_complete_entries_and_exact_cursor_under_response_bound(accuracy_lab, monkeypatch):
    ids = {create_accuracy(accuracy_lab, "response-page-" + str(index))["id"] for index in range(6)}
    public = accuracy_lab[0].accuracy.public
    def representative_source_metadata(state, campaign, **kwargs):
        result = public(state, campaign, **kwargs)
        result["source_preserved_metadata"] = "x"*210000
        return result
    monkeypatch.setattr(accuracy_lab[0].accuracy, "public", representative_source_metadata)
    seen, cursor, pages = [], None, 0
    while True:
        query = {"limit": 50, "archived": "all"}
        if cursor:
            query["cursor"] = cursor
        status, page = call(accuracy_lab, "GET", "/v1/projects/p/benchmark-accuracy/campaigns", query)
        assert status == 200 and len(p.canonical(page)) <= p.HTTP_RESPONSE_BYTES
        assert all(len(item["source_preserved_metadata"]) == 210000 for item in page["campaigns"])
        seen.extend(item["id"] for item in page["campaigns"])
        cursor = page["next_cursor"]
        pages += 1
        if cursor is None:
            break
        assert pages < 10
    assert pages == 2 and len(seen) == len(set(seen)) == 6 and set(seen) == ids


def test_single_campaign_too_large_errors_without_an_empty_continuation_loop(accuracy_lab, monkeypatch):
    create_accuracy(accuracy_lab, "single-oversized")
    public = accuracy_lab[0].accuracy.public
    def oversized(state, campaign, **kwargs):
        return {**public(state, campaign, **kwargs), "source_preserved_metadata": "x"*p.HTTP_RESPONSE_BYTES}
    monkeypatch.setattr(accuracy_lab[0].accuracy, "public", oversized)
    status, response = call(accuracy_lab, "GET", "/v1/projects/p/benchmark-accuracy/campaigns", {"limit": 50})
    assert status == 413 and response["code"] == "accuracy_response_too_large"
    assert "next_cursor" not in response


def test_native_result_record_and_terminal_pointer_commit_atomically_and_replay_exactly(accuracy_lab, monkeypatch):
    from aws_publishing.storage import Conflict
    from test_accuracy import send_event
    suite = accuracy_lab
    cid = prepared(suite, "factored-native-result")
    claimed = claim_kind(suite, cid, "guide_reference")
    begin(suite, claimed)
    result = native_result(suite, claimed)
    tid = claimed["task"]["id"]
    key = "accuracy_native_result:" + cid + ":" + tid
    save = suite[1].save
    failed = []
    def interrupted(revision, before, after):
        if key in after["requests"] and not failed:
            failed.append(True)
            # Both proposed mutations exist only in the uncommitted snapshot.
            assert after["accuracy_tasks"][cid + ":" + tid]["status"] == "succeeded"
            assert after["accuracy_tasks"][cid + ":" + tid]["result"]["record_key"] == key
            _, durable = suite[1].load()
            assert durable["requests"].get(key) is None
            assert durable["accuracy_tasks"][cid + ":" + tid]["status"] == "publishing"
            raise Conflict("synthetic CAS interruption before commit")
        return save(revision, before, after)
    monkeypatch.setattr(suite[1], "save", interrupted)
    status, response, frozen = send_event(suite, claimed, "factored-result", "result", {"result": result})
    assert status == 200 and failed == [True]
    _, state = suite[1].load()
    task = state["accuracy_tasks"][cid + ":" + tid]
    campaign = state["accuracy_campaigns"]["p:alice:" + cid]
    assert task["result"] == {"record_key": key, "sha256": p.checksum(result)}
    assert len(p.canonical(task["result"])) < 300
    assert state["requests"][key]["result"] == result
    assert len(p.canonical(state["requests"][key])) <= 262144
    assert suite[0].accuracy._read_native_result(state, campaign, task) == result
    assert len([name for name in state["requests"] if name == key]) == 1
    status, replay = call(suite, "POST",
        f"/v1/benchmark-connector/accuracy/campaigns/{cid}/tasks/{tid}/events", frozen)
    assert status == 200 and replay == response
    retained = deepcopy(state["requests"][key])
    altered = deepcopy(result)
    altered["scientific_status"] = "changed-outcome"
    with pytest.raises(Exception) as conflict:
        suite[0].accuracy._retain_native_result(state, campaign, task, altered)
    assert getattr(conflict.value, "code", None) == "accuracy_native_result_conflict"
    assert state["requests"][key] == retained


@pytest.mark.parametrize("field", ["owner_sub", "project_id", "context_id", "manifest_sha256",
                                   "source_binding_sha256", "sha256", "result"])
def test_internal_native_result_reconstruction_rejects_transplanted_or_changed_records(accuracy_lab, field):
    from test_accuracy_graph import finish
    suite = accuracy_lab
    cid = prepared(suite, "native-audit-" + field)
    claimed = claim_kind(suite, cid, "guide_reference")
    finish(suite, claimed)
    _, state = suite[1].load()
    task = state["accuracy_tasks"][cid + ":" + claimed["task"]["id"]]
    campaign = state["accuracy_campaigns"]["p:alice:" + cid]
    record = state["requests"][task["result"]["record_key"]]
    if field == "result":
        record["result"]["scientific_status"] = "tampered"
    else:
        record[field] = "foreign"
    with pytest.raises(ValueError):
        suite[0].accuracy._read_native_result(state, campaign, task)


def test_large_legal_manifest_and_native_result_use_independent_safe_records(accuracy_lab):
    from test_accuracy import send_event
    suite = accuracy_lab
    cid = prepared(suite, "large-native-record")
    revision, state = suite[1].load()
    before = deepcopy(state)
    campaign = state["accuracy_campaigns"]["p:alice:" + cid]
    task = state["accuracy_tasks"][cid + ":" + campaign["active_tasks"][0]]
    task["manifest"]["inputs"]["synthetic_bounded_history"] = "x"*160000
    task["manifest_sha256"] = p.checksum(task["manifest"])
    p.validate_manifest(task["manifest"])
    suite[1].save(revision, before, state)
    claimed = claim_kind(suite, cid, "guide_reference")
    begin(suite, claimed)
    result = native_result(suite, claimed, qualification={"synthetic_source_metadata": "y"*150000})
    status, response, frozen = send_event(suite, claimed, "large-result", "result", {"result": result})
    assert status == 200
    _, state = suite[1].load()
    task = state["accuracy_tasks"][cid + ":" + claimed["task"]["id"]]
    record = state["requests"][task["result"]["record_key"]]
    assert len(p.canonical(task)) + len(p.canonical(record["result"])) > 300000
    assert len(p.canonical(task)) < 300000 and len(p.canonical(record)) <= 262144
    assert suite[0].accuracy._read_native_result(state,
        state["accuracy_campaigns"]["p:alice:" + cid], task) == result
    # Events retain the immutable body hash/ACK, never a second full result.
    assert all("synthetic_source_metadata" not in p.canonical(value).decode()
               for value in state["events"].values())
    assert all(len(p.canonical(value)) <= 300000 for bucket in state.values()
               if isinstance(bucket, dict) for value in bucket.values())
