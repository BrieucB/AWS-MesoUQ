"""Reservations release without racing scientific completion or deleting verified data."""

import hashlib

import pytest

from aws_publishing.api import PENDING_UPLOAD_SECONDS
from aws_publishing.storage import Conflict
from test_api import env, call, error
from test_release_policy import production


def pending(production):
    data = b"force,displacement\n1,2\n3,4\n"
    _, response = call(production, "POST", "/v1/projects/p/artifacts", {
        "name": "curve.csv", "kind": "curve", "media_type": "text/csv",
        "sha256": hashlib.sha256(data).hexdigest(), "size_bytes": len(data),
    })
    ident = response["artifact"]["id"]
    key = production[1].state["artifacts"][ident]["storage_key"]
    production[2].data[key] = data
    return ident, key


def test_old_unverified_upload_expires_before_delete_and_keeps_summary(production):
    ident, key = pending(production)
    production[3][0] += PENDING_UPLOAD_SECONDS - 1
    production[0].maintenance()
    assert key in production[2].data
    production[3][0] += 1
    original = production[2].discard

    def check_commit_precedes_delete(storage_key):
        artifact = production[1].state["artifacts"][ident]
        assert "upload_expired_at" in artifact and not artifact["verified"]
        error("upload_expired", lambda: call(production, "POST", f"/v1/projects/p/artifacts/{ident}/complete", {}))
        original(storage_key)

    production[2].discard = check_commit_precedes_delete
    production[0].maintenance()
    assert key not in production[2].data
    assert production[1].state["artifacts"][ident]["reservation_released"] is True


def test_cleanup_failure_retains_reservation_and_retries(production):
    ident, key = pending(production)
    production[3][0] += PENDING_UPLOAD_SECONDS
    original = production[2].discard

    def fail(storage_key):
        raise OSError("retry cleanup")

    production[2].discard = fail
    with pytest.raises(OSError, match="retry cleanup"):
        production[0].maintenance()
    artifact = production[1].state["artifacts"][ident]
    assert "upload_expired_at" in artifact and not artifact.get("reservation_released")
    production[2].discard = original
    production[0].maintenance()
    assert production[1].state["artifacts"][ident]["reservation_released"] is True
    assert key not in production[2].data


def test_completion_winning_cas_prevents_cleanup(production, monkeypatch):
    ident, key = pending(production)
    production[3][0] += PENDING_UPLOAD_SECONDS
    original = production[1].save
    raced = []

    def race(revision, before, after):
        if not raced:
            raced.append(True)
            production[1].state["artifacts"][ident]["verified"] = True
            production[1].revision += 1
            raise Conflict("Concurrent completion won")
        original(revision, before, after)

    monkeypatch.setattr(production[1], "save", race)
    production[0].maintenance()
    artifact = production[1].state["artifacts"][ident]
    assert artifact["verified"] and "upload_expired_at" not in artifact
    assert not artifact.get("reservation_released") and key in production[2].data


def test_verified_unarchived_never_deleted_and_archived_releases_only_after_absence(production):
    ident, key = pending(production)
    call(production, "POST", f"/v1/projects/p/artifacts/{ident}/complete", {})
    production[3][0] += 100 * PENDING_UPLOAD_SECONDS
    production[0].maintenance()
    assert key in production[2].data
    assert not production[1].state["artifacts"][ident].get("reservation_released")
    production[1].state["artifacts"][ident]["archive_tagged"] = True
    # Present but corrupted bytes must not be mistaken for an expired object.
    production[2].data[key] = b"corrupted"
    production[0].maintenance()
    assert not production[1].state["artifacts"][ident].get("reservation_released")
    del production[2].data[key]
    production[0].maintenance()
    assert production[1].state["artifacts"][ident]["reservation_released"] is True


def test_tiny_remaining_budget_defers_delete_but_expiry_is_durable(production):
    ident, key = pending(production)
    production[3][0] += PENDING_UPLOAD_SECONDS
    production[0].maintenance(remaining_ms=lambda: 5000)
    assert "upload_expired_at" in production[1].state["artifacts"][ident]
    assert key in production[2].data
    production[0].maintenance()
    assert key not in production[2].data
