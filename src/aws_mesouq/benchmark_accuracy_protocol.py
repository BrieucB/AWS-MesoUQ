"""Pure metadata protocol for source-bound accuracy-target orchestration.

Scientific decisions and receipt validation run on Karolina. This module only
validates transport, identity, funding, custody and native-accounting metadata.
"""
from __future__ import annotations

from copy import deepcopy
import hashlib
import json
import re
from typing import Any

from . import benchmark_accuracy as wire

PROTOCOL = "aws_mesouq.benchmark_accuracy_task.v1"
MANIFEST_SCHEMA = "aws_mesouq.benchmark_accuracy_manifest.v1"
RESULT_SCHEMA = "aws_mesouq.benchmark_accuracy_task_result.v1"
SNAPSHOT_SCHEMA = "aws_mesouq.benchmark_accuracy_allocation_costs.v1"
CONTEXT_SCHEMA = "aws_mesouq.benchmark_accuracy_context.v1"
NATIVE_RESULT_BYTES = 250000
EVENT_REQUEST_BYTES = 262144
EVENT_WIRE_BYTES = 524288
SIGNING_DOMAIN = "aws-mesouq-benchmark-accuracy-task-event-v1"
FACTORS = ("global", "mechanical_1", "mechanical_2", "mechanical_3",
           "acoustic_1", "acoustic_2", "acoustic_3")
TERMINAL = frozenset(("blocked", "cancelled", "failed", "succeeded"))
ACTIVE = frozenset(("cancelling", "claimed", "queued", "running",
                    "submission_unknown", "submitted", "submitting"))
KINDS = {
    "guide_preparation": ("hnpe", "guide_preparation"),
    "guide_reference": ("reference", "guide_reference"),
    "plan": ("overhead", "metrics"),
    "hnpe_initialize": ("hnpe", "hnpe_initialize"),
    "hnpe_advance": ("hnpe", "hnpe_advance"),
    "guide_hnpe_inference": ("hnpe", "guide_hnpe_inference"),
    "guide_tmcmc_attempt": ("tmcmc", "guide_tmcmc_attempt"),
    "guide_compile": ("overhead", "metrics"),
    "method_seal": ("overhead", "metrics"),
    "confirmation_preparation": ("overhead", "confirmation_preparation"),
    "confirmation_hnpe_inference": ("hnpe", "confirmation_hnpe_inference"),
    "confirmation_tmcmc_attempt": ("tmcmc", "confirmation_tmcmc_attempt"),
    "confirmation_reference": ("reference", "confirmation_reference"),
    "confirmation_compile": ("overhead", "metrics"),
    "record_confirmation_outcome": ("overhead", "metrics"),
}
EVENTS = frozenset(("heartbeat", "submitting", "submitted", "queued", "running",
                    "submission_unknown", "allocation_closed", "artifact_upload",
                    "artifact_status", "result", "failed", "cancelled"))
REQUIRED_CAPABILITIES = frozenset(("campaign_authority_v3", "immutable_method_seal",
                                   "immutable_confirmation_outcomes"))
PINS = frozenset(("case_plan_sha256", "guide_case_set_sha256",
                  "normalization_sha256", "validation_sha256", "candidate_sha256",
                  "method_seal_sha256", "sealed_allocation_anchor_sha256", "opening_id"))
SNAPSHOT_FIELDS = frozenset(("schema", "campaign_instance_id", "scientific_request_hash",
    "transport_request_hash", "source_binding_sha256", "case_plan_sha256",
    "guide_case_set_sha256", "revision", "allocations"))
ROW_FIELDS = frozenset(("allocation_id", "attempt", "method", "kind", "factor",
    "status", "slurm_job_id", "allocated_gpu_seconds", "allocated_cpu_seconds",
    "reserved_gpu_seconds", "not_submitted"))


def canonical(value: Any) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"),
                      allow_nan=False).encode("utf-8")


def checksum(value: Any) -> str:
    return hashlib.sha256(canonical(value)).hexdigest()


def keys(value: Any, required: set[str] | frozenset[str],
         optional: set[str] | frozenset[str] = frozenset()) -> dict:
    if not isinstance(value, dict) or not required <= set(value) or set(value) - required - optional:
        raise ValueError("Missing or unsupported protocol fields")
    return value


def integer(value: Any, name: str, low: int = 0, high: int = 2**63 - 1) -> int:
    if type(value) is not int or not low <= value <= high:
        raise ValueError(f"Invalid {name}")
    return value


def identifier(value: Any, name: str = "identifier") -> str:
    if not isinstance(value, str) or re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}", value) is None:
        raise ValueError(f"Invalid {name}")
    return value


def sha(value: Any, name: str = "sha256") -> str:
    if not isinstance(value, str) or re.fullmatch(r"[a-f0-9]{64}", value) is None:
        raise ValueError(f"Invalid {name}")
    return value


def descriptor(value: Any, *, native_root: str | None = None) -> dict:
    keys(value, {"directory", "sha256"})
    sha(value["sha256"])
    path = value["directory"]
    if not isinstance(path, str) or not path.startswith("/") or any(
        part in (".", "..", "") for part in path.split("/")[1:]
    ) or any(ord(c) < 32 for c in path):
        raise ValueError("Invalid native receipt directory")
    if native_root is not None and not path.startswith(native_root.rstrip("/") + "/"):
        raise ValueError("Native receipt is outside approved campaign storage")
    return deepcopy(value)


def context_id(value: dict) -> str:
    return checksum({k: v for k, v in value.items() if k not in ("id", "enabled")})


