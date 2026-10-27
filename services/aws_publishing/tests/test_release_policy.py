"""Production admission cannot be bypassed through directly crafted HTTP bodies."""

import json
import base64
import copy
import hashlib

import pytest

from aws_mesouq.release import configuration
from aws_publishing.api import Application
from aws_publishing import handler
from test_api import env, upload, spec, call, error, claim, event, result
from test_cloud_pipeline_binding import install, binding
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives import serialization
from aws_publishing.attestation import archive_payload, result_payload


@pytest.fixture
def production(env):
    # Upload through the real strict service and install a synthetic trusted model.
    env[0].release_policy = "v1"
    install(env)
    env[1].state["models"]["p:v1"]["approved_adapter_sha256"] = "d" * 64
    env[1].device_key = Ed25519PrivateKey.generate()
    key = base64.b64encode(env[1].device_key.public_key().public_bytes(
        serialization.Encoding.Raw, serialization.PublicFormat.Raw)).decode()
    env[1].state["identities"]["alice"]["connector_public_keys"] = {"c1": key}
    return env


def locked_spec(artifact):
    body = spec(artifact)
    body.update(kind="calibration", configuration=configuration(3.2))
    return body


def publish_file(env, name, blob):
    media = "application/json" if name.endswith(".json") else "text/csv" if name.endswith(".csv") else "image/png" if name.endswith(".png") else "application/pdf"
    _, response = call(env, "POST", "/v1/projects/p/artifacts", {
        "name": name.replace("/", "__"), "kind": "manifest" if name == "provenance.json" else "result",
        "sha256": hashlib.sha256(blob).hexdigest(), "size_bytes": len(blob), "media_type": media,
    })
    artifact = response["artifact"]
    env[2].data[env[1].state["artifacts"][artifact["id"]]["storage_key"]] = blob
    call(env, "POST", f"/v1/projects/p/artifacts/{artifact['id']}/complete", {})
    return artifact


def complete_result(env, job, *, upload_outputs=True):
    scientific = result(job)
    scientific.update(kind="calibration", interval_probability=0.9)
    scientific["parameters"] = [
        {"name": name, "unit": unit, "lower": low, "median": mid, "upper": high}
        for name, unit, low, mid, high in (
            ("ka", "DPD stiffness", 8500, 9500, 10500),
            ("kb", "DPD energy", 17000, 20000, 23000),
            ("d0", "DPD length", .05, .1, .15),
        )
    ]
    scientific["provenance"]["pipeline_binding"] = binding()
    scientific["provenance"]["runtime"]["adapter_sha256"] = "d" * 64
    scientific["publication_files"] = [
        "result.json", "provenance.json", "settings_and_versions.json", "posterior_curve.csv",
        "parameter_intervals.csv", "fitted_curve.csv", "resonance.csv",
        *[f"figures/{stem}.{ext}" for stem in ("posterior", "fitted_curve", "resonance") for ext in ("png", "pdf")],
    ]
    if not upload_outputs:
        return scientific
    refs = []
    for name in scientific["publication_files"]:
        if name == "result.json":
            blob = json.dumps(scientific).encode()
        elif name == "provenance.json":
            blob = json.dumps(scientific["provenance"]).encode()
        elif name == "settings_and_versions.json":
            blob = json.dumps({"preset_id": "indentation-sonovue-v1", "configuration": job["configuration"], "provenance": scientific["provenance"]}).encode()
        elif name.endswith(".csv"):
            blob = b"synthetic,fixture\n1,2\n"
        else:
            blob = b"synthetic figure fixture"
        refs.append(publish_file(env, name, blob))
    scientific["artifacts"] = refs
    return scientific


def signed_event(env, lease, kind, *, event_id=None, signer=None, **fields):
    event_id = event_id or kind
    payload = {"project_id": "p", "connector_id": "c1", "event_id": event_id, "type": kind, **fields}
    job = env[1].state["jobs"][lease["job"]["id"]]
    if kind == "succeeded":
        fields.setdefault("slurm_job_id", job["slurm_job_id"])
        payload["slurm_job_id"] = fields["slurm_job_id"]
    canonical = archive_payload if kind == "archive_verified" else result_payload
    signature = base64.b64encode((signer or env[1].device_key).sign(canonical(job, payload))).decode()
    field = "archive_signature" if kind == "archive_verified" else "result_signature"
    return event(env, lease, kind, event_id=event_id, **fields, **{field: signature})


