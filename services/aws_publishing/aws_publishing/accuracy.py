"""Authenticated metadata controller for Karolina accuracy-target campaigns."""
from __future__ import annotations

import base64
from copy import deepcopy
import os
import json
import re
import secrets
from uuid import uuid4

from aws_mesouq import benchmark_accuracy as wire
from aws_mesouq import benchmark_accuracy_protocol as protocol
from .api import Application, APIError
from .benchmark import BenchmarkRoutes


def is_accuracy_path(path):
    return bool(re.match(r"^/v1/projects/[^/]+/benchmark-accuracy(?:/|$)", path)
                or path.startswith("/v1/benchmark-connector/accuracy/"))


def is_accuracy_event_request(method, path):
    identifier = r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}"
    return method == "POST" and re.fullmatch(
        rf"/v1/benchmark-connector/accuracy/campaigns/{identifier}/tasks/{identifier}/events",
        path,
    ) is not None


def request_wire_limit(method, path):
    """Raw JSON cap for only the exact signed accuracy task-event route."""
    return protocol.EVENT_WIRE_BYTES if is_accuracy_event_request(method, path) else 200_000


def policies_from_environment():
    try:
        value = json.loads(os.environ.get("BENCHMARK_ACCURACY_POLICIES", "{}"))
    except (ValueError, TypeError):
        raise ValueError("Invalid accuracy policy configuration") from None
    if not isinstance(value, dict):
        raise ValueError("Invalid accuracy policy configuration")
    return value


def _error(status, code, message):
    raise APIError(status, code, message)


def _key(project, owner, campaign):
    return f"{project}:{owner}:{campaign}"


def _cursor(value):
    if value is None:
        return None
    if not isinstance(value, str) or len(value) > 2048:
        raise ValueError("Invalid history cursor")
    try:
        return base64.urlsafe_b64decode(value + "=" * (-len(value) % 4)).decode()
    except Exception as exc:
        raise ValueError("Invalid history cursor") from exc


def _encoded(value):
    return None if value is None else base64.urlsafe_b64encode(value.encode()).decode().rstrip("=")


def defaults():
    return {
        "schema_version": 2, "scenario": {"family": "baseline", "scale": 1.0},
        "seed": 1000,
        "controls": {
            "hnpe": {"width": 128, "depth": 2, "learning_rate": 0.0003,
                     "batch_size": 256, "max_updates": 1000000, "training_budget": 1800},
            "tmcmc": {"population_size": 4096, "target_cov": 0.6,
                      "proposal_scale": 1.0, "burn_in": 7, "max_generations": 1000}},
        "evaluation": {"cohort": "central_90", "guide_case_count": 8,
                       "confirmation_case_count": 256, "posterior_draws": 2048},
        "budget": {"mode": "accuracy_target", "method_gpu_seconds": 14400},
        "accuracy_target": {"profile_id": wire.PROFILE_ID,
            "effort_policy_id": wire.EFFORT_POLICY_ID,
            "reference_profile_id": wire.REFERENCE_PROFILE_ID}}


def _account():
    return {"settled_gpu_seconds": 0, "settled_cpu_seconds": 0,
            "reserved_gpu_seconds": 0, "unknown_allocations": 0}