def validate_context(value: Any) -> dict:
    required = {"schema", "id", "enabled", "owner_sub", "project_id", "site",
        "science_revision", "source_binding_sha256", "adapter_sha256", "policy_sha256",
        "runtime_profile_sha256", "native_root", "archive_root", "native_user",
        "native_cpus", "native_gpus", "maximum_artifact_bytes", "maximum_total_artifact_bytes",
        "maximum_artifact_files", "capabilities", "allowed_artifacts"}
    keys(value, required, {"stage_reservations"})
    windows = value.get("stage_reservations", {})
    if not isinstance(windows, dict) or set(windows) - set(KINDS):
        raise ValueError("Invalid approved native stage windows")
    for seconds in windows.values():
        integer(seconds, "native stage window", 1, 2**31 - 1)
    if value["schema"] != CONTEXT_SCHEMA or type(value["enabled"]) is not bool or value["site"] != "karolina":
        raise ValueError("Invalid accuracy context")
    for name in ("owner_sub", "project_id", "native_user"):
        identifier(value[name], name)
    if not isinstance(value["science_revision"], str) or re.fullmatch(r"[a-f0-9]{40}", value["science_revision"]) is None:
        raise ValueError("Invalid scientific source commit")
    for name in ("id", "source_binding_sha256", "adapter_sha256", "policy_sha256", "runtime_profile_sha256"):
        sha(value[name], name)
    for name in ("native_root", "archive_root"):
        descriptor({"directory": value[name], "sha256": "0" * 64})
    if value["native_root"] == value["archive_root"]:
        raise ValueError("Independent native and archive roots required")
    integer(value["native_cpus"], "native CPU count", 1, 4096)
    if type(value["native_gpus"]) is not int or value["native_gpus"] != 1:
        raise ValueError("This approved protocol requires one allocated GPU per native task")
    integer(value["maximum_artifact_bytes"], "per-file artifact allowance", 1, 67_108_864)
    integer(value["maximum_total_artifact_bytes"], "curated artifact allowance", value["maximum_artifact_bytes"], 536_870_912)
    integer(value["maximum_artifact_files"], "curated artifact count", 1, 64)
    capabilities = value["capabilities"]
    if not isinstance(capabilities, list) or any(not isinstance(v, str) for v in capabilities) or len(set(capabilities)) != len(capabilities):
        raise ValueError("Invalid source capabilities")
    if value["enabled"] and not REQUIRED_CAPABILITIES <= set(capabilities):
        raise ValueError("Final source custody capabilities are required")
    allowed = value["allowed_artifacts"]
    if not isinstance(allowed, dict) or set(allowed) - set(KINDS):
        raise ValueError("Invalid curated artifact policy")
    for kind, names in allowed.items():
        if not isinstance(names, list) or len(names) > 100 or len(set(names)) != len(names):
            raise ValueError("Invalid curated artifact names")
        for name in names:
            identifier(name, "artifact name")
    if context_id(value) != value["id"]:
        raise ValueError("Context checksum mismatch")
    return deepcopy(value)


def public_context(value: dict) -> dict:
    return {name: value[name] for name in ("id", "source_binding_sha256",
        "science_revision", "adapter_sha256", "policy_sha256")}


def validate_claim(value: Any) -> dict:
    keys(value, {"project_id", "connector_id", "site", "request_id", "supported_contexts"},
         {"campaign_id", "task_id", "exclude_task_ids", "signature"})
    for name in ("project_id", "connector_id", "request_id"):
        identifier(value[name], name)
    if value["site"] != "karolina":
        raise ValueError("Only Karolina is supported")
    contexts = value["supported_contexts"]
    if not isinstance(contexts, list) or not 1 <= len(contexts) <= 16 or any(not isinstance(v, str) for v in contexts) or contexts != sorted(set(contexts)):
        raise ValueError("Contexts must be a sorted unique list")
    for item in contexts:
        sha(item)
    if "campaign_id" in value:
        identifier(value["campaign_id"])
    if "task_id" in value:
        identifier(value["task_id"])
        if "campaign_id" not in value:
            raise ValueError("Exact task targeting requires its campaign")
    excluded = value.get("exclude_task_ids", [])
    if not isinstance(excluded, list) or len(excluded) > 100 or any(not isinstance(v, str) for v in excluded) or excluded != sorted(set(excluded)):
        raise ValueError("Excluded task IDs must be sorted and unique")
    for item in excluded:
        identifier(item)
    if value.get("task_id") in excluded:
        raise ValueError("Exact task target conflicts with exclusions")
    return deepcopy(value)


def claim_signing_bytes(owner_sub: str, body: dict) -> bytes:
    return canonical({"domain": SIGNING_DOMAIN + ":claim", "owner_sub": owner_sub,
                      "body": {k: v for k, v in body.items() if k != "signature"}})


def validate_event(value: Any) -> dict:
    required = {"project_id", "connector_id", "campaign_id", "task_id", "attempt",
        "lease_epoch", "lease_token", "event_id", "expected_task_revision",
        "manifest_sha256", "type", "payload", "signature"}
    keys(value, required)
    for name in ("project_id", "connector_id", "campaign_id", "task_id", "event_id"):
        identifier(value[name], name)
    for name in ("attempt", "lease_epoch"):
        integer(value[name], name, 1)
    integer(value["expected_task_revision"], "task revision")
    sha(value["manifest_sha256"])
    if value["type"] not in EVENTS or not isinstance(value["payload"], dict):
        raise ValueError("Invalid event type or payload")
    if not isinstance(value["lease_token"], str) or not 1 <= len(value["lease_token"]) <= 256:
        raise ValueError("Invalid lease token")
    required, optional = EVENT_PAYLOAD_FIELDS[value["type"]]
    keys(value["payload"], required, optional)
    return deepcopy(value)


def event_signing_bytes(owner_sub: str, body: dict) -> bytes:
    return canonical({"domain": SIGNING_DOMAIN, "owner_sub": owner_sub,
                      "body": {k: v for k, v in body.items() if k != "signature"}})


def archive_signing_bytes(owner_sub: str, body: dict) -> bytes:
    return canonical({"domain": SIGNING_DOMAIN + ":archive", "owner_sub": owner_sub,
                      "body": {k: v for k, v in body.items() if k != "signature"}})


def validate_accounting(value: Any, *, job_id: str, task_status: str,
                        expected_cpus: int, allow_preallocation: bool) -> tuple[int, int]:
    base = {"source", "site", "slurm_job_id", "state", "elapsed_seconds",
            "allocated_gpus", "cpu_seconds"}
    keys(value, base, {"preallocation_cancellation"})
    if value["source"] != "sacct" or value["site"] != "karolina" or value["slurm_job_id"] != job_id:
        raise ValueError("Native accounting identity mismatch")
    if value["state"] not in {"COMPLETED", "FAILED", "CANCELLED", "TIMEOUT", "OUT_OF_MEMORY", "NODE_FAIL", "PREEMPTED", "BOOT_FAIL", "DEADLINE"}:
        raise ValueError("Terminal native accounting required")
    elapsed = integer(value["elapsed_seconds"], "native elapsed seconds")
    cpus = integer(value["cpu_seconds"], "native CPU seconds")
    gpus = integer(value["allocated_gpus"], "allocated GPU count")
    if gpus == 1:
        if "preallocation_cancellation" in value or cpus != elapsed * expected_cpus:
            raise ValueError("Allocated native resource evidence mismatch")
        return elapsed, cpus
    if not allow_preallocation or task_status != "cancelling" or value["state"] != "CANCELLED" or gpus != 0 or elapsed != 0 or cpus != 0:
        raise ValueError("Zero allocation requires an authoritative queued cancellation")
    proof = value.get("preallocation_cancellation")
    keys(proof, {"start", "allocated_cpus", "allocated_tres"})
    if proof["start"] is not None or type(proof["allocated_cpus"]) is not int or proof["allocated_cpus"] != 0 or type(proof["allocated_tres"]) is not str or proof["allocated_tres"] != "":
        raise ValueError("Exact pre-allocation cancellation evidence required")
    return 0, 0


