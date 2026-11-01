"""Durable accuracy task coordination for a single supervised connector.

This worker owns neither authentication nor a journal lock/SSH connection. A
reviewed native executor is injected by the maintained CLI. Its bounded sweep
can share one journal and transport with the unchanged benchmark-v1 worker.
"""
from __future__ import annotations

from copy import deepcopy
import ast
import hashlib
import json
import math
import subprocess
from pathlib import PurePosixPath
import time
from urllib.parse import urlsplit
from uuid import uuid4

from . import benchmark_accuracy_protocol as p
from .benchmark_worker import BenchmarkWorker, ArtifactIntegrityError
from .connector.api import APIError as ConnectorAPIError
from .connector.auth import SessionExpired
from .connector.ssh_session import SSHSessionError
from .connector.archive_source import TREE_HELPER

def _archive_destination_helper():
    """Retain the source scanner with one typed destination control-lock rule."""
    tree = ast.parse(TREE_HELPER)
    walks = [node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name == "walk"]
    if len(walks) != 1:
        raise ValueError("Reviewed archive scanner shape changed")
    loops = [node for node in walks[0].body if isinstance(node, ast.For)]
    if len(loops) != 1:
        raise ValueError("Reviewed archive enumeration shape changed")
    guard = ast.parse("""if not prefix and name == ".archive.lock":
    assert not accuracy_archive_lock
    lockfd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=fd)
    try:
        info = src_private(lockfd)
        assert stat.S_ISREG(info.st_mode)
        assert stat.S_IMODE(info.st_mode) == 0o600
        assert info.st_nlink == 1 and info.st_size == 0
        accuracy_archive_lock.append((lockfd, src_stamp(info)))
    except BaseException:
        os.close(lockfd)
        raise
    continue
""").body[0]
    loops[0].body.insert(0, guard)
    tree.body.insert(0, ast.parse("accuracy_archive_lock = []").body[0])
    blocks = [node for node in tree.body if isinstance(node, ast.Try)]
    if len(blocks) != 1:
        raise ValueError("Reviewed archive final guard shape changed")
    checks = ast.parse("""assert len(accuracy_archive_lock) == 1
lockfd, lockstamp = accuracy_archive_lock[0]
assert src_stamp(src_private(lockfd)) == lockstamp
lockpath = os.stat(".archive.lock", dir_fd=rootfd, follow_symlinks=False)
assert stat.S_ISREG(lockpath.st_mode) and src_stamp(lockpath) == lockstamp
""").body
    blocks[0].body[-1:-1] = checks
    blocks[0].finalbody.extend(ast.parse("""for lockfd, lockstamp in accuracy_archive_lock:
    os.close(lockfd)
""").body)
    return ast.unparse(ast.fix_missing_locations(tree))


ARCHIVE_DESTINATION_HELPER = _archive_destination_helper()
JOURNAL_SCHEMA = "aws_mesouq.benchmark_accuracy_worker.v1"


class AccuracyWorkerBlocked(RuntimeError):
    """A retained ambiguity or unmet source contract requires reconciliation."""


