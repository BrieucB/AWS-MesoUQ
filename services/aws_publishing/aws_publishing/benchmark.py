"""Private, transactional Benchmark Lab control plane. Scientific work stays on Karolina."""
from __future__ import annotations

import base64
import copy
import hashlib
import re
import secrets
import uuid

from aws_mesouq.benchmark import (
    SCHEMA, FAMILIES, TERMINAL, canonical, checksum, claim_signing_bytes,
    stage_signing_bytes, stage_plan, token, validate_artifacts, validate_create, capabilities,
    validate_stage_result, keys, number, archive_signing_bytes,
)
from .api import fail, require_keys

ACTIVE = frozenset(("claimed", "submitting", "submission_unknown", "submitted", "queued", "running", "cancelling"))
TRANSITIONS = {
    "submitting": {"claimed"},
    "submitted": {"submitting", "submission_unknown", "cancelling"},
    "queued": {"submitted", "queued"},
    "running": {"submitted", "queued", "running"},
    "submission_unknown": {"submitting", "submitted", "queued", "running", "cancelling"},
    "failed": ACTIVE,
    "cancelled": ACTIVE,
    "succeeded": {"submitted", "queued", "running", "cancelling"},
}
SCHEDULER = re.compile(r"[1-9][0-9]{0,19}")


def is_benchmark_path(path):
    return path == "/v1/benchmark-scenarios" or path.startswith("/v1/benchmark-connector/") or bool(re.match(r"/v1/projects/[^/]+/benchmarks(?:/|$)", path))


def public_campaign(campaign):
    result = {k: copy.deepcopy(v) for k, v in campaign.items() if k not in ("request_hash", "artifacts")}
    for stage in result["stages"]:
        stage.pop("lease", None)
    result["artifacts"] = [{k: v for k, v in ref.items() if k != "storage_key"} for ref in campaign.get("artifacts", {}).values()]
    return result


def stage_costs(campaign):
    def total(kinds):
        timings = [s.get("result", {}).get("timing", s.get("timing", {"wall_seconds": 0, "cpu_seconds": 0, "gpu_seconds": 0} if s["status"] == "blocked" or s["status"] == "cancelled" and not s.get("slurm_job_id") else None)) for s in campaign["stages"] if s["kind"] in kinds]
        # Missing accounting is unknown, never reported as free computation.
        return {name: sum(t[name] for t in timings) if timings and all(t is not None and name in t for t in timings) else None for name in ("wall_seconds", "cpu_seconds", "gpu_seconds")}
    training = total({"hnpe_train"})
    inference = total({"hnpe_infer"})
    cases = next(s for s in campaign["stages"] if s["id"] == "cases")
    preparation = cases.get("result", {}).get("timing", cases.get("timing"))
    combined = {name: training[name] + inference[name] + preparation[name] if training[name] is not None and inference[name] is not None and preparation is not None and name in preparation else None for name in training}
    return {
        "primary_basis": "total_method_cost_including_training",
        "methods": [
            {"method": "hnpe", "preparation_seconds": preparation.get("wall_seconds") if preparation else None, "training_seconds": training["wall_seconds"], "inference_seconds": inference["wall_seconds"], "total_seconds": combined["wall_seconds"], "resource_cost": combined},
            {"method": "tmcmc", "training_seconds": 0, "inference_seconds": total({"tmcmc"})["wall_seconds"], "total_seconds": total({"tmcmc"})["wall_seconds"], "resource_cost": total({"tmcmc"})},
        ],
        "reference_seconds": total({"reference"})["wall_seconds"],
        "reference_resource_cost": total({"reference"}),
        "preparation_accounting": "entire_cases_allocation_conservatively_charged_to_hnpe",
        "shared_case_generation": cases.get("result", {}).get("metrics", {}).get("common_case_generation_costs"),
        "wall_seconds_semantics": "sum_of_stage_elapsed_seconds_not_campaign_makespan",
        "missing_accounting": "unknown_never_zero",
        "currency_cost": None,
    }


