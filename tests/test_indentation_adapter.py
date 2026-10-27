"""Synthetic adapter tests; no model, HPC account, or scientific runtime required."""
import ast
import copy
import csv
import hashlib
import importlib.util
import io
import json
from pathlib import Path
import shutil
import sys

import pytest

from aws_mesouq.adapters import indentation as adapter
from aws_mesouq.release import configuration, preset


def manifest_for(tmp_path, diameter=3.2, site="karolina"):
    runtime = tmp_path / "external-mesouq"
    runtime.mkdir(exist_ok=True)
    return {
        "job": {"kind": "calibration", "configuration": configuration(diameter)},
        "inputs": [{"path": str(tmp_path / "private-input.csv")}],
        "site": site,
        "repository_root": str(runtime),
        "runtime": {"adapter_sha256": hashlib.sha256(Path(adapter.__file__).read_bytes()).hexdigest()},
    }


@pytest.fixture
def scientific_output(tmp_path):
    output = tmp_path / "output"
    output.mkdir()
    result = {
        "schema_version": 1,
        "kind": "calibration",
        "interval_probability": 0.9,
        "parameters": [
            {"name": "ka", "unit": "DPD stiffness", "lower": 8500, "median": 9500, "upper": 10500},
            {"name": "kb", "unit": "DPD energy", "lower": 17000, "median": 20000, "upper": 23000},
            {"name": "d0", "unit": "DPD length", "lower": 0.05, "median": 0.1, "upper": 0.15},
        ],
        "curves": [{
            "dataset_id": "synthetic-curve", "force": [2.0, 0.0, 1.0],
            "observed_displacement": [0.4, 0.0, 0.2], "median": [0.38, 0.0, 0.19],
            "lower": [0.30, 0.0, 0.15], "upper": [0.46, 0.0, 0.23],
            "force_unit": "nN", "displacement_unit": "nm",
        }],
        "resonance": {
            "status": "available", "dataset_id": "synthetic-curve",
            "lower_mhz": 1.8, "median_mhz": 2.1, "upper_mhz": 2.4,
            "conditional_fixed_kb_dpd": 7850.288865935088,
            "kb_uncertainty_propagated": False,
            "assumptions": ["SYNTHETIC: fixed kb; ka uncertainty only"],
        },
        "diagnostics": {"test_fixture": True},
        "provenance": {"model_sha256": "a" * 64, "runtime": {"site": "karolina", "python": "3.10.12"}},
        "publication_files": ["result.json", "provenance.json", "posterior_synthetic.csv"],
    }
    (output / "posterior_synthetic.csv").write_text(
        "ka,kb,d0\n8500,17000,0.05\n9500,20000,0.10\n10500,23000,0.15\n"
    )
    (output / "result.json").write_text(json.dumps(result))
    (output / "provenance.json").write_text(json.dumps(result["provenance"]))
    return output, result


@pytest.fixture(autouse=True)
def clean_figures():
    yield
    if "matplotlib.pyplot" in sys.modules:
        sys.modules["matplotlib.pyplot"].close("all")


@pytest.mark.parametrize("diameter", [3.2, 3.4, 5.8])
@pytest.mark.parametrize("site", ["karolina", "vega"])
def test_embedded_preset_matches_shared_release_and_accepts_both_sites(tmp_path, diameter, site):
    assert adapter.PRESET_CONFIGURATION == preset()["configuration"]
    adapter.validate_manifest(manifest_for(tmp_path, diameter, site))


@pytest.mark.parametrize("mutation", [
    lambda m: m["job"].update(kind="prediction"),
    lambda m: m["inputs"].append({"path": "second.csv"}),
    lambda m: m["job"]["configuration"].update(emb_diameters=[4.0]),
    lambda m: m["job"]["configuration"].update(emb_diameters=[3.2, 3.4]),
    lambda m: m["job"]["configuration"].update(pop_size=32),
    lambda m: m["job"]["configuration"].update(interval_probability=0.95),
    lambda m: m["job"]["configuration"].update(seed=42),
    lambda m: m.update(site="unknown"),
    lambda m: m["runtime"].update(adapter_sha256="0" * 64),
])
def test_manifest_mutations_fail_closed(tmp_path, mutation):
    manifest = manifest_for(tmp_path)
    mutation(manifest)
    with pytest.raises(ValueError):
        adapter.validate_manifest(manifest)


