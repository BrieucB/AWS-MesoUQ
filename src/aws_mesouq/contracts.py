"""Wire-level validation shared by the cloud service and scientific runner.

Keep this module stdlib-only: the API and connector must not install the scientific
runtime simply to validate a request. Scientific execution performs further
model/data-support checks in the explicitly imported runner.
"""

from __future__ import annotations

import copy
import json
import math
import re
from collections.abc import Mapping
from typing import Any

SUPPORTED_DIAMETERS = (3.2, 3.4, 5.8)
CANONICAL_PROFILE = "uq_emb_sonovue_202607"
JOB_KINDS = frozenset({"calibration", "prediction", "training"})
PARAMETERS = frozenset({"Yt", "ka", "kb", "b1", "b2", "a3", "a4", "d0", "sigma"})
RESERVED_KEYS = frozenset({
    "data_dir", "data_files", "data_prefix", "surrogate_dir", "out", "output_dir",
    "repository_root", "run_root", "runtime", "python_bin", "script", "command",
    "sbatch", "site", "account", "partition", "job_env", "env", "environment",
    "experiments", "checkpoint", "model_path", "artifact_path", "init_path",
})
SCIENTIFIC_KEYS = frozenset({
    "scientific_profile", "phase1_contract_mode", "calibration_data", "indentation_prior_sigma", "acoustic_prior_sigma",
    "indentation_prior_ka_by_diameter_um", "indentation_prior_kb_by_diameter_um", "acoustic_prior_ka_by_diameter_um",
    "experiment", "emb_diameters", "frac_diam", "use_surrogate", "surrogate",
    "pop_size", "max_gen", "target_cov", "covariance_scaling", "phase1_burn_in",
    "hbi_pop_size", "hbi_burn_in", "hbi_target_cov", "hbi_covariance_scaling",
    "phase2_backend", "phase3b_pop_size", "phase3b_max_gen", "phase3b_target_cov",
    "phase3b_covariance_scaling", "fixed_params", "debug", "dump",
    "map_n_displacements", "description", "seed", "interval_probability",
    "max_predictive_samples", "calibration_mode", "parameters",
    "epochs", "batch_size", "learning_rate",
    "hidden_dims", "validation_fraction", "disp_source", "rupture_ratio_threshold",
    "resonance", "parameterization", "emb_parameterization", "preprocessing",
    "include_observation_noise", "validation_max_relative_rmse",
})
INTEGER_KEYS = frozenset({
    "pop_size", "hbi_pop_size", "phase3b_pop_size", "phase1_burn_in", "hbi_burn_in",
    "map_n_displacements", "seed", "max_predictive_samples", "epochs", "batch_size",
})
_PATH_KEY = re.compile(r"(^|_)(path|dir|file|command|script|module|env)(_|$)")


