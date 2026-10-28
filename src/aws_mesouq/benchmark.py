"""Portable Benchmark Lab wire contracts. No scientific runtime imports."""
from __future__ import annotations

import hashlib
import json
import math
import re

SCHEMA = "aws_mesouq.benchmark_lab.v1"
FAMILIES = ("baseline", "higher_noise", "broader_priors")
FACTORS = ("global", "mechanical_1", "mechanical_2", "mechanical_3", "acoustic_1", "acoustic_2", "acoustic_3")
TERMINAL = frozenset(("succeeded", "failed", "cancelled", "blocked"))
ARTIFACT_PROJECT_BYTES = 1073741824
ARTIFACT_CAMPAIGN_BYTES = 1073741824
ARTIFACT_SINGLE_BYTES = 100000000
ARTIFACT_MAX_COUNT = 100
ARTIFACT_OVERHEAD_BYTES = 8 * 1024 * 1024


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True, allow_nan=False).encode()


def checksum(value):
    return hashlib.sha256(canonical(value)).hexdigest()


def keys(value, required, optional=()):
    if not isinstance(value, dict) or set(value) - set(required) - set(optional) or set(required) - set(value):
        raise ValueError("Unexpected or missing benchmark fields")


def number(value, name, low, high, integer=False):
    if type(value) not in ((int,) if integer else (int, float)) or not math.isfinite(value) or not low <= value <= high:
        raise ValueError(f"{name} is outside its supported range")
    return value


def token(value, name="identifier"):
    if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}", value):
        raise ValueError(f"Invalid {name}")
    return value


def validate_create(value):
    keys(value, ("schema_version", "idempotency_key", "scenario", "seed", "controls", "evaluation", "budget", "reference"))
    if type(value["schema_version"]) is not int or value["schema_version"] != 1:
        raise ValueError("Benchmark schema_version must be 1")
    token(value["idempotency_key"], "idempotency key")
    number(value["seed"], "seed", 0, 2147483647, True)
    scenario = value["scenario"]
    keys(scenario, ("family", "scale"))
    if scenario["family"] not in FAMILIES:
        raise ValueError("Unsupported benchmark scenario family")
    number(scenario["scale"], "scenario scale", 1, 3)
    if scenario["scale"] * 4 != int(scenario["scale"] * 4) or (scenario["family"] == "baseline" and scenario["scale"] != 1):
        raise ValueError("Scenario scale uses steps of 0.25; baseline requires 1")
    controls = value["controls"]
    keys(controls, ("hnpe", "tmcmc"))
    hnpe = controls["hnpe"]
    keys(hnpe, ("width", "depth", "learning_rate", "batch_size", "max_updates", "training_budget"))
    for name, allowed in (("width", (64, 128, 256)), ("depth", (1, 2, 3)), ("learning_rate", (0.0001, 0.0003)), ("batch_size", (256, 512, 1024, 2048))):
        if type(hnpe[name]) not in (int, float) or hnpe[name] not in allowed:
            raise ValueError(f"Unsupported HNPE {name}")
    for name in ("width", "depth", "batch_size"):
        if type(hnpe[name]) is not int:
            raise ValueError(f"HNPE {name} must be an integer")
    number(hnpe["max_updates"], "HNPE max updates", 1, 1000000, True)
    number(hnpe["training_budget"], "HNPE per-factor training seconds", 120, 14400, True)
    tmcmc = controls["tmcmc"]
    keys(tmcmc, ("population_size", "target_cov", "proposal_scale", "burn_in", "max_generations"))
    for name, allowed in (("population_size", (2048, 4096, 8192)), ("target_cov", (0.4, 0.6, 0.8)), ("proposal_scale", (0.25, 0.5, 1.0)), ("burn_in", (1, 7, 10))):
        if type(tmcmc[name]) not in (int, float) or tmcmc[name] not in allowed:
            raise ValueError(f"Unsupported TMCMC {name}")
    for name in ("population_size", "burn_in"):
        if type(tmcmc[name]) is not int:
            raise ValueError(f"TMCMC {name} must be an integer")
    number(tmcmc["max_generations"], "TMCMC maximum generations", 100, 10000, True)
    evaluation = value["evaluation"]
    keys(evaluation, ("case_count", "posterior_draws", "cohort"))
    number(evaluation["case_count"], "case count", 1, 256, True)
    number(evaluation["posterior_draws"], "posterior draws", 256, 8192, True)
    if evaluation["cohort"] != "central_90":
        raise ValueError("Unsupported matched evaluation cohort")
    budget = value["budget"]
    keys(budget, ("mode", "method_gpu_seconds"))
    if budget["mode"] != "fixed":
        raise ValueError("Benchmark comparison uses a fixed total method budget")
    number(budget["method_gpu_seconds"], "total method GPU seconds", 1, 2147483647, True)
    reference = value["reference"]
    keys(reference, ("initial_power", "maximum_power", "scrambles", "relative_tolerance"))
    number(reference["initial_power"], "reference initial power", 4, 20, True)
    number(reference["maximum_power"], "reference maximum power", 5, 22, True)
    if reference["maximum_power"] <= reference["initial_power"]:
        raise ValueError("Reference maximum power cannot precede initial power")
    number(reference["scrambles"], "reference scrambles", 4, 16, True)
    number(reference["relative_tolerance"], "reference relative tolerance", 0.0, 0.1)
    if reference["relative_tolerance"] == 0:
        raise ValueError("Reference relative tolerance must be positive")
    normalized = json.loads(canonical(value))
    normalized["scenario"]["scale"] = round(float(scenario["scale"]) * 4) / 4
    return normalized