def funded_window(reserved_gpu_seconds, internal_seconds):
    """Never round an allocated minute above the original one-GPU reservation."""
    p.integer(reserved_gpu_seconds, "original GPU reservation", 1)
    if (type(internal_seconds) not in (int, float)
            or not math.isfinite(internal_seconds) or internal_seconds <= 0):
        raise ValueError("Finite positive internal seconds required")
    allocated = min(reserved_gpu_seconds // 60 * 60, 172800)
    if allocated < 60:
        return None
    return {"allocated_seconds": allocated,
            "internal_seconds": min(float(internal_seconds), float(allocated)),
            "runtime_clock_policy": "allocation_deadline_v1",
            "time": f"{allocated // 3600:02d}:{allocated // 60 % 60:02d}:{allocated % 60:02d}"}


class AccuracyWorker:
    def __init__(self, client, slurm, journal, *, project, contexts, executor,
                 clock=time.time, maximum_active=16):
        p.integer(maximum_active, "active coordination slots", 1, 100)
        self.client, self.slurm, self.journal = client, slurm, journal
        self.project, self.contexts, self.executor = project, contexts, executor
        self.clock, self.maximum_active = clock, maximum_active
        p.identifier(project)
        if contexts.project != project or contexts.owner_sub != client.owner_sub:
            raise ValueError("Worker and trusted contexts have different owners/projects")
        original = journal.data.get("benchmark_accuracy")
        if original is None:
            original = {"schema": JOURNAL_SCHEMA, "project_id": project,
                "owner_sub": client.owner_sub, "pending_claim": None, "tasks": {},
                "campaigns": {}, "campaign_cursor": None, "sweep_cursor": 0}
            journal.data["benchmark_accuracy"] = original
            journal.save()
        if (original.get("schema") != JOURNAL_SCHEMA
                or original.get("project_id") != project
                or original.get("owner_sub") != client.owner_sub):
            raise ValueError("Accuracy journal namespace belongs to another execution owner")
        self.state = original
        self.state.setdefault("archives", {})

    @property
    def transport(self):
        return self.slurm.transport

    _archive_batches = staticmethod(BenchmarkWorker._archive_batches)
    _archive_file_records = staticmethod(BenchmarkWorker._archive_file_records)
    _archive_remote = BenchmarkWorker._archive_remote
    _archive_snapshot = BenchmarkWorker._archive_snapshot
    _immutable = BenchmarkWorker._immutable

    @staticmethod
    def _fatal(error):
        return isinstance(error, (SSHSessionError, SessionExpired)) or (
            isinstance(error, ConnectorAPIError) and error.status in (401, 403))

    def save(self):
        self.journal.save()

    def supported(self):
        reviewed = set(self.contexts.supported())
        admitted = self.executor.admitted_contexts()
        if not isinstance(admitted, list) or any(value not in reviewed for value in admitted):
            raise ValueError("Native executor advertised an unreviewed context")
        return sorted(set(admitted))

    def _entry_key(self, claim):
        return claim["campaign"]["id"] + ":" + claim["task"]["id"]

    def _validate_existing(self, entry):
        self.contexts.validate_claim(entry["claim"])
        manifest = entry["claim"]["task"]["manifest"]
        if (entry["context_id"] != manifest["context_id"]
                or entry["manifest_sha256"] != p.checksum(manifest)):
            raise ValueError("Durable task source binding changed")
        return self.contexts.context(entry["context_id"])

    def _retain_claim(self, claim):
        claim = self.contexts.validate_claim(claim)
        if claim["task"] is None:
            return None
        if claim["task"]["lease"]["connector_id"] != self.client._identity()["connector_id"]:
            raise ValueError("Claim lease belongs to another enrolled connector")
        key = self._entry_key(claim)
        existing = self.state["tasks"].get(key)
        if existing:
            self._validate_existing(existing)
            if existing["manifest_sha256"] != claim["task"]["manifest_sha256"]:
                raise ValueError("Same task changed its immutable native manifest")
            self._merge_claim(existing, claim)
            return existing
        task = claim["task"]
        phase = "claimed"
        if task["slurm_job_id"] is not None:
            phase = "waiting"
        elif task["reconcile_only"]:
            phase = "reconciling"
        entry = {"claim": deepcopy(claim), "context_id": task["manifest"]["context_id"],
            "manifest_sha256": task["manifest_sha256"], "phase": phase,
            "pending_event": None, "pending_refresh": None, "history": [],
            "job_id": task["slurm_job_id"], "submission_invoked": task["reconcile_only"],
            "last_refresh": self.clock(), "native_preparation": None,
            "native_terminal": None, "publication": None, "upload_plan": None,
            "upload_progress": {}, "closed_snapshot": None, "last_error": None}
        self.state["tasks"][key] = entry
        self.state["campaigns"][claim["campaign"]["id"]] = deepcopy(claim["campaign"])
        self.save()
        return entry

    def _merge_claim(self, entry, claim):
        old, new = entry["claim"]["task"], claim["task"]
        if (new["id"] != old["id"] or new["attempt"] != old["attempt"]
                or new["manifest_sha256"] != entry["manifest_sha256"]
                or new["lease"]["epoch"] < old["lease"]["epoch"]):
            raise ValueError("Exact task recovery changed native identity or lease fence")
        if entry.get("job_id") is not None and new["slurm_job_id"] not in (None, entry["job_id"]):
            raise ValueError("Native producer changed during task recovery")
        entry["claim"] = deepcopy(claim)
        entry["last_refresh"] = self.clock()
        if new["slurm_job_id"] is not None:
            entry["job_id"] = new["slurm_job_id"]
        self.state["campaigns"][claim["campaign"]["id"]] = deepcopy(claim["campaign"])
        self.save()

    def _claim_one(self):
        supported = self.supported()
        if not supported:
            return None
        if self.state["pending_claim"] is None:
            self.state["pending_claim"] = self.client.build_claim_body(self.project,
                "accuracy-claim-" + uuid4().hex, supported_contexts=supported)
            self.save()
        body = deepcopy(self.state["pending_claim"])
        claim = self.client.claim_frozen(body)
        entry = self._retain_claim(claim)
        self.state["pending_claim"] = None
        self.save()
        return entry

    def _refresh(self, entry, *, force=False):
        if entry["pending_event"] is not None:
            self._flush_event(entry)
        if entry["phase"] == "terminal":
            return False
        task = entry["claim"]["task"]
        if not force and self.clock() - entry["last_refresh"] < 15:
            if task["lease"]["expires_at"] > self.clock() + 10:
                return True
        if entry["pending_refresh"] is None:
            entry["pending_refresh"] = self.client.build_claim_body(self.project,
                "accuracy-refresh-" + uuid4().hex, supported_contexts=[entry["context_id"]],
                campaign_id=entry["claim"]["campaign"]["id"], task_id=task["id"])
            self.save()
        response = self.contexts.validate_claim(self.client.claim_frozen(entry["pending_refresh"]))
        if response["task"] is None:
            return self._reconcile_terminal(entry)
        self._merge_claim(entry, response)
        entry["pending_refresh"] = None
        if response["task"]["lease"]["expires_at"] <= self.clock() + 10:
            # A lost refresh ACK may replay an old, now-expired claim response.
            # Its idempotency record is valid; its lease is not permission for
            # another native side effect. The next pass requests a fresh fence.
            entry["last_refresh"] = 0
            self.save()
            raise AccuracyWorkerBlocked("Cached exact-task response has an expired lease")
        self.save()
        return True

    def _reconcile_terminal(self, entry):
        original, campaign = entry["claim"]["task"], entry["claim"]["campaign"]
        current = self.client.task(self.project, campaign["id"], original["id"])
        task, authoritative = current["task"], current["campaign"]
        if (task["id"] != original["id"] or task["attempt"] != original["attempt"]
                or task["manifest_sha256"] != entry["manifest_sha256"]
                or task["allocation_id"] != original["allocation_id"]
                or authoritative["id"] != campaign["id"]
                or any(authoritative[name] != campaign[name] for name in
                    ("execution_context_id", "configuration_hash", "scientific_request_hash"))):
            raise ValueError("Exact terminal-task read changed the source/request identity")
        if task["status"] not in p.TERMINAL:
            raise AccuracyWorkerBlocked("Exact task lease is unavailable; no fallback claim")
        ledger = self.client.accounting(self.project, campaign["id"])
        rows = [row for row in ledger["snapshot"]["allocations"]
                if row["allocation_id"] == original["allocation_id"]]
        if len(rows) != 1 or any(rows[0][name] is None for name in
                ("allocated_gpu_seconds", "allocated_cpu_seconds")) or rows[0]["reserved_gpu_seconds"] != 0:
            raise AccuracyWorkerBlocked("Terminal task accounting is not authoritative and complete")
        if rows[0]["status"] not in p.TERMINAL or task["slurm_job_id"] != rows[0]["slurm_job_id"]:
            raise ValueError("Terminal native producer/accounting identity changed")
        if entry["job_id"] not in (None, task["slurm_job_id"]):
            raise ValueError("Terminal read changed the known native producer")
        original.update(deepcopy(task))
        entry["claim"]["campaign"] = deepcopy(authoritative)
        entry["phase"] = "terminal"
        entry["pending_refresh"] = None
        entry["last_refresh"] = self.clock()
        entry["history"].append({"type": "authoritative_terminal_read",
            "task_revision": task["revision"], "accounting_sha256": ledger["sha256"]})
        self.state["campaigns"][campaign["id"]] = deepcopy(authoritative)
        self.save()
        return False

    def _archive_target_snapshot(self, root):
        output = self._archive_remote(self.transport.run, [
            getattr(self.transport, "staging_python", "/usr/bin/python3.11"),
            "-I", "-B", "-c", ARCHIVE_DESTINATION_HELPER, root])
        try:
            snapshot = json.loads(output)
        except (TypeError, ValueError) as error:
            raise ArtifactIntegrityError("Invalid destination archive inventory") from error
        p.keys(snapshot, {"version", "files", "source_link_groups"})
        if type(snapshot["version"]) is not int or snapshot["version"] != 1 or snapshot["source_link_groups"] != []:
            raise ArtifactIntegrityError("Destination archive must have no hardlink aliases")
        files = self._archive_file_records(snapshot["files"])
        if files != sorted(files, key=lambda item: item["path"]):
            raise ArtifactIntegrityError("Destination archive inventory is not canonical")
        return files, {}

    def _ack(self, entry, response):
        task = response["task"]
        old = entry["claim"]["task"]
        if task["id"] != old["id"] or task["manifest_sha256"] != entry["manifest_sha256"]:
            raise ValueError("Event ACK changed native task identity")
        # An idempotent replay can return an old ACK. Never regress known fences.
        if task["lease_epoch"] < old["lease"]["epoch"] or task["revision"] < old["revision"]:
            entry["last_refresh"] = 0
            return
        for key in ("revision", "status", "slurm_job_id"):
            if key in task:
                old[key] = deepcopy(task[key])
        if task["lease_epoch"] != old["lease"]["epoch"]:
            raise ValueError("Event ACK unexpectedly changed its lease fence")
        old["lease_epoch"] = task["lease_epoch"]
        old["lease"]["epoch"] = task["lease_epoch"]
        old["reconcile_only"] = old["status"] in ("submitting", "submission_unknown",
            "submitted", "queued", "running", "cancelling", "publishing")
        old["lease"]["expires_at"] = task["lease_expires_at"]
        if old["slurm_job_id"] is not None:
            if entry["job_id"] not in (None, old["slurm_job_id"]):
                raise ValueError("Event ACK changed the native allocation")
            entry["job_id"] = old["slurm_job_id"]
        entry["claim"]["campaign"] = deepcopy(response["campaign"])
        self.state["campaigns"][response["campaign"]["id"]] = deepcopy(response["campaign"])

    def _flush_event(self, entry):
        pending = entry["pending_event"]
        if pending is None:
            return None
        try:
            response = self.client.event_frozen(deepcopy(pending["body"]))
        except ConnectorAPIError as error:
            # The service checks an existing event's immutable body BEFORE its
            # lease/revision. These typed rejections therefore prove that this
            # event was not applied; transport failures never permit rebasing.
            if error.status != 409 or error.code not in (
                    "invalid_lease", "accuracy_revision_conflict", "accuracy_task_terminal"):
                raise
            entry.setdefault("rejected_events", []).append({
                **deepcopy(pending), "body_sha256": p.checksum(pending["body"]),
                "rejection_code": error.code, "rejected_at": self.clock()})
            entry["reconcile_event"] = deepcopy(pending)
            entry["pending_event"] = None
            entry["pending_refresh"] = None
            entry["last_refresh"] = 0
            self.save()
            raise AccuracyWorkerBlocked("Unapplied event retained; exact fenced reconciliation required") from error
        self._ack(entry, response)
        kind = pending["body"]["type"]
        if kind == "artifact_upload":
            if entry["upload_plan"] is not None:
                old_ids = [v["artifact_id"] for v in entry["upload_plan"]]
                if [v["artifact_id"] for v in response["uploads"]] != old_ids:
                    raise ValueError("Renewed publication changed immutable object identities")
            entry["upload_plan"] = deepcopy(response["uploads"])
            entry["renew_upload_plan"] = False
        if kind == "artifact_status":
            for status in response["artifacts"]:
                progress = entry["upload_progress"].setdefault(status["id"], {})
                progress["verified"] = status["verified"] is True
        if pending["phase_after"] is not None:
            entry["phase"] = pending["phase_after"]
        entry["history"].append({"event_id": pending["body"]["event_id"], "type": kind,
            "body_sha256": p.checksum(pending["body"]), "task_revision": response["task"]["revision"]})
        entry["pending_event"] = None
        # Require fresh exact state before another native side effect after ACK.
        entry["last_refresh"] = 0
        self.save()
        return response

    def _event(self, entry, kind, payload, *, phase_after=None):
        if entry["pending_event"] is not None:
            raise ValueError("Flush the exact durable event before creating another")
        body = self.client.build_event_body(self.project, entry["claim"],
            "accuracy-event-" + uuid4().hex, kind, payload)
        entry["pending_event"] = {"body": body, "phase_after": phase_after}
        self.save()
        return self._flush_event(entry)

    def _recover_rejected_event(self, entry):
        pending = entry["reconcile_event"]
        if not self._refresh(entry, force=True):
            entry["reconcile_event"] = None
            self.save()
            return
        original = entry["claim"]["task"]
        ledger = self.client.accounting(self.project, entry["claim"]["campaign"]["id"])
        rows = [row for row in ledger["snapshot"]["allocations"]
                if row["allocation_id"] == original["allocation_id"]]
        if (len(rows) != 1 or rows[0]["attempt"] != original["attempt"]
                or rows[0]["slurm_job_id"] != original["slurm_job_id"]):
            raise ValueError("Rejected-event recovery changed allocation identity")
        row = rows[0]
        closed = (row["status"] in p.TERMINAL
            and row["allocated_gpu_seconds"] is not None
            and row["allocated_cpu_seconds"] is not None
            and row["reserved_gpu_seconds"] == 0)
        kind = pending["body"]["type"]
        # Retain the old frozen body forever. Any safe semantic retry gets a
        # fresh event ID and the newly authenticated lease/revision.
        entry["reconcile_event"] = None
        self.save()
        if closed and kind in ("submitted", "queued", "running", "submission_unknown", "allocation_closed"):
            entry["phase"] = "closed" if entry["native_terminal"] is not None else "waiting"
            self.save()
            return
        if self._cancelled(entry) and kind in ("heartbeat", "artifact_upload", "artifact_status", "result"):
            if not closed:
                raise AccuracyWorkerBlocked("Cancellation requires authoritative actual allocation costs")
            self._event(entry, "cancelled", {"not_submitted": False}, phase_after="terminal")
            return
        if kind == "heartbeat":
            return
        self._event(entry, kind, deepcopy(pending["body"]["payload"]),
                    phase_after=pending["phase_after"])

    def _lookup(self, entry):
        matches = self.slurm.lookup(entry["claim"]["task"]["marker"])
        jobs = {job for job, state in matches}
        if len(jobs) > 1:
            raise AccuracyWorkerBlocked("Multiple native allocations match one immutable task")
        if jobs:
            job = next(iter(jobs))
            p.identifier(job, "native job")
            if not job.isdecimal() or int(job) <= 0:
                raise ValueError("Invalid native allocation identity")
            if entry["job_id"] not in (None, job):
                raise ValueError("Reconciliation found another native producer")
            entry["job_id"] = job
            entry["phase"] = "job_known"
            self.save()
            return True
        return False

    def _cancelled(self, entry):
        return (entry["claim"]["campaign"]["status"] == "cancelling"
            or entry["claim"]["task"]["status"] == "cancelling")

    def _record_preflight(self, entry, boundary, proof, *, allowed):
        p.keys(proof, {"schema", "context_id", "manifest_sha256", "checked_epoch", "home_quota"})
        checked = proof["checked_epoch"]
        if (proof["schema"] != "aws_mesouq.benchmark_accuracy_preflight.v1"
                or proof["context_id"] != entry["context_id"]
                or proof["manifest_sha256"] != entry["manifest_sha256"]
                or type(checked) not in (int, float) or not math.isfinite(checked)
                or checked < self.clock() - 60 or checked > self.clock() + 5
                or not isinstance(proof["home_quota"], dict)
                or len(p.canonical(proof)) > 65536):
            raise ValueError("Fresh bounded source/context preflight proof required")
        p._no_raw_secret(proof)
        entry.setdefault("preflight_history", []).append({"boundary": boundary,
            "allowed": allowed, "snapshot": deepcopy(proof), "sha256": p.checksum(proof)})
        self.save()

    def _preflight(self, entry, boundary):
        try:
            proof = self.executor.preflight(deepcopy(entry))
        except Exception as error:
            denied = getattr(error, "preflight", None)
            if denied is not None:
                self._record_preflight(entry, boundary, denied, allowed=False)
            raise
        self._record_preflight(entry, boundary, proof, allowed=True)
        return proof

    def _prepare(self, entry):
        context = self._validate_existing(entry)
        reservation = entry["claim"]["task"]["manifest"]["reservation"]["gpu_seconds"]
        window = funded_window(reservation, reservation)
        if window is None:
            # No native RPC, HTTP submission intent or scheduler call is allowed.
            # Activation remains disabled until the source sticky-unfundable
            # outcome contract is admitted; do not fake zero or repeat planning.
            entry["phase"] = "blocked_unfundable"
            entry["last_error"] = "source_sticky_unfundable_contract_required"
            self.save()
            return
        try:
            preparation = self.executor.prepare(deepcopy(entry["claim"]), window)
        except Exception as error:
            denied = getattr(error, "preflight", None)
            if denied is not None:
                self._record_preflight(entry, "before_prepare", denied, allowed=False)
            raise
        p.keys(preparation, {"script_path", "script_sha256", "intent_sha256", "marker", "window"}, {"preflight"})
        if "preflight" in preparation:
            self._record_preflight(entry, "before_prepare", preparation["preflight"], allowed=True)
        root = PurePosixPath(context["native_root"]) / entry["claim"]["campaign"]["id"]
        script = PurePosixPath(preparation["script_path"])
        if (root not in script.parents or ".." in script.parts
                or preparation["marker"] != entry["claim"]["task"]["marker"]
                or preparation["window"] != window):
            raise ValueError("Native preparation escaped its source-bound task")
        p.sha(preparation["script_sha256"]); p.sha(preparation["intent_sha256"])
        entry["native_preparation"] = deepcopy(preparation)
        entry["phase"] = "prepared"
        self.save()

    def _submit_once(self, entry):
        if not self._refresh(entry, force=True):
            return
        if self._cancelled(entry):
            # After cloud intent an absent marker is ambiguity, never zero.
            entry["phase"] = "reconciling"
            self.save()
            return
        authorization = deepcopy(entry["claim"]["task"]["lease"])
        if self._lookup(entry):
            return
        if entry["submission_invoked"]:
            raise AccuracyWorkerBlocked("Scheduler submission already invoked; reconcile only")
        self._preflight(entry, "before_submit")
        # Neither native lookup nor quota/source checks extend permission to
        # submit. An expired check cycle must be retried with explicit authority.
        if authorization["expires_at"] <= self.clock() + 10:
            raise AccuracyWorkerBlocked("Submission authorization expired during native checks")
        if not self._refresh(entry, force=True):
            return
        if self._cancelled(entry):
            entry["phase"] = "reconciling"
            self.save()
            return
        if entry["job_id"] is not None:
            entry["phase"] = "job_known"
            self.save()
            return
        lease = entry["claim"]["task"]["lease"]
        if lease["epoch"] != authorization["epoch"]:
            raise AccuracyWorkerBlocked("Submission fence changed during native checks")
        entry["submission_invoked"] = True
        entry["phase"] = "submission_invoked"
        self.save()
        # The durable intent must precede sbatch. If persistence itself consumed
        # the remaining lease, retain the ambiguity without invoking sbatch.
        if lease["expires_at"] <= self.clock() + 10:
            raise AccuracyWorkerBlocked("Submission authorization expired while recording invocation")
        job = self.slurm.submit(entry["native_preparation"]["script_path"])
        if not isinstance(job, str) or not job.isdecimal() or int(job) <= 0:
            raise AccuracyWorkerBlocked("Ambiguous scheduler submission response")
        entry["job_id"] = job
        entry["phase"] = "job_known"
        self.save()

    def _authorize_cancel(self, entry):
        if not self._refresh(entry, force=True) or not self._cancelled(entry):
            raise AccuracyWorkerBlocked("Cancellation is no longer authoritatively requested")
        return True

    def _observe(self, entry):
        observation = self.executor.observe(deepcopy(entry))
        if observation is None:
            return
        if observation["job_id"] != entry["job_id"]:
            raise ValueError("Native observation changed the allocated producer")
        if self._cancelled(entry) and observation["accounting"] is None:
            self.executor.cancel(deepcopy(entry), authorize=lambda: self._authorize_cancel(entry))
            return
        if observation["accounting"] is not None:
            entry["native_terminal"] = deepcopy(observation)
            self.save()
            self._event(entry, "allocation_closed",
                {"scheduler_accounting": observation["accounting"]}, phase_after="closed")
            return
        state = observation["state"]
        if state == "queued" and entry["claim"]["task"]["status"] != "queued":
            self._event(entry, "queued", {"slurm_job_id": entry["job_id"]})
        elif state == "running" and entry["claim"]["task"]["status"] != "running":
            self._event(entry, "running", {"slurm_job_id": entry["job_id"],
                "allocation_start_epoch": observation["allocation_start_epoch"],
                "allocation_deadline_epoch": observation["allocation_deadline_epoch"]})

    def _closed(self, entry):
        terminal = entry["native_terminal"]
        if self._cancelled(entry):
            self._event(entry, "cancelled", {"not_submitted": False}, phase_after="terminal")
            return
        if terminal.get("mechanical_failure") is True:
            self._event(entry, "failed", {"not_submitted": False,
                "failure": {"code": "native_execution_failed", "message": "Native execution failed; actual allocation costs retained"}},
                phase_after="terminal")
            return
        ledger = self.client.accounting(self.project, entry["claim"]["campaign"]["id"])
        publication = self.executor.publication(deepcopy(entry), deepcopy(ledger))
        p.keys(publication, {"result", "artifacts", "artifact_roles"})
        context = self._validate_existing(entry)
        artifacts = publication["artifacts"]
        if not isinstance(artifacts, list) or len(artifacts) > context["maximum_artifact_files"]:
            raise ValueError("Curated publication file count exceeds its context")
        if sum(item["size_bytes"] for item in artifacts) > context["maximum_total_artifact_bytes"]:
            raise ValueError("Curated publication exceeds its total byte envelope")
        for item in artifacts:
            p.integer(item["size_bytes"], "curated file bytes", 1, context["maximum_artifact_bytes"])
        if entry["publication"] is not None and entry["publication"] != publication:
            raise ValueError("Native publication changed after first source validation")
        entry["closed_snapshot"] = deepcopy(ledger)
        entry["publication"] = deepcopy(publication)
        entry["phase"] = "publishing"
        self.save()

    def _publish(self, entry):
        if self._cancelled(entry):
            self._event(entry, "cancelled", {"not_submitted": False}, phase_after="terminal")
            return
        publication = entry["publication"]
        artifacts = publication["artifacts"]
        if artifacts and (entry["upload_plan"] is None or entry.get("renew_upload_plan")):
            self._event(entry, "artifact_upload", {"artifacts": deepcopy(artifacts)})
            return
        plan = entry["upload_plan"] or []
        if len(plan) != len(artifacts):
            raise ValueError("Private upload plan changed the curated file count")
        refs = []
        for spec, upload in zip(artifacts, plan, strict=True):
            ref = upload["artifact"]
            if any(ref[key] != spec[key] for key in ("name", "sha256", "size_bytes", "media_type", "kind")):
                raise ValueError("Cloud object differs from authenticated curated bytes")
            aid = upload["artifact_id"]
            if aid != ref["id"]:
                raise ValueError("Private artifact identifier mismatch")
            progress = entry["upload_progress"].get(aid, {})
            if progress.get("verified") is not True:
                ack = self._event(entry, "artifact_status", {"artifact_ids": [aid]})
                if entry["upload_progress"].get(aid, {}).get("verified") is True:
                    return
                if not self._refresh(entry, force=True):
                    return
                if self._cancelled(entry):
                    self._event(entry, "cancelled", {"not_submitted": False}, phase_after="terminal")
                    return
                target, headers = upload["upload"]
                parsed = urlsplit(target)
                operator = self.contexts.operator(entry["context_id"])
                if (parsed.scheme + "://" + parsed.netloc not in operator["allowed_upload_origins"]
                        or parsed.username or parsed.password or parsed.scheme != "https"):
                    raise ValueError("Private upload escaped approved HTTPS origins")
                body = self.executor.read_artifact(deepcopy(entry), deepcopy(spec))
                if len(body) != spec["size_bytes"] or hashlib.sha256(body).hexdigest() != spec["sha256"]:
                    raise ValueError("Curated bytes changed after native authentication")
                entry["upload_progress"][aid] = {"putting": True, "verified": False}
                self.save()
                try:
                    self.client.api.transfer(target, data=body, headers=headers)
                except Exception:
                    # A failed/412/lost-ACK PUT is never proof of success.
                    # Next tick renews URLs but preserves immutable object IDs;
                    # the backend probe alone decides whether bytes exist.
                    entry["renew_upload_plan"] = True
                    self.save()
                    raise
                finally:
                    del body
                self._event(entry, "artifact_status", {"artifact_ids": [aid]})
                return
            refs.append({"id": aid, **deepcopy(spec)})
        result = deepcopy(publication["result"])
        by_name = {ref["name"]: ref for ref in refs}
        for role, name in publication["artifact_roles"].items():
            if role not in p.ROLE_OUTPUTS[result["kind"]] or name not in by_name:
                raise ValueError("Native publication role is not an authenticated curated file")
            result["outputs"][role] = by_name[name]["id"]
        result["artifacts"] = refs
        context = self.contexts.context(entry["context_id"])
        p.validate_result(result, entry["claim"]["task"]["manifest"],
            native_root=context["native_root"] + "/" + entry["claim"]["campaign"]["id"])
        if result["slurm_job_id"] != entry["job_id"]:
            raise ValueError("Result producer differs from actual native allocation")
        self._event(entry, "result", {"result": result}, phase_after="terminal")

    def tick(self, entry):
        self._validate_existing(entry)
        if entry.get("reconcile_event") is not None:
            self._recover_rejected_event(entry)
            return
        if entry["pending_event"] is not None:
            self._flush_event(entry)
            return
        if entry["phase"] in ("terminal", "blocked_unfundable"):
            return
        if not self._refresh(entry):
            return
        phase = entry["phase"]
        if self._cancelled(entry) and phase in ("claimed", "prepared"):
            self._event(entry, "cancelled", {"not_submitted": True}, phase_after="terminal")
        elif phase == "claimed":
            self._prepare(entry)
        elif phase == "prepared":
            self._preflight(entry, "before_intent")
            prepared = entry["native_preparation"]
            self._event(entry, "submitting", {key: prepared[key] for key in
                ("script_sha256", "intent_sha256", "marker")}, phase_after="intent_ack")
        elif phase == "intent_ack":
            self._submit_once(entry)
        elif phase in ("submission_invoked", "reconciling"):
            if not self._lookup(entry) and not entry.get("unknown_reported"):
                self._event(entry, "submission_unknown", {"marker": entry["claim"]["task"]["marker"],
                    "reason": "Scheduler acknowledgement is ambiguous; marker reconciliation only"},
                    phase_after="reconciling")
                entry["unknown_reported"] = True
                self.save()
        elif phase == "job_known":
            self._event(entry, "submitted", {"slurm_job_id": entry["job_id"]}, phase_after="waiting")
        elif phase == "waiting":
            self._observe(entry)
        elif phase == "closed":
            self._closed(entry)
        elif phase == "publishing":
            self._publish(entry)
        else:
            raise ValueError("Unknown durable accuracy worker phase")

    def _archive_bindings(self, campaign, ledger):
        context = self.contexts.context(campaign["execution_context_id"])
        if (context["owner_sub"] != self.client.owner_sub or context["project_id"] != self.project
                or ledger["snapshot"]["campaign_instance_id"] != campaign["id"]
                or ledger["snapshot"]["scientific_request_hash"] != campaign["scientific_request_hash"]
                or ledger["snapshot"]["transport_request_hash"] != campaign["configuration_hash"]
                or ledger["snapshot"]["source_binding_sha256"] != context["source_binding_sha256"]):
            raise ValueError("Archive source/owner/request binding changed")
        for row in ledger["snapshot"]["allocations"]:
            if (row["status"] not in p.TERMINAL or row["allocated_gpu_seconds"] is None
                    or row["allocated_cpu_seconds"] is None or row["reserved_gpu_seconds"] != 0):
                raise AccuracyWorkerBlocked("Archive requires all original actual costs")
        return context

    def _archive_one(self):
        records = self.state["archives"]
        active = [key for key, value in records.items() if value["phase"] != "verified"]
        page = self.client.campaigns(self.project, archived="false",
            cursor=self.state["campaign_cursor"], limit=50)
        self.state["campaign_cursor"] = page["next_cursor"]
        self.save()
        for campaign in page["campaigns"]:
            if (campaign.get("archive", {}).get("status") == "requested"
                    and campaign["id"] not in records):
                context = self.contexts.context(campaign["execution_context_id"])
                record = {"phase": "inventory", "context_id": context["id"],
                    "root": context["native_root"] + "/" + campaign["id"],
                    "pending_ack": None, "batch": 0}
                records[campaign["id"]] = record
                self.save()
        active = [key for key, value in records.items() if value["phase"] != "verified"]
        if active:
            index = self.state["sweep_cursor"] % len(active)
            cid = sorted(active)[index]
            campaign = self.client.get(self.project, cid)["campaign"]
            self._archive_tick(campaign, records[cid])

    def _archive_tick(self, campaign, record):
        cid = p.identifier(campaign["id"])
        if record["context_id"] != campaign["execution_context_id"]:
            raise ValueError("Archive execution context changed")
        context = self.contexts.context(record["context_id"])
        root = context["native_root"] + "/" + cid
        target = context["archive_root"] + "/" + cid
        if record["root"] != root:
            raise ValueError("Archive native source root changed")
        if record["pending_ack"] is not None:
            # Lost ACK replay is permitted even after cloud archival succeeded.
            response = self.client.archive_frozen(deepcopy(record["pending_ack"]))
            if response["campaign"]["archive"]["status"] != "verified":
                raise AccuracyWorkerBlocked("Cloud did not confirm the verified private archive")
            record["phase"] = "verified"
            self.save()
            return
        if campaign.get("archive", {}).get("status") != "requested":
            raise AccuracyWorkerBlocked("Archive is not currently requested")
        ledger = self.client.accounting(self.project, cid)
        self._archive_bindings(campaign, ledger)
        if record.get("ledger_sha256") not in (None, ledger["sha256"]):
            raise ValueError("Final accounting changed during archival")
        result = campaign.get("result") or {}
        result_binding = {"first_comparison_sha256": result.get("first_comparison_sha256"),
                          "final_accounting_sha256": result.get("accounting_sha256")}
        if record.get("result_binding") not in (None, result_binding):
            raise ValueError("Scientific result changed during archival")
        phase = record["phase"]
        if phase == "inventory":
            files, proofs = self._archive_snapshot(root)
            if any(item["path"] == "archive-manifest.json" for item in files) or len(files) > 10000:
                raise ArtifactIntegrityError("Unexpected or oversized native archive inventory")
            payload = {"schema": "aws_mesouq.benchmark_accuracy_archive_manifest.v1",
                "campaign_id": cid, "context_id": context["id"],
                "source_binding_sha256": context["source_binding_sha256"],
                "scientific_request_hash": campaign["scientific_request_hash"],
                "transport_request_hash": campaign["configuration_hash"],
                "allocation_snapshot_sha256": ledger["sha256"],
                "allocation_snapshot": deepcopy(ledger["snapshot"]),
                "method_seal_sha256": campaign.get("method_seal_sha256"),
                **result_binding, "original_files_sha256": p.checksum(files),
                "retained_outputs": files}
            blob = p.canonical(payload)
            manifest = {"path": "archive-manifest.json", "sha256": hashlib.sha256(blob).hexdigest(),
                        "size_bytes": len(blob)}
            record.update(ledger_sha256=ledger["sha256"], result_binding=result_binding,
                manifest=blob.decode(), manifest_ref=manifest, originals=files,
                files=sorted([*files, manifest], key=lambda item: item["path"]),
                source_proofs=proofs, phase="manifest", batch=0)
            self.save()
            return
        if phase == "manifest":
            self._archive_remote(self._immutable, root + "/archive-manifest.json",
                                 record["manifest"].encode())
            self._archive_remote(self.transport.archive_prepare, context["archive_root"], cid)
            record["phase"] = "copy"
            self.save()
            return
        batches = self._archive_batches(record["files"])
        if phase == "copy":
            index = record["batch"]
            if index >= len(batches):
                record.update(phase="verify", batch=0)
                self.save()
                return
            transfers = []
            for item in batches[index]:
                transfer = {"source_root": root, "source": item["path"], "destination": item["path"],
                            "sha256": item["sha256"], "size_bytes": item["size_bytes"]}
                if item["path"] in record["source_proofs"]:
                    transfer["source_link_group"] = record["source_proofs"][item["path"]]
                transfers.append(transfer)
            try:
                self.transport.archive_verify_batch(context["archive_root"], cid, transfers)
            except subprocess.CalledProcessError as error:
                if error.returncode == 255:
                    raise
                if error.returncode != 3:
                    raise ArtifactIntegrityError("Private archive verification refused an unsafe copy") from error
                fresh, proofs = self._archive_snapshot(root)
                expected = record["files"]
                if fresh != expected:
                    raise ArtifactIntegrityError("Native archive source changed after immutable inventory")
                record["source_proofs"] = proofs
                self.save()
                for transfer in transfers:
                    transfer.pop("source_link_group", None)
                    if transfer["source"] in proofs:
                        transfer["source_link_group"] = proofs[transfer["source"]]
                self._archive_remote(self.transport.archive_copy_batch,
                                     context["archive_root"], cid, transfers)
                self._archive_remote(self.transport.archive_verify_batch,
                                     context["archive_root"], cid, transfers)
            record["batch"] += 1
            self.save()
            return
        if phase == "verify":
            index = record["batch"]
            if index < len(batches):
                transfers = [{"destination": item["path"], "sha256": item["sha256"],
                              "size_bytes": item["size_bytes"]} for item in batches[index]]
                self._archive_remote(self.transport.archive_verify_batch,
                                     context["archive_root"], cid, transfers)
                record["batch"] += 1
                self.save()
                return
            copied, aliases = self._archive_target_snapshot(target)
            if copied != record["files"] or aliases:
                raise ArtifactIntegrityError("Archive must be a complete independent copy without hardlinks")
            originals = [item for item in copied if item["path"] != "archive-manifest.json"]
            unique_proofs = {p.checksum(value): value for value in record["source_proofs"].values()}
            closed_pairs = sum(len(value["paths"]) - 1 for value in unique_proofs.values())
            receipt = {"schema": "aws_mesouq.benchmark_accuracy_archive.v1",
                "campaign_id": cid, "context_id": context["id"],
                "source_binding_sha256": context["source_binding_sha256"],
                "scientific_request_hash": campaign["scientific_request_hash"],
                "transport_request_hash": campaign["configuration_hash"],
                "allocation_snapshot_sha256": record["ledger_sha256"],
                "method_seal_sha256": campaign.get("method_seal_sha256"),
                "manifest_sha256": record["manifest_ref"]["sha256"],
                "source_snapshot_sha256": p.checksum(record["originals"]),
                "copy_snapshot_sha256": p.checksum(originals),
                "file_count": len(copied), "total_bytes": sum(item["size_bytes"] for item in copied),
                "closed_pair_count": closed_pairs, "independent_copy": True,
                "no_hardlinks": True, "aliases_closed": True, **record["result_binding"]}
            # The seal pin is obtained from the immutable admitted task chain;
            # it is absent only for a mechanically failed pre-seal campaign.
            matches = [entry for entry in self.state["tasks"].values()
                       if entry["claim"]["campaign"]["id"] == cid]
            seals = set()
            for entry in matches:
                pins = [entry["claim"]["task"]["manifest"]["pins"]]
                publication = entry.get("publication")
                if publication is not None:
                    pins.append(publication["result"]["output_pins"])
                for pin in pins:
                    if pin["method_seal_sha256"] is not None:
                        seals.add(pin["method_seal_sha256"])
            if len(seals) > 1:
                raise ValueError("Archive task history contains conflicting method seals")
            expected_seal = campaign.get("method_seal_sha256")
            if expected_seal is not None:
                p.sha(expected_seal)
            if seals and seals != {expected_seal}:
                raise ValueError("Archive local history differs from the authoritative method seal")
            receipt["method_seal_sha256"] = expected_seal
            record["pending_ack"] = self.client.build_archive_body(self.project, cid,
                "accuracy-archive-" + uuid4().hex, receipt)
            self.save()
            return
        raise ValueError("Unknown bounded archive phase")

    def sweep(self):
        """One bounded fair pass; the supervising CLI owns looping/authentication."""
        active = [entry for entry in self.state["tasks"].values()
                  if entry["phase"] not in ("terminal", "blocked_unfundable")]
        start = self.state["sweep_cursor"] % max(1, len(active))
        ordered = active[start:] + active[:start]
        failures = []
        for entry in ordered[:self.maximum_active]:
            try:
                self.tick(entry)
                entry["last_error"] = None
            except Exception as exc:
                if self._fatal(exc):
                    self.save()
                    raise
                # Retain exact pending intent/body and continue other campaigns.
                # Never log presigned URLs, tokens or exception response bodies.
                entry["last_error"] = type(exc).__name__
                failures.append({"task_id": entry["claim"]["task"]["id"], "error_type": type(exc).__name__})
            self.save()
        self.state["sweep_cursor"] += 1
        if len(active) < self.maximum_active:
            try:
                self._claim_one()
            except Exception as exc:
                if self._fatal(exc):
                    self.save()
                    raise
                failures.append({"task_id": None, "error_type": type(exc).__name__})
        try:
            self._archive_one()
        except Exception as exc:
            if self._fatal(exc):
                self.save()
                raise
            failures.append({"task_id": None, "error_type": type(exc).__name__, "operation": "archive"})
        self.save()
        return {"active": len(active), "failures": failures,
            "blocked_unfundable": sum(entry["phase"] == "blocked_unfundable"
                for entry in self.state["tasks"].values())}
