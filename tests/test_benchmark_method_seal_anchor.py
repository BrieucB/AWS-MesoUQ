"""Method-seal source/wire ABI regressions; all state and jobs are synthetic."""
from copy import deepcopy
import importlib.util
from pathlib import Path
import sys
from types import ModuleType, SimpleNamespace

import pytest
from aws_mesouq import benchmark_accuracy_protocol as protocol

SOURCE = Path(__file__).parents[1]/"src/aws_mesouq/adapters/benchmark_accuracy.py"
spec = importlib.util.spec_from_file_location("method_seal_adapter_under_test", SOURCE)
native = importlib.util.module_from_spec(spec)
spec.loader.exec_module(native)


def request():
    return protocol.wire.validate_create({
        "schema_version": 2, "idempotency_key": "synthetic-method-seal-contract",
        "scenario": {"family": "baseline", "scale": 1}, "seed": 1000,
        "controls": {"hnpe": {"width": 128, "depth": 2, "learning_rate": .0003,
            "batch_size": 256, "max_updates": 3840, "training_budget": 1800},
            "tmcmc": {"population_size": 2048, "target_cov": .6,
                "proposal_scale": 1, "burn_in": 7, "max_generations": 1000}},
        "evaluation": {"cohort": "central_90", "guide_case_count": 8,
            "confirmation_case_count": 256, "posterior_draws": 2048},
        "budget": {"mode": "accuracy_target", "method_gpu_seconds": 14400},
        "accuracy_target": {"profile_id": "w2-rms0p1-central90-2048-244of256-v1",
            "effort_policy_id": "bounded-effort-ladders-v1",
            "reference_profile_id": "independent-resolution-relative-reference-v1"}})


def source_contract_anchor(snapshot):
    # Independent source ABI fixture, not the adapter helper under test. The
    # allocated source proof obtains this object from the genuine SCI validator.
    return {"snapshot": deepcopy(snapshot), "snapshot_sha256": protocol.checksum(snapshot),
        "allocation_ids": sorted(f"{r['allocation_id']}:{r['attempt']}" for r in snapshot["allocations"]),
        "revision": snapshot["revision"]}


def fixture_snapshot():
    req = request()
    rows = []
    for method in ("hnpe", "tmcmc"):
        rows.append({"allocation_id": method+"-guide", "attempt": 1,
            "method": method, "kind": "guide_preparation" if method == "hnpe" else "guide_tmcmc_attempt",
            "factor": None, "status": "failed", "slurm_job_id": "91001" if method == "hnpe" else "91002",
            "allocated_gpu_seconds": 14400, "allocated_cpu_seconds": 57600,
            "reserved_gpu_seconds": 0, "not_submitted": False})
    rows.append({"allocation_id": "seal-control", "attempt": 1, "method": "overhead",
        "kind": "metrics", "factor": None, "status": "claimed", "slurm_job_id": None,
        "allocated_gpu_seconds": None, "allocated_cpu_seconds": None,
        "reserved_gpu_seconds": 60, "not_submitted": False})
    return {"schema": protocol.SNAPSHOT_SCHEMA, "campaign_instance_id": "synthetic-seal-campaign",
        "scientific_request_hash": protocol.wire.scientific_request_checksum(req),
        "transport_request_hash": protocol.wire.transport_request_checksum(req),
        "source_binding_sha256": "f"*64, "case_plan_sha256": "c"*64,
        "guide_case_set_sha256": "d"*64, "revision": 1, "allocations": rows}


def dispatch_fixture(tmp_path, monkeypatch, *, adapter=native):
    req, snapshot = request(), fixture_snapshot()
    manifest = {"schema": protocol.MANIFEST_SCHEMA, "campaign_instance_id": snapshot["campaign_instance_id"],
        "task_id": "synthetic-method-seal", "attempt": 1, "kind": "method_seal", "method": "overhead",
        "factor": None, "effort_target": None, "generation": 0, "expected_state_sha256": None,
        "request": req, "scientific_request_hash": snapshot["scientific_request_hash"],
        "transport_request_hash": snapshot["transport_request_hash"], "context_id": "a"*64,
        "source_binding_sha256": "f"*64, "policy_sha256": "b"*64, "accounting": snapshot,
        "input_ledger_sha256": protocol.checksum(snapshot),
        "reservation": {"allocation_id": "seal-control", "gpu_seconds": 60, "native_cpus": 4, "native_gpus": 1},
        "pins": {key: None for key in protocol.PINS},
        "inputs": {"method_wrappers": {m: {"directory": str(tmp_path/m), "sha256": "1"*64}
                                          for m in ("hnpe", "tmcmc")}}}
    protocol.validate_manifest(manifest)
    control = {"manifest": manifest, "control_directory": str(tmp_path/"control"),
        "window": {"internal_seconds": 60}}
    clock = {"slurm_job_id": "91003", "allocation_start_epoch": 1, "allocation_deadline_epoch": 61}
    bound = protocol.bind_current_allocation(manifest, clock["slurm_job_id"])
    seal = {"directory": str(tmp_path/"guide"), "sha256": "2"*64}
    states = {"methods": {m: {"status": "no_candidate", "candidate": None} for m in ("hnpe", "tmcmc")}}
    verified = {"method_states": states, "accounting_anchor": source_contract_anchor(bound), "seal_sha256": seal["sha256"]}
    trace, published = [], []
    runner = SimpleNamespace(validate_allocation_snapshot=lambda *args: {"snapshot": deepcopy(args[-1])},
        seal_method_states=lambda *a, **k: trace.append("source_commit"),
        sealing_receipt_descriptor=lambda *a: deepcopy(seal),
        validate_sealing_receipt=lambda *a: deepcopy(verified))
    custody = SimpleNamespace(sealed_evidence=lambda *a: {"sealing_receipt": deepcopy(seal)})
    provenance = SimpleNamespace(source_binding=lambda: {"sha256": "f"*64},
        StageClock=lambda *a, **k: SimpleNamespace())
    for name in ("meso_uq", "meso_uq.benchmark_lab", "meso_uq.benchmark_lab.accuracy_v2"):
        monkeypatch.setitem(sys.modules, name, ModuleType(name))
    sys.modules["meso_uq.benchmark_lab"].provenance = provenance
    accuracy = sys.modules["meso_uq.benchmark_lab.accuracy_v2"]
    accuracy.custody, accuracy.runner, accuracy.policy = custody, runner, SimpleNamespace()
    monkeypatch.setattr(adapter, "_guide", lambda *a: (tmp_path/"guide", {}, {"sha256": "3"*64}))
    monkeypatch.setattr(adapter, "_pinned", lambda *a, **k: {"status": "no_candidate"})
    monkeypatch.setattr(adapter, "_native_publication", lambda *a, **k: published.append(deepcopy(k)))
    return SimpleNamespace(control=control, clock=clock, snapshot=bound, seal=seal,
        states=states, verified=verified, runner=runner, custody=custody, trace=trace, published=published)