def scientific_request_checksum(value):
    """Science identity excludes transport idempotency; full campaign identity retains it."""
    normalized = validate_create(value)
    return checksum({k: v for k, v in normalized.items() if k != "idempotency_key"})


DEFAULT_CONFIGURATION = {
    "schema_version": 1, "scenario": {"family": "baseline", "scale": 1}, "seed": 1000,
    "controls": {
        "hnpe": {"width": 128, "depth": 2, "learning_rate": 0.0003, "batch_size": 2048, "max_updates": 1000000, "training_budget": 1800},
        "tmcmc": {"population_size": 4096, "target_cov": 0.6, "proposal_scale": 1.0, "burn_in": 7, "max_generations": 1000},
    },
    "evaluation": {"case_count": 16, "posterior_draws": 2048, "cohort": "central_90"},
    "budget": {"mode": "fixed", "method_gpu_seconds": 14400},
    "reference": {"initial_power": 14, "maximum_power": 20, "scrambles": 4, "relative_tolerance": 0.02},
}


def capabilities():
    return {
        "schema": SCHEMA, "families": list(FAMILIES), "compute_site": "karolina",
        "scale": {"minimum": 1, "maximum": 3, "step": 0.25, "baseline": 1, "nonbaseline_default": 2},
        "defaults": json.loads(canonical(DEFAULT_CONFIGURATION)),
        "controls": {"hnpe": {"width": [64, 128, 256], "depth": [1, 2, 3], "learning_rate": [0.0001, 0.0003], "batch_size": [256, 512, 1024, 2048], "training_budget": {"minimum": 120, "maximum": 14400, "unit": "seconds_per_factor"}, "max_updates": {"minimum": 1, "maximum": 1000000}, "flow_blocks": 6, "bins": 8}, "tmcmc": {"population_size": [2048, 4096, 8192], "target_cov": [0.4, 0.6, 0.8], "proposal_scale": [0.25, 0.5, 1.0], "burn_in": [1, 7, 10], "max_generations": {"minimum": 100, "maximum": 10000}}},
        "evaluation": {"case_count": {"minimum": 1, "maximum": 256}, "posterior_draws": {"minimum": 256, "maximum": 8192}, "cohort": ["central_90"]},
        "primary_cost": "training_plus_inference", "reference_cost": "reported_separately",
        "budget_semantics": "per_method_total_gpu_seconds_including_preparation_training_and_inference",
        "factor_ids": list(FACTORS), "fresh_training_required": True,
        "artifact_budget": {"campaign_bytes": ARTIFACT_CAMPAIGN_BYTES, "project_bytes": ARTIFACT_PROJECT_BYTES, "artifact_bytes": ARTIFACT_SINGLE_BYTES, "artifact_count": ARTIFACT_MAX_COUNT, "posterior_methods": 3, "posterior_parameters": 21, "float64_bytes": 8, "overhead_bytes": ARTIFACT_OVERHEAD_BYTES, "reservation": "expected_bytes_reserved_at_creation_including_later_uploads"},
        "actions": {"cancel": True, "archive": True},
        "limits": {
            "scenario.scale": {"minimum": 1, "maximum": 3, "step": 0.25},
            "controls.hnpe.width": {"choices": [64, 128, 256]},
            "controls.hnpe.depth": {"choices": [1, 2, 3]},
            "controls.hnpe.learning_rate": {"choices": [0.0001, 0.0003]},
            "controls.hnpe.batch_size": {"choices": [256, 512, 1024, 2048]},
            "controls.hnpe.max_updates": {"minimum": 1, "maximum": 1000000},
            "controls.hnpe.training_budget": {"minimum": 120, "maximum": 14400},
            "controls.tmcmc.population_size": {"choices": [2048, 4096, 8192]},
            "controls.tmcmc.target_cov": {"choices": [0.4, 0.6, 0.8]},
            "controls.tmcmc.proposal_scale": {"choices": [0.25, 0.5, 1.0]},
            "controls.tmcmc.burn_in": {"choices": [1, 7, 10]},
            "controls.tmcmc.max_generations": {"minimum": 100, "maximum": 10000},
            "evaluation.case_count": {"minimum": 1, "maximum": 256},
            "evaluation.posterior_draws": {"minimum": 256, "maximum": 8192},
            "budget.method_gpu_seconds": {"minimum": 1, "maximum": 2147483647},
            "reference.initial_power": {"minimum": 4, "maximum": 20},
            "reference.maximum_power": {"minimum": 5, "maximum": 22},
            "reference.scrambles": {"minimum": 4, "maximum": 16},
            "reference.relative_tolerance": {"exclusiveMinimum": 0, "maximum": 0.1},
        },
    }


