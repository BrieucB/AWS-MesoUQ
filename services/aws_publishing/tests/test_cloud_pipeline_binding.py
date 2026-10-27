"""Actual API admission and successful-event profile authority tests."""

import copy

import pytest

from aws_publishing.api import CANONICAL_PROFILE
from test_api import env, upload, spec, call, error, claim, event, result


def binding():
    return {
        "id": CANONICAL_PROFILE,
        "bank_sha256": "f" * 64,
        "model_sha256": {"3.2": "b" * 64},
        "verified_dependency_file_count": 200,
    }


def canonical_spec(artifact):
    body = spec(artifact)
    body.update(
        kind="calibration",
        configuration={
            "scientific_profile": CANONICAL_PROFILE,
            "calibration_data": "curve_only",
        },
    )
    return body


def install(env):
    model = env[1].state["models"]["p:v1"]
    model["provenance"] = {"pipeline_binding": binding()}
    # The accepted baseline hash is lineage; a candidate can have different bytes.
    model["validation"] = {
        "passed": True,
        "model_sha256": "a" * 64,
        "training_data_sha256": "c" * 64,
    }
    model["scientific_profile"] = {"artifact_root": "/private/canonical/root"}
    return model


def test_canonical_request_requires_authoritative_registry_binding(env):
    artifact = upload(env)
    error(
        "invalid_model",
        lambda: call(env, "POST", "/v1/projects/p/jobs", canonical_spec(artifact)),
    )
    assert not env[1].state["jobs"]
    assert not env[1].state["requests"]


@pytest.mark.parametrize("bad", [None, {}, {"id": "other-profile"}])
def test_bad_authoritative_binding_cannot_admit_canonical_profile(env, bad):
    artifact = upload(env)
    env[1].state["models"]["p:v1"]["provenance"] = {"pipeline_binding": bad}
    error(
        "invalid_model",
        lambda: call(env, "POST", "/v1/projects/p/jobs", canonical_spec(artifact)),
    )
    assert not env[1].state["jobs"]


def test_bound_model_cannot_cross_into_legacy_request_and_private_root_not_copied(env):
    artifact = upload(env)
    model = install(env)
    error(
        "invalid_model",
        lambda: call(env, "POST", "/v1/projects/p/jobs", spec(artifact)),
    )
    job = call(env, "POST", "/v1/projects/p/jobs", canonical_spec(artifact))[1]
    approved = job["approved_model"]
    assert approved["provenance"]["pipeline_binding"] == binding()
    assert approved["validation"] == model["validation"]
    assert approved["sha256"]["3.2"] != binding()["model_sha256"]["3.2"]
    assert "scientific_profile" not in approved
    assert "/private/canonical/root" not in str(approved)
    model["validation"]["training_data_sha256"] = "d" * 64
    model["provenance"]["pipeline_binding"]["bank_sha256"] = "e" * 64
    assert approved["validation"]["training_data_sha256"] == "c" * 64
    assert approved["provenance"]["pipeline_binding"] == binding()


@pytest.mark.parametrize("mutation", ["missing", "hash", "extra", "type"])
def test_success_requires_exact_pinned_pipeline_binding(env, mutation):
    artifact = upload(env)
    install(env)
    job = call(env, "POST", "/v1/projects/p/jobs", canonical_spec(artifact))[1]
    lease = claim(env)
    event(env, lease, "submitting")
    event(env, lease, "submitted", slurm_job_id="12")
    event(env, lease, "running")
    scientific = result(job)
    scientific["kind"] = "calibration"
    scientific["provenance"]["pipeline_binding"] = binding()
    bad = copy.deepcopy(scientific)
    if mutation == "missing":
        bad["provenance"].pop("pipeline_binding")
    elif mutation == "hash":
        bad["provenance"]["pipeline_binding"]["bank_sha256"] = "0" * 64
    elif mutation == "extra":
        bad["provenance"]["pipeline_binding"]["extra"] = "invented"
    else:
        bad["provenance"]["pipeline_binding"]["verified_dependency_file_count"] = 200.0
    error(
        "invalid_provenance",
        lambda: event(env, lease, "succeeded", event_id="bad", result=bad),
    )
    assert env[1].state["jobs"][job["id"]]["status"] == "running"
    succeeded = event(env, lease, "succeeded", event_id="valid", result=scientific)
    assert succeeded["status"] == "succeeded"
    assert (
        event(env, lease, "succeeded", event_id="valid", result=scientific)["status"]
        == "succeeded"
    )
    error(
        "event_conflict",
        lambda: event(env, lease, "succeeded", event_id="valid", result=bad),
    )


def test_legacy_success_cannot_invent_canonical_binding(env):
    artifact = upload(env)
    job = call(env, "POST", "/v1/projects/p/jobs", spec(artifact))[1]
    lease = claim(env)
    event(env, lease, "submitting")
    event(env, lease, "submitted", slurm_job_id="12")
    scientific = result(job)
    scientific["provenance"]["pipeline_binding"] = binding()
    error(
        "invalid_provenance", lambda: event(env, lease, "succeeded", result=scientific)
    )
