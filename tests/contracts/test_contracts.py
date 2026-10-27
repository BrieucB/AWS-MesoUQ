from __future__ import annotations

import copy
import subprocess
import sys

import pytest

from aws_mesouq.contracts import validate_job_configuration, validate_result


@pytest.mark.parametrize("config", [
    {"prior_Yt": [1e5, 1e9], "prior_sigma": [0, 1], "pop_size": 50000},
    {"fixed_params": {"b1": 0, "b2": 0, "a3": 0, "a4": 0}, "calibration_mode": "population", "emb_diameters": [3.2, 5.8]},
])
def test_scientific_settings_are_editable_and_copied(config):
    original = copy.deepcopy(config)
    validated = validate_job_configuration(config, kind="calibration", diameter_um=3.2)
    assert config == original
    assert validated["surrogate"] == {"backend": "dnn"}


@pytest.mark.parametrize("config", [
    {"command": "touch /tmp/injected"},
    {"surrogate": {"backend": "dnn", "model_path": "/etc/passwd"}},
    {"resonance": {"evaluator": {"artifact_path": "/tmp/model.pkl"}}},
    {"experiment": "compression"},
    {"use_surrogate": False},
    {"surrogate": {"backend": "unsupported"}},
    {"prior_Yt": [10, 1]},
    {"prior_sigma": [-1, 1]},
    {"prior_Yt": [0, float("inf")]},
    {"emb_diameters": [3.5]},
    {"emb_diameters": [3.2, 3.2]},
    {"pop_size": True},
    {"epochs": 0},
    {"fixed_params": {"unknown": 1}},
    {"resonance": {"evaluator": {"mode": "analytical_vacuum_shell"}}},
    {"resonance": {"forward_model": "analytical_vacuum_shell"}},
    {"resonance": {"evaluator": {"mode": "analytical_dpd_formula", "typo": 1}}},
    {"resonance": {"dpd_constants": {"rho_l_dpd": 0}}},
    {"resonance": {"dpd_constants": {"kappa_g": True}}},
    {"resonance": {"dpd_constants": {"rho_l_typo": 1}}},
    {"resonance": {"dpd_constants": {"c_gas_dpd": 1}}},
    {"resonance": {"dpd_constants": {"c_gas_dpd": 1, "rho_gas_dpd": 1, "p_gas_dpd": 1, "kappa_g": 1}}},
    {"resonance": {"radius_dpd_by_diameter_um": {"3.2": -1}}},
    {"resonance": {"radius_dpd_by_diameter_um": {"3.3": 1}}},
    {"resonance": {"radius_dpd_by_diameter_um": {"3.2": 1, "3.20": 2}}},
    {"include_observation_noise": "false"},
    {"hidden_dims": [0]},
    {"disp_source": "auto"},
    {"forces": [-1]},
    {"fixed_params": {"Yt": 123}},
    {"fixed_params": {"b1": 0}},
    {"forces": [0, 1]},
    {"parameters": {"Yt": 123, "kb": 456}},
])
def test_rejects_unsafe_unsupported_or_invalid_settings(config):
    with pytest.raises(ValueError):
        validate_job_configuration(config, kind="calibration", diameter_um=3.2)


@pytest.mark.parametrize("gas", [{"kappa_g": 1.095}, {"c_gas_dpd": 2, "rho_gas_dpd": 1, "p_gas_dpd": 4}])
def test_acoustic_assumptions_are_editable_without_ignored_fields(gas):
    config = {"resonance": {"unit_defaults_modality": "indentation",
              "evaluator": {"mode": "analytical_dpd_formula"},
              "dpd_constants": {"rho_l_dpd": 10, "p0_dpd": 91, **gas},
              "radius_dpd_by_diameter_um": {"3.2": 6.38}}, "include_observation_noise": False}
    original = copy.deepcopy(config)
    result = validate_job_configuration(config, kind="calibration", diameter_um=3.2)
    assert config == original
    assert result["resonance"] == original["resonance"]
    assert result["include_observation_noise"] is False


def result_fixture():
    return {
        "schema_version": 1, "kind": "calibration", "provenance": {
            "job_id": "job", "model_version": "model-v1", "model_sha256": "a" * 64,
            "code_sha256": "b" * 64, "config_sha256": "c" * 64,
            "data_sha256": {"curve": "d" * 64}, "runtime": {"python": "3.11"}},
        "interval_probability": 0.9,
        "parameters": [{"name": "Yt", "unit": "Pa", "lower": 1, "median": 2, "upper": 3}],
        "curves": [{"dataset_id": "curve", "force": [0, 1], "observed_displacement": [0, 2],
                    "lower": [0, 1], "median": [0, 2], "upper": [0, 3],
                    "force_unit": "nN", "displacement_unit": "nm"}],
        "resonance": {"status": "unavailable", "reason": "No validated physical assumptions supplied"},
        "diagnostics": {"uncertainty_includes": ["parameter uncertainty"]},
    }


def test_valid_result_is_independent_json_copy():
    result = result_fixture()
    copied = validate_result(result)
    assert copied == result and copied is not result


@pytest.mark.parametrize("change", [
    lambda r: r["parameters"][0].update(lower=5),
    lambda r: r["curves"][0].update(force=[0]),
    lambda r: r["curves"][0].update(median=[0, float("nan")]),
    lambda r: r.update(provenance={}),
    lambda r: r.update(parameters=[]),
    lambda r: r.update(resonance={"status": "unavailable"}),
    lambda r: r.update(resonance={"status": "available", "lower_mhz": 1, "median_mhz": 2, "upper_mhz": 3}),
    lambda r: r.update(diagnostics={"misleading": float("inf")}),
])
def test_rejects_incomplete_or_misleading_scientific_results(change):
    result = result_fixture()
    change(result)
    with pytest.raises(ValueError):
        validate_result(result)


def test_contract_import_does_not_import_scientific_runtime():
    code = "import sys; import aws_mesouq.contracts; assert not ({'torch','korali','numpy'} & set(sys.modules))"
    subprocess.run([sys.executable, "-c", code], check=True)


def test_training_requires_explicit_interpretation():
    with pytest.raises(ValueError, match="explicit"):
        validate_job_configuration({"epochs": 10}, kind="training", diameter_um=3.2)
    validate_job_configuration({"epochs": 10, "disp_source": "diameter"}, kind="training", diameter_um=3.2)
