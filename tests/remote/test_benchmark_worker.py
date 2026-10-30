"""Metadata fixtures only: no real Slurm submission, science runtime or AWS calls."""
from __future__ import annotations

import copy
import hashlib
import json
import os
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import urlsplit

import pytest

from aws_mesouq.benchmark import DEFAULT_CONFIGURATION, canonical, checksum, scientific_request_checksum
from aws_mesouq.benchmark_worker import BenchmarkWorker, existing_identity, marker, scheduler_window, validate_operator
from aws_mesouq.connector.journal import Journal
from aws_mesouq.connector.signing import device_identity
from aws_mesouq.connector.slurm import Slurm
from aws_mesouq.connector.transport import Transport
from aws_publishing.api import Application
from aws_publishing.storage import MemoryObjects, MemoryStore


class Objects(MemoryObjects):
    def upload_url(self, key, artifact):
        return "https://uploads.example.invalid/" + key, {}


class Cluster(Transport):
    def __init__(self):
        super().__init__(local=True)
        self.jobs = {}
        self.submit_count = 0
        self.cancelled = []
        self.lose_submit_ack = False

    def identity(self):
        return "fixtureuser"

    def run(self, argv, **kwargs):
        if argv == ["hostname", "-f"]:
            return "fixture.cluster\n"
        if argv[0] in ("squeue", "sacct"):
            if "--jobs" in argv:
                job = self.jobs.get(argv[argv.index("--jobs") + 1])
                if not job:
                    return ""
                return f"{job['id']}|{job['marker']}|fixtureuser|{job['state']}|{job['seconds']}|billing=1,cpu=16,gres/gpu=1,mem=16G,node=1|{job['seconds'] * 16}\n"
            selected = argv[argv.index("--name") + 1]
            return "\n".join(f"{j['id']}|{j['marker']}|fixtureuser|{j['state']}" for j in self.jobs.values() if j["marker"] == selected)
        if argv[0] == "scancel":
            self.cancelled.append(argv[-1])
            self.jobs[argv[-1]]["state"] = "CANCELLED"
            return ""
        return super().run(argv, **kwargs)


@pytest.fixture
def session(tmp_path):
    run = tmp_path / "runs"
    archive = tmp_path / "archive"
    repository = tmp_path / "science"
    for path in (run, archive, repository):
        path.mkdir(mode=0o700)
    environment = tmp_path / "runtime.sh"
    environment.write_text("true\n")
    os.chmod(environment, 0o600)
    adapter = Path(__file__).parents[2] / "src/aws_mesouq/adapters/benchmark.py"
    operator = {
        "repository": str(repository), "science_revision": "a" * 40, "adapter_sha256": hashlib.sha256(adapter.read_bytes()).hexdigest(),
        "python_bin": "/usr/bin/python3.11", "environment_script": str(environment), "environment_sha256": hashlib.sha256(environment.read_bytes()).hexdigest(),
        "run_root": str(run), "archive_root": str(archive), "expected_user": "fixtureuser", "expected_host": "fixture.cluster",
        "scheduler": {"account": "fixture-account", "partition": "qgpu_exp", "qos": "fixture-qos", "cpus-per-task": 4, "mem": "16G"},
        "stage_seconds": {"reference": 300, "metrics": 300}, "allowed_upload_origins": ["https://uploads.example.invalid"],
    }
    journal = Journal(tmp_path / "journal")
    # A newly generated test-only identity fixture represents the pre-existing normal journal.
    identity = device_identity(journal)
    journal.data["owner_sub"] = "alice"
    journal.data["jobs"]["existing-calibration"] = {"retained": True}
    journal.save()
    original_identity = copy.deepcopy(journal.data["archive_device"])
    store, objects, clock = MemoryStore(), Objects(), [1000]
    store.state["projects"]["p"] = {"name": "Fixture", "benchmark_policy": {"adapter_sha256": operator["adapter_sha256"], "science_revision": operator["science_revision"], "scenario_families": ["baseline", "higher_noise", "broader_priors"], "archive_root": str(archive)}}
    store.state["identities"]["alice"] = {"projects": {"p": "maintainer"}, "connector_public_keys": {identity["connector_id"]: identity["archive_public_key"]}}
    app = Application(store, objects, clock=lambda: clock[0])

    class API:
        base = "https://api.example.invalid"
        lose_event_ack = None
        event_ids = []

        def request(self, method, path, payload=None):
            result = app.handle(method, path, {"sub": "alice"}, payload)[1]
            if payload and payload.get("type") and payload.get("event_id"):
                self.event_ids.append((payload["type"], payload["event_id"]))
                if self.lose_event_ack == payload["type"]:
                    self.lose_event_ack = None
                    raise OSError("Fixture lost acknowledged response")
            return result

        def transfer(self, url, *, data=None, headers=None):
            assert urlsplit(url).netloc == "uploads.example.invalid"
            key = urlsplit(url).path.lstrip("/")
            objects.data[key] = data
            return b""

    api = API()
    cluster = Cluster()
    slurm = Slurm(cluster)

    def submit(path):
        cluster.submit_count += 1
        text = Path(path).read_text()
        selected = next(line.split("=", 1)[1] for line in text.splitlines() if line.startswith("#SBATCH --job-name="))
        ident = str(100 + cluster.submit_count)
        cluster.jobs[ident] = {"id": ident, "marker": selected, "state": "PENDING", "seconds": 10}
        if cluster.lose_submit_ack:
            cluster.lose_submit_ack = False
            raise OSError("Fixture lost sbatch acknowledgement")
        return ident

    slurm.submit = submit
    worker = BenchmarkWorker(api, slurm, journal, project="p", operator=operator, clock=lambda: clock[0])
    configuration = {**copy.deepcopy(DEFAULT_CONFIGURATION), "idempotency_key": "fixture"}
    campaign = app.handle("POST", "/v1/projects/p/benchmarks", {"sub": "alice"}, configuration)[1]["campaign"]
    value = SimpleNamespace(worker=worker, journal=journal, api=api, app=app, cluster=cluster, slurm=slurm, clock=clock, store=store, objects=objects, campaign=campaign, original_identity=original_identity, operator=operator)
    yield value
    journal.close()


