"""Synthetic single-worker crash/lease/accounting/archive fault tests."""
from copy import deepcopy
import hashlib
import json
import os
from pathlib import Path
import subprocess
import time

import pytest

from test_benchmark import lab
from test_accuracy import accuracy_lab, spec
from test_accuracy_sdk import registry, sdk
from aws_mesouq import benchmark_accuracy_protocol as p
from aws_mesouq.benchmark_accuracy_contexts import AccuracyExecutionContexts
from aws_mesouq.benchmark_accuracy_worker import (
    AccuracyWorker, AccuracyWorkerBlocked, funded_window)
from aws_mesouq.connector.api import APIError as ConnectorAPIError
from aws_mesouq.connector.transport import Transport


class Journal:
    def __init__(self, data=None):
        self.data = deepcopy(data or {"jobs": {}})
        self.persisted = deepcopy(self.data)
        self.saves = []
        self.crash_if = None
    def save(self):
        if self.crash_if is not None and self.crash_if(self.data):
            self.crash_if = None
            raise OSError("synthetic pre-persist crash")
        self.persisted = deepcopy(self.data)
        self.saves.append(deepcopy(self.data))
    def restored(self):
        return Journal(self.persisted)


class Slurm:
    def __init__(self):
        self.jobs = {}
        self.submit_calls = []
        self.marker = None
        self.fail_before_native = False
        self.fail_after_native = False
        self.transport = None
        self.user = "test-user"
    def lookup(self, marker):
        return list(self.jobs.get(marker, []))
    def submit(self, path):
        self.submit_calls.append(path)
        if self.fail_before_native:
            raise RuntimeError("synthetic unknown pre-native failure")
        job = str(9_000_000 + len(self.submit_calls))
        self.jobs[self.marker] = [(job, "PENDING")]
        if self.fail_after_native:
            raise RuntimeError("synthetic lost sbatch acknowledgement")
        return job


class Executor:
    def __init__(self, contexts, slurm):
        self.contexts, self.slurm = contexts, slurm
        self.prepared = []
        self.observation = None
        self.cancel_calls = []
        self.clock = time.time
        self.preflight_error = None
        self.preflight_calls = []
    def preflight(self, entry):
        claim = entry["claim"]
        proof = {"schema": "aws_mesouq.benchmark_accuracy_preflight.v1",
            "context_id": claim["task"]["manifest"]["context_id"],
            "manifest_sha256": claim["task"]["manifest_sha256"],
            "checked_epoch": self.clock(), "home_quota": {"eligible": self.preflight_error is None}}
        self.preflight_calls.append(deepcopy(proof))
        if self.preflight_error is not None:
            error = RuntimeError(self.preflight_error)
            error.preflight = proof
            raise error
        return proof
    def admitted_contexts(self):
        return self.contexts.supported()
    def prepare(self, claim, window):
        proof = self.preflight({"claim": claim})
        self.prepared.append(deepcopy(claim))
        task = claim["task"]
        self.slurm.marker = task["marker"]
        context = self.contexts.context(task["manifest"]["context_id"])
        return {"script_path": context["native_root"] + "/" + claim["campaign"]["id"] +
                    "/tasks/" + task["id"] + "/run.sh",
            "script_sha256": "a"*64, "intent_sha256": "b"*64,
            "marker": task["marker"], "window": deepcopy(window), "preflight": proof}
    def observe(self, entry):
        return deepcopy(self.observation)
    def cancel(self, entry, authorize):
        # Match baseline Slurm.cancel: callback must raise; its return is ignored.
        authorize()
        self.cancel_calls.append(entry["job_id"])
    def publication(self, entry, ledger):
        raise AssertionError("These fault fixtures do not fabricate scientific publication")
    def read_artifact(self, entry, spec):
        raise AssertionError("No fabricated scientific artifact bytes")


def make_worker(suite, monkeypatch, *, native_root=None, archive_root=None, budget=None):
    document, context = registry(suite)
    if native_root is not None:
        context["native_root"] = str(native_root)
        context["archive_root"] = str(archive_root)
        context["id"] = p.context_id(context)
        document["contexts"][0]["context"] = context
        suite[0].accuracy_policies["p"] = [context]
    contexts = AccuracyExecutionContexts(document, project="p", owner_sub="alice")
    client, api = sdk(suite, monkeypatch)
    journal, slurm = Journal(), Slurm()
    executor = Executor(contexts, slurm)
    executor.clock = lambda: suite[3][0]
    worker = AccuracyWorker(client, slurm, journal, project="p", contexts=contexts,
                            executor=executor, clock=lambda: suite[3][0])
    request = spec()
    if budget is not None:
        request["budget"]["method_gpu_seconds"] = budget
    campaign = client.create("p", request)["campaign"]
    entry = worker._claim_one()
    assert entry["claim"]["campaign"]["id"] == campaign["id"]
    return worker, entry, client, api, journal, slurm, executor, contexts


