"""Real worker catch sites and final native-submit authorization; synthetic only."""
from copy import deepcopy
import pytest

# Shared fixtures live in two test directories. Resolve their exact source files
# instead of relying on pytest's directory-prepend order or extra PYTHONPATH.
import hashlib
import importlib.util
from pathlib import Path
import sys

_TEST_ROOT = Path(__file__).resolve().parents[2]
_FIXTURE_PATHS = {
    "test_benchmark": "services/aws_publishing/tests/test_benchmark.py",
    "test_accuracy": "services/aws_publishing/tests/test_accuracy.py",
    "test_accuracy_sdk": "services/aws_publishing/tests/test_accuracy_sdk.py",
    "test_accuracy_worker": "services/aws_publishing/tests/test_accuracy_worker.py",
    "test_benchmark_worker": "tests/remote/test_benchmark_worker.py",
}
_FIXTURE_SHA256 = {}


def _load_fixture_module(name):
    path = _TEST_ROOT / _FIXTURE_PATHS[name]
    if path.is_symlink() or not path.is_file():
        raise ImportError("Shared fixture source must be a regular repository file")
    raw = path.read_bytes()
    digest = hashlib.sha256(raw).hexdigest()
    if name in sys.modules:
        module = sys.modules[name]
        observed = getattr(module, "__file__", None)
        origin = getattr(getattr(module, "__spec__", None), "origin", None)
        if not observed or not origin or Path(observed).resolve() != path or Path(origin).resolve() != path:
            raise ImportError("Shared fixture module name resolves to another source file")
    else:
        spec = importlib.util.spec_from_file_location(name, path)
        if spec is None or spec.loader is None:
            raise ImportError("Shared fixture source has no Python loader")
        module = importlib.util.module_from_spec(spec)
        # Baseline fixtures import each other by these canonical pytest names.
        # Register only this explicitly resolved file, never an unverified alias.
        sys.modules[name] = module
        try:
            exec(compile(raw, str(path), "exec"), module.__dict__)
        except BaseException:
            if sys.modules.get(name) is module:
                del sys.modules[name]
            raise
    if Path(module.__file__).resolve() != path or hashlib.sha256(path.read_bytes()).hexdigest() != digest:
        raise ImportError("Shared fixture source changed while it was loaded")
    _FIXTURE_SHA256[name] = digest
    return module


# Load the finite shared-fixture import closure before its dependent modules.
_benchmark_fixtures = _load_fixture_module("test_benchmark")
_accuracy_fixtures = _load_fixture_module("test_accuracy")
_load_fixture_module("test_accuracy_sdk")
_accuracy_worker_fixtures = _load_fixture_module("test_accuracy_worker")
_legacy_worker_fixtures = _load_fixture_module("test_benchmark_worker")
lab = _benchmark_fixtures.lab
accuracy_lab = _accuracy_fixtures.accuracy_lab
make_worker, intent = _accuracy_worker_fixtures.make_worker, _accuracy_worker_fixtures.intent
session = _legacy_worker_fixtures.session
from aws_mesouq.benchmark_dispatch import FairBenchmarkWorker, PROTOCOLS
from aws_mesouq.benchmark_accuracy_worker import AccuracyWorkerBlocked
from aws_mesouq.benchmark_worker import BenchmarkWorker
from aws_mesouq.connector.api import APIError
from aws_mesouq.connector.auth import SessionExpired
from aws_mesouq.connector.ssh_session import SSHSessionError


def failure(kind):
    if kind == "expired":
        return SessionExpired("synthetic private provider detail")
    if kind == "ssh":
        return SSHSessionError("synthetic_guarded_transport_failure")
    return APIError(int(kind), "synthetic_control_error", "synthetic private response detail")


class OtherProtocol:
    def __init__(self):
        self.calls = 0
    def sweep(self):
        self.calls += 1
        return {"active": 0}