def entries(session):
    return list(session.worker.state["stages"].values())


def write_fixture_receipt(session, entry, status="completed"):
    root = Path(entry["root"])
    stage = entry["claim"]["stage"]["id"]
    target = root / "stages" / stage
    target.mkdir(parents=True, mode=0o700)
    os.chmod(root / "stages", 0o700)
    source_files = {"fixture.py": "f" * 64}
    receipt = {"stage": stage, "status": status, "campaign_instance_id": entry["claim"]["campaign"]["id"], "request_fingerprint": entry["claim"]["campaign"]["scientific_request_hash"], "transport_request_fingerprint": entry["claim"]["campaign"]["configuration_hash"], "source_binding": {"files": source_files, "sha256": checksum(source_files)}, "slurm_job_id": entry["job_id"], "wall_seconds": 5}
    if stage == "cases":
        case_blob = b"fixture-only-case-data"
        case_path = target / "cases.npz"
        case_path.write_bytes(case_blob)
        os.chmod(case_path, 0o600)
        receipt["case_bank"] = {"file": "cases.npz", "sha256": hashlib.sha256(case_blob).hexdigest(), "size_bytes": len(case_blob)}
        receipt["artifacts"] = [receipt["case_bank"]]
        receipt["shared_case_generation_seconds"] = 1
    if stage.startswith("train_"):
        weights = target / "selected.keras"
        weights.write_bytes(b"fixture-private-model")
        os.chmod(weights, 0o600)
        receipt.update(factor=stage[6:], updates=1, history=[{"update": 1, "validation_nll": 2.5}], selected_checkpoint={"file": "selected.keras", "sha256": hashlib.sha256(weights.read_bytes()).hexdigest(), "size_bytes": weights.stat().st_size})
    if stage == "metrics":
        comparison = {"scientific_status": "honest_comparison", "methods": {"hnpe": {"summary": {"scored": 0}, "cases": []}, "tmcmc": {"summary": {"scored": 1}, "cases": [{"world_index": 0, "status": "completed"}]}}, "limitations": ["Fixture metadata only"]}
        blob = canonical(comparison)
        (target / "comparison.json").write_bytes(blob)
        os.chmod(target / "comparison.json", 0o600)
        receipt["allocation_costs_sha256"] = hashlib.sha256((Path(entry["control"]) / "allocation-costs.json").read_bytes()).hexdigest()
        receipt.update(result=comparison, artifacts=[{"file": "comparison.json", "sha256": hashlib.sha256(blob).hexdigest(), "size_bytes": len(blob)}])
        posterior = b"fixture-only-coherent-float64-posterior-chunk"
        (target / "posterior-hnpe-000.npz").write_bytes(posterior)
        os.chmod(target / "posterior-hnpe-000.npz", 0o600)
        receipt["artifacts"].append({"file": "posterior-hnpe-000.npz", "sha256": hashlib.sha256(posterior).hexdigest(), "size_bytes": len(posterior)})
    path = target / "receipt.json"
    path.write_bytes(canonical(receipt))
    os.chmod(path, 0o600)
    session.cluster.jobs[entry["job_id"]]["state"] = "COMPLETED"
    return receipt