def test_default_application_and_handler_use_v1_policy(production, monkeypatch):
    assert Application(production[1], production[2]).release_policy == "v1"
    # HTTP adapter receives a real strict Application; body flags cannot select
    # its constructor-only internal compatibility policy.
    monkeypatch.setattr(handler, "application", lambda: production[0])
    monkeypatch.setenv("COGNITO_ISSUER", "https://identity.invalid")
    monkeypatch.setenv("COGNITO_CLIENT_ID", "browser")
    artifact = upload(production)
    for kind in ("prediction", "training"):
        body = locked_spec(artifact)
        body["kind"] = kind
        response = handler.handler({
            "rawPath": "/v1/projects/p/jobs",
            "requestContext": {"http": {"method": "POST"}, "authorizer": {"jwt": {"claims": {
                "sub": "alice", "iss": "https://identity.invalid", "client_id": "browser", "token_use": "access",
            }}}},
            "body": json.dumps(body),
        }, None)
        assert response["statusCode"] == 400
    assert not production[1].state["jobs"]
    assert not production[1].state["requests"]


@pytest.mark.parametrize("site", ["karolina", "vega"])
def test_strict_admission_idempotency_and_site_claim_preserved(production, site):
    artifact = upload(production)
    body = locked_spec(artifact)
    status, job = call(production, "POST", "/v1/projects/p/jobs", body)
    assert status == 202
    assert job["configuration"] == configuration(3.2)
    assert job["approved_model"]["provenance"]["pipeline_binding"]["id"] == "uq_emb_sonovue_202607"
    assert call(production, "POST", "/v1/projects/p/jobs", body)[1]["id"] == job["id"]
    claim = call(production, "POST", "/v1/connector/claim", {
        "project_id": "p", "connector_id": "connector", "site": site,
    })[1]
    assert claim["job"]["id"] == job["id"] and claim["job"]["site"] == site


@pytest.mark.parametrize("mutation", ["population", "joint", "sampler", "two", "policy"])
def test_crafted_scientific_edits_never_admitted(production, mutation):
    artifact = upload(production)
    body = locked_spec(artifact)
    if mutation == "population":
        body["configuration"]["calibration_mode"] = "population"
    elif mutation == "joint":
        body["configuration"]["calibration_data"] = "accepted_joint"
    elif mutation == "sampler":
        body["configuration"]["pop_size"] = 32
    elif mutation == "two":
        body["input_artifacts"].append(artifact)
    else:
        body["release_policy"] = "nonproduction_legacy"
    error("invalid_artifacts" if mutation == "two" else "invalid_request" if mutation == "policy" else "invalid_configuration",
          lambda: call(production, "POST", "/v1/projects/p/jobs", body))
    assert not production[1].state["jobs"]


def test_injected_generic_validator_cannot_bypass_release_policy(production):
    production[0].validate_configuration = lambda value, **kwargs: value
    artifact = upload(production)
    error("invalid_configuration", lambda: call(production, "POST", "/v1/projects/p/jobs", spec(artifact)))


@pytest.mark.parametrize("kind", ["training_table", "model", "validation"])
def test_retraining_artifacts_and_release_endpoint_unavailable(production, kind):
    error("invalid_artifact", lambda: upload(production, kind=kind))
    error("not_found", lambda: call(production, "POST", "/v1/projects/p/models/v1/release", {}))


def test_v1_capacity_gate_is_ten_active_jobs(production):
    artifact = upload(production)
    for index in range(10):
        body = locked_spec(artifact)
        body["idempotency_key"] = str(index)
        assert call(production, "POST", "/v1/projects/p/jobs", body)[0] == 202
    body["idempotency_key"] = "eleven"
    error("capacity_exceeded", lambda: call(production, "POST", "/v1/projects/p/jobs", body))