def restart(parts):
    worker, entry, client, api, journal, slurm, executor, contexts = parts
    journal = journal.restored()
    worker = AccuracyWorker(client, slurm, journal, project="p", contexts=contexts,
        executor=executor, clock=worker.clock)
    entry = next(iter(worker.state["tasks"].values()))
    return worker, entry, client, api, journal, slurm, executor, contexts


def intent(parts):
    worker, entry = parts[:2]
    worker.tick(entry)
    assert entry["phase"] == "prepared"
    worker.tick(entry)
    assert entry["phase"] == "intent_ack"


@pytest.mark.parametrize("reservation,expected", [(59, None), (60, 60), (61, 60), (119, 60), (120, 120)])
def test_funded_native_minutes_never_exceed_original_reservation(reservation, expected):
    value = funded_window(reservation, reservation)
    if expected is None:
        assert value is None
    else:
        assert value["allocated_seconds"] == expected <= reservation
        assert value["internal_seconds"] <= expected
        assert value["runtime_clock_policy"] == "allocation_deadline_v1"


@pytest.mark.parametrize("value", [True, 0, -1, 1.5])
def test_funded_reservation_refuses_bool_coercion_and_nonpositive_values(value):
    with pytest.raises(ValueError):
        funded_window(value, 60)


def test_under_minute_task_never_stages_or_invokes_scheduler(accuracy_lab, monkeypatch):
    document, context = registry(accuracy_lab)
    contexts = AccuracyExecutionContexts(document, project="p", owner_sub="alice")
    client, api = sdk(accuracy_lab, monkeypatch)
    journal, slurm = Journal(), Slurm()
    executor = Executor(contexts, slurm)
    worker = AccuracyWorker(client, slurm, journal, project="p", contexts=contexts,
                            executor=executor, clock=lambda: accuracy_lab[3][0])
    request = spec()
    request["budget"]["method_gpu_seconds"] = 59
    campaign = client.create("p", request)["campaign"]
    assert campaign["status"] == "blocked_unfundable"
    assert campaign["blocked_reason"]["minimum_allocation_seconds"] == 60
    assert campaign["budget_exceeded"] is False
    assert campaign["scientific_status"] is None and campaign["result"] is None
    assert campaign["tasks"] == []
    assert worker._claim_one() is None
    ledger = client.accounting("p", campaign["id"])["snapshot"]
    assert ledger["allocations"] == [] and ledger["case_plan_sha256"] is None
    assert executor.prepared == [] and slurm.submit_calls == []


def test_lost_claim_ack_replays_exact_frozen_request(accuracy_lab, monkeypatch):
    document, context = registry(accuracy_lab)
    contexts = AccuracyExecutionContexts(document, project="p", owner_sub="alice")
    client, api = sdk(accuracy_lab, monkeypatch)
    client.create("p", spec())
    journal, slurm = Journal(), Slurm()
    executor = Executor(contexts, slurm)
    worker = AccuracyWorker(client, slurm, journal, project="p", contexts=contexts, executor=executor)
    request = api.request
    lost = []
    def lose(method, path, payload=None):
        response = request(method, path, payload)
        if path.endswith("/accuracy/claim") and not lost:
            lost.append(deepcopy(payload))
            raise ConnectionError("synthetic lost claim ACK")
        return response
    monkeypatch.setattr(api, "request", lose)
    with pytest.raises(ConnectionError):
        worker._claim_one()
    frozen = deepcopy(journal.persisted["benchmark_accuracy"]["pending_claim"])
    restored = Journal(journal.persisted)
    resumed = AccuracyWorker(client, slurm, restored, project="p", contexts=contexts, executor=executor)
    entry = resumed._claim_one()
    assert entry is not None and len(resumed.state["tasks"]) == 1
    assert lost[0]["request_id"] == frozen["request_id"]
    claims = [payload for method, path, payload in api.sent if path.endswith("/accuracy/claim")]
    assert claims[0] == claims[1]
    assert slurm.submit_calls == []


def test_lost_intent_ack_flushes_same_body_before_renewal_and_one_submit(accuracy_lab, monkeypatch):
    parts = make_worker(accuracy_lab, monkeypatch)
    worker, entry, client, api, journal, slurm, executor, contexts = parts
    worker.tick(entry)
    request = api.request
    lost = []
    def lose(method, path, payload=None):
        response = request(method, path, payload)
        if payload and payload.get("type") == "submitting" and not lost:
            lost.append(deepcopy(payload))
            raise ConnectionError("synthetic lost intent ACK")
        return response
    monkeypatch.setattr(api, "request", lose)
    with pytest.raises(ConnectionError):
        worker.tick(entry)
    pending = deepcopy(journal.persisted["benchmark_accuracy"]["tasks"][worker._entry_key(entry["claim"])]["pending_event"]["body"])
    parts = restart(parts)
    worker, entry, client, api, journal, slurm, executor, contexts = parts
    worker.tick(entry)
    assert entry["phase"] == "intent_ack"
    worker.tick(entry)
    assert len(slurm.submit_calls) == 1 and entry["phase"] == "job_known"
    sent = [payload for method, path, payload in api.sent if payload and payload.get("type") == "submitting"]
    assert sent[0] == sent[1] and sent[0]["event_id"] == pending["event_id"]