def test_snapshot_imports_standalone_and_runner_uses_external_checkout(tmp_path, scientific_output, monkeypatch):
    snapshot = tmp_path / "snapshot.py"
    shutil.copyfile(adapter.__file__, snapshot)
    spec = importlib.util.spec_from_file_location("standalone_indentation_snapshot", snapshot)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    tree = ast.parse(snapshot.read_text())
    top_level_imports = [node for node in tree.body if isinstance(node, (ast.Import, ast.ImportFrom))]
    names = [node.module if isinstance(node, ast.ImportFrom) else alias.name
             for node in top_level_imports for alias in (node.names if isinstance(node, ast.Import) else [None])]
    assert not any(name.startswith(("aws_mesouq", "meso_uq", "numpy", "matplotlib")) for name in names)
    manifest = manifest_for(tmp_path)
    manifest["runtime"]["adapter_sha256"] = hashlib.sha256(snapshot.read_bytes()).hexdigest()
    manifest_path = tmp_path / "manifest.json"
    manifest_path.write_text(json.dumps(manifest))
    prepared, _ = scientific_output
    runner_fixture = tmp_path / "synthetic-runtime-payload"
    prepared.rename(runner_fixture)
    output = tmp_path / "output"
    # Initialize font lookup before spying on the scientific subprocess alone.
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot
    calls = []
    monkeypatch.setenv("PYTHONPATH", "/untrusted/inherited-path")
    def external_runner(*args, **kwargs):
        assert not output.exists()
        calls.append((args, kwargs))
        shutil.copytree(runner_fixture, output)
    monkeypatch.setattr(module.subprocess, "run", external_runner)
    module.run(manifest_path, output)
    assert len(calls) == 1
    args, kwargs = calls[0]
    assert args[0] == [sys.executable, "-m", "meso_uq.publishing.runner", "--manifest", str(manifest_path), "--output-dir", str(output)]
    repository = Path(manifest["repository_root"])
    assert kwargs["cwd"] == repository
    assert kwargs["check"] is True
    assert kwargs["env"]["PYTHONPATH"] == f"{repository / 'src'}:{repository}"


def read_csv(path):
    return list(csv.DictReader(io.StringIO(path.read_text())))


def test_native_packages_require_pinned_environment_and_stay_within_runtime(tmp_path, monkeypatch):
    runtime = tmp_path / "approved-runtime"
    native = runtime / "native-korali/site-packages"
    native.mkdir(parents=True)
    outside = tmp_path / "untrusted"
    outside.mkdir()
    escape = runtime / "escape"
    escape.symlink_to(outside, target_is_directory=True)
    script = tmp_path / "runtime-env.sh"
    script.write_text("# synthetic pinned operator environment\n")
    manifest = {"runtime": {"site_runtime_root": str(runtime), "environment_script": str(script),
                            "environment_sha256": hashlib.sha256(script.read_bytes()).hexdigest()}}
    monkeypatch.setenv("PYTHONPATH", f"{native}:{outside}:{escape}:")
    repository = tmp_path / "mesouq"
    environment = adapter.scientific_environment(manifest, repository)
    assert environment["PYTHONPATH"].split(":") == [str(repository / "src"), str(repository), str(native)]
    assert environment["PYTHONNOUSERSITE"] == "1"
    manifest["runtime"]["environment_sha256"] = "0" * 64
    with pytest.raises(ValueError, match="trusted pin"):
        adapter.scientific_environment(manifest, repository)
    manifest["runtime"].pop("environment_script")
    assert adapter.scientific_environment(manifest, repository)["PYTHONPATH"].split(":") == [str(repository / "src"), str(repository)]