def _number(value: Any, label: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        raise ValueError(f"{label} must be a finite number")
    return float(value)


def _json_tree(value: Any, label: str = "configuration", depth: int = 0) -> None:
    if depth > 12:
        raise ValueError(f"{label} is nested too deeply")
    if isinstance(value, Mapping):
        if len(value) > 256:
            raise ValueError(f"{label} has too many fields")
        for key, child in value.items():
            if not isinstance(key, str) or key.startswith("__"):
                raise ValueError(f"{label} contains an invalid field name")
            if key in RESERVED_KEYS or _PATH_KEY.search(key):
                raise ValueError(f"{label}.{key} is a server-owned runtime field")
            _json_tree(child, f"{label}.{key}", depth + 1)
    elif isinstance(value, list):
        if len(value) > 100_000:
            raise ValueError(f"{label} has too many entries")
        for child in value:
            _json_tree(child, label, depth + 1)
    elif isinstance(value, (int, float)) and not isinstance(value, bool):
        _number(value, label)
    elif value is not None and not isinstance(value, (str, bool)):
        raise ValueError(f"{label} must contain only JSON data")


def nominal_diameter(value: Any) -> float:
    diameter = _number(value, "diameter_um")
    if diameter not in SUPPORTED_DIAMETERS:
        raise ValueError(f"Unsupported nominal diameter {diameter}; supported: {SUPPORTED_DIAMETERS}")
    return diameter


def validate_job_configuration(
    configuration: Mapping[str, Any], *, kind: str, diameter_um: float | None = None,
) -> dict[str, Any]:
    """Validate editable scientific data and return an independent JSON copy.

    Sampling and physical assumptions are editable. Executable/runtime paths and
    scheduler controls belong to trusted deployment profiles and are never accepted.
    """
    if kind not in JOB_KINDS:
        raise ValueError("Unsupported job kind")
    if not isinstance(configuration, Mapping):
        raise ValueError("configuration must be an object")
    _json_tree(configuration)
    if len(json.dumps(configuration, allow_nan=False).encode()) > 1_048_576:
        raise ValueError("configuration exceeds 1 MiB")
    config = copy.deepcopy(dict(configuration))
    profile = config.get("scientific_profile")
    canonical = profile == CANONICAL_PROFILE
    if profile is not None and not canonical:
        raise ValueError("Unknown scientific profile")
    common = {"scientific_profile", "experiment", "emb_diameters", "use_surrogate", "surrogate", "description", "seed", "parameterization", "emb_parameterization"}
    outputs = {"interval_probability", "max_predictive_samples", "resonance", "include_observation_noise", "preprocessing"}
    training_fields = {"epochs", "batch_size", "learning_rate", "hidden_dims", "validation_fraction", "disp_source", "rupture_ratio_threshold", "validation_max_relative_rmse"}
    if kind == "training" and set(config) - common - outputs - training_fields:
        raise ValueError("Training configuration contains settings that do not apply to retraining")
    if kind == "prediction" and set(config) - common - outputs - {"parameters", "fixed_params"}:
        raise ValueError("Prediction configuration contains settings that do not apply to selected-parameter prediction")
    if kind == "calibration" and (set(config) & (training_fields | {"parameters"})):
        raise ValueError("Calibration configuration contains training or selected-prediction settings")
    canonical_controls = {"phase1_contract_mode", "calibration_data", "indentation_prior_sigma", "acoustic_prior_sigma",
                          "indentation_prior_ka_by_diameter_um", "indentation_prior_kb_by_diameter_um", "acoustic_prior_ka_by_diameter_um"}
    if not canonical and set(config) & canonical_controls:
        raise ValueError("Direct-ka and accepted acoustic controls require the canonical scientific profile")
    if canonical:
        if config.get("fixed_params"):
            raise ValueError("The canonical direct-ka contract fixes higher-order physical terms internally")
        if kind == "calibration":
            if config.get("phase1_contract_mode", "emb_direct_ka_kb") != "emb_direct_ka_kb":
                raise ValueError("Canonical calibration requires emb_direct_ka_kb")
            if config.get("calibration_data") not in {"curve_only", "accepted_joint"}:
                raise ValueError("Explicitly select curve_only or accepted_joint calibration data")
            if any(key.startswith("prior_") and key not in {"prior_ka", "prior_kb", "prior_d0", "prior_sigma"} for key in config) or any(key.startswith("hyperprior_") and key not in {"hyperprior_mu_ka", "hyperprior_sigma_ka", "hyperprior_mu_kb", "hyperprior_sigma_kb"} for key in config):
                raise ValueError("Canonical hierarchy infers ka/kb and retains d0/sigma as local nuisance parameters")
            if config.get("calibration_data") == "accepted_joint" and config.get("calibration_mode", "individual") != "population":
                raise ValueError("Joint acoustic reference fitting requires population calibration")
            if config.get("calibration_data") == "curve_only" and set(config) & {"acoustic_prior_sigma", "acoustic_prior_ka_by_diameter_um"}:
                raise ValueError("Curve-only calibration cannot edit unused acoustic likelihood controls")
            if "seed" in config and (isinstance(config["seed"], bool) or not isinstance(config["seed"], int) or config["seed"] <= 0):
                raise ValueError("Canonical inference seeds must be positive integers")
        for key in ("indentation_prior_sigma", "acoustic_prior_sigma"):
            if key in config:
                bounds = config[key]
                if not isinstance(bounds, list) or len(bounds) != 2 or not 0 <= _number(bounds[0], key) < _number(bounds[1], key):
                    raise ValueError("Experiment noise bounds must be nonnegative and ordered")
        for key in ("indentation_prior_ka_by_diameter_um", "indentation_prior_kb_by_diameter_um", "acoustic_prior_ka_by_diameter_um"):
            if key not in config:
                continue
            bounds_by_diameter = config[key]
            allowed = {3.2, 3.4, 5.8} if key.startswith("indentation") else {2.6, 3.2, 4.0}
            if not isinstance(bounds_by_diameter, dict) or not bounds_by_diameter:
                raise ValueError("Experiment prior overrides must map supported diameters to bounds")
            seen = set()
            for label, bounds in bounds_by_diameter.items():
                try:
                    diameter = float(label)
                except (TypeError, ValueError):
                    raise ValueError("Invalid experiment prior diameter") from None
                if diameter not in allowed or diameter in seen or not isinstance(bounds, list) or len(bounds) != 2 or not 0 < _number(bounds[0], key) < _number(bounds[1], key):
                    raise ValueError("Experiment priors require unique supported diameters and positive ordered bounds")
                seen.add(diameter)
    for key, value in config.items():
        if key not in SCIENTIFIC_KEYS:
            if key.startswith("prior_"):
                if key[6:] not in PARAMETERS:
                    raise ValueError(f"Unknown parameter prior {key}")
            elif key.startswith("hyperprior_"):
                if not re.fullmatch(r"hyperprior_(mu|sigma)_(Yt|ka|kb|b1|b2|a3|a4|d0|sigma)", key):
                    raise ValueError(f"Unknown hyperprior {key}")
            else:
                raise ValueError(f"Unsupported scientific configuration field: {key}")
        if key.startswith(("prior_", "hyperprior_")):
            if not isinstance(value, list) or len(value) != 2:
                raise ValueError(f"{key} must have two bounds")
            low, high = (_number(v, key) for v in value)
            if low >= high:
                raise ValueError(f"{key} lower bound must be smaller than upper bound")
            if canonical and ((key in {"prior_ka", "prior_kb", "hyperprior_mu_ka", "hyperprior_mu_kb"} and low <= 0)
                              or (key.startswith("hyperprior_sigma_") and low < 0)):
                raise ValueError("Canonical stiffness/bending bounds must be positive and spread bounds nonnegative")
            if key == "prior_sigma" and low < 0:
                raise ValueError("prior_sigma cannot include negative measurement noise")
        if key in INTEGER_KEYS:
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise ValueError(f"{key} must be a non-negative integer")
            if key in {"pop_size", "hbi_pop_size", "phase3b_pop_size", "epochs", "batch_size", "max_predictive_samples"} and value < 1:
                raise ValueError(f"{key} must be positive")
        if key.endswith("max_gen"):
            if isinstance(value, bool) or not isinstance(value, int) or value < -1:
                raise ValueError(f"{key} must be -1 or a non-negative integer")
        if key in {"target_cov", "hbi_target_cov", "phase3b_target_cov", "covariance_scaling", "hbi_covariance_scaling", "phase3b_covariance_scaling", "validation_max_relative_rmse"}:
            if _number(value, key) <= 0:
                raise ValueError(f"{key} must be positive")
    if config.get("experiment", "indentation") != "indentation":
        raise ValueError("Only indentation is supported")
    if config.get("use_surrogate", True) is not True:
        raise ValueError("This service requires the existing surrogate; simulation execution is unavailable")
    surrogate = config.get("surrogate", {"backend": "dnn"})
    if not isinstance(surrogate, dict) or surrogate.get("backend", "dnn") != "dnn":
        raise ValueError("Only the indentation DNN backend is supported")
    if set(surrogate) - {"backend", "predictive_mc_samples", "predictive_mc_chunk_size"}:
        raise ValueError("Unsupported surrogate settings")
    for key in ("predictive_mc_samples", "predictive_mc_chunk_size"):
        if key in surrogate and (type(surrogate[key]) is not int or surrogate[key] < 1):
            raise ValueError(f"surrogate.{key} must be a positive integer")
    if diameter_um is not None:
        nominal_diameter(diameter_um)
    diameters = config.get("emb_diameters", [diameter_um] if diameter_um is not None else [])
    if not isinstance(diameters, list):
        raise ValueError("emb_diameters must be a list of unique supported diameters")
    for diameter in diameters:
        nominal_diameter(diameter)
    if len(set(diameters)) != len(diameters):
        raise ValueError("emb_diameters must be unique")
    if diameter_um is not None and diameters and diameter_um not in diameters:
        raise ValueError("diameter_um must be included in emb_diameters")
    fixed = config.get("fixed_params", {})
    if not isinstance(fixed, dict) or set(fixed) - PARAMETERS:
        raise ValueError("fixed_params contains unknown material parameters")
    for name, value in fixed.items():
        _number(value, f"fixed_params.{name}")
    if kind == "calibration" and set(fixed) not in (set(), {"b1", "b2", "a3", "a4"}):
        raise ValueError("Existing calibration supports either all material parameters or fixed b1, b2, a3 and a4 together")
    if config.get("calibration_mode", "individual") not in {"individual", "population"}:
        raise ValueError("calibration_mode must be individual or population")
    if config.get("phase2_backend", "native-cuda") not in {"native-cuda", "cpu-mpi"}:
        raise ValueError("Unsupported phase2_backend")
    if not canonical and kind == "calibration" and ("prior_ka" in config or "ka" in fixed):
        raise ValueError("Existing indentation calibration uses Yt/kb; ka is derived using pinned physical assumptions")
    if any(config.get(key, "legacy_yt_kb") != "legacy_yt_kb" for key in ("parameterization", "emb_parameterization")):
        raise ValueError("Only the existing legacy_yt_kb indentation parameterization is supported")
    if config.get("preprocessing", "none") not in {"none", "legacy_morris"}:
        raise ValueError("preprocessing must be none or legacy_morris")
    if kind != "calibration" and config.get("preprocessing", "none") != "none":
        raise ValueError("Legacy Morris preprocessing applies only to calibration")
    if "interval_probability" in config and not 0 < _number(config["interval_probability"], "interval_probability") < 1:
        raise ValueError("interval_probability must lie strictly between zero and one")
    if "validation_fraction" in config and not 0 < _number(config["validation_fraction"], "validation_fraction") < 1:
        raise ValueError("validation_fraction must lie strictly between zero and one")
    if "learning_rate" in config and _number(config["learning_rate"], "learning_rate") <= 0:
        raise ValueError("learning_rate must be positive")
    if "hidden_dims" in config:
        dims = config["hidden_dims"]
        if not isinstance(dims, list) or not dims or len(dims) > 8 or any(isinstance(x, bool) or not isinstance(x, int) or not 1 <= x <= 2048 for x in dims):
            raise ValueError("hidden_dims must contain one to eight positive widths, each at most 2048")
    if (kind == "training" or "disp_source" in config) and config.get("disp_source") not in {"displacement", "diameter"}:
        raise ValueError("Training displacement interpretation must be explicit")
    parameters = config.get("parameters")
    if parameters is not None:
        if not isinstance(parameters, dict) or set(parameters) - PARAMETERS:
            raise ValueError("parameters must map supported parameter names to values")
        for name, value in parameters.items():
            _number(value, f"parameters.{name}")
    if kind == "prediction":
        selected_parameters = {**fixed, **(parameters or {})}
        if canonical:
            if not {"ka", "kb"}.issubset(selected_parameters) or set(selected_parameters) - {"ka", "kb", "d0", "sigma"}:
                raise ValueError("Canonical prediction requires direct ka/kb with optional d0/sigma")
            if selected_parameters["ka"] <= 0 or selected_parameters["kb"] <= 0 or selected_parameters.get("sigma", 0) < 0:
                raise ValueError("Canonical material parameters must be positive and noise nonnegative")
        elif "ka" in selected_parameters or not {"Yt", "kb"}.issubset(selected_parameters):
            raise ValueError("Prediction requires explicit Yt and kb; ka is derived")
    if "forces" in config:
        if not isinstance(config["forces"], list) or not config["forces"]:
            raise ValueError("forces must be a nonempty array")
        for force in config["forces"]:
            if _number(force, "forces") < 0:
                raise ValueError("forces cannot contain negative forces")
    resonance = config.get("resonance", {})
    if not isinstance(resonance, dict):
        raise ValueError("resonance must be an object")
    allowed_resonance = {"unit_defaults_modality", "evaluator", "agent"} if canonical else {"unit_defaults_modality", "evaluator", "dpd_constants", "radius_dpd_by_diameter_um"}
    if set(resonance) - allowed_resonance:
        raise ValueError("Unsupported acoustic propagation setting")
    if resonance.get("unit_defaults_modality", "indentation") != "indentation":
        raise ValueError("Acoustic propagation uses the pinned indentation unit defaults")
    evaluator = resonance.get("evaluator", {})
    expected_evaluator = "artifact_polynomial_bank" if canonical else "analytical_dpd_formula"
    if (not isinstance(evaluator, dict) or set(evaluator) - {"mode"}
            or evaluator.get("mode", expected_evaluator) != expected_evaluator):
        raise ValueError(f"This profile requires the {expected_evaluator} acoustic evaluator")
    if canonical and resonance.get("agent", "sonovue") != "sonovue":
        raise ValueError("Canonical acoustic profile is SonoVue")
    constants = resonance.get("dpd_constants", {})
    gas_fields = {"c_gas_dpd", "rho_gas_dpd", "p_gas_dpd"}
    if not isinstance(constants, dict) or set(constants) - ({"rho_l_dpd", "p0_dpd", "kappa_g"} | gas_fields):
        raise ValueError("Unsupported acoustic DPD constant; use canonical names")
    for name, value in constants.items():
        if _number(value, f"resonance.dpd_constants.{name}") <= 0:
            raise ValueError("Acoustic DPD constants must be positive")
    gas_selected = set(constants) & gas_fields
    if gas_selected and (gas_selected != gas_fields or "kappa_g" in constants):
        raise ValueError("Select either kappa_g or all three acoustic gas constants")
    radii = resonance.get("radius_dpd_by_diameter_um", {})
    if not isinstance(radii, dict):
        raise ValueError("Acoustic radii must map supported nominal diameters to positive DPD radii")
    seen_diameters = set()
    for key, radius in radii.items():
        try:
            diameter = nominal_diameter(float(key))
        except (TypeError, ValueError):
            raise ValueError("Acoustic radius uses an unsupported nominal diameter") from None
        if diameter in seen_diameters or _number(radius, "acoustic radius") <= 0:
            raise ValueError("Acoustic radii require unique nominal diameters and positive values")
        seen_diameters.add(diameter)
    if "include_observation_noise" in config and type(config["include_observation_noise"]) is not bool:
        raise ValueError("include_observation_noise must be a JSON boolean")
    config.setdefault("experiment", "indentation")
    config.setdefault("use_surrogate", True)
    if not canonical:
        config.setdefault("surrogate", {"backend": "dnn"})
    return config


def validate_job_artifacts(input_artifacts, *, kind: str, configuration, diameter_um: float) -> None:
    """Reject mixed input types and incomplete geometry selections before execution."""
    nominal_diameter(diameter_um)
    if not isinstance(input_artifacts, list) or not input_artifacts:
        raise ValueError("At least one verified input artifact is required")
    if any(not isinstance(a, Mapping) or not isinstance(a.get("id"), str) for a in input_artifacts) or len({a["id"] for a in input_artifacts}) != len(input_artifacts):
        raise ValueError("Input artifact identities must be unique strings")
    if kind == "training":
        if len(input_artifacts) != 1 or input_artifacts[0].get("kind") != "training_table":
            raise ValueError("Training requires exactly one training_table and no other input kinds")
        metadata = input_artifacts[0].get("metadata", {})
        if metadata.get("table_schema") != "mesouq_indentation_wide_dpd_v1":
            raise ValueError("Training requires the explicit mesouq_indentation_wide_dpd_v1 table schema")
        if nominal_diameter(metadata.get("nominal_diameter_um")) != diameter_um:
            raise ValueError("Training table nominal diameter differs from the selected model")
        if configuration.get("emb_diameters", [diameter_um]) != [diameter_um]:
            raise ValueError("Training supports one selected model diameter")
        return
    if kind not in {"calibration", "prediction"} or any(a.get("kind") != "curve" for a in input_artifacts):
        raise ValueError("Calibration and prediction accept only curve artifacts")
    if kind == "calibration":
        population = configuration.get("calibration_mode", "individual") == "population"
        joint = configuration.get("scientific_profile") == CANONICAL_PROFILE and configuration.get("calibration_data") == "accepted_joint"
        if (not population and len(input_artifacts) != 1) or (population and not joint and len(input_artifacts) < 2):
            raise ValueError("Individual calibration requires one curve; population calibration requires at least two")
    actual = set()
    for artifact in input_artifacts:
        metadata = artifact.get("metadata", {})
        actual.add(nominal_diameter(metadata.get("nominal_diameter_um")))
        if metadata.get("force_unit") not in {"N", "nN", "uN", "µN"} or metadata.get("displacement_unit") not in {"m", "nm", "um", "µm"}:
            raise ValueError("Curve requires supported explicit physical units")
        if metadata.get("column_order") not in {"force-displacement", "displacement-force"}:
            raise ValueError("Curve requires explicit column_order")
    selected = configuration.get("emb_diameters", [diameter_um])
    if actual != set(selected):
        raise ValueError("Input curve diameters must exactly cover selected emb_diameters")


def _interval(row: Mapping[str, Any], keys: tuple[str, str, str], label: str) -> None:
    low, middle, high = (_number(row.get(key), f"{label}.{key}") for key in keys)
    if not low <= middle <= high:
        raise ValueError(f"{label} has unordered interval bounds")


def validate_result(result: Mapping[str, Any]) -> dict[str, Any]:
    """Reject incomplete, non-finite and internally inconsistent scientific output."""
    if not isinstance(result, Mapping) or result.get("schema_version") != 1:
        raise ValueError("Result schema_version must be 1")
    if result.get("kind") not in JOB_KINDS:
        raise ValueError("Result kind is unsupported")
    # Serialization also rejects runtime objects and NaNs nested in diagnostics.
    try:
        serialized = json.dumps(result, allow_nan=False)
    except (TypeError, ValueError) as exc:
        raise ValueError("Result must contain finite JSON data") from exc
    provenance = result.get("provenance")
    if not isinstance(provenance, dict) or not provenance:
        raise ValueError("Result requires provenance")
    for key in ("job_id", "model_version"):
        if not isinstance(provenance.get(key), str) or not provenance[key]:
            raise ValueError(f"Result provenance requires {key}")
    for key in ("code_sha256", "config_sha256"):
        if not isinstance(provenance.get(key), str) or not re.fullmatch(r"[a-f0-9]{64}", provenance[key]):
            raise ValueError(f"Result provenance requires a SHA256 {key}")
    hashes = provenance.get("model_sha256")
    if isinstance(hashes, str):
        hashes = {"model": hashes}
    if not isinstance(hashes, dict) or not hashes or any(not isinstance(v, str) or not re.fullmatch(r"[a-f0-9]{64}", v) for v in hashes.values()):
        raise ValueError("Result provenance requires immutable model_sha256")
    data_hashes = provenance.get("data_sha256")
    if not isinstance(data_hashes, dict) or any(not isinstance(k, str) or not isinstance(v, str) or not re.fullmatch(r"[a-f0-9]{64}", v) for k, v in data_hashes.items()):
        raise ValueError("Result provenance requires data_sha256 mapping")
    if not isinstance(provenance.get("runtime"), dict) or not isinstance(provenance["runtime"].get("python"), str):
        raise ValueError("Result provenance requires Python runtime identity")
    probability = _number(result.get("interval_probability"), "interval_probability")
    if not 0 < probability < 1:
        raise ValueError("Result interval_probability must lie between zero and one")
    parameters, curves = result.get("parameters"), result.get("curves")
    if not isinstance(parameters, list) or not isinstance(curves, list):
        raise ValueError("Result parameters and curves must be arrays")
    names: set[str] = set()
    for row in parameters:
        if not isinstance(row, dict) or not isinstance(row.get("name"), str) or row["name"] in names:
            raise ValueError("Result parameter names must be unique strings")
        names.add(row["name"])
        if not isinstance(row.get("unit"), str) or not row["unit"]:
            raise ValueError("Result parameters require explicit units")
        _interval(row, ("lower", "median", "upper"), "parameter")
    dataset_ids: set[str] = set()
    for row in curves:
        if not isinstance(row, dict) or not isinstance(row.get("dataset_id"), str):
            raise ValueError("Result curve needs dataset_id")
        if row["dataset_id"] in dataset_ids:
            raise ValueError("Result dataset identities must be unique")
        dataset_ids.add(row["dataset_id"])
        keys = ("force", "observed_displacement", "median", "lower", "upper")
        arrays = [row.get(key) for key in keys]
        if any(not isinstance(a, list) for a in arrays) or not arrays[0]:
            raise ValueError("Result curve arrays must be nonempty")
        if len({len(a) for a in arrays}) != 1:
            raise ValueError("Result curve arrays have different lengths")
        for i in range(len(arrays[0])):
            for key in keys:
                _number(row[key][i], f"curve.{key}")
            if not row["lower"][i] <= row["median"][i] <= row["upper"][i]:
                raise ValueError("Result curve has unordered prediction interval")
        if not row.get("force_unit") or not row.get("displacement_unit"):
            raise ValueError("Result curve requires units")
    resonance = result.get("resonance")
    if not isinstance(resonance, dict) or resonance.get("status") not in {"available", "unavailable"}:
        raise ValueError("Result requires resonance status")
    if resonance["status"] == "available":
        rows = resonance.get("by_dataset", [resonance])
        if not isinstance(rows, list) or not rows:
            raise ValueError("Available resonance requires predictions")
        available = 0
        for row in rows:
            if row.get("status", "available") == "unavailable":
                if not row.get("reason"):
                    raise ValueError("Unavailable dataset resonance requires a reason")
                continue
            _interval(row, ("lower_mhz", "median_mhz", "upper_mhz"), "resonance")
            available += 1
            if row["lower_mhz"] <= 0:
                raise ValueError("Resonance frequencies must be positive")
        if not available or not resonance.get("assumptions"):
            raise ValueError("Available resonance requires positive frequencies and assumptions")
    elif not resonance.get("reason"):
        raise ValueError("Unavailable resonance requires a reason")
    if not isinstance(result.get("diagnostics"), dict):
        raise ValueError("Result requires diagnostics")
    if result["kind"] == "calibration" and (not parameters or not curves):
        raise ValueError("Calibration result requires parameters and fitted curves")
    if result["kind"] == "prediction" and not curves:
        raise ValueError("Prediction result requires curves")
    if result["kind"] == "training" and not isinstance(result.get("training"), dict):
        raise ValueError("Training result requires training metrics/artifacts")
    return json.loads(serialized)