def test_lost_sbatch_ack_reconciles_existing_job_without_resubmit(accuracy_lab, monkeypatch):
    parts = make_worker(accuracy_lab, monkeypatch)
    intent(parts)
    worker, entry, client, api, journal, slurm, executor, contexts = parts
    slurm.fail_after_native = True
    with pytest.raises(RuntimeError):
        worker.tick(entry)
    assert journal.persisted["benchmark_accuracy"]["tasks"][worker._entry_key(entry["claim"])]["submission_invoked"] is True
    parts = restart(parts)
    worker, entry, client, api, journal, slurm, executor, contexts = parts
    worker.tick(entry)
    assert entry["phase"] == "job_known"
    worker.tick(entry)
    assert entry["phase"] == "waiting" and len(slurm.submit_calls) == 1


def test_unknown_submission_never_becomes_authoritative_zero(accuracy_lab, monkeypatch):
    parts = make_worker(accuracy_lab, monkeypatch)
    intent(parts)
    worker, entry, client, api, journal, slurm, executor, contexts = parts
    slurm.fail_before_native = True
    with pytest.raises(RuntimeError):
        worker.tick(entry)
    parts = restart(parts)
    worker, entry, client, api, journal, slurm, executor, contexts = parts
    worker.tick(entry)
    assert entry["phase"] == "reconciling" and len(slurm.submit_calls) == 1
    worker.tick(entry)
    assert len(slurm.submit_calls) == 1
    ledger = client.accounting("p", entry["claim"]["campaign"]["id"])["snapshot"]
    row = ledger["allocations"][0]
    assert row["status"] == "submission_unknown"
    assert row["allocated_gpu_seconds"] is None and row["not_submitted"] is False
    assert row["reserved_gpu_seconds"] > 0


def test_cancellation_before_intent_is_zero_without_native_submission(accuracy_lab, monkeypatch):
    parts = make_worker(accuracy_lab, monkeypatch)
    worker, entry, client, api, journal, slurm, executor, contexts = parts
    client.cancel("p", entry["claim"]["campaign"]["id"])
    worker._refresh(entry, force=True)
    worker.tick(entry)
    assert entry["phase"] == "terminal"
    assert executor.prepared == [] and slurm.submit_calls == []
    row = client.accounting("p", entry["claim"]["campaign"]["id"])["snapshot"]["allocations"][0]
    assert row["not_submitted"] is True and row["allocated_gpu_seconds"] == 0


def test_cancellation_after_intent_before_invocation_never_submits_to_obtain_zero(accuracy_lab, monkeypatch):
    parts = make_worker(accuracy_lab, monkeypatch)
    intent(parts)
    worker, entry, client, api, journal, slurm, executor, contexts = parts
    client.cancel("p", entry["claim"]["campaign"]["id"])
    worker.tick(entry)
    assert entry["phase"] == "reconciling" and slurm.submit_calls == []
    row = client.accounting("p", entry["claim"]["campaign"]["id"])["snapshot"]["allocations"][0]
    assert row["not_submitted"] is False and row["allocated_gpu_seconds"] is None


def test_auth_failure_stops_pass_without_losing_frozen_claim(accuracy_lab, monkeypatch):
    parts = make_worker(accuracy_lab, monkeypatch)
    worker, entry, client, api, journal, slurm, executor, contexts = parts
    def deny(*args, **kwargs):
        raise ConnectorAPIError(401, "unauthorized", "synthetic expired access token")
    monkeypatch.setattr(api, "request", deny)
    with pytest.raises(ConnectorAPIError):
        worker.sweep()
    assert slurm.submit_calls == []
    assert worker.state["pending_claim"] is not None or entry["pending_refresh"] is not None