def validate_snapshot(value: Any, *, allow_unprepared: bool = False) -> dict:
    keys(value, SNAPSHOT_FIELDS)
    if value["schema"] != SNAPSHOT_SCHEMA:
        raise ValueError("Invalid allocation snapshot schema")
    identifier(value["campaign_instance_id"])
    for name in ("scientific_request_hash", "transport_request_hash", "source_binding_sha256"):
        sha(value[name])
    for name in ("case_plan_sha256", "guide_case_set_sha256"):
        if value[name] is None and allow_unprepared:
            continue
        sha(value[name])
    integer(value["revision"], "allocation revision")
    if not isinstance(value["allocations"], list):
        raise ValueError("Invalid allocation rows")
    seen = set()
    owners = set(KINDS.values())
    for row in value["allocations"]:
        keys(row, ROW_FIELDS)
        identifier(row["allocation_id"])
        if row["allocation_id"] in seen:
            raise ValueError("Duplicate allocation")
        seen.add(row["allocation_id"])
        integer(row["attempt"], "allocation attempt", 1)
        if (row["method"], row["kind"]) not in owners or row["status"] not in ACTIVE | TERMINAL:
            raise ValueError("Invalid allocation role or state")
        if row["factor"] is not None and row["factor"] not in FACTORS:
            raise ValueError("Invalid factor")
        integer(row["reserved_gpu_seconds"], "reservation")
        if type(row["not_submitted"]) is not bool:
            raise ValueError("not_submitted must be boolean")
        if row["slurm_job_id"] is not None and (not isinstance(row["slurm_job_id"], str) or re.fullmatch(r"[1-9][0-9]{0,19}", row["slurm_job_id"]) is None):
            raise ValueError("Invalid native job ID")
        for name in ("allocated_gpu_seconds", "allocated_cpu_seconds"):
            if row[name] is not None:
                integer(row[name], name)
        if row["status"] in TERMINAL and (row["allocated_gpu_seconds"] is None or row["allocated_cpu_seconds"] is None):
            raise ValueError("Terminal cost cannot be unknown")
        if row["not_submitted"] and (row["status"] not in TERMINAL or row["slurm_job_id"] is not None or row["allocated_gpu_seconds"] != 0 or row["allocated_cpu_seconds"] != 0 or row["reserved_gpu_seconds"] != 0):
            raise ValueError("Never-submitted allocation must be terminal and exactly zero")
    return deepcopy(value)


def validate_anchor(anchor: dict, current: dict, *, require_reference: bool = False) -> None:
    validate_snapshot(anchor)
    validate_snapshot(current)
    for name in SNAPSHOT_FIELDS - {"revision", "allocations"}:
        if anchor[name] != current[name]:
            raise ValueError("Sealed accounting identity changed")
    if current["revision"] < anchor["revision"]:
        raise ValueError("Sealed accounting revision regressed")
    original_ids = {row["allocation_id"] for row in anchor["allocations"]}
    for row in current["allocations"]:
        if row["allocation_id"] not in original_ids and row["kind"] not in {
            "confirmation_hnpe_inference", "confirmation_tmcmc_attempt",
            "confirmation_reference", "confirmation_preparation", "metrics"
        }:
            raise ValueError("New pre-confirmation allocation after sealing")
    existing = {row["allocation_id"]: row for row in current["allocations"]}
    for old in anchor["allocations"]:
        if old["allocation_id"] not in existing:
            raise ValueError("Sealed allocation was omitted")
        new = existing[old["allocation_id"]]
        if old["status"] in TERMINAL:
            if canonical(old) != canonical(new):
                raise ValueError("Sealed terminal allocation changed")
        elif old["method"] != "overhead":
            raise ValueError("Only overhead may be active in a sealed anchor")
        else:
            for name in ROW_FIELDS - {"status", "slurm_job_id", "allocated_gpu_seconds",
                                      "allocated_cpu_seconds", "reserved_gpu_seconds"}:
                if old[name] != new[name]:
                    raise ValueError("Sealed overhead identity changed")
            if old["slurm_job_id"] is not None and old["slurm_job_id"] != new["slurm_job_id"]:
                raise ValueError("Sealed native producer changed")
            if new["reserved_gpu_seconds"] > old["reserved_gpu_seconds"]:
                raise ValueError("Sealed overhead reservation increased")
            for name in ("allocated_gpu_seconds", "allocated_cpu_seconds"):
                if old[name] is not None and (new[name] is None or new[name] < old[name]):
                    raise ValueError("Sealed overhead cost regressed")
    required = {"hnpe", "tmcmc"} | ({"reference"} if require_reference else set())
    if any(r["method"] in required and r["status"] not in TERMINAL for r in anchor["allocations"]):
        raise ValueError("Method/reference native accounting must be complete")


def validate_manifest(value: Any) -> dict:
    required = {"schema", "campaign_instance_id", "task_id", "attempt", "kind", "method",
        "factor", "effort_target", "generation", "expected_state_sha256", "request",
        "scientific_request_hash", "transport_request_hash", "context_id",
        "source_binding_sha256", "policy_sha256", "accounting", "input_ledger_sha256",
        "reservation", "pins", "inputs"}
    keys(value, required)
    if value["schema"] != MANIFEST_SCHEMA or value["kind"] not in KINDS:
        raise ValueError("Invalid native task manifest")
    for name in ("campaign_instance_id", "task_id"):
        identifier(value[name])
    integer(value["attempt"], "attempt", 1)
    integer(value["generation"], "generation")
    if value["factor"] is not None and value["factor"] not in FACTORS:
        raise ValueError("Invalid factor")
    if value["effort_target"] is not None:
        integer(value["effort_target"], "effort target", 1)
    for name in ("scientific_request_hash", "transport_request_hash", "context_id",
                 "source_binding_sha256", "policy_sha256", "input_ledger_sha256"):
        sha(value[name])
    if value["expected_state_sha256"] is not None:
        sha(value["expected_state_sha256"])
    if value["method"] not in {"hnpe", "tmcmc", "reference", "overhead"}:
        raise ValueError("Invalid task method")
    normalized = wire.validate_create(value["request"])
    if wire.scientific_request_checksum(normalized) != value["scientific_request_hash"] or wire.transport_request_checksum(normalized) != value["transport_request_hash"]:
        raise ValueError("Task scientific/transport identity mismatch")
    validate_snapshot(value["accounting"], allow_unprepared=value["kind"] == "guide_preparation")
    if checksum(value["accounting"]) != value["input_ledger_sha256"]:
        raise ValueError("Task ledger checksum mismatch")
    keys(value["pins"], PINS)
    for name, item in value["pins"].items():
        if item is not None:
            if name == "opening_id":
                identifier(item)
            else:
                sha(item, name)
    keys(value["reservation"], {"allocation_id", "gpu_seconds", "native_cpus", "native_gpus"})
    identifier(value["reservation"]["allocation_id"])
    integer(value["reservation"]["gpu_seconds"], "reservation", 1)
    integer(value["reservation"]["native_cpus"], "native CPUs", 1)
    if type(value["reservation"]["native_gpus"]) is not int or value["reservation"]["native_gpus"] != 1:
        raise ValueError("Approved one-GPU task required")
    if not isinstance(value["inputs"], dict) or len(canonical(value)) > 250000:
        raise ValueError("Bounded task input metadata required")
    return deepcopy(value)


