"""Lease-aware single-session worker. Scientific execution belongs to publishing.runner."""
from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
import re
import mimetypes
from pathlib import Path, PurePosixPath
import shlex
import uuid

from .transport import absolute_path, safe_id, safe_relative
from .signing import device_identity, sign_archive, sign_result
from aws_mesouq.contracts import CANONICAL_PROFILE, validate_result


class ModelUnavailable(ValueError):
    """An approved checkpoint needs trusted installation on this HPC account."""


class StaleEvent(RuntimeError):
    """Definitively rejected event; resume from authoritative cloud state."""


class LeaseExpired(RuntimeError):
    pass


class Connector:
    def __init__(self, api, slurm, journal, *, project: str, site: str, run_root: str,
                 repository: str, model_registry: dict, scheduler: dict | None = None, runtime: dict | None = None,
                 archive_root: str | None = None, durable_archive_root: bool = False):
        if site not in {"vega", "karolina"}:
            raise ValueError("Explicit site must be vega or karolina")
        self.api, self.slurm, self.journal = api, slurm, journal
        self.project, self.site = safe_id(project), site
        self.run_root, self.repository = absolute_path(run_root), absolute_path(repository)
        self.archive_root = absolute_path(archive_root) if archive_root else None
        self.durable_archive_root = bool(durable_archive_root)
        if self.durable_archive_root and not self.archive_root:
            raise ValueError("Durable archive attestation requires a separate archive_root")
        if self.archive_root:
            run_path, archive_path = PurePosixPath(self.run_root), PurePosixPath(self.archive_root)
            if (run_path == archive_path or run_path in archive_path.parents or archive_path in run_path.parents
                    or archive_path == PurePosixPath("/") or run_path == PurePosixPath("/")):
                raise ValueError("Compute and archive roots must be separate, nonnested private directories")
        if not re.fullmatch(r"/[A-Za-z0-9_./-]+", self.run_root):
            raise ValueError("Run root must be safe for Slurm directives")
        self.models = model_registry
        self.runtime = dict(runtime or {"python_bin": "/usr/bin/python3.11"})
        if set(self.runtime) - {"python_bin", "environment_script", "environment_sha256", "site_runtime_root"}:
            raise ValueError("Unsupported trusted runtime profile field")
        self.runtime["python_bin"] = absolute_path(self.runtime["python_bin"])
        if self.runtime.get("site_runtime_root"):
            self.runtime["site_runtime_root"] = absolute_path(self.runtime["site_runtime_root"])
        if self.runtime.get("environment_script"):
            self.runtime["environment_script"] = absolute_path(self.runtime["environment_script"])
        if "environment_sha256" in self.runtime and (not self.runtime.get("environment_script") or not re.fullmatch(r"[a-f0-9]{64}", self.runtime["environment_sha256"])):
            raise ValueError("Environment digest must pin a trusted script with SHA-256")
        self.scheduler = {"cpus-per-task": "1", "time": "00:30:00", **(scheduler or {})}
        if set(self.scheduler) - {"cpus-per-task", "time", "partition", "account", "qos", "gres", "mem"}:
            raise ValueError("Unsupported trusted scheduler setting")
        if any(not re.fullmatch(r"[A-Za-z0-9_:.+-]+", str(value)) for value in self.scheduler.values()):
            raise ValueError("Unsafe scheduler setting")
        if not 1 <= int(self.scheduler["cpus-per-task"]) <= 16:
            raise ValueError("Connector jobs permit at most sixteen CPU cores")
        if self.scheduler.get("gres") not in {None, "gpu:1"}:
            raise ValueError("Connector jobs permit at most one GPU")
        if "connector_id" not in journal.data:
            journal.data["connector_id"] = str(uuid.uuid4())
            journal.save()
        self.connector_id = journal.data["connector_id"]
        identity = device_identity(journal)
        context = {"site": self.site, "project": self.project, "run_root": self.run_root,
                   "repository": self.repository, "scheduler_user": self.slurm.user,
                   "host": self.slurm.transport.host if hasattr(self.slurm.transport, "host") else None,
                   "api": getattr(self.api, "base", None), "runtime": self.runtime,
                   "archive_root": self.archive_root, "durable_archive_root": self.durable_archive_root,
                   "archive_public_key": identity["archive_public_key"]}
        if journal.data.get("context", context) != context:
            raise ValueError("Journal belongs to a different endpoint/site/account/project or durability policy; reuse its original session configuration")
        journal.data["context"] = context
        journal.save()
        self.lease = None

    @property
    def archive_capable(self):
        # Vega cannot claim a Karolina durable receipt in this release.
        return self.site == "karolina" and self.durable_archive_root and self.archive_root is not None

    def valid_lease(self):
        expiry = None if not self.lease else self.lease["lease_expires_at"]
        expires = (datetime.fromtimestamp(expiry, timezone.utc) if isinstance(expiry, (int, float))
                   else datetime.fromisoformat(expiry.replace("Z", "+00:00")) if expiry else None)
        if expires is None or expires <= datetime.now(timezone.utc):
            raise LeaseExpired("Connector lease expired; no scheduler mutation is permitted")

    def renew_before(self, job_id: str, *, minimum_seconds: float = 105):
        """Renew before a bounded slow operation; pending events must replay first."""
        self.valid_lease()
        if self.lease["job"]["id"] != job_id:
            raise LeaseExpired("Current lease belongs to another job")
        if self.journal.data["jobs"][job_id].get("pending_event"):
            raise RuntimeError("Pending event must be reconciled before further operations")
        expiry = self.lease["lease_expires_at"]
        expires = (datetime.fromtimestamp(expiry, timezone.utc) if isinstance(expiry, (int, float))
                   else datetime.fromisoformat(expiry.replace("Z", "+00:00")))
        if (expires - datetime.now(timezone.utc)).total_seconds() < minimum_seconds:
            result = self.event(job_id, "heartbeat")
            self.valid_lease()
            if result.get("status") == "cancelling":
                raise StaleEvent("Cancellation observed during lease renewal")
            renewed = self.lease["lease_expires_at"]
            renewed_at = (datetime.fromtimestamp(renewed, timezone.utc) if isinstance(renewed, (int, float))
                          else datetime.fromisoformat(renewed.replace("Z", "+00:00")))
            if (renewed_at - datetime.now(timezone.utc)).total_seconds() < minimum_seconds:
                raise LeaseExpired("Heartbeat did not provide enough lease time for a slow operation")

    def slow(self, job_id, operation, *args, **kwargs):
        self.renew_before(job_id)
        result = operation(*args, **kwargs)
        self.valid_lease()
        return result

    def preflight_storage(self, job_id):
        """Reject permissive inherited file permissions before staging/submission."""
        for root in [self.run_root, *([self.archive_root] if self.archive_root else [])]:
            self.slow(job_id, self.slurm.transport.private_root_probe, root)

    def event(self, job_id, event_type, **fields):
        self.valid_lease()
        if self.lease["job"]["id"] != job_id:
            raise LeaseExpired("Current lease belongs to another job")
        entry = self.journal.data["jobs"][job_id]
        payload = entry.get("pending_event")
        replaying = payload is not None
        if payload is not None and payload["type"] != event_type:
            raise RuntimeError("Pending event must replay before a different event type")
        if payload is None:
            payload = {"project_id": self.project, "connector_id": self.connector_id,
                       "event_id": str(uuid.uuid4()), "type": event_type, **fields}
            if event_type == "archive_verified":
                payload["archive_signature"] = sign_archive(self.journal, job_id, self.journal.data["owner_sub"], payload)
            elif event_type == "succeeded":
                payload["result_signature"] = sign_result(self.journal, job_id, self.journal.data["owner_sub"], payload)
            entry["pending_event"] = payload
            self.journal.save()
        if payload["type"] == "archive_verified" or replaying and payload["type"] == "succeeded" and entry.get("archive_receipt"):
            # A receipt may not have reached the server before disconnect. Check
            # the retained durable bytes again before replaying its stable ID,
            # independently of whether SCRATCH still exists.
            binding = entry.get("archive_binding")
            if not self.archive_capable or not binding or (payload["type"] == "archive_verified" and payload["archive"] != entry.get("archive_receipt")):
                raise ValueError("Pending archive receipt lacks its verified immutable binding")
            token = PurePosixPath(binding["path"]).name
            if binding["path"] != self.archive_root + "/" + token:
                raise ValueError("Pending archive receipt path differs from configured PROJECT root")
            for item in binding["files"]:
                self.valid_lease()
                self.slurm.transport.archive_verify(self.archive_root, token, item)
                self.valid_lease()
        try:
            result = self.api.request("POST", f"/v1/connector/jobs/{job_id}/events",
                                      {**payload, "lease_token": self.lease["lease_token"]})
        except Exception as exc:
            # Only a definitive rejected transition can be discarded. Transport
            # failures may have committed remotely and must retain the event ID.
            if getattr(exc, "status", None) == 409 and getattr(exc, "code", None) == "invalid_transition":
                current = self.api.request("GET", f"/v1/projects/{self.project}/jobs/{job_id}")
                if current.get("id") != job_id or current.get("project_id") != self.project or current.get("owner_sub") != self.journal.data.get("owner_sub"):
                    raise ValueError("Rejected event reconciliation identity mismatch") from exc
                entry.pop("pending_event", None)
                self.journal.save()
                raise StaleEvent("Cloud state changed; reconcile on next claim") from None
            raise
        entry.pop("pending_event", None)
        if payload["type"] in {"failed", "cancelled"}:
            entry["terminal"] = True
        if payload["type"] in {"succeeded", "archive_verified"}:
            entry[payload["type"] + "_sent"] = True
        self.journal.save()
        if result.get("lease_expires_at"):
            self.lease["lease_expires_at"] = result["lease_expires_at"]
        return result

    def stage(self, job, entry):
        model = self.models.get(job["model_version"])
        approved = job.get("approved_model")
        if model is None or model.get("version") != job["model_version"] or not isinstance(approved, dict) or approved.get("version") != job["model_version"]:
            raise ModelUnavailable("Approved model version is not installed in the trusted local registry")
        adapter = (Path(__file__).resolve().parents[1] / "adapters" / "indentation.py").read_bytes()
        adapter_sha256 = hashlib.sha256(adapter).hexdigest()
        if approved.get("approved_adapter_sha256") != adapter_sha256 or model.get("approved_adapter_sha256", adapter_sha256) != adapter_sha256:
            raise ModelUnavailable("Approved adapter hash differs from the trusted shipped scientific adapter")
        requested_profile = job.get("configuration", {}).get("scientific_profile")
        cloud_provenance = approved.get("provenance", {})
        local_provenance = model.get("provenance", {})
        private_profile = model.get("scientific_profile")
        cloud_bound = isinstance(cloud_provenance, dict) and "pipeline_binding" in cloud_provenance
        local_bound = isinstance(local_provenance, dict) and "pipeline_binding" in local_provenance
        if requested_profile is not None or cloud_bound or local_bound or private_profile is not None:
            binding = cloud_provenance.get("pipeline_binding") if isinstance(cloud_provenance, dict) else None
            canonical_json = lambda value: json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)
            try:
                matches = (requested_profile == CANONICAL_PROFILE and isinstance(binding, dict)
                           and binding.get("id") == CANONICAL_PROFILE and local_bound
                           and isinstance(private_profile, dict) and private_profile.get("id") == CANONICAL_PROFILE
                           and canonical_json(local_provenance["pipeline_binding"]) == canonical_json(binding)
                           and "binding" in private_profile and canonical_json(private_profile["binding"]) == canonical_json(binding))
            except (ValueError, TypeError):
                matches = False
            if not matches:
                raise ModelUnavailable("Approved scientific profile binding differs from the trusted installed profile")
        diameters = job.get("configuration", {}).get("emb_diameters") or [job["diameter_um"]]
        expected = approved.get("sha256")
        local_hashes = model.get("sha256", {})
        for diameter in diameters:
            key = str(float(diameter))
            digest = expected.get(key) if isinstance(expected, dict) else expected if approved.get("diameter_um") == diameter else None
            if not isinstance(digest, str) or not re.fullmatch(r"[a-f0-9]{64}", digest) or local_hashes.get(key) != digest:
                raise ModelUnavailable("Approved model checkpoint hashes do not match the trusted installed model")
            if cloud_bound and digest != binding.get("model_sha256", {}).get(key):
                # A promoted checkpoint has its own validated support table;
                # accepted baseline support cannot describe different weights.
                validation = approved.get("validation", {})
                support_by_diameter = model.get("training_support", {})
                support = support_by_diameter.get(key) if isinstance(support_by_diameter, dict) else None
                if (not isinstance(validation, dict) or validation.get("passed") is not True
                        or validation.get("model_sha256") != digest
                        or not isinstance(validation.get("training_data_sha256"), str)
                        or not re.fullmatch(r"[a-f0-9]{64}", validation["training_data_sha256"])
                        or not isinstance(support, dict) or support.get("sha256") != validation["training_data_sha256"]):
                    raise ModelUnavailable("Promoted model requires its authoritative validated training support")
                try:
                    absolute_path(support.get("path"))
                except (ValueError, TypeError):
                    raise ModelUnavailable("Promoted model training support requires a trusted private absolute path") from None
        run = entry["run_dir"]
        transport = self.slurm.transport
        self.preflight_storage(job["id"])
        self.slow(job["id"], transport.run, ["mkdir", "-m", "700", run])  # No -p: never reuse an existing run directory.
        inputs = []
        for index, artifact in enumerate(job.get("input_artifacts", [])):
            aid = safe_id(artifact["id"])
            reference = self.slow(job["id"], self.api.request, "GET", f"/v1/projects/{self.project}/artifacts/{aid}")
            blob = self.slow(job["id"], self.api.transfer, reference["download_url"])
            if len(blob) != artifact["size_bytes"] or hashlib.sha256(blob).hexdigest() != artifact["sha256"]:
                raise ValueError("Input artifact checksum/size mismatch")
            path = f"{run}/input-{index}"
            self.slow(job["id"], transport.write, path, blob)
            inputs.append({**artifact, "path": path})
        runtime = dict(self.runtime)
        runtime_source = ("export MESOUQ_SITE_RUNTIME_ROOT=" + shlex.quote(runtime["site_runtime_root"]) + "\n"
                          if runtime.get("site_runtime_root") else "")
        if runtime.get("environment_script"):
            blob = self.slow(job["id"], transport.read, runtime["environment_script"])
            digest = hashlib.sha256(blob).hexdigest()
            if runtime.get("environment_sha256", digest) != digest:
                raise ValueError("Trusted runtime environment checksum mismatch")
            runtime["source_environment_script"] = runtime["environment_script"]
            runtime["environment_script"] = f"{run}/runtime-env.sh"
            runtime["environment_sha256"] = digest
            entry["runtime_environment_sha256"] = digest
            self.journal.save()
            self.slow(job["id"], transport.write, runtime["environment_script"], blob)
            self.slow(job["id"], transport.run, ["chmod", "400", runtime["environment_script"]])
            runtime_source += "source " + shlex.quote(runtime["environment_script"]) + "\n"
        if entry.get("adapter_sha256", adapter_sha256) != adapter_sha256:
            raise ValueError("Trusted adapter changed during immutable staging")
        entry["adapter_sha256"] = adapter_sha256
        self.journal.save()
        runtime["adapter_sha256"] = adapter_sha256
        self.slow(job["id"], transport.write, f"{run}/aws-mesouq-adapter.py", adapter)
        manifest = {"schema_version": 1, "job": job, "inputs": inputs, "model": model, "site": self.site, "repository_root": self.repository, "runtime": runtime}
        self.slow(job["id"], transport.write, f"{run}/manifest.json", json.dumps(manifest, allow_nan=False).encode())
        command = [runtime["python_bin"], f"{run}/aws-mesouq-adapter.py", "--manifest", f"{run}/manifest.json", "--output-dir", f"{run}/output"]
        directives = "".join(f"#SBATCH --{key}={value}\n" for key, value in sorted(self.scheduler.items()))
        allocation_guard = ('case "${SLURM_JOB_CPUS_PER_NODE:-}" in ""|*[!0-9]*) echo "Cannot verify single-node CPU allocation"; exit 2;; esac\n'
                            'if (( SLURM_JOB_CPUS_PER_NODE > 16 )); then echo "CPU allocation exceeds sixteen cores"; exit 2; fi\n'
                            'case "${SLURM_GPUS_ON_NODE:-0}" in *[!0-9]*) echo "Cannot verify GPU allocation"; exit 2;; esac\n'
                            'if (( ${SLURM_GPUS_ON_NODE:-0} > 1 )); then echo "GPU allocation exceeds one GPU"; exit 2; fi\n')
        script = ("#!/bin/bash\n#SBATCH --job-name=" + entry["marker"] + "\n" + directives + "#SBATCH --output=" + run + "/slurm.log\nset -euo pipefail\numask 077\n" + allocation_guard + "export MESOUQ_SITE=" + self.site + "\n" + runtime_source + "cd " + shlex.quote(self.repository) + "\nexport PYTHONPATH=" + shlex.quote(self.repository + "/src") + '${PYTHONPATH:+:"$PYTHONPATH"}\nexec ' + shlex.join(command) + "\n")
        self.slow(job["id"], transport.write, f"{run}/submit.sh", script.encode())
        self.slow(job["id"], transport.run, ["chmod", "400", f"{run}/manifest.json", f"{run}/submit.sh", f"{run}/aws-mesouq-adapter.py", *[item["path"] for item in inputs]])

    def reconcile(self, job, entry):
        self.valid_lease()
        records = self.slurm.lookup(entry["marker"])
        if len(records) != 1:
            self.event(job["id"], "heartbeat" if job["status"] in {"submission_unknown", "cancelling"} else "submission_unknown", detail={"reason": "Scheduler identity is absent or ambiguous; operator reconciliation required"})
            return
        job_id, state = records[0]
        if entry.get("slurm_job_id") and entry["slurm_job_id"] != job_id:
            raise RuntimeError("Scheduler identity changed")
        entry["slurm_job_id"] = job_id
        self.journal.save()
        cloud_status = job["status"]
        if cloud_status in {"submitting", "submission_unknown"}:
            cloud_status = self.event(job["id"], "submitted", slurm_job_id=job_id).get("status", "queued")
        if job["status"] == "cancelling":
            if state == "CANCELLED":
                self.event(job["id"], "cancelled", slurm_job_id=job_id)
                return
            if state in {"COMPLETED", "FAILED", "TIMEOUT", "OUT_OF_MEMORY", "NODE_FAIL", "BOOT_FAIL", "DEADLINE"}:
                self.event(job["id"], "failed", slurm_job_id=job_id, detail={"scheduler_state": state, "reason": "Cancellation arrived after terminal scheduler state"})
                return
            self.valid_lease()
            self.slurm.cancel(job_id, entry["marker"], authorize=self.valid_lease)
            self.event(job["id"], "heartbeat", slurm_job_id=job_id)
        elif state == "COMPLETED":
            self.publish(job, entry)
        elif state in {"CANCELLED", "PREEMPTED", "FAILED", "TIMEOUT", "OUT_OF_MEMORY", "NODE_FAIL", "BOOT_FAIL", "DEADLINE"}:
            self.event(job["id"], "cancelled" if state == "CANCELLED" else "failed", slurm_job_id=job_id, detail={"scheduler_state": state})
            entry["terminal"] = True
            self.journal.save()
        else:
            self.event(job["id"], "running" if state in {"RUNNING", "COMPLETING"} and cloud_status == "queued" else "heartbeat", slurm_job_id=job_id)

    def publish(self, job, entry):
        if entry.get("archive_binding"):
            return self.resume_archive(job, entry)
        transport = self.slurm.transport
        output = entry["run_dir"] + "/output"
        result = validate_result(json.loads(self.slow(job["id"], transport.read_beneath, output, "result.json")))
        if result.get("schema_version") != 1 or result.get("kind") != job["kind"]:
            raise ValueError("Invalid scientific result identity")
        runtime = result["provenance"]["runtime"]
        if set(runtime) - {"python", "numpy", "torch", "device", "site", "profile", "environment_sha256", "korali_module_sha256", "adapter_sha256"}:
            raise ValueError("Cloud runtime provenance must contain versions and hashes only")
        for key in ("python", "numpy", "torch", "device", "site"):
            if key in runtime and (not isinstance(runtime[key], str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.:+-]{0,127}", runtime[key])):
                raise ValueError("Cloud runtime version fields must not contain private paths or environment text")
        for key in ("environment_sha256", "korali_module_sha256", "adapter_sha256"):
            if runtime.get(key) is not None and (not isinstance(runtime[key], str) or not re.fullmatch(r"[a-f0-9]{64}", runtime[key])):
                raise ValueError("Cloud runtime fingerprints must be valid SHA-256 hashes")
        profile = runtime.get("profile", {})
        if not isinstance(profile, dict) or any(not key.endswith("_sha256") or not isinstance(value, str)
                or not re.fullmatch(r"[a-f0-9]{64}", value) for key, value in profile.items()):
            raise ValueError("Cloud runtime profile must contain only verified SHA-256 fingerprints")
        for adapter_digest in (runtime.get("adapter_sha256"), profile.get("adapter_sha256")):
            if adapter_digest is not None and adapter_digest != entry.get("adapter_sha256"):
                raise ValueError("Published runtime adapter fingerprint differs from private execution snapshot")
        names = result.get("publication_files")
        if not isinstance(names, list) or not names or len(names) > 4096:
            raise ValueError("Scientific result requires an explicit publication_files list")
        for name in names:
            safe_relative(name)
        if len(names) != len(set(names)) or not {"result.json", "provenance.json"}.issubset(names):
            raise ValueError("Publication files must be unique and include result/provenance JSON")
        if any(PurePosixPath(name).name in {"manifest.json", "runtime-env.sh", "aws-mesouq-adapter.py", "submit.sh"} or name.endswith(".sh") for name in names):
            raise ValueError("Publication file conflicts with trusted connector snapshot")
        self.slow(job["id"], transport.check_files, output, names)
        if json.loads(self.slow(job["id"], transport.read_beneath, output, "provenance.json")) != result["provenance"]:
            raise ValueError("Published provenance file differs from sanitized result provenance")
        sources = {name: (output, name) for name in names}
        # Operator environment code and the full execution manifest can contain
        # credentials/private account paths. They remain only in the HPC archive.
        refs = []
        receipts = []
        archive_files = []
        uploaded = entry.setdefault("uploaded", {})
        for name, source in sources.items():
            if any(not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]*", part) or part in {".", ".."}
                   for part in name.split("/")):
                raise ValueError("Unsafe scientific output name")
            blob = self.slow(job["id"], transport.read_beneath, *source)
            digest = hashlib.sha256(blob).hexdigest()
            if name not in uploaded:
                basename = PurePosixPath(name).name
                if basename in {"manifest.json", "provenance.json", "runtime-env.sh"}:
                    kind = "manifest"
                elif name.endswith((".pkl", ".pt", ".safetensors")):
                    kind = "model"
                elif basename.startswith("validation"):
                    kind = "validation"
                elif basename == "posterior.npz":
                    kind = "posterior"
                elif basename.endswith(".log"):
                    kind = "log"
                else:
                    kind = "result"
                metadata = {"name": name.replace("/", "__"), "kind": kind,
                            "sha256": digest, "size_bytes": len(blob),
                            "media_type": mimetypes.guess_type(name)[0] or "application/octet-stream"}
                response = self.slow(job["id"], self.api.request, "POST", f"/v1/projects/{self.project}/artifacts", metadata)
                self.slow(job["id"], self.api.transfer, response["upload_url"], data=blob, headers=response.get("required_headers", {}))
                artifact = response["artifact"]
                self.slow(job["id"], self.api.request, "POST", f"/v1/projects/{self.project}/artifacts/{safe_id(artifact['id'])}/complete", {})
                uploaded[name] = artifact
                self.journal.save()
            artifact = uploaded[name]
            if artifact["sha256"] != digest or artifact["size_bytes"] != len(blob):
                raise ValueError("Scientific output changed after upload")
            refs.append(artifact)
            receipts.append({"id": artifact["id"], "sha256": digest, "size_bytes": len(blob)})
            archive_files.append({"source_root": entry["run_dir"], "source": "output/" + name,
                                  "destination": "output/" + name, "sha256": digest, "size_bytes": len(blob)})
        result["artifacts"] = refs
        for index, artifact in enumerate(job.get("input_artifacts", [])):
            blob = self.slow(job["id"], transport.read, entry["run_dir"] + f"/input-{index}")
            if hashlib.sha256(blob).hexdigest() != artifact["sha256"] or len(blob) != artifact["size_bytes"]:
                raise ValueError("Archived input changed")
            receipts.append({"id": artifact["id"], "sha256": artifact["sha256"], "size_bytes": len(blob)})
            archive_files.append({"source_root": entry["run_dir"], "source": f"input-{index}",
                                  "destination": f"input-{index}", "sha256": artifact["sha256"], "size_bytes": len(blob)})
        archive = None
        if self.archive_capable:
            archive = self.archive(job, entry, receipts, archive_files)
        if not entry.get("succeeded_sent"):
            self.event(job["id"], "succeeded", slurm_job_id=entry["slurm_job_id"], result=result)
        if archive and not entry.get("archive_verified_sent"):
            self.event(job["id"], "archive_verified", archive=archive)
        entry["terminal"] = True
        self.journal.save()

    def archive(self, job, entry, receipts, files):
        """Copy bound bytes to PROJECT and journal progress before a receipt exists."""
        transport = self.slurm.transport
        # These private snapshots never enter the cloud publication list.
        private_names = ["manifest.json", "submit.sh", "aws-mesouq-adapter.py"]
        if entry.get("runtime_environment_sha256"):
            private_names.append("runtime-env.sh")
        for name in private_names:
            blob = self.slow(job["id"], transport.read_beneath, entry["run_dir"], name)
            digest = hashlib.sha256(blob).hexdigest()
            if name == "aws-mesouq-adapter.py" and digest != entry.get("adapter_sha256"):
                raise ValueError("Private adapter snapshot checksum changed")
            if name == "runtime-env.sh" and digest != entry.get("runtime_environment_sha256"):
                raise ValueError("Private runtime environment snapshot checksum changed")
            files.append({"source_root": entry["run_dir"], "source": name, "destination": "execution/" + name,
                          "sha256": digest, "size_bytes": len(blob)})
        token = PurePosixPath(entry["run_dir"]).name
        path = self.archive_root + "/" + token
        by_id = {}
        for row in receipts:
            if row["id"] in by_id and by_id[row["id"]] != row:
                raise ValueError("Conflicting input/output artifact identity in archive receipt")
            by_id[row["id"]] = row
        binding = {"path": path, "files": files, "artifacts": list(by_id.values())}
        if entry.get("archive_binding", binding) != binding:
            raise ValueError("Immutable durable archive content binding changed")
        entry["archive_binding"] = binding
        entry.setdefault("archive_copied", [])
        self.journal.save()
        return self.complete_archive(job, entry)

    def complete_archive(self, job, entry):
        """Finish or revalidate a journaled archive without reading copied sources."""
        transport = self.slurm.transport
        binding = entry["archive_binding"]
        token = PurePosixPath(entry["run_dir"]).name
        path = self.archive_root + "/" + token
        if not self.archive_capable or binding["path"] != path:
            raise ValueError("Durable archive binding differs from configured PROJECT path")
        self.slow(job["id"], transport.archive_prepare, self.archive_root, token)
        # Revalidate every file, including prior copies: journal success alone is
        # insufficient proof after reconnect or storage damage.
        for item in binding["files"]:
            entry["archive_copy_pending"] = item["destination"]
            self.journal.save()
            self.slow(job["id"], transport.archive_copy, self.archive_root, token, item)
            if item["destination"] not in entry["archive_copied"]:
                entry["archive_copied"].append(item["destination"])
            entry.pop("archive_copy_pending", None)
            self.journal.save()
        receipt = {"site": "karolina", "path": path, "artifacts": binding["artifacts"],
                   "verified_at": entry.get("archive_receipt", {}).get("verified_at", datetime.now(timezone.utc).isoformat())}
        if entry.get("archive_receipt", receipt) != receipt:
            raise ValueError("Immutable archive receipt changed")
        entry["archive_receipt"] = receipt
        self.journal.save()
        return receipt

    def resume_archive(self, job, entry):
        """Recover every post-copy stage entirely from bound PROJECT bytes."""
        archive = self.complete_archive(job, entry)
        output = archive["path"] + "/output"
        blob = self.slow(job["id"], self.slurm.transport.read_beneath, output, "result.json")
        bound_result = next((item for item in entry["archive_binding"]["files"] if item["destination"] == "output/result.json"), None)
        if not bound_result or hashlib.sha256(blob).hexdigest() != bound_result["sha256"] or len(blob) != bound_result["size_bytes"]:
            raise ValueError("Archived scientific result differs from immutable content binding")
        result = validate_result(json.loads(blob))
        if result["kind"] != job["kind"] or result["provenance"]["job_id"] != job["id"]:
            raise ValueError("Archived scientific result identity differs from claimed job")
        names = result["publication_files"]
        bound_outputs = {item["destination"][len("output/"):] for item in entry["archive_binding"]["files"] if item["destination"].startswith("output/")}
        if set(names) != bound_outputs or set(entry["uploaded"]) != bound_outputs:
            raise ValueError("Archived publication files differ from uploaded artifact bindings")
        result["artifacts"] = [entry["uploaded"][name] for name in names]
        if not entry.get("succeeded_sent"):
            self.event(job["id"], "succeeded", slurm_job_id=entry["slurm_job_id"], result=result)
        if not entry.get("archive_verified_sent"):
            self.event(job["id"], "archive_verified", archive=archive)
        entry["terminal"] = True
        self.journal.save()

    def sweep(self) -> list[str]:
        """Process at most ten distinct owner jobs sequentially before polling."""
        visited = []
        for _ in range(10):
            if not self.tick(exclude_job_ids=visited):
                break
            if self.last_claimed_job_id is None:
                raise RuntimeError("Processed claim lacks a verified job identity")
            visited.append(self.last_claimed_job_id)
        return visited

    def recover_committed_archive(self, excluded):
        """Acknowledge only an already accepted signed receipt without a lease.

        Accepted archived jobs are deliberately absent from claim results. A
        placeholder token can only reach the API's exact prior-event idempotency
        branch; it grants no authority to create an event or mutate Slurm.
        """
        candidates = [(job_id, entry) for job_id, entry in self.journal.data["jobs"].items()
                      if job_id not in excluded and entry.get("pending_event", {}).get("type") == "archive_verified"]
        for job_id, entry in candidates[:10]:
            safe_id(job_id)
            payload = entry["pending_event"]
            if (not self.archive_capable or payload.get("project_id") != self.project
                    or payload.get("connector_id") != self.connector_id
                    or payload.get("archive") != entry.get("archive_receipt")
                    or payload.get("archive_signature") != sign_archive(self.journal, job_id, self.journal.data["owner_sub"], payload)):
                raise ValueError("Pending archive recovery lacks its original signed device/content binding")
            current = self.api.request("GET", f"/v1/projects/{self.project}/jobs/{job_id}")
            if current.get("id") != job_id or current.get("project_id") != self.project or current.get("owner_sub") != self.journal.data["owner_sub"]:
                raise ValueError("Committed archive recovery job identity mismatch")
            if not current.get("archive"):
                # An uncommitted receipt still needs the normal claimed lease.
                continue
            def core(receipt):
                return {"site": receipt["site"], "path": receipt["path"],
                        "artifacts": sorted(receipt["artifacts"], key=lambda row: row["id"])}
            if (current.get("status") != "succeeded" or current.get("site") != "karolina"
                    or current["archive"].get("verified_by") != self.journal.data["owner_sub"]
                    or core(current["archive"]) != core(payload["archive"])):
                raise ValueError("Accepted archive differs from the exact pending receipt")
            binding = entry.get("archive_binding", {})
            token = PurePosixPath(binding.get("path", "")).name
            if (not token or binding.get("path") != self.archive_root + "/" + token
                    or binding.get("path") != payload["archive"]["path"]
                    or binding.get("artifacts") != payload["archive"]["artifacts"]):
                raise ValueError("Committed archive lacks its immutable durable content binding")
            for item in binding["files"]:
                self.slurm.transport.archive_verify(self.archive_root, token, item)
            acknowledged = self.api.request("POST", f"/v1/connector/jobs/{job_id}/events",
                                            {**payload, "lease_token": "idempotent-archive-replay-only"})
            if (acknowledged.get("id") != job_id or acknowledged.get("project_id") != self.project
                    or acknowledged.get("owner_sub") != self.journal.data["owner_sub"]
                    or acknowledged.get("status") != "succeeded" or not acknowledged.get("archive")
                    or core(acknowledged["archive"]) != core(payload["archive"])):
                raise ValueError("Archive event replay did not acknowledge the exact accepted receipt")
            entry.pop("pending_event")
            entry["archive_verified_sent"] = True
            entry["terminal"] = True
            self.journal.save()
            self.last_claimed_job_id = job_id
            return True
        return False

    def tick(self, *, exclude_job_ids=None):
        exclusions = [] if exclude_job_ids is None else list(exclude_job_ids)
        if len(exclusions) > 10 or any(safe_id(value) != value for value in exclusions) or len(set(exclusions)) != len(exclusions):
            raise ValueError("Claim exclusions must contain at most ten distinct valid job IDs")
        self.lease = None
        self.last_claimed_job_id = None
        try:
            return self._tick(exclusions if exclude_job_ids is not None else None)
        except StaleEvent:
            return True

    def _tick(self, exclude_job_ids=None):
        me = self.api.request("GET", "/v1/me")
        if self.journal.data.get("owner_sub", me["subject"]) != me["subject"]:
            raise ValueError("Journal belongs to another cloud identity")
        self.journal.data["owner_sub"] = me["subject"]
        self.journal.save()
        if self.recover_committed_archive(set(exclude_job_ids or [])):
            return True
        payload = {"project_id": self.project, "connector_id": self.connector_id, "site": self.site,
                   "archive_capable": self.archive_capable}
        if exclude_job_ids is not None:
            payload["exclude_job_ids"] = exclude_job_ids
        claim = self.api.request("POST", "/v1/connector/claim", payload)
        job = claim.get("job")
        if job is None:
            return False
        job_id = safe_id(job["id"])
        if job_id in (exclude_job_ids or []):
            raise RuntimeError("Claim server ignored job exclusions; stopping sweep")
        if job["project_id"] != self.project:
            raise ValueError("Claim project mismatch")
        if job["owner_sub"] != me["subject"]:
            raise ValueError("Claim owner mismatch")
        self.lease = claim
        self.last_claimed_job_id = job_id
        self.valid_lease()
        entries = self.journal.data["jobs"]
        if job_id not in entries:
            token = uuid.uuid4().hex
            # Marker contains no cloud-supplied shell/path characters.
            entries[job_id] = {"marker": "mesouq-" + token, "run_dir": str(PurePosixPath(self.run_root) / token),
                               "phase": "new" if job["status"] in {"claimed", "pending"} else "submission_attempted"}
            self.journal.save()
        entry = entries[job_id]
        if entry.get("pending_event"):
            self.event(job_id, entry["pending_event"]["type"])
        if entry.get("succeeded_sent") and entry.get("archive_verified_sent"):
            entry["terminal"] = True
            self.journal.save()
        if entry.get("archive_binding") and not entry.get("terminal") and job["status"] not in {"cancelling", "failed", "cancelled"}:
            self.resume_archive(job, entry)
            return True
        if entry.get("terminal"):
            if job["status"] == "succeeded" and self.archive_capable and not entry.get("archive_verified_sent"):
                self.publish(job, entry)
            return True
        if entry.get("succeeded_sent"):
            self.publish(job, entry)
            return True
        if job["status"] == "cancelling" and entry["phase"] in {"new", "staging", "staged"}:
            self.event(job_id, "cancelled")
            entry["terminal"] = True
            self.journal.save()
            return True
        if entry["phase"] == "staging":
            self.event(job_id, "failed", detail={"reason": "Interrupted immutable staging; create a new job after inspection"})
            entry["terminal"] = True
            self.journal.save()
            return True
        if entry["phase"] == "new":
            entry["phase"] = "staging"
            self.journal.save()
            try:
                self.stage(job, entry)
            except StaleEvent:
                raise
            except Exception as exc:
                if entry.get("pending_event"):
                    # Renewal may have committed remotely before disconnect.
                    # Preserve its identity; restart replays it, then the
                    # interrupted-staging branch explicitly fails the job.
                    raise
                self.event(job_id, "failed", detail={"code": "model_unavailable" if isinstance(exc, ModelUnavailable) else "staging_failed",
                                                     "reason": "Approved model requires trusted operator installation" if isinstance(exc, ModelUnavailable) else "Input/model staging failed; inspect connector journal"})
                entry["terminal"] = True
                self.journal.save()
                raise
            entry["phase"] = "staged"
            self.journal.save()
        if entry["phase"] == "staged" and job["status"] in {"submitting", "submission_unknown", "queued", "running"}:
            entry["phase"] = "submission_attempted"
            self.journal.save()
        if entry["phase"] == "staged":
            # A staged job can resume in a later session after operator storage
            # policy changes; check both roots again immediately before sbatch.
            self.preflight_storage(job_id)
            self.renew_before(job_id)
            state = self.event(job_id, "submitting")
            if state.get("status") == "cancelling":
                self.event(job_id, "cancelled")
                entry["terminal"] = True
                self.journal.save()
                return True
            self.renew_before(job_id)
            # Write-ahead before sbatch. A crash at any later point ONLY reconciles.
            entry["phase"] = "submission_attempted"
            self.journal.save()
            try:
                entry["slurm_job_id"] = self.slurm.submit(entry["run_dir"] + "/submit.sh")
                self.journal.save()
            except Exception:
                if entry.get("pending_event"):
                    raise
                self.event(job_id, "submission_unknown", detail={"reason": "Submission acknowledgement unavailable; reconciliation required"})
                return True
            self.event(job_id, "submitted", slurm_job_id=entry["slurm_job_id"])
        else:
            self.reconcile(job, entry)
        return True
