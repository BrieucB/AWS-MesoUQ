"""Own-account Benchmark Lab executor with durable submission and artifact provenance."""
from __future__ import annotations

import argparse
import copy
import hashlib
import json
import math
from pathlib import Path, PurePosixPath
import re
import shlex
import subprocess
import time
from urllib.parse import urlsplit
import uuid

from .benchmark import SCHEMA, FACTORS, TERMINAL, canonical, checksum, validate_create, scientific_request_checksum
from .benchmark_client import BenchmarkClient, native_accounting
from .connector.transport import absolute_path, safe_id, safe_relative, check_transport_health
from .connector.ssh_session import SSHSessionError
from .connector.archive_source import TREE_HELPER

class ArtifactIntegrityError(ValueError):
    """A publication differs from its immutable local or cloud reservation."""


NATIVE_TERMINAL = {"COMPLETED", "FAILED", "CANCELLED", "TIMEOUT", "OUT_OF_MEMORY", "NODE_FAIL", "PREEMPTED", "BOOT_FAIL", "DEADLINE"}



def marker(campaign, stage, attempt):
    return "bm-" + checksum([campaign, stage, attempt])[:40]


def existing_identity(journal):
    """Reject absent identity before normal signing helpers can create anything."""
    if not isinstance(journal.data.get("connector_id"), str) or not isinstance(journal.data.get("archive_device"), dict):
        raise ValueError("Reuse the existing enrolled connector journal and owner identity; no new identity is created")
    from .connector.signing import device_identity
    identity = device_identity(journal)
    return identity


def scheduler_window(seconds, partition):
    seconds = float(seconds)
    maximum = 3600 if partition == "qgpu_exp" else 172800
    minutes = math.floor(min(seconds, maximum) / 60)
    if minutes < 1:
        raise ValueError("Remaining reservation cannot fund one native Slurm minute")
    wall = minutes * 60
    grace = min(30, max(10, wall * 0.1))
    return {"allocated_seconds": wall, "internal_seconds": wall - grace, "time": f"{wall // 3600:02d}:{wall % 3600 // 60:02d}:00"}


def validate_operator(config):
    required = {"repository", "science_revision", "adapter_sha256", "python_bin", "environment_script", "environment_sha256", "run_root", "archive_root", "expected_user", "expected_host", "scheduler", "stage_seconds", "allowed_upload_origins"}
    if not isinstance(config, dict) or set(config) != required:
        raise ValueError("Unexpected or missing trusted benchmark operator fields")
    for name in ("repository", "python_bin", "environment_script", "run_root", "archive_root"):
        absolute_path(config[name])
        if not re.fullmatch(r"/[A-Za-z0-9_./-]+", config[name]):
            raise ValueError("Trusted runtime and storage paths must be safe for Slurm directives")
    run, archive = PurePosixPath(config["run_root"]), PurePosixPath(config["archive_root"])
    if run == archive or run in archive.parents or archive in run.parents or run == PurePosixPath("/") or archive == PurePosixPath("/"):
        raise ValueError("Run and durable archive roots must be separate private directories")
    for name, count in (("science_revision", 40), ("adapter_sha256", 64), ("environment_sha256", 64)):
        if not re.fullmatch("[a-f0-9]{" + str(count) + "}", config[name]):
            raise ValueError("Trusted source and runtime hashes must be immutable")
    scheduler = config["scheduler"]
    if set(scheduler) != {"partition", "account", "qos", "cpus-per-task", "mem"} or scheduler["partition"] not in ("qgpu_exp", "qgpu"):
        raise ValueError("Select an explicitly approved one-GPU Karolina scheduler profile")
    if any(not re.fullmatch(r"[A-Za-z0-9_:.+-]+", str(v)) for v in scheduler.values()):
        raise ValueError("Unsafe scheduler policy")
    if not 1 <= int(scheduler["cpus-per-task"]) <= 16:
        raise ValueError("Unsupported requested CPU count")
    if set(config["stage_seconds"]) != {"reference", "metrics"} or any(type(v) not in (int, float) or not 60 <= v <= 172800 for v in config["stage_seconds"].values()):
        raise ValueError("Reference and metrics require finite operator stage limits")
    origins = config["allowed_upload_origins"]
    if not isinstance(origins, list) or not origins:
        raise ValueError("Exact trusted HTTPS artifact origins are required")
    for origin in origins:
        parsed = urlsplit(origin)
        if parsed.scheme != "https" or not parsed.netloc or parsed.username or parsed.password or parsed.path or parsed.query or parsed.fragment:
            raise ValueError("Artifact origins must be exact HTTPS origins")
    return copy.deepcopy(config)


