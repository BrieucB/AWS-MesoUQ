import base64
import copy
import hashlib
import json
from concurrent.futures import ThreadPoolExecutor
import pytest
from aws_publishing.api import Application, APIError, digest, ARCHIVE_RETENTION_SECONDS
from aws_publishing.storage import MemoryStore, MemoryObjects, Conflict
from aws_mesouq.contracts import validate_job_configuration, validate_result


@pytest.fixture
def env():
    store, objects, clock = MemoryStore(), MemoryObjects(), [1000]
    store.state["projects"] = {"p": {"name": "Project"}, "q": {"name": "Other"}}
    store.state["identities"] = {
        "alice": {"email": "alice@example.invalid", "projects": {"p": "maintainer"}},
        "bob": {"projects": {"p": "member"}},
        "outsider": {"projects": {"q": "maintainer"}},
        "disabled": {"disabled": True, "projects": {"p": "maintainer"}},
    }
    store.state["models"]["p:v1"] = {
        "project_id": "p",
        "version": "v1",
        "trusted": True,
        "status": "released",
        "format": "safetensors",
        "sha256": {"3.2": "a" * 64, "3.4": "b" * 64, "5.8": "c" * 64},
    }
    app = Application(
        store,
        objects,
        clock=lambda: clock[0],
        validate_configuration=validate_job_configuration,
        validate_result=validate_result,
        release_policy="nonproduction_legacy",
    )
    return app, store, objects, clock


def call(env, method, path, body=None, sub="alice"):
    return env[0].handle(method, path, {"sub": sub}, body)


def error(code, fn):
    with pytest.raises(APIError) as exc:
        fn()
    assert exc.value.code == code


def upload(env, sub="alice", kind="curve", data=None, artifact_metadata=None):
    data = data if data is not None else b"force,displacement\n1,2\n3,4\n"
    metadata = dict(
        name="curve.csv",
        kind=kind,
        sha256=hashlib.sha256(data).hexdigest(),
        size_bytes=len(data),
        media_type="text/csv",
    )
    if artifact_metadata is None and kind == "curve":
        artifact_metadata = {
            "force_unit": "nN",
            "displacement_unit": "nm",
            "column_order": "force-displacement",
            "nominal_diameter_um": 3.2,
        }
    elif artifact_metadata is None and kind == "training_table":
        artifact_metadata = {
            "nominal_diameter_um": 3.2,
            "table_schema": "mesouq_indentation_wide_dpd_v1",
        }
    if artifact_metadata is not None:
        metadata["metadata"] = artifact_metadata
    _, response = call(env, "POST", "/v1/projects/p/artifacts", metadata, sub)
    artifact = response["artifact"]
    env[2].data[env[1].state["artifacts"][artifact["id"]]["storage_key"]] = data
    call(env, "POST", f"/v1/projects/p/artifacts/{artifact['id']}/complete", {}, sub)
    return {
        k: artifact[k]
        for k in (
            "id",
            "name",
            "kind",
            "sha256",
            "size_bytes",
            "media_type",
            "metadata",
        )
        if k in artifact
    }


def spec(artifact, key="one"):
    return dict(
        schema_version=1,
        kind="prediction",
        model_version="v1",
        diameter_um=3.2,
        configuration={"parameters": {"Yt": 5e6, "kb": 1.0}},
        input_artifacts=[artifact],
        idempotency_key=key,
    )


def claim(
    env,
    sub="alice",
    connector="c1",
    site="karolina",
    archive_capable=None,
    exclude_job_ids=None,
):
    return call(
        env,
        "POST",
        "/v1/connector/claim",
        {
            "project_id": "p",
            "connector_id": connector,
            "site": site,
            **(
                {"archive_capable": archive_capable}
                if archive_capable is not None
                else {}
            ),
            **(
                {"exclude_job_ids": exclude_job_ids}
                if exclude_job_ids is not None
                else {}
            ),
        },
        sub,
    )[1]


def event(env, claimed, kind, event_id=None, sub="alice", **fields):
    body = dict(
        project_id="p",
        connector_id="c1",
        lease_token=claimed["lease_token"],
        event_id=event_id or kind,
        type=kind,
        **fields,
    )
    return call(
        env, "POST", f"/v1/connector/jobs/{claimed['job']['id']}/events", body, sub
    )[1]


def result(job):
    return dict(
        schema_version=1,
        kind="prediction",
        provenance={
            "job_id": job["id"],
            "model_version": job["model_version"],
            "code_sha256": "d" * 64,
            "model_sha256": job["approved_model"]["sha256"],
            "data_sha256": {ref["id"]: ref["sha256"] for ref in job["input_artifacts"]},
            "config_sha256": digest(job["configuration"]),
            "runtime": {"python": "3.11"},
        },
        interval_probability=0.95,
        parameters=[
            {"name": "E", "unit": "Pa", "median": 2.0, "lower": 1.0, "upper": 3.0}
        ],
        curves=[
            {
                "dataset_id": "curve",
                "force": [1],
                "observed_displacement": [2],
                "median": [2],
                "lower": [1],
                "upper": [3],
                "force_unit": "N",
                "displacement_unit": "m",
            }
        ],
        resonance={"status": "unavailable", "reason": "No acoustic measurements"},
        diagnostics={},
    )


