"""Accuracy-v2 wire contract tests; no scientific package dependency."""
from copy import deepcopy
import math
import pytest

from aws_mesouq.benchmark_accuracy import (
    validate_create,
    scientific_request,
    scientific_request_checksum,
    transport_request_checksum,
)


@pytest.fixture
def campaign_request():
    return {
        "schema_version": 2,
        "idempotency_key": "wire-compatibility-example",
        "scenario": {"family": "baseline", "scale": 1},
        "seed": 1000,
        "controls": {
            "hnpe": {"width": 128, "depth": 2, "learning_rate": 0.0003,
                     "batch_size": 256, "max_updates": 1000000, "training_budget": 1800},
            "tmcmc": {"population_size": 4096, "target_cov": 0.6, "proposal_scale": 1,
                      "burn_in": 7, "max_generations": 1000},
        },
        "evaluation": {"cohort": "central_90", "guide_case_count": 8,
                       "confirmation_case_count": 256, "posterior_draws": 2048},
        "budget": {"mode": "accuracy_target", "method_gpu_seconds": 14400},
        "accuracy_target": {
            "profile_id": "w2-rms0p1-central90-2048-244of256-v1",
            "effort_policy_id": "bounded-effort-ladders-v1",
            "reference_profile_id": "independent-resolution-relative-reference-v1",
        },
    }


def assign(campaign_request, path, value):
    target = campaign_request
    parts = path.split(".")
    for part in parts[:-1]:
        target = target[part]
    target[parts[-1]] = value


def test_normalization_does_not_mutate_input(campaign_request):
    before = deepcopy(campaign_request)
    validated = validate_create(campaign_request)
    assert campaign_request == before
    assert validated == before
    assert type(validated["scenario"]["scale"]) is float
    assert type(validated["controls"]["tmcmc"]["proposal_scale"]) is float
    validated["controls"]["hnpe"]["width"] = 64
    assert campaign_request["controls"]["hnpe"]["width"] == 128


def test_transport_nonce_is_distinct_from_scientific_identity(campaign_request):
    other = deepcopy(campaign_request)
    other["idempotency_key"] = "another-transport-nonce"
    assert scientific_request_checksum(other) == scientific_request_checksum(campaign_request)
    assert transport_request_checksum(other) != transport_request_checksum(campaign_request)
    assert set(campaign_request) - set(scientific_request(campaign_request)) == {"idempotency_key"}


def test_json_key_order_and_explicit_float_normalization(campaign_request):
    other = dict(reversed(list(deepcopy(campaign_request).items())))
    other["scenario"]["scale"] = 1.0
    other["controls"]["tmcmc"]["proposal_scale"] = 1.0
    assert transport_request_checksum(other) == transport_request_checksum(campaign_request)


def test_changed_science_changes_both_identities(campaign_request):
    other = deepcopy(campaign_request)
    other["seed"] += 1
    assert scientific_request_checksum(other) != scientific_request_checksum(campaign_request)
    assert transport_request_checksum(other) != transport_request_checksum(campaign_request)


@pytest.mark.parametrize("path", [
    "schema_version", "seed", "controls.hnpe.width", "controls.hnpe.depth",
    "controls.hnpe.batch_size", "controls.hnpe.max_updates", "controls.hnpe.training_budget",
    "controls.tmcmc.population_size", "controls.tmcmc.burn_in", "controls.tmcmc.max_generations",
    "evaluation.guide_case_count", "evaluation.confirmation_case_count",
    "evaluation.posterior_draws", "budget.method_gpu_seconds",
])
@pytest.mark.parametrize("invalid", [True, False, 1.0, "1", None])
def test_integer_fields_reject_boolean_and_coercions(campaign_request, path, invalid):
    assign(campaign_request, path, invalid)
    with pytest.raises(ValueError):
        validate_create(campaign_request)


@pytest.mark.parametrize("path", [
    "scenario.scale", "controls.hnpe.learning_rate",
    "controls.tmcmc.target_cov", "controls.tmcmc.proposal_scale",
])
@pytest.mark.parametrize("invalid", [True, False, math.nan, math.inf, -math.inf, "1", None, 10**1000])
def test_numeric_fields_reject_invalid_or_nonfinite_values(campaign_request, path, invalid):
    assign(campaign_request, path, invalid)
    with pytest.raises(ValueError):
        validate_create(campaign_request)