def test_native_failure_costs_survive_resumable_independent_archive_and_lost_ack(accuracy_lab, monkeypatch, tmp_path):
    native, archive = tmp_path / "native", tmp_path / "project"
    native.mkdir(mode=0o700)
    archive.mkdir(mode=0o700)
    parts = make_worker(accuracy_lab, monkeypatch, native_root=native, archive_root=archive)
    worker, entry, client, api, journal, slurm, executor, contexts = parts
    intent(parts)
    worker.tick(entry)
    worker.tick(entry)
    assert entry["phase"] == "waiting"
    job = entry["job_id"]
    executor.observation = {"job_id": job, "state": "failed", "mechanical_failure": True,
        "accounting": {"source": "sacct", "site": "karolina", "slurm_job_id": job,
            "state": "FAILED", "elapsed_seconds": 2, "allocated_gpus": 1, "cpu_seconds": 32}}
    worker.tick(entry)
    worker.tick(entry)
    assert entry["phase"] == "terminal"
    cid = entry["claim"]["campaign"]["id"]
    root = native / cid
    root.mkdir(mode=0o700)
    (root / "stdout.log").write_bytes(b"synthetic failed native attempt\n")
    (root / "stdout.log").chmod(0o600)
    (root / "state.keras").write_bytes(b"synthetic alias fixture, no real model")
    (root / "state.keras").chmod(0o600)
    os.link(root / "state.keras", root / "selected.keras")
    slurm.transport = Transport(local=True)
    client.request_archive("p", cid)
    request = api.request
    lost = []
    def lose(method, path, payload=None):
        response = request(method, path, payload)
        if (path.endswith("/archive") and payload and "signature" in payload and not lost):
            lost.append(deepcopy(payload))
            raise ConnectionError("synthetic lost archive ACK")
        return response
    monkeypatch.setattr(api, "request", lose)
    for step in range(30):
        try:
            worker._archive_one()
        except ConnectionError:
            pass
        if worker.state["archives"].get(cid, {}).get("phase") == "verified":
            break
    assert worker.state["archives"][cid]["phase"] == "verified"
    copied = archive / cid
    assert (copied / "state.keras").read_bytes() == (root / "state.keras").read_bytes()
    assert (copied / "state.keras").stat().st_ino != (copied / "selected.keras").stat().st_ino
    assert (copied / "state.keras").stat().st_nlink == (copied / "selected.keras").stat().st_nlink == 1
    assert (root / "state.keras").stat().st_nlink == 2  # Originals are preserved.
    manifest = json.loads((copied / "archive-manifest.json").read_text())
    assert manifest["allocation_snapshot"] == client.accounting("p", cid)["snapshot"]
    assert manifest["allocation_snapshot_sha256"] == p.checksum(manifest["allocation_snapshot"])
    assert manifest["allocation_snapshot"]["allocations"][0]["allocated_gpu_seconds"] == 2
    campaign = client.get("p", cid)["campaign"]
    assert campaign["status"] == "failed" and campaign["archive"]["status"] == "verified"
    assert campaign["accounting"]["methods"]["hnpe"]["allocated_gpu_seconds"] == 2
    archive_bodies = [payload for method, path, payload in api.sent if path.endswith("/archive")
                      and payload and "signature" in payload]
    assert len(archive_bodies) == 2 and archive_bodies[0] == archive_bodies[1]


def test_exact_terminal_task_read_is_source_bound_and_has_no_lease_secret(accuracy_lab, monkeypatch):
    parts = make_worker(accuracy_lab, monkeypatch)
    worker, entry, client, api, journal, slurm, executor, contexts = parts
    cid, tid = entry["claim"]["campaign"]["id"], entry["claim"]["task"]["id"]
    client.cancel("p", cid)
    response = client.task("p", cid, tid)
    assert response["task"]["status"] == "cancelled"
    assert response["task"]["manifest_sha256"] == entry["manifest_sha256"]
    assert response["task"]["allocation_id"] == entry["claim"]["task"]["allocation_id"]
    assert "lease" not in response["task"] and "lease_token" not in response["task"]
    assert "manifest" not in response["task"]


@pytest.mark.parametrize("kind", ["missing", "nonempty", "public", "symlink", "hardlink", "extra"])
def test_destination_archive_control_lock_exception_is_exact(accuracy_lab, monkeypatch, tmp_path, kind):
    from aws_mesouq.benchmark_worker import ArtifactIntegrityError
    parts = make_worker(accuracy_lab, monkeypatch)
    worker, entry, client, api, journal, slurm, executor, contexts = parts
    slurm.transport = Transport(local=True)
    target = tmp_path / "destination"
    target.mkdir(mode=0o700)
    report = target / "report.json"
    report.write_bytes(b"{}")
    report.chmod(0o400)
    lock = target / ".archive.lock"
    if kind != "missing":
        lock.write_bytes(b"bad" if kind == "nonempty" else b"")
        lock.chmod(0o644 if kind == "public" else 0o600)
    if kind == "symlink":
        lock.unlink()
        lock.symlink_to(report)
    elif kind == "hardlink":
        os.link(lock, tmp_path / "external-lock-alias")
    elif kind == "extra":
        extra = target / ".unexpected"
        extra.write_bytes(b"")
        extra.chmod(0o600)
    with pytest.raises(ArtifactIntegrityError):
        worker._archive_target_snapshot(str(target))


def test_source_scanner_stays_strict_while_destination_allows_only_owned_empty_lock(accuracy_lab, monkeypatch, tmp_path):
    from aws_mesouq.benchmark_worker import ArtifactIntegrityError
    parts = make_worker(accuracy_lab, monkeypatch)
    worker, entry, client, api, journal, slurm, executor, contexts = parts
    slurm.transport = Transport(local=True)
    target = tmp_path / "destination"
    target.mkdir(mode=0o700)
    (target / "report.json").write_bytes(b"{}")
    (target / "report.json").chmod(0o400)
    (target / ".archive.lock").write_bytes(b"")
    (target / ".archive.lock").chmod(0o600)
    files, aliases = worker._archive_target_snapshot(str(target))
    assert aliases == {} and [value["path"] for value in files] == ["report.json"]
    assert files[0]["sha256"] == hashlib.sha256(b"{}").hexdigest()
    with pytest.raises(ArtifactIntegrityError):
        worker._archive_snapshot(str(target))