class BenchmarkRoutes:
    def __init__(self, app):
        self.app = app

    def owned(self, state, identity, sub, project, ident):
        self.app.member(state, identity, project)
        campaign = state["benchmark_campaigns"].get(ident)
        if not campaign or campaign["project_id"] != project or campaign["owner_sub"] != sub:
            fail(404, "not_found", "Benchmark campaign not found")
        return campaign

    def signature(self, identity, connector, signature, message):
        key = identity.get("connector_public_keys", {}).get(connector)
        try:
            from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
            from cryptography.exceptions import InvalidSignature
            if not isinstance(key, str) or not isinstance(signature, str):
                raise ValueError()
            public = base64.b64decode(key, validate=True)
            signed = base64.b64decode(signature, validate=True)
            if len(public) != 32 or len(signed) != 64:
                raise ValueError()
            Ed25519PublicKey.from_public_bytes(public).verify(signed, message)
        except (ValueError, TypeError, InvalidSignature):
            fail(403, "invalid_signature", "An operator-enrolled benchmark connector signature is required")

    def lease(self, stage, body, now):
        lease = stage.get("lease", {})
        if not isinstance(body.get("lease_token"), str) or lease.get("connector_id") != body.get("connector_id") or lease.get("expires_at", 0) <= now or not secrets.compare_digest(lease.get("hash", ""), hashlib.sha256(body["lease_token"].encode()).hexdigest()):
            fail(403, "invalid_lease", "Benchmark stage lease is absent, expired or belongs to another connector")

    def refresh(self, campaign, now):
        stages = {s["id"]: s for s in campaign["stages"]}
        changed = True
        while changed:
            changed = False
            for stage in stages.values():
                if stage["status"] == "pending" and stage.get("dependency_policy") != "terminal" and any(stages[d]["status"] in ("failed", "cancelled", "blocked") for d in stage["depends_on"]):
                    stage.update(status="blocked", failure={"code": "dependency_failed", "message": "Required stage did not succeed"}, updated_at=now)
                    changed = True
        if campaign.get("cancel_requested_at") is not None:
            campaign["status"] = "cancelled" if all(s["status"] in TERMINAL for s in stages.values()) else "cancelling"
        elif stages["metrics"]["status"] == "succeeded":
            campaign["status"] = "completed"
        elif stages["metrics"]["status"] in ("failed", "blocked"):
            campaign["status"] = "failed"
        elif any(s["status"] != "pending" for s in stages.values()):
            campaign["status"] = "running"
        campaign["updated_at"] = now

    def route(self, method, path, sub, identity, body, state):
        now = int(self.app.clock())
        try:
            if path == "/v1/benchmark-scenarios" and method == "GET":
                return 200, capabilities()
            if path == "/v1/benchmark-connector/archive" and method == "POST":
                return self.archive_verified(state, sub, identity, body, now)
            if path == "/v1/benchmark-connector/claim" and method == "POST":
                return self.claim(state, sub, identity, body, now)
            event = re.fullmatch(r"/v1/benchmark-connector/campaigns/([^/]+)/stages/([^/]+)/(events|artifacts)", path)
            if event and method == "POST":
                return self.event(state, sub, identity, *event.groups(), body, now)
            capability = re.fullmatch(r"/v1/projects/([^/]+)/benchmarks/capabilities", path)
            if capability and method == "GET":
                self.app.member(state, identity, capability[1])
                return 200, capabilities()
            match = re.fullmatch(r"/v1/projects/([^/]+)/benchmarks(?:/([^/]+))?(?:/(cancel|results|artifacts|archive)(?:/([^/]+))?)?", path)
            if not match:
                fail(404, "not_found", "Benchmark route not found")
            project, ident, action, artifact_id = match.groups()
            self.app.member(state, identity, project)
            if ident is None:
                if method == "POST":
                    return self.create(state, sub, project, body, now)
                if method == "GET":
                    return 200, {"campaigns": [public_campaign(c) for c in state["benchmark_campaigns"].values() if c["project_id"] == project and c["owner_sub"] == sub]}
            campaign = self.owned(state, identity, sub, project, ident)
            if method == "GET" and action is None:
                return 200, {"campaign": public_campaign(campaign)}
            if method == "POST" and action == "cancel":
                require_keys(body, ())
                if campaign["status"] not in ("completed", "failed", "cancelled"):
                    campaign.setdefault("cancel_requested_at", now)
                    for stage in campaign["stages"]:
                        if stage["status"] == "pending":
                            stage["status"] = "cancelled"
                        elif stage["status"] in ACTIVE:
                            stage["status"] = "cancelling"
                    self.refresh(campaign, now)
                return 200, {"campaign": public_campaign(campaign)}
            if method == "POST" and action == "archive":
                require_keys(body, ())
                if campaign["status"] not in ("completed", "failed", "cancelled"):
                    fail(409, "not_ready", "Archive request requires a terminal benchmark campaign")
                if not campaign["approved_adapter"].get("archive_root"):
                    fail(409, "archive_not_enrolled", "An operator-approved durable Karolina archive root is required")
                campaign.setdefault("archive", {"status": "requested", "requested_at": now})
                return 202, {"campaign": public_campaign(campaign)}
            if method == "GET" and action == "results":
                if "result" not in campaign:
                    fail(409, "not_ready", "Benchmark comparison is not ready")
                return 200, {"result": campaign["result"]}
            if method == "GET" and action == "artifacts" and artifact_id:
                artifact = campaign.get("artifacts", {}).get(artifact_id)
                if not artifact or not artifact.get("verified"):
                    fail(404, "not_found", "Benchmark artifact not found")
                return 200, {"artifact": {k: v for k, v in artifact.items() if k != "storage_key"}, "download_url": self.app.objects.download_url(artifact["storage_key"])}
            fail(405, "method_not_allowed", "Benchmark method not allowed")
        except (ValueError, TypeError, KeyError) as exc:
            fail(400, "invalid_benchmark", str(exc) if isinstance(exc, ValueError) else "Invalid benchmark request")

    def create(self, state, sub, project, body, now):
        configuration = validate_create(body)
        request_id = checksum(["benchmark", project, sub, body["idempotency_key"]])
        previous = state["requests"].get(request_id)
        if previous:
            if previous["hash"] != checksum(configuration):
                fail(409, "idempotency_conflict", "Benchmark idempotency key was used with different content")
            return 202, {"campaign": public_campaign(state["benchmark_campaigns"][previous["campaign_id"]])}
        policy = state["projects"][project].get("benchmark_policy", {})
        if not policy:
            fail(403, "benchmark_not_enrolled", "Project requires an operator-approved benchmark adapter")
        keys(policy, ("adapter_sha256", "science_revision", "scenario_families"), ("max_concurrent_stages", "archive_root"))
        if not re.fullmatch("[0-9a-f]{64}", policy["adapter_sha256"]) or not re.fullmatch("[0-9a-f]{40}", policy["science_revision"]) or body["scenario"]["family"] not in policy["scenario_families"]:
            fail(403, "benchmark_not_enrolled", "Project requires an operator-approved benchmark adapter and scenario family")
        ident = uuid.uuid4().hex
        campaign = {
            "schema": SCHEMA, "id": ident, "project_id": project, "owner_sub": sub,
            "configuration": configuration, "configuration_hash": checksum(configuration),
            "approved_adapter": copy.deepcopy(policy), "status": "pending",
            "created_at": now, "updated_at": now, "revision": 1,
            "stages": stage_plan(), "artifacts": {},
            "budget_ledger": {method: {"limit_gpu_seconds": configuration["budget"]["method_gpu_seconds"], "settled_gpu_seconds": 0, "reservations": {}} for method in ("hnpe", "tmcmc")},
        }
        state["benchmark_campaigns"][ident] = campaign
        state["requests"][request_id] = {"hash": checksum(configuration), "campaign_id": ident}
        return 202, {"campaign": public_campaign(campaign)}

    def claim(self, state, sub, identity, body, now):
        keys(body, ("project_id", "connector_id", "site", "request_id", "signature"), ("campaign_id", "exclude_stage_ids"))
        self.app.member(state, identity, body["project_id"])
        token(body["connector_id"])
        token(body["request_id"])
        if body["site"] != "karolina":
            fail(400, "invalid_site", "Benchmark scientific execution is restricted to Karolina")
        exclusions = body.get("exclude_stage_ids", [])
        if not isinstance(exclusions, list) or len(exclusions) > 64 or any(not isinstance(s, str) for s in exclusions):
            raise ValueError("Invalid stage exclusions")
        self.signature(identity, body["connector_id"], body["signature"], claim_signing_bytes(sub, body))
        request_id = checksum(["benchmark_claim", sub, body["connector_id"], body["request_id"]])
        previous = state["requests"].get(request_id)
        if previous:
            if previous["hash"] != checksum(body):
                fail(409, "idempotency_conflict", "Claim request ID already has different content")
            if previous.get("expires_at", 0) <= now:
                fail(409, "claim_expired", "Use a fresh claim request ID to reconcile expired work")
            return 200, previous["response"]
        campaigns = sorted(state["benchmark_campaigns"].values(), key=lambda c: c["created_at"])
        for campaign in campaigns:
            if campaign["project_id"] != body["project_id"] or campaign["owner_sub"] != sub or body.get("campaign_id", campaign["id"]) != campaign["id"] or campaign["status"] in ("completed", "failed", "cancelled"):
                continue
            stages = {s["id"]: s for s in campaign["stages"]}
            self.refresh(campaign, now)
            # Expired leases are reconciled before admitting new work.
            for stage in campaign["stages"]:
                if stage["id"] in exclusions or stage["status"] in TERMINAL:
                    continue
                lease = stage.get("lease", {})
                recovering = stage["status"] in ACTIVE
                if recovering:
                    if lease.get("expires_at", 0) > now:
                        continue
                else:
                    if campaign.get("cancel_requested_at") is not None:
                        continue
                    dependencies = [stages[d]["status"] for d in stage["depends_on"]]
                    if not all(s in TERMINAL if stage.get("dependency_policy") == "terminal" else s == "succeeded" for s in dependencies):
                        continue
                    concurrency = 64
                    concurrency = min(concurrency, campaign["approved_adapter"].get("max_concurrent_stages", 64))
                    if sum(s["status"] in ACTIVE for s in campaign["stages"]) >= concurrency:
                        continue
                method_group = "hnpe" if stage["kind"] in ("cases", "hnpe_train", "hnpe_infer") else "tmcmc" if stage["kind"] == "tmcmc" else None
                if method_group and not recovering:
                    ledger = campaign["budget_ledger"][method_group]
                    remaining = ledger["limit_gpu_seconds"] - ledger["settled_gpu_seconds"] - sum(r["gpu_seconds"] for r in ledger["reservations"].values())
                    if remaining < 1 and ledger["reservations"] and not ledger.get("cost_exceeded"):
                        continue
                    if remaining < 1 or ledger.get("cost_exceeded"):
                        stage.update(status="failed", failure={"code": "budget_exhausted", "message": "The fixed total method GPU budget is exhausted"}, timing={"wall_seconds": 0, "cpu_seconds": 0, "gpu_seconds": 0}, updated_at=now)
                        self.refresh(campaign, now)
                        continue
                    requested = campaign["configuration"]["controls"]["hnpe"]["training_budget"] if stage["kind"] == "hnpe_train" else 300 if stage["kind"] == "cases" else 1800 if stage["kind"] == "hnpe_infer" else remaining
                    reserved = min(requested, remaining)
                    ledger["reservations"][stage["id"]] = {"gpu_seconds": reserved, "allocated_gpus": 1}
                    stage["reserved_seconds"] = reserved
                    stage["method_group"] = method_group
                lease_token = secrets.token_urlsafe(32)
                if stage["status"] == "pending":
                    stage["status"] = "claimed"
                    stage["attempt"] += 1
                elif stage["status"] == "submitting":
                    stage["status"] = "submission_unknown"
                stage["lease"] = {"connector_id": body["connector_id"], "hash": hashlib.sha256(lease_token.encode()).hexdigest(), "expires_at": now + self.app.lease_seconds}
                stage["revision"] += 1
                stage["updated_at"] = now
                campaign["revision"] += 1
                self.refresh(campaign, now)
                response = {"campaign": public_campaign(campaign), "stage": {k: copy.deepcopy(v) for k, v in stage.items() if k != "lease"}, "lease_token": lease_token, "lease_expires_at": stage["lease"]["expires_at"], "reconcile_required": recovering}
                state["requests"][request_id] = {"hash": checksum(body), "response": response, "expires_at": stage["lease"]["expires_at"]}
                return 200, response
        return 200, {"campaign": None, "stage": None}

    def event(self, state, sub, identity, campaign_id, stage_id, action, body, now):
        keys(body, ("project_id", "connector_id", "lease_token", "event_id", "type", "signature"), ("slurm_job_id", "result", "failure", "timing", "artifacts", "scheduler_accounting", "not_submitted"))
        campaign = self.owned(state, identity, sub, body["project_id"], campaign_id)
        stage = next((s for s in campaign["stages"] if s["id"] == stage_id), None)
        if stage is None:
            fail(404, "not_found", "Benchmark stage not found")
        token(body["event_id"])
        token(body["connector_id"])
        self.signature(identity, body["connector_id"], body["signature"], stage_signing_bytes(campaign, stage, body))
        event_id = checksum(["benchmark_event", campaign_id, stage_id, body["event_id"]])
        event_hash = checksum({k: v for k, v in body.items() if k != "lease_token"})
        previous = state["events"].get(event_id)
        if previous:
            if previous["hash"] != event_hash:
                fail(409, "event_conflict", "Benchmark event ID already has different content")
            if action == "artifacts":
                return self.upload_response(campaign, previous["artifact_ids"])
            return 200, {"campaign": public_campaign(campaign)}
        self.lease(stage, body, now)
        if action == "artifacts":
            if body["type"] != "artifact_upload" or stage["status"] not in ACTIVE:
                raise ValueError("An active stage must request its own artifacts")
            refs = validate_artifacts(body.get("artifacts"))
            if len(campaign["artifacts"]) + len(refs) > 100 or sum(r["size_bytes"] for r in campaign["artifacts"].values()) + sum(r["size_bytes"] for r in refs) > 100000000:
                fail(413, "artifact_quota_exceeded", "Benchmark private result artifact quota exceeded")
            artifact_ids = []
            for ref in refs:
                ident = uuid.uuid4().hex
                campaign["artifacts"][ident] = {**copy.deepcopy(ref), "id": ident, "stage_id": stage_id, "owner_sub": sub, "project_id": campaign["project_id"], "verified": False, "storage_key": f"projects/{campaign['project_id']}/benchmarks/{campaign_id}/{stage_id}/{ident}"}
                artifact_ids.append(ident)
            state["events"][event_id] = {"hash": event_hash, "artifact_ids": artifact_ids}
            return self.upload_response(campaign, artifact_ids)
        kind = body["type"]
        if kind == "heartbeat":
            if stage["status"] in TERMINAL:
                fail(409, "invalid_transition", "Terminal benchmark stages do not accept heartbeats")
        elif kind in TRANSITIONS:
            if stage["status"] not in TRANSITIONS[kind]:
                fail(409, "invalid_transition", "Invalid benchmark stage transition")
            scheduler_id = body.get("slurm_job_id", stage.get("slurm_job_id"))
            if kind in ("submitted", "queued", "running", "succeeded") and (not isinstance(scheduler_id, str) or not SCHEDULER.fullmatch(scheduler_id)):
                raise ValueError("A native Slurm job ID is required")
            if "slurm_job_id" in body:
                if not isinstance(body["slurm_job_id"], str) or not SCHEDULER.fullmatch(body["slurm_job_id"]):
                    raise ValueError("Invalid Slurm job ID")
                if stage.get("slurm_job_id") not in (None, body["slurm_job_id"]):
                    fail(409, "scheduler_conflict", "Benchmark Slurm identity is immutable")
                stage["slurm_job_id"] = body["slurm_job_id"]
            if kind == "submitting":
                stage["submission_started"] = True
            if kind in ("succeeded", "failed", "cancelled") and stage.get("method_group"):
                self.settle_budget(campaign, stage, body, kind)
            if kind == "succeeded":
                result = validate_stage_result(body.get("result"), campaign, stage)
                for ref in result["artifacts"]:
                    artifact = campaign["artifacts"].get(ref.get("id"))
                    if not artifact or artifact["stage_id"] != stage_id or any(artifact[k] != ref[k] for k in ("name", "sha256", "size_bytes", "media_type")) or not self.app.objects.verify(artifact["storage_key"], artifact):
                        raise ValueError("Stage output must match a verified private uploaded artifact")
                    artifact["verified"] = True
                stage["result"] = copy.deepcopy(result)
                if stage_id == "cases":
                    campaign["case_set_sha256"] = result["provenance"]["case_set_sha256"]
                if stage_id == "metrics":
                    summary = copy.deepcopy(result.get("summary", {}))
                    if not isinstance(summary, dict):
                        raise ValueError("Benchmark comparison summary must be an object")
                    summary["costs"] = stage_costs(campaign)
                    summary["budget_ledger"] = copy.deepcopy(campaign["budget_ledger"])
                    summary["provenance"] = copy.deepcopy(result["provenance"])
                    summary["failures"] = [{"stage": s["id"], **s["failure"]} for s in campaign["stages"] if "failure" in s]
                    campaign["result"] = summary
            if kind == "failed":
                failure = body.get("failure")
                keys(failure, ("code", "message"))
                token(failure["code"])
                if not isinstance(failure["message"], str) or not 1 <= len(failure["message"]) <= 1000:
                    raise ValueError("A bounded stage failure message is required")
                stage["failure"] = copy.deepcopy(failure)
            if "timing" in body:
                keys(body["timing"], ("wall_seconds", "cpu_seconds", "gpu_seconds"))
                for name, value in body["timing"].items():
                    number(value, name, 0, 1e12)
                stage["timing"] = copy.deepcopy(body["timing"])
            stage["status"] = kind
            if kind in TERMINAL and stage.get("cost_exceeded"):
                stage["status"] = "failed"
                stage["failure"] = {"code": "budget_overrun", "message": "Actual Slurm allocated GPU time exceeded the approved stage reservation"}
        else:
            raise ValueError("Unsupported benchmark event")
        stage["lease"]["expires_at"] = now + self.app.lease_seconds
        stage["revision"] += 1
        stage["updated_at"] = now
        campaign["revision"] += 1
        self.refresh(campaign, now)
        state["events"][event_id] = {"hash": event_hash}
        return 200, {"campaign": public_campaign(campaign), "lease_expires_at": stage["lease"]["expires_at"]}

    def archive_verified(self, state, sub, identity, body, now):
        from pathlib import PurePosixPath
        keys(body, ("project_id", "campaign_id", "connector_id", "event_id", "type", "receipt", "signature"))
        if body["type"] != "archive_verified":
            raise ValueError("Expected an archive verification event")
        campaign = self.owned(state, identity, sub, body["project_id"], body["campaign_id"])
        if campaign["status"] not in ("completed", "failed", "cancelled") or campaign.get("archive", {}).get("status") not in ("requested", "verified"):
            fail(409, "archive_not_requested", "A terminal campaign archive must first be requested by its owner")
        token(body["event_id"])
        token(body["connector_id"])
        enrolled_for_campaign = {s.get("lease", {}).get("connector_id") for s in campaign["stages"] if s.get("submission_started")}
        if body["connector_id"] not in enrolled_for_campaign:
            fail(403, "invalid_connector", "Archive signer must be an enrolled connector used by this campaign")
        self.signature(identity, body["connector_id"], body["signature"], archive_signing_bytes(campaign, body))
        receipt = body["receipt"]
        keys(receipt, ("site", "path", "artifacts", "retained_outputs", "manifest_sha256"))
        root_text = campaign["approved_adapter"].get("archive_root")
        path_text = receipt["path"]
        if not isinstance(root_text, str) or not isinstance(path_text, str) or receipt["site"] != "karolina":
            raise ValueError("Archive requires the operator-approved durable Karolina root")
        root, path = PurePosixPath(root_text), PurePosixPath(path_text)
        if not root.is_absolute() or not path.is_absolute() or str(path) != path_text or ".." in path.parts or root not in path.parents or any(ord(c) < 32 for c in path_text):
            raise ValueError("Archive path is outside the operator-approved durable root")
        if not isinstance(receipt["manifest_sha256"], str) or not re.fullmatch("[a-f0-9]{64}", receipt["manifest_sha256"]):
            raise ValueError("Archive requires a verified immutable manifest SHA256")
        expected = {ref["id"]: ref for ref in campaign["artifacts"].values()}
        refs = receipt["artifacts"]
        if not isinstance(refs, list) or len(refs) != len(expected) or {r.get("id") for r in refs if isinstance(r, dict)} != set(expected):
            raise ValueError("Archive receipt must include every retained cloud artifact exactly once")
        for ref in refs:
            keys(ref, ("id", "sha256", "size_bytes"))
            artifact = expected[ref["id"]]
            if not artifact.get("verified") or ref["sha256"] != artifact["sha256"] or ref["size_bytes"] != artifact["size_bytes"] or not self.app.objects.verify(artifact["storage_key"], artifact):
                raise ValueError("Archive artifact differs from verified private cloud bytes")
        outputs = receipt["retained_outputs"]
        if not isinstance(outputs, list) or not outputs or len(outputs) > 2000:
            raise ValueError("Archive requires retained scientific output evidence")
        seen = set()
        for output in outputs:
            keys(output, ("path", "sha256", "size_bytes"))
            relative = output["path"]
            if not isinstance(relative, str) or PurePosixPath(relative).is_absolute() or ".." in PurePosixPath(relative).parts or str(PurePosixPath(relative)) != relative or relative in seen or any(ord(c) < 32 for c in relative):
                raise ValueError("Invalid retained output path")
            seen.add(relative)
            if not isinstance(output["sha256"], str) or not re.fullmatch("[a-f0-9]{64}", output["sha256"]):
                raise ValueError("Invalid retained output SHA256")
            number(output["size_bytes"], "retained output size", 0, 1e15, True)
        accepted = campaign.get("archive", {})
        if accepted.get("status") == "verified":
            if checksum(accepted["receipt"]) != checksum(receipt):
                fail(409, "archive_conflict", "Accepted archive receipts are immutable")
        else:
            campaign["archive"] = {**accepted, "status": "verified", "verified_at": self.app.clock(), "connector_id": body["connector_id"], "receipt": copy.deepcopy(receipt), "cloud_retention": {"automatic_expiry": False, "mode": "retain_until_benchmark_maintenance_supported"}}
        return 200, {"campaign": public_campaign(campaign)}

    def settle_budget(self, campaign, stage, body, kind):
        ledger = campaign["budget_ledger"][stage["method_group"]]
        reservation = ledger["reservations"].get(stage["id"])
        if not reservation:
            fail(409, "budget_reservation_missing", "Stage has no active method reservation")
        accounting = body.get("scheduler_accounting")
        if body.get("not_submitted") is True and not stage.get("submission_started") and not stage.get("slurm_job_id") and kind in ("failed", "cancelled"):
            actual = 0
            stage["timing"] = {"wall_seconds": 0, "cpu_seconds": 0, "gpu_seconds": 0}
        else:
            keys(accounting, ("source", "site", "slurm_job_id", "state", "elapsed_seconds", "allocated_gpus", "cpu_seconds"))
            if accounting["source"] != "sacct" or accounting["site"] != "karolina" or accounting["slurm_job_id"] != stage.get("slurm_job_id") or accounting["state"] not in ("COMPLETED", "FAILED", "CANCELLED", "TIMEOUT", "OUT_OF_MEMORY", "NODE_FAIL", "PREEMPTED", "BOOT_FAIL", "DEADLINE"):
                raise ValueError("Method reservation requires matching native terminal Slurm accounting")
            if kind == "succeeded" and accounting["state"] != "COMPLETED":
                raise ValueError("Successful stage requires COMPLETED Slurm accounting")
            number(accounting["elapsed_seconds"], "allocated elapsed seconds", 0, 10000000)
            number(accounting["cpu_seconds"], "allocated CPU seconds", 0, 1e12)
            if type(accounting["allocated_gpus"]) is not int or accounting["allocated_gpus"] != reservation["allocated_gpus"]:
                raise ValueError("Slurm GPU count does not match the approved reservation")
            actual = accounting["elapsed_seconds"] * accounting["allocated_gpus"]
            if actual > reservation["gpu_seconds"]:
                stage["cost_exceeded"] = True
                ledger["cost_exceeded"] = True
            timing = body.get("result", {}).get("timing") if kind == "succeeded" else body.get("timing")
            if not isinstance(timing, dict) or timing.get("gpu_seconds") != actual or timing.get("wall_seconds") != accounting["elapsed_seconds"] or timing.get("cpu_seconds") != accounting["cpu_seconds"]:
                raise ValueError("Stage timing must match allocated native Slurm accounting")
            stage["scheduler_accounting"] = copy.deepcopy(accounting)
        ledger["settled_gpu_seconds"] += actual
        del ledger["reservations"][stage["id"]]
        stage["settled_gpu_seconds"] = actual
        if ledger["settled_gpu_seconds"] + sum(r["gpu_seconds"] for r in ledger["reservations"].values()) > ledger["limit_gpu_seconds"]:
            ledger["cost_exceeded"] = True

    def upload_response(self, campaign, artifact_ids):
        uploads = []
        for ident in artifact_ids:
            artifact = campaign["artifacts"][ident]
            url, headers = self.app.objects.upload_url(artifact["storage_key"], artifact)
            uploads.append({"artifact": {k: v for k, v in artifact.items() if k != "storage_key"}, "upload_url": url, "required_headers": headers})
        return 200, {"uploads": uploads}