def test_existing_identity_is_required_without_mutation():
    journal = SimpleNamespace(data={"jobs": {}}, save=lambda: pytest.fail("Must not save or create an identity"))
    with pytest.raises(ValueError, match="existing enrolled"):
        existing_identity(journal)
    assert journal.data == {"jobs": {}}


def test_trusted_windows_and_operator_policy(session):
    assert scheduler_window(1800, "qgpu_exp") == {"allocated_seconds": 1800, "internal_seconds": 1770, "time": "00:30:00"}
    assert scheduler_window(179, "qgpu_exp")["allocated_seconds"] == 120
    with pytest.raises(ValueError):
        scheduler_window(59, "qgpu_exp")
    malicious = copy.deepcopy(session.operator)
    malicious["scheduler"]["partition"] = "qcpu"
    with pytest.raises(ValueError):
        validate_operator(malicious)
    malicious = copy.deepcopy(session.operator)
    malicious["allowed_upload_origins"] = ["https://uploads.example.invalid/extra"]
    with pytest.raises(ValueError):
        validate_operator(malicious)


def test_real_submission_script_is_private_pinned_and_inside_allocation(session):
    session.worker.sweep()
    first = entries(session)[0]
    assert session.cluster.submit_count == 1
    script = Path(first["script"]).read_text()
    assert "#SBATCH --gres=gpu:1" in script
    assert "#SBATCH --time=00:05:00" in script
    assert "sha256sum" in script and session.operator["environment_sha256"] in script
    assert "source " + session.operator["environment_script"] in script
    assert first["submission_intent"] is True
    assert session.journal.data["jobs"]["existing-calibration"] == {"retained": True}
    assert session.journal.data["archive_device"] == session.original_identity
    assert all((p.stat().st_mode & 0o077) == 0 for p in Path(first["root"]).rglob("*"))


def test_lost_sbatch_ack_recovers_exact_marker_without_resubmission(session):
    session.cluster.lose_submit_ack = True
    first = session.worker.sweep()
    assert first["errors"] == ["OSError"]
    entry = entries(session)[0]
    assert entry["submission_intent"] and "job_id" not in entry
    session.worker.sweep()
    assert session.cluster.submit_count == 1
    assert entry["job_id"] == "101"
    assert entry["claim"]["stage"]["slurm_job_id"] == "101"


def test_unknown_submission_with_no_lookup_result_never_resubmits(session):
    session.cluster.lose_submit_ack = True
    session.worker.sweep()
    session.cluster.jobs.clear()
    session.worker.sweep()
    assert session.cluster.submit_count == 1
    assert entries(session)[0]["submission_unknown"] is True


def test_lost_event_ack_replays_stable_id_and_preserves_device(session):
    session.api.lose_event_ack = "submitted"
    session.worker.sweep()
    entry = entries(session)[0]
    pending_id = entry["pending_event"]["id"]
    session.worker.sweep()
    submitted = [ident for kind, ident in session.api.event_ids if kind == "submitted"]
    assert submitted == [pending_id, pending_id]
    assert session.cluster.submit_count == 1
    assert session.journal.data["archive_device"] == session.original_identity


def test_expired_lease_reconciles_attempt_and_pending_event(session):
    session.worker.sweep()
    entry = entries(session)[0]
    old_attempt = entry["claim"]["stage"]["attempt"]
    session.clock[0] += 121
    session.worker.sweep()
    assert session.cluster.submit_count == 1
    assert entry["claim"]["stage"]["attempt"] == old_attempt
    assert entry["claim"]["lease_expires_at"] > session.clock[0]