EVENT_PAYLOAD_FIELDS = {
    "heartbeat": (set(), set()),
    "submitting": ({"script_sha256", "intent_sha256", "marker"}, set()),
    "submitted": ({"slurm_job_id"}, set()),
    "queued": ({"slurm_job_id"}, set()),
    "running": ({"slurm_job_id", "allocation_start_epoch", "allocation_deadline_epoch"}, set()),
    "submission_unknown": ({"marker", "reason"}, set()),
    "allocation_closed": ({"scheduler_accounting"}, set()),
    "artifact_upload": ({"artifacts"}, set()),
    "artifact_status": ({"artifact_ids"}, set()),
    "result": ({"result"}, set()),
    "failed": ({"failure", "not_submitted"}, {"scheduler_accounting"}),
    "cancelled": ({"not_submitted"}, {"scheduler_accounting"}),
}

PROPOSAL_ACTIONS = frozenset(("train_factor", "fresh_tmcmc_attempt", "evaluate_guide",
    "freeze", "wait", "reconcile_accounting", "reconcile_native_state", "record_failed_guide"))
OUTPUT_FIELDS = frozenset(("progress", "guide_preparation", "guide_reference", "guide_result",
    "candidate", "guide_history", "tmcmc_attempts", "method_states", "method_seal",
    "sealed_allocation_anchor", "confirmation_bank", "confirmation_outcomes",
    "result_artifact_id", "confirmation_commitment", "failure_reason"))


def validate_proposal(value: Any) -> dict:
    required = {"method", "action", "effort_target", "candidate_sha256",
        "eligible_factors", "maximum_reserved_gpu_seconds", "status", "reason"}
    keys(value, required)
    if value["method"] not in {"hnpe", "tmcmc"} or value["action"] not in PROPOSAL_ACTIONS:
        raise ValueError("Invalid source planner proposal")
    if value["effort_target"] is not None:
        integer(value["effort_target"], "effort target", 1)
    if value["candidate_sha256"] is not None:
        sha(value["candidate_sha256"])
    integer(value["maximum_reserved_gpu_seconds"], "native maximum reservation")
    if value["status"] is not None and value["status"] not in {"frozen", "terminal_unmet", "no_candidate"}:
        raise ValueError("Invalid source terminal status")
    if value["reason"] is not None and (not isinstance(value["reason"], str) or len(value["reason"]) > 256):
        raise ValueError("Invalid source planner reason")
    eligible = value["eligible_factors"]
    if not isinstance(eligible, list) or len(eligible) > 7:
        raise ValueError("Invalid eligible factor alternatives")
    names = set()
    for item in eligible:
        keys(item, {"factor", "operation", "to_update", "expected_state_sha256", "generation"})
        if item["factor"] not in FACTORS or item["factor"] in names or item["operation"] not in {"initialize", "advance"}:
            raise ValueError("Invalid factor action")
        names.add(item["factor"])
        integer(item["generation"], "generation")
        if item["operation"] == "initialize":
            if item["expected_state_sha256"] is not None or item["generation"] != 0 or item["to_update"] is not None:
                raise ValueError("Initialization cannot replay a native prefix")
        else:
            sha(item["expected_state_sha256"])
            integer(item["to_update"], "requested update", 1)
    if value["action"] == "train_factor" and (value["method"] != "hnpe" or not eligible or value["maximum_reserved_gpu_seconds"] < 1):
        raise ValueError("A funded single-factor alternative is required")
    if value["action"] != "train_factor" and eligible:
        raise ValueError("Unexpected factor alternatives")
    if value["action"] == "freeze" and value["status"] is None:
        raise ValueError("Source terminal proposal required")
    return deepcopy(value)


def _no_raw_secret(value: Any) -> None:
    if isinstance(value, dict):
        if set(value) & {"nonce", "raw_nonce", "confirmation_nonce", "private_key", "lease_token", "refresh_token"}:
            raise ValueError("Raw secrets are not publication metadata")
        for child in value.values():
            _no_raw_secret(child)
    elif isinstance(value, list):
        for child in value:
            _no_raw_secret(child)


