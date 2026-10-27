"""In-process contract integration; no external transports or scheduler work."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
import time

import pytest

from aws_mesouq.contracts import validate_result, validate_job_configuration
from aws_mesouq.connector.connector import Connector
from aws_mesouq.connector.journal import Journal
from aws_mesouq.connector.transport import Transport
from aws_mesouq.connector.slurm import Slurm


@pytest.mark.parametrize("race", [None, "submitting", "running", "succeeded"])
def test_session_through_real_control_plane(tmp_path, monkeypatch, race):
    monkeypatch.syspath_prepend(str(Path(__file__).resolve().parents[2] / "services/aws_publishing"))
    from aws_publishing.api import Application, ARCHIVE_RETENTION_SECONDS
    from aws_publishing.storage import MemoryStore, MemoryObjects
    store, objects = MemoryStore(), MemoryObjects()
    store.state["projects"] = {"p": {"name": "Project"}}
    store.state["identities"] = {"alice": {"projects": {"p": "maintainer"}}}
    store.state["models"]["p:v1"] = {"project_id": "p", "version": "v1", "trusted": True, "status": "released", "approved_adapter_sha256": hashlib.sha256((Path(__file__).resolve().parents[2] / "src/aws_mesouq/adapters/indentation.py").read_bytes()).hexdigest(), "sha256": {"3.2": "a" * 64}}
    app = Application(store, objects, release_policy='nonproduction_legacy', clock=time.time, validate_configuration=validate_job_configuration, validate_result=validate_result)

    class InProcessAPI:
        dropped_submission_ack = False
        injected_cancellation = False
        def request(self, method, path, payload=None):
            if race and payload and payload.get("type") == race and not self.injected_cancellation:
                self.injected_cancellation = True
                job_id = path.split("/")[-2]
                app.handle("POST", f"/v1/projects/p/jobs/{job_id}/cancel", {"sub": "alice"}, {})
            response = app.handle(method, path, {"sub": "alice"}, payload)[1]
            if payload and payload.get("type") == "submitted" and not self.dropped_submission_ack:
                self.dropped_submission_ack = True
                raise OSError("Connection lost after server accepted event")
            return response

        def transfer(self, url, *, data=None, headers=None):
            key = url.removeprefix("memory://")
            if data is None:
                return objects.data[key]
            objects.data[key] = data
            return b""

    api = InProcessAPI()
    raw = b"force,displacement\n1,2\n"
    artifact = api.request("POST", "/v1/projects/p/artifacts", {"name": "curve.csv", "kind": "curve",
        "sha256": hashlib.sha256(raw).hexdigest(), "size_bytes": len(raw), "media_type": "text/csv", "metadata": {"nominal_diameter_um": 3.2, "force_unit": "N", "displacement_unit": "m", "column_order": "force-displacement"}})
    api.transfer(artifact["upload_url"], data=raw)
    ref = artifact["artifact"]
    api.request("POST", f"/v1/projects/p/artifacts/{ref['id']}/complete", {})
    job = api.request("POST", "/v1/projects/p/jobs", {"schema_version": 1, "kind": "prediction",
        "model_version": "v1", "diameter_um": 3.2, "configuration": {"parameters": {"Yt": 1.0, "kb": 2.0}}, "input_artifacts": [ref], "idempotency_key": "unique"})

    class Cluster:
        private_root_probe = Transport.private_root_probe
        host = "owncluster"
        state = "PENDING"
        files = {}
        marker = None
        def identity(self):
            return "ownuser"
        def run(self, argv, **kwargs):
            if argv[0] in {"squeue", "sacct"}:
                return f"123|{self.marker}|ownuser|{self.state}" if self.marker else ""
            if "rglob" in " ".join(argv):
                return '["result.json", "provenance.json"]'
            return ""
        def write(self, path, data):
            assert path not in self.files
            self.files[path] = data
        def read(self, path):
            return self.files[path]
        def read_beneath(self, root, name):
            return self.read(root + '/' + name)
        def archive_prepare(self, root, token):
            pass
        def archive_verify(self, root, token, item):
            blob = self.files[root + '/' + token + '/' + item['destination']]
            assert hashlib.sha256(blob).hexdigest() == item['sha256']
            assert len(blob) == item['size_bytes']

        def archive_copy(self, root, token, item):
            path = root + '/' + token + '/' + item['destination']
            if path not in self.files:
                blob = self.files[item['source_root'] + '/' + item['source']]
                assert hashlib.sha256(blob).hexdigest() == item['sha256']
                assert len(blob) == item['size_bytes']
                self.files[path] = blob
            self.archive_verify(root, token, item)

        def check_files(self, root, names):
            for name in names:
                if root + '/' + name not in self.files:
                    raise FileNotFoundError(name)

    transport = Cluster()
    scheduler = Slurm(transport)
    submitted = []
    def submit(path):
        submitted.append(path)
        transport.marker = next(row["marker"] for row in journal.data["jobs"].values())
        return "123"
    scheduler.submit = submit
    journal = Journal(tmp_path / "journal")
    try:
        connector = Connector(api, scheduler, journal, project="p", site="karolina", archive_root="/private/archive", durable_archive_root=True, run_root="/private/runs",
                              repository="/trusted/repository", model_registry={"v1": {"version": "v1", "trained_dirs": {}, "sha256": {"3.2": "a" * 64}}})
        store.state['identities']['alice']['connector_public_keys'] = {
            connector.connector_id: journal.data['archive_device']['public_key']}
        if race == "submitting":
            connector.tick()
            assert "pending_event" not in journal.data["jobs"][job["id"]]
            connector.tick()
            assert store.state["jobs"][job["id"]]["status"] == "cancelled"
            assert not submitted
            return
        with pytest.raises(OSError):
            connector.tick()
        connector.tick()  # Reclaims a rotated lease and replays the accepted event.
        connector.tick()  # Queued -> heartbeat, preserving state.
        transport.state = "RUNNING"
        connector.tick()
        if race == "running":
            assert "pending_event" not in journal.data["jobs"][job["id"]]
            transport.state = "CANCELLED"
            connector.tick()
            assert store.state["jobs"][job["id"]]["status"] == "cancelled"
            assert len(submitted) == 1
            return
        connector.tick()  # Running -> heartbeat, preserving state.
        entry = journal.data["jobs"][job["id"]]
        result = {"schema_version": 1, "kind": "prediction", "provenance": {"code_sha256": hashlib.sha256(b"trusted fixture source").hexdigest(),
                "runtime": {"python": "3.11.2", "site": "karolina"}, "job_id": job["id"], "model_version": job["model_version"],
                "model_sha256": job["approved_model"]["sha256"], "data_sha256": {ref["id"]: ref["sha256"]},
                "config_sha256": hashlib.sha256(json.dumps(job["configuration"], sort_keys=True, separators=(",", ":"), allow_nan=False).encode()).hexdigest()},
            "publication_files": ["result.json", "provenance.json"],
            "interval_probability": .95, "parameters": [], "diagnostics": {},
            "resonance": {"status": "unavailable", "reason": "fixture"},
            "curves": [{"dataset_id": ref["id"], "force": [1], "observed_displacement": [2],
                "median": [2], "lower": [1], "upper": [3], "force_unit": "N", "displacement_unit": "m"}]}
        transport.files[entry["run_dir"] + "/output/result.json"] = json.dumps(result).encode()
        transport.files[entry["run_dir"] + "/output/provenance.json"] = json.dumps(result["provenance"]).encode()
        transport.state = "COMPLETED"
        connector.tick()
        if race == "succeeded":
            assert "pending_event" not in entry
            connector.tick()
            assert store.state["jobs"][job["id"]]["status"] == "failed"
            assert len(submitted) == 1
            return
        current = api.request("GET", f"/v1/projects/p/jobs/{job['id']}")
        assert current["status"] == "succeeded"
        assert current["archive"]["site"] == "karolina"
        assert len(current["archive"]["artifacts"]) == 3
        assert len(submitted) == 1
        assert objects.tags == {}  # receipt acceptance never starts early expiry
        eligible_at = current["archive"]["verified_at"] + ARCHIVE_RETENTION_SECONDS
        app.clock = lambda: eligible_at - .001
        assert app.maintenance()["tagged"] == 0
        assert objects.tags == {}
        app.clock = lambda: eligible_at
        assert app.maintenance()["tagged"] == 3
        assert len(objects.tags) == 3
    finally:
        journal.close()