def test_native_allocation_cost_is_16_cpu_cores_and_one_gpu(session):
    session.worker.sweep()
    entry = entries(session)[0]
    write_fixture_receipt(session, entry)
    session.worker.sweep()
    saved = session.store.state["benchmark_campaigns"][session.campaign["id"]]
    case = next(stage for stage in saved["stages"] if stage["id"] == "cases")
    assert case["result"]["timing"] == {"wall_seconds": 10, "cpu_seconds": 160, "gpu_seconds": 10}
    assert session.cluster.submit_count == 10  # Cases plus seven trains, TMCMC and reference.
    assert saved["budget_ledger"]["hnpe"]["settled_gpu_seconds"] == 10


def test_lost_upload_ack_does_not_duplicate_cloud_reservations(session):
    session.worker.sweep()
    entry = entries(session)[0]
    write_fixture_receipt(session, entry)
    session.api.lose_event_ack = "artifact_upload"
    session.worker.sweep()
    assert "pending_event" in entry
    session.worker.sweep()
    campaign = session.store.state["benchmark_campaigns"][session.campaign["id"]]
    case_artifacts = [a for a in campaign["artifacts"].values() if a["stage_id"] == "cases"]
    assert len(case_artifacts) == 1
    assert case_artifacts[0]["verified"]


def test_cancel_only_exact_own_native_stage(session):
    session.worker.sweep()
    entry = entries(session)[0]
    session.app.handle("POST", f"/v1/projects/p/benchmarks/{session.campaign['id']}/cancel", {"sub": "alice"}, {})
    session.worker.sweep()
    assert session.cluster.cancelled == ["101"]
    session.worker.sweep()
    assert session.store.state["benchmark_campaigns"][session.campaign["id"]]["status"] == "cancelled"
    assert entry["cloud_terminal"]


def test_missing_receipt_is_failed_and_actual_reservation_settles(session):
    session.worker.sweep()
    session.cluster.jobs["101"]["state"] = "FAILED"
    session.worker.sweep()
    stage = session.store.state["benchmark_campaigns"][session.campaign["id"]]["stages"][0]
    assert stage["status"] == "failed"
    assert stage["failure"]["code"] == "missing_scientific_receipt"
    assert session.store.state["benchmark_campaigns"][session.campaign["id"]]["budget_ledger"]["hnpe"]["settled_gpu_seconds"] == 10


def test_forged_receipt_request_hash_is_not_published_as_success(session):
    session.worker.sweep()
    entry = entries(session)[0]
    receipt = write_fixture_receipt(session, entry)
    receipt["request_fingerprint"] = "0" * 64
    path = Path(entry["root"]) / "stages/cases/receipt.json"
    path.write_bytes(canonical(receipt))
    result = session.worker.sweep()
    assert not result["errors"]
    stage = session.store.state["benchmark_campaigns"][session.campaign["id"]]["stages"][0]
    assert stage["status"] == "failed" and stage["failure"]["code"] == "invalid_scientific_receipt"
    ledger = session.store.state["benchmark_campaigns"][session.campaign["id"]]["budget_ledger"]["hnpe"]
    assert ledger["settled_gpu_seconds"] == 10 and not ledger["reservations"]