def test_authentication_and_server_membership(env):
    assert env[0].handle("GET", "/health")[0] == 200
    error("unauthenticated", lambda: env[0].handle("GET", "/v1/me"))
    error("not_invited", lambda: call(env, "GET", "/v1/me", sub="stranger"))
    error("not_invited", lambda: call(env, "GET", "/v1/me", sub="disabled"))
    error("not_found", lambda: call(env, "GET", "/v1/projects/p/jobs", sub="outsider"))
    assert call(env, "GET", "/v1/me")[1]["subject"] == "alice"
    # A forged group/role claim cannot change a server-side member to maintainer.
    artifact = upload(env)
    body = spec(artifact)
    body["kind"] = "training"
    body["configuration"] = {"disp_source": "displacement"}
    error(
        "maintainer_required",
        lambda: env[0].handle(
            "POST",
            "/v1/projects/p/jobs",
            {"sub": "bob", "groups": ["maintainer"]},
            body,
        ),
    )
    body = spec(artifact)
    body["owner_sub"] = "bob"
    error("invalid_request", lambda: call(env, "POST", "/v1/projects/p/jobs", body))


def test_upload_verification_and_private_access(env):
    data = b"data"
    metadata = dict(
        name="../../elsewhere",
        kind="curve",
        sha256=hashlib.sha256(data).hexdigest(),
        size_bytes=4,
        media_type="text/csv",
    )
    _, response = call(env, "POST", "/v1/projects/p/artifacts", metadata)
    artifact = response["artifact"]
    stored = env[1].state["artifacts"][artifact["id"]]
    assert "../../" not in stored["storage_key"]
    error(
        "checksum_mismatch",
        lambda: call(
            env, "POST", f"/v1/projects/p/artifacts/{artifact['id']}/complete"
        ),
    )
    env[2].data[stored["storage_key"]] = b"evil"
    error(
        "checksum_mismatch",
        lambda: call(
            env, "POST", f"/v1/projects/p/artifacts/{artifact['id']}/complete"
        ),
    )
    env[2].data[stored["storage_key"]] = data
    error(
        "owner_required",
        lambda: call(
            env,
            "POST",
            f"/v1/projects/p/artifacts/{artifact['id']}/complete",
            sub="bob",
        ),
    )
    call(env, "POST", f"/v1/projects/p/artifacts/{artifact['id']}/complete")
    assert call(env, "GET", f"/v1/projects/p/artifacts/{artifact['id']}", sub="bob")[1][
        "download_url"
    ].startswith("memory://")
    error(
        "not_found",
        lambda: call(
            env, "GET", f"/v1/projects/p/artifacts/{artifact['id']}", sub="outsider"
        ),
    )


def test_idempotency_and_capacity_under_concurrency(env):
    artifact = upload(env)

    def submit(i):
        try:
            return call(env, "POST", "/v1/projects/p/jobs", spec(artifact, str(i)))[1]
        except APIError as exc:
            return exc.code

    with ThreadPoolExecutor(max_workers=16) as pool:
        responses = list(pool.map(submit, range(20)))
    assert len(env[1].state["jobs"]) == 10
    assert sum(isinstance(r, dict) for r in responses) == 10
    assert all(
        isinstance(r, dict) or r in ("capacity_exceeded", "concurrent_update")
        for r in responses
    )
    first = next(r for r in responses if isinstance(r, dict))
    again = call(
        env, "POST", "/v1/projects/p/jobs", spec(artifact, first["idempotency_key"])
    )[1]
    assert again["id"] == first["id"]
    changed = spec(artifact, first["idempotency_key"])
    changed["diameter_um"] = 3.4
    error(
        "idempotency_conflict",
        lambda: call(env, "POST", "/v1/projects/p/jobs", changed),
    )


def test_own_connector_claim_and_replay_guards(env):
    artifact = upload(env)
    job = call(env, "POST", "/v1/projects/p/jobs", spec(artifact))[1]
    assert claim(env, sub="bob") == {"job": None}
    lease = claim(env)
    assert "lease" not in lease["job"]
    assert claim(env, connector="other") == {"job": None}
    error("owner_required", lambda: event(env, lease, "submitting", sub="bob"))
    submitted = event(env, lease, "submitting")
    assert submitted["status"] == "submitting"
    assert event(env, lease, "submitting")["status"] == "submitting"
    error(
        "event_conflict",
        lambda: event(env, lease, "submitting", detail={"changed": True}),
    )
    env[3][0] += 121
    error("invalid_lease", lambda: event(env, lease, "heartbeat"))
    recovered = claim(env, connector="other")
    assert recovered["job"]["status"] == "submission_unknown"
    assert recovered["job"]["id"] == job["id"]
    # No transition can turn an uncertain submission into a fresh submission.
    body = dict(
        project_id="p",
        connector_id="other",
        lease_token=recovered["lease_token"],
        event_id="fresh",
        type="submitting",
    )
    error(
        "invalid_transition",
        lambda: call(env, "POST", f"/v1/connector/jobs/{job['id']}/events", body),
    )


def test_cancel_race_records_scheduler_identity(env):
    artifact = upload(env)
    job = call(env, "POST", "/v1/projects/p/jobs", spec(artifact))[1]
    lease = claim(env)
    event(env, lease, "submitting")
    error(
        "owner_required",
        lambda: call(env, "POST", f"/v1/projects/p/jobs/{job['id']}/cancel", sub="bob"),
    )
    call(env, "POST", f"/v1/projects/p/jobs/{job['id']}/cancel")
    updated = event(env, lease, "submitted", slurm_job_id="123")
    assert updated["status"] == "cancelling" and updated["slurm_job_id"] == "123"
    assert event(env, lease, "cancelled")["status"] == "cancelled"
    error("invalid_transition", lambda: event(env, lease, "running"))