def stage_plan():
    stages = [{"id": "cases", "kind": "cases", "depends_on": []}]
    stages += [{"id": "train_" + factor, "kind": "hnpe_train", "factor": factor, "depends_on": ["cases"]} for factor in FACTORS]
    stages += [
        {"id": "hnpe_infer", "kind": "hnpe_infer", "depends_on": ["train_" + f for f in FACTORS]},
        {"id": "tmcmc", "kind": "tmcmc", "depends_on": ["cases"]},
        {"id": "reference", "kind": "reference", "depends_on": ["cases"]},
        {"id": "metrics", "kind": "metrics", "depends_on": ["hnpe_infer", "tmcmc", "reference"], "dependency_policy": "terminal"},
    ]
    return [{**stage, "status": "pending", "attempt": 0, "revision": 1} for stage in stages]


def claim_signing_bytes(owner_sub, body):
    return canonical({"schema": SCHEMA, "type": "claim", "owner_sub": owner_sub, "request": {k: v for k, v in body.items() if k != "signature"}})


def stage_signing_bytes(campaign, stage, body):
    return canonical({
        "schema": SCHEMA, "type": "stage_event",
        "campaign_id": campaign["id"], "stage_id": stage["id"], "attempt": stage["attempt"],
        "project_id": campaign["project_id"], "owner_sub": campaign["owner_sub"],
        "configuration_hash": campaign["configuration_hash"], "approved_adapter": campaign["approved_adapter"],
        "event": {k: v for k, v in body.items() if k not in ("signature", "lease_token")},
    })


def validate_artifacts(refs):
    if not isinstance(refs, list) or len(refs) > 100:
        raise ValueError("Invalid benchmark artifact list")
    seen = set()
    for ref in refs:
        keys(ref, ("name", "sha256", "size_bytes", "media_type"), ("id",))
        token(ref["name"], "artifact name")
        if ref["name"] in seen:
            raise ValueError("Duplicate benchmark artifact name")
        seen.add(ref["name"])
        if not isinstance(ref["sha256"], str) or not re.fullmatch("[0-9a-f]{64}", ref["sha256"]):
            raise ValueError("Invalid artifact SHA256")
        number(ref["size_bytes"], "artifact size", 1, ARTIFACT_SINGLE_BYTES, True)
        if ref["media_type"] not in ("application/json", "text/csv", "image/png", "image/svg+xml", "application/octet-stream"):
            raise ValueError("Unsupported benchmark artifact media type")
    return refs