@pytest.mark.parametrize("boundary", ["prepare", "intent", "submit"])
def test_home_quota_denial_is_fresh_retained_and_prevents_native_invocation(accuracy_lab, monkeypatch, boundary):
    parts = make_worker(accuracy_lab, monkeypatch)
    worker, entry, client, api, journal, slurm, executor, contexts = parts
    if boundary in ("intent", "submit"):
        worker.tick(entry)
    if boundary == "submit":
        worker.tick(entry)
    executor.preflight_error = "synthetic HOME quota refusal"
    with pytest.raises(RuntimeError, match="quota refusal"):
        worker.tick(entry)
    assert slurm.submit_calls == [] and entry["submission_invoked"] is False
    history = entry["preflight_history"]
    assert history[-1]["allowed"] is False
    assert history[-1]["snapshot"]["home_quota"]["eligible"] is False
    assert journal.persisted["benchmark_accuracy"]["tasks"][worker._entry_key(entry["claim"])]["preflight_history"] == history
    if boundary in ("prepare", "intent"):
        assert entry["pending_event"] is None
        assert not any(payload and payload.get("type") == "submitting"
                       for method, path, payload in api.sent)
    frozen_manifest = deepcopy(entry["claim"]["task"]["manifest"])
    executor.preflight_error = None
    worker.tick(entry)
    assert entry["claim"]["task"]["manifest"] == frozen_manifest
    if boundary == "submit":
        assert len(slurm.submit_calls) == 1
        assert history[-1]["boundary"] == "before_submit" and history[-1]["allowed"] is True


def test_quota_preflight_is_requeried_after_intent_delay_and_not_on_reconcile(accuracy_lab, monkeypatch):
    parts = make_worker(accuracy_lab, monkeypatch)
    intent(parts)
    worker, entry, client, api, journal, slurm, executor, contexts = parts
    old = len(executor.preflight_calls)
    accuracy_lab[3][0] += 20
    slurm.fail_after_native = True
    with pytest.raises(RuntimeError):
        worker.tick(entry)
    assert len(executor.preflight_calls) == old + 1
    assert entry["preflight_history"][-1]["boundary"] == "before_submit"
    fresh = entry["preflight_history"][-1]["snapshot"]["checked_epoch"]
    assert fresh == accuracy_lab[3][0]
    calls = len(executor.preflight_calls)
    parts = restart(parts)
    worker, entry = parts[:2]
    worker.tick(entry)
    assert entry["phase"] == "job_known"
    assert len(executor.preflight_calls) == calls and len(slurm.submit_calls) == 1


@pytest.mark.parametrize("change", ["terminal_refresh", "cancellation_disappears"])
def test_cancel_authorizer_raises_before_scancel_when_authority_changes(accuracy_lab, monkeypatch, change):
    parts = make_worker(accuracy_lab, monkeypatch)
    intent(parts)
    worker, entry, client, api, journal, slurm, executor, contexts = parts
    worker.tick(entry)
    worker.tick(entry)
    assert entry["phase"] == "waiting"
    entry["claim"]["campaign"]["status"] = "cancelling"
    executor.observation = {"job_id": entry["job_id"], "state": "queued", "accounting": None}
    def refresh(*args, **kwargs):
        if change == "terminal_refresh":
            return False
        entry["claim"]["campaign"]["status"] = "active"
        entry["claim"]["task"]["status"] = "queued"
        return True
    monkeypatch.setattr(worker, "_refresh", refresh)
    with pytest.raises(AccuracyWorkerBlocked, match="no longer authoritatively"):
        worker._observe(entry)
    assert executor.cancel_calls == []


def test_cancel_authorizer_explicitly_allows_only_current_cancellation(accuracy_lab, monkeypatch):
    parts = make_worker(accuracy_lab, monkeypatch)
    intent(parts)
    worker, entry, client, api, journal, slurm, executor, contexts = parts
    worker.tick(entry)
    worker.tick(entry)
    entry["claim"]["campaign"]["status"] = "cancelling"
    monkeypatch.setattr(worker, "_refresh", lambda *args, **kwargs: True)
    assert worker._authorize_cancel(entry) is True


def close_native_fixture(parts):
    worker, entry, client, api, journal, slurm, executor, contexts = parts
    intent(parts)
    worker.tick(entry)
    worker.tick(entry)
    assert entry["phase"] == "waiting"
    job = entry["job_id"]
    executor.observation = {"job_id": job, "state": "succeeded",
        "accounting": {"source": "sacct", "site": "karolina", "slurm_job_id": job,
            "state": "COMPLETED", "elapsed_seconds": 7, "allocated_gpus": 1, "cpu_seconds": 112}}
    worker.tick(entry)
    assert entry["phase"] == "closed"
    return worker, entry, client, api, journal, slurm, executor, contexts