def test_result_and_archive_before_expiration(env):
    artifact = upload(env)
    job = call(env, "POST", "/v1/projects/p/jobs", spec(artifact))[1]
    lease = claim(env)
    event(env, lease, "submitting")
    event(env, lease, "submitted", slurm_job_id="12")
    event(env, lease, "running")
    bad_result = result(job)
    bad_result["parameters"][0]["median"] = 10
    error("invalid_result", lambda: event(env, lease, "succeeded", result=bad_result))
    event(env, lease, "succeeded", result=result(job))
    assert call(env, "GET", f"/v1/projects/p/jobs/{job['id']}/result", sub="bob")[
        1
    ] == result(job)
    assert env[2].tags == {}
    # A durable connector may resume after success solely to acknowledge archive.
    lease = claim(env, archive_capable=True)
    assert lease["job"]["id"] == job["id"] and lease["job"]["status"] == "succeeded"
    receipt = {
        "site": "karolina",
        "path": "/canonical/run/output",
        "artifacts": [{k: artifact[k] for k in ("id", "sha256", "size_bytes")}],
    }
    corrupt = copy.deepcopy(receipt)
    corrupt["artifacts"][0]["sha256"] = "0" * 64
    error(
        "invalid_archive",
        lambda: event(env, lease, "archive_verified", archive=corrupt),
    )
    assert env[2].tags == {}
    event(env, lease, "archive_verified", archive=receipt)
    assert env[2].tags == {}
    env[3][0] += ARCHIVE_RETENTION_SECONDS
    env[0].maintenance()
    assert env[2].tags
    assert all(v == {"archive_verified": "true"} for v in env[2].tags.values())
    assert env[1].state["jobs"][job["id"]]["archive"]["verified_by"] == "alice"
    assert claim(env) == {"job": None}


def test_same_connector_resume_and_site_preservation(env):
    artifact = upload(env)
    job = call(env, "POST", "/v1/projects/p/jobs", spec(artifact))[1]
    original = claim(env, site="vega")
    assert original["job"]["id"] == job["id"]
    recovered = claim(env, site="vega")
    assert recovered["lease_token"] != original["lease_token"]
    error("invalid_lease", lambda: event(env, original, "heartbeat"))
    assert claim(env, site="karolina") == {"job": None}


def test_models_reject_untrusted_and_nonmaintainer_release(env):
    artifact = upload(env, kind="model")
    validation = upload(env, kind="validation")
    model = dict(
        project_id="p",
        version="v2",
        trusted=False,
        status="candidate",
        format="safetensors",
        sha256=artifact["sha256"],
        validation={"passed": True},
        artifact_id=artifact["id"],
        validation_artifact_id=validation["id"],
    )
    env[1].state["models"]["p:v2"] = model
    error(
        "maintainer_required",
        lambda: call(env, "POST", "/v1/projects/p/models/v2/release", sub="bob"),
    )
    error(
        "model_not_validated",
        lambda: call(env, "POST", "/v1/projects/p/models/v2/release"),
    )
    model["trusted"] = True
    env[1].state["models"]["p:v2"] = model
    released = call(env, "POST", "/v1/projects/p/models/v2/release")[1]
    assert released["status"] == "released"
    assert call(env, "POST", "/v1/projects/p/models/v2/release")[1] == released


def test_scientific_configuration_injection_rejected(env):
    artifact = upload(env)
    for field in (
        "command",
        "backend",
        "data_dir",
        "account",
        "partition",
        "unknown_scientific_field",
    ):
        body = spec(artifact)
        body["configuration"] = {field: "evil"}
        error(
            "invalid_configuration",
            lambda: call(env, "POST", "/v1/projects/p/jobs", body),
        )


def test_receipt_commit_precedes_lifecycle_side_effect(env):
    artifact = upload(env)
    job = call(env, "POST", "/v1/projects/p/jobs", spec(artifact))[1]
    lease = claim(env)
    event(env, lease, "failed")
    receipt = {
        "site": "karolina",
        "path": "/archive",
        "artifacts": [{k: artifact[k] for k in ("id", "sha256", "size_bytes")}],
    }
    original = env[2].archived

    def checked(key):
        assert env[1].state["jobs"][job["id"]].get("archive")
        original(key)

    env[2].archived = checked
    event(env, lease, "archive_verified", archive=receipt)
    assert env[2].tags == {}
    env[3][0] += ARCHIVE_RETENTION_SECONDS
    env[0].maintenance()
    assert env[2].tags


def test_replay_survives_lease_rotation(env):
    artifact = upload(env)
    call(env, "POST", "/v1/projects/p/jobs", spec(artifact))
    original = claim(env)
    event(env, original, "submitting", event_id="stable-id")
    replacement = claim(env)
    assert replacement["job"]["status"] == "submission_unknown"
    assert (
        event(env, replacement, "submitting", event_id="stable-id")["status"]
        == "submission_unknown"
    )


