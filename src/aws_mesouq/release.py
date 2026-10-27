"""Locked, dependency-light public release policy shared by API and connector.

The preset is a source-controlled scientific contract. Selecting one nominal
diameter is the only scientific configuration choice exposed by release v1.
"""

import copy
import json
import re
import math
from importlib import resources

from .contracts import (
    nominal_diameter, validate_job_artifacts, validate_job_configuration,
    validate_result as validate_wire_result,
)

RELEASE_ID = "indentation-sonovue-v1"
CURVE_MAX_POINTS = 1000
_DECIMAL = re.compile(r"^[+-]?(?:\d+\.?\d*|\.\d+)(?:[eE][+-]?\d+)?$", re.ASCII)


def validate_curve_csv(data):
    """Bound the exact uploaded curve without changing or dropping any sample."""
    if not isinstance(data, bytes) or len(data) > 1_048_576:
        raise ValueError("Curve CSV exceeds 1 MiB")
    rows = [[cell.strip() for cell in line.split(",")]
            for line in data.decode("utf-8-sig").strip().split("\n") if line.strip()]
    if not rows:
        raise ValueError("Curve CSV is empty")

    def numeric(cell):
        return bool(_DECIMAL.fullmatch(cell)) and math.isfinite(float(cell))

    if len(rows[0]) == 2 and all(
        re.match(r"^[A-Za-z]", cell) and not numeric(cell)
        and not re.fullmatch(r"nan|[-+]?inf(?:inity)?", cell, re.IGNORECASE)
        for cell in rows[0]
    ):
        rows = rows[1:]
    if not 2 <= len(rows) <= CURVE_MAX_POINTS:
        raise ValueError("Curve CSV requires 2 to 1000 numeric data rows; no rows are truncated")
    if any(len(row) != 2 or any(not numeric(cell) for cell in row) for row in rows):
        raise ValueError("Use an unquoted two-column finite decimal numeric CSV with an optional header")
    return len(rows)


def preset():
    """Return a fresh copy of the canonical release document."""
    return json.loads(
        resources.files("aws_mesouq").joinpath("presets/indentation-v1.json").read_text(encoding="utf-8")
    )


def configuration(diameter_um):
    """Clone the approved settings for one supported nominal diameter."""
    diameter = nominal_diameter(diameter_um)
    result = preset()["configuration"]
    result["emb_diameters"] = [diameter]
    return result


def _equal(left, right):
    # JSON booleans must never compare equal to numeric scientific controls.
    if type(left) in (int, float) and type(right) in (int, float):
        return left == right
    if type(left) is not type(right):
        return False
    if isinstance(left, dict):
        return left.keys() == right.keys() and all(_equal(left[k], right[k]) for k in left)
    if isinstance(left, list):
        return len(left) == len(right) and all(_equal(a, b) for a, b in zip(left, right))
    return left == right


def validate_configuration(value, *, kind, diameter_um):
    """Validate and normalize settings, rejecting every scientific preset edit."""
    if kind != "calibration":
        raise ValueError("Release v1 supports calibration only")
    expected = configuration(diameter_um)
    normalized = validate_job_configuration(value, kind=kind, diameter_um=diameter_um)
    if not _equal(normalized, expected):
        raise ValueError("Release v1 requires the locked approved scientific preset")
    return copy.deepcopy(expected)


def validate_artifacts(artifacts, *, configuration, diameter_um):
    """Require exactly one CSV mechanical curve with explicit supported units."""
    if not isinstance(artifacts, list) or len(artifacts) != 1:
        raise ValueError("Release v1 requires exactly one CSV mechanical curve")
    artifact = artifacts[0]
    if (
        not isinstance(artifact, dict)
        or artifact.get("kind") != "curve"
        or artifact.get("media_type") != "text/csv"
        or not isinstance(artifact.get("name"), str)
        or not artifact["name"].lower().endswith(".csv")
    ):
        raise ValueError("Release v1 requires a CSV mechanical curve")
    validate_job_artifacts(
        artifacts, kind="calibration", configuration=configuration, diameter_um=diameter_um
    )


def validate_job(body):
    """Return an independent v1 job copy after scientific admission checks."""
    if not isinstance(body, dict):
        raise ValueError("Job must be an object")
    result = copy.deepcopy(body)
    normalized = validate_configuration(
        body.get("configuration"), kind=body.get("kind"), diameter_um=body.get("diameter_um")
    )
    validate_artifacts(
        body.get("input_artifacts"), configuration=normalized, diameter_um=body.get("diameter_um")
    )
    result["configuration"] = normalized
    return result


