"""Boundary regressions shared with the frozen pre-migration source; metadata only."""
import copy

import pytest

from test_benchmark_worker import entries, session, write_fixture_receipt


def test_native_timeout_with_completed_receipt_is_native_failure(session):
    session.worker.sweep()
    entry = entries(session)[0]
    write_fixture_receipt(session, entry)
    session.cluster.jobs[entry["job_id"]]["state"] = "TIMEOUT"
    session.worker.poll(entry)
    stage = session.store.state["benchmark_campaigns"][session.campaign["id"]]["stages"][0]
    assert stage["status"] == "failed"
    assert stage["failure"]["code"] == "native_timeout"
    assert stage["scheduler_accounting"]["state"] == "TIMEOUT"
    assert stage["scheduler_accounting"]["elapsed_seconds"] == 10


def test_event_response_context_change_is_rejected_before_local_binding(session):
    session.worker.sweep()
    entry = entries(session)[0]
    before = copy.deepcopy(entry["claim"]["campaign"])
    original = session.api.request

    def changed_response(method, path, payload=None):
        result = original(method, path, payload)
        if path.endswith("/events"):
            result = copy.deepcopy(result)
            result["campaign"]["approved_adapter"]["science_revision"] = "b" * 40
        return result

    session.api.request = changed_response
    with pytest.raises(ValueError, match="context"):
        session.worker._send(entry, "heartbeat")
    assert entry["claim"]["campaign"] == before
    assert entry["pending_event"]["type"] == "heartbeat"


def test_poll_get_context_change_is_rejected_before_local_binding(session, monkeypatch):
    session.worker.sweep()
    entry = entries(session)[0]
    before = copy.deepcopy(entry["claim"]["campaign"])
    session.cluster.jobs[entry["job_id"]]["state"] = "RUNNING"
    original = session.api.request

    def changed_response(method, path, payload=None):
        result = original(method, path, payload)
        if method == "GET" and path.endswith(session.campaign["id"]):
            result = copy.deepcopy(result)
            result["campaign"]["approved_adapter"]["science_revision"] = "b" * 40
        return result

    session.api.request = changed_response
    monkeypatch.setattr(session.worker, "accounting", lambda entry: None)
    monkeypatch.setattr(session.worker, "_send", lambda *args, **kwargs: None)
    with pytest.raises(ValueError, match="context"):
        session.worker.poll(entry)
    assert entry["claim"]["campaign"] == before