def test_fixture_full_dag_honest_failure_and_verified_private_archive(session):
    session.worker.sweep()
    first = entries(session)[0]
    write_fixture_receipt(session, first)
    session.worker.sweep()
    for entry in entries(session)[1:]:
        write_fixture_receipt(session, entry, "budget_exhausted" if entry["claim"]["stage"]["id"] == "train_global" else "completed")
    session.worker.sweep()
    metrics = next(e for e in entries(session) if e["claim"]["stage"]["id"] == "metrics")
    costs_snapshot = json.loads((Path(metrics["control"]) / "allocation-costs.json").read_text())
    assert costs_snapshot["costs"]["methods"]["hnpe"]["allocated_gpu_seconds"] == 80
    assert costs_snapshot["costs"]["methods"]["hnpe"]["allocated_cpu_seconds"] == 1280
    assert costs_snapshot["costs"]["reference"]["allocated_gpu_seconds"] == 10
    assert costs_snapshot["source_binding_sha256"] == checksum({"fixture.py": "f" * 64})
    write_fixture_receipt(session, metrics)
    session.worker.sweep()
    campaign = session.store.state["benchmark_campaigns"][session.campaign["id"]]
    assert campaign["status"] == "completed"
    assert any(failure["code"] == "budget_exhausted" for failure in campaign["result"]["failures"])
    assert campaign["result"]["training_histories"]
    assert not any(ref["name"].endswith(".keras") for ref in campaign["artifacts"].values())
    assert any(ref["name"] == "metrics-posterior-hnpe-000.npz" and ref["verified"] for ref in campaign["artifacts"].values())
    empty_log = Path(first["control"]) / "stderr.log"
    empty_log.touch(mode=0o600)
    session.worker.client.request_archive("p", campaign["id"])
    result = session.worker.sweep()
    assert not result["errors"]
    archived = session.store.state["benchmark_campaigns"][campaign["id"]]["archive"]
    assert archived["status"] == "verified"
    assert archived["receipt"]["retained_outputs"][0]["path"] == "archive-manifest.json"
    archive_manifest = json.loads((Path(session.operator["archive_root"]) / campaign["id"] / "archive-manifest.json").read_text())
    assert any(item["path"].endswith("selected.keras") for item in archive_manifest["retained_outputs"])
    assert archived["receipt"]["attested_original_file_count"] == len(archive_manifest["retained_outputs"])
    assert (Path(session.operator["archive_root"]) / campaign["id"] / "archive-manifest.json").is_file()



@pytest.mark.parametrize("stage,uses_mpi", [("tmcmc", True), ("cases", False), ("metrics", False)])
def test_standalone_adapter_pins_source_and_runs_the_real_stage_cli(tmp_path, monkeypatch, stage, uses_mpi):
    from aws_mesouq.adapters import benchmark
    request_path = tmp_path / "request.json"
    request_path.write_bytes(canonical({"fixture": True}))
    manifest = {"adapter_sha256": "a" * 64, "environment_script": "/trusted/runtime.sh", "environment_sha256": "a" * 64, "repository": "/trusted/science", "science_revision": "b" * 40, "request": str(request_path), "configuration_hash": checksum({"fixture": True}), "python_bin": "/trusted/bin/python", "campaign_root": "/private/fixture", "campaign_instance_id": "fixture-campaign", "stage": stage, "internal_seconds": 50, "device": "cpu" if stage == "cases" else "cuda"}
    if stage == "metrics":
        manifest.update(allocation_costs="/trusted/allocation-costs.json", allocation_costs_sha256="a" * 64)
    manifest_path = tmp_path / "manifest.json"
    manifest_path.write_text(json.dumps(manifest))
    monkeypatch.setenv("SLURM_JOB_ID", "123")
    monkeypatch.setattr(benchmark, "digest", lambda path: "a" * 64)
    monkeypatch.setattr(benchmark.subprocess, "check_output", lambda argv, **kwargs: "b" * 40 if argv[-1] == "HEAD" else "")
    commands = []
    monkeypatch.setattr(benchmark.subprocess, "run", lambda argv, **kwargs: commands.append(argv) or SimpleNamespace(returncode=0))
    assert benchmark.execute(manifest_path) == 0
    assert (commands[0][:3] == ["mpirun", "-np", "1"]) == uses_mpi
    assert "meso_uq.benchmark_lab.cli" in commands[0] and "--request" in commands[0]
    assert commands[0][commands[0].index("--campaign-instance-id") + 1] == "fixture-campaign"
    if stage == "metrics":
        assert "--allocation-costs" in commands[0] and "--allocation-costs-sha256" in commands[0]
    request_path.write_text('{"fixture": false}')
    with pytest.raises(ValueError, match="Frozen scientific request"):
        benchmark.execute(manifest_path)


def test_existing_enrolled_identity_can_bind_missing_owner_from_authenticated_api(session):
    session.journal.data.pop("owner_sub")
    identity = copy.deepcopy(session.journal.data["archive_device"])
    BenchmarkWorker(session.api, session.slurm, session.journal, project="p", operator=session.operator, clock=lambda: session.clock[0])
    assert session.journal.data["owner_sub"] == "alice"
    assert session.journal.data["archive_device"] == identity


def test_other_authenticated_owner_cannot_reuse_enrolled_journal(session):
    original = copy.deepcopy(session.journal.data)
    class OtherAPI:
        base = session.api.base
        def request(self, method, path, payload=None):
            assert path == "/v1/me"
            return {"subject": "bob"}
    with pytest.raises(ValueError, match="Authenticated application identity"):
        BenchmarkWorker(OtherAPI(), session.slurm, session.journal, project="p", operator=session.operator)
    assert session.journal.data == original