def test_actual_synthetic_posterior_generates_portable_numerical_and_png_pdf_downloads(scientific_output, tmp_path, monkeypatch):
    output, result = scientific_output
    original = copy.deepcopy(result)
    import matplotlib.axes
    histogram_inputs = []
    native_histogram = matplotlib.axes.Axes.hist
    def record_histogram(axes, values, *args, **kwargs):
        histogram_inputs.append(list(values))
        return native_histogram(axes, values, *args, **kwargs)
    monkeypatch.setattr(matplotlib.axes.Axes, "hist", record_histogram)
    posterior = (output / "posterior_synthetic.csv").read_bytes()
    final = adapter.add_downloads(output, result, configuration(3.2))
    expected = {"result.json", "provenance.json", "posterior_synthetic.csv", "parameter_intervals.csv", "fitted_curve.csv", "resonance.csv", "settings_and_versions.json"}
    expected.update(f"figures/{stem}.{extension}" for stem in ["posterior", "fitted_curve", "resonance"] for extension in ["png", "pdf"])
    assert histogram_inputs == [[8500.0, 9500.0, 10500.0], [17000.0, 20000.0, 23000.0], [0.05, 0.1, 0.15]]
    assert set(final["publication_files"]) == expected
    assert len(final["publication_files"]) == len(expected)
    assert (output / "posterior_synthetic.csv").read_bytes() == posterior
    assert final["curves"] == original["curves"]
    assert final["parameters"] == original["parameters"]
    assert final["resonance"] == original["resonance"]
    for name in expected:
        path = output / name
        assert path.is_file() and not path.is_symlink()
        if path.suffix == ".png":
            assert path.read_bytes().startswith(b"\x89PNG\r\n\x1a\n")
        elif path.suffix == ".pdf":
            assert path.read_bytes().startswith(b"%PDF-")
        else:
            assert str(tmp_path) not in path.read_text()
    settings = json.loads((output / "settings_and_versions.json").read_text())
    assert settings["preset_id"] == adapter.PRESET_ID
    assert settings["configuration"] == configuration(3.2)
    assert settings["provenance"]["runtime"]["adapter_sha256"] == hashlib.sha256(Path(adapter.__file__).read_bytes()).hexdigest()
    assert json.loads((output / "provenance.json").read_text()) == final["provenance"]
    assert json.loads((output / "result.json").read_text()) == final
    parameters = read_csv(output / "parameter_intervals.csv")
    assert [row["name"] for row in parameters] == ["ka", "kb", "d0"]
    assert [row["unit"] for row in parameters] == [row["unit"] for row in original["parameters"]]
    assert all(float(row["interval_probability"]) == 0.9 for row in parameters)
    fitted = read_csv(output / "fitted_curve.csv")
    assert [float(row["force"]) for row in fitted] == original["curves"][0]["force"]
    assert [float(row["median"]) for row in fitted] == original["curves"][0]["median"]
    assert all(row["force_unit"] == "nN" and row["displacement_unit"] == "nm" for row in fitted)
    resonance = read_csv(output / "resonance.csv")[0]
    assert float(resonance["median_mhz"]) == 2.1
    assert float(resonance["conditional_fixed_kb_dpd"]) == 7850.288865935088
    assert resonance["kb_uncertainty_propagated"] == "False"
    assert float(resonance["interval_probability"]) == 0.9
    assert "ka uncertainty only" in resonance["assumptions"]


@pytest.mark.parametrize("by_dataset", [False, True])
def test_unavailable_resonance_has_explicit_reason_and_no_fabricated_frequency(scientific_output, monkeypatch, by_dataset):
    output, result = scientific_output
    reason = "SYNTHETIC unsupported posterior range"
    unavailable = {"status": "unavailable", "reason": reason, "dataset_id": "synthetic-curve"}
    result["resonance"] = {"status": "unavailable", "by_dataset": [unavailable]} if by_dataset else unavailable
    import matplotlib.figure
    texts = []
    native_savefig = matplotlib.figure.Figure.savefig
    def capture_figure(figure, filename, *args, **kwargs):
        if Path(filename).stem == "resonance":
            texts.extend(text.get_text() for axes in figure.axes for text in axes.texts)
            assert not any(line.get_xdata().size for axes in figure.axes for line in axes.lines)
        return native_savefig(figure, filename, *args, **kwargs)
    monkeypatch.setattr(matplotlib.figure.Figure, "savefig", capture_figure)
    adapter.add_downloads(output, result, configuration(3.2))
    row = read_csv(output / "resonance.csv")[0]
    assert row["status"] == "unavailable" and row["reason"] == reason
    assert row["lower_mhz"] == row["median_mhz"] == row["upper_mhz"] == ""
    assert any(reason in text for text in texts)


@pytest.mark.parametrize("contents", [
    "ka,kb,d0\nnan,20000,.1\n", "ka,kb,d0\n9500,inf,.1\n",
    "ka,kb,d0\ncorrupt,20000,.1\n", "ka,kb\n9500,20000\n", "ka,kb,d0\n",
])
def test_nonfinite_corrupt_or_empty_posterior_rejected(scientific_output, contents):
    output, result = scientific_output
    (output / "posterior_synthetic.csv").write_text(contents)
    with pytest.raises((ValueError, KeyError, TypeError)):
        adapter.add_downloads(output, result, configuration(3.2))


def test_missing_posterior_rejected(scientific_output):
    output, result = scientific_output
    (output / "posterior_synthetic.csv").unlink()
    with pytest.raises((ValueError, OSError)):
        adapter.add_downloads(output, result, configuration(3.2))


@pytest.mark.parametrize("name", ["../private.csv", "/private/results.csv", "nested/../../secret.csv", "private model.csv", "operator-private-notes.csv"])
def test_unsafe_or_private_publication_filename_rejected(scientific_output, name):
    output, result = scientific_output
    result["publication_files"].append(name)
    with pytest.raises(ValueError):
        adapter.add_downloads(output, result, configuration(3.2))