@pytest.mark.parametrize("site", ["karolina", "vega"])
def test_success_requires_locked_interval_and_exact_pinned_binding(production, site):
    artifact = upload(production)
    job = call(production, "POST", "/v1/projects/p/jobs", locked_spec(artifact))[1]
    lease = claim(production, site=site)
    event(production, lease, "submitting")
    event(production, lease, "submitted", slurm_job_id="42")
    event(production, lease, "running")
    scientific = complete_result(production, job)
    scientific["interval_probability"] = 0.95
    error("invalid_result", lambda: signed_event(production, lease, "succeeded", event_id="wrong-interval", result=scientific))
    scientific["interval_probability"] = 0.9
    scientific["provenance"]["pipeline_binding"]["bank_sha256"] = "0" * 64
    error("invalid_result", lambda: signed_event(production, lease, "succeeded", event_id="wrong-binding", result=scientific))
    scientific["provenance"]["pipeline_binding"] = binding()
    assert signed_event(production, lease, "succeeded", event_id="success", result=scientific)["status"] == "succeeded"
    assert signed_event(production, lease, "succeeded", event_id="success", result=scientific)["status"] == "succeeded"


def running_job(production, *, site="karolina", archive_capable=True):
    artifact = upload(production)
    job = call(production, "POST", "/v1/projects/p/jobs", locked_spec(artifact))[1]
    lease = claim(production, site=site, archive_capable=archive_capable)
    event(production, lease, "submitting")
    event(production, lease, "submitted", slurm_job_id="42")
    event(production, lease, "running")
    return job, lease, artifact


def receipt(job, scientific):
    return {"site": "karolina", "path": "/private/project/run/output", "artifacts": [
        {key: ref[key] for key in ("id", "sha256", "size_bytes")}
        for ref in [*job["input_artifacts"], *scientific["artifacts"]]
    ]}


@pytest.mark.parametrize("mutation", ["missing", "wrong-key", "modified", "unregistered"])
def test_browser_cannot_forge_scientific_completion(production, mutation):
    job, lease, _ = running_job(production)
    scientific = complete_result(production, job)
    if mutation == "missing":
        fn = lambda: event(production, lease, "succeeded", result=scientific)
    elif mutation == "wrong-key":
        fn = lambda: signed_event(production, lease, "succeeded", result=scientific, signer=Ed25519PrivateKey.generate())
    elif mutation == "unregistered":
        production[1].state["identities"]["alice"]["connector_public_keys"] = {}
        fn = lambda: signed_event(production, lease, "succeeded", result=scientific)
    else:
        payload = {"project_id": "p", "connector_id": "c1", "event_id": "succeeded", "result": scientific}
        signature = base64.b64encode(production[1].device_key.sign(result_payload(job, payload))).decode()
        scientific["diagnostics"]["altered"] = True
        fn = lambda: event(production, lease, "succeeded", result=scientific, result_signature=signature)
    error("invalid_connector_signature", fn)
    assert production[1].state["jobs"][job["id"]]["status"] == "running"


@pytest.mark.parametrize("mutation", ["sparse", "parameter", "curve", "probability", "adapter", "artifacts", "publication", "metadata", "bytes", "json", "units", "dense-curve"])
def test_signed_incomplete_or_mismatched_results_rejected(production, mutation):
    job, lease, _ = running_job(production)
    scientific = complete_result(production, job)
    if mutation == "sparse":
        scientific = result(job)
        scientific.update(kind="calibration", interval_probability=.9)
    elif mutation == "parameter":
        scientific["parameters"] = scientific["parameters"][:1]
    elif mutation == "curve":
        scientific["curves"] = []
    elif mutation == "dense-curve":
        for name in ("force", "observed_displacement", "median", "lower", "upper"):
            scientific["curves"][0][name] *= 1001
    elif mutation == "probability":
        scientific["interval_probability"] = .95
    elif mutation == "adapter":
        scientific["provenance"]["runtime"]["adapter_sha256"] = "0" * 64
    elif mutation == "artifacts":
        scientific["artifacts"].pop()
    elif mutation == "publication":
        scientific["publication_files"] = ["result.json", "provenance.json"]
    elif mutation == "metadata":
        scientific["artifacts"][0]["name"] = "forged.json"
    elif mutation == "units":
        scientific["parameters"][0]["unit"] = "Pa"
    elif mutation == "bytes":
        ref = scientific["artifacts"][0]
        production[2].data[production[1].state["artifacts"][ref["id"]]["storage_key"]] = b"changed bytes"
    else:
        scientific["diagnostics"]["changed-after-publish"] = True
    error("invalid_result", lambda: signed_event(production, lease, "succeeded", result=scientific))
    assert production[1].state["jobs"][job["id"]]["status"] == "running"


