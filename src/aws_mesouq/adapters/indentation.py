"""Standalone, snapshotted adapter to an operator-installed MesoUQ runtime.

The connector copies this one file beside the private job manifest. It imports no
AWS-MesoUQ package on the cluster. Scientific calculations remain in MesoUQ;
this adapter enforces the release preset and adds portable downloads and figures.
"""
from __future__ import annotations

import argparse
import copy
import csv
import hashlib
import json
import math
import os
from pathlib import Path
import re
import stat
import subprocess
import sys
import tempfile


PRESET_CONFIGURATION = {'calibration_data': 'curve_only',
 'calibration_mode': 'individual',
 'covariance_scaling': 0.04,
 'emb_diameters': [3.2],
 'experiment': 'indentation',
 'frac_diam': 0.2,
 'hbi_burn_in': 1,
 'hbi_covariance_scaling': 0.04,
 'hbi_pop_size': 50000,
 'hbi_target_cov': 0.6,
 'hyperprior_mu_ka': [8500, 11000],
 'hyperprior_mu_kb': [14000, 24000],
 'hyperprior_sigma_ka': [300, 1000],
 'hyperprior_sigma_kb': [4000, 10000],
 'indentation_prior_sigma': [0.045, 0.11],
 'interval_probability': 0.9,
 'max_gen': -1,
 'phase1_burn_in': 1,
 'phase1_contract_mode': 'emb_direct_ka_kb',
 'phase3b_covariance_scaling': 0.015,
 'phase3b_max_gen': -1,
 'phase3b_pop_size': 50000,
 'phase3b_target_cov': 0.8,
 'pop_size': 50000,
 'prior_d0': [0, 0.5],
 'prior_ka': [617.0581351146693, 42000],
 'prior_kb': [100, 48000],
 'prior_sigma': [0.005, 0.3],
 'resonance': {'agent': 'sonovue',
               'evaluator': {'mode': 'artifact_polynomial_bank'},
               'unit_defaults_modality': 'indentation'},
 'scientific_profile': 'uq_emb_sonovue_202607',
 'surrogate': {'backend': 'dnn', 'predictive_mc_chunk_size': 8, 'predictive_mc_samples': 32},
 'target_cov': 0.8,
 'use_surrogate': True}
PRESET_ID = "indentation-sonovue-v1"
DIAMETERS = (3.2, 3.4, 5.8)


def _json_bytes(value):
    return (json.dumps(value, sort_keys=True, allow_nan=False, indent=2) + "\n").encode()


def _read(path: Path) -> bytes:
    with os.fdopen(os.open(path, os.O_RDONLY | os.O_NOFOLLOW), "rb") as stream:
        if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
            raise ValueError("Expected a regular scientific output file")
        return stream.read()


def _relative(root: Path, name: str) -> Path:
    if not isinstance(name, str) or not name or any(
        not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]*", p) or p in {".", ".."}
        for p in name.split("/")
    ):
        raise ValueError("Unsafe scientific output filename")
    result = root / name
    for p in (result, *result.parents):
        if p.is_symlink():
            raise ValueError("Scientific output paths must not contain symlinks")
        if p == root:
            break
    return result


def validate_manifest(manifest: dict) -> None:
    job = manifest.get("job", {})
    config = job.get("configuration", {})
    diameters = config.get("emb_diameters", [])
    if job.get("kind") != "calibration" or len(manifest.get("inputs", [])) != 1:
        raise ValueError("Release 1 requires one-curve calibration")
    if len(diameters) != 1 or type(diameters[0]) not in {float, int} or diameters[0] not in DIAMETERS:
        raise ValueError("Unsupported nominal indentation diameter")
    expected = copy.deepcopy(PRESET_CONFIGURATION)
    expected["emb_diameters"] = list(diameters)
    if _json_bytes(config) != _json_bytes(expected):
        raise ValueError("Scientific configuration differs from the locked release preset")
    if manifest.get("site") not in {"karolina", "vega"}:
        raise ValueError("An explicit supported HPC site is required")
    expected_hash = manifest.get("runtime", {}).get("adapter_sha256")
    if expected_hash != hashlib.sha256(_read(Path(__file__))).hexdigest():
        raise ValueError("Adapter snapshot does not match the trusted manifest hash")