@pytest.mark.parametrize("target", ["posterior_synthetic.csv", "provenance.json", "result.json", "figures"])
def test_symlinked_outputs_are_never_followed(scientific_output, tmp_path, target):
    output, result = scientific_output
    destination = output / target
    outside = tmp_path / "private-target"
    if target == "figures":
        outside.mkdir()
    else:
        outside.write_bytes(destination.read_bytes())
        destination.unlink()
    destination.symlink_to(outside, target_is_directory=target == "figures")
    before = outside.read_bytes() if outside.is_file() else None
    with pytest.raises((ValueError, OSError)):
        adapter.add_downloads(output, result, configuration(3.2))
    if before is not None:
        assert outside.read_bytes() == before
    else:
        assert list(outside.iterdir()) == []


def test_private_path_content_is_rejected_before_any_publication_write(scientific_output):
    output, result = scientific_output
    before = {path.relative_to(output): path.read_bytes() for path in output.rglob("*") if path.is_file()}
    result["provenance"]["model_path"] = "/private/operator/model-weights/checkpoint.pt"
    with pytest.raises(ValueError):
        adapter.add_downloads(output, result, configuration(3.2))
    after = {path.relative_to(output): path.read_bytes() for path in output.rglob("*") if path.is_file()}
    assert after == before


@pytest.mark.parametrize("environment_hash", [None, "b" * 64])
def test_existing_runner_build_banner_and_optional_hash_are_portable(scientific_output, environment_hash):
    output, result = scientific_output
    result["provenance"]["runtime"].update(
        python="3.10.12 (main, Jun 23 2026, 12:00:00)\n[GCC 14.3.0]",
        environment_sha256=environment_hash,
    )
    # The existing runner writes both JSON files before the adapter runs.
    (output / "result.json").write_text(json.dumps(result))
    (output / "provenance.json").write_text(json.dumps(result["provenance"]))
    final = adapter.add_downloads(output, result, configuration(3.2))
    runtime = final["provenance"]["runtime"]
    assert runtime["python"] == "3.10.12"
    assert ("environment_sha256" in runtime) == (environment_hash is not None)
    for filename in ("result.json", "provenance.json", "settings_and_versions.json"):
        document = json.loads((output / filename).read_text())
        actual = document.get("provenance", document)["runtime"]
        assert actual == runtime


def test_legitimate_runtime_model_snapshot_is_excluded_from_publication(scientific_output):
    output, result = scientific_output
    private = output / "models/3.2um/trained/checkpoint.pkl"
    private.parent.mkdir(parents=True)
    private.write_bytes(b"SYNTHETIC private checkpoint placeholder")
    result["publication_files"].append("models/3.2um/trained/checkpoint.pkl")
    final = adapter.add_downloads(output, result, configuration(3.2))
    assert "models/3.2um/trained/checkpoint.pkl" not in final["publication_files"]
    assert all("models/" not in name for name in final["publication_files"])
    assert private.read_bytes() == b"SYNTHETIC private checkpoint placeholder"


@pytest.mark.parametrize("mutation", [
    lambda r: r["parameters"][0].update(median=float("nan")),
    lambda r: r["curves"][0]["force"].__setitem__(0, float("inf")),
    lambda r: r["curves"][0]["median"].__setitem__(0, float("nan")),
    lambda r: r["curves"][0].update(upper=[1.0]),
    lambda r: r["resonance"].update(lower_mhz=float("nan")),
])
def test_nonfinite_or_corrupt_result_intervals_rejected(scientific_output, mutation):
    output, result = scientific_output
    mutation(result)
    with pytest.raises((ValueError, TypeError)):
        adapter.add_downloads(output, result, configuration(3.2))


@pytest.mark.parametrize("mutation", [
    lambda r: r.update(publication_files=[]),
    lambda r: r["publication_files"].append("result.json"),
    lambda r: r["publication_files"].append("fitted_curve_missing.csv"),
])
def test_missing_duplicate_or_nonexistent_explicit_publications_rejected(scientific_output, mutation):
    output, result = scientific_output
    mutation(result)
    with pytest.raises((ValueError, OSError)):
        adapter.add_downloads(output, result, configuration(3.2))


def test_symlinked_manifest_rejected_before_runtime_invocation(tmp_path, monkeypatch):
    source = tmp_path / "actual-manifest.json"
    source.write_text(json.dumps(manifest_for(tmp_path)))
    symlink = tmp_path / "manifest.json"
    symlink.symlink_to(source)
    calls = []
    monkeypatch.setattr(adapter.subprocess, "run", lambda *args, **kwargs: calls.append(args))
    with pytest.raises((ValueError, OSError)):
        adapter.run(symlink, tmp_path / "output")
    assert calls == []