class BenchmarkWorker:
    def __init__(self, api, slurm, journal, *, project, operator, clock=time.time, maximum_active=16):
        self.operator = validate_operator(operator)
        identity = existing_identity(journal)
        self.api, self.slurm, self.journal, self.clock = api, slurm, journal, clock
        self.project = safe_id(project)
        subject = api.request("GET", "/v1/me").get("subject")
        if not isinstance(subject, str) or not subject or journal.data.get("owner_sub") not in (None, subject):
            raise ValueError("Authenticated application identity differs from the existing journal owner")
        journal.data.setdefault("owner_sub", subject)
        self.client = BenchmarkClient(api, journal, subject)
        self.maximum_active = maximum_active
        if not 1 <= maximum_active <= 64:
            raise ValueError("Invalid local stage concurrency")
        transport = slurm.transport
        if slurm.user != operator["expected_user"] or transport.run(["hostname", "-f"]).strip() != operator["expected_host"]:
            raise ValueError("Verified Karolina account or hostname differs from operator policy")
        context = {"project": project, "owner_sub": journal.data["owner_sub"], "api": getattr(api, "base", None), "operator": self.operator, "connector_id": identity["connector_id"]}
        namespace = journal.data.setdefault("benchmark_lab", {"context": context, "stages": {}, "campaigns": {}})
        if namespace["context"] != context:
            raise ValueError("Benchmark journal belongs to another immutable operator/session context")
        self.state = namespace
        journal.save()

    @property
    def transport(self):
        return self.slurm.transport

    def save(self):
        self.journal.save()

    def _immutable(self, path, blob):
        code = """import os,stat,sys
path=sys.argv[1]
assert os.path.realpath(os.path.dirname(path))==os.path.dirname(path)
blob=sys.stdin.buffer.read()
try:
    fd=os.open(path,os.O_WRONLY|os.O_CREAT|os.O_EXCL|os.O_NOFOLLOW,0o600)
except FileExistsError:
    fd=os.open(path,os.O_RDONLY|os.O_NOFOLLOW|os.O_NONBLOCK)
    with os.fdopen(fd,'rb') as stream:
        info=os.fstat(stream.fileno())
        assert stat.S_ISREG(info.st_mode) and info.st_uid==os.getuid() and info.st_nlink==1 and not stat.S_IMODE(info.st_mode)&0o077
        assert stream.read()==blob, 'Immutable benchmark staging differs'
else:
    with os.fdopen(fd,'wb') as stream:
        stream.write(blob);stream.flush();os.fsync(stream.fileno())
    parent=os.open(os.path.dirname(path),os.O_DIRECTORY)
    os.fsync(parent);os.close(parent)
"""
        self.transport.run([getattr(self.transport, "staging_python", "/usr/bin/python3.11"), "-I", "-c", code, path], data=blob)

    def _valid_lease(self, entry):
        if entry["claim"].get("lease_expires_at", 0) <= self.clock():
            self._refresh(entry)

    def _refresh(self, entry):
        check_transport_health(self.transport)
        campaign, stage = entry["claim"]["campaign"], entry["claim"]["stage"]
        request_id = entry.setdefault("refresh_request_id", uuid.uuid4().hex)
        self.save()
        exclusions = [s["id"] for s in campaign["stages"] if s["id"] != stage["id"]]
        claimed = self.client.claim(self.project, request_id, campaign_id=campaign["id"], exclude_stage_ids=exclusions)
        if not claimed.get("stage"):
            # An acknowledged terminal event may have already committed.
            current = self.client.get(self.project, campaign["id"])["campaign"]
            if next(s for s in current["stages"] if s["id"] == stage["id"])["status"] in TERMINAL:
                entry["cloud_terminal"] = True
                self.save()
                return
            raise RuntimeError("Expired stage is not currently claimable")
        if claimed["stage"]["id"] != stage["id"] or claimed["stage"]["attempt"] != stage["attempt"]:
            raise ValueError("Stage recovery identity differs")
        entry["claim"] = claimed
        entry.pop("refresh_request_id", None)
        self.save()

    def _send(self, entry, kind=None, **fields):
        pending = entry.get("pending_event")
        if pending is None:
            self._valid_lease(entry)
            if entry.get("cloud_terminal"):
                return None
            pending = {"id": uuid.uuid4().hex, "type": kind, "fields": fields}
            entry["pending_event"] = pending
            self.save()
        try:
            result = self.client.event(entry["claim"], pending["id"], pending["type"], **pending["fields"])
        except Exception as error:
            if getattr(error, "code", None) == "invalid_transition":
                current = self.client.get(self.project, entry["claim"]["campaign"]["id"])["campaign"]
                authoritative = next(s for s in current["stages"] if s["id"] == entry["claim"]["stage"]["id"])
                if authoritative["status"] == "cancelling" and pending["type"] in ("submitting", "running", "queued", "heartbeat"):
                    entry.pop("pending_event", None)
                    entry["claim"]["campaign"] = current
                    entry["claim"]["stage"] = authoritative
                    self.save()
                    return {"campaign": current}
                raise
            if getattr(error, "code", None) != "invalid_lease":
                raise
            self._refresh(entry)
            result = self.client.event(entry["claim"], pending["id"], pending["type"], **pending["fields"])
        if pending["type"] == "artifact_upload" and result.get("uploads") is not None:
            entry["upload_plan"] = result["uploads"]
            entry["upload_reservation"] = copy.deepcopy(pending)
        entry.pop("pending_event", None)
        if result.get("lease_expires_at"):
            entry["claim"]["lease_expires_at"] = result["lease_expires_at"]
        if result.get("campaign"):
            entry["claim"]["campaign"] = result["campaign"]
            updated = next(s for s in result["campaign"]["stages"] if s["id"] == entry["claim"]["stage"]["id"])
            entry["claim"]["stage"] = updated
            entry["cloud_terminal"] = updated["status"] in TERMINAL
        self.save()
        return result

    def _before_slow(self, entry):
        if entry.get("pending_event"):
            self._send(entry)
        self._valid_lease(entry)
        if entry.get("cloud_terminal"):
            return
        if entry["claim"]["lease_expires_at"] - self.clock() < 105:
            self._send(entry, "heartbeat")

    def _slow(self, entry, operation, *args, **kwargs):
        self._before_slow(entry)
        result = operation(*args, **kwargs)
        return result

    def _claim(self):
        check_transport_health(self.transport)
        request_id = self.state.setdefault("claim_request_id", uuid.uuid4().hex)
        self.save()
        try:
            claimed = self.client.claim(self.project, request_id)
        except Exception as error:
            if getattr(error, "code", None) == "claim_expired":
                self.state.pop("claim_request_id", None)
                self.save()
            raise
        self.state.pop("claim_request_id", None)
        if claimed.get("stage"):
            campaign, stage = claimed["campaign"], claimed["stage"]
            expected = campaign["approved_adapter"]
            if campaign["owner_sub"] != self.journal.data["owner_sub"] or campaign["project_id"] != self.project or expected["adapter_sha256"] != self.operator["adapter_sha256"] or expected["science_revision"] != self.operator["science_revision"] or expected.get("archive_root") != self.operator["archive_root"]:
                raise ValueError("Cloud campaign differs from trusted owner, source or durable policy")
            validate_create(campaign["configuration"])
            if checksum(campaign["configuration"]) != campaign["configuration_hash"]:
                raise ValueError("Cloud configuration hash differs from frozen request")
            key = campaign["id"] + "/" + stage["id"] + "/" + str(stage["attempt"])
            entry = self.state["stages"].setdefault(key, {"claim": claimed, "marker": marker(campaign["id"], stage["id"], stage["attempt"])})
            entry["claim"] = claimed
            self.state["campaigns"].setdefault(campaign["id"], {"root": self.operator["run_root"] + "/" + campaign["id"]})
            self.save()
            return entry
        self.save()
        return None

    def stage(self, entry):
        campaign, stage = entry["claim"]["campaign"], entry["claim"]["stage"]
        root = self.operator["run_root"] + "/" + safe_id(campaign["id"])
        control = root + "/control/" + safe_id(stage["id"])
        for private in (self.operator["run_root"], self.operator["archive_root"]):
            self._slow(entry, self.transport.private_root_probe, private)
        mkdir_code = """import os,stat,sys
fd=os.open(sys.argv[1],os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW)
for part in sys.argv[2].split('/'):
    assert part and part not in ('.','..')
    try: os.mkdir(part,0o700,dir_fd=fd)
    except FileExistsError: pass
    child=os.open(part,os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW,dir_fd=fd)
    info=os.fstat(child)
    assert info.st_uid==os.getuid() and stat.S_IMODE(info.st_mode)==0o700
    os.close(fd);fd=child
for name in ('stdout.log','stderr.log'):
    try: output=os.open(name,os.O_WRONLY|os.O_CREAT|os.O_EXCL|os.O_NOFOLLOW,0o600,dir_fd=fd)
    except FileExistsError: output=os.open(name,os.O_RDONLY|os.O_NOFOLLOW|os.O_NONBLOCK,dir_fd=fd)
    info=os.fstat(output)
    assert stat.S_ISREG(info.st_mode) and info.st_uid==os.getuid() and info.st_nlink==1 and not stat.S_IMODE(info.st_mode)&0o077
    os.close(output)
os.close(fd)
"""
        self._slow(entry, self.transport.run, [getattr(self.transport, "staging_python", "/usr/bin/python3.11"), "-I", "-c", mkdir_code, self.operator["run_root"], campaign["id"] + "/control/" + stage["id"]])
        entry["root"] = root
        request_path = root + "/request.json"
        self._slow(entry, self._immutable, request_path, canonical(campaign["configuration"]))
        adapter = Path(__file__).with_name("adapters").joinpath("benchmark.py").read_bytes()
        if hashlib.sha256(adapter).hexdigest() != self.operator["adapter_sha256"]:
            raise ValueError("Installed benchmark adapter differs from operator pin")
        window = scheduler_window(stage.get("reserved_seconds", self.operator["stage_seconds"].get(stage["id"], 0)), self.operator["scheduler"]["partition"])
        manifest = {**{k: self.operator[k] for k in ("repository", "science_revision", "python_bin", "environment_script", "environment_sha256", "adapter_sha256")}, "request": request_path, "campaign_root": root, "campaign_instance_id": campaign["id"], "configuration_hash": campaign["configuration_hash"], "stage": stage["id"], "internal_seconds": window["internal_seconds"], "device": "cpu" if stage["id"] in ("cases", "reference", "metrics") else "cuda"}
        if stage["id"] == "metrics":
            costs = self.allocation_snapshot(entry)
            costs_blob = canonical(costs)
            costs_path = control + "/allocation-costs.json"
            self._slow(entry, self._immutable, costs_path, costs_blob)
            manifest["allocation_costs"] = costs_path
            manifest["allocation_costs_sha256"] = hashlib.sha256(costs_blob).hexdigest()
        self._slow(entry, self._immutable, control + "/adapter.py", adapter)
        self._slow(entry, self._immutable, control + "/manifest.json", canonical(manifest))
        script = self.script(entry, control, window)
        self._slow(entry, self._immutable, control + "/submit.sh", script.encode())
        entry.update(root=root, control=control, script=control + "/submit.sh", window=window)
        self.save()

    def allocation_snapshot(self, entry):
        campaign = self.client.get(self.project, entry["claim"]["campaign"]["id"])["campaign"]
        stages = [s for s in campaign["stages"] if s["id"] != "metrics"]
        if not all(s["status"] in TERMINAL for s in stages):
            raise ValueError("Authoritative metrics require all scientific stages to be terminal")
        raw = self._slow(entry, self.transport.read_beneath, entry["root"], "stages/cases/receipt.json")
        cases = json.loads(raw)
        scientific_hash = campaign.get("scientific_request_hash", scientific_request_checksum(campaign["configuration"]))
        if cases.get("campaign_instance_id") != campaign["id"] or cases.get("request_fingerprint") != scientific_hash or cases.get("transport_request_fingerprint") != campaign["configuration_hash"]:
            raise ValueError("Allocation-cost snapshot differs from frozen case request")
        binding = cases.get("source_binding", {})
        if binding.get("sha256") != checksum(binding.get("files", {})):
            raise ValueError("Allocation-cost source snapshot is inconsistent")
        case_bank = cases["case_bank"]
        self._file(entry, "stages/cases/" + safe_relative(case_bank["file"]), case_bank)

        def totals(selected):
            gpu, cpu, missing = 0, 0, []
            for stage in selected:
                accounting = stage.get("scheduler_accounting")
                if accounting is not None:
                    gpu += accounting["elapsed_seconds"] * accounting["allocated_gpus"]
                    cpu += accounting["cpu_seconds"]
                elif stage["status"] in ("blocked", "failed", "cancelled") and not stage.get("slurm_job_id") and not stage.get("submission_started"):
                    pass  # Known unsubmitted stage has no native allocation.
                else:
                    missing.append(stage["id"])
            return {"allocated_gpu_seconds": None if missing else gpu, "allocated_cpu_seconds": None if missing else cpu, "status": "unknown" if missing else "complete", "missing_stage_ids": missing, "stage_ids": [s["id"] for s in selected]}

        hnpe = [s for s in stages if s["kind"] in ("cases", "hnpe_train", "hnpe_infer")]
        tmcmc = [s for s in stages if s["id"] == "tmcmc"]
        reference = [s for s in stages if s["id"] == "reference"]
        return {"schema": "aws_mesouq.benchmark_allocation_costs.v1", "campaign_id": campaign["id"], "configuration_hash": campaign["configuration_hash"], "scientific_request_hash": scientific_hash, "case_set_sha256": case_bank["sha256"], "science_revision": self.operator["science_revision"], "adapter_sha256": self.operator["adapter_sha256"], "source_binding_sha256": binding["sha256"], "stages": [{k: copy.deepcopy(s[k]) for k in ("id", "kind", "status", "slurm_job_id", "scheduler_accounting", "timing", "failure") if k in s} for s in stages], "costs": {"methods": {"hnpe": totals(hnpe), "tmcmc": totals(tmcmc)}, "reference": totals(reference)}}

    def script(self, entry, control, window):
        scheduler = {**self.operator["scheduler"], "gres": "gpu:1", "time": window["time"]}
        lines = ["#!/bin/bash", "#SBATCH --job-name=" + entry["marker"], "#SBATCH --output=" + control + "/stdout.log", "#SBATCH --error=" + control + "/stderr.log"]
        lines += ["#SBATCH --" + name + "=" + str(value) for name, value in scheduler.items()]
        lines += ["set -euo pipefail", "umask 077"]
        environment = shlex.quote(self.operator["environment_script"])
        digest = shlex.quote(self.operator["environment_sha256"])
        lines += ["test \"$(sha256sum " + environment + " | cut -d' ' -f1)\" = " + digest, "source " + environment, "export MESOUQ_SITE=karolina", "export PYTHONPATH=" + shlex.quote(self.operator["repository"] + "/src") + '${PYTHONPATH:+:"$PYTHONPATH"}']
        stage = entry["claim"]["stage"]["id"]
        # Select Keras before the isolated interpreter imports a native backend.
        if stage in {"train_" + factor for factor in FACTORS}:
            lines.append("export KERAS_BACKEND=jax")
        elif stage == "hnpe_infer":
            lines.append("export KERAS_BACKEND=torch")
        lines += ["exec " + shlex.quote(self.operator["python_bin"]) + " " + shlex.quote(control + "/adapter.py") + " --manifest " + shlex.quote(control + "/manifest.json")]
        return "\n".join(lines) + "\n"

    def accounting(self, entry):
        job = entry.get("job_id") or entry["claim"]["stage"].get("slurm_job_id")
        if not job:
            return None
        output = self._slow(entry, self.transport.run, ["sacct", "--noheader", "--parsable2", "--user", self.slurm.user, "--jobs", job, "--format=JobIDRaw,JobName%128,User%128,State,ElapsedRaw,AllocTRES%512,CPUTimeRAW"])
        for line in output.splitlines():
            parts = line.strip().split("|")
            if len(parts) >= 7 and parts[0] == job and parts[1] == entry["marker"] and parts[2] == self.slurm.user:
                state = parts[3].split()[0].rstrip("+")
                if state not in NATIVE_TERMINAL:
                    return None
                tres = dict(item.split("=", 1) for item in parts[5].split(",") if "=" in item)
                gpu = int(tres.get("gres/gpu", 0))
                if gpu != 1:
                    raise ValueError("Native allocated GPU count differs from the one-GPU stage policy")
                return native_accounting(job_id=job, state=state, elapsed_seconds=int(parts[4]), allocated_gpus=gpu, cpu_seconds=int(parts[6]))
        return None

    def receipt(self, entry):
        campaign, stage = entry["claim"]["campaign"], entry["claim"]["stage"]
        blob = self._slow(entry, self.transport.read_beneath, entry["root"], "stages/" + stage["id"] + "/receipt.json")
        receipt = json.loads(blob)
        if receipt.get("campaign_instance_id") != campaign["id"] or receipt.get("stage") != stage["id"] or receipt.get("request_fingerprint") != campaign.get("scientific_request_hash", scientific_request_checksum(campaign["configuration"])) or str(receipt.get("slurm_job_id")) != entry["job_id"]:
            raise ValueError("Scientific receipt is not bound to this frozen request and native stage")
        if receipt.get("transport_request_fingerprint") != campaign["configuration_hash"]:
            raise ValueError("Scientific receipt transport request differs from this campaign")
        binding = receipt.get("source_binding", {})
        if binding.get("sha256") != checksum(binding.get("files", {})):
            raise ValueError("Scientific source receipt hash is inconsistent")
        if stage["id"] == "metrics":
            costs_path = entry["control"] + "/allocation-costs.json"
            costs_blob = self._slow(entry, self.transport.read, costs_path)
            if receipt.get("allocation_costs_sha256") != hashlib.sha256(costs_blob).hexdigest():
                raise ValueError("Scientific metrics did not bind the authoritative allocation-cost snapshot")
        entry["receipt"] = receipt
        self.state["campaigns"][campaign["id"]].setdefault("receipts", {})[stage["id"]] = receipt
        self.save()
        return receipt

    def _file(self, entry, relative, descriptor=None):
        safe_relative(relative)
        blob = self._slow(entry, self.transport.read_beneath, entry["root"], relative)
        actual = {"sha256": hashlib.sha256(blob).hexdigest(), "size_bytes": len(blob)}
        if descriptor and any(actual[k] != descriptor[k] for k in actual):
            raise ArtifactIntegrityError("Scientific artifact differs from manifested checksum and size")
        return blob, actual

    def _renew_upload_plan(self, entry):
        reservation = entry.get("upload_reservation")
        if not reservation:
            # Older journals lack the reservation event ID. Signed single-object
            # probes refresh existing reservations without creating new objects.
            return
        response = self._slow(entry, self.client.event, entry["claim"], reservation["id"], "artifact_upload", **reservation["fields"])
        refreshed = response["uploads"]
        old_refs = [upload["artifact"]["id"] for upload in entry["upload_plan"]]
        if [upload["artifact"]["id"] for upload in refreshed] != old_refs:
            raise ArtifactIntegrityError("Renewed upload plan changed immutable object identities")
        entry["upload_plan"] = refreshed
        self.save()

    def _confirm_upload(self, entry, artifact_id):
        return self._send(entry, "artifact_status", artifact_ids=[artifact_id])

    def publish(self, entry, receipt):
        stage = entry["claim"]["stage"]["id"]
        selected = []
        if stage == "metrics":
            for ref in receipt.get("artifacts", []):
                name = safe_relative(ref["file"])
                if name.endswith((".png", ".csv", ".json", ".npz")):
                    selected.append(("stages/" + stage + "/" + name, ref))
        # Weights and training banks remain private PROJECT products.
        if not selected:
            portable = {k: receipt[k] for k in ("stage", "status", "factor", "updates", "best_update", "best_validation_nll", "termination_reason", "training_state", "history") if k in receipt}
            relative = "control/" + stage + "/public-stage.json"
            blob = canonical(portable)
            self._slow(entry, self._immutable, entry["root"] + "/" + relative, blob)
            selected.append((relative, {"sha256": hashlib.sha256(blob).hexdigest(), "size_bytes": len(blob)}))
            del blob
        files = []
        for relative, descriptor in selected:
            name = stage + "-" + PurePosixPath(relative).name
            media = "image/png" if name.endswith(".png") else "text/csv" if name.endswith(".csv") else "application/octet-stream" if name.endswith(".npz") else "application/json"
            files.append({"relative": relative, "ref": {"name": name, "media_type": media, "sha256": descriptor["sha256"], "size_bytes": descriptor["size_bytes"]}})
        if entry.get("upload_source") is not None and entry["upload_source"] != files:
            raise ArtifactIntegrityError("Publication changed after immutable upload reservation")
        if not entry.get("upload_plan"):
            entry["upload_source"] = files
            response = self._send(entry, "artifact_upload", artifacts=[item["ref"] for item in files])
            entry["upload_plan"] = response["uploads"]
            self.save()
        self._renew_upload_plan(entry)
        progress = entry.setdefault("upload_progress", {})
        refs = []
        for item, upload in zip(files, entry["upload_plan"], strict=True):
            ref = upload["artifact"]
            if any(ref[k] != item["ref"][k] for k in ("name", "sha256", "size_bytes", "media_type")):
                raise ArtifactIntegrityError("Cloud upload reservation differs from scientific output")
            # At most one artifact body is resident. Completed local markers are
            # hints only: every restart re-verifies the exact remote SHA and size.
            blob, _ = self._file(entry, item["relative"], item["ref"])
            try:
                confirmed = False
                for attempt in range(2):
                    status = self._confirm_upload(entry, ref["id"])
                    if status["uploaded"]:
                        confirmed = True
                        break
                    target = urlsplit(status["upload_url"])
                    origin = target.scheme + "://" + target.netloc
                    if origin not in self.operator["allowed_upload_origins"] or target.username or target.password:
                        raise ValueError("Artifact upload URL is outside operator-approved HTTPS origins")
                    progress[ref["id"]] = {"status": "putting", "sha256": ref["sha256"], "size_bytes": ref["size_bytes"]}
                    self.save()
                    try:
                        self._slow(entry, self.api.transfer, status["upload_url"], data=blob, headers=status["required_headers"])
                    except Exception:
                        # A 412, lost acknowledgment or expired signature is not
                        # proof of success. Only the backend checksum/size probe is.
                        status = self._confirm_upload(entry, ref["id"])
                        if status["uploaded"]:
                            confirmed = True
                            break
                        if attempt == 1:
                            raise
                        self._renew_upload_plan(entry)
                    else:
                        status = self._confirm_upload(entry, ref["id"])
                        if status["uploaded"]:
                            confirmed = True
                            break
                        if attempt == 1:
                            raise RuntimeError("Upload acknowledgment could not be verified")
                        self._renew_upload_plan(entry)
                if not confirmed:
                    raise RuntimeError("Private object upload remains unconfirmed")
                progress[ref["id"]] = {"status": "confirmed", "sha256": ref["sha256"], "size_bytes": ref["size_bytes"], "confirmed_at": self.clock()}
                self.save()
                refs.append({k: ref[k] for k in ("id", "name", "sha256", "size_bytes", "media_type")})
            finally:
                del blob
        entry["published_refs"] = refs
        self.save()
        return refs

    def summary(self, entry, receipt, refs):
        scientific = receipt.get("result", {})
        histories = []
        for factor in FACTORS:
            trained = self.state["campaigns"][entry["claim"]["campaign"]["id"]].get("receipts", {}).get("train_" + factor, {})
            history = trained.get("history")
            if history is None:
                history = scientific.get("hnpe_training", {}).get(factor, {}).get("history")
            if history is None:
                history = []
            kept = history[::max(1, math.ceil(len(history) / 200))]
            if history and (not kept or kept[-1] != history[-1]):
                kept.append(history[-1])
            if kept:
                histories.append({"title": factor + " validation history", "x_label": "Training updates", "y_label": "Validation NLL", "series": [{"name": factor, "x": [row["update"] for row in kept], "y": [row["validation_nll"] for row in kept]}], "history": history[::max(1, math.ceil(len(history) / 50))], "metadata_sampling": "Bounded measured checkpoints; full histories are retained in scientific artifacts"})
        return {"scientific_status": scientific.get("scientific_status"), "paired_comparison": scientific.get("paired_comparison"), "allocation_costs": scientific.get("allocation_costs"), "allocation_costs_binding": scientific.get("allocation_costs_binding"), "allocation_costs_sha256": scientific.get("allocation_costs_sha256"), "metrics": {method: payload.get("summary") for method, payload in scientific.get("methods", {}).items()}, "case_results": {method: [{"world_index": row.get("world_index"), "status": row.get("status"), "metrics": {"score": row.get("metrics", {}).get("score")}, "termination_reason": row.get("termination_reason")} for row in payload.get("cases", [])] for method, payload in scientific.get("methods", {}).items()}, "plots": [], "training_histories": histories, "artifacts": refs, "limitations": scientific.get("limitations", [])}

    def complete(self, entry, accounting):
        timing = {"wall_seconds": accounting["elapsed_seconds"], "cpu_seconds": accounting["cpu_seconds"], "gpu_seconds": accounting["elapsed_seconds"] * accounting["allocated_gpus"]}
        if accounting["state"] == "CANCELLED":
            self._send(entry, "cancelled", timing=timing, scheduler_accounting=accounting)
            return
        relative = "stages/" + entry["claim"]["stage"]["id"] + "/receipt.json"
        probe = self._slow(entry, self.transport.run, [getattr(self.transport, "staging_python", "/usr/bin/python3.11"), "-I", "-c", "import os,sys;print('present' if os.path.lexists(sys.argv[1]) else 'missing')", entry["root"] + "/" + relative]).strip()
        if probe not in ("present", "missing"):
            raise RuntimeError("Ambiguous scientific receipt availability")
        try:
            receipt = self.receipt(entry) if probe == "present" else None
        except ValueError:
            self._send(entry, "failed", failure={"code": "invalid_scientific_receipt", "message": "Native stage output failed its immutable request or source binding"}, timing=timing, scheduler_accounting=accounting)
            return
        if accounting["state"] != "COMPLETED" or receipt is None or receipt.get("status") not in ("completed", "reference_unresolved", "budget_exhausted"):
            code = "missing_scientific_receipt" if receipt is None else receipt.get("status", "native_stage_failed")
            self._send(entry, "failed", failure={"code": safe_id(code), "message": "Native stage did not produce an eligible bound scientific completion"}, timing=timing, scheduler_accounting=accounting)
            return
        if receipt["status"] in ("reference_unresolved", "budget_exhausted"):
            self._send(entry, "failed", failure={"code": receipt["status"], "message": str(receipt.get("termination_reason", receipt["status"]))[:1000]}, timing=timing, scheduler_accounting=accounting)
            return
        try:
            refs = self.publish(entry, receipt)
        except Exception as error:
            if not isinstance(error, ArtifactIntegrityError) and getattr(error, "code", None) != "artifact_integrity_mismatch":
                raise
            if entry.get("pending_event", {}).get("type") == "artifact_status":
                entry.pop("pending_event", None)  # Definitively rejected read-only probe.
            entry["upload_failure"] = {"code": "artifact_integrity_mismatch"}
            self.save()
            self._send(entry, "failed", failure={"code": "artifact_integrity_mismatch", "message": "Publication checksum or size differs from its immutable reservation"}, timing=timing, scheduler_accounting=accounting)
            return
        campaign, stage = entry["claim"]["campaign"], entry["claim"]["stage"]
        stored = self.state["campaigns"][campaign["id"]].get("receipts", {})
        cases = stored.get("cases")
        if cases is None:
            raw = self._slow(entry, self.transport.read_beneath, entry["root"], "stages/cases/receipt.json")
            cases = json.loads(raw)
        case_hash = cases["case_bank"]["sha256"]
        result = {"schema": SCHEMA, "provenance": {"campaign_id": campaign["id"], "configuration_hash": campaign["configuration_hash"], "adapter_sha256": self.operator["adapter_sha256"], "science_revision": self.operator["science_revision"], "site": "karolina", "case_set_sha256": case_hash}, "timing": timing, "artifacts": refs, "metrics": {"scientific_status": receipt["status"]}}
        if stage["id"] == "cases":
            result["metrics"]["common_case_generation_costs"] = {"wall_seconds": receipt.get("shared_case_generation_seconds")}
        if stage["id"] == "metrics":
            result["summary"] = self.summary(entry, receipt, refs)
        self._send(entry, "succeeded", result=result, scheduler_accounting=accounting)

    def poll(self, entry):
        check_transport_health(self.transport)
        if entry.get("pending_event"):
            self._send(entry)
        if entry.get("cloud_terminal"):
            return
        self._before_slow(entry)
        stage = entry["claim"]["stage"]
        if "script" not in entry:
            if stage.get("reserved_seconds", 60) < 60:
                self._send(entry, "failed", not_submitted=True, failure={"code": "budget_exhausted", "message": "Remaining method budget cannot fund one native Slurm minute"})
                return
            self.stage(entry)
        records = dict(self._slow(entry, self.slurm.lookup, entry["marker"]))
        if len(records) > 1:
            raise RuntimeError("Ambiguous own-user benchmark stage marker; no submission or cancellation")
        job = entry.get("job_id") or stage.get("slurm_job_id")
        if job is not None:
            entry["job_id"] = job
            self.save()
        if job is None and records:
            if len(records) != 1:
                raise RuntimeError("Ambiguous own-user benchmark stage marker; no submission or cancellation")
            job = next(iter(records))
            entry["job_id"] = job
            self.save()
        if job is None:
            if entry.get("submission_intent") or entry["claim"].get("reconcile_required") or stage["status"] in ("submitting", "submission_unknown", "cancelling"):
                # No match is not proof sbatch did not commit or accounting is visible.
                if stage["status"] == "cancelling" and not entry.get("submission_intent") and not stage.get("submission_started"):
                    self._send(entry, "cancelled", not_submitted=True)
                return
            self._send(entry, "submitting")
            if entry["claim"]["stage"]["status"] == "cancelling":
                if not entry["claim"]["stage"].get("submission_started"):
                    self._send(entry, "cancelled", not_submitted=True)
                return
            entry["submission_intent"] = True
            self.save()
            self._before_slow(entry)
            try:
                job = self.slurm.submit(entry["script"])
            except Exception:
                # Persist intent; subsequent sweeps only look up the exact marker.
                entry["submission_unknown"] = True
                self.save()
                raise
            entry["job_id"] = job
            self.save()
            self._send(entry, "submitted", slurm_job_id=job)
            return
        if stage.get("slurm_job_id") is None:
            self._send(entry, "submitted", slurm_job_id=job)
        if job not in records:
            return
        current = self.client.get(self.project, entry["claim"]["campaign"]["id"])["campaign"]
        entry["claim"]["campaign"] = current
        authoritative = next(s for s in current["stages"] if s["id"] == stage["id"])
        entry["claim"]["stage"] = authoritative
        if authoritative["status"] == "cancelling" and records[job] not in NATIVE_TERMINAL:
            self._before_slow(entry)
            self.slurm.cancel(job, entry["marker"], authorize=lambda: self._valid_lease(entry))
            return
        accounting = self.accounting(entry)
        if accounting:
            self.complete(entry, accounting)
        elif records[job] == "RUNNING" and authoritative["status"] in ("submitted", "queued"):
            self._send(entry, "running")

    @staticmethod
    def _archive_batches(items):
        batches = []
        batch = []
        logical_bytes = 0
        for item in items:
            size = item.get("size_bytes")
            if type(size) is not int or size < 0:
                raise ArtifactIntegrityError("Invalid archive file size")
            if batch and (len(batch) >= 32 or logical_bytes + size > 128 * 1024 * 1024):
                batches.append(batch)
                batch = []
                logical_bytes = 0
            batch.append(item)
            logical_bytes += size
            if len(batch) == 32 or logical_bytes >= 128 * 1024 * 1024:
                batches.append(batch)
                batch = []
                logical_bytes = 0
        if batch:
            batches.append(batch)
        return batches

    def _archive_remote(self, operation, *args, **kwargs):
        try:
            return operation(*args, **kwargs)
        except subprocess.CalledProcessError as error:
            if error.returncode == 255:
                raise
            raise ArtifactIntegrityError("Archive helper refused unsafe, changed or unavailable files") from error

    @staticmethod
    def _archive_file_records(values):
        if not isinstance(values, list) or len(values) > 10001:
            raise ArtifactIntegrityError("Invalid bounded archive inventory")
        result, names = [], set()
        for value in values:
            if not isinstance(value, dict) or set(value) != {"path", "sha256", "size_bytes"}:
                raise ArtifactIntegrityError("Archive file records must remain path/hash/size only")
            try:
                name = safe_relative(value["path"])
            except (TypeError, ValueError) as error:
                raise ArtifactIntegrityError("Unsafe archive path") from error
            if name in names or not isinstance(value["sha256"], str) or not re.fullmatch(r"[a-f0-9]{64}", value["sha256"]) or type(value["size_bytes"]) is not int or value["size_bytes"] < 0:
                raise ArtifactIntegrityError("Invalid or duplicate archive file")
            names.add(name)
            result.append(dict(value))
        return result

    def _archive_snapshot(self, root):
        output = self._archive_remote(self.transport.run, [getattr(self.transport, "staging_python", "/usr/bin/python3.11"), "-I", "-c", TREE_HELPER, root])
        try:
            snapshot = json.loads(output)
        except (ValueError, TypeError) as error:
            raise ArtifactIntegrityError("Invalid archive source inventory response") from error
        if not isinstance(snapshot, dict) or set(snapshot) != {"version", "files", "source_link_groups"} or type(snapshot["version"]) is not int or snapshot["version"] != 1:
            raise ArtifactIntegrityError("Unsupported archive source inventory")
        files = self._archive_file_records(snapshot["files"])
        if files != sorted(files, key=lambda item: item["path"]):
            raise ArtifactIntegrityError("Archive source inventory is not canonical")
        by_path = {item["path"]: item for item in files}
        groups = snapshot["source_link_groups"]
        if not isinstance(groups, list) or len(groups) > 5000:
            raise ArtifactIntegrityError("Invalid bounded archive link groups")
        proofs = {}
        root_keys = {"dev", "ino", "uid", "mode"}
        file_keys = root_keys | {"nlink", "size", "mtime_ns", "ctime_ns"}
        for proof in groups:
            if not isinstance(proof, dict) or set(proof) != {"root", "file", "paths"} or not isinstance(proof["root"], dict) or set(proof["root"]) != root_keys or not isinstance(proof["file"], dict) or set(proof["file"]) != file_keys:
                raise ArtifactIntegrityError("Malformed source link proof")
            if any(type(value) is not int or value < 0 for value in (*proof["root"].values(), *proof["file"].values())):
                raise ArtifactIntegrityError("Invalid source link identity")
            paths = proof["paths"]
            if not isinstance(paths, list) or not 2 <= len(paths) <= 128 or not all(isinstance(name, str) for name in paths) or paths != sorted(set(paths)) or proof["file"]["nlink"] != len(paths) or len(canonical(proof)) > 65536:
                raise ArtifactIntegrityError("Incomplete or excessive source alias proof")
            if proof["root"]["mode"] != 0o700 or proof["root"]["uid"] != proof["file"]["uid"] or proof["file"]["mode"] & 0o077:
                raise ArtifactIntegrityError("Source proof is not private")
            hashes = set()
            for name in paths:
                if name not in by_path or name in proofs or name == "archive-manifest.json" or by_path[name]["size_bytes"] != proof["file"]["size"]:
                    raise ArtifactIntegrityError("Source proof differs from its complete inventory")
                hashes.add(by_path[name]["sha256"])
                proofs[name] = proof
            if len(hashes) != 1:
                raise ArtifactIntegrityError("One source inode has inconsistent bytes")
        return files, proofs

    def _archive_manifest(self, campaign, originals):
        return canonical({"campaign_id": campaign["id"], "configuration_hash": campaign["configuration_hash"], "scientific_request_hash": campaign.get("scientific_request_hash", scientific_request_checksum(campaign["configuration"])), "approved_adapter": campaign["approved_adapter"], "original_file_count": len(originals), "original_files_sha256": checksum(originals), "retained_outputs": originals})

    def _archive_cached(self, campaign, record):
        files = self._archive_file_records(record["archive_files"])
        originals = [item for item in files if item["path"] != "archive-manifest.json"]
        manifests = [item for item in files if item["path"] == "archive-manifest.json"]
        if len(originals) > 10000 or originals != sorted(originals, key=lambda item: item["path"]) or len(manifests) != 1 or not isinstance(record.get("archive_manifest"), str):
            raise ArtifactIntegrityError("Cached archive inventory is incomplete")
        blob = self._archive_manifest(campaign, originals)
        if record["archive_manifest"].encode() != blob:
            raise ArtifactIntegrityError("Cached archive manifest differs from campaign/request/source bindings")
        expected = {"path": "archive-manifest.json", "sha256": hashlib.sha256(blob).hexdigest(), "size_bytes": len(blob)}
        if manifests[0] != expected:
            raise ArtifactIntegrityError("Cached archive manifest hash or size differs")
        return files, originals, expected, blob

    def _archive_acknowledge(self, campaign, record, files):
        manifest = next(item for item in files if item["path"] == "archive-manifest.json")
        originals = [item for item in files if item["path"] != "archive-manifest.json"]
        refs = [{key: artifact[key] for key in ("id", "sha256", "size_bytes")} for artifact in campaign.get("artifacts", []) if artifact.get("verified")]
        receipt = {"site": "karolina", "path": self.operator["archive_root"] + "/" + campaign["id"], "manifest_sha256": manifest["sha256"], "artifacts": refs, "retained_outputs": [manifest], "attested_original_file_count": len(originals), "attested_original_files_sha256": checksum(originals)}
        event_id = record.setdefault("archive_event_id", uuid.uuid4().hex)
        self.save()
        self.client.archive(campaign, event_id, receipt)
        record["archive_verified"] = True
        self.save()

    def archive(self, campaign):
        check_transport_health(self.transport)
        ident = safe_id(campaign["id"])
        if campaign.get("archive", {}).get("status") != "requested":
            return
        binding = campaign["approved_adapter"]
        if binding.get("archive_root") != self.operator["archive_root"] or binding.get("science_revision") != self.operator["science_revision"] or binding.get("adapter_sha256") != self.operator["adapter_sha256"] or campaign.get("owner_sub") != self.journal.data["owner_sub"] or campaign.get("project_id") != self.project:
            raise ArtifactIntegrityError("Archive campaign differs from immutable owner/source/storage binding")
        validate_create(campaign["configuration"])
        if checksum(campaign["configuration"]) != campaign["configuration_hash"] or campaign.get("scientific_request_hash", scientific_request_checksum(campaign["configuration"])) != scientific_request_checksum(campaign["configuration"]):
            raise ArtifactIntegrityError("Archive request hashes differ from the frozen configuration")
        expected_root = self.operator["run_root"] + "/" + ident
        record = self.state["campaigns"].setdefault(ident, {"root": expected_root})
        root = record["root"]
        if root != expected_root:
            raise ArtifactIntegrityError("Archive source root differs from the campaign binding")
        cached = record.get("archive_files") is not None
        if cached:
            files, originals, manifest_ref, blob = self._archive_cached(campaign, record)
            complete = True
            for batch in self._archive_batches(files):
                transfers = [{"destination": item["path"], "sha256": item["sha256"], "size_bytes": item["size_bytes"]} for item in batch]
                try:
                    self.transport.archive_verify_batch(self.operator["archive_root"], ident, transfers)
                except subprocess.CalledProcessError as error:
                    if error.returncode == 255:
                        raise
                    if error.returncode != 3:
                        raise ArtifactIntegrityError("Durable archive verification failed; operator review is required") from error
                    complete = False
            if complete:
                self._archive_acknowledge(campaign, record, files)
                return
        elif record.get("archive_manifest") is not None:
            raise ArtifactIntegrityError("Cached archive manifest lacks its bound inventory")
        fresh, proofs = self._archive_snapshot(root)
        original_inventory = [item for item in fresh if item["path"] != "archive-manifest.json"]
        own_manifest = [item for item in fresh if item["path"] == "archive-manifest.json"]
        if cached:
            if original_inventory != originals or own_manifest and own_manifest != [manifest_ref]:
                raise ArtifactIntegrityError("Partial archive source differs from its immutable inventory")
        else:
            if own_manifest or len(original_inventory) > 10000:
                raise ArtifactIntegrityError("Unexpected pre-existing archive manifest")
            originals = original_inventory
            blob = self._archive_manifest(campaign, originals)
            manifest_ref = {"path": "archive-manifest.json", "sha256": hashlib.sha256(blob).hexdigest(), "size_bytes": len(blob)}
            files = [*originals, manifest_ref]
            record["archive_manifest"] = blob.decode()
            record["archive_files"] = files
            self.save()
        self._archive_remote(self._immutable, root + "/archive-manifest.json", blob)
        self._archive_remote(self.transport.archive_prepare, self.operator["archive_root"], ident)
        for batch in self._archive_batches(files):
            transfers = []
            for item in batch:
                transfer = {"source_root": root, "source": item["path"], "destination": item["path"], "sha256": item["sha256"], "size_bytes": item["size_bytes"]}
                if item["path"] in proofs:
                    transfer["source_link_group"] = proofs[item["path"]]
                transfers.append(transfer)
            self._archive_remote(self.transport.archive_copy_batch, self.operator["archive_root"], ident, transfers)
            self._archive_remote(self.transport.archive_verify_batch, self.operator["archive_root"], ident, transfers)
        self._archive_acknowledge(campaign, record, files)



    def sweep(self):
        check_transport_health(self.transport)
        errors = []
        for entry in list(self.state["stages"].values()):
            if not entry.get("cloud_terminal"):
                try:
                    self.poll(entry)
                except SSHSessionError:
                    raise
                except Exception as error:
                    errors.append(type(error).__name__)
        while sum(not e.get("cloud_terminal") for e in self.state["stages"].values()) < self.maximum_active:
            entry = self._claim()
            if entry is None:
                break
            try:
                self.poll(entry)
            except SSHSessionError:
                raise
            except Exception as error:
                errors.append(type(error).__name__)
        check_transport_health(self.transport)
        campaigns = self.api.request("GET", f"/v1/projects/{self.project}/benchmarks")["campaigns"]
        for campaign in campaigns:
            if campaign.get("archive", {}).get("status") == "requested":
                try:
                    self.archive(campaign)
                except (SSHSessionError, ArtifactIntegrityError):
                    raise
                except Exception as error:
                    errors.append(type(error).__name__)
        return {"active_stages": sum(not e.get("cloud_terminal") for e in self.state["stages"].values()), "errors": errors}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--api-url", required=True)
    parser.add_argument("--project", required=True)
    parser.add_argument("--oauth-domain", required=True)
    parser.add_argument("--client-id", required=True)
    parser.add_argument("--callback-port", type=int, default=8765)
    parser.add_argument("--operator-config", required=True, type=Path)
    parser.add_argument("--journal", required=True, type=Path)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--ssh-host")
    mode.add_argument("--local-cluster", action="store_true")
    parser.add_argument("--once", action="store_true")
    parser.add_argument("--poll-seconds", type=float, default=20)
    args = parser.parse_args(argv)
    if not args.journal.is_dir() or not (args.journal / "journal.json").is_file():
        parser.error("An existing enrolled normal connector journal is required")
    if args.poll_seconds < 1:
        parser.error("Poll interval must be at least one second")
    from .connector.api import API
    from .connector.auth import browser_login
    from .connector.journal import Journal
    from .connector.slurm import Slurm
    from .connector.transport import Transport
    operator = validate_operator(json.loads(args.operator_config.read_text()))
    transport = Transport(host=args.ssh_host, local=args.local_cluster)
    journal = None
    try:
        journal = Journal(args.journal)
        existing_identity(journal)
        slurm = Slurm(transport)
        if transport.identity() != operator["expected_user"] or transport.run(["hostname", "-f"]).strip() != operator["expected_host"]:
            parser.error("HPC identity differs from the approved operator account and endpoint")
        api = API(args.api_url, browser_login(args.oauth_domain, args.client_id, args.callback_port))
        worker = BenchmarkWorker(api, slurm, journal, project=args.project, operator=operator)
        while True:
            print(json.dumps(worker.sweep(), sort_keys=True), flush=True)
            if args.once:
                break
            time.sleep(args.poll_seconds)
        return 0
    except (SSHSessionError, ArtifactIntegrityError) as error:
        category = error.category if isinstance(error, SSHSessionError) else "archive_integrity_error"
        print(json.dumps({"observed_at": time.time(), "connector_blocked": True, "error_category": category, "recovery_required": True}), flush=True)
        return 2
    finally:
        try:
            if journal is not None:
                journal.close()
        finally:
            transport.close()


if __name__ == "__main__":
    raise SystemExit(main())
