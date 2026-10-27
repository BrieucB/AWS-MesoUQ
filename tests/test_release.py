import copy

import pytest

from aws_mesouq import release


def curve(diameter=3.2):
    return {
        "id": "curve",
        "name": "curve.csv",
        "kind": "curve",
        "media_type": "text/csv",
        "metadata": {
            "force_unit": "nN",
            "displacement_unit": "nm",
            "column_order": "force-displacement",
            "nominal_diameter_um": diameter,
        },
    }


@pytest.mark.parametrize("diameter", [3.2, 3.4, 5.8])
def test_exact_preset_admitted_for_each_diameter_and_returns_independent_copy(diameter):
    config = release.configuration(diameter)
    original = copy.deepcopy(config)
    normalized = release.validate_configuration(config, kind="calibration", diameter_um=diameter)
    assert normalized == original
    assert normalized["emb_diameters"] == [diameter]
    assert normalized["interval_probability"] == 0.9
    normalized["surrogate"]["backend"] = "changed"
    assert release.configuration(diameter) == config
    assert release.preset()["configuration"]["emb_diameters"] == [3.2]
    assert release.validate_job({
        "kind": "calibration", "diameter_um": diameter,
        "configuration": config, "input_artifacts": [curve(diameter)],
    })["configuration"] == original


def test_equivalent_json_numbers_and_normalized_defaults_do_not_change_science():
    config = release.configuration(3.2)
    del config["experiment"]
    del config["use_surrogate"]
    config["prior_kb"] = [100.0, 48000.0]
    assert release.validate_configuration(config, kind="calibration", diameter_um=3.2) == release.configuration(3.2)


@pytest.mark.parametrize("field,value", [
    ("pop_size", 32), ("max_gen", 1), ("interval_probability", 0.95),
    ("calibration_mode", "population"), ("calibration_data", "accepted_joint"),
    ("scientific_profile", "legacy"), ("emb_diameters", [3.2, 3.4]),
    ("seed", 42), ("use_surrogate", 1), ("frac_diam", True),
    ("prior_ka", [617.0, 42000]), ("surrogate", {"backend": "dnn"}),
])
def test_scientific_edits_rejected(field, value):
    config = release.configuration(3.2)
    config[field] = value
    with pytest.raises((ValueError, TypeError)):
        release.validate_configuration(config, kind="calibration", diameter_um=3.2)


@pytest.mark.parametrize("kind", ["prediction", "training", "population", None])
def test_other_job_kinds_rejected(kind):
    with pytest.raises(ValueError, match="calibration only"):
        release.validate_configuration(release.configuration(3.2), kind=kind, diameter_um=3.2)


@pytest.mark.parametrize("diameter", [True, "3.2", 4.0, None, float("nan")])
def test_invalid_diameters_rejected(diameter):
    with pytest.raises((ValueError, TypeError)):
        release.configuration(diameter)


@pytest.mark.parametrize("mutation", ["none", "two", "table", "media", "extension", "diameter"])
def test_requires_one_csv_mechanical_curve(mutation):
    artifacts = [curve()]
    if mutation == "none":
        artifacts = []
    elif mutation == "two":
        artifacts.append(curve())
    elif mutation == "table":
        artifacts[0]["kind"] = "training_table"
    elif mutation == "media":
        artifacts[0]["media_type"] = "application/json"
    elif mutation == "extension":
        artifacts[0]["name"] = "data.json"
    else:
        artifacts[0]["metadata"]["nominal_diameter_um"] = 3.4
    with pytest.raises(ValueError):
        release.validate_artifacts(artifacts, configuration=release.configuration(3.2), diameter_um=3.2)