def test_real_dispatch_commits_source_then_publishes_raw_snapshot(tmp_path, monkeypatch):
    fixture = dispatch_fixture(tmp_path, monkeypatch)
    assert native._dispatch(fixture.control, fixture.clock) == 0
    assert fixture.trace == ["source_commit"]
    record, = fixture.published
    assert record["outputs"]["sealed_allocation_anchor"] == fixture.snapshot
    assert record["outputs"]["method_seal"] == fixture.seal
    assert record["pins"]["sealed_allocation_anchor_sha256"] == protocol.checksum(fixture.snapshot)


def tamper(verified, kind):
    anchor = verified["accounting_anchor"]
    if kind == "snapshot":
        anchor["snapshot"]["allocations"][0]["allocated_cpu_seconds"] += 1
        anchor["snapshot_sha256"] = protocol.checksum(anchor["snapshot"])
    elif kind == "hash": anchor["snapshot_sha256"] = "0"*64
    elif kind == "membership": anchor["allocation_ids"] = anchor["allocation_ids"][:-1]
    elif kind == "membership_order": anchor["allocation_ids"].reverse()
    elif kind == "revision": anchor["revision"] += 1
    elif kind == "revision_type": anchor["revision"] = True
    elif kind == "source_pin": verified["seal_sha256"] = "0"*64
    elif kind == "raw_snapshot": verified["accounting_anchor"] = anchor["snapshot"]
    else: anchor["unexpected"] = True


@pytest.mark.parametrize("kind", ["snapshot", "hash", "membership", "membership_order", "revision", "revision_type", "source_pin", "raw_snapshot", "extra_field"])
def test_dispatch_rejects_every_changed_source_wrapper_after_commit(tmp_path, monkeypatch, kind):
    fixture = dispatch_fixture(tmp_path, monkeypatch)
    tamper(fixture.verified, kind)
    with pytest.raises(native.NativeContractError, match="Source seal"):
        native._dispatch(fixture.control, fixture.clock)
    assert fixture.trace == ["source_commit"]
    assert fixture.published == []


def test_consumer_accepts_source_wrapper_against_raw_transport_snapshot(tmp_path, monkeypatch):
    fixture = dispatch_fixture(tmp_path, monkeypatch)
    manifest = fixture.control["manifest"]
    manifest["inputs"].update(method_seal=fixture.seal, sealed_allocation_anchor=fixture.snapshot)
    manifest["pins"]["method_seal_sha256"] = fixture.seal["sha256"]
    monkeypatch.setattr(native, "_pinned", lambda *a, **k: {"method_states": fixture.states})
    assert native._sealed(fixture.control, fixture.custody, fixture.runner, {})[1] == fixture.verified


@pytest.mark.parametrize("kind", ["snapshot", "hash", "membership", "revision", "source_pin", "raw_snapshot"])
def test_consumer_rejects_changed_source_seal_wrapper(tmp_path, monkeypatch, kind):
    fixture = dispatch_fixture(tmp_path, monkeypatch)
    manifest = fixture.control["manifest"]
    manifest["inputs"].update(method_seal=fixture.seal, sealed_allocation_anchor=fixture.snapshot)
    manifest["pins"]["method_seal_sha256"] = fixture.seal["sha256"]
    monkeypatch.setattr(native, "_pinned", lambda *a, **k: {"method_states": fixture.states})
    tamper(fixture.verified, kind)
    with pytest.raises(native.NativeContractError, match="Global sealing"):
        native._sealed(fixture.control, fixture.custody, fixture.runner, {})
