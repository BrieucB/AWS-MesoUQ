"""Trusted operator policy and concurrent cloud-storage admissions; no scientific compute."""
import copy
import json
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace

import pytest

from aws_mesouq.benchmark import (
    ARTIFACT_PROJECT_BYTES, expected_artifact_bytes, project_artifact_charge,
    validate_benchmark_policy,
)
from aws_publishing.api import APIError, Application, ARCHIVE_RETENTION_SECONDS
from test_benchmark import (
    lab, create, request, specification, assert_error, claim, event_body, send,
)


def fallback_lab(lab, policies):
    app = Application(lab[1], lab[2], clock=lambda: lab[3][0], benchmark_policies=policies)
    return (app, *lab[1:])


def policy(lab):
    return copy.deepcopy(lab[1].state["projects"]["p"]["benchmark_policy"])


def test_trusted_fallback_is_project_scoped_and_absence_preserves_old_denial(lab):
    approved = policy(lab)
    lab[1].state["projects"]["p"].pop("benchmark_policy")
    assert_error("benchmark_not_enrolled", lambda: create(lab))
    wrong = fallback_lab(lab, {"other-project": approved})
    assert_error("benchmark_not_enrolled", lambda: create(wrong))
    deployed = fallback_lab(lab, {"p": approved})
    campaign = create(deployed)
    assert campaign["approved_adapter"] == approved
    assert_error("not_invited", lambda: request(deployed, "POST", "/v1/projects/p/benchmarks", specification("uninvited"), sub="unknown"))
    malicious = specification("browser-pin")
    malicious["benchmark_policy"] = approved
    assert_error("invalid_benchmark", lambda: request(deployed, "POST", "/v1/projects/p/benchmarks", malicious))


def test_project_operator_binding_overrides_fallback_and_invalid_binding_fails_closed(lab):
    fallback = policy(lab)
    fallback["adapter_sha256"] = "f" * 64
    deployed = fallback_lab(lab, {"p": fallback})
    assert create(deployed)["approved_adapter"]["adapter_sha256"] == "a" * 64
    lab[1].state["projects"]["p"]["benchmark_policy"] = {}
    assert_error("invalid_benchmark", lambda: create(deployed, "invalid-operator-policy"))


@pytest.mark.parametrize("bad", [
    {"adapter_sha256": "a" * 63},
    {"science_revision": "not-a-commit"},
    {"scenario_families": ["baseline", "baseline"]},
    {"scenario_families": ["unknown"]},
    {"archive_root": "/private/../outside"},
    {"archive_root": "/"},
    {"max_concurrent_stages": True},
    {"untrusted_command": "execute"},
])
def test_fallback_policy_rejects_invalid_operator_configuration(lab, bad):
    approved = {**policy(lab), **bad}
    with pytest.raises(ValueError):
        fallback_lab(lab, {"p": approved})


def test_lambda_loads_only_operator_environment_policy(monkeypatch, lab):
    from aws_publishing import handler
    boto3 = pytest.importorskip("boto3")
    approved = policy(lab)
    monkeypatch.setenv("BENCHMARK_POLICIES", json.dumps({"p": approved}))
    for name, value in {"STATE_TABLE": "fixture-table", "DATA_BUCKET": "fixture-bucket", "AWS_REGION": "eu-central-1", "EVENT_QUEUE_URL": "https://queue.example.invalid/fixture"}.items():
        monkeypatch.setenv(name, value)
    monkeypatch.setattr(boto3, "client", lambda *args, **kwargs: object())
    monkeypatch.setattr(handler, "DynamoStore", lambda *args: lab[1])
    monkeypatch.setattr(handler, "S3Objects", lambda *args: lab[2])
    monkeypatch.setattr(handler, "SQSNotifications", lambda *args: None)
    monkeypatch.setattr(handler, "_application", None)
    initialized = handler.application()
    assert initialized.benchmark_policies == {"p": approved}
    assert initialized.release_policy == "v1"


def test_concurrent_large_campaigns_reserve_once_before_any_compute(lab):
    def admit(index):
        body = specification(f"concurrent-{index}")
        body["evaluation"].update(case_count=256, posterior_draws=8192)
        try:
            return request(lab, "POST", "/v1/projects/p/benchmarks", body)[0]
        except APIError as error:
            return error.code
    with ThreadPoolExecutor(max_workers=2) as pool:
        outcomes = list(pool.map(admit, range(2)))
    assert sorted(map(str, outcomes)) == ["202", "benchmark_storage_quota_exceeded"]
    assert len(lab[1].state["benchmark_campaigns"]) == 1
    assert project_artifact_charge(lab[1].state, "p") <= ARTIFACT_PROJECT_BYTES
    assert lab[1].state["jobs"] == {}
    assert lab[2].data == {}


