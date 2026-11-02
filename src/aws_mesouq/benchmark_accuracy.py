"""Strict wire contract for independently referenced accuracy-target campaigns.

This module does not select execution sources, allocate resources, or authorize
confirmation access. Those identities and decisions belong to the controller.
"""
from __future__ import annotations

from copy import deepcopy
import hashlib
import json
import math
from typing import Any

from . import benchmark as fixed

SCHEMA_VERSION = 2
PROFILE_ID = "w2-rms0p1-central90-2048-244of256-v1"
EFFORT_POLICY_ID = "bounded-effort-ladders-v1"
REFERENCE_PROFILE_ID = "independent-resolution-relative-reference-v1"

_ROOT_KEYS = frozenset(
    "schema_version idempotency_key scenario seed controls evaluation budget accuracy_target".split()
)
_EVALUATION = {
    "cohort": "central_90",
    "guide_case_count": 8,
    "confirmation_case_count": 256,
    "posterior_draws": 2048,
}
_PROFILES = {
    "profile_id": PROFILE_ID,
    "effort_policy_id": EFFORT_POLICY_ID,
    "reference_profile_id": REFERENCE_PROFILE_ID,
}
_REFERENCE = {
    "initial_power": 14,
    "maximum_power": 20,
    "scrambles": 4,
    "relative_tolerance": 0.02,
}


def _keys(value: Any, expected: frozenset[str] | set[str], name: str) -> None:
    if not isinstance(value, dict) or set(value) != expected:
        raise ValueError(f"{name} has missing or unsupported fields")


def _integer(value: Any, name: str) -> None:
    if type(value) is not int:
        raise ValueError(f"{name} must be an integer")


def _finite_number(value: Any, name: str) -> None:
    if type(value) not in (int, float):
        raise ValueError(f"{name} must be a finite number")
    try:
        valid = math.isfinite(value)
    except (OverflowError, ValueError):
        valid = False
    if not valid:
        raise ValueError(f"{name} must be a finite number")


def validate_create(value: Any) -> dict[str, Any]:
    """Return the normalized v2 request, rejecting unknown or unsupported input.

    The unchanged fixed-budget contract supplies the shared scientific control
    bounds. Accuracy-specific evaluation, profiles, and integer types are checked
    before constructing that contract's validation bridge.
    """
    _keys(value, _ROOT_KEYS, "accuracy request")
    _integer(value["schema_version"], "schema_version")
    if value["schema_version"] != SCHEMA_VERSION:
        raise ValueError("schema_version=2 required")
    _integer(value["seed"], "seed")
    if not isinstance(value["idempotency_key"], str) or not 1 <= len(value["idempotency_key"]) <= 128:
        raise ValueError("idempotency_key must be a nonempty string of at most 128 characters")
    _keys(value["scenario"], {"family", "scale"}, "scenario")
    _finite_number(value["scenario"]["scale"], "scenario scale")
    _keys(value["controls"], {"hnpe", "tmcmc"}, "controls")
    _keys(
        value["controls"]["hnpe"],
        {"width", "depth", "learning_rate", "batch_size", "max_updates", "training_budget"},
        "HNPE controls",
    )
    _keys(
        value["controls"]["tmcmc"],
        {"population_size", "target_cov", "proposal_scale", "burn_in", "max_generations"},
        "TMCMC controls",
    )
    for method, names in (
        ("hnpe", ("width", "depth", "batch_size", "max_updates", "training_budget")),
        ("tmcmc", ("population_size", "burn_in", "max_generations")),
    ):
        for name in names:
            _integer(value["controls"][method][name], f"{method}.{name}")
    for method, name in (
        ("hnpe", "learning_rate"),
        ("tmcmc", "target_cov"),
        ("tmcmc", "proposal_scale"),
    ):
        _finite_number(value["controls"][method][name], f"{method}.{name}")
    _keys(value["evaluation"], set(_EVALUATION), "accuracy evaluation")
    for name in ("guide_case_count", "confirmation_case_count", "posterior_draws"):
        _integer(value["evaluation"][name], f"evaluation.{name}")
    if value["evaluation"] != _EVALUATION:
        raise ValueError("declared central_90 accuracy evaluation required")
    _keys(value["accuracy_target"], set(_PROFILES), "accuracy profiles")
    if value["accuracy_target"] != _PROFILES:
        raise ValueError("supported immutable accuracy profiles required")
    _keys(value["budget"], {"mode", "method_gpu_seconds"}, "accuracy budget")
    if value["budget"]["mode"] != "accuracy_target":
        raise ValueError("accuracy_target method-budget mode required")
    _integer(value["budget"]["method_gpu_seconds"], "method_gpu_seconds")
    bridge = {
        "schema_version": 1,
        "idempotency_key": "accuracy-control-validation",
        "scenario": value["scenario"],
        "seed": value["seed"],
        "controls": value["controls"],
        "evaluation": {
            "cohort": "central_90",
            "case_count": 1,
            "posterior_draws": 2048,
        },
        "budget": {
            "mode": "fixed",
            "method_gpu_seconds": value["budget"]["method_gpu_seconds"],
        },
        "reference": deepcopy(_REFERENCE),
    }
    # The transport nonce is opaque data; only shared scientific controls use
    # the v1 bridge. Its narrower token syntax is not the v2 nonce contract.
    fixed.validate_create(deepcopy(bridge))
    result = deepcopy(value)
    result["scenario"]["scale"] = float(result["scenario"]["scale"])
    for method, name in (
        ("hnpe", "learning_rate"),
        ("tmcmc", "target_cov"),
        ("tmcmc", "proposal_scale"),
    ):
        result["controls"][method][name] = float(result["controls"][method][name])
    return result


def scientific_request(value: Any) -> dict[str, Any]:
    """Return the normalized scientific identity, excluding only the nonce."""
    request = validate_create(value)
    del request["idempotency_key"]
    return request


def _checksum(value: dict[str, Any]) -> str:
    raw = json.dumps(
        value, sort_keys=True, separators=(",", ":"), allow_nan=False
    ).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


def transport_request_checksum(value: Any) -> str:
    """Bind all normalized wire fields, including the transport nonce."""
    return _checksum(validate_create(value))


def scientific_request_checksum(value: Any) -> str:
    """Bind the science request independently of transport and campaign IDs."""
    return _checksum(scientific_request(value))