def validate_result(value: Any, manifest: dict, *, native_root: str) -> dict:
    required = {"schema", "campaign_instance_id", "task_id", "attempt", "kind",
        "context_id", "source_binding_sha256", "scientific_request_hash",
        "transport_request_hash", "manifest_sha256", "slurm_job_id",
        "input_ledger_sha256", "native_receipt", "input_pins", "output_pins",
        "outputs", "source_validation", "proposal", "scientific_status",
        "qualification", "artifacts"}
    keys(value, required)
    if value["schema"] != RESULT_SCHEMA:
        raise ValueError("Invalid native result envelope")
    for name in ("campaign_instance_id", "task_id", "attempt", "kind", "context_id",
                 "source_binding_sha256", "scientific_request_hash",
                 "transport_request_hash", "input_ledger_sha256"):
        if value[name] != manifest[name]:
            raise ValueError("Native result identity or source changed")
    if value["manifest_sha256"] != checksum(manifest):
        raise ValueError("Native manifest checksum changed")
    if not isinstance(value["slurm_job_id"], str) or re.fullmatch(r"[1-9][0-9]{0,19}", value["slurm_job_id"]) is None:
        raise ValueError("Native producer job required")
    descriptor(value["native_receipt"], native_root=native_root)
    if value["input_pins"] != manifest["pins"]:
        raise ValueError("Native input chain changed")
    keys(value["output_pins"], PINS)
    for name, item in value["output_pins"].items():
        if item is not None:
            identifier(item) if name == "opening_id" else sha(item)
        if item != manifest["pins"][name] and name not in PIN_CHANGES[value["kind"]]:
            raise ValueError("This native role cannot change that source/case/custody pin")
        if manifest["pins"][name] is not None and item != manifest["pins"][name] and name not in {"candidate_sha256"}:
            raise ValueError("Existing source/case/custody pin changed")
    if manifest["pins"]["method_seal_sha256"] is not None and value["output_pins"]["candidate_sha256"] != manifest["pins"]["candidate_sha256"]:
        raise ValueError("Sealed method candidates cannot change")
    if not isinstance(value["outputs"], dict) or set(value["outputs"]) - ROLE_OUTPUTS[value["kind"]]:
        raise ValueError("Unsupported native role output metadata")
    states = value["outputs"].get("method_states")
    if states is not None:
        expected = {manifest["method"]} if value["kind"] == "plan" else {"hnpe", "tmcmc"}
        if value["kind"] not in ("plan", "method_seal") or not isinstance(states, dict) or set(states) != expected:
            raise ValueError("Thin immutable method-state descriptor map required")
        for state in states.values():
            descriptor(state, native_root=native_root)
    history = value["outputs"].get("guide_history")
    if history is not None:
        if not isinstance(history, list) or len(history) > 1000:
            raise ValueError("Bounded immutable guide-history descriptors required")
        for item in history:
            descriptor(item, native_root=native_root)
    proof = value["source_validation"]
    keys(proof, {"schema", "verified", "producer_job_id", "receipt_sha256",
                 "manifest_sha256", "verified_inputs_sha256", "artifact_manifest_sha256", "checks"})
    if proof["schema"] != "aws_mesouq.benchmark_accuracy_native_validation.v1" or type(proof["verified"]) is not bool or not proof["verified"]:
        raise ValueError("Source-owned native receipt validation required")
    if proof["producer_job_id"] != value["slurm_job_id"] or proof["receipt_sha256"] != value["native_receipt"]["sha256"] or proof["manifest_sha256"] != value["manifest_sha256"] or proof["verified_inputs_sha256"] != checksum(manifest["inputs"]):
        raise ValueError("Native validation chain mismatch")
    checks = proof["checks"]
    if not isinstance(checks, list) or any(not isinstance(c, str) for c in checks) or len(set(checks)) != len(checks):
        raise ValueError("Invalid native validation checks")
    needed = required_validation_checks(value["kind"])
    if not needed <= set(checks):
        raise ValueError("Missing source-chain validation")
    if value["proposal"] is not None:
        if value["kind"] != "plan":
            raise ValueError("Only a native planner may propose scientific actions")
        validate_proposal(value["proposal"])
    if value["kind"] == "plan" and value["proposal"] is None:
        raise ValueError("Native planner receipt is missing its proposal")
    for name in ("scientific_status",):
        if value[name] is not None and (not isinstance(value[name], str) or len(value[name]) > 128):
            raise ValueError("Invalid source status")
    if value["qualification"] is not None and not isinstance(value["qualification"], dict):
        raise ValueError("Invalid source qualification metadata")
    if not isinstance(value["artifacts"], list) or len(value["artifacts"]) > 100:
        raise ValueError("Invalid curated artifact references")
    curated = []
    for artifact in value["artifacts"]:
        keys(artifact, {"id", "name", "sha256", "size_bytes", "media_type", "kind"})
        identifier(artifact["id"]); identifier(artifact["name"])
        sha(artifact["sha256"]); integer(artifact["size_bytes"], "artifact bytes", 1)
        curated.append({k: v for k, v in artifact.items() if k != "id"})
    if len({a["name"] for a in curated}) != len(curated):
        raise ValueError("Duplicate curated native artifact name")
    if proof["artifact_manifest_sha256"] != checksum(sorted(curated, key=lambda a: a["name"])):
        raise ValueError("Curated object bytes differ from the native artifact manifest")
    _no_raw_secret(value["outputs"])
    _no_raw_secret(value["qualification"])
    if len(canonical(value)) > NATIVE_RESULT_BYTES:
        raise ValueError("Native result metadata exceeds bound")
    return deepcopy(value)

PIN_CHANGES = {kind: frozenset() for kind in KINDS}
PIN_CHANGES.update({
    "guide_preparation": frozenset(("case_plan_sha256", "guide_case_set_sha256", "normalization_sha256", "validation_sha256")),
    "plan": frozenset(("candidate_sha256",)),
    "guide_tmcmc_attempt": frozenset(("candidate_sha256",)),
    "method_seal": frozenset(("method_seal_sha256", "sealed_allocation_anchor_sha256")),
    "confirmation_preparation": frozenset(("opening_id",)),
})
ROLE_OUTPUTS = {
    "guide_preparation": frozenset(("guide_preparation", "confirmation_commitment")),
    "guide_reference": frozenset(("guide_reference",)),
    "plan": frozenset(("guide_history", "method_states", "candidate", "failure_reason")),
    "hnpe_initialize": frozenset(("progress",)),
    "hnpe_advance": frozenset(("progress",)),
    "guide_hnpe_inference": frozenset(("candidate",)),
    "guide_tmcmc_attempt": frozenset(("candidate", "tmcmc_attempts")),
    "guide_compile": frozenset(("guide_history", "guide_result")),
    "method_seal": frozenset(("method_seal", "sealed_allocation_anchor", "method_states")),
    "confirmation_preparation": frozenset(("confirmation_bank",)),
    "confirmation_hnpe_inference": frozenset(("confirmation_outcomes",)),
    "confirmation_tmcmc_attempt": frozenset(("confirmation_outcomes",)),
    "confirmation_reference": frozenset(("confirmation_outcomes",)),
    "record_confirmation_outcome": frozenset(("confirmation_outcomes",)),
    "confirmation_compile": frozenset(("result_artifact_id", "accounting_artifact_id", "summary_artifact_id", "validation_capsule_artifact_id", "final_accounting_receipt")),
}


