"""Reservations bound storage before any caller receives an upload URL."""

import copy
import hashlib
from concurrent.futures import ThreadPoolExecutor

import pytest

from aws_publishing import api
from aws_publishing.api import APIError
from test_api import env, call


@pytest.fixture
def strict(env):
    env[0].release_policy = "v1"
    return env


def request(size=20, *, kind="curve"):
    return {
        "name": "curve.csv" if kind == "curve" else "result.json",
        "kind": kind,
        "size_bytes": size,
        "sha256": hashlib.sha256(b"x" * min(size, 100)).hexdigest(),
        "media_type": "text/csv" if kind == "curve" else "application/json",
    }


def reserve(env, size=20, **kwargs):
    return call(env, "POST", "/v1/projects/p/artifacts", request(size, **kwargs))


@pytest.mark.parametrize("kind,limit", [
    ("curve", api.CURVE_UPLOAD_MAX_BYTES),
    ("result", api.RESULT_UPLOAD_MAX_BYTES),
    ("posterior", api.RESULT_UPLOAD_MAX_BYTES),
])
def test_per_object_bound_applies_before_reservation(strict, kind, limit):
    assert reserve(strict, limit, kind=kind)[0] == 201
    before = copy.deepcopy(strict[1].state)
    with pytest.raises(APIError) as exc:
        reserve(strict, limit + 1, kind=kind)
    assert (exc.value.status, exc.value.code) == (413, "artifact_too_large")
    assert strict[1].state == before


def test_pending_uploads_consume_user_capacity(strict, monkeypatch):
    monkeypatch.setattr(api, "ARTIFACT_USER_MAX_BYTES", 40)
    reserve(strict)
    reserve(strict)
    with pytest.raises(APIError, match="quota"):
        reserve(strict, 1)
    assert len(strict[1].state["artifacts"]) == 2
    assert all(not a["verified"] for a in strict[1].state["artifacts"].values())


def test_global_limit_cannot_be_bypassed_with_another_project(strict, monkeypatch):
    monkeypatch.setattr(api, "ARTIFACT_TOTAL_MAX_BYTES", 30)
    reserve(strict)
    with pytest.raises(APIError) as exc:
        call(strict, "POST", "/v1/projects/q/artifacts", request(), sub="outsider")
    assert exc.value.code == "artifact_quota_exceeded"


def test_concurrent_reservations_cannot_overcommit(strict, monkeypatch):
    monkeypatch.setattr(api, "ARTIFACT_MAX_COUNT", 1)

    def attempt(_):
        try:
            return reserve(strict)[0]
        except APIError as exc:
            return exc.status

    with ThreadPoolExecutor(max_workers=8) as workers:
        statuses = list(workers.map(attempt, range(8)))
    assert statuses.count(201) == 1
    assert set(statuses) <= {201, 429}
    assert len(strict[1].state["artifacts"]) == 1


def test_only_confirmed_released_reservations_restore_capacity(strict, monkeypatch):
    monkeypatch.setattr(api, "ARTIFACT_MAX_COUNT", 1)
    _, response = reserve(strict)
    artifact = strict[1].state["artifacts"][response["artifact"]["id"]]
    artifact["upload_expired_at"] = strict[3][0]
    with pytest.raises(APIError):
        reserve(strict)
    artifact["reservation_released"] = True
    assert reserve(strict)[0] == 201
    assert len(strict[1].state["artifacts"]) == 2  # Summary survives reclamation.


def test_bad_completion_does_not_restore_capacity(strict, monkeypatch):
    monkeypatch.setattr(api, "ARTIFACT_MAX_COUNT", 1)
    _, response = reserve(strict)
    artifact = response["artifact"]
    record = strict[1].state["artifacts"][artifact["id"]]
    strict[2].data[record["storage_key"]] = b"oversized and wrong checksum" * 20
    with pytest.raises(APIError):
        call(strict, "POST", f"/v1/projects/p/artifacts/{artifact['id']}/complete", {})
    with pytest.raises(APIError) as exc:
        reserve(strict)
    assert exc.value.code == "artifact_quota_exceeded"
    assert not strict[1].state["artifacts"][artifact["id"]]["verified"]


def test_presigned_contract_includes_exact_transfer_length(strict):
    _, response = reserve(strict, 123)
    assert response["required_headers"]["Content-Length"] == "123"
