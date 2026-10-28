"""Real control protocol, in-memory objects and fake scheduler; no AWS/science calls."""
import copy
import gc
import hashlib
import json
from pathlib import Path
from urllib.error import HTTPError
from urllib.parse import parse_qs, urlsplit
import weakref

import pytest

from aws_mesouq.benchmark_worker import BenchmarkWorker
from test_benchmark_worker import session


def publication(session, bodies=(b'{"first":1}', b'{"second":2}')):
    campaign = session.store.state["benchmark_campaigns"][session.campaign["id"]]
    for stage in campaign["stages"]:
        if stage["id"] != "metrics":
            stage["status"] = "succeeded" if stage["id"] == "cases" else "blocked"
    campaign["case_set_sha256"] = "d" * 64
    entry = session.worker._claim()
    assert entry["claim"]["stage"]["id"] == "metrics"
    root = Path(session.operator["run_root"]) / campaign["id"]
    stage_root = root / "stages" / "metrics"
    stage_root.mkdir(parents=True, mode=0o700)
    (root / "stages").chmod(0o700)
    control = root / "control" / "metrics"
    control.mkdir(parents=True, mode=0o700)
    (root / "control").chmod(0o700)
    entry.update(root=str(root), control=str(control))
    refs = []
    for index, blob in enumerate(bodies):
        name = f"posterior-{index:03d}.npz"
        path = stage_root / name
        path.write_bytes(blob)
        path.chmod(0o600)
        refs.append({"file": name, "sha256": hashlib.sha256(blob).hexdigest(), "size_bytes": len(blob)})
    session.worker.save()
    return entry, {"artifacts": refs}


def restart(session):
    session.journal.data = json.loads(session.journal.path.read_text())
    session.worker = BenchmarkWorker(session.api, session.slurm, session.journal, project="p", operator=session.operator, clock=lambda: session.clock[0])
    return next(iter(session.worker.state["stages"].values()))


def conditional_put(session, calls, *, fail_second=False):
    def transfer(url, *, data=None, headers=None):
        key = urlsplit(url).path.lstrip("/")
        calls.append(key)
        if key in session.objects.data:
            raise HTTPError(url, 412, "Already exists", {}, None)
        if fail_second and len(set(calls)) > 1:
            raise OSError("Fixture second-object transfer interrupted")
        session.objects.data[key] = bytes(data)
        return b""
    return transfer


def test_partial_two_object_upload_restarts_without_reputting_confirmed_object(session, monkeypatch):
    entry, receipt = publication(session)
    calls = []
    monkeypatch.setattr(session.api, "transfer", conditional_put(session, calls, fail_second=True))
    with pytest.raises(OSError):
        session.worker.publish(entry, receipt)
    original_ids = [u["artifact"]["id"] for u in entry["upload_plan"]]
    assert [item["status"] for item in entry["upload_progress"].values()] == ["confirmed", "putting"]
    first_key = next(iter(session.objects.data))
    entry = restart(session)
    resumed_calls = []
    monkeypatch.setattr(session.api, "transfer", conditional_put(session, resumed_calls))
    refs = session.worker.publish(entry, receipt)
    assert [ref["id"] for ref in refs] == original_ids
    assert first_key not in resumed_calls
    assert len(resumed_calls) == 1 and len(session.objects.data) == 2
    assert len(session.store.state["benchmark_campaigns"][session.campaign["id"]]["artifacts"]) == 2
    reservation_events = [event for event in session.api.event_ids if event[0] == "artifact_upload"]
    assert len({event_id for _, event_id in reservation_events}) == 1


@pytest.mark.parametrize("acknowledgment", ["lost", "conditional_412"])
def test_ambiguous_or_412_put_only_succeeds_after_exact_backend_probe(session, monkeypatch, acknowledgment):
    entry, receipt = publication(session, (b"coherent-posterior",))
    calls = []
    def transfer(url, *, data=None, headers=None):
        key = urlsplit(url).path.lstrip("/")
        calls.append(key)
        session.objects.data[key] = bytes(data)
        if acknowledgment == "conditional_412":
            raise HTTPError(url, 412, "Already committed", {}, None)
        raise OSError("Fixture acknowledgment lost after commit")
    monkeypatch.setattr(session.api, "transfer", transfer)
    refs = session.worker.publish(entry, receipt)
    assert len(refs) == 1 and len(calls) == 1
    assert entry["upload_progress"][refs[0]["id"]]["status"] == "confirmed"