def publication_fixture(parts, *, second=False):
    worker, entry, client, api, journal, slurm, executor, contexts = close_native_fixture(parts)
    body = b'{"synthetic":"coordination-only"}'
    artifact = {"name": "stage-receipt.json", "sha256": hashlib.sha256(body).hexdigest(),
        "size_bytes": len(body), "media_type": "application/json", "kind": "scientific_artifact"}
    artifacts = [artifact]
    if second:
        artifacts.append({**artifact, "name": "comparison.json"})
    entry["publication"] = {"artifacts": artifacts, "result": {}, "artifact_roles": {}}
    entry["phase"] = "publishing"
    worker._event(entry, "artifact_upload", {"artifacts": artifacts})
    return parts, artifact, body


def freeze_unapplied(parts, kind, payload, phase_after=None):
    worker, entry, client = parts[:3]
    body = client.build_event_body("p", entry["claim"], "frozen-unapplied", kind, payload)
    entry["pending_event"] = {"body": deepcopy(body), "phase_after": phase_after}
    worker.save()
    return body


def test_expired_unapplied_intent_is_retained_then_rebased_once_with_new_fence(accuracy_lab, monkeypatch):
    parts = make_worker(accuracy_lab, monkeypatch)
    worker, entry, client, api, journal, slurm, executor, contexts = parts
    worker.tick(entry)
    prepared = entry["native_preparation"]
    original = freeze_unapplied(parts, "submitting",
        {name: prepared[name] for name in ("script_sha256", "intent_sha256", "marker")}, "intent_ack")
    accuracy_lab[3][0] += accuracy_lab[0].lease_seconds + 1
    with pytest.raises(AccuracyWorkerBlocked, match="Unapplied event"):
        worker.tick(entry)
    assert entry["pending_event"] is None and entry["reconcile_event"]["body"] == original
    assert entry["rejected_events"][0]["body"] == original
    parts = restart(parts)
    worker, entry, client, api, journal, slurm, executor, contexts = parts
    worker.tick(entry)
    assert entry["phase"] == "intent_ack" and entry["reconcile_event"] is None
    worker.tick(entry)
    assert len(slurm.submit_calls) == 1
    bodies = [body for method, path, body in api.sent if body and body.get("type") == "submitting"]
    assert bodies[0]["event_id"] == original["event_id"]
    assert bodies[-1]["event_id"] != original["event_id"]
    assert bodies[-1]["lease_epoch"] > original["lease_epoch"]
    assert bodies[-1]["payload"] == original["payload"]


def test_browser_cancel_of_unapplied_intent_recovers_exact_terminal_without_native_work(accuracy_lab, monkeypatch):
    parts = make_worker(accuracy_lab, monkeypatch)
    worker, entry, client, api, journal, slurm, executor, contexts = parts
    worker.tick(entry)
    prepared = entry["native_preparation"]
    original = freeze_unapplied(parts, "submitting",
        {name: prepared[name] for name in ("script_sha256", "intent_sha256", "marker")}, "intent_ack")
    client.cancel("p", entry["claim"]["campaign"]["id"])
    with pytest.raises(AccuracyWorkerBlocked, match="Unapplied event"):
        worker.tick(entry)
    worker.tick(entry)
    assert entry["phase"] == "terminal" and entry["reconcile_event"] is None
    assert entry["rejected_events"][0]["body"] == original
    assert slurm.submit_calls == []
    row = client.accounting("p", entry["claim"]["campaign"]["id"])["snapshot"]["allocations"][0]
    assert row["not_submitted"] is True and row["allocated_gpu_seconds"] == 0


def test_revision_rejected_event_gets_new_id_but_true_payload_conflict_is_never_rebased(accuracy_lab, monkeypatch):
    parts = make_worker(accuracy_lab, monkeypatch)
    worker, entry, client, api, journal, slurm, executor, contexts = parts
    original = freeze_unapplied(parts, "heartbeat", {})
    concurrent = client.build_event_body("p", entry["claim"], "concurrent-heartbeat", "heartbeat", {})
    client.event_frozen(concurrent)
    with pytest.raises(AccuracyWorkerBlocked):
        worker.tick(entry)
    worker.tick(entry)
    assert entry["reconcile_event"] is None and entry["pending_event"] is None
    assert entry["rejected_events"][-1]["rejection_code"] == "accuracy_revision_conflict"
    worker._refresh(entry, force=True)
    changed = deepcopy(concurrent)
    changed["payload"] = {"not_allowed": True}
    # An exact existing ID with a changed but valid body is an immutable conflict.
    changed["type"] = "submission_unknown"
    changed["payload"] = {"marker": entry["claim"]["task"]["marker"], "reason": "changed"}
    entry["pending_event"] = {"body": changed, "phase_after": None}
    worker.save()
    with pytest.raises(ConnectorAPIError) as rejected:
        worker.tick(entry)
    assert rejected.value.code == "idempotency_conflict"
    assert entry["pending_event"]["body"] == changed and entry.get("reconcile_event") is None


