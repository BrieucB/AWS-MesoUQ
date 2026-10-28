"""Framework-free HTTP control plane with explicit tenant and lease boundaries."""

import copy
import hashlib
import json
import math
import re
import secrets
import time
import uuid
from .storage import Conflict
from aws_mesouq.contracts import CANONICAL_PROFILE

ARCHIVE_RETENTION_SECONDS = 30 * 24 * 60 * 60
ARCHIVE_TAG_BATCH_SIZE = 64
CURVE_UPLOAD_MAX_BYTES = 1_048_576
RESULT_UPLOAD_MAX_BYTES = 25 * 1_048_576
ARTIFACT_TOTAL_MAX_BYTES = 1_073_741_824
ARTIFACT_USER_MAX_BYTES = 256 * 1_048_576
ARTIFACT_MAX_COUNT = 1000
PENDING_UPLOAD_SECONDS = 24 * 60 * 60

TERMINAL = {"succeeded", "failed", "cancelled"}
KINDS = {
    "curve",
    "training_table",
    "result",
    "posterior",
    "model",
    "validation",
    "log",
    "manifest",
}
TRANSITIONS = {
    "submitting": {"claimed"},
    "submitted": {"submitting", "submission_unknown", "cancelling"},
    "running": {"queued"},
    "succeeded": {"running", "queued"},
    "failed": {
        "claimed",
        "submitting",
        "queued",
        "running",
        "cancelling",
        "submission_unknown",
    },
    "cancelled": {
        "cancelling",
        "claimed",
        "queued",
        "running",
        "submitting",
        "submission_unknown",
    },
    "submission_unknown": {"submitting", "queued", "running", "cancelling"},
}


class APIError(Exception):
    def __init__(self, status, code, message):
        super().__init__(message)
        self.status, self.code, self.message = status, code, message


def fail(status, code, message):
    raise APIError(status, code, message)


def digest(value):
    return hashlib.sha256(
        json.dumps(
            value, sort_keys=True, separators=(",", ":"), allow_nan=False
        ).encode()
    ).hexdigest()


def require_keys(body, required, optional=()):
    if (
        not isinstance(body, dict)
        or set(body) - set(required) - set(optional)
        or set(required) - set(body)
    ):
        fail(400, "invalid_request", "Unexpected or missing fields")


def text(value, maximum=200):
    return (
        isinstance(value, str)
        and 0 < len(value) <= maximum
        and not any(ord(c) < 32 for c in value)
    )


def safe_json(value, depth=0):
    if depth > 12:
        fail(400, "invalid_configuration", "Configuration nesting limit exceeded")
    if isinstance(value, dict):
        for key, item in value.items():
            if not text(key, 80) or key.lower() in (
                "command",
                "script",
                "pickle",
                "path",
                "shell",
                "sbatch",
                "directive",
                "credential",
            ):
                fail(
                    400,
                    "invalid_configuration",
                    "Executable or path configuration is forbidden",
                )
            safe_json(item, depth + 1)
    elif isinstance(value, list):
        if len(value) > 1000:
            fail(400, "invalid_configuration", "Configuration list limit exceeded")
        for item in value:
            safe_json(item, depth + 1)
    elif isinstance(value, float) and not math.isfinite(value):
        fail(400, "invalid_configuration", "Nonfinite number")
    elif not isinstance(value, (str, int, float, bool, type(None))):
        fail(400, "invalid_configuration", "Invalid JSON value")