def test_training_registers_immutable_candidate_and_release(env):
    curve = upload(env, kind="training_table")
    body = spec(curve)
    body["kind"] = "training"
    body["configuration"] = {"disp_source": "displacement"}
    job = call(env, "POST", "/v1/projects/p/jobs", body)[1]
    lease = claim(env)
    event(env, lease, "submitting")
    event(env, lease, "submitted", slurm_job_id="42")
    event(env, lease, "running")
    validation_report = {"passed": True, "method": "held_out_source_curves"}
    validation_bytes = (
        json.dumps(validation_report, sort_keys=True, indent=2) + "\n"
    ).encode()
    model, validation = upload(env, kind="model"), upload(
        env, kind="validation", data=validation_bytes
    )
    trained = result(job)
    trained["kind"] = "training"
    trained["artifacts"] = [model, validation]
    trained["training"] = {
        "model_version": "v2",
        "model_sha256": model["sha256"],
        "metrics": {"validation_rmse_dpd": 0.1},
        "validation": {"passed": True, "method": "held_out_source_curves"},
        "validation_sha256": validation["sha256"],
    }
    event(env, lease, "succeeded", result=trained)
    candidate = env[1].state["models"]["p:v2"]
    assert (
        candidate["source_job_id"] == job["id"] and candidate["status"] == "candidate"
    )
    released = call(env, "POST", "/v1/projects/p/models/v2/release")[1]
    assert released["status"] == "released" and released["sha256"] == model["sha256"]


def test_lifecycle_failure_retries_after_durable_receipt(env):
    artifact = upload(env)
    job = call(env, "POST", "/v1/projects/p/jobs", spec(artifact))[1]
    lease = claim(env)
    event(env, lease, "failed")
    receipt = {
        "site": "karolina",
        "path": "/archive",
        "artifacts": [{k: artifact[k] for k in ("id", "sha256", "size_bytes")}],
    }
    original = env[2].archived

    def unavailable(key):
        raise OSError("S3 unavailable")

    env[2].archived = unavailable
    event(env, lease, "archive_verified", archive=receipt)
    assert env[1].state["jobs"][job["id"]]["archive"]
    assert env[2].tags == {}
    env[3][0] += ARCHIVE_RETENTION_SECONDS
    with pytest.raises(OSError):
        env[0].maintenance()
    assert env[1].state["jobs"][job["id"]]["archive"]["verified_at"] == 1000
    env[2].archived = original
    env[0].maintenance()
    assert env[2].tags


def test_artifact_metadata_is_immutable_and_authoritative(env):
    artifact = upload(env)
    forged = copy.deepcopy(artifact)
    forged["metadata"] = {
        "force_unit": "N",
        "displacement_unit": "m",
        "nominal_diameter_um": 5.8,
    }
    error(
        "invalid_artifacts",
        lambda: call(env, "POST", "/v1/projects/p/jobs", spec(forged)),
    )
    job = call(env, "POST", "/v1/projects/p/jobs", spec(artifact))[1]
    assert job["input_artifacts"] == [artifact]
    assert job["configuration"]["surrogate"] == {"backend": "dnn"}
    assert job["approved_model"]["sha256"]["3.2"] == "a" * 64
    env[1].state["models"]["p:v1"]["sha256"]["3.2"] = "d" * 64
    assert (
        call(env, "GET", f"/v1/projects/p/jobs/{job['id']}")[1]["approved_model"][
            "sha256"
        ]["3.2"]
        == "a" * 64
    )


def test_missing_model_hash_or_bundle_diameter_rejected(env):
    artifact = upload(env)
    del env[1].state["models"]["p:v1"]["sha256"]
    error(
        "invalid_model",
        lambda: call(env, "POST", "/v1/projects/p/jobs", spec(artifact)),
    )
    env[1].state["models"]["p:v1"]["sha256"] = {"3.2": "a" * 64}
    body = spec(artifact)
    body["configuration"]["emb_diameters"] = [3.2, 5.8]
    error("invalid_model", lambda: call(env, "POST", "/v1/projects/p/jobs", body))
    env[1].state["models"]["p:v1"]["sha256"] = "a" * 64
    error(
        "invalid_model",
        lambda: call(env, "POST", "/v1/projects/p/jobs", spec(artifact)),
    )


@pytest.mark.parametrize(
    "field,value",
    [
        ("job_id", "another-job"),
        ("model_version", "different-model"),
        ("model_sha256", {"3.2": "f" * 64}),
        ("data_sha256", {}),
        ("config_sha256", "0" * 64),
    ],
)
def test_success_binds_scientific_provenance(env, field, value):
    artifact = upload(env)
    job = call(env, "POST", "/v1/projects/p/jobs", spec(artifact))[1]
    lease = claim(env)
    event(env, lease, "submitting")
    event(env, lease, "submitted", slurm_job_id="77")
    event(env, lease, "running")
    forged = result(job)
    forged["provenance"][field] = value
    error("invalid_provenance", lambda: event(env, lease, "succeeded", result=forged))
    assert env[1].state["jobs"][job["id"]]["status"] == "running"


def test_canonical_dnn_configuration_is_accepted(env):
    artifact = upload(env)
    body = spec(artifact)
    body["configuration"] = {
        "surrogate": {"backend": "dnn"},
        "description": "Research experiment",
        "parameters": {"Yt": 5e6, "kb": 1.0},
    }
    assert call(env, "POST", "/v1/projects/p/jobs", body)[0] == 202


def test_corrupt_or_expired_completed_upload_cannot_be_admitted(env):
    artifact = upload(env)
    key = env[1].state["artifacts"][artifact["id"]]["storage_key"]
    env[2].data[key] = b"corrupted after completion"
    error(
        "invalid_artifacts",
        lambda: call(env, "POST", "/v1/projects/p/jobs", spec(artifact)),
    )
    error(
        "artifact_unavailable",
        lambda: call(env, "GET", f"/v1/projects/p/artifacts/{artifact['id']}"),
    )