def test_compact_archive_verifies_every_original_beyond_two_thousand_files(session, monkeypatch):
    import shutil
    session.worker.sweep()
    first = entries(session)[0]
    session.app.handle("POST", f"/v1/projects/p/benchmarks/{session.campaign['id']}/cancel", {"sub": "alice"}, {})
    session.worker.sweep()
    session.worker.sweep()
    extra = Path(first["root"]) / "retained"
    extra.mkdir(mode=0o700)
    for index in range(2001):
        path = extra / f"native-{index:04d}.json"
        path.write_bytes(b"{}")
        os.chmod(path, 0o600)
    copied, verified = set(), set()
    def verify(root, token, item):
        path = Path(root) / token / item["destination"]
        blob = path.read_bytes()
        assert len(blob) == item["size_bytes"] and hashlib.sha256(blob).hexdigest() == item["sha256"]
        verified.add(item["destination"])
    def copy_file(root, token, item):
        source = Path(item["source_root"]) / item["source"]
        target = Path(root) / token / item["destination"]
        target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        shutil.copyfile(source, target)
        os.chmod(target, 0o400)
        copied.add(item["destination"])
        verify(root, token, item)
    monkeypatch.setattr(session.cluster, "archive_copy", copy_file)
    monkeypatch.setattr(session.cluster, "archive_verify", verify)
    session.worker.client.request_archive("p", session.campaign["id"])
    report = session.worker.sweep()
    assert not report["errors"]
    archived = session.store.state["benchmark_campaigns"][session.campaign["id"]]["archive"]
    receipt = archived["receipt"]
    assert receipt["attested_original_file_count"] > 2000
    assert len(receipt["retained_outputs"]) == 1
    assert len(canonical(receipt)) < 2000
    assert copied == verified
    assert len(copied) == receipt["attested_original_file_count"] + 1
    assert (Path(session.operator["archive_root"]) / session.campaign["id"] / "retained/native-2000.json").read_bytes() == b"{}"


def test_same_request_receipt_from_another_campaign_is_rejected(session):
    session.worker.sweep()
    entry = entries(session)[0]
    receipt = write_fixture_receipt(session, entry)
    receipt["campaign_instance_id"] = "different-global-campaign"
    path = Path(entry["root"]) / "stages/cases/receipt.json"
    path.write_bytes(canonical(receipt))
    result = session.worker.sweep()
    assert not result["errors"]
    stage = session.store.state["benchmark_campaigns"][session.campaign["id"]]["stages"][0]
    assert stage["status"] == "failed"
    assert stage["failure"]["code"] == "invalid_scientific_receipt"


def test_module_cli_help_exits_before_authentication(tmp_path):
    import subprocess
    import sys
    hooks = tmp_path / 'hooks'
    hooks.mkdir()
    loaded = hooks / 'loaded'
    (hooks / 'sitecustomize.py').write_text(
        'import sys\nfrom pathlib import Path\n'
        + f'Path({str(loaded)!r}).write_text("loaded")\n'
        + 'def guard(event, arguments):\n'
        + '    if event in {"socket.connect", "subprocess.Popen", "webbrowser.open"}:\n'
        + '        raise RuntimeError("help attempted external work")\n'
        + 'sys.addaudithook(guard)\n'
    )
    environment = dict(os.environ)
    source = Path(__file__).resolve().parents[2] / 'src'
    environment['PYTHONPATH'] = os.pathsep.join([str(hooks), str(source)])
    environment['PYTHONDONTWRITEBYTECODE'] = '1'
    environment.pop('AWS_MESOUQ_SSH_STATE_DIR', None)
    result = subprocess.run(
        [sys.executable, '-m', 'aws_mesouq.benchmark_worker', '--help'],
        cwd=tmp_path, env=environment, stdout=subprocess.PIPE,
        stderr=subprocess.PIPE, text=True, timeout=10,
    )
    assert loaded.read_text() == 'loaded'
    assert result.returncode == 0
    assert 'usage:' in result.stdout
    assert '--operator-config' in result.stdout and '--ssh-host' in result.stdout
    assert result.stderr == '' and set(tmp_path.iterdir()) == {hooks}