class Application:
    def __init__(
        self,
        store,
        objects,
        *,
        clock=time.time,
        lease_seconds=120,
        validate_configuration=None,
        validate_result=None,
        notifications=None,
        release_policy="v1",
    ):
        if release_policy not in ("v1", "nonproduction_legacy"):
            raise ValueError("Unknown release policy")
        # Constructor-only compatibility for internal lifecycle tests. The HTTP
        # handler always selects v1; no request field or environment can opt out.
        self.release_policy = release_policy
        self.store, self.objects, self.clock = store, objects, clock
        self.lease_seconds = lease_seconds
        self.notifications = notifications
        if validate_configuration is None or validate_result is None:
            from aws_mesouq.contracts import (
                validate_job_configuration,
                validate_result as canonical_validate_result,
            )

            validate_configuration = (
                validate_configuration or validate_job_configuration
            )
            validate_result = validate_result or canonical_validate_result
        self.validate_configuration, self.validate_result = (
            validate_configuration,
            validate_result,
        )

    def handle(self, method, path, claims=None, body=None, *, remaining_ms=None):
        if path == "/health" and method == "GET":
            return 200, {"status": "ok"}
        if not claims or not text(claims.get("sub")):
            fail(401, "unauthenticated", "Verified access token required")
        if body is None:
            body = {}
        if len(json.dumps(body, allow_nan=False).encode()) > 200_000:
            fail(413, "request_too_large", "Request exceeds metadata size limit")
        for _ in range(8):
            revision, state = self.store.load()
            before = copy.deepcopy(state)
            identity = state["identities"].get(claims["sub"])
            if not identity or identity.get("disabled"):
                fail(403, "not_invited", "An active invitation is required")
            # Roles and project membership are never accepted from token groups/body.
            status, result = self.route(
                method, path.rstrip("/"), claims["sub"], identity, body, state
            )
            if state == before:
                self.finalize(state, remaining_ms=remaining_ms)
                return status, result
            try:
                self.store.save(revision, before, state)
                self.finalize(state, remaining_ms=remaining_ms)
                if self.notifications:
                    self.notifications.changed(revision + 1)
                return status, result
            except Conflict:
                continue
        fail(
            409,
            "concurrent_update",
            "Concurrent update; retry with the same idempotency key",
        )

    def archive_due(self, artifact):
        verified = artifact.get("archive_verified_at")
        return (
            type(verified) in (int, float)
            and math.isfinite(verified)
            and not artifact.get("archive_tagged")
            and self.clock() >= verified + ARCHIVE_RETENTION_SECONDS
        )

    def acknowledge_archive_tags(self, artifacts):
        if not artifacts:
            return
        for _ in range(8):
            revision, current = self.store.load()
            before = copy.deepcopy(current)
            for artifact in artifacts:
                current["artifacts"][artifact["id"]]["archive_tagged"] = True
            try:
                self.store.save(revision, before, current)
                return
            except Conflict:
                continue
        raise Conflict("Lifecycle reconciliation busy; retry safely")

    def finalize(self, state, *, remaining_ms=None):
        # Receipt commit precedes tags, and the trusted server clock must reach
        # thirty days AFTER verification regardless of the object's creation age.
        pending = [a for a in state["artifacts"].values() if self.archive_due(a)][
            :ARCHIVE_TAG_BATCH_SIZE
        ]
        tagged = []
        try:
            for artifact in pending:
                # S3 uses one attempt with <=2s connect/3s read timeouts. Reserve
                # additional time for conditional acknowledgement of this prefix.
                if remaining_ms is not None and remaining_ms() < 12_000:
                    break
                if self.archive_due(artifact):
                    self.objects.archived(artifact["storage_key"])
                    tagged.append(artifact)
        except Exception:
            # A slow/failing suffix must not starve an already successful prefix.
            self.acknowledge_archive_tags(tagged)
            raise
        self.acknowledge_archive_tags(tagged)
        return len(tagged)

    def maintenance(self, *, remaining_ms=None):
        """Private scheduled reconciliation; never exposed as an HTTP route.

        Batches stay below DynamoDB's transaction bound. Failed or interrupted
        tagging is safe to retry because the receipt/clock is already durable.
        """
        self.reconcile_upload_reservations(remaining_ms=remaining_ms)
        tagged = 0
        for _ in range(16):
            if remaining_ms is not None and remaining_ms() < 12_000:
                break
            _, state = self.store.load()
            count = self.finalize(state, remaining_ms=remaining_ms)
            tagged += count
            if count == 0:
                return {
                    "status": "ok",
                    "tagged": tagged,
                    "remaining_due": any(
                        self.archive_due(a) for a in state["artifacts"].values()
                    ),
                }
        _, state = self.store.load()
        return {
            "status": "ok",
            "tagged": tagged,
            "remaining_due": any(
                self.archive_due(a) for a in state["artifacts"].values()
            ),
        }

    def reconcile_upload_reservations(self, *, remaining_ms=None):
        """Expire pending reservations before deleting, retaining all summaries.

        The conditional expiry commit races safely with upload completion: only
        one can win. Deletion/acknowledgement retries cannot delete verified data.
        Mature archived copies release capacity only after S3 confirms absence.
        """
        if self.release_policy != "v1":
            return
        for _ in range(8):
            revision, state = self.store.load()
            before = copy.deepcopy(state)
            pending = [a for a in state["artifacts"].values()
                       if a.get("verified") is False and "upload_expired_at" not in a
                       and self.clock() >= a.get("created_at", self.clock()) + PENDING_UPLOAD_SECONDS][:ARCHIVE_TAG_BATCH_SIZE]
            for artifact in pending:
                artifact["upload_expired_at"] = self.clock()
            if not pending:
                break
            try:
                self.store.save(revision, before, state)
                break
            except Conflict:
                continue
        else:
            raise Conflict("Upload expiry reconciliation busy")
        _, state = self.store.load()
        released = []
        for artifact in state["artifacts"].values():
            if len(released) >= ARCHIVE_TAG_BATCH_SIZE or (remaining_ms is not None and remaining_ms() < 12_000):
                break
            if artifact.get("reservation_released"):
                continue
            if "upload_expired_at" in artifact and artifact.get("verified") is False:
                self.objects.discard(artifact["storage_key"])
                released.append(artifact["id"])
            elif artifact.get("archive_tagged") and artifact.get("verified") is True:
                if self.objects.absent(artifact["storage_key"]):
                    released.append(artifact["id"])
        if not released:
            return
        for _ in range(8):
            revision, state = self.store.load()
            before = copy.deepcopy(state)
            for ident in released:
                artifact = state["artifacts"][ident]
                if "upload_expired_at" in artifact or artifact.get("archive_tagged"):
                    artifact["reservation_released"] = True
            try:
                self.store.save(revision, before, state)
                return
            except Conflict:
                continue
        raise Conflict("Upload reservation reconciliation busy")

    def member(self, state, identity, project):
        role = identity.get("projects", {}).get(project)
        if role not in ("member", "maintainer") or project not in state["projects"]:
            fail(404, "not_found", "Project not found")
        return role

    def public_job(self, job):
        return {
            key: value
            for key, value in job.items()
            if key not in ("lease", "last_event", "request_hash")
        } | ({"lease_expires_at": job["lease"]["expires_at"]} if "lease" in job else {})

    def route(self, method, path, sub, identity, body, state):
        from .benchmark import BenchmarkRoutes, is_benchmark_path

        if is_benchmark_path(path):
            return BenchmarkRoutes(self).route(method, path, sub, identity, body, state)
        now = int(self.clock())
        if path in ("/v1/me", "/v1/projects") and method == "GET":
            projects = [
                {**state["projects"][pid], "id": pid, "role": role}
                for pid, role in identity.get("projects", {}).items()
                if pid in state["projects"] and role in ("member", "maintainer")
            ]
            return 200, (
                {"projects": projects}
                if path.endswith("projects")
                else {
                    "subject": sub,
                    "email": identity.get("email"),
                    "roles": sorted(set(p["role"] for p in projects)),
                    "projects": projects,
                }
            )
        if path == "/v1/connector/claim" and method == "POST":
            require_keys(
                body,
                ("project_id", "connector_id", "site"),
                ("archive_capable", "exclude_job_ids"),
            )
            if type(body.get("archive_capable", False)) is not bool:
                fail(400, "invalid_connector", "archive_capable must be a JSON boolean")
            exclusions = body.get("exclude_job_ids", [])
            if (
                not isinstance(exclusions, list)
                or len(exclusions) > 10
                or any(
                    not isinstance(ident, str)
                    or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,127}", ident)
                    for ident in exclusions
                )
                or len(set(exclusions)) != len(exclusions)
            ):
                fail(
                    400,
                    "invalid_connector",
                    "exclude_job_ids must contain at most ten unique valid job IDs",
                )
            excluded_ids = set(exclusions)
            self.member(state, identity, body["project_id"])
            if not text(body["connector_id"]) or body["site"] not in (
                "karolina",
                "vega",
            ):
                fail(
                    400,
                    "invalid_connector",
                    "A connector identity and supported site are required",
                )
            candidates = sorted(
                state["jobs"].values(),
                key=lambda j: (
                    (
                        2
                        if j["status"] in TERMINAL
                        else 1 if j["status"] == "pending" else 0
                    ),
                    j["created_at"],
                ),
            )
            for job in candidates:
                if job["project_id"] != body["project_id"] or job["owner_sub"] != sub:
                    continue
                # Resume expired work with the recorded scheduler/submission state intact.
                # Only an unsubmitted claimed job can return to a fresh claim.
                lease = job.get("lease", {})
                if job["status"] in TERMINAL and (
                    not body.get("archive_capable", False)
                    or job["status"] != "succeeded"
                    or job.get("site") != "karolina"
                    or job.get("archive")
                    or not lease
                ):
                    continue
                if (
                    job["status"] != "pending"
                    and lease.get("expires_at", 0) > now
                    and lease.get("connector_id") != body["connector_id"]
                ):
                    continue
                if job.get("site") and job["site"] != body["site"]:
                    continue
                if job["id"] in excluded_ids:
                    continue
                token = secrets.token_urlsafe(32)
                if job["status"] == "pending":
                    job["status"] = "claimed"
                elif job["status"] == "submitting":
                    job["status"] = "submission_unknown"
                job["lease"] = {
                    "hash": hashlib.sha256(token.encode()).hexdigest(),
                    "connector_id": body["connector_id"],
                    "expires_at": now + self.lease_seconds,
                    "archive_capable": body.get("archive_capable", False),
                }
                job["site"] = body["site"]
                job["revision"] += 1
                job["updated_at"] = now
                return 200, {
                    "job": self.public_job(job),
                    "lease_token": token,
                    "lease_expires_at": job["lease"]["expires_at"],
                }
            return 200, {"job": None}
        event_match = re.fullmatch(r"/v1/connector/jobs/([^/]+)/events", path)
        if event_match and method == "POST":
            return self.event(state, sub, identity, event_match[1], body, now)
        match = re.fullmatch(
            r"/v1/projects/([^/]+)/(jobs|artifacts|models)(?:/([^/]+))?(?:/(cancel|result|complete|release))?",
            path,
        )
        if not match:
            fail(404, "not_found", "Route not found")
        project, resource, ident, action = match.groups()
        role = self.member(state, identity, project)
        if resource == "jobs":
            if ident is None:
                if method == "GET":
                    return 200, {
                        "jobs": [
                            {
                                key: value
                                for key, value in self.public_job(j).items()
                                if key != "result"
                            }
                            for j in state["jobs"].values()
                            if j["project_id"] == project
                        ]
                    }
                if method == "POST":
                    return self.create_job(state, sub, project, role, body, now)
            job = state["jobs"].get(ident)
            if not job or job["project_id"] != project:
                fail(404, "not_found", "Job not found")
            if method == "GET" and action is None:
                return 200, self.public_job(job)
            if method == "GET" and action == "result":
                if job["status"] != "succeeded" or "result" not in job:
                    fail(409, "not_ready", "Result is not available")
                return 200, job["result"]
            if method == "POST" and action == "cancel":
                require_keys(body, ())
                if job["owner_sub"] != sub:
                    fail(403, "owner_required", "Only the job owner may cancel")
                if job["status"] not in TERMINAL:
                    job["status"] = (
                        "cancelled" if job["status"] == "pending" else "cancelling"
                    )
                    job["revision"] += 1
                    job["updated_at"] = now
                return 200, self.public_job(job)
        elif resource == "artifacts":
            if method == "POST" and ident is None:
                return self.create_artifact(state, sub, project, body, now)
            artifact = state["artifacts"].get(ident)
            if not artifact or artifact["project_id"] != project:
                fail(404, "not_found", "Artifact not found")
            if method == "POST" and action == "complete":
                require_keys(body, ())
                if artifact["owner_sub"] != sub:
                    fail(403, "owner_required", "Only the uploader may complete")
                if "upload_expired_at" in artifact or artifact.get("reservation_released"):
                    fail(409, "upload_expired", "Upload reservation expired; create a new upload")
                if not self.objects.verify(artifact["storage_key"], artifact):
                    fail(
                        409,
                        "checksum_mismatch",
                        "Stored object does not match declared checksum and size",
                    )
                if self.release_policy == "v1" and artifact["kind"] == "curve":
                    from aws_mesouq.release import validate_curve_csv

                    try:
                        validate_curve_csv(self.objects.read_bytes(
                            artifact["storage_key"], artifact, maximum=CURVE_UPLOAD_MAX_BYTES
                        ))
                    except (ValueError, TypeError, UnicodeError) as exc:
                        fail(400, "invalid_curve", str(exc))
                artifact["verified"] = True
                return 200, self.public_artifact(artifact)
            if method == "GET" and action is None:
                if not artifact["verified"]:
                    fail(409, "not_ready", "Upload has not been verified")
                if not self.objects.verify(artifact["storage_key"], artifact):
                    fail(
                        410,
                        "artifact_unavailable",
                        "Cloud object expired or is unavailable; consult the Karolina archive receipt",
                    )
                return 200, {
                    "artifact": self.public_artifact(artifact),
                    "download_url": self.objects.download_url(artifact["storage_key"]),
                }
        elif resource == "models":
            if self.release_policy == "v1" and action == "release":
                fail(404, "not_found", "Route not found")
            if method == "GET" and ident is None:
                return 200, {
                    "models": [
                        m
                        for m in state["models"].values()
                        if m["project_id"] == project
                    ]
                }
            if method == "POST" and action == "release":
                require_keys(body, ())
                if role != "maintainer":
                    fail(403, "maintainer_required", "Maintainer role required")
                model = state["models"].get(f"{project}:{ident}")
                if not model or model["project_id"] != project:
                    fail(404, "not_found", "Model not found")
                if (
                    not model.get("trusted")
                    or model.get("validation", {}).get("passed") is not True
                    or model.get("format")
                    not in ("safetensors", "onnx", "mesouq_checkpoint")
                ):
                    fail(
                        409,
                        "model_not_validated",
                        "Trusted immutable model and passing validation required",
                    )
                if model.get("status") == "released":
                    return 200, model
                for artifact_id in (
                    model.get("artifact_id"),
                    model.get("validation_artifact_id"),
                ):
                    artifact = state["artifacts"].get(artifact_id)
                    if (
                        not artifact
                        or artifact["project_id"] != project
                        or not artifact["verified"]
                        or not self.objects.verify(artifact["storage_key"], artifact)
                    ):
                        fail(
                            409,
                            "model_not_validated",
                            "Verified immutable model and validation artifacts required",
                        )
                model_artifact = state["artifacts"][model["artifact_id"]]
                if (
                    model_artifact["sha256"] != model.get("sha256")
                    or model_artifact["kind"] != "model"
                    or state["artifacts"][model["validation_artifact_id"]]["kind"]
                    != "validation"
                ):
                    fail(
                        409,
                        "model_not_validated",
                        "Model checksum and validation artifact kinds must match registry",
                    )
                model["status"] = "released"
                model.setdefault("released_at", now)
                model.setdefault("released_by", sub)
                return 200, model
        fail(405, "method_not_allowed", "Method not allowed")

    def public_artifact(self, artifact):
        return {key: value for key, value in artifact.items() if key != "storage_key"}

    def create_artifact(self, state, sub, project, body, now):
        require_keys(
            body, ("name", "kind", "sha256", "size_bytes", "media_type"), ("metadata",)
        )
        if (
            not text(body["name"], 255)
            or body["kind"] not in KINDS
            or not isinstance(body["sha256"], str)
            or not re.fullmatch("[0-9a-f]{64}", body["sha256"])
            or type(body["size_bytes"]) is not int
            or not 0 < body["size_bytes"] <= 1_000_000_000
            or not text(body["media_type"], 120)
        ):
            fail(400, "invalid_artifact", "Invalid artifact metadata")
        if "metadata" in body and not isinstance(body["metadata"], dict):
            fail(400, "invalid_artifact", "Metadata must be an object")
        if self.release_policy == "v1":
            if body["kind"] in ("training_table", "model", "validation"):
                fail(400, "invalid_artifact", "Artifact kind is unavailable in release v1")
            if body["kind"] == "curve" and (
                body["media_type"] != "text/csv" or not body["name"].lower().endswith(".csv")
            ):
                fail(400, "invalid_artifact", "Release v1 requires CSV mechanical curves")
            maximum = CURVE_UPLOAD_MAX_BYTES if body["kind"] == "curve" else RESULT_UPLOAD_MAX_BYTES
            if body["size_bytes"] > maximum:
                fail(413, "artifact_too_large", "Artifact exceeds the release upload size limit")
            retained = [a for a in state["artifacts"].values() if not a.get("reservation_released")]
            owned = [a for a in retained if a["owner_sub"] == sub]
            project_artifacts = [a for a in retained if a["project_id"] == project]
            if (
                len(retained) >= ARTIFACT_MAX_COUNT
                or sum(a["size_bytes"] for a in retained) + body["size_bytes"] > ARTIFACT_TOTAL_MAX_BYTES
                or sum(a["size_bytes"] for a in project_artifacts) + body["size_bytes"] > ARTIFACT_TOTAL_MAX_BYTES
                or sum(a["size_bytes"] for a in owned) + body["size_bytes"] > ARTIFACT_USER_MAX_BYTES
            ):
                fail(429, "artifact_quota_exceeded", "Retained upload reservations exceed the release quota")
        ident = uuid.uuid4().hex
        artifact = {
            **body,
            "id": ident,
            "owner_sub": sub,
            "project_id": project,
            "verified": False,
            "created_at": now,
            "storage_key": f"projects/{project}/artifacts/{ident}",
        }
        state["artifacts"][ident] = artifact
        url, headers = self.objects.upload_url(artifact["storage_key"], artifact)
        return 201, {
            "artifact": self.public_artifact(artifact),
            "upload_url": url,
            "required_headers": headers,
        }

    def create_job(self, state, sub, project, role, body, now):
        require_keys(
            body,
            (
                "schema_version",
                "kind",
                "model_version",
                "diameter_um",
                "configuration",
                "input_artifacts",
                "idempotency_key",
            ),
        )
        if (
            body["schema_version"] != 1
            or type(body["schema_version"]) is not int
            or body["kind"] not in ("prediction", "calibration", "training")
            or type(body["diameter_um"]) not in (int, float)
            or body["diameter_um"] not in (3.2, 3.4, 5.8)
            or not isinstance(body["configuration"], dict)
            or not text(body["model_version"])
            or not text(body["idempotency_key"], 128)
        ):
            fail(400, "invalid_job", "Invalid scientific job specification")
        if body["kind"] == "training" and role != "maintainer":
            fail(403, "maintainer_required", "Training requires maintainer role")
        safe_json(body["configuration"])
        normalized_configuration = copy.deepcopy(body["configuration"])
        if self.release_policy == "v1":
            from aws_mesouq.release import validate_configuration

            try:
                normalized_configuration = validate_configuration(
                    body["configuration"], kind=body["kind"], diameter_um=body["diameter_um"]
                )
            except (ValueError, TypeError) as exc:
                fail(400, "invalid_configuration", str(exc))
        if self.validate_configuration:
            try:
                normalized_configuration = self.validate_configuration(
                    body["configuration"],
                    kind=body["kind"],
                    diameter_um=body["diameter_um"],
                )
            except (ValueError, TypeError) as exc:
                fail(400, "invalid_configuration", str(exc))
        request_id = digest([project, sub, body["idempotency_key"]])
        existing = state["requests"].get(request_id)
        if existing:
            if existing["hash"] != digest(body):
                fail(
                    409,
                    "idempotency_conflict",
                    "Idempotency key was used with different content",
                )
            return 202, self.public_job(state["jobs"][existing["job_id"]])
        model = state["models"].get(f"{project}:{body['model_version']}")
        if not model or not model.get("trusted") or model.get("status") != "released":
            fail(
                400, "invalid_model", "An immutable trusted released model is required"
            )
        requested_profile = normalized_configuration.get("scientific_profile")
        model_provenance = model.get("provenance", {})
        binding_present = (
            isinstance(model_provenance, dict)
            and "pipeline_binding" in model_provenance
        )
        pipeline_binding = (
            model_provenance.get("pipeline_binding")
            if isinstance(model_provenance, dict)
            else None
        )
        if binding_present or requested_profile == CANONICAL_PROFILE:
            if (
                not isinstance(pipeline_binding, dict)
                or pipeline_binding.get("id") != CANONICAL_PROFILE
                or requested_profile != pipeline_binding["id"]
            ):
                fail(
                    400,
                    "invalid_model",
                    "Scientific profile requires its authoritative trusted model pipeline binding",
                )
        selected_diameters = body["configuration"].get(
            "emb_diameters", [body["diameter_um"]]
        )
        checksums = model.get("sha256")
        if isinstance(checksums, str):
            if (
                model.get("diameter_um") != body["diameter_um"]
                or any(d != model["diameter_um"] for d in selected_diameters)
                or not re.fullmatch("[a-f0-9]{64}", checksums)
            ):
                fail(
                    400,
                    "invalid_model",
                    "Single-checkpoint model must bind the selected diameter and SHA256",
                )
        elif isinstance(checksums, dict):
            if any(
                not isinstance(checksums.get(f"{float(d):g}"), str)
                or not re.fullmatch("[a-f0-9]{64}", checksums[f"{float(d):g}"])
                for d in selected_diameters
            ):
                fail(
                    400,
                    "invalid_model",
                    "Model bundle requires an immutable SHA256 for every selected diameter",
                )
        else:
            fail(
                400,
                "invalid_model",
                "Model registry is missing immutable checkpoint hashes",
            )
        approved_model = {
            key: copy.deepcopy(model[key])
            for key in (
                "sha256",
                "format",
                "provenance",
                "validation",
                "diameter_um",
                "diameters_um",
                "source_job_id",
                "approved_adapter_sha256",
            )
            if key in model
        }
        approved_model["version"] = body["model_version"]
        if self.release_policy == "v1" and (
            not isinstance(approved_model.get("approved_adapter_sha256"), str)
            or not re.fullmatch(r"[a-f0-9]{64}", approved_model["approved_adapter_sha256"])
        ):
            fail(400, "invalid_model", "Release v1 requires an operator-pinned adapter SHA256")
        artifacts = body["input_artifacts"]
        if not isinstance(artifacts, list) or not 1 <= len(artifacts) <= 100:
            fail(
                400,
                "invalid_artifacts",
                "One to one hundred verified input artifacts are required",
            )
        authoritative_refs = []
        for ref in artifacts:
            artifact = (
                state["artifacts"].get(ref.get("id")) if isinstance(ref, dict) else None
            )
            if (
                not artifact
                or artifact["project_id"] != project
                or not artifact["verified"]
                or artifact["kind"] not in ("curve", "training_table")
            ):
                fail(
                    400,
                    "invalid_artifacts",
                    "Artifact must be a verified project curve or training table",
                )
            if not self.objects.verify(artifact["storage_key"], artifact):
                fail(
                    400,
                    "invalid_artifacts",
                    "Verified artifact is missing or its checksum changed",
                )
            if ref.get("metadata", {}) != artifact.get("metadata", {}):
                fail(
                    400,
                    "invalid_artifacts",
                    "Artifact metadata does not match verified upload metadata",
                )
            authoritative_refs.append(
                {
                    key: copy.deepcopy(artifact[key])
                    for key in (
                        "id",
                        "name",
                        "kind",
                        "sha256",
                        "size_bytes",
                        "media_type",
                        "metadata",
                    )
                    if key in artifact
                }
            )
            for key in ("name", "kind", "sha256", "size_bytes", "media_type"):
                if ref.get(key) != artifact[key]:
                    fail(
                        400,
                        "invalid_artifacts",
                        "Artifact reference does not match verified metadata",
                    )
        from aws_mesouq.contracts import validate_job_artifacts

        try:
            if self.release_policy == "v1":
                from aws_mesouq.release import validate_artifacts

                validate_artifacts(
                    authoritative_refs,
                    configuration=normalized_configuration,
                    diameter_um=body["diameter_um"],
                )
            validate_job_artifacts(
                authoritative_refs,
                kind=body["kind"],
                configuration=normalized_configuration,
                diameter_um=body["diameter_um"],
            )
        except (ValueError, TypeError) as exc:
            fail(400, "invalid_artifacts", str(exc))
        if sum(j["status"] not in TERMINAL for j in state["jobs"].values()) >= 10:
            fail(
                429, "capacity_exceeded", "Ten active calculations are already admitted"
            )
        ident = uuid.uuid4().hex
        job = {
            **body,
            "configuration": normalized_configuration,
            "approved_model": approved_model,
            "input_artifacts": authoritative_refs,
            "id": ident,
            "project_id": project,
            "owner_sub": sub,
            "status": "pending",
            "created_at": now,
            "updated_at": now,
            "revision": 1,
        }
        state["jobs"][ident] = job
        state["requests"][request_id] = {"hash": digest(body), "job_id": ident}
        return 202, self.public_job(job)

    def event(self, state, sub, identity, ident, body, now):
        require_keys(
            body,
            ("project_id", "connector_id", "lease_token", "event_id", "type"),
            ("slurm_job_id", "detail", "result", "archive", "archive_signature", "result_signature"),
        )
        self.member(state, identity, body["project_id"])
        job = state["jobs"].get(ident)
        if not job or job["project_id"] != body["project_id"]:
            fail(404, "not_found", "Job not found")
        if job["owner_sub"] != sub:
            fail(403, "owner_required", "Connector can only update caller-owned jobs")
        if (
            not text(body["event_id"], 128)
            or not text(body["lease_token"])
            or not text(body["connector_id"])
        ):
            fail(400, "invalid_event", "Invalid event identity")
        event_key = digest([ident, body["event_id"]])
        event_hash = digest(
            {key: value for key, value in body.items() if key != "lease_token"}
        )
        previous = state["events"].get(event_key)
        if previous:
            if previous["hash"] != event_hash:
                fail(409, "event_conflict", "Event ID was used with different content")
            return 200, self.public_job(job)
        lease = job.get("lease", {})
        if (
            lease.get("connector_id") != body["connector_id"]
            or lease.get("expires_at", 0) <= now
            or not secrets.compare_digest(
                lease.get("hash", ""),
                hashlib.sha256(body["lease_token"].encode()).hexdigest(),
            )
        ):
            fail(
                403,
                "invalid_lease",
                "Connector lease is absent, expired or belongs to another connector",
            )
        kind = body["type"]
        if kind == "heartbeat":
            lease["expires_at"] = now + self.lease_seconds
        elif kind == "archive_verified":
            receipt = body.get("archive")
            if self.release_policy == "v1":
                from .attestation import verify_archive

                try:
                    verify_archive(job, identity, body)
                except (ValueError, TypeError) as exc:
                    fail(400, "invalid_archive", str(exc))
            if (
                job["status"] not in TERMINAL
                or not isinstance(receipt, dict)
                or receipt.get("site") != "karolina"
                or not text(receipt.get("path"), 2048)
                or not isinstance(receipt.get("artifacts"), list)
                or not receipt["artifacts"]
            ):
                fail(
                    400,
                    "invalid_archive",
                    "Terminal job requires a Karolina checksum archive receipt",
                )
            expected = {ref["id"] for ref in job["input_artifacts"]}
            expected.update(
                ref["id"] for ref in job.get("result", {}).get("artifacts", [])
            )
            refs = receipt["artifacts"]
            if {r.get("id") for r in refs if isinstance(r, dict)} != expected or len(
                refs
            ) != len(expected):
                fail(
                    400,
                    "invalid_archive",
                    "Receipt must include every input and output artifact exactly once",
                )

            def archive_core(value):
                return {
                    "site": value["site"],
                    "path": value["path"],
                    "artifacts": sorted(value["artifacts"], key=lambda ref: ref["id"]),
                }

            if job.get("archive"):
                if digest(archive_core(job["archive"])) != digest(
                    archive_core(receipt)
                ):
                    fail(
                        409,
                        "archive_conflict",
                        "An accepted archive receipt is immutable",
                    )
                # A new event ID for the same attestation must not rearm clocks.
            else:
                checked = []
                for ref in refs:
                    artifact = state["artifacts"].get(ref["id"])
                    if (
                        not artifact
                        or artifact["project_id"] != job["project_id"]
                        or not artifact["verified"]
                        or ref.get("sha256") != artifact["sha256"]
                        or ref.get("size_bytes") != artifact["size_bytes"]
                        or not self.objects.verify(artifact["storage_key"], artifact)
                    ):
                        fail(
                            400,
                            "invalid_archive",
                            "Archive checksum/size does not match verified cloud artifact",
                        )
                    checked.append(artifact)
                # Preserve fractional seconds: truncation could expire copies early.
                verified_at = self.clock()
                job["archive"] = {
                    **receipt,
                    "verified_by": sub,
                    "verified_at": verified_at,
                }
                for artifact in checked:
                    # Shared artifacts use their FIRST accepted verified receipt.
                    artifact.setdefault("archive_verified_at", verified_at)
                    artifact.setdefault(
                        "archive_eligible_at",
                        artifact["archive_verified_at"] + ARCHIVE_RETENTION_SECONDS,
                    )
        elif kind in TRANSITIONS:
            if job["status"] not in TRANSITIONS[kind]:
                fail(
                    409, "invalid_transition", f"Cannot apply {kind} to {job['status']}"
                )
            if kind == "submitted":
                scheduler_id = body.get("slurm_job_id")
                if not isinstance(scheduler_id, str) or not re.fullmatch(
                    "[1-9][0-9]{0,19}", scheduler_id
                ):
                    fail(
                        400,
                        "invalid_scheduler_id",
                        "A numeric scheduler ID is required",
                    )
                if job.get("slurm_job_id") not in (None, scheduler_id):
                    fail(
                        409,
                        "scheduler_conflict",
                        "Recorded scheduler identity is immutable",
                    )
                job["slurm_job_id"] = scheduler_id
            if kind == "succeeded":
                if self.release_policy == "v1":
                    from .attestation import verify_result

                    try:
                        verify_result(job, identity, body)
                    except (ValueError, TypeError) as exc:
                        fail(403, "invalid_connector_signature", str(exc))
                    if not job.get("slurm_job_id") or body.get("slurm_job_id") != job["slurm_job_id"]:
                        fail(400, "invalid_scheduler_id", "Completion must bind the recorded scheduler ID")
                result = body.get("result")
                if (
                    not isinstance(result, dict)
                    or result.get("schema_version") != 1
                    or type(result.get("schema_version")) is not int
                    or result.get("kind") != job["kind"]
                ):
                    fail(
                        400,
                        "invalid_result",
                        "Normalized result matching the job kind required",
                    )
                if self.validate_result:
                    try:
                        self.validate_result(result)
                    except (ValueError, TypeError) as exc:
                        fail(400, "invalid_result", str(exc))
                if self.release_policy == "v1" and (
                    result.get("interval_probability") != job["configuration"]["interval_probability"]
                ):
                    fail(400, "invalid_result", "Result interval must match the locked approved preset")
                if self.release_policy == "v1":
                    try:
                        self.validate_release_result(state, job, result)
                    except (ValueError, TypeError, KeyError, UnicodeError) as exc:
                        fail(400, "invalid_result", str(exc))
                provenance = result.get("provenance", {})
                if (
                    provenance.get("job_id") != job["id"]
                    or provenance.get("model_version") != job["model_version"]
                    or provenance.get("model_sha256") != job["approved_model"]["sha256"]
                    or provenance.get("data_sha256")
                    != {ref["id"]: ref["sha256"] for ref in job["input_artifacts"]}
                    or provenance.get("config_sha256") != digest(job["configuration"])
                ):
                    fail(
                        400,
                        "invalid_provenance",
                        "Result provenance does not match immutable job/model/data/configuration",
                    )
                approved_provenance = job["approved_model"].get("provenance", {})
                binding_expected = (
                    isinstance(approved_provenance, dict)
                    and "pipeline_binding" in approved_provenance
                )
                binding_returned = "pipeline_binding" in provenance
                if binding_expected != binding_returned or (
                    binding_expected
                    and digest(provenance["pipeline_binding"])
                    != digest(approved_provenance["pipeline_binding"])
                ):
                    fail(
                        400,
                        "invalid_provenance",
                        "Result pipeline binding differs from the immutable approved model",
                    )
                for ref in result.get("artifacts", []):
                    artifact = state["artifacts"].get(ref.get("id"))
                    if (
                        not artifact
                        or artifact["project_id"] != job["project_id"]
                        or not artifact["verified"]
                        or artifact["sha256"] != ref.get("sha256")
                    ):
                        fail(
                            400,
                            "invalid_result",
                            "Result artifacts must be verified project objects",
                        )
                if job["kind"] == "training":
                    self.register_candidate(state, job, result, now)
                job["result"] = result
            if kind in ("failed", "submission_unknown"):
                job["error"] = body.get("detail", {"message": kind})
            job["status"] = (
                ("cancelling" if job["status"] == "cancelling" else "queued")
                if kind == "submitted"
                else kind
            )
        else:
            fail(400, "invalid_event", "Unsupported connector event")
        job["revision"] += 1
        job["updated_at"] = now
        state["events"][event_key] = {
            "hash": event_hash,
            "created_at": now,
            "job_id": ident,
            "project_id": job["project_id"],
            "owner_sub": sub,
            "connector_id": body["connector_id"],
            "type": kind,
            "revision": job["revision"],
        }
        return 200, self.public_job(job)

    def validate_release_result(self, state, job, result):
        """Bind the complete v1 publication to verified, owner-created objects."""
        from aws_mesouq.release import validate_result

        validate_result(result, approved_adapter_sha256=job["approved_model"].get("approved_adapter_sha256"))
        by_name = {}
        for ref in result["artifacts"]:
            artifact = state["artifacts"].get(ref.get("id"))
            if (
                not artifact or artifact["project_id"] != job["project_id"]
                or artifact["owner_sub"] != job["owner_sub"] or not artifact["verified"]
                or not self.objects.verify(artifact["storage_key"], artifact)
                or any(ref.get(key) != artifact.get(key) for key in (
                    "id", "name", "kind", "sha256", "size_bytes", "media_type", "metadata"
                ))
            ):
                raise ValueError("Publication reference differs from a verified owner-created output")
            by_name[artifact["name"]] = artifact

        def read(name):
            artifact = by_name[name]
            return self.objects.read_json(artifact["storage_key"], artifact, maximum=200_000)

        published = read("result.json")
        if digest(published) != digest({key: value for key, value in result.items() if key != "artifacts"}):
            raise ValueError("Published result JSON differs from the event result")
        if digest(read("provenance.json")) != digest(result["provenance"]):
            raise ValueError("Published provenance differs from the event provenance")
        if digest(read("settings_and_versions.json")) != digest({
            "preset_id": "indentation-sonovue-v1", "configuration": job["configuration"],
            "provenance": result["provenance"],
        }):
            raise ValueError("Published settings differ from the approved job")

    def register_candidate(self, state, job, result, now):
        training = result.get("training", {})
        version = training.get("model_version")
        checksum = training.get("model_sha256")
        validation = training.get("validation")
        if (
            not text(version)
            or not isinstance(checksum, str)
            or not re.fullmatch("[a-f0-9]{64}", checksum)
            or not isinstance(validation, dict)
            or type(validation.get("passed")) is not bool
        ):
            fail(
                400,
                "invalid_model_candidate",
                "Training requires version, SHA256 and explicit validation outcome",
            )
        key = f"{job['project_id']}:{version}"
        if key in state["models"]:
            fail(409, "immutable_model", "Model version already exists")
        refs = [state["artifacts"][ref["id"]] for ref in result.get("artifacts", [])]
        models = [
            a
            for a in refs
            if a["kind"] == "model"
            and a["sha256"] == checksum
            and a["owner_sub"] == job["owner_sub"]
        ]
        validations = [
            a
            for a in refs
            if a["kind"] == "validation" and a["owner_sub"] == job["owner_sub"]
        ]
        if (
            len(models) != 1
            or len(validations) != 1
            or validations[0]["sha256"] != training.get("validation_sha256")
        ):
            fail(
                400,
                "invalid_model_candidate",
                "Verified model and validation artifact hashes must match the training result",
            )
        try:
            validation_report = self.objects.read_json(
                validations[0]["storage_key"], validations[0]
            )
        except (ValueError, TypeError, UnicodeError):
            fail(
                400,
                "invalid_model_candidate",
                "Validation artifact must be bounded checksum-verified JSON",
            )
        if validation_report != validation:
            fail(
                400,
                "invalid_model_candidate",
                "Validation report content differs from training validation attestation",
            )
        state["models"][key] = dict(
            project_id=job["project_id"],
            version=version,
            model_version=version,
            sha256=checksum,
            diameter_um=job["diameter_um"],
            format="mesouq_checkpoint",
            status="candidate",
            trusted=True,
            source_job_id=job["id"],
            provenance=result["provenance"],
            validation=validation,
            artifact_id=models[0]["id"],
            validation_artifact_id=validations[0]["id"],
            created_at=now,
        )