def test_validation_report_content_must_match_training_attestation(env):
    curve = upload(env, kind="training_table")
    body = spec(curve)
    body["kind"] = "training"
    body["configuration"] = {"disp_source": "displacement"}
    job = call(env, "POST", "/v1/projects/p/jobs", body)[1]
    lease = claim(env)
    event(env, lease, "submitting")
    event(env, lease, "submitted", slurm_job_id="43")
    event(env, lease, "running")
    model = upload(env, kind="model")
    validation = upload(
        env,
        kind="validation",
        data=b'{"passed":false,"method":"held_out_source_curves"}',
    )
    trained = result(job)
    trained["kind"] = "training"
    trained["artifacts"] = [model, validation]
    trained["training"] = {
        "model_version": "invalid-v2",
        "model_sha256": model["sha256"],
        "validation_sha256": validation["sha256"],
        "validation": {"passed": True, "method": "held_out_source_curves"},
    }
    error(
        "invalid_model_candidate",
        lambda: event(env, lease, "succeeded", result=trained),
    )
    assert "p:invalid-v2" not in env[1].state["models"]


def test_provenance_runtime_paths_are_data_not_executable_config(env):
    artifact = upload(env)
    job = call(env, "POST", "/v1/projects/p/jobs", spec(artifact))[1]
    lease = claim(env)
    event(env, lease, "submitting")
    event(env, lease, "submitted", slurm_job_id="17")
    event(env, lease, "running")
    actual = result(job)
    actual["provenance"]["runtime"].update(
        site="karolina",
        numpy="2.1",
        torch="2.6",
        device="cpu",
        profile={
            "python_bin": "/verified/python3.11",
            "environment_script": "/trusted/environment.sh",
            "environment_sha256": "e" * 64,
            "snapshot_path": "/trusted/snapshot",
        },
    )
    actual["provenance"]["source_files"] = {
        "src/meso_uq/publishing/runner.py": "e" * 64
    }
    assert event(env, lease, "succeeded", result=actual)["status"] == "succeeded"


@pytest.mark.parametrize(
    "kind,artifact_kind",
    [
        ("prediction", "training_table"),
        ("calibration", "training_table"),
        ("training", "curve"),
    ],
)
def test_admission_rejects_scientifically_wrong_artifact_kind(env, kind, artifact_kind):
    artifact = upload(env, kind=artifact_kind)
    body = spec(artifact)
    body["kind"] = kind
    if kind == "calibration":
        body["configuration"] = {}
    if kind == "training":
        body["configuration"] = {"disp_source": "displacement"}
    error("invalid_artifacts", lambda: call(env, "POST", "/v1/projects/p/jobs", body))
    assert not env[1].state["jobs"]


@pytest.mark.parametrize(
    "metadata",
    [
        {},
        {
            "force_unit": "wrong",
            "displacement_unit": "nm",
            "column_order": "force-displacement",
            "nominal_diameter_um": 3.2,
        },
        {
            "force_unit": "nN",
            "displacement_unit": "nm",
            "column_order": "ambiguous",
            "nominal_diameter_um": 3.2,
        },
        {
            "force_unit": "nN",
            "displacement_unit": "nm",
            "column_order": "force-displacement",
            "nominal_diameter_um": 5.8,
        },
    ],
)
def test_admission_validates_authoritative_curve_metadata(env, metadata):
    artifact = upload(env, artifact_metadata=metadata)
    error(
        "invalid_artifacts",
        lambda: call(env, "POST", "/v1/projects/p/jobs", spec(artifact)),
    )
    assert not env[1].state["jobs"]


def test_admission_calibration_mode_binds_selected_curve_count(env):
    first, second = upload(env), upload(env)
    body = spec(first)
    body["kind"] = "calibration"
    body["configuration"] = {}
    body["input_artifacts"] = [first, second]
    error("invalid_artifacts", lambda: call(env, "POST", "/v1/projects/p/jobs", body))
    body["configuration"] = {"calibration_mode": "population"}
    body["input_artifacts"] = [first]
    error("invalid_artifacts", lambda: call(env, "POST", "/v1/projects/p/jobs", body))
    body["input_artifacts"] = [first, second]
    assert call(env, "POST", "/v1/projects/p/jobs", body)[0] == 202


def test_training_displacement_interpretation_is_required_at_admission(env):
    table = upload(env, kind="training_table")
    body = spec(table)
    body["kind"] = "training"
    error(
        "invalid_configuration", lambda: call(env, "POST", "/v1/projects/p/jobs", body)
    )
    assert not env[1].state["jobs"]


def test_training_table_schema_is_required_at_admission(env):
    table = upload(
        env, kind="training_table", artifact_metadata={"nominal_diameter_um": 3.2}
    )
    body = spec(table)
    body["kind"] = "training"
    body["configuration"] = {"disp_source": "displacement"}
    error("invalid_artifacts", lambda: call(env, "POST", "/v1/projects/p/jobs", body))
    assert not env[1].state["jobs"]


@pytest.mark.parametrize(
    "kind,configuration",
    [
        ("calibration", {"fixed_params": {"Yt": 5e6}}),
        ("calibration", {"fixed_params": {"b1": 0}}),
        ("calibration", {"parameters": {"Yt": 5e6, "kb": 1}}),
        ("calibration", {"epochs": 10}),
        ("prediction", {}),
        ("prediction", {"parameters": {"Yt": 5e6, "kb": 1, "ka": 1}}),
        ("prediction", {"parameters": {"Yt": 5e6, "kb": 1}, "pop_size": 32}),
        ("prediction", {"parameters": {"Yt": 5e6, "kb": 1}, "forces": [1, 2]}),
        ("prediction", {"parameters": {"Yt": 5e6, "kb": 1}, "force_unit": "N"}),
        ("training", {"disp_source": "displacement", "pop_size": 32}),
        (
            "training",
            {"disp_source": "displacement", "parameters": {"Yt": 5e6, "kb": 1}},
        ),
    ],
)
def test_admission_rejects_ignored_or_unsupported_scientific_controls(
    env, kind, configuration
):
    artifact = upload(env, kind="training_table" if kind == "training" else "curve")
    body = spec(artifact)
    body["kind"], body["configuration"] = kind, configuration
    error(
        "invalid_configuration", lambda: call(env, "POST", "/v1/projects/p/jobs", body)
    )
    assert not env[1].state["jobs"]


