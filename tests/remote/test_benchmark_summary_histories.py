"""Synthetic failure metadata must still publish without invented training history."""
from copy import deepcopy
import hashlib
from pathlib import Path

import pytest

from aws_mesouq.benchmark import FACTORS, canonical
from aws_mesouq.benchmark_worker import BenchmarkWorker
from test_benchmark_worker import entries, session, write_fixture_receipt
from test_benchmark_upload_recovery import restart


def failed_scientific_result():
    return {
        "scientific_status": "honest_comparison",
        "hnpe_training": {factor: {"status": "failed", "updates": None, "history": None}
                          for factor in FACTORS},
        "methods": {
            "hnpe": {"summary": {"scored": 0},
                     "cases": [{"world_index": i, "status": "method_failed",
                                "termination_reason": "No eligible fresh training bundle"} for i in range(16)]},
            "tmcmc": {"summary": {"scored": 16},
                      "cases": [{"world_index": i, "status": "scored",
                                 "metrics": {"score": .1}} for i in range(16)]},
        },
        "limitations": ["Synthetic metadata fixture; failed HNPE has no posterior samples"],
    }


def summary_fixture(scientific, cached=None):
    worker = object.__new__(BenchmarkWorker)
    worker.state = {"campaigns": {"fixture": {"receipts": cached or {}}}}
    entry = {"claim": {"campaign": {"id": "fixture"}}}
    return worker, entry, {"result": scientific}


@pytest.mark.parametrize("cached_mode", ["absent", "missing-history", "explicit-null"])
def test_all_failed_factors_with_null_scientific_histories_have_no_fabricated_rows(cached_mode):
    scientific = failed_scientific_result()
    cached = {} if cached_mode == "absent" else {
        "train_" + factor: {"status": "failed", **({"history": None} if cached_mode == "explicit-null" else {})}
        for factor in FACTORS}
    worker, entry, receipt = summary_fixture(scientific, cached)
    before = deepcopy((worker.state, receipt))
    result = worker.summary(entry, receipt, [])
    assert result["training_histories"] == []
    assert result["scientific_status"] == "honest_comparison"
    assert result["metrics"]["hnpe"]["scored"] == 0
    assert all(row["status"] == "method_failed" for row in result["case_results"]["hnpe"])
    assert result["limitations"] == scientific["limitations"]
    assert (worker.state, receipt) == before


def test_mixed_cached_and_scientific_histories_preserve_existing_precedence_and_values():
    scientific = failed_scientific_result()
    first, second = ({"update": 1, "validation_nll": 2.5}, {"update": 2, "validation_nll": 2.})
    cached = {
        "train_global": {"status": "completed", "history": [first, second]},
        "train_mechanical_1": {"status": "failed"},
        "train_mechanical_2": {"status": "failed", "history": None},
        "train_mechanical_3": {"status": "failed", "history": []},
    }
    for factor in ("mechanical_1", "mechanical_2", "mechanical_3", "acoustic_2"):
        scientific["hnpe_training"][factor]["history"] = [second]
    worker, entry, receipt = summary_fixture(scientific, cached)
    before = deepcopy((worker.state, receipt))
    result = worker.summary(entry, receipt, [])
    assert [row["title"] for row in result["training_histories"]] == [
        "global validation history", "mechanical_1 validation history",
        "mechanical_2 validation history", "acoustic_2 validation history"]
    assert [row["history"] for row in result["training_histories"]] == [[first, second], [second], [second], [second]]
    assert result["training_histories"][0]["series"] == [{"name": "global", "x": [1, 2], "y": [2.5, 2.]}]
    assert result["metrics"]["hnpe"]["scored"] == 0
    assert (worker.state, receipt) == before


@pytest.mark.parametrize("invalid_history", [0, False])
def test_null_normalization_does_not_silently_accept_other_malformed_falsy_histories(invalid_history):
    worker, entry, receipt = summary_fixture(
        failed_scientific_result(), {"train_global": {"history": invalid_history}})
    with pytest.raises(TypeError):
        worker.summary(entry, receipt, [])


def test_all_seven_failed_receipts_publish_honest_metrics_and_resume_lost_success_ack(session, monkeypatch):
    session.worker.sweep()
    write_fixture_receipt(session, entries(session)[0])
    session.worker.sweep()
    for entry in entries(session)[1:]:
        training = entry["claim"]["stage"]["id"].startswith("train_")
        receipt = write_fixture_receipt(session, entry, "failed" if training else "completed")
        if training:
            for key in ("history", "updates", "selected_checkpoint"):
                receipt.pop(key, None)
            stage_root = Path(entry["root"]) / "stages" / entry["claim"]["stage"]["id"]
            (stage_root / "selected.keras").unlink()
            (stage_root / "receipt.json").write_bytes(canonical(receipt))
            session.cluster.jobs[entry["job_id"]]["state"] = "FAILED"
    session.worker.sweep()
    metrics = next(entry for entry in entries(session) if entry["claim"]["stage"]["id"] == "metrics")
    receipt = write_fixture_receipt(session, metrics)
    receipt["result"] = failed_scientific_result()
    root = Path(metrics["root"]) / "stages" / "metrics"
    (root / "posterior-hnpe-000.npz").unlink()
    comparison = canonical(receipt["result"])
    (root / "comparison.json").write_bytes(comparison)
    receipt["artifacts"] = [{"file": "comparison.json", "sha256": hashlib.sha256(comparison).hexdigest(),
                             "size_bytes": len(comparison)}]
    (root / "receipt.json").write_bytes(canonical(receipt))
    transfers = []
    original_transfer = session.api.transfer
    def transfer(url, **kwargs):
        transfers.append(url)
        return original_transfer(url, **kwargs)
    monkeypatch.setattr(session.api, "transfer", transfer)
    session.api.lose_event_ack = "succeeded"
    session.worker.sweep()
    assert metrics["pending_event"]["type"] == "succeeded"
    pending_id = metrics["pending_event"]["id"]
    transferred = len(transfers)
    restart(session)  # Test-only journal; preserve the existing fixture identity/context.
    session.worker.sweep()
    saved = session.store.state["benchmark_campaigns"][session.campaign["id"]]
    metrics = next(entry for entry in entries(session) if entry["claim"]["stage"]["id"] == "metrics")
    assert metrics["cloud_terminal"] and not metrics.get("pending_event")
    assert saved["status"] == "completed"
    assert saved["result"]["training_histories"] == []
    assert saved["result"]["metrics"]["hnpe"]["scored"] == 0
    assert all(row["status"] == "method_failed" for row in saved["result"]["case_results"]["hnpe"])
    assert all(next(stage for stage in saved["stages"] if stage["id"] == "train_" + factor)["status"] == "failed"
               for factor in FACTORS)
    assert not any("posterior-hnpe" in ref["name"] or ref["name"].endswith(".keras")
                   for ref in saved["artifacts"].values())
    assert len(transfers) == transferred
    assert session.cluster.submit_count == 11
    assert [event_id for kind, event_id in session.api.event_ids if kind == "succeeded"].count(pending_id) == 2