@pytest.mark.parametrize("path,invalid", [
    ("schema_version", 1), ("schema_version", 3),
    ("scenario.family", "unsupported"), ("scenario.scale", 0.5),
    ("controls.hnpe.width", 17), ("controls.hnpe.depth", 0),
    ("controls.hnpe.max_updates", 1000001), ("controls.hnpe.training_budget", 119),
    ("controls.tmcmc.population_size", 17), ("controls.tmcmc.max_generations", 99),
    ("evaluation.cohort", "all_prior"), ("evaluation.guide_case_count", 16),
    ("evaluation.confirmation_case_count", 255), ("evaluation.posterior_draws", 1024),
    ("budget.mode", "fixed_budget"), ("budget.method_gpu_seconds", 0),
    ("accuracy_target.profile_id", "different-profile"),
    ("accuracy_target.effort_policy_id", "different-policy"),
    ("accuracy_target.reference_profile_id", "different-reference"),
])
def test_rejects_unsupported_controls_or_accuracy_profiles(campaign_request, path, invalid):
    assign(campaign_request, path, invalid)
    with pytest.raises(ValueError):
        validate_create(campaign_request)


@pytest.mark.parametrize("path", [
    "", "scenario", "controls", "controls.hnpe", "controls.tmcmc",
    "evaluation", "budget", "accuracy_target",
])
def test_unknown_fields_are_rejected_at_every_boundary(campaign_request, path):
    node = campaign_request
    for part in filter(None, path.split(".")):
        node = node[part]
    node["source_commit"] = "caller-controlled-source"
    with pytest.raises(ValueError):
        validate_create(campaign_request)


@pytest.mark.parametrize("missing", [
    "schema_version", "idempotency_key", "scenario", "seed", "controls",
    "evaluation", "budget", "accuracy_target",
])
def test_missing_top_level_fields_rejected(campaign_request, missing):
    del campaign_request[missing]
    with pytest.raises(ValueError):
        validate_create(campaign_request)


@pytest.mark.parametrize("invalid", [None, [], "campaign_request", 2])
def test_top_level_must_be_object(invalid):
    with pytest.raises(ValueError):
        validate_create(invalid)

@pytest.mark.parametrize("nonce", ["a b", "line\nbreak", "unicode-µ", "__", "a.b", "x/y", "a" * 128])
def test_transport_nonce_is_opaque_bounded_data(campaign_request, nonce):
    campaign_request["idempotency_key"] = nonce
    assert validate_create(campaign_request)["idempotency_key"] == nonce
    assert len(transport_request_checksum(campaign_request)) == 64


@pytest.mark.parametrize("nonce", ["", "a" * 129, None, True, 1])
def test_transport_nonce_requires_nonempty_bounded_string(campaign_request, nonce):
    campaign_request["idempotency_key"] = nonce
    with pytest.raises(ValueError):
        validate_create(campaign_request)


# Generated independently by frozen scientific policy 8a11514715b785c5dff57dca422ce012a53e9ab9d5e0b136fad456402b79f9ae
# in native QA5308458; original comparison failure and oracle output are retained.
@pytest.mark.parametrize("family,scale,expected_transport,expected_science", [('baseline', 1, '9afaffefaed640ef83f85db4e8d5a1c4fe3be6269cd3893f9e07e6b360cdc91a', 'f88eb40b27a30bc2dfd1db06fe2896f67857f1f05d5757330eeb5e8a67a9e252'), ('higher_noise', 1.25, '23d945069a99ce9476ff0af30f50273d3f056d9a2904543c6e54c41b3d866528', 'eee269108c02d8380011f28ff9fd6b35a8e22bc7c9b4ab69778685f1713e2bfb'), ('broader_priors', 1.5, 'b9d7df156ebc6e02b30d215fbbf7ca3b4a42fb1cf3407995ad8d305f9eec27d6', '32b5373e56a4b452b84ab40e15a8d8a7644203be17cd8bb7463c969b75551900')])
def test_frozen_scientific_policy_identity_goldens(
    campaign_request, family, scale, expected_transport, expected_science
):
    campaign_request["scenario"] = {"family": family, "scale": scale}
    assert transport_request_checksum(campaign_request) == expected_transport
    assert scientific_request_checksum(campaign_request) == expected_science