def test_calibration_accepts_complete_supported_fixed_shape(env):
    artifact = upload(env)
    body = spec(artifact)
    body["kind"] = "calibration"
    body["configuration"] = {"fixed_params": {"b1": 0, "b2": 0, "a3": 0, "a4": 0}}
    assert call(env, "POST", "/v1/projects/p/jobs", body)[0] == 202


def test_default_application_validators_cannot_omit_scientific_admission(env):
    artifact = upload(env)
    app = Application(env[1], env[2])
    body = spec(artifact)
    body["configuration"] = {}
    error(
        "invalid_configuration",
        lambda: app.handle("POST", "/v1/projects/p/jobs", {"sub": "alice"}, body),
    )


def test_duplicate_input_identity_cannot_double_count_measurements(env):
    artifact = upload(env)
    body = spec(artifact)
    body["input_artifacts"] = [artifact, artifact]
    error("invalid_artifacts", lambda: call(env, "POST", "/v1/projects/p/jobs", body))
    assert not env[1].state["jobs"]


@pytest.mark.parametrize("site", ["karolina", "vega"])
def test_nonarchiving_connector_skips_completed_jobs_and_can_continue_work(env, site):
    artifact = upload(env)
    completed = call(env, "POST", "/v1/projects/p/jobs", spec(artifact))[1]
    lease = claim(env, site=site)
    event(env, lease, "submitting")
    event(env, lease, "submitted", slurm_job_id="501")
    event(env, lease, "running")
    event(env, lease, "succeeded", result=result(completed))
    assert claim(env, site=site) == {"job": None}  # absent capability defaults false
    assert claim(env, site=site, archive_capable=False) == {"job": None}
    assert not env[1].state["jobs"][completed["id"]].get("archive")
    assert not env[2].tags
    pending = call(env, "POST", "/v1/projects/p/jobs", spec(artifact, key="next-job"))[
        1
    ]
    assert claim(env, site=site)["job"]["id"] == pending["id"]
    assert claim(env, site=site, archive_capable=False)["job"]["id"] == pending["id"]


def test_durable_connector_can_explicitly_upgrade_to_archive_recovery(env):
    artifact = upload(env)
    completed = call(env, "POST", "/v1/projects/p/jobs", spec(artifact))[1]
    initial = claim(env)  # initial connector did not declare durable archival
    event(env, initial, "submitting")
    event(env, initial, "submitted", slurm_job_id="502")
    event(env, initial, "running")
    event(env, initial, "succeeded", result=result(completed))
    assert claim(env) == {"job": None}
    recovered = claim(env, archive_capable=True)
    assert recovered["job"]["id"] == completed["id"]
    assert recovered["job"]["status"] == "succeeded"
    assert recovered["lease_token"] != initial["lease_token"]
    # Declaring capability does not produce an archive receipt or expiration tag.
    assert not env[1].state["jobs"][completed["id"]].get("archive")
    assert not env[2].tags


@pytest.mark.parametrize("value", [None, 0, 1, "true", "false", [], {}])
def test_claim_archive_capability_requires_a_strict_json_boolean(env, value):
    error(
        "invalid_connector",
        lambda: call(
            env,
            "POST",
            "/v1/connector/claim",
            {
                "project_id": "p",
                "connector_id": "c1",
                "site": "karolina",
                "archive_capable": value,
            },
        ),
    )


@pytest.mark.parametrize("site", ["karolina", "vega"])
def test_claim_exclusions_make_a_fair_ten_job_sweep(env, site):
    artifact = upload(env)
    jobs = [
        call(env, "POST", "/v1/projects/p/jobs", spec(artifact, key=f"sweep-{i}"))[1]
        for i in range(10)
    ]
    seen, leases = [], set()
    for _ in range(10):
        claimed = claim(env, site=site, exclude_job_ids=seen)
        assert claimed["job"]["id"] not in seen
        assert claimed["lease_token"] not in leases
        seen.append(claimed["job"]["id"])
        leases.add(claimed["lease_token"])
        event(env, claimed, "submitting")
        event(env, claimed, "submitted", slurm_job_id=str(600 + len(seen)))
        event(env, claimed, "running")
    assert set(seen) == {job["id"] for job in jobs}
    assert claim(env, site=site, exclude_job_ids=seen) == {"job": None}
    # The next sweep can revisit every running job with a newly rotated lease.
    revisited = []
    for _ in range(10):
        current = claim(env, site=site, exclude_job_ids=revisited)
        assert current["job"]["status"] == "running"
        assert current["lease_token"] not in leases
        revisited.append(current["job"]["id"])
    assert set(revisited) == set(seen)