def test_expired_signed_url_and_lease_refresh_keep_original_reservation(session, monkeypatch):
    entry, receipt = publication(session, (b"complete-posterior",))
    generated = []
    def signed_url(key, artifact):
        generated.append(key)
        return f"https://uploads.example.invalid/{key}?expires={session.clock[0] + 900}&nonce={len(generated)}", {"If-None-Match": "*"}
    monkeypatch.setattr(session.objects, "upload_url", signed_url)
    calls = []
    def transfer(url, *, data=None, headers=None):
        calls.append(url)
        if len(calls) == 1:
            session.clock[0] += 901
        expires = int(parse_qs(urlsplit(url).query)["expires"][0])
        if session.clock[0] > expires:
            raise HTTPError(url, 403, "Fixture signed URL expired", {}, None)
        key = urlsplit(url).path.lstrip("/")
        assert key not in session.objects.data
        session.objects.data[key] = bytes(data)
        return b""
    monkeypatch.setattr(session.api, "transfer", transfer)
    refs = session.worker.publish(entry, receipt)
    assert len(calls) == 2 and calls[0] != calls[1]
    assert len(refs) == 1
    assert entry["claim"]["lease_expires_at"] > session.clock[0]
    assert len(session.store.state["benchmark_campaigns"][session.campaign["id"]]["artifacts"]) == 1
    assert len({event_id for kind, event_id in session.api.event_ids if kind == "artifact_upload"}) == 1


@pytest.mark.parametrize("corrupt", [b"wrong-same-size!", b"x"])
def test_existing_wrong_checksum_or_size_is_never_overwritten_or_accepted(session, monkeypatch, corrupt):
    expected = b"right-original!"
    entry, receipt = publication(session, (expected,))
    calls = []
    def transfer(url, *, data=None, headers=None):
        key = urlsplit(url).path.lstrip("/")
        calls.append(key)
        session.objects.data[key] = corrupt
        raise HTTPError(url, 412, "Fixture object exists", {}, None)
    monkeypatch.setattr(session.api, "transfer", transfer)
    with pytest.raises(Exception) as exc:
        session.worker.publish(entry, receipt)
    assert getattr(exc.value, "code", None) == "artifact_integrity_mismatch"
    assert len(calls) == 1
    assert list(session.objects.data.values()) == [corrupt]
    assert not entry.get("published_refs")


def test_completed_markers_are_rechecked_after_restart(session, monkeypatch):
    entry, receipt = publication(session, (b"immutable-file",))
    calls = []
    monkeypatch.setattr(session.api, "transfer", conditional_put(session, calls))
    refs = session.worker.publish(entry, receipt)
    old_key = next(iter(session.objects.data))
    session.objects.data.clear()
    entry = restart(session)
    session.worker.publish(entry, receipt)
    assert calls == [old_key, old_key]
    session.objects.data[old_key] = b"corrupt"
    entry = restart(session)
    with pytest.raises(Exception) as exc:
        session.worker.publish(entry, receipt)
    assert getattr(exc.value, "code", None) == "artifact_integrity_mismatch"
    assert len(calls) == 2
    assert refs[0]["id"] in entry["upload_progress"]


def test_publication_holds_only_one_artifact_body(session, monkeypatch):
    entry, receipt = publication(session, (b"a" * 1024, b"b" * 1024, b"c" * 1024))
    references = []
    original_file = session.worker._file
    class Buffer(bytearray):
        pass
    def read_one(*args, **kwargs):
        gc.collect()
        assert not any(reference() is not None for reference in references)
        blob, metadata = original_file(*args, **kwargs)
        buffered = Buffer(blob)
        references.append(weakref.ref(buffered))
        return buffered, metadata
    monkeypatch.setattr(session.worker, "_file", read_one)
    monkeypatch.setattr(session.api, "transfer", conditional_put(session, []))
    session.worker.publish(entry, receipt)
    gc.collect()
    assert all(reference() is None for reference in references)
    assert all("blob" not in item for item in entry["upload_source"])


def test_legacy_cached_plan_without_original_event_id_refreshes_without_duplicates(session, monkeypatch):
    entry, receipt = publication(session, (b"legacy-journal",))
    monkeypatch.setattr(session.api, "transfer", conditional_put(session, []))
    session.worker.publish(entry, receipt)
    identity = entry["upload_plan"][0]["artifact"]["id"]
    entry.pop("upload_reservation")
    session.objects.data.clear()
    entry["upload_plan"][0]["upload_url"] = "https://uploads.example.invalid/expired-cache"
    session.worker.save()
    entry = restart(session)
    refs = session.worker.publish(entry, receipt)
    assert refs[0]["id"] == identity
    assert len(session.store.state["benchmark_campaigns"][session.campaign["id"]]["artifacts"]) == 1