def required_validation_checks(kind: str) -> frozenset[str]:
    """Name source validations the admitted native adapter must actually perform."""
    base = {"source", "producer", "inputs", "curated_publication"}
    additional = {
        "guide_preparation": {"campaign_authority", "case_plan", "bank", "normalization", "validation"},
        "guide_reference": {"reference", "case_plan", "bank"},
        "plan": {"planner", "accounting", "native_state", "guide_history"},
        "hnpe_initialize": {"campaign_authority", "native_state", "optimizer_predecessor", "case_plan", "normalization", "validation"},
        "hnpe_advance": {"campaign_authority", "native_state", "optimizer_predecessor", "case_plan", "normalization", "validation"},
        "guide_hnpe_inference": {"candidate", "case_plan", "bank", "native_state"},
        "guide_tmcmc_attempt": {"fresh_attempt", "case_plan", "bank"},
        "guide_compile": {"candidate", "reference", "case_plan", "guide_history"},
        "method_seal": {"method_seal", "custody", "accounting", "candidate", "reference", "case_plan", "guide_history", "native_state", "original_guide_preparation"},
        "confirmation_preparation": {"method_seal", "custody", "accounting", "case_plan", "bank", "confirmation_opening"},
        "confirmation_hnpe_inference": {"method_seal", "custody", "accounting", "case_plan", "bank", "candidate", "first_confirmation_outcome"},
        "confirmation_tmcmc_attempt": {"method_seal", "custody", "accounting", "case_plan", "bank", "candidate", "first_confirmation_outcome"},
        "record_confirmation_outcome": {"method_seal", "custody", "accounting", "case_plan", "bank", "candidate", "first_confirmation_outcome"},
        "confirmation_reference": {"method_seal", "custody", "accounting", "reference", "case_plan", "bank", "confirmation_opening"},
        "confirmation_compile": {"method_seal", "custody", "accounting", "candidate", "reference", "case_plan", "native_state", "confirmation_outcomes", "first_comparison_outcome", "complete_confirmation_denominator", "postexit_accounting_first_comparison", "postexit_accounting_closed_ledger"},
    }
    if kind not in additional:
        raise ValueError("Unknown native validation role")
    return frozenset(base | additional[kind])


POSTEXIT_ACCOUNTING_SCHEMA = "mesouq.benchmark_lab.accuracy_v2.postexit_accounting.v1"


def source_qualification(comparison: dict) -> dict:
    """Copy source outcomes for display; never evaluate a numerical criterion."""
    if not isinstance(comparison, dict) or not isinstance(comparison.get("evaluation"), dict):
        raise ValueError("Source comparison evaluation is missing")
    evaluation = comparison["evaluation"]
    count = integer(evaluation.get("confirmation_case_count"), "confirmation case count", 256, 256)
    methods = comparison.get("methods")
    if not isinstance(methods, dict):
        raise ValueError("Source comparison methods are missing")
    result = {"confirmation_case_count": count}
    copied = {"verdict", "accuracy_target_within_budget", "within_method_ceiling", "within_factor_ceilings"}
    if all(isinstance(methods.get(method), dict) and "guide_status" in methods[method]
           for method in ("hnpe", "tmcmc")):
        copied.add("guide_status")
    for method in ("hnpe", "tmcmc"):
        value = methods.get(method)
        if not isinstance(value, dict) or not copied <= set(value):
            raise ValueError("Source method verdict or budget flags are missing")
        for flag in ("accuracy_target_within_budget", "within_method_ceiling", "within_factor_ceilings"):
            if type(value[flag]) is not bool:
                raise ValueError("Typed source method budget flags required")
        result[method] = {name: deepcopy(value[name]) for name in sorted(copied)}
    _no_raw_secret(result)
    return result


def validate_final_accounting(value: Any, comparison: dict, *,
                              comparison_sha256: str, current: dict) -> dict:
    """Validate the separate source-owned postexit appendix and closed ledger.

    This does not recompute scientific results or permit new allocations. The
    numerical comparison is immutable; only its already-pinned overhead rows
    may advance to actual terminal accounting.
    """
    keys(value, {"schema", "binding", "comparison_sha256", "prior_snapshot_sha256",
        "final_allocation_costs", "closed_numerical_prefix_preserved",
        "numerical_results_recomputed"})
    if value["schema"] != POSTEXIT_ACCOUNTING_SCHEMA:
        raise ValueError("Typed source postexit accounting receipt required")
    if value["closed_numerical_prefix_preserved"] is not True or value["numerical_results_recomputed"] is not False:
        raise ValueError("Numerical comparison must remain immutable")
    sha(comparison_sha256)
    if value["comparison_sha256"] != comparison_sha256 or value["binding"] != comparison.get("binding"):
        raise ValueError("Postexit accounting is not bound to the first comparison")
    prior_summary = comparison.get("allocation_costs")
    final_summary = value["final_allocation_costs"]
    if not isinstance(prior_summary, dict) or not isinstance(final_summary, dict):
        raise ValueError("Source allocation summaries are required")
    prior, final = prior_summary.get("snapshot"), final_summary.get("snapshot")
    validate_snapshot(prior)
    validate_snapshot(final)
    validate_snapshot(current)
    if (prior_summary.get("snapshot_sha256") != checksum(prior)
            or value["prior_snapshot_sha256"] != checksum(prior)
            or final_summary.get("snapshot_sha256") != checksum(final)):
        raise ValueError("Postexit accounting snapshot checksum mismatch")
    old = {row["allocation_id"]: row for row in prior["allocations"]}
    new = {row["allocation_id"]: row for row in final["allocations"]}
    if set(old) != set(new):
        raise ValueError("Postexit accounting cannot append or omit allocations")
    validate_anchor(prior, final, require_reference=True)
    for allocation_id, before in old.items():
        after = new[allocation_id]
        if before["slurm_job_id"] != after["slurm_job_id"]:
            raise ValueError("Pinned native producer cannot change after comparison")
        if before["method"] != "overhead" and canonical(before) != canonical(after):
            raise ValueError("Postexit accounting changed a numerical allocation")
    if canonical(final) != canonical(current):
        raise ValueError("Postexit accounting differs from authoritative final accounting")
    if any(row["status"] not in TERMINAL or row["allocated_gpu_seconds"] is None
           or row["allocated_cpu_seconds"] is None or row["reserved_gpu_seconds"] != 0
           for row in final["allocations"]):
        raise ValueError("Every allocation cost must be complete before publication")
    if final_summary.get("complete") is not True:
        raise ValueError("Source final accounting must be complete")
    binding = value["binding"]
    if not isinstance(binding, dict):
        raise ValueError("Source comparison binding required")
    for field in ("campaign_instance_id", "scientific_request_hash", "transport_request_hash",
                  "source_binding_sha256", "case_plan_sha256", "guide_case_set_sha256"):
        if binding.get(field) != current[field]:
            raise ValueError("Final accounting source/case/request identity mismatch")
    return deepcopy(value)