def validate_stage_result(result, campaign, stage):
    keys(result, ("schema", "provenance", "timing", "artifacts"), ("metrics", "summary"))
    if result["schema"] != SCHEMA:
        raise ValueError("Unexpected benchmark result schema")
    provenance = result["provenance"]
    keys(provenance, ("campaign_id", "configuration_hash", "adapter_sha256", "science_revision", "site", "case_set_sha256"), ("model_sha256",))
    expected = {"campaign_id": campaign["id"], "configuration_hash": campaign["configuration_hash"], "adapter_sha256": campaign["approved_adapter"]["adapter_sha256"], "science_revision": campaign["approved_adapter"]["science_revision"], "site": "karolina"}
    if any(provenance.get(k) != v for k, v in expected.items()):
        raise ValueError("Result provenance does not bind the approved campaign")
    if not isinstance(provenance["case_set_sha256"], str) or not re.fullmatch("[0-9a-f]{64}", provenance["case_set_sha256"]):
        raise ValueError("Result requires an immutable matched case-set hash")
    if stage["id"] != "cases" and provenance["case_set_sha256"] != campaign.get("case_set_sha256"):
        raise ValueError("All methods and reference must use the identical frozen cases")
    timing = result["timing"]
    keys(timing, ("wall_seconds", "cpu_seconds", "gpu_seconds"), ("queue_seconds", "accounting_source"))
    for name in ("wall_seconds", "cpu_seconds", "gpu_seconds", "queue_seconds"):
        if name in timing:
            number(timing[name], name, 0, 1e12)
    validate_artifacts(result["artifacts"])
    canonical(result)
    return result


def archive_signing_bytes(campaign, body):
    return canonical({
        "schema": SCHEMA, "type": "archive_verified",
        "campaign_id": campaign["id"], "project_id": campaign["project_id"],
        "owner_sub": campaign["owner_sub"], "configuration_hash": campaign["configuration_hash"],
        "approved_adapter": campaign["approved_adapter"],
        "event": {k: v for k, v in body.items() if k != "signature"},
    })


def validate_benchmark_policy(policy):
    """Operator-only policy; never accepted from campaign request fields."""
    from pathlib import PurePosixPath
    keys(policy, ("adapter_sha256", "science_revision", "scenario_families"), ("max_concurrent_stages", "archive_root"))
    for name, length in (("adapter_sha256", 64), ("science_revision", 40)):
        if not isinstance(policy[name], str) or not re.fullmatch("[0-9a-f]{" + str(length) + "}", policy[name]):
            raise ValueError("Invalid operator benchmark source pin")
    families = policy["scenario_families"]
    if not isinstance(families, list) or not families or any(not isinstance(f, str) or f not in FAMILIES for f in families) or len(set(families)) != len(families):
        raise ValueError("Operator benchmark scenario families must be a unique supported list")
    if "max_concurrent_stages" in policy:
        number(policy["max_concurrent_stages"], "operator stage concurrency", 1, 64, True)
    if "archive_root" in policy:
        root = policy["archive_root"]
        if not isinstance(root, str) or len(root) > 2048 or not re.fullmatch(r"/[A-Za-z0-9_./-]+", root):
            raise ValueError("Invalid operator durable archive root")
        path = PurePosixPath(root)
        if not path.is_absolute() or str(path) != root or root == "/" or ".." in path.parts:
            raise ValueError("Operator archive root must be a canonical absolute private directory")
    return json.loads(canonical(policy))


def validate_benchmark_policies(policies):
    if policies is None:
        return {}
    if not isinstance(policies, dict):
        raise ValueError("Trusted benchmark deployment policies must be a project mapping")
    validated = {}
    for project, policy in policies.items():
        token(project, "operator project identifier")
        validated[project] = validate_benchmark_policy(policy)
    return validated


def expected_artifact_bytes(configuration):
    evaluation = configuration["evaluation"]
    return 3 * evaluation["case_count"] * evaluation["posterior_draws"] * 21 * 8 + ARTIFACT_OVERHEAD_BYTES


def campaign_artifact_charge(campaign):
    actual = sum(ref["size_bytes"] for ref in campaign.get("artifacts", {}).values())
    reservation = campaign.get("artifact_reservation", {})
    return max(actual, reservation.get("expected_bytes", 0)) if reservation.get("status") == "active" else actual


def project_artifact_charge(state, project):
    legacy = sum(ref["size_bytes"] for ref in state["artifacts"].values() if ref.get("project_id") == project and not ref.get("reservation_released"))
    return legacy + sum(campaign_artifact_charge(campaign) for campaign in state["benchmark_campaigns"].values() if campaign["project_id"] == project)