def _write_new(path: Path, blob: bytes) -> None:
    path.parent.mkdir(mode=0o700, exist_ok=True)
    with os.fdopen(os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600), "wb") as stream:
        stream.write(blob)
        stream.flush()
        os.fsync(stream.fileno())


def _replace_json(path: Path, value) -> None:
    if not stat.S_ISREG(path.lstat().st_mode) or path.is_symlink():
        raise ValueError("Refusing to replace a nonregular result file")
    fd, temporary = tempfile.mkstemp(prefix=".adapter-", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(_json_bytes(value))
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def _csv(path: Path, fields, rows) -> None:
    import io
    stream = io.StringIO(newline="")
    writer = csv.DictWriter(stream, fieldnames=fields, extrasaction="ignore")
    writer.writeheader()
    writer.writerows(rows)
    _write_new(path, stream.getvalue().encode())


def _portable(value) -> None:
    if isinstance(value, dict):
        for key, item in value.items():
            _portable(key)
            _portable(item)
    elif isinstance(value, (list, tuple)):
        for item in value:
            _portable(item)
    elif isinstance(value, str) and (
        value.startswith(("/", "~/", "file://"))
        or re.search(r"(?:^|[\s=\"'(])/(?:home|scratch|mnt|tmp|private|var|etc|run|root|data|Users)/", value)
        or re.search(r"(?:[A-Za-z]:\\|\\\\[A-Za-z0-9_.-]+\\)", value)
    ):
        raise ValueError("Portable scientific outputs must not contain private filesystem paths")


def add_downloads(output: Path, result: dict, configuration: dict) -> dict:
    """Render actual posterior draws and returned intervals; never invent samples."""
    _json_bytes(result)  # Reject nonfinite values before creating any exports.
    _portable(result)
    _portable(configuration)
    runtime = result["provenance"]["runtime"]
    # MesoUQ records sys.version, including compiler text and newlines. The
    # publication contract exposes the Python version, not that build banner.
    runtime = {key: value for key, value in runtime.items()
               if not (key.endswith("_sha256") and value is None)}
    if "python" in runtime:
        version = runtime["python"]
        if not isinstance(version, str) or not re.fullmatch(r"[0-9]+\.[0-9]+\.[0-9]+[A-Za-z0-9_.+-]*", version.split()[0] if version.split() else ""):
            raise ValueError("Scientific Python version is invalid")
        runtime["python"] = version.split()[0]
    result["provenance"]["runtime"] = runtime
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    output = output.resolve()
    names = result.get("publication_files", [])
    if len(names) != len(set(names)) or not {"result.json", "provenance.json"}.issubset(names):
        raise ValueError("Scientific result requires a unique explicit publication list")
    for name in names:
        _relative(output, name)
    # The scientific engine snapshots its trusted models locally. Model uploads
    # and model release are outside the first AWS-MesoUQ workflow.
    names = [name for name in names if not name.startswith("models/")
             and not name.endswith((".pkl", ".pt", ".onnx", ".safetensors"))]
    allowed = {"result.json", "provenance.json", "scientific_configuration.json",
               "scientific_inputs.json", "derived_configuration.json", "tempering.json"}
    for name in names:
        posterior = re.fullmatch(r"posterior_[A-Za-z0-9_.-]+\.csv", name)
        if name not in allowed and not posterior:
            raise ValueError("Unexpected scientific publication file")
        blob = _read(_relative(output, name))
        if not blob:
            raise ValueError("Scientific publication files must not be empty")
        if name.endswith(".json"):
            _portable(json.loads(blob))
    posteriors = [name for name in names if re.fullmatch(r"posterior_[A-Za-z0-9_.-]+\.csv", name)]
    if len(posteriors) != 1 or len(result.get("curves", [])) != 1:
        raise ValueError("One portable posterior and fitted curve are required")
    probability = float(result["interval_probability"])
    if not 0 < probability < 1:
        raise ValueError("Invalid credible interval probability")
    rows = list(csv.DictReader(_read(_relative(output, posteriors[0])).decode().splitlines()))
    if not rows or not {"ka", "kb", "d0"}.issubset(rows[0]):
        raise ValueError("Posterior samples require ka, kb and d0 columns")
    draws = {key: [float(row[key]) for row in rows] for key in ("ka", "kb", "d0")}
    if any(not math.isfinite(value) for values in draws.values() for value in values):
        raise ValueError("Posterior samples must be finite")
    parameters = result["parameters"]
    summaries = {row["name"]: row for row in parameters}
    if not set(draws).issubset(summaries):
        raise ValueError("Scientific parameter summaries are incomplete")
    generated = []

    def figure(fig, stem):
        for extension in ("png", "pdf"):
            name = f"figures/{stem}.{extension}"
            path = _relative(output, name)
            path.parent.mkdir(mode=0o700, exist_ok=True)
            if path.exists():
                raise ValueError("Figure output already exists")
            fig.savefig(path, dpi=180, bbox_inches="tight")
            generated.append(name)
        plt.close(fig)

    fig, axes = plt.subplots(1, 3, figsize=(10, 3))
    for ax, key, label in zip(axes, draws, ("kA", "kB", "d0")):
        ax.hist(draws[key], bins=min(50, max(5, int(math.sqrt(len(rows))))), density=True, color="#2563eb", alpha=.7)
        summary = summaries[key]
        ax.axvline(summary["median"], color="black", label="Median")
        ax.axvspan(summary["lower"], summary["upper"], color="#64748b", alpha=.15,
                   label=f"{probability:.0%} credible interval")
        ax.set_xlabel(f"{label} ({summary['unit']})")
        ax.set_ylabel("Posterior density")
    axes[0].legend(fontsize=7)
    figure(fig, "posterior")

    _csv(output / "parameter_intervals.csv", ["name", "unit", "lower", "median", "upper", "interval_probability"],
         [{**row, "interval_probability": probability} for row in parameters])
    generated.append("parameter_intervals.csv")
    curve = result["curves"][0]
    fields = ("force", "observed_displacement", "median", "lower", "upper")
    if not curve["force"] or len({len(curve[key]) for key in fields}) != 1:
        raise ValueError("Fitted curve arrays must have equal nonzero lengths")
    curve_rows = [{**{key: curve[key][index] for key in fields},
                   "force_unit": curve["force_unit"], "displacement_unit": curve["displacement_unit"],
                   "interval_probability": probability} for index in range(len(curve["force"]))]
    _csv(output / "fitted_curve.csv", [*fields, "force_unit", "displacement_unit", "interval_probability"], curve_rows)
    generated.append("fitted_curve.csv")
    ordered = sorted(curve_rows, key=lambda row: row["force"])
    fig, ax = plt.subplots(figsize=(6, 4))
    force = [r["force"] for r in ordered]
    ax.fill_between(force, [r["lower"] for r in ordered], [r["upper"] for r in ordered], alpha=.2,
                    label=f"{probability:.0%} predictive interval")
    ax.plot(force, [r["median"] for r in ordered], label="Predicted median")
    ax.scatter(curve["force"], curve["observed_displacement"], s=15, color="black", label="Observed")
    ax.set(xlabel=f"Force ({curve['force_unit']})", ylabel=f"Displacement ({curve['displacement_unit']})")
    ax.legend(fontsize=8)
    figure(fig, "fitted_curve")

    resonance = result["resonance"]
    frequency_rows = resonance.get("by_dataset", [resonance])
    assumptions = resonance.get("assumptions", [])
    _csv(output / "resonance.csv", ["dataset_id", "status", "lower_mhz", "median_mhz", "upper_mhz", "conditional_fixed_kb_dpd", "kb_uncertainty_propagated", "interval_probability", "reason", "assumptions"],
         [{**row, "status": row.get("status", resonance["status"]),
           "interval_probability": probability,
           "assumptions": " | ".join(row.get("assumptions", assumptions))} for row in frequency_rows])
    generated.append("resonance.csv")
    fig, ax = plt.subplots(figsize=(7, 3))
    available = [row for row in frequency_rows if row.get("status", resonance["status"]) == "available"]
    if available:
        for index, row in enumerate(available):
            median = row["median_mhz"]
            ax.errorbar(median, index, xerr=[[median-row["lower_mhz"]], [row["upper_mhz"]-median]], fmt="o", capsize=4)
        ax.set_yticks(range(len(available)), [row.get("dataset_id", "Curve") for row in available])
        ax.set_xlabel("Resonance frequency (MHz)")
    else:
        reasons = [str(row["reason"]) for row in frequency_rows if row.get("reason")]
        reason = resonance.get("reason") or "; ".join(reasons) or "Unsupported inputs"
        ax.text(.5, .5, "Resonance unavailable\n" + reason,
                transform=ax.transAxes, ha="center", va="center", wrap=True)
        ax.set_axis_off()
    ax.set_title(f"{probability:.0%} conditional interval: fixed kB; kA uncertainty only", fontsize=10)
    fig.text(.5, -.03, "Approximation error, model discrepancy and diameter uncertainty are unquantified.",
             ha="center", fontsize=8)
    figure(fig, "resonance")
    provenance = result["provenance"]
    provenance["runtime"]["adapter_sha256"] = hashlib.sha256(_read(Path(__file__))).hexdigest()
    _write_new(output / "settings_and_versions.json", _json_bytes({"preset_id": PRESET_ID,
        "configuration": configuration, "provenance": provenance}))
    generated.append("settings_and_versions.json")
    result["publication_files"] = names + generated
    _replace_json(output / "provenance.json", provenance)
    _replace_json(output / "result.json", result)
    return result


def scientific_environment(manifest: dict, repository: Path) -> dict:
    environment = dict(os.environ)
    paths = [str(repository / "src"), str(repository)]
    runtime = manifest.get("runtime", {})
    if runtime.get("site_runtime_root") and runtime.get("environment_script"):
        script = Path(runtime["environment_script"])
        if hashlib.sha256(_read(script)).hexdigest() != runtime.get("environment_sha256"):
            raise ValueError("Scientific environment script differs from its trusted pin")
        root = Path(runtime["site_runtime_root"])
        if not root.is_absolute() or not root.is_dir() or root.resolve() != root:
            raise ValueError("Scientific runtime root must be an existing canonical directory")
        # The verified environment script supplies native Korali and companion
        # packages. Retain only paths inside its operator-selected runtime.
        for value in environment.get("PYTHONPATH", "").split(os.pathsep):
            candidate = Path(value)
            if value and candidate.is_absolute() and candidate.is_dir():
                candidate = candidate.resolve()
                if root == candidate or root in candidate.parents:
                    if str(candidate) not in paths:
                        paths.append(str(candidate))
    environment["PYTHONPATH"] = os.pathsep.join(paths)
    environment["PYTHONNOUSERSITE"] = "1"
    return environment


def run(manifest_path: Path, output: Path) -> dict:
    manifest = json.loads(_read(manifest_path))
    validate_manifest(manifest)
    repository = Path(manifest["repository_root"])
    if not repository.is_absolute() or not repository.is_dir():
        raise ValueError("An operator-installed MesoUQ checkout is required")
    environment = scientific_environment(manifest, repository)
    subprocess.run([sys.executable, "-m", "meso_uq.publishing.runner", "--manifest", str(manifest_path),
                    "--output-dir", str(output)], cwd=repository, env=environment, check=True)
    result = json.loads(_read(output / "result.json"))
    return add_downloads(output, result, manifest["job"]["configuration"])


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", required=True, type=Path)
    parser.add_argument("--output-dir", required=True, type=Path)
    args = parser.parse_args(argv)
    run(args.manifest, args.output_dir)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