def test_uploads_consume_existing_reservation_without_double_counting(lab):
    campaign = create(lab)
    original = expected_artifact_bytes(campaign["configuration"])
    assert project_artifact_charge(lab[1].state, "p") == original
    owned = claim(lab, "cases")
    def upload(event_id, size):
        ref = {"name": event_id + ".npz", "sha256": "e" * 64, "size_bytes": size, "media_type": "application/octet-stream"}
        body = event_body(lab, owned, event_id, "artifact_upload", artifacts=[ref])
        return request(lab, "POST", f"/v1/benchmark-connector/campaigns/{campaign['id']}/stages/cases/artifacts", body)
    upload("part1", 10000000)
    assert project_artifact_charge(lab[1].state, "p") == original
    upload("part2", 20000000)
    assert project_artifact_charge(lab[1].state, "p") == 30000000
    assert len(lab[1].state["benchmark_campaigns"][campaign["id"]]["artifacts"]) == 2


def test_terminal_cancel_releases_unused_expected_storage_but_retains_object_reservations(lab):
    campaign = create(lab)
    owned = claim(lab, "cases")
    ref = {"name": "partial.json", "sha256": "e" * 64, "size_bytes": 10, "media_type": "application/json"}
    body = event_body(lab, owned, "partial", "artifact_upload", artifacts=[ref])
    request(lab, "POST", f"/v1/benchmark-connector/campaigns/{campaign['id']}/stages/cases/artifacts", body)
    request(lab, "POST", f"/v1/projects/p/benchmarks/{campaign['id']}/cancel", {})
    send(lab, owned, "cancel-unsubmitted", "cancelled", not_submitted=True)
    saved = lab[1].state["benchmark_campaigns"][campaign["id"]]
    assert saved["artifact_reservation"]["status"] == "settled"
    assert project_artifact_charge(lab[1].state, "p") == 10


def test_legacy_project_uploads_cannot_spend_benchmark_reserved_capacity(lab):
    body = specification()
    body["evaluation"].update(case_count=256, posterior_draws=8192)
    request(lab, "POST", "/v1/projects/p/benchmarks", body)
    lab[1].state["artifacts"]["existing"] = {"id": "existing", "project_id": "p", "owner_sub": "bob", "kind": "result", "size_bytes": 7 * 1024 * 1024, "verified": True}
    legacy = {"name": "result.json", "kind": "result", "sha256": "f" * 64, "size_bytes": 1100000, "media_type": "application/json"}
    assert_error("artifact_quota_exceeded", lambda: request(lab, "POST", "/v1/projects/p/artifacts", legacy))
    assert set(lab[1].state["artifacts"]) == {"existing"}


def test_preflight_counts_existing_retained_legacy_reservations_and_caps_report_remaining(lab):
    lab[1].state["artifacts"]["existing"] = {"project_id": "p", "size_bytes": 9 * 1024 * 1024}
    body = specification()
    body["evaluation"].update(case_count=256, posterior_draws=8192)
    assert_error("benchmark_storage_quota_exceeded", lambda: request(lab, "POST", "/v1/projects/p/benchmarks", body))
    caps = request(lab, "GET", "/v1/projects/p/benchmarks/capabilities")[1]
    assert caps["artifact_budget"]["project_available_bytes"] == ARTIFACT_PROJECT_BYTES - 9 * 1024 * 1024
    assert caps["artifact_budget"]["artifact_bytes"] == 100000000


def test_real_fastapi_deadline_defers_existing_maintenance_and_keeps_membership_checks(lab, monkeypatch):
    from aws_publishing import handler
    from container_smoke import event
    monkeypatch.setenv("COGNITO_ISSUER", "https://issuer.example.invalid")
    monkeypatch.setenv("COGNITO_CLIENT_ID", "fixture-client")
    monkeypatch.setattr(handler, "application", lambda: lab[0])
    monkeypatch.setattr(handler, "_benchmark_adapter", None)
    lab[1].state["artifacts"]["old"] = {"id": "old", "project_id": "p", "owner_sub": "alice", "size_bytes": 1, "storage_key": "private-old", "archive_verified_at": lab[3][0] - ARCHIVE_RETENTION_SECONDS}
    monkeypatch.setattr(lab[2], "archived", lambda key: pytest.fail("Short invocation budget must defer S3 maintenance"))
    payload = event("/v1/projects/p/benchmarks/capabilities", "GET")
    payload["requestContext"]["authorizer"] = {"jwt": {"claims": {"sub": "alice", "iss": "https://issuer.example.invalid", "client_id": "fixture-client", "token_use": "access"}}}
    context = SimpleNamespace(get_remaining_time_in_millis=lambda: 9000)
    assert handler.handler(payload, context)["statusCode"] == 200
    assert not lab[1].state["artifacts"]["old"].get("archive_tagged")
    payload["requestContext"]["authorizer"]["jwt"]["claims"]["sub"] = "uninvited"
    assert handler.handler(payload, context)["statusCode"] == 403


