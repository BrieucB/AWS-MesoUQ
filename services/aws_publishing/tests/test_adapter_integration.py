"""Real adapter exports cross the signed service boundary with runner provenance."""

import hashlib
import json
from pathlib import Path
import sys

from aws_mesouq.adapters import indentation
from test_api import env, upload, call, claim, event
from test_release_policy import production, locked_spec, complete_result, publish_file, signed_event


def test_actual_adapter_downloads_admitted_with_existing_runner_runtime(production, tmp_path):
    adapter_sha256 = hashlib.sha256(Path(indentation.__file__).read_bytes()).hexdigest()
    production[1].state["models"]["p:v1"]["approved_adapter_sha256"] = adapter_sha256
    artifact = upload(production)
    job = call(production, "POST", "/v1/projects/p/jobs", locked_spec(artifact))[1]
    lease = claim(production, archive_capable=True)
    event(production, lease, "submitting")
    event(production, lease, "submitted", slurm_job_id="42")
    event(production, lease, "running")

    scientific = complete_result(production, job, upload_outputs=False)
    # MesoUQ runner provenance contains the full interpreter banner and optional
    # unavailable hashes as null. The public adapter normalizes this boundary.
    scientific["provenance"]["runtime"] = {
        "python": sys.version, "environment_sha256": None,
        "korali_module_sha256": None, "adapter_sha256": None,
    }
    scientific["publication_files"] = ["result.json", "provenance.json", "posterior_curve.csv"]
    (tmp_path / "result.json").write_text(json.dumps(scientific))
    (tmp_path / "provenance.json").write_text(json.dumps(scientific["provenance"]))
    (tmp_path / "posterior_curve.csv").write_text(
        "ka,kb,d0\n8500,17000,0.05\n9500,20000,0.10\n10500,23000,0.15\n"
    )
    exported = indentation.add_downloads(tmp_path, scientific, job["configuration"])
    assert len(exported["publication_files"]) == 13
    assert exported["provenance"]["runtime"]["python"] == sys.version.split()[0]
    assert "environment_sha256" not in exported["provenance"]["runtime"]
    exported["artifacts"] = [
        publish_file(production, name, (tmp_path / name).read_bytes())
        for name in exported["publication_files"]
    ]
    completed = signed_event(production, lease, "succeeded", result=exported)
    assert completed["status"] == "succeeded"
    assert len(completed["result"]["artifacts"]) == 13
    assert completed["result"]["provenance"]["runtime"]["adapter_sha256"] == adapter_sha256