@pytest.mark.parametrize("boundary", ["before_publish", "before_put", "terminal_before_put"])
def test_publication_cancel_or_terminal_refresh_stops_all_new_byte_reads_and_puts(accuracy_lab, monkeypatch, boundary):
    parts, artifact, body = publication_fixture(make_worker(accuracy_lab, monkeypatch))
    worker, entry, client, api, journal, slurm, executor, contexts = parts
    transfers, reads = [], []
    monkeypatch.setattr(executor, "read_artifact", lambda *args: reads.append(args))
    monkeypatch.setattr(api, "transfer", lambda *args, **kwargs: transfers.append(args), raising=False)
    cid = entry["claim"]["campaign"]["id"]
    if boundary == "before_publish":
        client.cancel("p", cid)
        worker._refresh(entry, force=True)
    else:
        refresh = worker._refresh
        def change(entry, *, force=False):
            if force:
                if boundary == "terminal_before_put":
                    entry["phase"] = "terminal"
                    return False
                client.cancel("p", cid)
            return refresh(entry, force=force)
        monkeypatch.setattr(worker, "_refresh", change)
    worker._publish(entry)
    assert reads == transfers == []
    assert entry["phase"] == "terminal"
    assert len(slurm.submit_calls) == 1
    assert client.accounting("p", cid)["snapshot"]["allocations"][0]["allocated_gpu_seconds"] == 7


def test_cancelled_publication_keeps_verified_data_and_waits_for_authoritative_upload_expiry(accuracy_lab, monkeypatch):
    from aws_publishing.api import PENDING_UPLOAD_SECONDS
    parts, artifact, body = publication_fixture(make_worker(accuracy_lab, monkeypatch), second=True)
    worker, entry, client, api, journal, slurm, executor, contexts = parts
    cid = entry["claim"]["campaign"]["id"]
    first = entry["upload_plan"][0]
    _, state = accuracy_lab[1].load()
    first_key = state["artifacts"][first["artifact_id"]]["storage_key"]
    accuracy_lab[2].data[first_key] = body
    worker._event(entry, "artifact_status", {"artifact_ids": [first["artifact_id"]]})
    second_id = entry["upload_plan"][1]["artifact_id"]
    client.cancel("p", cid)
    worker._refresh(entry, force=True)
    worker._publish(entry)
    current = client.get("p", cid)["campaign"]
    assert current["status"] == "cancelled" and current["archive"]["available"] is False
    assert current["result"] is None
    assert current["verified_artifacts"] == [first["artifact"]]
    assert accuracy_lab[2].data[first_key] == body
    accuracy_lab[0].release_policy = "v1"
    accuracy_lab[3][0] += PENDING_UPLOAD_SECONDS + 1
    accuracy_lab[0].reconcile_upload_reservations()
    _, state = accuracy_lab[1].load()
    assert state["artifacts"][first["artifact_id"]]["verified"] is True
    assert state["artifacts"][first["artifact_id"]].get("reservation_released") is False
    assert state["artifacts"][second_id]["reservation_released"] is True
    assert client.get("p", cid)["campaign"]["archive"]["available"] is True
    assert accuracy_lab[2].data[first_key] == body


@pytest.mark.parametrize("event_type", ["submitted", "queued", "running", "submission_unknown"])
def test_closed_accounting_never_reopens_on_stale_native_observation(accuracy_lab, monkeypatch, event_type):
    parts = close_native_fixture(make_worker(accuracy_lab, monkeypatch))
    worker, entry, client, api, journal, slurm, executor, contexts = parts
    before = client.accounting("p", entry["claim"]["campaign"]["id"])
    payload = {"slurm_job_id": entry["job_id"]}
    if event_type == "running":
        payload.update(allocation_start_epoch=1700000000, allocation_deadline_epoch=1700000100)
    if event_type == "submission_unknown":
        payload = {"marker": entry["claim"]["task"]["marker"], "reason": "stale"}
    with pytest.raises(ConnectorAPIError) as rejected:
        worker._event(entry, event_type, payload)
    assert rejected.value.code == "accuracy_accounting_closed"
    assert client.accounting("p", entry["claim"]["campaign"]["id"]) == before


def test_cancelled_closed_lease_expiry_preserves_actual_cost_row(accuracy_lab, monkeypatch):
    parts = close_native_fixture(make_worker(accuracy_lab, monkeypatch))
    worker, entry, client, api, journal, slurm, executor, contexts = parts
    cid = entry["claim"]["campaign"]["id"]
    client.cancel("p", cid)
    before = client.accounting("p", cid)
    accuracy_lab[3][0] += accuracy_lab[0].lease_seconds + 1
    worker._refresh(entry, force=True)
    after = client.accounting("p", cid)
    assert after["snapshot"]["allocations"] == before["snapshot"]["allocations"]
    worker.tick(entry)
    assert entry["phase"] == "terminal"