def test_claim_without_exclusions_keeps_previous_resume_behavior(env):
    artifact = upload(env)
    first = call(env, "POST", "/v1/projects/p/jobs", spec(artifact, key="first"))[1]
    second = call(env, "POST", "/v1/projects/p/jobs", spec(artifact, key="second"))[1]
    original = claim(env)
    assert original["job"]["id"] == first["id"]
    resumed = claim(env)
    assert resumed["job"]["id"] == first["id"]
    assert resumed["lease_token"] != original["lease_token"]
    assert claim(env, exclude_job_ids=[first["id"]])["job"]["id"] == second["id"]


@pytest.mark.parametrize(
    "excluded",
    [
        None,
        True,
        {},
        "job-1",
        [None],
        [1],
        [["job-1"]],
        [""],
        ["../job"],
        ["job/other"],
        ["x" * 129],
        ["job-1", "job-1"],
        [f"job-{i}" for i in range(11)],
    ],
)
def test_claim_exclusions_are_bounded_unique_safe_job_ids(env, excluded):
    error(
        "invalid_connector",
        lambda: call(
            env,
            "POST",
            "/v1/connector/claim",
            {
                "project_id": "p",
                "connector_id": "c1",
                "site": "karolina",
                "exclude_job_ids": excluded,
            },
        ),
    )


def test_claim_exclusions_cannot_expose_or_lease_other_owners_jobs(env):
    artifact = upload(env)
    alice = call(env, "POST", "/v1/projects/p/jobs", spec(artifact, key="alice-job"))[1]
    bob = call(
        env, "POST", "/v1/projects/p/jobs", spec(artifact, key="bob-job"), sub="bob"
    )[1]
    bob_before = copy.deepcopy(env[1].state["jobs"][bob["id"]])
    # Excluding another user's known/unknown IDs never grants access to that user.
    owned = claim(env, exclude_job_ids=[bob["id"], "unknown-job"])
    assert owned["job"]["id"] == alice["id"]
    assert env[1].state["jobs"][bob["id"]] == bob_before
    assert claim(env, exclude_job_ids=[alice["id"]]) == {"job": None}
    assert claim(env, sub="bob", exclude_job_ids=[bob["id"]]) == {"job": None}
    assert (
        claim(env, sub="bob", exclude_job_ids=[alice["id"]])["job"]["id"] == bob["id"]
    )


def test_exclusions_do_not_override_live_connector_lease_or_site(env):
    artifact = upload(env)
    first = call(env, "POST", "/v1/projects/p/jobs", spec(artifact, key="vega-job"))[1]
    assert claim(env, site="vega")["job"]["id"] == first["id"]
    second = call(
        env, "POST", "/v1/projects/p/jobs", spec(artifact, key="karolina-job")
    )[1]
    assert (
        claim(env, connector="other", site="karolina", exclude_job_ids=[])["job"]["id"]
        == second["id"]
    )
    # Exclusions cannot bypass either the site pin or another connector's live lease.
    assert claim(
        env, connector="third", site="karolina", exclude_job_ids=[first["id"]]
    ) == {"job": None}
    assert claim(
        env, connector="third", site="vega", exclude_job_ids=[second["id"]]
    ) == {"job": None}


def archive_fixture(
    env, *, artifact=None, key="archive-job", path="/canonical/archive"
):
    artifact = artifact or upload(env)
    job = call(env, "POST", "/v1/projects/p/jobs", spec(artifact, key=key))[1]
    lease = claim(env)
    event(env, lease, "failed")
    receipt = {
        "site": "karolina",
        "path": path,
        "artifacts": [
            {name: artifact[name] for name in ("id", "sha256", "size_bytes")}
        ],
    }
    event(env, lease, "archive_verified", archive=receipt)
    return artifact, job, lease, receipt


def test_old_objects_wait_full_thirty_days_after_verified_receipt_at_epoch_zero(env):
    env[3][0] = 0
    artifact = upload(env)
    stored = env[1].state["artifacts"][artifact["id"]]
    stored["created_at"] = -365 * 86400  # older than S3's creation-age lifecycle
    artifact, job, lease, receipt = archive_fixture(env, artifact=artifact)
    stored = env[1].state["artifacts"][artifact["id"]]
    assert stored["archive_verified_at"] == 0
    assert stored["archive_eligible_at"] == ARCHIVE_RETENTION_SECONDS
    assert env[1].state["jobs"][job["id"]]["archive"]["verified_at"] == 0
    assert not env[2].tags
    env[3][0] = ARCHIVE_RETENTION_SECONDS - 0.001
    assert env[0].maintenance()["tagged"] == 0
    assert not env[2].tags
    env[3][0] = ARCHIVE_RETENTION_SECONDS
    assert env[0].maintenance()["tagged"] == 1
    assert env[2].tags[stored["storage_key"]] == {"archive_verified": "true"}
    assert env[0].maintenance()["tagged"] == 0


def test_fractional_receipt_time_is_never_truncated_into_early_expiration(env):
    env[3][0] = 1000.75
    artifact, job, lease, receipt = archive_fixture(env)
    assert env[1].state["artifacts"][artifact["id"]]["archive_verified_at"] == 1000.75
    env[3][0] = 1000 + ARCHIVE_RETENTION_SECONDS
    assert env[0].maintenance()["tagged"] == 0
    env[3][0] = 1000.75 + ARCHIVE_RETENTION_SECONDS
    assert env[0].maintenance()["tagged"] == 1