class AccuracyApplication(Application):
    """Use the existing authentication, revision-CAS and notification boundary."""
    def __init__(self, *args, accuracy_policies=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.accuracy_policies = {}
        if accuracy_policies is not None and not isinstance(accuracy_policies, dict):
            raise ValueError("Invalid accuracy policy registry")
        for project, contexts in (accuracy_policies or {}).items():
            protocol.identifier(project)
            if not isinstance(contexts, list) or len(contexts) > 16:
                raise ValueError("Invalid accuracy context list")
            seen = set()
            trusted = []
            for context in contexts:
                value = protocol.validate_context(context)
                if value["project_id"] != project or value["id"] in seen:
                    raise ValueError("Accuracy context project/identity mismatch")
                seen.add(value["id"])
                trusted.append(value)
            self.accuracy_policies[project] = sorted(trusted, key=lambda v: v["id"])
        self.accuracy = AccuracyRoutes(self)

    def finalize_request(self, method, path, state, *, remaining_ms=None):
        if method == "GET" and is_accuracy_path(path):
            return 0
        return super().finalize_request(method, path, state, remaining_ms=remaining_ms)

    def check_request_size(self, method, path, body):
        if is_accuracy_event_request(method, path):
            if len(protocol.canonical(body)) > protocol.EVENT_REQUEST_BYTES:
                _error(413, "request_too_large", "Accuracy event exceeds metadata size limit")
            return
        return super().check_request_size(method, path, body)

    def route(self, method, path, sub, identity, body, state):
        if not is_accuracy_path(path):
            return super().route(method, path, sub, identity, body, state)
        try:
            response = self.accuracy.route(method, path, sub, identity, body, state)
            if len(protocol.canonical(response[1])) > protocol.HTTP_RESPONSE_BYTES:
                _error(413, "accuracy_response_too_large", "The complete metadata response exceeds its bounded transport envelope")
            return response
        except ValueError as exc:
            _error(422, "invalid_accuracy_request", str(exc))


class AccuracyRoutes:
    def __init__(self, app):
        self.app = app
        self.signatures = BenchmarkRoutes(app)

    def _contexts(self, project, sub, enabled=True):
        return [c for c in self.app.accuracy_policies.get(project, [])
                if c["owner_sub"] == sub and (not enabled or c["enabled"])]

    def _context(self, campaign):
        for context in self.app.accuracy_policies.get(campaign["project_id"], []):
            if context["id"] == campaign["execution_context_id"] and context["owner_sub"] == campaign["owner_sub"]:
                return context
        _error(409, "accuracy_context_unavailable", "The immutable execution context is unavailable")

    @staticmethod
    def _record(state, bucket, key, *, mutable=False):
        """Install mutable point reads into the request's explicit CAS write set.

        LazyRecords intentionally does not cache reads. Ordinary metadata reads
        stay sparse; only the exact records selected for mutation are retained.
        Repeated mutable reads return the same request-owned object.
        """
        value = state[bucket].get(key)
        if mutable and value is not None:
            state[bucket][key] = value
        return value

    def _owned(self, state, identity, sub, project, ident, *, mutable=False):
        protocol.identifier(project)
        protocol.identifier(ident)
        self.app.member(state, identity, project)
        campaign = self._record(state, "accuracy_campaigns", _key(project, sub, ident), mutable=mutable)
        if campaign is None or campaign["owner_sub"] != sub:
            _error(404, "accuracy_campaign_not_found", "Calculation not found")
        return campaign

    def _records(self, state, bucket, prefix, limit=100, after=None, *, reverse=False):
        if after is not None and not after.startswith(prefix):
            raise ValueError("History cursor belongs to another campaign")
        records, next_key = self.app.store.records(bucket, prefix, limit=limit, after=after, reverse=reverse)
        values = dict(records)
        for key, value in state[bucket].items():
            if key.startswith(prefix) and (after is None or (key < after if reverse else key > after)):
                values[key] = value
        selected = sorted(values, reverse=reverse)[:limit]
        for key in selected:
            state[bucket].get(key)
        if len(values) > limit:
            next_key = selected[-1]
        return [(key, values[key]) for key in selected], next_key

    def _rows(self, state, campaign):
        prefix = campaign["id"] + ":"
        rows, after = {}, None
        while True:
            batch, after = self._records(state, "accuracy_allocations", prefix, 100, after)
            for key, value in batch:
                rows[key] = value
            if after is None:
                break
            if len(rows) > 10000:
                _error(409, "accuracy_history_limit", "Native history exceeds the bounded snapshot protocol")
        for key, value in state["accuracy_allocations"].items():
            if key.startswith(prefix):
                rows[key] = value
        return [deepcopy(v["row"]) for _, v in sorted(rows.items())]

    def _snapshot(self, state, campaign):
        return {
            "schema": protocol.SNAPSHOT_SCHEMA, "campaign_instance_id": campaign["id"],
            "scientific_request_hash": campaign["scientific_request_hash"],
            "transport_request_hash": campaign["configuration_hash"],
            "source_binding_sha256": campaign["source_binding_sha256"],
            "case_plan_sha256": campaign["pins"]["case_plan_sha256"],
            "guide_case_set_sha256": campaign["pins"]["guide_case_set_sha256"],
            "revision": campaign["ledger_revision"],
            "allocations": self._rows(state, campaign)}

    def _public_account(self, campaign):
        result = {"methods": {}}
        for name, account in campaign["accounting"].items():
            value = {
                "allocated_gpu_seconds": None if account["unknown_allocations"] else account["settled_gpu_seconds"],
                "cpu_seconds": None if account["unknown_allocations"] else account["settled_cpu_seconds"],
                "settled_gpu_seconds": account["settled_gpu_seconds"],
                "settled_cpu_seconds": account["settled_cpu_seconds"],
                "reserved_gpu_seconds": account["reserved_gpu_seconds"],
                "unknown_allocations": account["unknown_allocations"]}
            if name in ("hnpe", "tmcmc"):
                value["limit_gpu_seconds"] = campaign["request"]["budget"]["method_gpu_seconds"]
                value["budget_exceeded"] = account["settled_gpu_seconds"] > value["limit_gpu_seconds"]
                result["methods"][name] = value
            else:
                result[name] = value
        return result

    def _archive_available(self, state, campaign):
        if campaign["status"] not in ("completed", "failed", "cancelled", "blocked"):
            return False, "Campaign is not terminal"
        if any(a["unknown_allocations"] or a["reserved_gpu_seconds"] for a in campaign["accounting"].values()):
            return False, "Native costs or reservations remain unresolved"
        if campaign["active_tasks"]:
            return False, "Task publication or recovery is still active"
        if not campaign["native_job_count"]:
            return False, "No native execution to archive"
        for ident in campaign["artifacts"]:
            artifact = state["artifacts"].get(ident)
            if artifact and not artifact.get("verified") and not artifact.get("reservation_released"):
                return False, "Private publication reservations remain unsettled"
        return True, None

    def _verified_artifacts(self, state, campaign):
        refs = []
        for aid in campaign["artifacts"]:
            artifact = state["artifacts"].get(aid)
            if artifact is None:
                continue
            if (artifact.get("owner_sub") != campaign["owner_sub"]
                    or artifact.get("project_id") != campaign["project_id"]
                    or artifact.get("campaign_id") != campaign["id"]):
                raise ValueError("Private artifact ownership/campaign binding changed")
            if artifact.get("verified") is True and not artifact.get("reservation_released"):
                refs.append(self.public_artifact(artifact))
        if len(refs) > self._context(campaign)["maximum_artifact_files"]:
            raise ValueError("Verified private artifact list exceeds its curated envelope")
        return refs

    def public(self, state, campaign, *, detail=False):
        available, reason = self._archive_available(state, campaign)
        result = {name: deepcopy(campaign[name]) for name in (
            "id", "schema_version", "configuration_hash", "scientific_request_hash",
            "execution_context_id", "created_at", "updated_at", "status",
            "scientific_status", "methods", "result")}
        result["configuration"] = wire.scientific_request(campaign["request"])
        result["accounting"] = self._public_account(campaign)
        result["max_artifact_bytes"] = self._context(campaign)["maximum_artifact_bytes"]
        result["budget_exceeded"] = bool(campaign.get("budget_exceeded", False))
        result["blocked_reason"] = deepcopy(campaign.get("blocked_reason"))
        result["method_seal_sha256"] = campaign["pins"]["method_seal_sha256"]
        result["archive"] = {**deepcopy(campaign["archive"]), "available": available, "reason": reason}
        if detail:
            result["verified_artifacts"] = self._verified_artifacts(state, campaign)
            rows, next_key = self._records(state, "accuracy_tasks", campaign["id"] + ":", 50, reverse=True)
            result["tasks"] = [self.public_task(task) for _, task in rows]
            result["next_task_cursor"] = _encoded(next_key)
        return result

    @staticmethod
    def public_task(task):
        result = {name: deepcopy(task.get(name)) for name in (
            "id", "kind", "method", "factor", "attempt", "status", "generation",
            "effort_target", "created_at", "updated_at", "failure_code", "progress")}
        return result

    def capabilities(self, state, identity, sub, project):
        self.app.member(state, identity, project)
        contexts = self._contexts(project, sub)
        enabled = len(contexts) == 1
        reason = None if enabled else ("No approved accuracy execution context is enabled" if not contexts else "Multiple enabled source contexts require an unambiguous operator policy")
        return 200, {"schema_version": 2, "enabled": enabled,
            "reason": reason,
            "approved_profile": defaults()["accuracy_target"], "defaults": defaults(),
            "max_artifact_bytes": max((c["maximum_artifact_bytes"] for c in contexts), default=0),
            "execution_contexts": [protocol.public_context(c) for c in contexts],
            "limits": {"scenario_families": ["baseline", "higher_noise", "broader_priors"],
                "scale": {"min": 1, "max": 3, "step": 0.05},
                "hnpe": {"width": [64, 128, 256], "depth": [1, 2, 3],
                    "learning_rate": [0.0001, 0.0003], "batch_size": [256, 512, 1024, 2048],
                    "max_updates": {"min": 1, "max": 1000000},
                    "training_budget": {"min": 120, "max": 14400}},
                "tmcmc": {"population_size": [2048, 4096, 8192], "target_cov": [0.4, 0.6, 0.8],
                    "proposal_scale": [0.25, 0.5, 1.0], "burn_in": [1, 7, 10],
                    "max_generations": {"min": 100, "max": 10000}},
                "method_gpu_seconds": {"min": 1, "max": 2147483647}}}

    def _window(self, campaign, kind):
        configured = self._context(campaign).get("stage_reservations", {}).get(kind)
        if configured is not None:
            return configured
        if kind in ("guide_reference", "confirmation_reference"):
            # Numerical-reference settings and native window are independent
            # of either method's experimental compute allowance.
            return 14400
        if kind in ("hnpe_initialize", "hnpe_advance", "guide_preparation"):
            return campaign["request"]["controls"]["hnpe"]["training_budget"]
        return 600

    def _new_task(self, state, campaign, kind, *, method=None, factor=None,
                  effort_target=None, generation=0, previous=None, inputs=None, maximum=None):
        if campaign["status"] in ("cancelling", "cancelled", "failed", "completed"):
            return None
        context = self._context(campaign)
        if not context["enabled"]:
            campaign["status"] = "blocked"
            return None
        owner, allocation_kind = protocol.KINDS[kind]
        amount = self._window(campaign, kind) if maximum is None else maximum
        protocol.integer(amount, "task reservation", 1)
        if owner in ("hnpe", "tmcmc"):
            account = campaign["accounting"][owner]
            if account["unknown_allocations"]:
                _error(409, "accuracy_method_allocation_active", "Reserve one source-selected native method action, then reconcile before replanning")
            remaining = campaign["request"]["budget"]["method_gpu_seconds"] - account["settled_gpu_seconds"] - account["reserved_gpu_seconds"]
            if factor is not None:
                factor_used = sum((r["allocated_gpu_seconds"] or 0) + r["reserved_gpu_seconds"]
                    for r in self._rows(state, campaign) if r["method"] == "hnpe" and r["factor"] == factor)
                remaining = min(remaining, campaign["request"]["controls"]["hnpe"]["training_budget"] - factor_used)
            amount = min(amount, remaining)
            if amount < 1 or (kind != "guide_preparation" and amount < 60):
                return None
        if kind == "guide_preparation" and amount < 60:
            campaign["status"] = "blocked_unfundable"
            campaign["blocked_reason"] = {"code": "initial_native_allocation_unfundable",
                "minimum_allocation_seconds": 60, "available_gpu_seconds": amount,
                "message": "The initial allocation cannot fund Karolina's required 60-second minimum; no calculation was started."}
            for info in campaign["methods"].values():
                info["status"] = "blocked_unfundable"
            return None
        logical = protocol.checksum({"campaign": campaign["id"], "kind": kind,
            "method": method, "factor": factor, "effort_target": effort_target,
            "generation": generation, "previous": previous, "inputs": inputs or {}})
        outbox_key = "accuracy_task:" + campaign["id"] + ":" + logical
        existing = state["requests"].get(outbox_key)
        if existing is not None:
            return state["accuracy_tasks"].get(campaign["id"] + ":" + existing["task_id"])
        campaign["task_count"] += 1
        ident = f"t{campaign['task_count']:08d}-{uuid4().hex}"
        allocation = f"a{campaign['task_count']:08d}-{uuid4().hex}"
        row = {"allocation_id": allocation, "attempt": 1, "method": owner,
            "kind": allocation_kind, "factor": factor, "status": "claimed",
            "slurm_job_id": None, "allocated_gpu_seconds": None,
            "allocated_cpu_seconds": None, "reserved_gpu_seconds": amount,
            "not_submitted": False}
        state["accuracy_allocations"][campaign["id"] + ":" + allocation] = {"row": row, "task_id": ident}
        campaign["ledger_revision"] += 1
        account = campaign["accounting"][owner]
        account["reserved_gpu_seconds"] += amount
        account["unknown_allocations"] += 1
        snapshot = self._snapshot(state, campaign)
        task = {"id": ident, "campaign_id": campaign["id"], "kind": kind,
            "method": method or owner, "factor": factor, "attempt": 1,
            "generation": generation, "effort_target": effort_target,
            "expected_state_sha256": previous, "allocation_id": allocation,
            "status": "ready", "revision": 0, "lease_epoch": 0, "lease": None,
            "created_at": self.app.clock(), "updated_at": self.app.clock(),
            "failure_code": None, "progress": None, "slurm_job_id": None,
            "marker": f"mqacc-{campaign['id']}-{ident}", "submission_intent": None,
            "result": None, "artifact_ids": [], "accounting_closed": False}
        manifest = {
            "schema": protocol.MANIFEST_SCHEMA, "campaign_instance_id": campaign["id"],
            "task_id": ident, "attempt": 1, "kind": kind, "method": task["method"],
            "factor": factor, "effort_target": effort_target, "generation": generation,
            "expected_state_sha256": previous, "request": deepcopy(campaign["request"]),
            "scientific_request_hash": campaign["scientific_request_hash"],
            "transport_request_hash": campaign["configuration_hash"],
            "context_id": campaign["execution_context_id"],
            "source_binding_sha256": campaign["source_binding_sha256"],
            "policy_sha256": context["policy_sha256"], "accounting": snapshot,
            "input_ledger_sha256": protocol.checksum(snapshot),
            "reservation": {"allocation_id": allocation, "gpu_seconds": amount,
                "native_cpus": context["native_cpus"], "native_gpus": context["native_gpus"]},
            "pins": {**deepcopy(campaign["pins"]), "candidate_sha256":
                campaign["methods"][task["method"]]["candidate_sha256"]
                if task["method"] in ("hnpe", "tmcmc") else None},
            "inputs": deepcopy(inputs or {})}
        protocol.validate_manifest(manifest)
        task["manifest"], task["manifest_sha256"] = manifest, protocol.checksum(manifest)
        state["accuracy_tasks"][campaign["id"] + ":" + ident] = task
        campaign["active_tasks"].append(ident)
        state["requests"][outbox_key] = {"task_id": ident, "manifest_sha256": task["manifest_sha256"]}
        state["events"]["accuracy_ready:" + ident] = {"task_id": ident, "manifest_sha256": task["manifest_sha256"]}
        return task

    def create(self, state, identity, sub, project, body):
        self.app.member(state, identity, project)
        request = wire.validate_create(body)
        contexts = self._contexts(project, sub)
        if not contexts:
            _error(409, "accuracy_disabled", "No approved accuracy execution context is enabled")
        transport = wire.transport_request_checksum(request)
        request_key = "accuracy_create:" + protocol.checksum([project, sub, request["idempotency_key"]])
        previous = state["requests"].get(request_key)
        if previous is not None:
            if previous["hash"] != transport:
                _error(409, "idempotency_conflict", "The transport nonce already binds a different request")
            return 202, deepcopy(previous["response"])
        if len(contexts) != 1:
            _error(409, "accuracy_context_ambiguous", "Operator policy must select exactly one current source context")
        context, ident, now = contexts[0], uuid4().hex, self.app.clock()
        campaign = {
            "id": ident, "schema_version": 2, "project_id": project, "owner_sub": sub,
            "request": request, "configuration_hash": transport,
            "scientific_request_hash": wire.scientific_request_checksum(request),
            "execution_context_id": context["id"],
            "source_binding_sha256": context["source_binding_sha256"],
            "created_at": now, "updated_at": now, "status": "pending",
            "scientific_status": None, "result": None, "revision": 0,
            "ledger_revision": 0, "task_count": 0, "active_tasks": [], "native_job_count": 0,
            "accounting": {name: _account() for name in ("hnpe", "tmcmc", "reference", "overhead")},
            "pins": {name: None for name in protocol.PINS}, "phase": "guide_preparation",
            "phase_receipts": {}, "progress": {}, "guide_history": {"hnpe": [], "tmcmc": []},
            "tmcmc_attempts": [], "artifacts": [], "method_wrappers": {},
            "methods": {name: {"status": "pending", "candidate_sha256": None,
                "effort_target": None, "guide_status": None} for name in ("hnpe", "tmcmc")},
            "archive": {"status": "not_requested"}, "confirmation_authorization": None}
        state["accuracy_campaigns"][_key(project, sub, ident)] = campaign
        if self._new_task(state, campaign, "guide_preparation") is None and campaign["status"] != "blocked_unfundable":
            campaign["status"] = "blocked"
        response = {"campaign": self.public(state, campaign, detail=True)}
        state["requests"][request_key] = {"hash": transport, "campaign_id": ident, "response": deepcopy(response)}
        return 202, response

    def route(self, method, path, sub, identity, body, state):
        now = self.app.clock()
        match = re.fullmatch(r"/v1/projects/([^/]+)/benchmark-accuracy(?:/(.*))?", path)
        if match:
            project, tail = match.group(1), match.group(2) or ""
            protocol.identifier(project)
            self.app.member(state, identity, project)
            if method == "GET" and tail == "capabilities":
                return self.capabilities(state, identity, sub, project)
            if tail == "campaigns":
                if method == "POST":
                    return self.create(state, identity, sub, project, body)
                if method == "GET":
                    protocol.keys(body, set(), {"archived", "limit", "cursor"})
                    archived = body.get("archived", "all")
                    if archived not in ("all", "true", "false"):
                        raise ValueError("Invalid archived filter")
                    limit = protocol.integer(body.get("limit", 50), "page limit", 1, 50)
                    rows, cursor = self._records(state, "accuracy_campaigns", project + ":" + sub + ":", limit, _cursor(body.get("cursor")))
                    campaigns, consumed = [], None
                    for key, value in rows:
                        if archived != "all" and (value["archive"]["status"] == "verified") != (archived == "true"):
                            consumed = key
                            continue
                        public = self.public(state, value)
                        candidate = {"campaigns": [*campaigns, public], "next_cursor": _encoded(key)}
                        if len(protocol.canonical(candidate)) > protocol.HTTP_RESPONSE_BYTES:
                            if not campaigns:
                                _error(413, "accuracy_response_too_large", "One complete campaign exceeds its bounded metadata envelope")
                            cursor = consumed
                            break
                        campaigns.append(public)
                        consumed = key
                    return 200, {"campaigns": campaigns, "next_cursor": _encoded(cursor)}
            detail = re.fullmatch(r"campaigns/([^/]+)(?:/(.*))?", tail)
            if detail:
                ident, action = detail.group(1), detail.group(2) or ""
                campaign = self._owned(state, identity, sub, project, ident)
                if method == "GET" and not action:
                    return 200, {"campaign": self.public(state, campaign, detail=True)}
                if method == "GET" and action == "tasks":
                    protocol.keys(body, set(), {"limit", "cursor"})
                    limit = protocol.integer(body.get("limit", 50), "page limit", 1, 100)
                    rows, cursor = self._records(state, "accuracy_tasks", ident + ":", limit, _cursor(body.get("cursor")), reverse=True)
                    return 200, {"tasks": [self.public_task(value) for _, value in rows], "next_cursor": _encoded(cursor)}
                task_match = re.fullmatch(r"tasks/([^/]+)", action)
                if method == "GET" and task_match:
                    protocol.keys(body, set())
                    task = self._task(state, campaign, protocol.identifier(task_match.group(1)))
                    return 200, {"campaign": self.public(state, campaign),
                        "task": {**self.public_task(task), **{name: deepcopy(task[name]) for name in
                            ("revision", "manifest_sha256", "allocation_id", "slurm_job_id")}}}
                if method == "POST" and action == "cancel":
                    protocol.keys(body, set())
                    self.cancel(state, campaign)
                    return 200, {"campaign": self.public(state, campaign, detail=True)}
                if method == "POST" and action == "archive":
                    protocol.keys(body, set())
                    available, reason = self._archive_available(state, campaign)
                    if campaign["archive"]["status"] != "verified":
                        if not available:
                            _error(409, "accuracy_archive_not_ready", reason)
                        state["accuracy_campaigns"][_key(project, sub, ident)] = campaign
                        campaign["archive"]["status"] = "requested"
                        campaign["updated_at"] = now
                    return 200, {"campaign": self.public(state, campaign, detail=True)}
                if method == "GET" and action == "accounting":
                    protocol.keys(body, set())
                    snapshot = self._snapshot(state, campaign)
                    protocol.validate_snapshot(snapshot, allow_unprepared=campaign["pins"]["case_plan_sha256"] is None)
                    return 200, {"snapshot": snapshot, "sha256": protocol.checksum(snapshot)}
                if method == "GET" and action == "results":
                    if campaign["result"] is None:
                        _error(409, "result_not_ready", "A source-validated result is not available")
                    raw_artifact = state["artifacts"].get(campaign["result"]["artifact_id"])
                    summary_artifact = state["artifacts"].get(campaign["result"]["summary_artifact_id"])
                    for artifact in (raw_artifact, summary_artifact):
                        if artifact is None or not artifact.get("verified") or not self.app.objects.verify(artifact["storage_key"], artifact):
                            _error(409, "result_not_ready", "The private source result/summary is not verified")
                    if summary_artifact["sha256"] != campaign["result"]["summary_sha256"]:
                        _error(409, "result_not_ready", "The immutable display summary changed")
                    result = self.app.objects.read_json(summary_artifact["storage_key"], summary_artifact,
                        maximum=protocol.DISPLAY_BYTES)
                    protocol.canonical(result)
                    protocol._no_raw_secret(result)
                    return 200, {"result": result,
                        "raw_result_artifact": self.public_artifact(raw_artifact), "artifacts": [
                        self.public_artifact(state["artifacts"].get(a)) for a in campaign["artifacts"]
                        if state["artifacts"].get(a, {}).get("verified")],
                        "accounting": self._public_account(campaign)}
                artifact_match = re.fullmatch(r"artifacts/([^/]+)", action)
                if method == "GET" and artifact_match:
                    aid = protocol.identifier(artifact_match.group(1))
                    artifact = state["artifacts"].get(aid)
                    if aid not in campaign["artifacts"] or artifact is None or artifact["owner_sub"] != sub or not artifact.get("verified"):
                        _error(404, "accuracy_artifact_not_found", "Artifact not found")
                    if not self.app.objects.verify(artifact["storage_key"], artifact):
                        _error(409, "accuracy_artifact_unavailable", "Artifact checksum or size is unavailable")
                    return 200, {"artifact": self.public_artifact(artifact),
                        "url": self.app.objects.download_url(artifact["storage_key"])}
        if method == "POST" and path == "/v1/benchmark-connector/accuracy/claim":
            return self.claim(state, identity, sub, body)
        event = re.fullmatch(r"/v1/benchmark-connector/accuracy/campaigns/([^/]+)/tasks/([^/]+)/events", path)
        if method == "POST" and event:
            if body.get("campaign_id") != event.group(1) or body.get("task_id") != event.group(2):
                raise ValueError("Task route/body mismatch")
            return self.event(state, identity, sub, body)
        archive = re.fullmatch(r"/v1/benchmark-connector/accuracy/campaigns/([^/]+)/archive", path)
        if method == "POST" and archive:
            if body.get("campaign_id") != archive.group(1):
                raise ValueError("Archive route/body mismatch")
            return self.archive_verified(state, identity, sub, body)
        _error(404, "accuracy_route_not_found", "Route not found")

    def _row(self, state, campaign, task):
        value = self._record(state, "accuracy_allocations", campaign["id"] + ":" + task["allocation_id"], mutable=True)
        if value is None or value["task_id"] != task["id"]:
            _error(409, "accuracy_ledger_mismatch", "Native allocation is missing")
        return value["row"]

    def _task(self, state, campaign, ident, *, mutable=False):
        task = self._record(state, "accuracy_tasks", campaign["id"] + ":" + ident, mutable=mutable)
        if task is None or task["campaign_id"] != campaign["id"]:
            _error(404, "accuracy_task_not_found", "Task not found")
        return task

    def _touch(self, campaign, task):
        task["updated_at"] = campaign["updated_at"] = self.app.clock()
        task["revision"] += 1
        campaign["revision"] += 1

    def _lease_expiry(self, state, campaign, task):
        lease = task["lease"]
        if lease and lease["expires_at"] <= self.app.clock() and task["status"] not in ("succeeded", "failed", "cancelled"):
            if task["status"] in ("claimed", "ready") and task["submission_intent"] is None:
                task["status"] = "ready"
            elif task["accounting_closed"]:
                task["status"] = "cancelling" if campaign["status"] == "cancelling" else "publishing"
            elif task["status"] != "publishing":
                task["status"] = "submission_unknown"
                self._row(state, campaign, task)["status"] = "submission_unknown"
                campaign["ledger_revision"] += 1
            task["lease"] = None
            self._touch(campaign, task)

    def _private_claim_task(self, task):
        result = self.public_task(task)
        result.update({name: deepcopy(task[name]) for name in (
            "revision", "manifest", "manifest_sha256", "marker", "allocation_id", "slurm_job_id")})
        result["lease"] = deepcopy(task["lease"])
        result["lease_epoch"] = task["lease_epoch"]
        result["reconcile_only"] = task["status"] in ("submitting", "submission_unknown", "submitted", "queued", "running", "cancelling", "publishing")
        return result

    def claim(self, state, identity, sub, body):
        value = protocol.validate_claim(body)
        self.app.member(state, identity, value["project_id"])
        self.signatures.signature(identity, value["connector_id"], value.get("signature"),
            protocol.claim_signing_bytes(sub, value))
        key = "accuracy_claim:" + protocol.checksum([sub, value["connector_id"], value["request_id"]])
        digest = protocol.checksum({k: v for k, v in value.items() if k != "signature"})
        previous = state["requests"].get(key)
        if previous:
            if previous["hash"] != digest:
                _error(409, "idempotency_conflict", "Claim request ID already binds another payload")
            return 200, deepcopy(previous["response"])
        if value.get("campaign_id") is not None:
            campaign_key = _key(value["project_id"], sub, value["campaign_id"])
            target = state["accuracy_campaigns"].get(campaign_key)
            rows = [] if target is None else [(campaign_key, target)]
        else:
            rows, cursor = self._records(state, "accuracy_campaigns", value["project_id"] + ":" + sub + ":", 100)
            # A generic claim scans bounded pages, never every task or historical allocation.
            while cursor is not None and len(rows) < 1000:
                batch, cursor = self._records(state, "accuracy_campaigns", value["project_id"] + ":" + sub + ":", 100, cursor)
                rows.extend(batch)
        response = {"campaign": None, "task": None}
        excluded = set(value.get("exclude_task_ids", []))
        for campaign_key, _ in rows:
            campaign = self._record(state, "accuracy_campaigns", campaign_key, mutable=True)
            if value.get("campaign_id") not in (None, campaign["id"]) or campaign["execution_context_id"] not in value["supported_contexts"]:
                continue
            context = self._context(campaign)
            for ident in list(campaign["active_tasks"]):
                if ident in excluded or value.get("task_id") not in (None, ident):
                    continue
                task = self._task(state, campaign, ident, mutable=True)
                self._lease_expiry(state, campaign, task)
                if task["lease"] is not None:
                    if value.get("campaign_id") == campaign["id"] and task["lease"]["connector_id"] == value["connector_id"]:
                        task["lease"]["expires_at"] = self.app.clock() + self.app.lease_seconds
                        self._touch(campaign, task)
                        response = {"campaign": self.public(state, campaign), "task": self._private_claim_task(task)}
                        break
                    continue
                if task["status"] in ("succeeded", "failed", "cancelled"):
                    continue
                if not context["enabled"] and task["status"] == "ready":
                    continue
                task["lease_epoch"] += 1
                task["lease"] = {"token": secrets.token_urlsafe(48),
                    "epoch": task["lease_epoch"], "connector_id": value["connector_id"],
                    "expires_at": self.app.clock() + self.app.lease_seconds}
                if task["status"] == "ready":
                    task["status"] = "claimed"
                self._touch(campaign, task)
                response = {"campaign": self.public(state, campaign),
                    "task": self._private_claim_task(task)}
                break
            if response["task"] is not None:
                break
        state["requests"][key] = {"hash": digest, "response": deepcopy(response)}
        return 200, response

    def _ack(self, state, campaign, task):
        value = self.public_task(task)
        value.update({"revision": task["revision"], "lease_epoch": task["lease_epoch"],
            "lease_expires_at": task["lease"]["expires_at"] if task["lease"] else None,
            "manifest_sha256": task["manifest_sha256"], "slurm_job_id": task["slurm_job_id"]})
        return {"campaign": self.public(state, campaign), "task": value}

    def _close(self, state, campaign, task, accounting):
        if task["accounting_closed"]:
            if task.get("scheduler_accounting") != accounting:
                _error(409, "accuracy_accounting_conflict", "Closed native allocation changed")
            return
        context = self._context(campaign)
        gpu, cpu = protocol.validate_accounting(accounting, job_id=task["slurm_job_id"],
            task_status=task["status"], expected_cpus=context["native_cpus"],
            allow_preallocation=campaign["status"] == "cancelling")
        row = self._row(state, campaign, task)
        if task["accounting_closed"]:
            if task["scheduler_accounting"] != accounting:
                _error(409, "accuracy_accounting_conflict", "Closed native allocation changed")
            return
        account = campaign["accounting"][row["method"]]
        account["reserved_gpu_seconds"] -= row["reserved_gpu_seconds"]
        account["unknown_allocations"] -= 1
        account["settled_gpu_seconds"] += gpu
        account["settled_cpu_seconds"] += cpu
        row.update({"allocated_gpu_seconds": gpu, "allocated_cpu_seconds": cpu,
                    "reserved_gpu_seconds": 0,
                    "status": "cancelled" if accounting["state"] == "CANCELLED" else
                              "succeeded" if accounting["state"] == "COMPLETED" else "failed"})
        task["scheduler_accounting"] = deepcopy(accounting)
        task["accounting_closed"] = True
        campaign["ledger_revision"] += 1
        if row["method"] in ("hnpe", "tmcmc") and account["settled_gpu_seconds"] > campaign["request"]["budget"]["method_gpu_seconds"]:
            campaign["budget_exceeded"] = True

    def _never_submitted(self, state, campaign, task, terminal):
        if task["submission_intent"] is not None or task["slurm_job_id"] is not None or task["status"] not in ("ready", "claimed"):
            _error(409, "accuracy_submission_ambiguous", "A submitted intent cannot be guessed as zero")
        row = self._row(state, campaign, task)
        if not task["accounting_closed"]:
            account = campaign["accounting"][row["method"]]
            account["reserved_gpu_seconds"] -= row["reserved_gpu_seconds"]
            account["unknown_allocations"] -= 1
            row.update({"status": terminal, "allocated_gpu_seconds": 0,
                "allocated_cpu_seconds": 0, "reserved_gpu_seconds": 0, "not_submitted": True})
            task["accounting_closed"] = True
            campaign["ledger_revision"] += 1
        task["status"] = terminal
        task["lease"] = None
        if task["id"] in campaign["active_tasks"]:
            campaign["active_tasks"].remove(task["id"])

    def cancel(self, state, campaign):
        if campaign["status"] in ("completed", "cancelled") or (campaign["status"] == "failed" and not campaign["active_tasks"]):
            return
        state["accuracy_campaigns"][_key(campaign["project_id"], campaign["owner_sub"], campaign["id"])] = campaign
        campaign["status"] = "cancelling"
        for ident in list(campaign["active_tasks"]):
            task = self._task(state, campaign, ident, mutable=True)
            if task["status"] in ("ready", "claimed") and task["submission_intent"] is None:
                self._never_submitted(state, campaign, task, "cancelled")
            else:
                task["status"] = "cancelling"
                if not task["accounting_closed"]:
                    self._row(state, campaign, task)["status"] = "cancelling"
                    campaign["ledger_revision"] += 1
            self._touch(campaign, task)
        if not campaign["active_tasks"]:
            campaign["status"] = campaign.get("terminal_after_cancel", "cancelled")
        campaign["updated_at"] = self.app.clock()

    @staticmethod
    def public_artifact(value):
        return {name: deepcopy(value[name]) for name in (
            "id", "name", "sha256", "size_bytes", "media_type", "kind") if name in value}

    def _uploads(self, state, campaign, task, artifacts):
        context = self._context(campaign)
        if not isinstance(artifacts, list) or not 1 <= len(artifacts) <= 100:
            raise ValueError("Bounded curated upload list required")
        allowed = set(context["allowed_artifacts"].get(task["kind"], []))
        seen, result = set(), []
        for spec in artifacts:
            protocol.keys(spec, {"name", "sha256", "size_bytes", "media_type", "kind"})
            name = protocol.identifier(spec["name"], "artifact name")
            if name not in allowed or name in seen:
                raise ValueError("Artifact is not in the reviewed curated publication policy")
            seen.add(name)
            protocol.sha(spec["sha256"])
            protocol.integer(spec["size_bytes"], "artifact bytes", 1, context["maximum_artifact_bytes"])
            extensions = {"application/json": ".json", "text/csv": ".csv", "image/png": ".png",
                "image/svg+xml": ".svg", "application/octet-stream": ".npz", "text/markdown": ".md"}
            if spec["media_type"] not in extensions or not name.endswith(extensions[spec["media_type"]]):
                raise ValueError("Curated artifact media type and extension must match")
            protocol.identifier(spec["kind"], "artifact kind")
            existing = None
            for aid in task["artifact_ids"]:
                prior = state["artifacts"].get(aid)
                if prior and prior["name"] == name:
                    if any(prior[k] != spec[k] for k in ("sha256", "size_bytes", "media_type", "kind")):
                        _error(409, "accuracy_artifact_conflict", "Immutable native artifact metadata changed")
                    if prior.get("upload_expired_at") and not prior.get("reservation_released"):
                        _error(409, "accuracy_upload_expired", "Expired upload awaits authoritative reservation settlement")
                    if not prior.get("reservation_released"):
                        existing = prior
            if existing is None:
                charged = sum(a["size_bytes"] for a in state["artifacts"].values()
                    if a.get("project_id") == campaign["project_id"] and not a.get("reservation_released"))
                campaign_artifacts = [a for a in state["artifacts"].values()
                    if a.get("campaign_id") == campaign["id"] and not a.get("reservation_released")]
                campaign_bytes = sum(a["size_bytes"] for a in campaign_artifacts)
                if (charged + spec["size_bytes"] > 1_073_741_824
                    or campaign_bytes + spec["size_bytes"] > context["maximum_total_artifact_bytes"]
                    or len(campaign_artifacts) >= context["maximum_artifact_files"]):
                    _error(409, "accuracy_storage_quota_exceeded", "Private publication capacity is reserved; existing artifacts are retained")
                aid = uuid4().hex
                existing = {**deepcopy(spec), "id": aid, "stage_id": task["id"],
                    "task_id": task["id"], "campaign_id": campaign["id"],
                    "owner_sub": campaign["owner_sub"], "project_id": campaign["project_id"],
                    "verified": False, "created_at": self.app.clock(), "reservation_released": False,
                    "storage_key": f"{campaign['project_id']}/{campaign['owner_sub']}/artifacts/accuracy/{campaign['id']}/{task['id']}/{aid}"}
                state["artifacts"][aid] = existing
                task["artifact_ids"].append(aid)
                campaign["artifacts"].append(aid)
            result.append({"artifact_id": existing["id"], "artifact": self.public_artifact(existing),
                "upload": self.app.objects.upload_url(existing["storage_key"], existing)})
        return result

    def event(self, state, identity, sub, body):
        value = protocol.validate_event(body)
        campaign = self._owned(state, identity, sub, value["project_id"], value["campaign_id"])
        task = self._task(state, campaign, value["task_id"])
        self.signatures.signature(identity, value["connector_id"], value["signature"],
            protocol.event_signing_bytes(sub, value))
        digest = protocol.checksum({k: v for k, v in value.items() if k != "signature"})
        event_key = "accuracy_event:" + protocol.checksum([sub, campaign["id"], task["id"], value["event_id"]])
        prior = state["events"].get(event_key)
        if prior:
            if prior["hash"] != digest:
                _error(409, "idempotency_conflict", "Event ID already binds a different payload")
            return 200, deepcopy(prior["response"])
        if value["attempt"] != task["attempt"] or value["manifest_sha256"] != task["manifest_sha256"]:
            _error(409, "accuracy_task_conflict", "Immutable task identity changed")
        lease = task["lease"]
        if lease is None or lease["connector_id"] != value["connector_id"] or lease["epoch"] != value["lease_epoch"] or lease["token"] != value["lease_token"] or lease["expires_at"] <= self.app.clock():
            _error(409, "invalid_lease", "Current fenced task lease required")
        if value["expected_task_revision"] != task["revision"]:
            _error(409, "accuracy_revision_conflict", "Task revision changed")
        kind, payload, status = value["type"], value["payload"], task["status"]
        if status in ("succeeded", "failed", "cancelled"):
            _error(409, "accuracy_task_terminal", "Terminal tasks cannot reopen")
        state["accuracy_campaigns"][_key(campaign["project_id"], sub, campaign["id"])] = campaign
        state["accuracy_tasks"][campaign["id"] + ":" + task["id"]] = task
        extras = {}
        if kind == "heartbeat":
            pass
        elif kind == "submitting":
            if status != "claimed" or task["submission_intent"] is not None or campaign["status"] == "cancelling":
                _error(409, "accuracy_submission_conflict", "Task cannot submit another native allocation")
            protocol.sha(payload["script_sha256"]); protocol.sha(payload["intent_sha256"])
            if payload["marker"] != task["marker"]:
                raise ValueError("Native owned marker changed")
            task["submission_intent"] = deepcopy(payload)
            task["status"] = "submitting"
            self._row(state, campaign, task)["status"] = "submitting"
        elif kind in ("submitted", "queued", "running"):
            if task["accounting_closed"]:
                _error(409, "accuracy_accounting_closed", "Closed native allocation cannot reopen")
            if status not in ("submitting", "submission_unknown", "submitted", "queued", "running", "cancelling"):
                _error(409, "accuracy_transition_conflict", "Native observation requires the durable submit intent")
            job = payload["slurm_job_id"]
            if not isinstance(job, str) or re.fullmatch(r"[1-9][0-9]{0,19}", job) is None:
                raise ValueError("Invalid native job ID")
            if task["slurm_job_id"] not in (None, job):
                _error(409, "accuracy_native_identity_conflict", "Native producer changed")
            binding = {"campaign_id": campaign["id"], "task_id": task["id"],
                       "allocation_id": task["allocation_id"]}
            native_key = "accuracy_native_job:karolina:" + self._context(campaign)["native_user"] + ":" + job
            bound = state["requests"].get(native_key)
            if bound is not None and bound != binding:
                _error(409, "accuracy_native_producer_reused", "One native allocation cannot fund multiple tasks")
            state["requests"][native_key] = binding
            if task["slurm_job_id"] is None:
                campaign["native_job_count"] += 1
            task["slurm_job_id"] = job
            self._row(state, campaign, task)["slurm_job_id"] = job
            task["status"] = "cancelling" if campaign["status"] == "cancelling" else kind
            self._row(state, campaign, task)["status"] = task["status"]
            if kind == "running":
                start = protocol.integer(payload["allocation_start_epoch"], "native start", 1)
                end = protocol.integer(payload["allocation_deadline_epoch"], "native end", start + 1)
                clock = {"job_id": job, "start": start, "end": end}
                if task.get("native_clock") not in (None, clock):
                    _error(409, "accuracy_native_clock_conflict", "Native allocation clock cannot change")
                task["native_clock"] = clock
            campaign["status"] = "cancelling" if campaign["status"] == "cancelling" else "running"
        elif kind == "submission_unknown":
            if task["accounting_closed"]:
                _error(409, "accuracy_accounting_closed", "Closed native allocation cannot become unknown")
            if task["submission_intent"] is None or payload["marker"] != task["marker"]:
                _error(409, "accuracy_submission_conflict", "Unknown submission must retain its existing owned intent")
            if not isinstance(payload["reason"], str) or len(payload["reason"]) > 256:
                raise ValueError("Invalid reconciliation reason")
            task["status"] = "cancelling" if campaign["status"] == "cancelling" else "submission_unknown"
            self._row(state, campaign, task)["status"] = task["status"]
        elif kind == "allocation_closed":
            if task["slurm_job_id"] is None:
                _error(409, "accuracy_submission_ambiguous", "Native job identity must be reconciled")
            self._close(state, campaign, task, payload["scheduler_accounting"])
            task["status"] = "publishing"
        elif kind == "artifact_upload":
            if not task["accounting_closed"] or campaign["archive"]["status"] != "not_requested" or campaign["status"] == "cancelling":
                _error(409, "accuracy_upload_not_ready", "Close native accounting before publication")
            extras["uploads"] = self._uploads(state, campaign, task, payload["artifacts"])
        elif kind == "artifact_status":
            ids = payload["artifact_ids"]
            if not isinstance(ids, list) or len(ids) > 100 or len(set(ids)) != len(ids) or any(a not in task["artifact_ids"] for a in ids):
                raise ValueError("Invalid artifact status selectors")
            checked = []
            for aid in ids:
                artifact = state["artifacts"].get(aid)
                if artifact.get("reservation_released") or artifact.get("upload_expired_at"):
                    checked.append({"id": aid, "verified": bool(artifact.get("verified")),
                        "reservation_released": bool(artifact.get("reservation_released")),
                        "upload_expired": bool(artifact.get("upload_expired_at"))})
                    continue
                verified = self.app.objects.verify(artifact["storage_key"], artifact)
                if verified:
                    artifact["verified"] = True
                    artifact["uploaded_at"] = self.app.clock()
                checked.append({"id": aid, "verified": bool(artifact.get("verified"))})
            extras["artifacts"] = checked
        elif kind == "result":
            if not task["accounting_closed"] or campaign["status"] == "cancelling":
                _error(409, "accuracy_result_not_ready", "Close native accounting and cancellation before scientific publication")
            self.complete(state, campaign, task, payload["result"])
        elif kind in ("failed", "cancelled"):
            if kind == "cancelled" and campaign["status"] != "cancelling":
                _error(409, "accuracy_cancel_not_requested", "Cancellation must be authoritative before native settlement")
            if type(payload["not_submitted"]) is not bool:
                raise ValueError("not_submitted must be boolean")
            if payload["not_submitted"]:
                self._never_submitted(state, campaign, task, "cancelled" if kind == "cancelled" else "failed")
            else:
                if "scheduler_accounting" in payload:
                    self._close(state, campaign, task, payload["scheduler_accounting"])
                if not task["accounting_closed"]:
                    _error(409, "accuracy_accounting_unknown", "Unknown allocated cost cannot be settled as zero")
                task["status"] = "cancelled" if kind == "cancelled" else "failed"
                task["lease"] = None
                if task["id"] in campaign["active_tasks"]:
                    campaign["active_tasks"].remove(task["id"])
            if kind == "failed":
                protocol.keys(payload["failure"], {"code", "message"})
                task["failure_code"] = protocol.identifier(payload["failure"]["code"])
                if not isinstance(payload["failure"]["message"], str) or len(payload["failure"]["message"]) > 512:
                    raise ValueError("Invalid failure description")
                task["failure"] = deepcopy(payload["failure"])
                campaign["status"] = "failed"
                if campaign["active_tasks"]:
                    campaign["terminal_after_cancel"] = "failed"
                    self.cancel(state, campaign)
            elif campaign["status"] == "cancelling" and not campaign["active_tasks"]:
                campaign["status"] = campaign.get("terminal_after_cancel", "cancelled")
        if kind in ("submitting", "submitted", "queued", "running", "submission_unknown"):
            campaign["ledger_revision"] += 1
        self._touch(campaign, task)
        if task["lease"] is not None:
            task["lease"]["expires_at"] = self.app.clock() + self.app.lease_seconds
        response = {**self._ack(state, campaign, task), **extras}
        state["events"][event_key] = {"hash": digest, "response": deepcopy(response)}
        return 200, response

    def _inputs(self, state, campaign, **extra):
        value = {"phase_receipts": deepcopy(campaign["phase_receipts"]),
            "progress": deepcopy(campaign["progress"]),
            "guide_history": deepcopy(campaign["guide_history"]),
            "tmcmc_attempts": deepcopy(campaign["tmcmc_attempts"]),
            "method_wrappers": deepcopy(campaign["method_wrappers"]),
            "confirmation_authorization": deepcopy(campaign["confirmation_authorization"]),
            "confirmation_commitment": campaign.get("confirmation_commitment")}
        if campaign["pins"]["sealed_allocation_anchor_sha256"] is not None:
            sealed = state["requests"].get("accuracy_seal:" + campaign["id"])
            if sealed is None:
                _error(409, "accuracy_seal_missing", "Immutable method seal is unavailable")
            value["method_seal"] = deepcopy(sealed["descriptor"])
            value["sealed_allocation_anchor"] = deepcopy(sealed["anchor"])
        value.update(extra)
        return value

    def _has_active(self, state, campaign, *, kind=None, method=None):
        return any((kind is None or self._task(state, campaign, t)["kind"] == kind)
            and (method is None or self._task(state, campaign, t)["method"] == method)
            for t in campaign["active_tasks"])

    def _plan(self, state, campaign, method, reason):
        if self._has_active(state, campaign, method=method):
            return
        self._new_task(state, campaign, "plan", method=method,
            inputs=self._inputs(state, campaign, planning_method=method,
                planning_reason=reason, ledger_revision=campaign["ledger_revision"]))

    def _set_history(self, campaign, method, values):
        if not isinstance(values, list) or len(values) > 1000:
            raise ValueError("Bounded immutable guide history required")
        previous = campaign["guide_history"][method]
        if values[:len(previous)] != previous:
            _error(409, "accuracy_history_conflict", "A retained guide result was omitted or changed")
        for item in values:
            protocol.descriptor(item, native_root=self._context(campaign)["native_root"] + "/" + campaign["id"])
        campaign["guide_history"][method] = deepcopy(values)

    def _planner_action(self, state, campaign, task, result):
        proposal = protocol.validate_proposal(result["proposal"])
        method = proposal["method"]
        if task["manifest"]["inputs"].get("planning_method") != method:
            raise ValueError("Native planner method changed")
        # Only this method's funding/producer history must still match. Other
        # campaigns and the other method have independent native allowances.
        old = [r for r in task["manifest"]["accounting"]["allocations"] if r["method"] == method]
        current = [r for r in self._rows(state, campaign) if r["method"] == method]
        if old != current:
            _error(409, "accuracy_stale_planner", "Native method accounting changed during planning")
        if proposal["action"] in ("train_factor", "fresh_tmcmc_attempt", "evaluate_guide", "freeze") and (
            self._has_active(state, campaign, method=method)
            or any(r["status"] not in protocol.TERMINAL for r in current)
        ):
            _error(409, "accuracy_method_allocation_active", "Current native method action/publication must close before another source decision")
        outputs = result["outputs"]
        if "guide_history" in outputs:
            self._set_history(campaign, method, outputs["guide_history"])
        if "method_states" in outputs:
            states = outputs["method_states"]
            if not isinstance(states, dict) or set(states) != {method}:
                raise ValueError("One source-derived method wrapper required")
            protocol.descriptor(states[method],
                native_root=self._context(campaign)["native_root"] + "/" + campaign["id"])
            campaign["method_wrappers"][method] = deepcopy(states[method])
        info = campaign["methods"][method]
        action = proposal["action"]
        info["effort_target"] = proposal["effort_target"]
        info["candidate_sha256"] = proposal["candidate_sha256"]
        if (action in ("train_factor", "fresh_tmcmc_attempt")
                or (action == "evaluate_guide" and method == "hnpe")) and proposal["maximum_reserved_gpu_seconds"] < 60:
            raise ValueError("Source action cannot fund the approved 60-second native minimum")
        if action == "train_factor":
            chosen = proposal["eligible_factors"][0]
            factor = chosen["factor"]
            previous = campaign["progress"].get(factor)
            if chosen["operation"] == "initialize":
                if previous is not None or any(r["method"] == "hnpe" and r["factor"] == factor
                    and not r["not_submitted"] for r in current):
                    _error(409, "accuracy_lineage_reinitialization", "An allocated lineage cannot be initialized again")
                kind = "hnpe_initialize"
            else:
                if previous is None or previous["state_sha256"] != chosen["expected_state_sha256"] or previous["generation"] != chosen["generation"]:
                    _error(409, "accuracy_generation_conflict", "Native optimizer predecessor changed")
                kind = "hnpe_advance"
            made = self._new_task(state, campaign, kind, method="hnpe", factor=factor,
                effort_target=chosen["to_update"], generation=chosen["generation"],
                previous=chosen["expected_state_sha256"],
                maximum=proposal["maximum_reserved_gpu_seconds"],
                inputs=self._inputs(state, campaign, planner_receipt=result["native_receipt"]))
            if made is None:
                info["status"], campaign["status"] = "budget_blocked", "blocked"
            else:
                info["status"] = "training"
        elif action == "fresh_tmcmc_attempt":
            if method != "tmcmc" or proposal["effort_target"] not in (2048, 4096, 8192):
                raise ValueError("Bounded fresh TMCMC population required")
            made = self._new_task(state, campaign, "guide_tmcmc_attempt", method=method,
                effort_target=proposal["effort_target"],
                maximum=proposal["maximum_reserved_gpu_seconds"],
                inputs=self._inputs(state, campaign, planner_receipt=result["native_receipt"]))
            info["status"] = "sampling" if made is not None else "budget_blocked"
            if made is None:
                campaign["status"] = "blocked"
        elif action == "evaluate_guide":
            if proposal["candidate_sha256"] is None:
                raise ValueError("Positive selected candidate required for guide evaluation")
            kind = "guide_hnpe_inference" if method == "hnpe" else "guide_compile"
            if method == "hnpe" and proposal["maximum_reserved_gpu_seconds"] < 1:
                raise ValueError("Native HNPE guide inference requires a positive source allowance")
            made = self._new_task(state, campaign, kind, method=method,
                effort_target=proposal["effort_target"],
                maximum=proposal["maximum_reserved_gpu_seconds"] if method == "hnpe" else None,
                inputs=self._inputs(state, campaign, planner_receipt=result["native_receipt"]))
            info["status"] = "evaluating" if made is not None else "budget_blocked"
            if made is None:
                campaign["status"] = "blocked"
        elif action == "freeze":
            if method not in campaign["method_wrappers"]:
                raise ValueError("A source-derived terminal wrapper is required")
            info["status"] = proposal["status"]
            campaign["phase_receipts"]["freeze_" + method] = deepcopy(result["native_receipt"])
            if all(campaign["methods"][m]["status"] in ("frozen", "terminal_unmet", "no_candidate")
                   for m in ("hnpe", "tmcmc")):
                if not self._has_active(state, campaign, kind="method_seal"):
                    self._new_task(state, campaign, "method_seal", inputs=self._inputs(state, campaign))
        elif action == "record_failed_guide":
            if "guide_history" not in outputs:
                raise ValueError("An authenticated complete failed-guide prefix is required")
            info["guide_status"] = "failed"
            self._plan(state, campaign, method, "failed_guide_recorded")
        else:
            info["status"] = "waiting" if action == "wait" else "reconcile_required"
            if action == "reconcile_native_state":
                campaign["status"] = "blocked"

    def _training_progress(self, campaign, task, result):
        value = result["outputs"].get("progress")
        if not isinstance(value, dict):
            raise ValueError("Source-validated factor progress required")
        protocol.keys(value, {"factor", "state_sha256", "generation", "optimizer_updates",
            "candidate_sha256", "native_receipt"}, {
            "best_validation_nll", "validation_nll", "stop_reason", "native_generation"})
        if value["factor"] != task["factor"]:
            raise ValueError("Native factor changed")
        protocol.sha(value["state_sha256"])
        protocol.integer(value["generation"], "generation")
        updates = protocol.integer(value["optimizer_updates"], "optimizer updates", 0,
            campaign["request"]["controls"]["hnpe"]["max_updates"])
        if task["kind"] == "hnpe_initialize":
            if value["generation"] != 0 or updates != 0 or value["candidate_sha256"] is not None:
                raise ValueError("Initialization is not positive trained evidence")
        else:
            if value["generation"] != task["generation"] + 1 or updates > task["effort_target"]:
                raise ValueError("Native advance generation/target mismatch")
            previous = campaign["progress"].get(task["factor"])
            if previous is None or previous["state_sha256"] != task["expected_state_sha256"]:
                _error(409, "accuracy_generation_conflict", "Current optimizer predecessor changed")
            if updates < previous["optimizer_updates"]:
                raise ValueError("Committed native optimizer progress cannot regress")
            if updates < task["effort_target"] and (not isinstance(value.get("stop_reason"), str) or not value["stop_reason"]):
                raise ValueError("Partial native progress requires an authenticated native stop reason")
        if value["candidate_sha256"] is not None:
            protocol.sha(value["candidate_sha256"])
            if updates < 1:
                raise ValueError("A zero-update model is not an eligible candidate")
        protocol.descriptor(value["native_receipt"],
            native_root=self._context(campaign)["native_root"] + "/" + campaign["id"])
        import math
        for field in ("best_validation_nll", "validation_nll"):
            number = value.get(field)
            if number is not None and (type(number) not in (int, float) or not math.isfinite(number)):
                raise ValueError("Source monitoring values must be finite")
        if value.get("stop_reason") is not None and (not isinstance(value["stop_reason"], str) or len(value["stop_reason"]) > 128):
            raise ValueError("Invalid source stop reason")
        campaign["progress"][task["factor"]] = deepcopy(value)
        task["progress"] = {field: deepcopy(value.get(field)) for field in
            ("optimizer_updates", "best_validation_nll", "validation_nll", "stop_reason")}

    def _seal(self, state, campaign, task, result):
        outputs, pins = result["outputs"], result["output_pins"]
        if set(outputs) - {"method_seal", "sealed_allocation_anchor", "method_states"}:
            raise ValueError("Unsupported method seal outputs")
        if any(campaign["methods"][m]["status"] not in ("frozen", "terminal_unmet", "no_candidate")
               for m in ("hnpe", "tmcmc")):
            _error(409, "accuracy_methods_not_frozen", "Both exact native method histories must freeze first")
        descriptor = protocol.descriptor(outputs.get("method_seal"),
            native_root=self._context(campaign)["native_root"] + "/" + campaign["id"])
        anchor = outputs.get("sealed_allocation_anchor")
        protocol.validate_snapshot(anchor)
        expected_anchor = protocol.bind_current_allocation(task["manifest"], task["slurm_job_id"])
        if protocol.canonical(anchor) != protocol.canonical(expected_anchor):
            raise ValueError("The seal must include the exact producer-bound authoritative allocation anchor")
        if pins["method_seal_sha256"] != descriptor["sha256"] or pins["sealed_allocation_anchor_sha256"] != protocol.checksum(anchor):
            raise ValueError("Immutable method seal/ledger pins differ")
        protocol.validate_anchor(anchor, self._snapshot(state, campaign))
        method_states = outputs.get("method_states", campaign["method_wrappers"])
        if method_states != campaign["method_wrappers"]:
            raise ValueError("Sealing cannot replace retained immutable method-state descriptors")
        key = "accuracy_seal:" + campaign["id"]
        sealed = {"descriptor": descriptor, "anchor": deepcopy(anchor),
            "method_states": deepcopy(method_states)}
        old = state["requests"].get(key)
        if old is not None and old != sealed:
            _error(409, "accuracy_seal_conflict", "Immutable method seal cannot change")
        state["requests"][key] = sealed
        campaign["phase_receipts"]["method_seal"] = descriptor
        if campaign["confirmation_authorization"] is not None:
            _error(409, "accuracy_confirmation_already_authorized", "Confirmation release is one-shot")
        campaign["confirmation_authorization"] = {
            "id": uuid4().hex, "campaign_instance_id": campaign["id"],
            "source_binding_sha256": campaign["source_binding_sha256"],
            "scientific_request_hash": campaign["scientific_request_hash"],
            "transport_request_hash": campaign["configuration_hash"],
            "case_plan_sha256": pins["case_plan_sha256"],
            "method_seal_sha256": pins["method_seal_sha256"],
            "sealed_allocation_anchor_sha256": pins["sealed_allocation_anchor_sha256"]}
        campaign["phase"] = "confirmation_preparation"
        self._new_task(state, campaign, "confirmation_preparation",
            inputs=self._inputs(state, campaign))

    def _confirmation_tasks(self, state, campaign):
        if campaign["confirmation_authorization"] is None or campaign["pins"]["opening_id"] is None:
            _error(409, "accuracy_confirmation_not_open", "Source-bound method seal and opening receipt required")
        self._new_task(state, campaign, "confirmation_reference", inputs=self._inputs(state, campaign))
        for method, kind in (("hnpe", "confirmation_hnpe_inference"), ("tmcmc", "confirmation_tmcmc_attempt")):
            info = campaign["methods"][method]
            if info["candidate_sha256"] is not None:
                made = self._new_task(state, campaign, kind, method=method,
                    effort_target=info["effort_target"], inputs=self._inputs(state, campaign))
            else:
                made = None
            if made is None:
                self._new_task(state, campaign, "record_confirmation_outcome", method=method,
                    inputs=self._inputs(state, campaign, outcome_method=method,
                        outcome_reason="no_candidate" if info["candidate_sha256"] is None else "insufficient_method_budget"))
        campaign["phase"] = "confirmation"

    def _final_result(self, state, campaign, task, result):
        sealed = state["requests"].get("accuracy_seal:" + campaign["id"])
        if sealed is None:
            _error(409, "accuracy_seal_missing", "Validated method seal is required")
        current = self._snapshot(state, campaign)
        protocol.validate_anchor(sealed["anchor"], current, require_reference=False)
        if any(r["method"] in ("hnpe", "tmcmc", "reference") and r["status"] not in protocol.TERMINAL
               for r in current["allocations"]):
            _error(409, "accuracy_accounting_unknown", "Final numerical publication requires method and reference costs")
        if any(a["unknown_allocations"] for a in campaign["accounting"].values()):
            _error(409, "accuracy_accounting_unknown", "Close control/report overhead before final publication")
        required = {"confirmation_reference", "confirmation_hnpe", "confirmation_tmcmc"}
        if not required <= set(campaign["phase_receipts"]):
            raise ValueError("Every confirmation outcome is required")
        if result["scientific_status"] is None:
            raise ValueError("Source scientific outcome required")
        role_names = {"comparison": ("result_artifact_id", "comparison.json"),
            "accounting": ("accounting_artifact_id", "allocation-accounting-final.json"),
            "summary": ("summary_artifact_id", "display-summary.json"),
            "validation_capsule": ("validation_capsule_artifact_id", "validation-capsule.json")}
        roles = {}
        for role, (output, filename) in role_names.items():
            aid = result["outputs"].get(output)
            artifact = state["artifacts"].get(aid)
            if (aid not in task["artifact_ids"] or artifact is None or not artifact.get("verified")
                    or artifact["media_type"] != "application/json" or artifact["name"] != filename
                    or not self.app.objects.verify(artifact["storage_key"], artifact)):
                raise ValueError("A verified source publication role is missing or changed")
            roles[role] = artifact
        final_receipt = result["outputs"].get("final_accounting_receipt")
        protocol.descriptor(final_receipt, native_root=self._context(campaign)["native_root"] + "/" + campaign["id"])
        if final_receipt["sha256"] != roles["accounting"]["sha256"]:
            raise ValueError("Postexit source receipt differs from verified final accounting")
        # Only bounded source-produced metadata crosses the Lambda read boundary.
        # Raw comparison and full final ledger remain private downloadable files.
        capsule = self.app.objects.read_json(roles["validation_capsule"]["storage_key"],
            roles["validation_capsule"], maximum=protocol.DISPLAY_BYTES)
        summary = self.app.objects.read_json(roles["summary"]["storage_key"],
            roles["summary"], maximum=protocol.DISPLAY_BYTES)
        expected_binding = {"campaign_instance_id": campaign["id"],
            "scientific_request_hash": campaign["scientific_request_hash"],
            "transport_request_hash": campaign["configuration_hash"],
            "source_binding_sha256": campaign["source_binding_sha256"],
            "case_plan_sha256": campaign["pins"]["case_plan_sha256"],
            "guide_case_set_sha256": campaign["pins"]["guide_case_set_sha256"],
            "policy_sha256": self._context(campaign)["policy_sha256"]}
        qualification = protocol.validate_publication_capsule(capsule, summary,
            manifest=task["manifest"], job_id=task["slurm_job_id"], current=current,
            artifacts=result["artifacts"], roles={role: self.public_artifact(value) for role, value in roles.items()},
            expected_binding=expected_binding)
        if qualification != result["qualification"] or capsule["scientific_status"] != result["scientific_status"]:
            raise ValueError("Public outcome differs from unchanged source capsule")
        comparison, accounting = roles["comparison"], roles["accounting"]
        campaign["result"] = {"artifact_id": comparison["id"], "sha256": comparison["sha256"],
            "size_bytes": comparison["size_bytes"], "scientific_status": result["scientific_status"],
            "qualification": deepcopy(qualification),
            "accounting_artifact_id": accounting["id"], "accounting_sha256": accounting["sha256"],
            "summary_artifact_id": roles["summary"]["id"], "summary_sha256": roles["summary"]["sha256"],
            "validation_capsule_artifact_id": roles["validation_capsule"]["id"],
            "validation_capsule_sha256": roles["validation_capsule"]["sha256"],
            "first_comparison_sha256": comparison["sha256"],
            "allocation_snapshot_sha256": protocol.checksum(current),
            "budget_exceeded": bool(campaign.get("budget_exceeded", False))}
        campaign["scientific_status"] = result["scientific_status"]
        campaign["status"], campaign["phase"] = "completed", "completed"

    @staticmethod
    def _native_result_key(campaign, task):
        return "accuracy_native_result:" + campaign["id"] + ":" + task["id"]

    def _retain_native_result(self, state, campaign, task, result):
        # This record and the terminal task pointer commit in the SAME existing
        # DynamoDB revision transaction. A crash cannot publish a dangling
        # terminal reference. The full body is not duplicated on the task.
        key = self._native_result_key(campaign, task)
        digest = protocol.checksum(result)
        record = {"schema": "aws_mesouq.benchmark_accuracy_native_result_record.v1",
            "project_id": campaign["project_id"], "owner_sub": campaign["owner_sub"],
            "campaign_id": campaign["id"], "task_id": task["id"],
            "context_id": campaign["execution_context_id"],
            "manifest_sha256": task["manifest_sha256"],
            "source_binding_sha256": campaign["source_binding_sha256"],
            "sha256": digest, "result": deepcopy(result)}
        if len(protocol.canonical(record)) > 262144:
            raise ValueError("Native result record exceeds its independent metadata envelope")
        previous = state["requests"].get(key)
        if previous is not None and protocol.canonical(previous) != protocol.canonical(record):
            _error(409, "accuracy_native_result_conflict", "Immutable native result record cannot change")
        pointer = {"record_key": key, "sha256": digest}
        if task.get("result") is not None and task["result"] != pointer:
            _error(409, "accuracy_native_result_conflict", "Task native result pointer cannot change")
        state["requests"][key] = record
        task["result"] = pointer

    def _read_native_result(self, state, campaign, task):
        """Internal audit reconstruction; browser results still use private S3."""
        pointer = task.get("result")
        protocol.keys(pointer, {"record_key", "sha256"})
        protocol.sha(pointer["sha256"])
        key = self._native_result_key(campaign, task)
        if pointer["record_key"] != key:
            raise ValueError("Native result pointer changed task ownership")
        record = state["requests"].get(key)
        protocol.keys(record, {"schema", "project_id", "owner_sub", "campaign_id",
            "task_id", "context_id", "manifest_sha256", "source_binding_sha256", "sha256", "result"})
        expected = {"schema": "aws_mesouq.benchmark_accuracy_native_result_record.v1",
            "project_id": campaign["project_id"], "owner_sub": campaign["owner_sub"],
            "campaign_id": campaign["id"], "task_id": task["id"],
            "context_id": campaign["execution_context_id"], "manifest_sha256": task["manifest_sha256"],
            "source_binding_sha256": campaign["source_binding_sha256"]}
        if (any(record[name] != value for name, value in expected.items())
                or record["sha256"] != pointer["sha256"]
                or protocol.checksum(record["result"]) != pointer["sha256"]):
            raise ValueError("Native result record source/owner/body binding changed")
        if (len(protocol.canonical(record)) > 262144
                or not isinstance(record["result"], dict)
                or record["result"].get("slurm_job_id") != task["slurm_job_id"]):
            raise ValueError("Native result record differs from the allocated producer or bound")
        return protocol.validate_result(record["result"], task["manifest"],
            native_root=self._context(campaign)["native_root"] + "/" + campaign["id"])

    def complete(self, state, campaign, task, raw):
        result = protocol.validate_result(raw, task["manifest"],
            native_root=self._context(campaign)["native_root"] + "/" + campaign["id"])
        if result["slurm_job_id"] != task["slurm_job_id"]:
            raise ValueError("Native result producer changed")
        refs = []
        for ref in result["artifacts"]:
            protocol.keys(ref, {"id", "name", "sha256", "size_bytes", "media_type", "kind"})
            aid = ref["id"]
            artifact = state["artifacts"].get(aid)
            if aid not in task["artifact_ids"] or artifact is None or not artifact.get("verified") or self.public_artifact(artifact) != ref or not self.app.objects.verify(artifact["storage_key"], artifact):
                raise ValueError("Unverified or foreign publication artifact")
            refs.append(aid)
        if len(refs) != len(set(refs)):
            raise ValueError("Duplicate publication artifact")
        if any(not state["artifacts"].get(a, {}).get("verified") and
               not state["artifacts"].get(a, {}).get("reservation_released")
               for a in task["artifact_ids"]):
            _error(409, "accuracy_upload_not_ready", "All private upload reservations must settle")
        if campaign["pins"]["method_seal_sha256"] is not None:
            allowed = {"confirmation_preparation", "confirmation_hnpe_inference",
                "confirmation_tmcmc_attempt", "confirmation_reference",
                "confirmation_compile", "record_confirmation_outcome"}
            if task["kind"] not in allowed:
                raise ValueError("Scientific selection cannot reopen after method sealing")
        kind, outputs = task["kind"], result["outputs"]
        if task["id"] in campaign["active_tasks"]:
            campaign["active_tasks"].remove(task["id"])
        self._retain_native_result(state, campaign, task, result)
        task["status"], task["lease"] = "succeeded", None
        if kind == "guide_preparation":
            if campaign["phase"] != "guide_preparation":
                raise ValueError("Guide authority cannot be recreated")
            for pin in ("case_plan_sha256", "guide_case_set_sha256", "normalization_sha256", "validation_sha256"):
                protocol.sha(result["output_pins"][pin])
            protocol.sha(outputs.get("confirmation_commitment"))
            prepared = protocol.descriptor(outputs.get("guide_preparation"),
                native_root=self._context(campaign)["native_root"] + "/" + campaign["id"])
            campaign["phase_receipts"]["guide_preparation"] = prepared
            campaign["confirmation_commitment"] = outputs["confirmation_commitment"]
            campaign["pins"] = {**deepcopy(result["output_pins"]), "candidate_sha256": None}
            campaign["phase"] = "guide_reference"
            self._new_task(state, campaign, "guide_reference", inputs=self._inputs(state, campaign))
        else:
            campaign["pins"] = {**deepcopy(result["output_pins"]), "candidate_sha256": None}
            if kind == "guide_reference":
                campaign["phase_receipts"]["guide_reference"] = deepcopy(result["native_receipt"])
                campaign["phase"] = "methods"
                for method in ("hnpe", "tmcmc"):
                    self._plan(state, campaign, method, "initial_guide")
            elif kind == "plan":
                self._planner_action(state, campaign, task, result)
            elif kind in ("hnpe_initialize", "hnpe_advance"):
                self._training_progress(campaign, task, result)
                self._plan(state, campaign, "hnpe", "factor_committed")
            elif kind == "guide_tmcmc_attempt":
                if result["output_pins"]["candidate_sha256"] is not None:
                    campaign["methods"]["tmcmc"]["candidate_sha256"] = result["output_pins"]["candidate_sha256"]
                attempt = deepcopy(result["native_receipt"])
                if attempt in campaign["tmcmc_attempts"]:
                    raise ValueError("Fresh TMCMC attempt was replayed")
                campaign["tmcmc_attempts"].append(attempt)
                self._new_task(state, campaign, "guide_compile", method="tmcmc",
                    effort_target=task["effort_target"], inputs=self._inputs(state, campaign,
                    method_result=attempt))
            elif kind == "guide_hnpe_inference":
                self._new_task(state, campaign, "guide_compile", method="hnpe",
                    effort_target=task["effort_target"], inputs=self._inputs(state, campaign,
                    method_result=deepcopy(result["native_receipt"])))
            elif kind == "guide_compile":
                history = outputs.get("guide_history")
                self._set_history(campaign, task["method"], history)
                campaign["methods"][task["method"]]["guide_status"] = result["scientific_status"]
                self._plan(state, campaign, task["method"], "guide_evaluated")
            elif kind == "method_seal":
                self._seal(state, campaign, task, result)
            elif kind == "confirmation_preparation":
                if campaign["confirmation_authorization"] is None:
                    raise ValueError("Confirmation lacks irreversible authorization")
                protocol.identifier(result["output_pins"]["opening_id"])
                descriptor = protocol.descriptor(outputs.get("confirmation_bank"),
                    native_root=self._context(campaign)["native_root"] + "/" + campaign["id"])
                campaign["phase_receipts"]["confirmation_bank"] = descriptor
                self._confirmation_tasks(state, campaign)
            elif kind in ("confirmation_hnpe_inference", "confirmation_tmcmc_attempt", "record_confirmation_outcome", "confirmation_reference"):
                role = "reference" if kind == "confirmation_reference" else task["method"]
                key = "confirmation_" + role
                if key in campaign["phase_receipts"]:
                    raise ValueError("First confirmation outcome cannot change")
                if kind != "confirmation_reference":
                    outcomes = outputs.get("confirmation_outcomes")
                    if not isinstance(outcomes, dict) or set(outcomes) != {role}:
                        raise ValueError("Source-owned first confirmation outcome receipt required")
                    protocol.descriptor(outcomes[role],
                        native_root=self._context(campaign)["native_root"] + "/" + campaign["id"])
                    campaign["phase_receipts"][key] = deepcopy(outcomes[role])
                else:
                    campaign["phase_receipts"][key] = deepcopy(result["native_receipt"])
                if all("confirmation_" + m in campaign["phase_receipts"] for m in ("hnpe", "tmcmc", "reference")):
                    self._new_task(state, campaign, "confirmation_compile",
                        inputs=self._inputs(state, campaign))
            elif kind == "confirmation_compile":
                self._final_result(state, campaign, task, result)

    def archive_verified(self, state, identity, sub, body):
        protocol.keys(body, {"project_id", "campaign_id", "connector_id", "event_id", "receipt", "signature"})
        for name in ("project_id", "campaign_id", "connector_id", "event_id"):
            protocol.identifier(body[name], name)
        campaign = self._owned(state, identity, sub, body["project_id"], body["campaign_id"])
        self.signatures.signature(identity, body["connector_id"], body["signature"],
            protocol.archive_signing_bytes(sub, body))
        key = "accuracy_archive:" + protocol.checksum([sub, campaign["id"], body["event_id"]])
        digest = protocol.checksum({k: v for k, v in body.items() if k != "signature"})
        previous = state["events"].get(key)
        if previous:
            if previous["hash"] != digest:
                _error(409, "idempotency_conflict", "Archive event ID changed")
            return 200, deepcopy(previous["response"])
        available, reason = self._archive_available(state, campaign)
        if not available or campaign["archive"]["status"] not in ("requested", "verifying", "verified"):
            _error(409, "accuracy_archive_not_ready", reason or "Archive was not requested")
        receipt = body["receipt"]
        protocol.keys(receipt, {'schema', 'campaign_id', 'context_id', 'source_binding_sha256', 'scientific_request_hash', 'transport_request_hash', 'allocation_snapshot_sha256', 'method_seal_sha256', 'manifest_sha256', 'source_snapshot_sha256', 'copy_snapshot_sha256', 'file_count', 'total_bytes', 'closed_pair_count', 'independent_copy', 'no_hardlinks', 'aliases_closed', 'first_comparison_sha256', 'final_accounting_sha256'})
        if receipt["schema"] != "aws_mesouq.benchmark_accuracy_archive.v1" or receipt["campaign_id"] != campaign["id"] or receipt["context_id"] != campaign["execution_context_id"]:
            raise ValueError("Archive identity changed")
        for field, expected in (
            ("source_binding_sha256", campaign["source_binding_sha256"]),
            ("scientific_request_hash", campaign["scientific_request_hash"]),
            ("transport_request_hash", campaign["configuration_hash"]),
            ("method_seal_sha256", campaign["pins"]["method_seal_sha256"]),
            ("allocation_snapshot_sha256", protocol.checksum(self._snapshot(state, campaign)))):
            if receipt[field] != expected:
                raise ValueError("Archive source/science/cost chain changed")
        for field in ("manifest_sha256", "source_snapshot_sha256", "copy_snapshot_sha256"):
            protocol.sha(receipt[field])
        if receipt["source_snapshot_sha256"] != receipt["copy_snapshot_sha256"]:
            raise ValueError("Independent archive inventory differs")
        for field in ("independent_copy", "no_hardlinks", "aliases_closed"):
            if type(receipt[field]) is not bool or not receipt[field]:
                raise ValueError("Verified independent alias-closed copy required")
        protocol.integer(receipt["file_count"], "archive file count", 1)
        protocol.integer(receipt["total_bytes"], "archive bytes", 1)
        protocol.integer(receipt["closed_pair_count"], "closed alias pairs")
        expected_result = campaign.get("result") or {}
        if (receipt["first_comparison_sha256"] != expected_result.get("first_comparison_sha256")
            or receipt["final_accounting_sha256"] != expected_result.get("accounting_sha256")):
            raise ValueError("Archive does not bind the immutable comparison and final accounting")
        receipt_sha = protocol.checksum(receipt)
        if campaign["archive"]["status"] == "verified" and campaign["archive"]["receipt_sha256"] != receipt_sha:
            _error(409, "accuracy_archive_conflict", "Verified archive receipt cannot change")
        state["accuracy_campaigns"][_key(campaign["project_id"], sub, campaign["id"])] = campaign
        campaign["archive"] = {"status": "verified", "receipt_sha256": receipt_sha,
            "file_count": receipt["file_count"], "total_bytes": receipt["total_bytes"],
            "verified_at": self.app.clock()}
        state["requests"]["accuracy_archive_receipt:" + campaign["id"]] = deepcopy(receipt)
        for aid in campaign["artifacts"]:
            artifact = state["artifacts"].get(aid)
            if artifact and artifact.get("verified"):
                artifact["archive_verified_at"] = self.app.clock()
        response = {"campaign": self.public(state, campaign, detail=True)}
        state["events"][key] = {"hash": digest, "response": deepcopy(response)}
        return 200, response