@pytest.mark.parametrize("phase", ["existing", "new_claim", "archive"])
@pytest.mark.parametrize("kind", ["401", "403", "expired"])
def test_real_legacy_mixed_auth_failure_stops_before_other_protocol(session, monkeypatch, phase, kind):
    worker = session.worker
    worker.propagate_auth_errors = True
    worker.maximum_active = 1
    calls = []
    entry = {"cloud_terminal": False}
    worker.state["stages"] = {"one": entry} if phase == "existing" else {}
    def request(method, path, payload=None):
        calls.append(path)
        if path == "/synthetic-failure":
            raise failure(kind)
        assert path.endswith("/benchmarks")
        return {"campaigns": [{"archive": {"status": "requested"}}]} if phase == "archive" else {"campaigns": []}
    monkeypatch.setattr(worker.api, "request", request)
    monkeypatch.setattr(worker, "poll", lambda value: worker.api.request("GET", "/synthetic-failure"))
    claims = [entry, None] if phase == "new_claim" else [None]
    monkeypatch.setattr(worker, "_claim", lambda: claims.pop(0))
    monkeypatch.setattr(worker, "archive", lambda value: worker.api.request("GET", "/synthetic-failure"))
    other = OtherProtocol()
    dispatcher = FairBenchmarkWorker(worker, other, session.journal, project="p", owner_sub="alice")
    with pytest.raises(type(failure(kind))):
        dispatcher.sweep()
    assert other.calls == 0
    assert calls.count("/synthetic-failure") == 1
    assert session.cluster.submit_count == 0
    assert dispatcher.state["next_protocol"] == PROTOCOLS[1]
    assert "private response" not in repr(session.journal.data)
    assert "private provider" not in repr(session.journal.data)


@pytest.mark.parametrize("phase", ["existing", "new_claim", "archive"])
@pytest.mark.parametrize("kind", ["401", "403", "expired", "500"])
def test_real_legacy_default_error_policy_stays_recoverable(session, monkeypatch, phase, kind):
    worker = session.worker
    assert worker.propagate_auth_errors is False
    worker.maximum_active = 1
    entry = {"cloud_terminal": False}
    worker.state["stages"] = {"one": entry} if phase == "existing" else {}
    def request(method, path, payload=None):
        if path == "/synthetic-failure":
            raise failure(kind)
        return {"campaigns": [{"archive": {"status": "requested"}}]} if phase == "archive" else {"campaigns": []}
    monkeypatch.setattr(worker.api, "request", request)
    monkeypatch.setattr(worker, "poll", lambda value: worker.api.request("GET", "/synthetic-failure"))
    claims = [entry, None] if phase == "new_claim" else [None]
    monkeypatch.setattr(worker, "_claim", lambda: claims.pop(0))
    monkeypatch.setattr(worker, "archive", lambda value: worker.api.request("GET", "/synthetic-failure"))
    result = worker.sweep()
    assert result["errors"] == [type(failure(kind)).__name__]
    assert session.cluster.submit_count == 0


def test_legacy_constructor_opt_in_is_explicit_and_boolean(session):
    worker = BenchmarkWorker(session.api, session.slurm, session.journal, project="p",
        operator=session.operator, clock=lambda: session.clock[0], propagate_auth_errors=True)
    assert worker.propagate_auth_errors is True
    with pytest.raises(ValueError, match="boolean"):
        BenchmarkWorker(session.api, session.slurm, session.journal, project="p",
            operator=session.operator, propagate_auth_errors=1)


@pytest.mark.parametrize("kind", ["401", "403", "expired", "ssh"])
def test_real_accuracy_typed_fatal_stops_local_pass_and_other_protocol(accuracy_lab, monkeypatch, kind):
    parts = make_worker(accuracy_lab, monkeypatch)
    worker, entry, client, api, journal, slurm, executor, contexts = parts
    second = deepcopy(entry)
    second["claim"]["task"]["id"] = "f"*32
    worker.state["tasks"]["second-synthetic-entry"] = second
    entry["last_refresh"] = 0
    second["last_refresh"] = 0
    calls = []
    def denied(*args, **kwargs):
        calls.append(True)
        raise failure(kind)
    monkeypatch.setattr(api, "request", denied)
    other = OtherProtocol()
    dispatcher = FairBenchmarkWorker(other, worker, journal, project="p", owner_sub="alice")
    dispatcher.state["next_protocol"] = PROTOCOLS[1]
    with pytest.raises(type(failure(kind))):
        dispatcher.sweep()
    assert calls == [True]
    assert other.calls == 0
    assert slurm.submit_calls == [] and executor.prepared == []
    assert entry["pending_refresh"] is not None
    assert journal.persisted["benchmark_accuracy"]["tasks"][worker._entry_key(entry["claim"])]["pending_refresh"] == entry["pending_refresh"]