def test_repeated_receipts_cannot_rearm_clock_and_conflicting_receipts_are_immutable(
    env,
):
    artifact, job, lease, receipt = archive_fixture(env)
    original = copy.deepcopy(env[1].state["jobs"][job["id"]]["archive"])
    env[3][0] += 10
    # A changed client timestamp is metadata; the trusted first server time wins.
    event(
        env,
        lease,
        "archive_verified",
        event_id="repeat",
        archive={**receipt, "verified_at": "2099-01-01T00:00:00Z"},
    )
    assert env[1].state["jobs"][job["id"]]["archive"] == original
    assert env[1].state["artifacts"][artifact["id"]]["archive_verified_at"] == 1000
    assert (
        env[1].state["artifacts"][artifact["id"]]["archive_eligible_at"]
        == 1000 + ARCHIVE_RETENTION_SECONDS
    )
    error(
        "archive_conflict",
        lambda: event(
            env,
            lease,
            "archive_verified",
            event_id="conflict",
            archive={**receipt, "path": "/different/archive"},
        ),
    )
    assert env[1].state["jobs"][job["id"]]["archive"] == original
    assert not env[2].tags


def test_shared_artifacts_use_the_first_valid_receipt_without_rearming(env):
    artifact, first, _, _ = archive_fixture(env)
    env[3][0] += 20
    _, second, _, _ = archive_fixture(
        env, artifact=artifact, key="second-job", path="/another/job/archive"
    )
    assert env[1].state["jobs"][second["id"]]["archive"]["verified_at"] == 1020
    assert env[1].state["artifacts"][artifact["id"]]["archive_verified_at"] == 1000
    env[3][0] = 1000 + ARCHIVE_RETENTION_SECONDS
    assert env[0].maintenance()["tagged"] == 1


def test_due_tag_acknowledgement_retries_conditional_storage_conflict(env):
    from aws_publishing.storage import Conflict

    artifact, job, _, _ = archive_fixture(env)
    env[3][0] += ARCHIVE_RETENTION_SECONDS
    original_save, saves = env[1].save, []

    def conflict_once(revision, before, after):
        saves.append(revision)
        assert env[1].state["jobs"][job["id"]].get("archive")
        assert (
            env[3][0]
            >= env[1].state["artifacts"][artifact["id"]]["archive_eligible_at"]
        )
        if len(saves) == 1:
            raise Conflict("Concurrent metadata update")
        original_save(revision, before, after)

    env[1].save = conflict_once
    assert env[0].maintenance()["tagged"] == 1
    assert len(saves) == 2
    assert env[1].state["artifacts"][artifact["id"]]["archive_tagged"] is True


def test_daily_reconciliation_batches_under_the_dynamo_transaction_bound(env):
    env[3][0] = ARCHIVE_RETENTION_SECONDS
    env[1].state["artifacts"] = {
        str(i): {"id": str(i), "storage_key": f"objects/{i}", "archive_verified_at": 0}
        for i in range(130)
    }
    original_save, sizes = env[1].save, []

    def checked_save(revision, before, after):
        changed = sum(
            before["artifacts"].get(key) != value
            for key, value in after["artifacts"].items()
        )
        sizes.append(changed)
        assert changed <= 64
        original_save(revision, before, after)

    env[1].save = checked_save
    assert env[0].maintenance() == {
        "status": "ok",
        "tagged": 130,
        "remaining_due": False,
    }
    assert sizes == [64, 64, 2]
    assert len(env[2].tags) == 130


def test_daily_reconciliation_stops_safely_when_invocation_budget_is_low(env):
    artifact, _, _, _ = archive_fixture(env)
    env[3][0] += ARCHIVE_RETENTION_SECONDS
    assert env[0].maintenance(remaining_ms=lambda: 9000) == {
        "status": "ok",
        "tagged": 0,
        "remaining_due": True,
    }
    assert not env[2].tags
    assert env[0].maintenance()["tagged"] == 1


def test_reconciliation_commits_successful_prefix_before_a_later_tag_failure(env):
    env[3][0] = ARCHIVE_RETENTION_SECONDS
    env[1].state["artifacts"] = {
        str(i): {"id": str(i), "storage_key": f"objects/{i}", "archive_verified_at": 0}
        for i in range(3)
    }
    original, calls = env[2].archived, []

    def fail_second(key):
        calls.append(key)
        if key == "objects/1":
            raise OSError("Tag request timed out")
        original(key)

    env[2].archived = fail_second
    with pytest.raises(OSError, match="timed out"):
        env[0].maintenance()
    assert env[1].state["artifacts"]["0"]["archive_tagged"] is True
    assert not env[1].state["artifacts"]["1"].get("archive_tagged")
    assert not env[1].state["artifacts"]["2"].get("archive_tagged")

    def recovered(key):
        calls.append(key)
        original(key)

    env[2].archived = recovered
    assert env[0].maintenance()["tagged"] == 2
    assert calls.count("objects/0") == 1
    assert len(env[2].tags) == 3


def test_reconciliation_checkpoints_a_prefix_when_budget_falls_inside_a_batch(env):
    env[3][0] = ARCHIVE_RETENTION_SECONDS
    env[1].state["artifacts"] = {
        str(i): {"id": str(i), "storage_key": f"objects/{i}", "archive_verified_at": 0}
        for i in range(3)
    }
    budgets = [28000, 28000, 11000]

    def remaining():
        return budgets.pop(0) if len(budgets) > 1 else budgets[0]

    assert env[0].maintenance(remaining_ms=remaining) == {
        "status": "ok",
        "tagged": 1,
        "remaining_due": True,
    }
    assert env[1].state["artifacts"]["0"]["archive_tagged"] is True
    assert not env[1].state["artifacts"]["1"].get("archive_tagged")
    assert env[0].maintenance(remaining_ms=lambda: 28000) == {
        "status": "ok",
        "tagged": 2,
        "remaining_due": False,
    }