def test_archive_accepts_verified_subset_and_explicitly_quarantines_partial_uploads(lab):
    from test_benchmark import start, accounting, archive_event
    lab[1].state["projects"]["p"]["benchmark_policy"]["archive_root"] = "/approved/durable"
    campaign = create(lab)
    owned = claim(lab, "cases")
    partial = {"name": "never-committed.json", "sha256": "e" * 64, "size_bytes": 100, "media_type": "application/json"}
    body = event_body(lab, owned, "partial", "artifact_upload", artifacts=[partial])
    request(lab, "POST", f"/v1/benchmark-connector/campaigns/{campaign['id']}/stages/cases/artifacts", body)
    start(lab, owned)
    request(lab, "POST", f"/v1/projects/p/benchmarks/{campaign['id']}/cancel", {})
    send(lab, owned, "cancelled", "cancelled", timing={"wall_seconds": 10, "cpu_seconds": 20, "gpu_seconds": 10}, scheduler_accounting=accounting(state="CANCELLED"))
    request(lab, "POST", f"/v1/projects/p/benchmarks/{campaign['id']}/archive", {})
    receipt = {"site": "karolina", "path": "/approved/durable/fixture", "manifest_sha256": "f" * 64, "artifacts": [], "retained_outputs": [{"path": "archive-manifest.json", "sha256": "f" * 64, "size_bytes": 1000}], "attested_original_file_count": 3001, "attested_original_files_sha256": "d" * 64}
    response = request(lab, "POST", "/v1/benchmark-connector/archive", archive_event(lab, campaign, receipt))[1]["campaign"]
    assert response["archive"]["status"] == "verified"
    assert len(response["archive"]["quarantined_unverified_artifact_ids"]) == 1
    assert response["artifacts"][0]["verified"] is False
    assert response["artifacts"][0]["retention_status"] == "quarantined_unverified"
    assert project_artifact_charge(lab[1].state, "p") == 100


def test_metrics_waits_for_every_fresh_factor_and_storage_remains_reserved(lab):
    from test_benchmark import finish_cases, start, result, accounting
    campaign = create(lab)
    finish_cases(lab)
    claimed = [claim(lab, f"parallel-{index}") for index in range(9)]
    global_factor = next(c for c in claimed if c["stage"]["id"] == "train_global")
    send(lab, global_factor, "failed", "failed", not_submitted=True, failure={"code": "training_failed", "message": "Fixture training failure"})
    for current in claimed:
        if current["stage"]["id"] in ("tmcmc", "reference"):
            start(lab, current)
            send(lab, current, "done", "succeeded", result=result(current, gpu=10), scheduler_accounting=accounting())
    assert claim(lab, "too-early-metrics")["stage"] is None
    assert lab[1].state["benchmark_campaigns"][campaign["id"]]["artifact_reservation"]["status"] == "active"
    for current in claimed:
        if current["stage"]["id"].startswith("train_") and current["stage"]["id"] != "train_global":
            send(lab, current, "cancel-unsubmitted", "cancelled", not_submitted=True)
    assert claim(lab, "ready-metrics")["stage"]["id"] == "metrics"


def test_typed_wire_preserves_scientific_integer_budgets_and_strict_bounds():
    from aws_publishing.benchmark_http import CreateCampaign
    from pydantic import ValidationError
    from aws_mesouq.benchmark import validate_create
    wire = CreateCampaign.model_validate(specification()).model_dump(mode="json")
    stored = validate_create(wire)
    assert type(stored["controls"]["hnpe"]["training_budget"]) is int
    assert type(stored["budget"]["method_gpu_seconds"]) is int
    assert type(stored["scenario"]["scale"]) is float
    for field, value in (("training_budget", 1800.0), ("training_budget", 119), ("max_updates", 1000001)):
        bad = specification()
        bad["controls"]["hnpe"][field] = value
        with pytest.raises(ValidationError):
            CreateCampaign.model_validate(bad)