def bind_current_allocation(manifest: dict, job_id: str, *, snapshot: dict | None = None) -> dict:
    """Bind only the reserved current row to its actual native producer.

    The immutable task and its input-ledger digest are never modified. Rebinding
    an already exact snapshot is idempotent; foreign rows, producers, statuses,
    costs or reservations are refused.
    """
    value = validate_manifest(manifest)
    if not isinstance(job_id, str) or re.fullmatch(r"[1-9][0-9]{0,19}", job_id) is None:
        raise ValueError("Actual native producer job is required")
    original = value["accounting"]
    result = deepcopy(original)
    reservation = value["reservation"]
    rows = [row for row in result["allocations"]
            if row["allocation_id"] == reservation["allocation_id"]]
    if len(rows) != 1:
        raise ValueError("Exactly one current reserved allocation is required")
    row = rows[0]
    owner, kind = KINDS[value["kind"]]
    if (row["attempt"] != value["attempt"] or row["method"] != owner
            or row["kind"] != kind or row["factor"] != value["factor"]
            or row["status"] != "claimed" or row["slurm_job_id"] is not None
            or row["allocated_gpu_seconds"] is not None
            or row["allocated_cpu_seconds"] is not None
            or row["reserved_gpu_seconds"] != reservation["gpu_seconds"]
            or row["not_submitted"] is not False):
        raise ValueError("Immutable current reservation is not an unallocated claimed row")
    row["status"], row["slurm_job_id"] = "running", job_id
    validate_snapshot(result, allow_unprepared=value["kind"] == "guide_preparation")
    if snapshot is not None:
        validate_snapshot(snapshot, allow_unprepared=value["kind"] == "guide_preparation")
        if canonical(snapshot) not in (canonical(original), canonical(result)):
            raise ValueError("Current producer binding changed an unapproved row or cost")
    return result

DISPLAY_BYTES = 262144
HTTP_RESPONSE_BYTES = 1048576
DISPLAY_SCHEMA = "mesouq.benchmark_lab.accuracy_v2.display_summary.v1"
CAPSULE_SCHEMA = "mesouq.benchmark_lab.accuracy_v2.validation_capsule.v1"
RESOURCE_SUMMARY_SCHEMA = "mesouq.benchmark_lab.accuracy_v2.final_resource_summary.v1"


def closed_resource_totals(snapshot: dict, request: dict) -> dict:
    """Compare native cost metadata with the authoritative closed allocation ledger."""
    validate_snapshot(snapshot)
    rows = snapshot["allocations"]
    if any(row["status"] not in TERMINAL or row["allocated_gpu_seconds"] is None
           or row["allocated_cpu_seconds"] is None or row["reserved_gpu_seconds"] != 0 for row in rows):
        raise ValueError("All original native costs must be known and closed")
    methods = {}
    for method in ("hnpe", "tmcmc", "reference", "overhead"):
        owned = [row for row in rows if row["method"] == method]
        gpu = sum((float(row["allocated_gpu_seconds"]) for row in owned), 0.0)
        cpu = sum((float(row["allocated_cpu_seconds"]) for row in owned), 0.0)
        ceiling = request["budget"]["method_gpu_seconds"] if method in ("hnpe", "tmcmc") else None
        methods[method] = {"allocated_gpu_seconds": gpu, "allocated_cpu_seconds": cpu,
            "reserved_gpu_seconds": 0.0, "active_allocation_ids": [], "unknown_allocation_ids": [],
            "cost_known": True, "complete": True, "ceiling_gpu_seconds": ceiling,
            "remaining_gpu_seconds": max(0.0, ceiling-gpu) if ceiling is not None else None,
            "ceiling_exceeded": gpu > ceiling if ceiling is not None else False}
    factors = {}
    for factor in FACTORS:
        gpu = sum((float(row["allocated_gpu_seconds"]) for row in rows if row["factor"] == factor), 0.0)
        ceiling = request["controls"]["hnpe"]["training_budget"]
        factors[factor] = {"allocated_gpu_seconds": gpu, "reserved_gpu_seconds": 0.0,
            "active_allocation_ids": [], "unknown_allocation_ids": [], "cost_known": True,
            "ceiling_gpu_seconds": ceiling, "remaining_gpu_seconds": max(0.0, ceiling-gpu),
            "ceiling_exceeded": gpu > ceiling}
    return {"methods": methods, "factors": factors}


def source_artifact_projection(value: dict) -> dict:
    """Preserve source descriptor extras for hashing; project only transport fields."""
    if not isinstance(value, dict):
        raise ValueError("Source artifact descriptor required")
    keys(value, {"file", "sha256", "size_bytes", "content_type"}, set(value)-{
        "file", "sha256", "size_bytes", "content_type"})
    identifier(value["file"], "source artifact filename")
    sha(value["sha256"])
    integer(value["size_bytes"], "source artifact bytes", 1, 67108864)
    if not isinstance(value["content_type"], str):
        raise ValueError("Source artifact media type required")
    kind = value.get("kind", "scientific_artifact")
    identifier(kind, "source artifact kind")
    canonical(value)
    _no_raw_secret(value)
    return {"name": value["file"], "sha256": value["sha256"],
            "size_bytes": value["size_bytes"], "media_type": value["content_type"], "kind": kind}


def publication_qualification(capsule: dict) -> dict:
    count = integer(capsule.get("confirmation_case_count"), "confirmation case count", 256, 256)
    assessments = capsule.get("source_assessments")
    keys(assessments, {"hnpe", "tmcmc"})
    for value in assessments.values():
        keys(value, {"verdict", "accuracy_target_within_budget", "within_method_ceiling",
                     "within_factor_ceilings", "guide_status"})
        if not isinstance(value["verdict"], dict):
            raise ValueError("Unchanged source verdict required")
        for field in ("accuracy_target_within_budget", "within_method_ceiling", "within_factor_ceilings"):
            if type(value[field]) is not bool:
                raise ValueError("Source budget flags must be literal booleans")
    result = {"confirmation_case_count": count, **deepcopy(assessments)}
    _no_raw_secret(result)
    return result


