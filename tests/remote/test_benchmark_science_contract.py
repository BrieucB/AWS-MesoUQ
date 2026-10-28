"""Cross-proof against the actual MesoUQ scientific contract; no model or solver calls.

Run in the verified scientific test environment with this repository's src and
the approved MesoUQ src on PYTHONPATH. Metadata-only environments explicitly skip.
"""
import copy
import importlib

import pytest

from aws_mesouq.benchmark import (
    DEFAULT_CONFIGURATION, checksum, scientific_request_checksum, validate_create,
)


@pytest.fixture
def science_contract():
    return pytest.importorskip("meso_uq.benchmark_lab.contracts", reason="Actual scientific contract requires the verified scientific test environment")


@pytest.mark.parametrize("family,scale", [("baseline", 1), ("higher_noise", 2.25), ("broader_priors", 3)])
def test_actual_scientific_contract_accepts_stored_wire_and_matches_both_hashes(science_contract, family, scale):
    request = {**copy.deepcopy(DEFAULT_CONFIGURATION), "idempotency_key": "real-cross-proof"}
    request["scenario"] = {"family": family, "scale": scale}
    stored = validate_create(request)
    scientific = science_contract.validate_request(stored)
    assert stored == scientific
    assert type(stored["controls"]["hnpe"]["training_budget"]) is int
    assert type(stored["budget"]["method_gpu_seconds"]) is int
    assert type(stored["scenario"]["scale"]) is float
    assert scientific_request_checksum(stored) == science_contract.request_fingerprint(scientific)
    assert checksum(stored) == science_contract.fingerprint(scientific)
    changed = copy.deepcopy(stored)
    changed["idempotency_key"] += "-fresh-campaign"
    assert scientific_request_checksum(changed) == science_contract.request_fingerprint(stored)
    assert checksum(changed) != checksum(stored)


@pytest.mark.parametrize("change", ["training_float", "training_short", "too_many_updates", "method_float", "few_scrambles", "wide_reference", "equal_powers", "loose_tolerance"])
def test_actual_scientific_and_cloud_contracts_reject_the_same_invalid_controls(science_contract, change):
    request = {**copy.deepcopy(DEFAULT_CONFIGURATION), "idempotency_key": "invalid-cross-proof"}
    if change == "training_float":
        request["controls"]["hnpe"]["training_budget"] = 1800.0
    elif change == "training_short":
        request["controls"]["hnpe"]["training_budget"] = 119
    elif change == "too_many_updates":
        request["controls"]["hnpe"]["max_updates"] = 1000001
    elif change == "method_float":
        request["budget"]["method_gpu_seconds"] = 14400.0
    elif change == "few_scrambles":
        request["reference"]["scrambles"] = 3
    elif change == "wide_reference":
        request["reference"]["maximum_power"] = 23
    elif change == "equal_powers":
        request["reference"]["initial_power"] = request["reference"]["maximum_power"]
    else:
        request["reference"]["relative_tolerance"] = .11
    with pytest.raises(ValueError):
        validate_create(request)
    with pytest.raises(ValueError):
        science_contract.validate_request(request)