def validate_result(value, *, approved_adapter_sha256):
    """Require the complete portable v1 result and explicit publication bundle.

    The cloud subsequently resolves each reference to its authoritative stored
    artifact and verifies its bytes. This check alone does not prove execution.
    """
    result = validate_wire_result(value)
    if result["kind"] != "calibration" or result["interval_probability"] != 0.9:
        raise ValueError("Release v1 requires calibration results at probability 0.9")
    units = {"ka": "DPD stiffness", "kb": "DPD energy", "d0": "DPD length", "sigma": "relative"}
    parameters = {row["name"]: row for row in result["parameters"]}
    if not {"ka", "kb", "d0"}.issubset(parameters) or set(parameters) - set(units):
        raise ValueError("Release v1 requires ka, kb and d0 parameter intervals")
    for name, row in parameters.items():
        if row["unit"] != units[name] or row["lower"] < 0:
            raise ValueError("Release v1 parameter units or physical bounds differ")
        if name in {"ka", "kb"} and row["lower"] <= 0:
            raise ValueError("Material parameters must be strictly positive")
    if len(result["curves"]) != 1:
        raise ValueError("Release v1 requires exactly one fitted curve")
    curve = result["curves"][0]
    if len(curve["force"]) > CURVE_MAX_POINTS:
        raise ValueError("Release v1 fitted curves contain at most 1000 points")
    if curve["force_unit"] not in {"N", "nN", "uN", "µN"} or curve["displacement_unit"] not in {"m", "nm", "um", "µm"}:
        raise ValueError("Fitted curve requires supported physical units")
    resonance = result["resonance"]
    if resonance["status"] == "available":
        rows = resonance.get("by_dataset", [resonance])
        if len(rows) != 1 or rows[0].get("dataset_id") != curve["dataset_id"]:
            raise ValueError("Resonance must identify the fitted dataset")
        row = rows[0]
        fixed_kb = row.get("conditional_fixed_kb_dpd")
        if (type(fixed_kb) not in (int, float) or fixed_kb <= 0
                or row.get("kb_uncertainty_propagated") is not False
                or not resonance.get("assumptions")):
            raise ValueError("Resonance must state conditional fixed-kb assumptions")
    runtime = result["provenance"]["runtime"]
    if (not isinstance(approved_adapter_sha256, str)
            or not re.fullmatch(r"[a-f0-9]{64}", approved_adapter_sha256)
            or runtime.get("adapter_sha256") != approved_adapter_sha256):
        raise ValueError("Result adapter SHA256 differs from the operator-approved adapter")
    allowed_runtime = {"python", "numpy", "torch", "device", "site", "profile", "environment_sha256", "korali_module_sha256", "adapter_sha256"}
    if set(runtime) - allowed_runtime:
        raise ValueError("Runtime provenance contains unsupported fields")
    for key, item in runtime.items():
        if key == "profile":
            if not isinstance(item, dict) or any(not k.endswith("_sha256") or not isinstance(v, str) or not re.fullmatch(r"[a-f0-9]{64}", v) for k, v in item.items()):
                raise ValueError("Runtime profile must contain only portable SHA256 fingerprints")
        elif key.endswith("_sha256"):
            if not isinstance(item, str) or not re.fullmatch(r"[a-f0-9]{64}", item):
                raise ValueError("Runtime fingerprints must be SHA256")
        elif not isinstance(item, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.:+-]{0,127}", item):
            raise ValueError("Runtime identity must be portable")
    names = result.get("publication_files")
    required = {
        "result.json", "provenance.json", "settings_and_versions.json",
        "parameter_intervals.csv", "fitted_curve.csv", "resonance.csv",
        *[f"figures/{stem}.{ext}" for stem in ("posterior", "fitted_curve", "resonance") for ext in ("png", "pdf")],
    }
    optional = {"scientific_configuration.json", "scientific_inputs.json", "derived_configuration.json", "tempering.json", "scientific_preflight.json"}
    if (not isinstance(names, list) or any(not isinstance(name, str) for name in names)
            or len(names) != len(set(names)) or not required.issubset(names)):
        raise ValueError("Release v1 requires the complete explicit publication bundle")
    posteriors = [name for name in names if re.fullmatch(r"posterior_[A-Za-z0-9_.-]+\.csv", name)]
    if len(posteriors) != 1 or set(names) - required - optional - set(posteriors):
        raise ValueError("Release v1 requires one posterior CSV and approved publication names")
    refs = result.get("artifacts")
    if (not isinstance(refs, list) or len(refs) != len(names)
            or any(not isinstance(ref, dict) or not isinstance(ref.get("id"), str) for ref in refs)
            or len({ref["id"] for ref in refs}) != len(refs)
            or len({ref.get("name") for ref in refs}) != len(refs)):
        raise ValueError("Every publication file requires one unique output artifact")
    if any(type(ref.get("size_bytes")) is not int or ref["size_bytes"] <= 0
           or not isinstance(ref.get("sha256"), str)
           or not re.fullmatch(r"[a-f0-9]{64}", ref["sha256"]) for ref in refs):
        raise ValueError("Publication references require exact byte lengths and SHA256 checksums")
    by_name = {ref.get("name"): ref for ref in refs}
    if set(by_name) != {name.replace("/", "__") for name in names}:
        raise ValueError("Publication references do not match the declared output files")
    for name in names:
        ref = by_name[name.replace("/", "__")]
        kind = "manifest" if name == "provenance.json" else "result"
        media = "application/json" if name.endswith(".json") else "text/csv" if name.endswith(".csv") else "image/png" if name.endswith(".png") else "application/pdf"
        if ref.get("kind") != kind or ref.get("media_type") != media:
            raise ValueError("Publication artifact kind or media type differs from its file")
    return result
