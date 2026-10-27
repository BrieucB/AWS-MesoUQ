"""Public semantic controls cannot select scientific runtime files or change profiles."""
import copy

import pytest

from aws_mesouq.contracts import CANONICAL_PROFILE, validate_job_artifacts, validate_job_configuration


def calibration(**overrides):
    return {"scientific_profile": CANONICAL_PROFILE, "calibration_data": "curve_only",
            "phase1_contract_mode": "emb_direct_ka_kb", "prior_ka": [617.0581351146693, 42000.0],
            "prior_kb": [100.0, 48000.0], "prior_sigma": [.005, .3],
            "indentation_prior_sigma": [.045, .11], **overrides}


def test_direct_contract_preserves_separate_noise_ranges_and_editable_bounds():
    request = calibration(calibration_data="accepted_joint", calibration_mode="population", acoustic_prior_sigma=[.001, .01],
                          hyperprior_mu_ka=[8500, 11000], hyperprior_sigma_ka=[300, 1000],
                          indentation_prior_ka_by_diameter_um={"3.2": [700, 41000]},
                          acoustic_prior_ka_by_diameter_um={"4.0": [800, 40000]})
    normalized = validate_job_configuration(request, kind="calibration", diameter_um=3.2)
    assert normalized["prior_sigma"] == [.005, .3]
    assert normalized["indentation_prior_sigma"] == [.045, .11]
    assert normalized["acoustic_prior_sigma"] == [.001, .01]
    normalized["prior_ka"][0] = 999
    assert request["prior_ka"][0] == 617.0581351146693


@pytest.mark.parametrize("override", [
    {"scientific_profile": "untrusted"}, {"calibration_data": "uploaded_acoustics"},
    {"phase1_contract_mode": "legacy"}, {"prior_Yt": [1, 2]}, {"prior_b1": [1, 2]},
    {"hyperprior_mu_sigma": [1, 2]}, {"fixed_params": {"b1": 0, "b2": 0, "a3": 0, "a4": 0}},
    {"resonance": {"evaluator": {"mode": "analytical_dpd_formula"}}},
    {"resonance": {"evaluator": {"mode": "artifact_polynomial_bank", "artifact_path": "/tmp/untrusted"}}},
    {"resonance": {"agent": "definity"}}, {"resonance": {"dpd_constants": {"kappa_g": 1.4}}},
    {"indentation_prior_sigma": [-1, 1]}, {"acoustic_prior_sigma": [.1, .01]},
    {"indentation_prior_ka_by_diameter_um": {"2.6": [1, 2]}},
    {"acoustic_prior_ka_by_diameter_um": {"3.4": [1, 2]}},
    {"acoustic_prior_ka_by_diameter_um": {"4": [1, 2], "4.0": [1, 2]}},
])
def test_canonical_rejects_changed_or_ignored_contract_and_untrusted_physics(override):
    with pytest.raises(ValueError):
        validate_job_configuration(calibration(**copy.deepcopy(override)), kind="calibration", diameter_um=3.2)


def test_joint_reference_requires_explicit_population_and_can_use_one_mechanical_curve():
    request = calibration(calibration_data="accepted_joint")
    with pytest.raises(ValueError, match="population"):
        validate_job_configuration(request, kind="calibration", diameter_um=3.2)
    request["calibration_mode"] = "population"
    normalized = validate_job_configuration(request, kind="calibration", diameter_um=3.2)
    artifacts = [{"id": "curve", "kind": "curve", "metadata": {"nominal_diameter_um": 3.2,
                  "force_unit": "nN", "displacement_unit": "nm", "column_order": "force-displacement"}}]
    validate_job_artifacts(artifacts, kind="calibration", configuration=normalized, diameter_um=3.2)
    with pytest.raises(ValueError, match="at least two"):
        validate_job_artifacts(artifacts, kind="calibration", configuration={**normalized, "calibration_data": "curve_only"}, diameter_um=3.2)


def test_canonical_prediction_requires_direct_material_parameters():
    request = {"scientific_profile": CANONICAL_PROFILE, "parameters": {"ka": 9500, "kb": 20000, "d0": 0, "sigma": .05}}
    assert validate_job_configuration(request, kind="prediction", diameter_um=3.2)["parameters"] == request["parameters"]
    for values in ({"Yt": 1e7, "kb": 20000}, {"ka": -1, "kb": 20000}, {"ka": 1, "kb": 1, "sigma": -1}):
        with pytest.raises(ValueError):
            validate_job_configuration({**request, "parameters": values}, kind="prediction", diameter_um=3.2)


def test_legacy_requests_cannot_silently_select_direct_canonical_controls():
    with pytest.raises(ValueError, match="canonical"):
        validate_job_configuration({"phase1_contract_mode": "emb_direct_ka_kb"}, kind="calibration", diameter_um=3.2)


@pytest.mark.parametrize("setting,value", [("target_cov", "bad"), ("hbi_target_cov", 0), ("hbi_covariance_scaling", -1), ("phase3b_target_cov", True)])
def test_invalid_sampler_controls_are_rejected_before_admission(setting, value):
    with pytest.raises(ValueError):
        validate_job_configuration({"scientific_profile": CANONICAL_PROFILE, "calibration_data": "curve_only", setting: value}, kind="calibration", diameter_um=3.2)


@pytest.mark.parametrize("value", ["bad", 0, -1, True])
def test_invalid_nested_sampling_controls_are_rejected(value):
    with pytest.raises(ValueError):
        validate_job_configuration({"scientific_profile": CANONICAL_PROFILE, "calibration_data": "curve_only", "surrogate": {"predictive_mc_samples": value}}, kind="calibration", diameter_um=3.2)


def test_canonical_normalization_leaves_omitted_surrogate_defaults_to_verified_source():
    config = validate_job_configuration({"scientific_profile": CANONICAL_PROFILE, "calibration_data": "curve_only"}, kind="calibration", diameter_um=3.2)
    assert "surrogate" not in config