def validate_publication_capsule(capsule: dict, summary: dict, *, manifest: dict,
                                 job_id: str, current: dict, artifacts: list[dict],
                                 roles: dict[str, dict], expected_binding: dict) -> dict:
    """Validate a bounded source-produced bridge; never load a raw comparison/ledger appendix."""
    if len(canonical(capsule)) > DISPLAY_BYTES or len(canonical(summary)) > DISPLAY_BYTES:
        raise ValueError("Source display/capsule exceeds its bounded metadata envelope")
    keys(capsule, {"schema", "binding", "comparison", "accounting", "summary",
        "prior_snapshot_sha256", "final_snapshot_sha256", "final_snapshot_revision",
        "allocation_membership_sha256", "allocation_count", "comparison_producer",
        "allocation_totals", "source_assessments", "scientific_status", "confirmation_case_count",
        "curation", "closed_numerical_prefix_preserved", "numerical_results_recomputed"})
    if (capsule["schema"] != CAPSULE_SCHEMA or summary.get("schema") != DISPLAY_SCHEMA
            or capsule["closed_numerical_prefix_preserved"] is not True
            or capsule["numerical_results_recomputed"] is not False
            or summary.get("numerical_statistics_recomputed") is not False):
        raise ValueError("Typed source finalization without numerical recomputation required")
    binding = capsule["binding"]
    keys(binding, set(expected_binding) | {"target_fingerprint"})
    sha(binding["target_fingerprint"])
    if any(binding.get(field) != value for field, value in expected_binding.items()) or summary.get("binding") != binding:
        raise ValueError("Source publication request/case/policy binding changed")
    prior = bind_current_allocation(manifest, job_id)
    validate_anchor(prior, current, require_reference=True)
    if capsule["prior_snapshot_sha256"] != checksum(prior):
        raise ValueError("First comparison does not bind the actual producer allocation")
    integer(capsule["final_snapshot_revision"], "final snapshot revision")
    if capsule["final_snapshot_sha256"] != checksum(current) or capsule["final_snapshot_revision"] != current["revision"]:
        raise ValueError("Final source capsule differs from authoritative accounting")
    old_ids = {(row["allocation_id"], row["attempt"]) for row in prior["allocations"]}
    new_ids = {(row["allocation_id"], row["attempt"]) for row in current["allocations"]}
    if old_ids != new_ids:
        raise ValueError("Postexit finalization cannot append or omit allocations")
    membership = [{field: row[field] for field in
        ("allocation_id", "attempt", "slurm_job_id", "method", "kind", "factor")}
        for row in sorted(current["allocations"], key=lambda row: (row["allocation_id"], row["attempt"]))]
    if (capsule["allocation_membership_sha256"] != checksum(membership)
            or type(capsule["allocation_count"]) is not int or capsule["allocation_count"] != len(membership)):
        raise ValueError("Source capsule allocation membership changed")
    expected_producer = {"allocation_id": manifest["reservation"]["allocation_id"],
                         "attempt": manifest["attempt"], "slurm_job_id": job_id}
    keys(capsule["comparison_producer"], {"allocation_id", "attempt", "slurm_job_id"})
    integer(capsule["comparison_producer"]["attempt"], "comparison producer attempt", 1)
    if capsule["comparison_producer"] != expected_producer:
        raise ValueError("First comparison native producer changed")
    totals = closed_resource_totals(current, manifest["request"])
    if canonical(capsule["allocation_totals"]) != canonical(totals):
        raise ValueError("Source resource totals contradict authoritative actual costs")
    resource = summary.get("allocation_costs")
    keys(resource, {"schema", "snapshot_sha256", "snapshot_revision", "methods", "factors",
                   "complete", "reference_cost_included_in_method_budget", "measurement",
                   "process_clocks_are_budget_authority"})
    if (resource["schema"] != RESOURCE_SUMMARY_SCHEMA
            or resource["snapshot_sha256"] != checksum(current)
            or resource["snapshot_revision"] != current["revision"]
            or resource["complete"] is not True
            or resource["reference_cost_included_in_method_budget"] is not False
            or resource["process_clocks_are_budget_authority"] is not False
            or resource["measurement"] != "controller-pinned cumulative native allocated GPU/CPU seconds"
            or canonical({key: resource[key] for key in ("methods", "factors")}) != canonical(totals)):
        raise ValueError("Source display resource projection differs from final known costs")
    qualification = publication_qualification(capsule)
    if summary.get("methods") != capsule["source_assessments"]:
        raise ValueError("Display source verdict projection changed")
    if (summary.get("scientific_status") != capsule["scientific_status"]
            or not isinstance(capsule["scientific_status"], str)
            or summary.get("evaluation", {}).get("confirmation_case_count") != 256):
        raise ValueError("Source scientific outcome or full confirmation denominator changed")
    for method in ("hnpe", "tmcmc"):
        flags = qualification[method]
        if totals["methods"][method]["ceiling_exceeded"] and (
                flags["within_method_ceiling"] or flags["accuracy_target_within_budget"]):
            raise ValueError("Source within-budget flags contradict authoritative actual method overrun")
    if any(value["ceiling_exceeded"] for value in totals["factors"].values()) and (
            qualification["hnpe"]["within_factor_ceilings"] or qualification["hnpe"]["accuracy_target_within_budget"]):
        raise ValueError("Source within-budget flags contradict authoritative actual factor overrun")
    for role in ("comparison", "accounting", "summary"):
        if source_artifact_projection(capsule[role]) != {key: value for key, value in roles[role].items() if key != "id"}:
            raise ValueError("Source role differs from verified private artifact")
    if summary.get("comparison") != capsule["comparison"] or summary.get("accounting") != capsule["accounting"]:
        raise ValueError("Bounded display links differ from the first comparison/final accounting")
    downloads = summary.get("downloads")
    if not isinstance(downloads, list) or not 2 <= len(downloads) <= 62:
        raise ValueError("Bounded complete source download descriptor list required")
    curated = [*downloads, capsule["summary"]]
    proof = capsule["curation"]
    keys(proof, {"excludes_validation_capsule_itself", "artifacts_sha256", "file_count", "size_bytes"})
    if (proof["excludes_validation_capsule_itself"] is not True
            or proof["artifacts_sha256"] != checksum(curated)
            or type(proof["file_count"]) is not int or proof["file_count"] != len(curated)
            or type(proof["size_bytes"]) is not int
            or proof["size_bytes"] != sum(item["size_bytes"] for item in curated)):
        raise ValueError("Source curation order/metadata/byte digest changed")
    projected = [source_artifact_projection(item) for item in curated]
    projected.append({key: value for key, value in roles["validation_capsule"].items() if key != "id"})
    if len(projected) > 64 or sum(item["size_bytes"] for item in projected) > 536870912:
        raise ValueError("Complete source curation exceeds its reviewed byte/count envelope")
    refs = [{key: value for key, value in item.items() if key != "id"} for item in artifacts]
    if (len({item["name"] for item in projected}) != len(projected)
            or sorted(projected, key=lambda item: item["name"]) != sorted(refs, key=lambda item: item["name"])):
        raise ValueError("All curated source files must be privately verified exactly once")
    _no_raw_secret(summary)
    _no_raw_secret(capsule)
    return qualification