@pytest.mark.parametrize("mutation", ["missing", "wrong-key", "modified", "relative", "failed", "vega", "not-capable", "missing-output"])
def test_forged_or_ineligible_archive_cannot_start_retention(production, mutation):
    job, lease, artifact = running_job(production, site="vega" if mutation == "vega" else "karolina", archive_capable=mutation != "not-capable")
    scientific = complete_result(production, job)
    if mutation == "failed":
        event(production, lease, "failed")
    else:
        signed_event(production, lease, "succeeded", result=scientific)
    archive = receipt(job, scientific)
    if mutation == "relative":
        archive["path"] = "NOT_REAL"
    elif mutation == "missing-output":
        archive["artifacts"].pop()
    if mutation == "missing":
        fn = lambda: event(production, lease, "archive_verified", archive=archive)
    elif mutation == "wrong-key":
        fn = lambda: signed_event(production, lease, "archive_verified", archive=archive, signer=Ed25519PrivateKey.generate())
    elif mutation == "modified":
        payload = {"project_id": "p", "connector_id": "c1", "event_id": "archive_verified", "archive": archive}
        signature = base64.b64encode(production[1].device_key.sign(archive_payload(job, payload))).decode()
        archive["path"] = "/private/project/changed"
        fn = lambda: event(production, lease, "archive_verified", archive=archive, archive_signature=signature)
    else:
        fn = lambda: signed_event(production, lease, "archive_verified", archive=archive)
    error("invalid_archive", fn)
    assert "archive_verified_at" not in production[1].state["artifacts"][artifact["id"]]


def test_trusted_archive_retains_all_inputs_outputs_for_thirty_days(production):
    from aws_publishing.api import ARCHIVE_RETENTION_SECONDS
    job, lease, artifact = running_job(production)
    scientific = complete_result(production, job)
    signed_event(production, lease, "succeeded", result=scientific)
    archive = receipt(job, scientific)
    signed_event(production, lease, "archive_verified", archive=archive)
    assert production[1].state["artifacts"][artifact["id"]]["archive_verified_at"] == 1000
    assert not production[2].tags
    production[3][0] += ARCHIVE_RETENTION_SECONDS
    production[0].maintenance()
    assert len(production[2].tags) == 1 + len(scientific["artifacts"])


def test_member_cannot_expire_another_members_input_through_failed_vega_job(production):
    from aws_publishing.api import ARCHIVE_RETENTION_SECONDS
    artifact = upload(production, sub="alice")
    job = call(production, "POST", "/v1/projects/p/jobs", locked_spec(artifact), sub="bob")[1]
    lease = claim(production, sub="bob", site="vega", archive_capable=False)
    event(production, lease, "failed", sub="bob")
    archive = {"site": "karolina", "path": "NOT_REAL", "artifacts": [
        {key: artifact[key] for key in ("id", "sha256", "size_bytes")}
    ]}
    error("invalid_archive", lambda: event(production, lease, "archive_verified", sub="bob", archive=archive))
    production[3][0] += ARCHIVE_RETENTION_SECONDS + 1
    production[0].maintenance()
    stored = production[1].state["artifacts"][artifact["id"]]
    assert "archive_verified_at" not in stored
    assert stored["storage_key"] in production[2].data
    assert not production[2].tags
    assert production[1].state["jobs"][job["id"]]["status"] == "failed"
