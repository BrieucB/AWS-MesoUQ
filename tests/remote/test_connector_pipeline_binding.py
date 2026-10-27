"""Profile checks must fail before mkdir, transfers or scheduler submission."""

import copy
import json

import pytest

from aws_mesouq.contracts import CANONICAL_PROFILE
from aws_mesouq.connector.connector import ModelUnavailable
from test_connector import session


def install(session):
    binding = {
        "id": CANONICAL_PROFILE,
        "bank_sha256": "b" * 64,
        "model_sha256": {"3.2": "f" * 64},
    }
    session.job["configuration"]["scientific_profile"] = CANONICAL_PROFILE
    session.job["approved_model"]["provenance"] = {
        "pipeline_binding": copy.deepcopy(binding)
    }
    model = session.connector.models["model-1"]
    model["provenance"] = {"pipeline_binding": copy.deepcopy(binding)}
    model["scientific_profile"] = {
        "id": CANONICAL_PROFILE,
        "artifact_root": "/private/canonical",
        "binding": copy.deepcopy(binding),
    }
    session.job["approved_model"]["validation"] = {
        "passed": True,
        "model_sha256": "a" * 64,
        "training_data_sha256": "c" * 64,
    }
    model["training_support"] = {
        "3.2": {"path": "/private/training/table.dat", "sha256": "c" * 64}
    }
    return binding, model


@pytest.mark.parametrize(
    "mutation",
    [
        "no_cloud",
        "no_local",
        "no_private",
        "local_hash",
        "private_hash",
        "private_id",
        "requested_id",
        "legacy_cross",
    ],
)
def test_mismatch_rejected_before_any_staging_or_submission(session, mutation):
    _, model = install(session)
    if mutation == "no_cloud":
        session.job["approved_model"].pop("provenance")
    elif mutation == "no_local":
        model.pop("provenance")
    elif mutation == "no_private":
        model.pop("scientific_profile")
    elif mutation == "local_hash":
        model["provenance"]["pipeline_binding"]["bank_sha256"] = "c" * 64
    elif mutation == "private_hash":
        model["scientific_profile"]["binding"]["bank_sha256"] = "c" * 64
    elif mutation == "private_id":
        model["scientific_profile"]["id"] = "other"
    elif mutation == "requested_id":
        session.job["configuration"]["scientific_profile"] = "other"
    else:
        session.job["configuration"].pop("scientific_profile")
    with pytest.raises(ModelUnavailable, match="profile"):
        session.connector.stage(session.job, {"run_dir": "/private/runs/run"})
    assert not session.transport.commands
    assert not session.transport.files
    assert session.slurm.submit_count == 0


def test_exact_binding_stages_candidate_with_distinct_actual_checkpoint(session):
    binding, _ = install(session)
    session.connector.tick()
    assert session.slurm.submit_count == 1
    entry = session.journal.data["jobs"][session.job["id"]]
    manifest = json.loads(session.transport.files[entry["run_dir"] + "/manifest.json"])
    assert (
        manifest["job"]["approved_model"]["provenance"]["pipeline_binding"] == binding
    )
    assert (
        manifest["model"]["scientific_profile"]["artifact_root"] == "/private/canonical"
    )
    assert "scientific_profile" not in manifest["job"]["approved_model"]
    assert (
        manifest["job"]["approved_model"]["sha256"]["3.2"]
        != binding["model_sha256"]["3.2"]
    )


@pytest.mark.parametrize(
    "mutation", ["missing_support", "support_hash", "validation_model", "relative_path"]
)
def test_candidate_support_metadata_fails_before_staging(session, mutation):
    _, model = install(session)
    if mutation == "missing_support":
        model.pop("training_support")
    elif mutation == "support_hash":
        model["training_support"]["3.2"]["sha256"] = "e" * 64
    elif mutation == "validation_model":
        session.job["approved_model"]["validation"]["model_sha256"] = "f" * 64
    else:
        model["training_support"]["3.2"]["path"] = "relative/table.dat"
    with pytest.raises(ModelUnavailable, match="support"):
        session.connector.stage(session.job, {"run_dir": "/private/runs/run"})
    assert not session.transport.commands and not session.transport.files
    assert session.slurm.submit_count == 0
