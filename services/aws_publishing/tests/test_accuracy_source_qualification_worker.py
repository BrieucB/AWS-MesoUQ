"""Recovery survives loss of new-work eligibility; synthetic controller/Slurm only."""
from copy import deepcopy
import pytest

from test_benchmark import lab
from test_accuracy import accuracy_lab, spec
from test_accuracy_sdk import registry, sdk
from test_accuracy_worker import Journal, Slurm, Executor, make_worker, restart, intent
from aws_mesouq.benchmark_accuracy_contexts import AccuracyExecutionContexts
from aws_mesouq.benchmark_accuracy_worker import AccuracyWorker


def deny_new_work(monkeypatch, contexts):
    monkeypatch.setattr(contexts, "supported", lambda: [])
    def reject(identity):
        raise ValueError("synthetic current source policy is unqualified")
    monkeypatch.setattr(contexts, "require_new_work", reject)


def test_existing_frozen_pending_claim_replays_with_no_supported_context(accuracy_lab, monkeypatch):
    document, context = registry(accuracy_lab)
    contexts = AccuracyExecutionContexts(document, project="p", owner_sub="alice")
    client, api = sdk(accuracy_lab, monkeypatch)
    client.create("p", spec())
    journal, slurm = Journal(), Slurm()
    executor = Executor(contexts, slurm)
    executor.clock = lambda: accuracy_lab[3][0]
    worker = AccuracyWorker(client, slurm, journal, project="p", contexts=contexts,
        executor=executor, clock=lambda: accuracy_lab[3][0])
    original = api.request
    lost = []
    def lose(method, path, payload=None):
        response = original(method, path, payload)
        if path.endswith("/accuracy/claim") and not lost:
            lost.append(deepcopy(payload))
            raise ConnectionError("synthetic lost claim acknowledgement")
        return response
    monkeypatch.setattr(api, "request", lose)
    with pytest.raises(ConnectionError):
        worker._claim_one()
    frozen = deepcopy(journal.persisted["benchmark_accuracy"]["pending_claim"])
    deny_new_work(monkeypatch, contexts)
    monkeypatch.setattr(client, "build_claim_body",
        lambda *a, **kw: pytest.fail("Recovery rebuilt a frozen claim"))
    seen = []
    original_claim = client.claim_frozen
    def replay(body):
        seen.append(deepcopy(body))
        return original_claim(body)
    monkeypatch.setattr(client, "claim_frozen", replay)
    resumed = AccuracyWorker(client, slurm, journal.restored(), project="p",
        contexts=contexts, executor=executor, clock=lambda: accuracy_lab[3][0])
    entry = resumed._claim_one()
    assert entry is not None and resumed.state["pending_claim"] is None
    claims = [payload for method, path, payload in api.sent if path.endswith("/accuracy/claim")]
    assert claims[0] == claims[1] and seen == [frozen]
    assert executor.prepared == [] and executor.preflight_calls == [] and slurm.submit_calls == []
    with pytest.raises(ValueError):
        resumed.tick(entry)
    assert executor.prepared == [] and executor.preflight_calls == [] and slurm.submit_calls == []


def test_no_supported_context_cannot_build_a_fresh_claim(accuracy_lab, monkeypatch):
    parts = make_worker(accuracy_lab, monkeypatch)
    worker, entry, client, api, journal, slurm, executor, contexts = parts
    deny_new_work(monkeypatch, contexts)
    monkeypatch.setattr(client, "build_claim_body", lambda *a, **kw: pytest.fail("Constructed fresh claim"))
    assert worker._claim_one() is None
    assert slurm.submit_calls == []


@pytest.mark.parametrize("phase", ["claimed", "prepared", "intent_ack"])
def test_unqualified_no_producer_is_never_newly_prepared_or_submitted(accuracy_lab, monkeypatch, phase):
    parts = make_worker(accuracy_lab, monkeypatch)
    worker, entry, client, api, journal, slurm, executor, contexts = parts
    if phase in {"prepared", "intent_ack"}:
        worker.tick(entry)
    if phase == "intent_ack":
        worker.tick(entry)
    before_prepare = len(executor.prepared)
    before_preflight = len(executor.preflight_calls)
    deny_new_work(monkeypatch, contexts)
    with pytest.raises(ValueError):
        worker.tick(entry)
    assert len(executor.prepared) == before_prepare
    assert len(executor.preflight_calls) == before_preflight
    assert slurm.submit_calls == [] and entry["submission_invoked"] is False
    row = client.accounting("p", entry["claim"]["campaign"]["id"])["snapshot"]["allocations"][0]
    assert row["allocated_gpu_seconds"] is None and row["not_submitted"] is False
    assert row["reserved_gpu_seconds"] > 0


def test_expired_lease_lost_submit_ack_restart_reconciles_without_new_admission(accuracy_lab, monkeypatch):
    parts = make_worker(accuracy_lab, monkeypatch)
    intent(parts)
    worker, entry, client, api, journal, slurm, executor, contexts = parts
    slurm.fail_after_native = True
    with pytest.raises(RuntimeError):
        worker.tick(entry)
    assert entry["submission_invoked"] is True and len(slurm.submit_calls) == 1
    original_manifest = deepcopy(entry["claim"]["task"]["manifest"])
    original_marker = entry["claim"]["task"]["marker"]
    accuracy_lab[3][0] += 1000
    parts = restart(parts)
    worker, entry, client, api, journal, slurm, executor, contexts = parts
    deny_new_work(monkeypatch, contexts)
    monkeypatch.setattr(executor, "prepare", lambda *a: pytest.fail("Recovery prepared new work"))
    monkeypatch.setattr(executor, "preflight", lambda *a: pytest.fail("Recovery preflight new work"))
    worker.tick(entry)
    assert entry["phase"] == "job_known" and entry["job_id"] is not None
    worker.tick(entry)
    assert entry["phase"] == "waiting"
    assert entry["claim"]["task"]["manifest"] == original_manifest
    assert entry["claim"]["task"]["marker"] == original_marker
    assert len(slurm.submit_calls) == 1
    rows = client.accounting("p", entry["claim"]["campaign"]["id"])["snapshot"]["allocations"]
    assert rows[0]["not_submitted"] is False and rows[0]["allocated_gpu_seconds"] is None
    assert any(payload.get("task_id") == entry["claim"]["task"]["id"]
        for method, path, payload in api.sent
        if payload and path.endswith("/accuracy/claim"))