def test_real_legacy_ssh_failure_always_stops_dispatch(session, monkeypatch):
    worker = session.worker
    worker.propagate_auth_errors = True
    worker.state["stages"] = {"one": {"cloud_terminal": False}}
    monkeypatch.setattr(worker, "poll", lambda entry: (_ for _ in ()).throw(failure("ssh")))
    other = OtherProtocol()
    dispatcher = FairBenchmarkWorker(worker, other, session.journal, project="p", owner_sub="alice")
    with pytest.raises(SSHSessionError):
        dispatcher.sweep()
    assert other.calls == 0 and session.cluster.submit_count == 0


def unresolved(parts):
    worker, entry, client = parts[:3]
    ledger = client.accounting("p", entry["claim"]["campaign"]["id"])["snapshot"]
    rows = [row for row in ledger["allocations"] if row["allocation_id"] == entry["claim"]["task"]["allocation_id"]]
    assert len(rows) == 1
    assert rows[0]["allocated_gpu_seconds"] is None
    assert rows[0]["not_submitted"] is False
    return rows[0]


def test_expired_lookup_cannot_authorize_native_submission(accuracy_lab, monkeypatch):
    parts = make_worker(accuracy_lab, monkeypatch)
    worker, entry, client, api, journal, slurm, executor, contexts = parts
    intent(parts)
    lookup = slurm.lookup
    def slow_lookup(marker):
        accuracy_lab[3][0] += accuracy_lab[0].lease_seconds + 1
        return lookup(marker)
    monkeypatch.setattr(slurm, "lookup", slow_lookup)
    with pytest.raises(AccuracyWorkerBlocked, match="expired during native checks"):
        worker._submit_once(entry)
    assert slurm.submit_calls == [] and entry["submission_invoked"] is False
    assert entry["phase"] == "intent_ack" and entry["native_preparation"] is not None
    unresolved(parts)


def test_preflight_cancel_is_refreshed_before_native_submission(accuracy_lab, monkeypatch):
    parts = make_worker(accuracy_lab, monkeypatch)
    worker, entry, client, api, journal, slurm, executor, contexts = parts
    intent(parts)
    preflight = executor.preflight
    def cancellation(value):
        proof = preflight(value)
        client.cancel("p", entry["claim"]["campaign"]["id"])
        return proof
    monkeypatch.setattr(executor, "preflight", cancellation)
    worker._submit_once(entry)
    assert slurm.submit_calls == [] and entry["submission_invoked"] is False
    assert entry["phase"] == "reconciling"
    unresolved(parts)


def test_late_journal_expiry_retains_invoked_ambiguity_without_submit_or_zero(accuracy_lab, monkeypatch):
    parts = make_worker(accuracy_lab, monkeypatch)
    worker, entry, client, api, journal, slurm, executor, contexts = parts
    intent(parts)
    save = journal.save
    delayed = []
    def slow_save():
        save()
        if entry["submission_invoked"] and not delayed:
            delayed.append(True)
            accuracy_lab[3][0] += accuracy_lab[0].lease_seconds + 1
    monkeypatch.setattr(journal, "save", slow_save)
    with pytest.raises(AccuracyWorkerBlocked, match="expired while recording invocation"):
        worker._submit_once(entry)
    assert slurm.submit_calls == [] and entry["submission_invoked"] is True
    assert entry["phase"] == "submission_invoked"
    assert journal.persisted["benchmark_accuracy"]["tasks"][worker._entry_key(entry["claim"])]["submission_invoked"] is True
    unresolved(parts)
    worker.tick(entry)
    assert slurm.submit_calls == [] and entry["phase"] == "reconciling"
    unresolved(parts)


def test_known_job_from_final_refresh_cannot_duplicate_native_submission(accuracy_lab, monkeypatch):
    parts = make_worker(accuracy_lab, monkeypatch)
    worker, entry, client, api, journal, slurm, executor, contexts = parts
    intent(parts)
    preflight = executor.preflight
    def known_native(value):
        proof = preflight(value)
        body = client.build_event_body("p", entry["claim"], "late-native-ack", "submitted", {"slurm_job_id": "9000000"})
        client.event_frozen(body)
        return proof
    monkeypatch.setattr(executor, "preflight", known_native)
    worker._submit_once(entry)
    assert slurm.submit_calls == [] and entry["submission_invoked"] is False
    assert entry["job_id"] == "9000000" and entry["phase"] == "job_known"
    unresolved(parts)