def test_expiry_fence_prevents_late_verification_or_url_renewal(accuracy_lab, monkeypatch):
    parts, artifact, body = publication_fixture(make_worker(accuracy_lab, monkeypatch))
    worker, entry, client, api, journal, slurm, executor, contexts = parts
    aid = entry["upload_plan"][0]["artifact_id"]
    revision, state = accuracy_lab[1].load()
    before = deepcopy(state)
    state["artifacts"][aid]["upload_expired_at"] = accuracy_lab[3][0]
    accuracy_lab[1].save(revision, before, state)
    key = state["artifacts"][aid]["storage_key"]
    accuracy_lab[2].data[key] = body
    response = worker._event(entry, "artifact_status", {"artifact_ids": [aid]})
    assert response["artifacts"] == [{"id": aid, "verified": False,
        "reservation_released": False, "upload_expired": True}]
    with pytest.raises(ConnectorAPIError) as rejected:
        worker._event(entry, "artifact_upload", {"artifacts": [artifact]})
    assert rejected.value.code == "accuracy_upload_expired"
    _, state = accuracy_lab[1].load()
    assert state["artifacts"][aid]["verified"] is False


def test_lost_refresh_ack_cannot_authorize_submit_after_cached_lease_expiry(accuracy_lab, monkeypatch):
    parts = make_worker(accuracy_lab, monkeypatch)
    intent(parts)
    worker, entry, client, api, journal, slurm, executor, contexts = parts
    request = api.request
    lost = []
    def lose(method, path, payload=None):
        response = request(method, path, payload)
        if path.endswith("/claim") and payload and payload.get("task_id") and not lost:
            lost.append(deepcopy(payload))
            raise ConnectionError("synthetic lost exact-refresh ACK")
        return response
    monkeypatch.setattr(api, "request", lose)
    with pytest.raises(ConnectionError):
        worker._submit_once(entry)
    assert entry["pending_refresh"] == {key: value for key, value in lost[0].items() if key != "signature"}
    assert slurm.submit_calls == []
    accuracy_lab[3][0] += accuracy_lab[0].lease_seconds + 1
    with pytest.raises(AccuracyWorkerBlocked, match="expired lease"):
        worker._submit_once(entry)
    assert entry["pending_refresh"] is None and slurm.submit_calls == []
    worker._submit_once(entry)
    assert len(slurm.submit_calls) == 1
    assert entry["claim"]["task"]["lease"]["epoch"] > 1


@pytest.mark.parametrize("kind", ["unverified", "released", "foreign_owner"])
def test_incomplete_attempt_file_descriptors_never_expose_unverified_released_or_foreign_files(accuracy_lab, monkeypatch, kind):
    from test_accuracy import call
    parts, artifact, body = publication_fixture(make_worker(accuracy_lab, monkeypatch))
    worker, entry, client, api, journal, slurm, executor, contexts = parts
    cid = entry["claim"]["campaign"]["id"]
    aid = entry["upload_plan"][0]["artifact_id"]
    if kind != "unverified":
        revision, state = accuracy_lab[1].load()
        before = deepcopy(state)
        state["artifacts"][aid]["verified"] = True
        state["artifacts"][aid]["reservation_released"] = kind == "released"
        accuracy_lab[1].save(revision, before, state)
    if kind == "foreign_owner":
        status, response = call(accuracy_lab, "GET",
            f"/v1/projects/p/benchmark-accuracy/campaigns/{cid}", sub="bob")
        assert status == 404 and "verified_artifacts" not in response
    else:
        assert client.get("p", cid)["campaign"]["verified_artifacts"] == []
    status, response = call(accuracy_lab, "GET", f"/v1/projects/p/benchmark-accuracy/campaigns/{cid}/results")
    assert status == 409 and "qualification" not in response


def test_owner_partial_artifact_descriptors_are_whitelisted_without_storage_urls_or_native_paths(accuracy_lab, monkeypatch):
    parts, artifact, body = publication_fixture(make_worker(accuracy_lab, monkeypatch))
    worker, entry, client, api, journal, slurm, executor, contexts = parts
    cid = entry["claim"]["campaign"]["id"]
    aid = entry["upload_plan"][0]["artifact_id"]
    _, state = accuracy_lab[1].load()
    key = state["artifacts"][aid]["storage_key"]
    accuracy_lab[2].data[key] = body
    worker._event(entry, "artifact_status", {"artifact_ids": [aid]})
    response = client.get("p", cid)["campaign"]
    assert response["result"] is None
    assert len(response["verified_artifacts"]) == 1
    assert set(response["verified_artifacts"][0]) == {"id","name","media_type","sha256","size_bytes","kind"}
    assert response["verified_artifacts"][0] == entry["upload_plan"][0]["artifact"]
    assert "storage_key" not in json.dumps(response["verified_artifacts"])
    assert "upload" not in response["verified_artifacts"][0]
