"""Focused native adapter boundary tests; synthetic inputs only."""
from __future__ import annotations

from copy import deepcopy
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import stat
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor

import pytest

_SOURCE = Path(__file__).parents[1] / "src/aws_mesouq/adapters/benchmark_accuracy.py"
_SPEC = importlib.util.spec_from_file_location("accuracy_adapter_under_test", _SOURCE)
native = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(native)


def digest(raw):
    return hashlib.sha256(raw).hexdigest()


def private(path, raw=b"content"):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.parent.chmod(0o700)
    path.write_bytes(raw)
    path.chmod(0o600)
    return path


@pytest.mark.parametrize("value", ['{"a":1,"a":2}', '{"x":NaN}', '{"x":Infinity}'])
def test_native_json_rejects_ambiguous_nonfinite_metadata(value):
    with pytest.raises((ValueError, native.NativeContractError)):
        native._json(value)


@pytest.mark.parametrize("path", ["relative", "/", "/a/../b", "/a//b", "/a/./b",
                                   "/a b", "/a\nb", "/a/ü", "/a/$(id)"])
def test_paths_are_canonical_and_never_shell_code(path):
    with pytest.raises(native.NativeContractError):
        native._safe_absolute(path)


def test_stable_reader_detects_mutation_during_authenticated_use(tmp_path):
    path = private(tmp_path / "data")
    with pytest.raises(native.NativeContractError, match="changed"):
        with native.stable_bytes(path, private=True) as (raw, identity):
            assert raw == b"content"
            path.write_bytes(b"CHANGED")


def test_stable_reader_detects_same_size_replacement(tmp_path):
    path = private(tmp_path / "data")
    with pytest.raises(native.NativeContractError, match="changed"):
        with native.stable_bytes(path, private=True):
            fresh = private(tmp_path / "fresh", b"content")
            fresh.replace(path)


@pytest.mark.parametrize("kind", ["symlink", "hardlink", "directory", "fifo", "world-readable"])
def test_stable_reader_rejects_unsafe_leaves_without_fifo_block(tmp_path, kind):
    path = tmp_path / "data"
    if kind == "symlink":
        path.symlink_to(private(tmp_path / "real"))
    elif kind == "hardlink":
        os.link(private(tmp_path / "real"), path)
    elif kind == "directory":
        path.mkdir()
    elif kind == "fifo":
        os.mkfifo(path, 0o600)
    else:
        private(path).chmod(0o644)
    with pytest.raises((OSError, native.NativeContractError)):
        with native.stable_bytes(path, private=True):
            pytest.fail("Unsafe native leaf admitted")


def test_stable_reader_rejects_parent_symlink(tmp_path):
    real = tmp_path / "real"
    private(real / "data")
    (tmp_path / "link").symlink_to(real, target_is_directory=True)
    with pytest.raises(OSError):
        with native.stable_bytes(tmp_path / "link/data", private=True):
            pytest.fail("Symlink parent admitted")


def test_stable_reader_retains_size_bound(tmp_path):
    path = private(tmp_path / "data", b"12345")
    with pytest.raises(native.NativeContractError, match="bound"):
        with native.stable_bytes(path, maximum=4, private=True):
            pytest.fail("Oversized native leaf admitted")


def test_immutable_identical_retry_conflict_and_private_bytes(tmp_path):
    tmp_path.chmod(0o700)
    path = tmp_path / "result.json"
    native.immutable(path, b"first")
    native.immutable(path, b"first")
    with pytest.raises(native.NativeContractError, match="changed"):
        native.immutable(path, b"other")
    assert path.read_bytes() == b"first"
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert path.stat().st_nlink == 1
    assert list(tmp_path.glob("pending-*")) == []


def test_immutable_same_payload_concurrent_publication(tmp_path):
    tmp_path.chmod(0o700)
    path = tmp_path / "result.json"
    with ThreadPoolExecutor(max_workers=4) as pool:
        results = list(pool.map(lambda _: native.immutable(path, b"one"), range(8)))
    assert results == [str(path)] * 8
    assert path.read_bytes() == b"one"
    assert path.stat().st_nlink == 1


def sacct(*, job="123", marker="mqacc-safe", user="tester", state="COMPLETED",
          elapsed="60", tres="cpu=4,gres/gpu=1,gres/gpu:a100=1",
          cpu="240", start="2026-11-01T09:00:00", cpus="4", exit="0:0"):
    return "|".join([job, marker, user, state, elapsed, tres, cpu, start, cpus, exit]) + "|\n"


def parse(raw, cancelling=False):
    return native._parse_sacct(raw, job_id="123", marker="mqacc-safe",
                               user="tester", cpus=4, cancelling=cancelling)


@pytest.mark.parametrize("state", ["COMPLETED", "FAILED", "CANCELLED", "TIMEOUT",
                                   "OUT_OF_MEMORY", "NODE_FAIL", "PREEMPTED",
                                   "BOOT_FAIL", "DEADLINE"])
def test_native_actual_costs_retained_for_every_terminal_state(state):
    result = parse(sacct(state=state))
    assert result["accounting"] == {
        "source": "sacct", "site": "karolina", "slurm_job_id": "123",
        "state": state, "elapsed_seconds": 60, "allocated_gpus": 1, "cpu_seconds": 240}
    assert result["mechanical_failure"] is (state != "COMPLETED")


def test_nonzero_exit_is_mechanical_failure_even_completed():
    assert parse(sacct(exit="1:0"))["mechanical_failure"] is True


@pytest.mark.parametrize("state,normalized", [("PENDING", "queued"),
    ("CONFIGURING", "queued"), ("RUNNING", "running"), ("COMPLETING", "running")])
def test_active_costs_remain_unknown(state, normalized):
    result = parse(sacct(state=state, elapsed="", cpu="", cpus="", tres=""))
    assert result == {"job_id": "123", "state": normalized, "accounting": None}


def test_no_sacct_record_is_unknown_not_zero():
    assert parse("") is None


def test_steps_are_ignored_but_exact_root_row_required():
    assert parse(sacct(job="123.batch") + sacct())["accounting"]["elapsed_seconds"] == 60


@pytest.mark.parametrize("overrides", [
    {"job": "124"}, {"marker": "foreign"}, {"user": "foreign"},
    {"elapsed": "-1"}, {"elapsed": "1.5"}, {"cpu": "239"}, {"cpus": "8"},
    {"tres": "cpu=4,gres/gpu=2"}, {"tres": "gres/gpu=1,gres/gpu=1"},
    {"tres": "gres/gpu=1,gres/gpu:a100=2"}, {"tres": "gres/gpu=unknown"},
    {"state": "UNKNOWN"}, {"state": "REVOKED"}])
def test_sacct_adversarial_or_inconsistent_records_rejected(overrides):
    with pytest.raises(native.NativeContractError):
        parse(sacct(**overrides))


def test_duplicate_sacct_root_is_ambiguous():
    with pytest.raises(native.NativeContractError, match="Ambiguous"):
        parse(sacct() + sacct())


def test_authoritative_queued_cancellation_has_exact_typed_zero_proof():
    result = parse(sacct(state="CANCELLED", elapsed="0", tres="", cpu="0",
                         start="Unknown", cpus="0"), cancelling=True)
    assert result["accounting"]["preallocation_cancellation"] == {
        "start": None, "allocated_cpus": 0, "allocated_tres": ""}


@pytest.mark.parametrize("overrides,cancelling", [
    ({}, False), ({"state": "COMPLETED"}, True), ({"elapsed": "1"}, True),
    ({"cpu": "1"}, True), ({"cpus": "1"}, True),
    ({"tres": "cpu=0"}, True), ({"start": "2026-11-01T09:00:00"}, True)])
def test_unknown_native_cost_can_never_be_fabricated_as_zero(overrides, cancelling):
    args = dict(state="CANCELLED", elapsed="0", tres="", cpu="0",
                start="Unknown", cpus="0")
    args.update(overrides)
    with pytest.raises(native.NativeContractError):
        parse(sacct(**args), cancelling=cancelling)


def source_fixture(tmp_path):
    aws = tmp_path / "aws-src"
    sci = tmp_path / "science/src"
    entry = aws / "aws_mesouq/adapters/benchmark_accuracy.py"
    paths = [aws / "aws_mesouq/__init__.py", entry,
             sci / "meso_uq/__init__.py",
             sci / "meso_uq/known.py"]
    for path in paths:
        private(path, b"MARKER = 'frozen-source'\n")
    sources = {str(path): digest(path.read_bytes()) for path in paths}
    binding = {str(path.relative_to(sci / "meso_uq")): sources[str(path)]
               for path in paths if sci / "meso_uq" in path.parents}
    operator = {"adapter_path": str(entry), "repository": str(tmp_path / "science"),
                "native_python_sources": sources}
    context = {"runtime_profile_sha256": native.checksum(operator),
               "adapter_sha256": sources[str(entry)],
               "source_binding_sha256": native.checksum(binding)}
    return context, operator


def test_source_guard_checks_complete_science_closure_and_pins(tmp_path):
    context, operator = source_fixture(tmp_path)
    guard = native.SourceGuard(context, operator)
    guard.check()
    private(tmp_path / "science/src/meso_uq/unbound.py", b"raise RuntimeError()\n")
    with pytest.raises(native.NativeContractError, match="Complete"):
        native.SourceGuard(context, operator)


def test_source_guard_refuses_edited_or_replaced_source(tmp_path):
    context, operator = source_fixture(tmp_path)
    guard = native.SourceGuard(context, operator)
    path = Path(operator["adapter_path"])
    private(path, b"MARKER = 'changed-source'\n")
    with pytest.raises(native.NativeContractError, match="changed"):
        guard.check()
    with pytest.raises(native.NativeContractError, match="checksum"):
        native.SourceGuard(context, operator)


def test_source_guard_rejects_imported_own_package_even_if_origin_is_pinned(tmp_path, monkeypatch):
    context, operator = source_fixture(tmp_path)
    guard = native.SourceGuard(context, operator)
    class AlreadyImported:
        __file__ = next(name for name in operator["native_python_sources"]
                        if name.endswith("/meso_uq/known.py"))
    monkeypatch.setitem(sys.modules, "meso_uq.already_imported", AlreadyImported())
    with pytest.raises(native.NativeContractError, match="before source verification"):
        guard.install()


def test_frozen_loader_compiles_verified_bytes_not_disk_or_pyc(tmp_path):
    path = str(tmp_path / "module.py")
    private(Path(path), b"VALUE = 'untrusted disk'\n")
    loader = native._FrozenLoader(path, b"VALUE = 'verified snapshot'\n", False)
    spec = importlib.util.spec_from_loader("frozen_test_module", loader)
    module = importlib.util.module_from_spec(spec)
    loader.exec_module(module)
    assert module.VALUE == "verified snapshot"


def test_script_has_one_gpu_no_untrusted_import_path_and_bounded_clock(tmp_path):
    control = {"marker": "mqacc-safe", "control_directory": str(tmp_path),
               "manifest": {"kind": "hnpe_advance", "campaign_instance_id": "a"*32,
                            "task_id": "t00000001-"+"b"*32}}
    context = {"native_cpus": 4, "native_root": str(tmp_path / "native"),
               "adapter_sha256": "c"*64}
    operator = {"scheduler": {"partition": "qgpu", "account": "safe",
                "qos": "normal", "mem": "4G"},
        "environment_script": "/safe/env.sh", "environment_sha256": "a" * 64,
        "python_bin": "/safe/python", "adapter_path": "/safe/aws_mesouq/adapters/benchmark_accuracy.py",
        "repository": "/safe/science"}
    script = native._script(control, context, operator, {"time": "00:01:00"})
    assert "#SBATCH --gres=gpu:1\n" in script
    assert "#SBATCH --time=00:01:00\n" in script
    assert "SLURM_JOB_START_TIME" in script
    assert "SLURM_JOB_END_TIME" in script
    assert " -I -S -B -c " in script
    assert "/safe/aws_mesouq/adapters/benchmark_accuracy.py" in script
    assert "--manifest" in script
    assert "Native source/control checksum changed before execution" in script
    assert "export KERAS_BACKEND=jax" in script
    assert "sbatch" not in script
    assert "export PYTHONPATH=" in script
    assert "unset PYTHONHOME PYTHONSTARTUP PYTHONUSERBASE PYTHONPLATLIBDIR" in script
    assert "PYTHONNOUSERSITE=1" in script
    assert "export TMPDIR=" in script
    assert "export MPLCONFIGDIR=" in script
    assert "export CUDA_CACHE_PATH=" in script
    assert "export TRITON_CACHE_DIR=" in script
    assert "export NUMBA_CACHE_DIR=" in script
    assert "runtime-cache/" in script
    assert "export HOME=" not in script


def test_rpc_bootstrap_binds_exact_entrypoint_and_refuses_changed_bytes(tmp_path):
    entry = private(tmp_path / "adapter.py",
        b"def remote_request(request): return {'verified': True}\n")
    request = {"operation": "verify_profile",
        "context": {"adapter_sha256": digest(entry.read_bytes())},
        "operator": {"adapter_path": str(entry)}}
    result = subprocess.run([sys.executable, "-I", "-S", "-B", "-c", native._BOOTSTRAP],
        input=native.canonical(request), capture_output=True, timeout=10)
    assert result.returncode == 0
    assert json.loads(result.stdout) == {"verified": True}
    private(entry, b"raise RuntimeError('modified must never run')\n")
    result = subprocess.run([sys.executable, "-I", "-S", "-B", "-c", native._BOOTSTRAP],
        input=native.canonical(request), capture_output=True, timeout=10)
    assert result.returncode != 0
    assert b"modified must never run" not in result.stderr


def test_executor_uses_only_fixed_existing_transport_and_cancel_guard():
    class Contexts:
        def context(self, identity): return {"id": identity}
        def operator(self, identity): return {"adapter_path": "/safe/adapter.py"}
        def supported(self): return ["approved"]
        def validate_claim(self, claim): return claim
    class Transport:
        staging_python = "/usr/bin/python3.11"
        calls = []
        def run_bytes(self, argv, *, data):
            self.calls.append((argv, json.loads(data)))
            return b'{"verified":true}'
    class Slurm:
        transport = Transport()
        def cancel(self, job, marker, *, authorize):
            self.cancelled = (job, marker)
            return authorize()
    slurm = Slurm()
    executor = native.AccuracyNativeExecutor(slurm, Contexts())
    assert executor.admitted_contexts() == ["approved"]
    argv, request = slurm.transport.calls[0]
    assert argv == ["/usr/bin/python3.11", "-I", "-S", "-B", "-c", native._BOOTSTRAP]
    assert request["operation"] == "verify_profile"
    entry = {"job_id": "123", "claim": {"task": {"marker": "safe"}}}
    invoked = []
    executor.cancel(entry, authorize=lambda: invoked.append(True) or True)
    assert invoked == [True]
    assert slurm.cancelled == ("123", "safe")
    with pytest.raises(native.NativeContractError):
        executor.cancel(entry, authorize=None)


def test_adapter_never_submits_itself():
    tree = __import__("ast").parse(_SOURCE.read_text())
    calls = [n for n in __import__("ast").walk(tree) if isinstance(n, __import__("ast").Call)]
    assert not any(isinstance(n.func, __import__("ast").Attribute) and n.func.attr == "submit"
                   for n in calls)


@pytest.mark.parametrize("reservation,allocated", [(60, 60), (61, 60),
    (119, 60), (120, 120), (172801, 172800)])
def test_funded_window_never_rounds_above_original_reservation(reservation, allocated):
    window = {"allocated_seconds": allocated, "internal_seconds": float(allocated),
        "runtime_clock_policy": "allocation_deadline_v1",
        "time": f"{allocated // 3600:02d}:{allocated // 60 % 60:02d}:{allocated % 60:02d}"}
    assert native._validate_window(window, reservation) == window


@pytest.mark.parametrize("field,value,reservation", [
    ("allocated_seconds", 60, 59), ("allocated_seconds", True, 60),
    ("allocated_seconds", 120, 119), ("internal_seconds", True, 60),
    ("internal_seconds", float("nan"), 60), ("internal_seconds", 61, 60),
    ("runtime_clock_policy", "other", 60), ("time", "00:01:00\n#SBATCH --gres=gpu:8", 60)])
def test_native_window_rejects_boolean_nonfinite_overfunded_or_injected_values(field, value, reservation):
    window = {"allocated_seconds": 60, "internal_seconds": 60,
              "runtime_clock_policy": "allocation_deadline_v1", "time": "00:01:00"}
    window[field] = value
    with pytest.raises(native.NativeContractError):
        native._validate_window(window, reservation)


def test_immutable_exact_crash_alias_recovery_is_idempotent(tmp_path):
    final = private(tmp_path / "receipt.json", b"one")
    pending = tmp_path / ("pending-receipt.json-" + "a" * 32 + ".tmp")
    os.link(final, pending)
    assert final.stat().st_nlink == 2
    native.immutable(final, b"one")
    assert not pending.exists()
    assert final.stat().st_nlink == 1


def test_immutable_never_unlinks_unknown_or_foreign_alias(tmp_path):
    final = private(tmp_path / "receipt.json", b"one")
    foreign = tmp_path / "foreign-original.json"
    os.link(final, foreign)
    with pytest.raises(native.NativeContractError, match="unclosed aliases"):
        native.immutable(final, b"one")
    assert foreign.read_bytes() == b"one"
    assert final.stat().st_nlink == 2


def wrapper_fixture(tmp_path, *, producer="123"):
    campaign = tmp_path / "campaign"
    stage = campaign / "stage"
    wrapper_root = campaign / "task"
    raw = {"slurm_job_id": producer, "cases": []}
    private(stage / "receipt.json", native.canonical(raw))
    manifest = {key: key + "-identity" for key in (
        "campaign_instance_id", "task_id", "context_id",
        "source_binding_sha256", "scientific_request_hash",
        "transport_request_hash", "input_ledger_sha256")}
    manifest.update(attempt=1, kind="guide_reference")
    control = {"campaign_directory": str(campaign), "manifest": manifest,
               "manifest_sha256": "f" * 64}
    wrapper = {"schema": native.WRAPPER_SCHEMA, "binding": native._binding(control, "123"),
        "source_receipts": {"stage": {"directory": str(stage), "file": "receipt.json",
            "sha256": digest((stage / "receipt.json").read_bytes())}},
        "outputs": {}, "output_pins": {}, "proposal": None,
        "scientific_status": None, "qualification": None, "checks": [], "artifacts": []}
    private(wrapper_root / "native-receipt.json", native.canonical(wrapper))
    pin = {"directory": str(wrapper_root),
           "sha256": digest((wrapper_root / "native-receipt.json").read_bytes())}
    return control, pin, wrapper, stage, wrapper_root


def test_source_entry_authenticated_wrapper_maps_exact_raw_receipt_abi(tmp_path):
    control, pin, wrapper, stage, root = wrapper_fixture(tmp_path)
    entry, raw = native._source_entry(control, pin)
    assert entry == {"directory": str(stage),
                     "receipt_sha256": wrapper["source_receipts"]["stage"]["sha256"]}
    assert raw["slurm_job_id"] == "123"


def test_wrapper_cannot_substitute_raw_receipt_or_changed_campaign(tmp_path):
    control, pin, wrapper, stage, root = wrapper_fixture(tmp_path)
    raw_pin = {"directory": str(stage),
               "sha256": wrapper["source_receipts"]["stage"]["sha256"]}
    with pytest.raises(FileNotFoundError):
        native._source_entry(control, raw_pin)
    changed = deepcopy(control)
    changed["manifest"]["campaign_instance_id"] = "changed"
    with pytest.raises(native.NativeContractError, match="identity/source"):
        native._source_entry(changed, pin)


def test_wrapper_cannot_change_source_file_checksum_or_producer(tmp_path):
    control, pin, wrapper, stage, root = wrapper_fixture(tmp_path, producer="124")
    with pytest.raises(native.NativeContractError, match="producer"):
        native._source_entry(control, pin)
    private(stage / "receipt.json", b'{"slurm_job_id":"123"}')
    with pytest.raises(native.NativeContractError, match="bytes changed"):
        native._source_entry(control, pin)


def test_dependency_manifest_hashes_exact_raw_crlf_bytes(tmp_path, monkeypatch):
    root = tmp_path / "demo_pkg-1.0.dist-info"
    metadata = b"Metadata-Version: 2.1\r\nName: demo-pkg\r\nVersion: 1.0\r\n\r\n"
    record = b"demo.py,sha256=example,4\r\n"
    private(root / "METADATA", metadata)
    private(root / "RECORD", record)
    class Distribution:
        _path = root
        def read_text(self, name):
            pytest.fail("Raw metadata pin must not use newline-normalized read_text")
    monkeypatch.setattr(native.importlib.metadata, "distribution", lambda name: Distribution())
    monkeypatch.setattr(native, "_file_sha", lambda *args, **kwargs: "a" * 64)
    snapshot = native.dependency_snapshot(["demo-pkg"])
    assert snapshot["distributions"] == [{
        "name": "demo-pkg", "version": "1.0", "record_sha256": digest(record),
        "metadata_sha256": digest(metadata)}]


def test_dependency_manifest_absent_record_is_explicit_null(tmp_path, monkeypatch):
    root = tmp_path / "demo_pkg-1.0.dist-info"
    private(root / "METADATA", b"Name: demo-pkg\nVersion: 1.0\n")
    class Distribution:
        _path = root
    monkeypatch.setattr(native.importlib.metadata, "distribution", lambda name: Distribution())
    monkeypatch.setattr(native, "_file_sha", lambda *args, **kwargs: "a" * 64)
    assert native.dependency_snapshot(["demo-pkg"])["distributions"][0]["record_sha256"] is None


@pytest.mark.parametrize("metadata", [
    b"Name: other\nVersion: 1.0\n", b"Name: demo-pkg\n",
    b"Name: demo-pkg\nVersion: 1.0\nVersion: 2.0\n"])
def test_dependency_manifest_rejects_ambiguous_package_identity(tmp_path, monkeypatch, metadata):
    root = tmp_path / "demo_pkg-1.0.dist-info"
    private(root / "METADATA", metadata)
    class Distribution:
        _path = root
    monkeypatch.setattr(native.importlib.metadata, "distribution", lambda name: Distribution())
    monkeypatch.setattr(native, "_file_sha", lambda *args, **kwargs: "a" * 64)
    with pytest.raises(native.NativeContractError, match="identity is ambiguous"):
        native.dependency_snapshot(["demo-pkg"])


def test_runtime_cache_policy_is_bound_outside_archived_campaign(tmp_path):
    context = {"native_root": str(tmp_path / "native")}
    manifest = {"campaign_instance_id": "a"*32, "task_id": "t00000001-"+"b"*32}
    environment = native._runtime_environment(context, manifest)
    assert set(environment) == {"TMPDIR", "XDG_CACHE_HOME", "MPLCONFIGDIR",
        "KERAS_HOME", "TORCH_HOME", "JAX_COMPILATION_CACHE_DIR",
        "CUDA_CACHE_PATH", "TRITON_CACHE_DIR", "NUMBA_CACHE_DIR", "TMP", "TEMP",
        "TORCH_EXTENSIONS_DIR", "TORCHINDUCTOR_CACHE_DIR", "PIP_CACHE_DIR"}
    archived = Path(context["native_root"]) / manifest["campaign_instance_id"]
    for path in environment.values():
        assert archived not in Path(path).parents
        assert Path(context["native_root"]) / "runtime-cache" in Path(path).parents


def dependency_v2_fixture(tmp_path, entry, environment):
    site = tmp_path / "dependencies" / "site"
    private(site / "benchmark_demo-1.0.dist-info/METADATA",
            b"Name: benchmark-demo\r\nVersion: 1.0\r\n")
    private(site / "benchmark_demo-1.0.dist-info/RECORD", b"demo.py,,4\r\n")
    cfg = private(tmp_path / "python" / "pyvenv.cfg", b"synthetic provenance only\n")
    receipt = private(tmp_path / "snapshot-receipt.json", b'{"synthetic":true}\n')
    normalization = private(tmp_path / "normalization-receipt.json", b'{"synthetic":true}\n')
    stdlib = Path(os.__file__).resolve().parent
    spec = {
        "python": {"requested_executable": sys.executable, "pyvenv_cfg_path": str(cfg)},
        "stdlib_paths": [str(stdlib), str(stdlib / "lib-dynload")],
        "overlays": [{"path": str(site), "origin": "/reviewed/original/site",
                      "source_content_sha256": "a"*64, "target_content_sha256": "a"*64}],
        "bootstrap": {"path": str(environment), "sha256": digest(environment.read_bytes()),
                      "declared_overlay_origins": ["/reviewed/original/site"],
                      "excluded_source_roots": ["/reviewed/legacy/science/src"]},
        "snapshot": {"receipt_path": str(receipt), "receipt_sha256": digest(receipt.read_bytes()),
                     "normalization_receipt_path": str(normalization),
                     "normalization_receipt_sha256": digest(normalization.read_bytes())},
        "distribution_names": ["benchmark-demo"]}
    command = ("import json,sys;"
        "p=sys.argv[1];d={'__name__':'__metadata_fixture__','__file__':p};"
        "exec(compile(open(p,'rb').read(),p,'exec',dont_inherit=True),d);"
        "print(json.dumps(d['isolated_dependency_snapshot'](json.load(sys.stdin))))")
    result = subprocess.run([sys.executable, "-I", "-S", "-B", "-c", command, str(entry)],
        input=native.canonical(spec), check=True, capture_output=True, timeout=30)
    return json.loads(result.stdout)


def executable_profile_fixture(tmp_path):
    """Actual -I-S-B interpreter, private synthetic metadata; no ML imports."""
    import pwd
    context, operator = source_fixture(tmp_path)
    entry = Path(operator["adapter_path"])
    private(entry, _SOURCE.read_bytes())
    operator["native_python_sources"][str(entry)] = digest(entry.read_bytes())
    context["adapter_sha256"] = digest(entry.read_bytes())
    environment = private(tmp_path / "environment.sh", b":\n")
    dependencies = dependency_v2_fixture(tmp_path, entry, environment)
    dep_path = private(tmp_path / "dependency-manifest.json", native.canonical(dependencies))
    operator.update(python_bin=sys.executable,
        python_sha256=dependencies["observation"]["python"]["sha256"],
        dependency_overlays=[row["path"] for row in dependencies["spec"]["overlays"]],
        environment_script=str(environment), environment_sha256=digest(environment.read_bytes()),
        dependency_manifest_path=str(dep_path),
        dependency_manifest_sha256=digest(dep_path.read_bytes()), expected_host="login1.karolina.it4i.cz")
    repository = Path(operator["repository"])
    git_env = {**os.environ, "GIT_CONFIG_GLOBAL": "/dev/null", "GIT_CONFIG_NOSYSTEM": "1"}
    for argv in (["git", "init", "-q", str(repository)],
        ["git", "-C", str(repository), "add", "src"],
        ["git", "-C", str(repository), "-c", "user.name=Synthetic QA",
         "-c", "user.email=qa@example.invalid", "commit", "-qm", "Synthetic source fixture"]):
        subprocess.run(argv, check=True, capture_output=True, env=git_env, timeout=15)
    revision = subprocess.run(["git", "-C", str(repository), "rev-parse", "HEAD"],
        check=True, capture_output=True, text=True, env=git_env, timeout=15).stdout.strip()
    context.update(native_user=pwd.getpwuid(os.getuid()).pw_name, science_revision=revision,
                   runtime_profile_sha256=native.checksum(operator))
    return context, operator, dependencies


def test_real_metadata_preflight_uses_no_scientific_package_import(tmp_path):
    context, operator, expected = executable_profile_fixture(tmp_path)
    before = {name for name in sys.modules if name == "meso_uq" or name.startswith("meso_uq.")}
    guard = native._verify_profile(context, operator, allocated=True)
    guard.check()
    after = {name for name in sys.modules if name == "meso_uq" or name.startswith("meso_uq.")}
    assert after == before


def test_dependency_preflight_rejects_reviewed_but_stale_distribution_version(tmp_path):
    context, operator, manifest = executable_profile_fixture(tmp_path)
    manifest["observation"]["distributions"][0]["version"] = "stale-version"
    path = Path(operator["dependency_manifest_path"])
    private(path, native.canonical(manifest))
    operator["dependency_manifest_sha256"] = digest(path.read_bytes())
    context["runtime_profile_sha256"] = native.checksum(operator)
    with pytest.raises(native.NativeContractError, match="differs from reviewed"):
        native._verify_profile(context, operator, allocated=True)


def test_preflight_rejects_changed_environment_before_any_probe(tmp_path):
    context, operator, _ = executable_profile_fixture(tmp_path)
    private(Path(operator["environment_script"]), b"exit 55\n")
    with pytest.raises(native.NativeContractError, match="Environment script changed"):
        native._verify_profile(context, operator, allocated=True)


def quota_text(*, used=22_162_300, hard=24_414_064, entries=247_597,
               hard_entries=500_000, grace=0, entries_grace=0,
               user="tester", uid=11515, mount="/home"):
    return (f"Disk quotas for user {user} (uid {uid}):\n"
        "Filesystem blocks quota limit grace files quota limit grace\n"
        f"10.49.4.126:/home {mount} {used} 0 {hard} {grace} "
        f"{entries} 0 {hard_entries} {entries_grace}\n")


def parse_quota(raw, **kwargs):
    return native.parse_home_quota(raw, user="tester", uid=11515,
                                   observed_at=100.0, now=100.0, **kwargs)


def test_quota_uses_exact_assigned_kib_cap_and_marks_warning():
    result = parse_quota(quota_text())
    assert result["hard_bytes"] == 24_414_064 * 1024
    assert result["free_bytes"] == (24_414_064 - 22_162_300) * 1024
    assert result["free_entries"] == 500_000 - 247_597
    assert result["eligible"] is True
    assert result["warning"] is True


@pytest.mark.parametrize("free_kib,free_entries,eligible,warning", [
    (1_048_575, 10_000, False, True),
    (1_048_576, 10_000, True, True),
    (1_048_576, 9_999, False, True),
    (5_242_879, 10_000, True, True),
    (5_242_880, 10_000, True, False)])
def test_quota_operational_byte_inode_boundaries(free_kib, free_entries, eligible, warning):
    result = parse_quota(quota_text(used=24_414_064-free_kib,
                                    entries=500_000-free_entries))
    assert result["eligible"] is eligible
    assert result["warning"] is warning


@pytest.mark.parametrize("change", [
    lambda s: s + s.splitlines()[-1] + "\n",
    lambda s: "\n".join(s.splitlines()[:2]) + "\n",
    lambda s: s.replace("tester", "other"),
    lambda s: s.replace("(uid 11515)", "(uid 11516)"),
    lambda s: s.replace("Filesystem blocks", "Filesystem bytes"),
    lambda s: s.replace("22162300", "22162300*"),
    lambda s: s.replace("22162300", "22.1M"),
    lambda s: s.replace("22162300", "-1"),
    lambda s: s.replace(" /home ", " /scratch "),
    lambda s: s.replace("10.49.4.126:/home", "10.49.4.126:/scratch"),
    lambda s: s.replace("24414064", "0"),
    lambda s: s.replace("500000", "0")])
def test_quota_rejects_ambiguous_unknown_or_foreign_rows(change):
    with pytest.raises(native.NativeContractError):
        parse_quota(change(quota_text()))


@pytest.mark.parametrize("grace,entries_grace", [(1, 0), (0, 1), (100, 100)])
def test_quota_active_grace_never_authorizes_new_work(grace, entries_grace):
    with pytest.raises(native.NativeContractError, match="grace"):
        parse_quota(quota_text(grace=grace, entries_grace=entries_grace))


@pytest.mark.parametrize("observed,now", [(100.0, 131.0), (102.0, 100.0),
                                         (True, 100.0), (float("nan"), 100.0)])
def test_quota_stale_future_boolean_nonfinite_snapshot_is_rejected(observed, now):
    with pytest.raises(native.NativeContractError):
        native.parse_home_quota(quota_text(), user="tester", uid=11515,
                                observed_at=observed, now=now)


def test_home_preflight_executes_fresh_implicit_uid_query_at_every_boundary(monkeypatch):
    from types import SimpleNamespace
    calls = []
    def query(argv, **kwargs):
        calls.append((argv, kwargs))
        return SimpleNamespace(returncode=0, stderr="",
            stdout=quota_text(uid=os.getuid(), used=22_162_300 + len(calls)))
    monkeypatch.setattr(native.subprocess, "run", query)
    first = native._home_preflight({"native_user": "tester"})
    second = native._home_preflight({"native_user": "tester"})
    assert second["used_bytes"] == first["used_bytes"] + 1024
    assert len(calls) == 2
    for argv, kwargs in calls:
        assert argv == ["quota", "-w", "-v", "-p", "--show-mntpoint", "-f", "/home"]
        assert "-u" not in argv
        assert kwargs["env"] == {"PATH": os.defpath, "LC_ALL": "C"}


def test_quota_denial_carries_only_bounded_task_bound_scalar_snapshot(monkeypatch):
    from types import SimpleNamespace
    monkeypatch.setattr(native.subprocess, "run", lambda *a, **kw: SimpleNamespace(
        returncode=0, stderr="", stdout=quota_text(uid=os.getuid(), used=24_414_063)))
    context = {"native_user": "tester", "id": "a"*64}
    with pytest.raises(native.NativeContractError) as caught:
        native._home_preflight(context, manifest_sha256="b"*64)
    proof = caught.value.preflight
    assert set(proof) == {"schema", "context_id", "manifest_sha256", "checked_epoch", "home_quota"}
    assert proof["schema"] == "aws_mesouq.benchmark_accuracy_preflight.v1"
    assert proof["context_id"] == "a"*64
    assert proof["manifest_sha256"] == "b"*64
    assert proof["home_quota"]["eligible"] is False
    assert len(native.canonical(proof)) < 65_536


@pytest.mark.parametrize("returncode,stderr", [(1, ""), (0, "warning")])
def test_unavailable_quota_fails_closed_without_invented_values(monkeypatch, returncode, stderr):
    from types import SimpleNamespace
    monkeypatch.setattr(native.subprocess, "run", lambda *a, **kw: SimpleNamespace(
        returncode=returncode, stderr=stderr, stdout=quota_text(uid=os.getuid())))
    with pytest.raises(native.NativeContractError, match="unavailable") as caught:
        native._home_preflight({"native_user": "tester"})
    assert not hasattr(caught.value, "preflight")


def test_low_quota_prepare_has_no_staging_side_effect(tmp_path, monkeypatch):
    from aws_mesouq import benchmark_accuracy_protocol as protocol
    class Guard:
        def install(self): return self
    monkeypatch.setattr(native, "_verify_profile", lambda *a, **kw: Guard())
    monkeypatch.setattr(protocol, "validate_context", lambda value: value)
    monkeypatch.setattr(protocol, "validate_manifest", lambda value: value)
    monkeypatch.setattr(native, "_home_preflight", lambda *a, **kw:
        (_ for _ in ()).throw(native.NativeContractError("quota refused")))
    monkeypatch.setattr(native, "_mkdir", lambda *a, **kw: pytest.fail("Staging ran before quota eligibility"))
    monkeypatch.setattr(native, "_require_scratch", lambda path: path)
    manifest = {"reservation": {"gpu_seconds": 60}}
    claim = {"task": {"manifest": manifest, "manifest_sha256": native.checksum(manifest)}}
    window = {"allocated_seconds": 60, "internal_seconds": 60,
              "runtime_clock_policy": "allocation_deadline_v1", "time": "00:01:00"}
    with pytest.raises(native.NativeContractError, match="quota refused"):
        native._prepare({"context": {"enabled": True, "native_root": str(tmp_path)}, "operator": {}, "claim": claim, "window": window})


def test_observation_recovery_does_not_require_new_work_quota(monkeypatch):
    class Guard:
        def check(self): return None
    monkeypatch.setattr(native, "_verify_profile", lambda *a, **kw: Guard())
    monkeypatch.setattr(native, "_home_preflight", lambda *a, **kw:
        pytest.fail("Quota eligibility blocked cost reconciliation"))
    expected = {"job_id": "123", "state": "terminal", "accounting": {"actual": "retained"}}
    monkeypatch.setattr(native, "_observe", lambda payload: expected)
    assert native.remote_request({"operation": "observe", "context": {}, "operator": {},
                                  "entry": {}}) == expected


def test_group_writable_dependency_metadata_only_allowed_with_private_ancestor(tmp_path):
    path = private(tmp_path / "private/metadata", b"metadata")
    path.chmod(0o664)
    with native._dependency_bytes(path, maximum=100) as (raw, _):
        assert raw == b"metadata"
    path.chmod(0o666)
    with pytest.raises(native.NativeContractError):
        with native._dependency_bytes(path, maximum=100):
            pytest.fail("World-writable metadata admitted")


def test_dependency_metadata_rejects_reparented_ancestor(tmp_path):
    path = private(tmp_path / "private/metadata", b"metadata")
    with pytest.raises(native.NativeContractError, match="ancestor changed"):
        with native._dependency_bytes(path, maximum=100):
            path.parent.rename(tmp_path / "moved")
            private(tmp_path / "private/metadata", b"metadata")


def test_group_writable_own_python_source_is_still_rejected(tmp_path):
    context, operator = source_fixture(tmp_path)
    Path(operator["adapter_path"]).chmod(0o664)
    with pytest.raises(native.NativeContractError):
        native.SourceGuard(context, operator)


def test_explicit_bytecode_disabled_bootstrap_creates_no_home_cache(tmp_path):
    probe = private(tmp_path / "probe.py", b"VALUE = 1\n")
    code = ("import sys\nsys.path.insert(0, " + repr(str(tmp_path)) + ")\n"
            "import probe\n"
            "def remote_request(request): return {'verified': True}\n").encode()
    entry = private(tmp_path / "adapter.py", code)
    request = {"operation": "verify_profile", "context": {"adapter_sha256": digest(code)},
               "operator": {"adapter_path": str(entry)}}
    result = subprocess.run([sys.executable, "-I", "-S", "-B", "-c", native._BOOTSTRAP],
        input=native.canonical(request), capture_output=True, timeout=10)
    assert result.returncode == 0
    assert not (tmp_path / "__pycache__").exists()


def native_control_fixture(tmp_path):
    context = {"id": "c"*64, "enabled": True, "native_root": str(tmp_path / "native"),
        "source_binding_sha256": "d"*64, "policy_sha256": "e"*64,
        "native_cpus": 4, "native_gpus": 1, "runtime_profile_sha256": native.checksum({})}
    manifest = {"campaign_instance_id": "a"*32, "task_id": "t00000001-"+"b"*32,
        "attempt": 1, "context_id": context["id"],
        "source_binding_sha256": context["source_binding_sha256"],
        "policy_sha256": context["policy_sha256"],
        "reservation": {"allocation_id": "allocation", "gpu_seconds": 60,
                        "native_cpus": 4, "native_gpus": 1}}
    campaign, directory = native._paths(context, manifest)
    window = {"allocated_seconds": 60, "internal_seconds": 60,
              "runtime_clock_policy": "allocation_deadline_v1", "time": "00:01:00"}
    control = {"schema": native.CONTROL_SCHEMA, "context": deepcopy(context),
        "operator": {}, "manifest": manifest, "manifest_sha256": native.checksum(manifest),
        "marker": "mqacc-"+manifest["campaign_instance_id"]+"-"+manifest["task_id"],
        "window": window, "control_directory": str(directory),
        "campaign_directory": str(campaign),
        "runtime_environment": native._runtime_environment(context, manifest)}
    private(directory / "control.json", native.canonical(control))
    script = private(directory / "run.sh", b"#!/bin/bash\nexit 0\n")
    script_sha = digest(script.read_bytes())
    entry = {"context_id": context["id"], "manifest_sha256": control["manifest_sha256"],
        "claim": {"task": {"manifest": manifest, "manifest_sha256": control["manifest_sha256"],
                           "marker": control["marker"], "status": "claimed"},
                  "campaign": {"status": "running"}},
        "native_preparation": {"script_path": str(script), "script_sha256": script_sha,
            "marker": control["marker"], "window": window,
            "intent_sha256": native.checksum({"manifest_sha256": control["manifest_sha256"],
                "script_sha256": script_sha, "marker": control["marker"], "window": window})}}
    return context, control, entry, script


def test_enabled_toggle_preserves_only_existing_context_recovery(tmp_path):
    context, control, entry, script = native_control_fixture(tmp_path)
    context["enabled"] = False
    assert native._entry_control(context, entry)["context"]["enabled"] is True
    context["native_cpus"] = 8
    with pytest.raises(native.NativeContractError, match="identity changed"):
        native._entry_control(context, entry)


def test_pre_submission_rechecks_exact_script_and_intent_before_quota(tmp_path, monkeypatch):
    context, control, entry, script = native_control_fixture(tmp_path)
    calls = []
    quota = parse_quota(quota_text())
    monkeypatch.setattr(native, "_home_preflight", lambda *a, **kw: calls.append(True) or quota)
    result = native._prepared_preflight({"context": context, "entry": entry})
    assert result["context_id"] == context["id"]
    assert result["manifest_sha256"] == control["manifest_sha256"]
    assert calls == [True]
    private(script, b"#!/bin/bash\nexit 1\n")
    with pytest.raises(native.NativeContractError, match="script/intent bytes changed"):
        native._prepared_preflight({"context": context, "entry": entry})
    assert calls == [True]


def test_disabled_context_cannot_authorize_first_submission(tmp_path, monkeypatch):
    context, control, entry, script = native_control_fixture(tmp_path)
    context["enabled"] = False
    monkeypatch.setattr(native, "_home_preflight", lambda *a, **kw:
        pytest.fail("Disabled context reached new-work quota authorization"))
    with pytest.raises(native.NativeContractError, match="Disabled"):
        native._prepared_preflight({"context": context, "entry": entry})


@pytest.mark.parametrize("changed", ["source", "control"])
def test_native_bootstrap_checks_source_and_control_before_main_runs(tmp_path, changed):
    marker = tmp_path / "must-not-execute"
    code = ("from pathlib import Path\n"
            "def main(argv):\n"
            " Path("+repr(str(marker))+").write_text('executed')\n"
            " return 0\n").encode()
    entry = private(tmp_path / "entry.py", code)
    control = private(tmp_path / "control.json", b'{"immutable":true}')
    source_sha, control_sha = digest(code), digest(control.read_bytes())
    if changed == "source":
        private(entry, b"raise RuntimeError('changed source must never execute')\n")
    else:
        private(control, b'{"immutable":false}')
    result = subprocess.run([sys.executable, "-I", "-B", "-c", native._NATIVE_BOOTSTRAP,
        str(entry), source_sha, str(control), control_sha, "--manifest"],
        capture_output=True, timeout=10)
    assert result.returncode != 0
    assert not marker.exists()
    assert b"checksum changed before execution" in result.stderr


def test_native_bootstrap_executes_authenticated_main_with_fixed_stage_action(tmp_path):
    code = b"def main(argv):\n assert argv[0]=='--execute-stage'\n return 0\n"
    entry = private(tmp_path / "entry.py", code)
    control = private(tmp_path / "control.json", b'{"immutable":true}')
    result = subprocess.run([sys.executable, "-I", "-B", "-c", native._NATIVE_BOOTSTRAP,
        str(entry), digest(code), str(control), digest(control.read_bytes()), "--execute-stage"],
        capture_output=True, timeout=10)
    assert result.returncode == 0


def test_typed_quota_denial_rpc_preserves_exact_task_binding():
    proof = {"schema": "aws_mesouq.benchmark_accuracy_preflight.v1",
        "context_id": "a"*64, "manifest_sha256": "b"*64, "checked_epoch": 100.0,
        "home_quota": {"eligible": False}}
    class Contexts:
        def context(self, identity): return {"id": identity}
        def operator(self, identity): return {}
        def validate_claim(self, claim): return claim
    class Transport:
        def run_bytes(self, argv, *, data):
            return native.canonical({"schema": "aws_mesouq.benchmark_accuracy_native_error.v1",
                "code": "home_quota_unhealthy", "preflight": proof})
    class Slurm:
        transport = Transport()
    entry = {"context_id": "a"*64, "manifest_sha256": "b"*64,
        "claim": {"task": {"manifest": {}, "manifest_sha256": "b"*64,
                           "marker": "safe", "status": "claimed"},
                  "campaign": {"status": "running"}}}
    executor = native.AccuracyNativeExecutor(Slurm(), Contexts())
    with pytest.raises(native.NativeContractError) as caught:
        executor.preflight(entry)
    assert caught.value.preflight == proof
    proof["manifest_sha256"] = "c"*64
    with pytest.raises(native.NativeContractError, match="Unbound") as caught:
        executor.preflight(entry)
    assert not hasattr(caught.value, "preflight")


def coordinator_entry_fixture(tmp_path):
    context, control, entry, script = native_control_fixture(tmp_path)
    entry["claim"]["task"]["lease"] = {"token": "LEASE_SENTINEL"}
    entry["claim"]["campaign"]["owner_email"] = "PRIVATE_OWNER_SENTINEL"
    entry.update(pending_event={"body": {"signature": "SIGNATURE_SENTINEL",
        "lease_token": "PENDING_LEASE_SENTINEL"}},
        upload_plan=[{"upload": ["https://private.invalid/?X-Amz-Signature=URL_SENTINEL", {}]}],
        pending_refresh={"signature": "REFRESH_SIGNATURE_SENTINEL"},
        auth={"access_token": "AUTH_TOKEN_SENTINEL"},
        history=[{"private": "JOURNAL_HISTORY_SENTINEL"}])
    return context, control, entry


@pytest.mark.parametrize("operation", ["preflight", "observe", "publication", "read_artifact"])
def test_native_rpc_never_serializes_full_coordinator_entry(tmp_path, operation):
    context, control, entry = coordinator_entry_fixture(tmp_path)
    manifest_sha = entry["manifest_sha256"]
    class Contexts:
        def context(self, identity): return {"id": identity}
        def operator(self, identity): return {"adapter_path": "/safe/adapter.py"}
        def validate_claim(self, claim): return claim
    class Transport:
        calls = []
        def run_bytes(self, argv, *, data):
            self.calls.append(data)
            return b"payload" if operation == "read_artifact" else b'{"verified":true}'
    class Slurm:
        transport = Transport()
    executor = native.AccuracyNativeExecutor(Slurm(), Contexts())
    if operation == "publication":
        snapshot = {"schema": "synthetic", "allocations": []}
        executor.publication(entry, {"snapshot": snapshot, "sha256": native.checksum(snapshot),
                                     "coordinator_only": "ACCOUNTING_PRIVATE_SENTINEL"})
    elif operation == "read_artifact":
        executor.read_artifact(entry, {"name": "data.json", "sha256": digest(b"payload"),
            "size_bytes": 7, "media_type": "application/json", "kind": "result"})
    else:
        getattr(executor, operation)(entry)
    wire = Slurm.transport.calls[-1]
    assert b"SENTINEL" not in wire
    assert b"X-Amz-Signature" not in wire
    payload = json.loads(wire)
    native_entry = payload["entry"]
    assert set(native_entry) == {"context_id", "manifest_sha256", "job_id", "claim", "native_preparation"}
    assert set(native_entry["claim"]["task"]) == {"manifest", "manifest_sha256", "marker", "status"}
    assert native_entry["manifest_sha256"] == manifest_sha


def test_prepare_rpc_projects_only_immutable_native_claim(tmp_path):
    context, control, entry = coordinator_entry_fixture(tmp_path)
    projected = native._native_payload("prepare",
        {"claim": entry["claim"], "window": control["window"]})
    assert set(projected["claim"]) == {"task"}
    assert set(projected["claim"]["task"]) == {"manifest", "manifest_sha256", "marker"}
    assert b"SENTINEL" not in native.canonical(projected)


@pytest.mark.parametrize("secret", ["lease_token", "refresh_token", "access_token",
    "private_key", "signature", "pending_event", "upload_plan", "raw_nonce"])
def test_secret_in_immutable_native_input_fails_closed_instead_of_transferring(tmp_path, secret):
    context, control, entry = coordinator_entry_fixture(tmp_path)
    entry["claim"]["task"]["manifest"]["inputs"] = {secret: "MUST_NOT_EGRESS"}
    with pytest.raises(native.NativeContractError, match="Coordinator credentials"):
        native._native_payload("observe", {"entry": entry})


@pytest.mark.parametrize("authorization", [False, None, 0, "yes"])
def test_cancel_requires_explicit_true_even_when_slurm_ignores_callback_return(authorization):
    class Contexts:
        def validate_claim(self, claim): return claim
    class Slurm:
        transport = None
        invoked = False
        def cancel(self, job, marker, *, authorize):
            authorize()  # Baseline Slurm intentionally ignores this return.
            self.invoked = True
    slurm = Slurm()
    executor = native.AccuracyNativeExecutor(slurm, Contexts())
    with pytest.raises(native.NativeContractError, match="no longer authoritative"):
        executor.cancel({"job_id": "123", "claim": {"task": {"marker": "safe"}}},
                        authorize=lambda: authorization)
    assert slurm.invoked is False


def publication_projection_fixture(tmp_path):
    root = tmp_path / "campaign/output"
    root.mkdir(parents=True)
    root.chmod(0o700)
    def descriptor(name, kind=None, content_type="application/json", raw=b"{}"):
        path = private(root / name, raw)
        result = {"file": name, "sha256": digest(raw), "size_bytes": len(raw),
                  "content_type": content_type}
        if kind is not None:
            result["kind"] = kind
        return result
    original = descriptor("posterior-part-000.npz", content_type="application/octet-stream", raw=b"NPZ")
    original.update(world_indices=[0, 1], coherent_rows=True)
    comparison = descriptor("comparison.json", "scientific_comparison")
    accounting = descriptor("allocation-accounting-final.json", "allocation_accounting_final")
    summary_descriptor = descriptor("display-summary.json", "display_summary")
    capsule_descriptor = descriptor("validation-capsule.json", "validation_capsule")
    without = [original, comparison, accounting, summary_descriptor]
    final = {"directory": str(root), "receipt_sha256": accounting["sha256"],
        "comparison": comparison, "accounting": accounting,
        "summary": summary_descriptor, "validation_capsule": capsule_descriptor,
        "artifacts": [*without, capsule_descriptor]}
    manifest = {key: key+"-binding" for key in ("campaign_instance_id",
        "scientific_request_hash", "transport_request_hash", "source_binding_sha256", "policy_sha256")}
    manifest.update(pins={"case_plan_sha256": "c"*64, "guide_case_set_sha256": "d"*64},
        reservation={"allocation_id": "allocated"}, attempt=1)
    binding = {key: manifest[key] for key in ("campaign_instance_id", "scientific_request_hash",
        "transport_request_hash", "source_binding_sha256", "policy_sha256")}
    binding.update(manifest["pins"])
    assessments = {method: {"verdict": {"observed_attainment": True,
        "population_qualified": True, "status": "attained"},
        "accuracy_target_within_budget": False, "within_method_ceiling": False,
        "within_factor_ceilings": True, "guide_status": "terminal_unmet"}
        for method in ("hnpe", "tmcmc")}
    snapshot = {"revision": 1, "allocations": []}
    ledger = {"snapshot": snapshot, "sha256": native.checksum(snapshot)}
    capsule = {"schema": "mesouq.benchmark_lab.accuracy_v2.validation_capsule.v1",
        "binding": binding, "comparison": comparison, "accounting": accounting,
        "summary": summary_descriptor, "final_snapshot_sha256": ledger["sha256"],
        "final_snapshot_revision": 1, "allocation_count": 0,
        "comparison_producer": {"allocation_id": "allocated", "attempt": 1, "slurm_job_id": "123"},
        "closed_numerical_prefix_preserved": True, "numerical_results_recomputed": False,
        "curation": {"excludes_validation_capsule_itself": True,
            "artifacts_sha256": native.checksum(without),
            "file_count": len(without), "size_bytes": sum(x["size_bytes"] for x in without)},
        "scientific_status": "honest_reproducible_comparison",
        "source_assessments": assessments, "confirmation_case_count": 256}
    summary = {"schema": "mesouq.benchmark_lab.accuracy_v2.display_summary.v1",
        "binding": deepcopy(binding), "comparison": comparison, "accounting": accounting,
        "numerical_statistics_recomputed": False,
        "downloads": [original, comparison, accounting],
        "scientific_status": capsule["scientific_status"], "methods": deepcopy(assessments),
        "evaluation": {"confirmation_case_count": 256}}
    context = {"maximum_artifact_files": 64, "maximum_artifact_bytes": 67_108_864,
        "maximum_total_artifact_bytes": 536_870_912,
        "allowed_artifacts": {"confirmation_compile": [a["file"] for a in final["artifacts"]]}}
    control = {"manifest": manifest, "context": context,
               "campaign_directory": str(tmp_path / "campaign")}
    return control, final, capsule, summary, ledger, root


def test_source_curation_preserves_extras_order_and_separate_accuracy_budget_claims(tmp_path):
    control, final, capsule, summary, ledger, root = publication_projection_fixture(tmp_path)
    native._validate_capsule_projection(control, final, capsule, summary, ledger, "123")
    specs, paths = native._curated_specs(control, root, final["artifacts"])
    assert len(specs) == len(final["artifacts"]) == len(paths)
    assert specs[0]["kind"] == "scientific_artifact"
    assert "world_indices" in summary["downloads"][0]
    assert "world_indices" not in specs[0]
    assert capsule["source_assessments"]["hnpe"]["verdict"]["population_qualified"] is True
    assert capsule["source_assessments"]["hnpe"]["accuracy_target_within_budget"] is False


@pytest.mark.parametrize("change", [
    lambda f,c,s,l: s["downloads"].reverse(),
    lambda f,c,s,l: f["artifacts"].reverse(),
    lambda f,c,s,l: c["comparison_producer"].update(slurm_job_id="124"),
    lambda f,c,s,l: c.update(final_snapshot_sha256="f"*64),
    lambda f,c,s,l: c.update(confirmation_case_count=255),
    lambda f,c,s,l: c["binding"].update(source_binding_sha256="foreign"),
    lambda f,c,s,l: s["methods"]["hnpe"].update(accuracy_target_within_budget=True),
    lambda f,c,s,l: c["curation"].update(excludes_validation_capsule_itself=False),
    lambda f,c,s,l: c["curation"].update(size_bytes=0)])
def test_source_publication_cannot_change_order_identity_denominator_or_assessments(tmp_path, change):
    control, final, capsule, summary, ledger, root = publication_projection_fixture(tmp_path)
    change(final, capsule, summary, ledger)
    with pytest.raises(native.NativeContractError):
        native._validate_capsule_projection(control, final, capsule, summary, ledger, "123")


def test_duplicate_curated_filename_fails_instead_of_truncation(tmp_path):
    control, final, capsule, summary, ledger, root = publication_projection_fixture(tmp_path)
    final["artifacts"].append(deepcopy(final["artifacts"][0]))
    with pytest.raises(native.NativeContractError, match="duplicate"):
        native._curated_specs(control, root, final["artifacts"])


@pytest.mark.parametrize("field,value", [("file", "../escape.json"),
    ("file", "unapproved.json"), ("content_type", "application/zip"), ("size_bytes", True)])
def test_curated_transport_rejects_unapproved_path_type_and_boolean_size(tmp_path, field, value):
    control, final, capsule, summary, ledger, root = publication_projection_fixture(tmp_path)
    final["artifacts"][0][field] = value
    with pytest.raises(native.NativeContractError):
        native._curated_specs(control, root, final["artifacts"])


def test_artifact_role_substitution_fails_before_json_is_loaded(tmp_path):
    control, final, capsule, summary, ledger, root = publication_projection_fixture(tmp_path)
    with pytest.raises(native.NativeContractError, match="substituted"):
        native._role_document(control, root, final["comparison"], "display-summary.json",
                              maximum=262_144)


def test_curated_count_and_total_caps_fail_explicitly(tmp_path):
    control, final, capsule, summary, ledger, root = publication_projection_fixture(tmp_path)
    control["context"]["maximum_artifact_files"] = 1
    with pytest.raises(native.NativeContractError, match="count"):
        native._curated_specs(control, root, final["artifacts"])
    control["context"]["maximum_artifact_files"] = 64
    control["context"]["maximum_total_artifact_bytes"] = 3
    with pytest.raises(native.NativeContractError, match="complete curated"):
        native._curated_specs(control, root, final["artifacts"])


@pytest.mark.parametrize("module", ["numpy", "numpy.linalg", "torch", "jax", "keras",
                                   "scipy", "matplotlib", "tensorflow"])
def test_postexit_guard_blocks_numerical_model_import_before_execution(module):
    finder = native._MetadataOnlyFinder()
    with pytest.raises(native.NativeContractError, match="forbidden"):
        finder.find_spec(module)


@pytest.mark.parametrize("path", ["/home/user/calculations", "/mnt/proj3/project/run",
                                  "/tmp/calculations", "/scratch/project/account/user"])
def test_new_native_work_refuses_nonassigned_scratch_roots(path):
    with pytest.raises(native.NativeContractError, match="SCRATCH"):
        native._require_scratch(path)


def test_allocated_supervision_uses_task_cwd_and_full_cache_policy():
    import ast
    tree = ast.parse(_SOURCE.read_text())
    allocated = next(n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == "_allocated")
    call = next(n for n in ast.walk(allocated) if isinstance(n, ast.Call)
        and isinstance(n.func, ast.Name) and n.func.id == "run_science")
    assert ast.unparse(call.args[1]) == "str(directory)"
    assert ast.unparse(call.args[1]) != "control['operator']['repository']"

def test_private_directory_creation_clears_only_new_inherited_sgid(tmp_path):
    parent = tmp_path / "assigned"
    parent.mkdir()
    parent.chmod(0o2770)
    child = parent / "private"
    native._mkdir(child)
    assert stat.S_IMODE(child.stat().st_mode) == 0o700
    assert stat.S_IMODE(parent.stat().st_mode) == 0o2770


def set_default_acl(path):
    import struct
    raw = struct.pack("<I", 2) + b"".join(
        struct.pack("<HHI", tag, permissions, 0xffffffff)
        for tag, permissions in ((1, 7), (4, 7), (32, 0)))
    os.setxattr(path, "system.posix_acl_default", raw)
    return raw


def test_mkdir_removes_only_new_inherited_acl_before_descendants(tmp_path):
    parent = tmp_path / "inheriting-parent"
    parent.mkdir()
    parent.chmod(0o700)
    original = set_default_acl(parent)
    leaf = parent / "new-private" / "child"
    native._mkdir(leaf)
    assert os.getxattr(parent, "system.posix_acl_default") == original
    for path in (leaf.parent, leaf):
        assert stat.S_IMODE(path.stat().st_mode) == 0o700
        for name in ("system.posix_acl_access", "system.posix_acl_default"):
            with pytest.raises(OSError) as error:
                os.getxattr(path, name)
            assert error.value.errno == native.errno.ENODATA
    previous = os.umask(0o077)
    try:
        import tempfile
        with tempfile.NamedTemporaryFile(dir=leaf) as file:
            assert stat.S_IMODE(os.fstat(file.fileno()).st_mode) == 0o600
    finally:
        os.umask(previous)


def test_mkdir_refuses_existing_private_leaf_with_default_acl(tmp_path):
    leaf = tmp_path / "existing-private"
    leaf.mkdir()
    leaf.chmod(0o700)
    original = set_default_acl(leaf)
    with pytest.raises(native.NativeContractError, match="inherited ACL"):
        native._mkdir(leaf)
    assert os.getxattr(leaf, "system.posix_acl_default") == original


def test_acl_unavailable_fails_closed(monkeypatch):
    def unknown(*args, **kwargs):
        raise OSError(native.errno.ENOTSUP, "Unsupported ACL observation")
    monkeypatch.setattr(native.os, "getxattr", unknown)
    with pytest.raises(native.NativeContractError, match="unavailable"):
        native._acl_absent(0, "system.posix_acl_default")


def test_isolated_dependency_snapshot_requires_site_disabled_interpreter(tmp_path):
    context, operator, manifest = executable_profile_fixture(tmp_path)
    with pytest.raises(native.NativeContractError, match="Python -I -S -B"):
        native.isolated_dependency_snapshot(manifest["spec"])


def test_dependency_v2_honest_site_free_prefix_and_raw_metadata(tmp_path):
    context, operator, manifest = executable_profile_fixture(tmp_path)
    assert manifest["schema"] == native.DEPENDENCY_SCHEMA
    python = manifest["observation"]["python"]
    assert python["site_disabled"] is True and python["isolated"] is True
    assert python["prefix"] == python["base_prefix"]
    assert python["requested_executable"] == sys.executable
    row = manifest["observation"]["distributions"][0]
    assert row["name"] == "benchmark-demo"
    assert row["metadata_sha256"] == digest(b"Name: benchmark-demo\r\nVersion: 1.0\r\n")
    assert row["record_sha256"] == digest(b"demo.py,,4\r\n")
    assert row["site_index"] == 0
    native._verify_profile(context, operator, allocated=True)


def test_dependency_provenance_origin_is_not_an_active_admission_dependency(tmp_path):
    context, operator, manifest = executable_profile_fixture(tmp_path)
    assert not Path(manifest["spec"]["overlays"][0]["origin"]).exists()
    native._verify_profile(context, operator, allocated=True)


def test_dependency_probe_never_executes_pth_or_ambient_pythonpath(tmp_path, monkeypatch):
    context, operator, manifest = executable_profile_fixture(tmp_path)
    site = Path(operator["dependency_overlays"][0])
    marker = tmp_path / "PTH_EXECUTED"
    private(site / "surprise.pth", ("import pathlib;pathlib.Path(" + repr(str(marker)) + ").touch()\n").encode())
    fallback = tmp_path / "ambient"
    private(fallback / "sitecustomize.py", b"raise RuntimeError('Ambient startup executed')\n")
    monkeypatch.setenv("PYTHONPATH", str(fallback))
    monkeypatch.setenv("PYTHONSTARTUP", str(fallback / "sitecustomize.py"))
    native._verify_profile(context, operator, allocated=True)
    assert not marker.exists()


@pytest.mark.parametrize("field", ["receipt", "normalization_receipt"])
def test_dependency_provenance_receipt_replacement_rejects(tmp_path, field):
    context, operator, manifest = executable_profile_fixture(tmp_path)
    private(Path(manifest["spec"]["snapshot"][field + "_path"]), b'{"changed":true}\n')
    with pytest.raises(native.NativeContractError, match="metadata verification failed"):
        native._verify_profile(context, operator, allocated=True)


def test_dependency_reviewed_site_replacement_rejects(tmp_path):
    context, operator, manifest = executable_profile_fixture(tmp_path)
    site = Path(operator["dependency_overlays"][0])
    old = site.with_name("retained-original")
    site.rename(old)
    import shutil
    shutil.copytree(old, site)
    for path in [site, *[x for x in site.rglob("*") if x.is_dir()]]:
        path.chmod(0o700)
    with pytest.raises(native.NativeContractError, match="differs from reviewed"):
        native._verify_profile(context, operator, allocated=True)


def test_dependency_portable_equivalence_keeps_shared_site_inode_and_modes(tmp_path):
    context, operator, manifest = executable_profile_fixture(tmp_path)
    changed = deepcopy(manifest)
    for row in changed["observation"]["overlays"]:
        row["device"] += 1
    for row in changed["observation"]["stdlib"]:
        row["device"] += 1
        row["inode"] += 1
    assert native._dependency_equivalent(changed, manifest)
    changed["observation"]["overlays"][0]["inode"] += 1
    assert not native._dependency_equivalent(changed, manifest)


def test_source_install_replaces_ambient_paths_and_sanitizes_child_environment(tmp_path, monkeypatch):
    context, operator = source_fixture(tmp_path)
    guard = native.SourceGuard(context, operator)
    guard.search_path = ["/reviewed/private/site", "/reviewed/stdlib"]
    for name in tuple(sys.modules):
        if name == "aws_mesouq" or name.startswith("aws_mesouq.") or name == "meso_uq" or name.startswith("meso_uq."):
            monkeypatch.delitem(sys.modules, name)
    monkeypatch.setattr(sys, "path", ["/stale/science", "/ambient/site"])
    monkeypatch.setattr(sys, "meta_path", list(sys.meta_path))
    monkeypatch.setenv("PYTHONHOME", "/stale/home")
    monkeypatch.setenv("PYTHONSTARTUP", "/stale/startup.py")
    monkeypatch.setenv("PYTHONUSERBASE", "/stale/userbase")
    monkeypatch.setenv("PYTHONPLATLIBDIR", "/stale/lib")
    monkeypatch.setenv("PYTHONPATH", "/stale/science")
    monkeypatch.setenv("PYTHONNOUSERSITE", "original")
    guard.install()
    expected = [str(guard.aws_root), str(guard.science_root), *guard.search_path]
    assert sys.path == expected
    assert os.environ["PYTHONPATH"] == os.pathsep.join(expected)
    assert os.environ["PYTHONNOUSERSITE"] == "1"
    assert all(key not in os.environ for key in
               ("PYTHONHOME", "PYTHONSTARTUP", "PYTHONUSERBASE", "PYTHONPLATLIBDIR"))


@pytest.mark.parametrize("change", ["duplicate", "overlap", "origin-active", "legacy-active",
                                     "unknown-field", "unknown-provenance"])
def test_dependency_spec_refuses_ambiguous_or_unsafe_search_roots(tmp_path, change):
    context, operator, manifest = executable_profile_fixture(tmp_path)
    spec = deepcopy(manifest["spec"])
    first = spec["overlays"][0]
    if change == "duplicate":
        spec["overlays"].append(deepcopy(first))
    elif change == "overlap":
        second = deepcopy(first)
        second["path"] += "/nested"
        spec["overlays"].append(second)
        spec["bootstrap"]["declared_overlay_origins"].append(second["origin"])
    elif change == "origin-active":
        first["path"] = first["origin"]
    elif change == "legacy-active":
        first["path"] = spec["bootstrap"]["excluded_source_roots"][0]
    elif change == "unknown-field":
        spec["caller_import_path"] = "/unchecked"
    else:
        spec["snapshot"]["unknown"] = True
    with pytest.raises(native.NativeContractError):
        native._dependency_spec(spec)


def test_dependency_operator_cannot_reorder_or_inject_sites(tmp_path):
    context, operator, manifest = executable_profile_fixture(tmp_path)
    operator["dependency_overlays"] = ["/injected/site", *operator["dependency_overlays"]]
    context["runtime_profile_sha256"] = native.checksum(operator)
    with pytest.raises(native.NativeContractError, match="reviewed order"):
        native._verify_profile(context, operator, allocated=True)


def test_effective_distribution_precedence_is_explicit_and_complete(tmp_path):
    sites = [tmp_path / "first", tmp_path / "second"]
    for i, site in enumerate(sites):
        private(site / ("demo-" + str(i+1) + ".dist-info/METADATA"),
                ("Name: demo\nVersion: " + str(i+1) + "\n").encode())
    rows = native._effective_distribution_metadata(["demo"], [str(x) for x in sites])
    assert rows[0]["version"] == "1" and rows[0]["site_index"] == 0
    assert rows[0]["record_sha256"] is None
    with pytest.raises(native.NativeContractError, match="inventory"):
        native._effective_distribution_metadata(["missing"], [str(x) for x in sites])


def test_dependency_directory_detects_reparented_same_leaf_symlink(tmp_path, monkeypatch):
    parent = tmp_path / "private"
    leaf = parent / "site"
    private(leaf / "marker")
    moved = tmp_path / "retained-private"
    original_open = native.os.open
    changed = []
    def racing_open(path, *args, **kwargs):
        result = original_open(path, *args, **kwargs)
        if path == "site" and kwargs.get("dir_fd") is not None and not changed:
            # Both old and fixed readers have opened this same leaf. Substitute
            # its ancestor before final checks while retaining the leaf inode.
            parent.rename(moved)
            parent.symlink_to(moved, target_is_directory=True)
            changed.append(True)
        return result
    monkeypatch.setattr(native.os, "open", racing_open)
    with pytest.raises(native.NativeContractError, match="ancestor changed"):
        native._dependency_directory(leaf, private=True)
    assert changed == [True]
    assert parent.is_symlink()
    assert (moved / "site/marker").read_bytes() == b"content"


def test_dependency_directory_rejects_inherited_acl_on_private_ancestor(tmp_path):
    parent = tmp_path / "private"
    leaf = parent / "site"
    private(leaf / "marker")
    set_default_acl(parent)
    with pytest.raises(native.NativeContractError, match="inherited ACL"):
        native._dependency_directory(leaf, private=True)


def real_protocol_native_control(tmp_path, monkeypatch, nonce):
    """Actual wire/protocol and prepare/load/main; stub only site/model boundaries."""
    from types import ModuleType
    from aws_mesouq import benchmark_accuracy_protocol as protocol
    request = protocol.wire.validate_create({
        "schema_version": 2, "idempotency_key": nonce,
        "scenario": {"family": "baseline", "scale": 1}, "seed": 1000,
        "controls": {
            "hnpe": {"width": 128, "depth": 2, "learning_rate": 0.0003,
                     "batch_size": 256, "max_updates": 1000000, "training_budget": 1800},
            "tmcmc": {"population_size": 4096, "target_cov": 0.6,
                      "proposal_scale": 1, "burn_in": 7, "max_generations": 1000}},
        "evaluation": {"cohort": "central_90", "guide_case_count": 8,
                       "confirmation_case_count": 256, "posterior_draws": 2048},
        "budget": {"mode": "accuracy_target", "method_gpu_seconds": 14400},
        "accuracy_target": {
            "profile_id": "w2-rms0p1-central90-2048-244of256-v1",
            "effort_policy_id": "bounded-effort-ladders-v1",
            "reference_profile_id": "independent-resolution-relative-reference-v1"}})
    operator = {
        "adapter_path": str(_SOURCE), "repository": str(tmp_path / "synthetic-science"),
        "python_bin": "/usr/bin/python3.11", "environment_script": "/synthetic/runtime.sh",
        "environment_sha256": "e"*64,
        "scheduler": {"partition": "qgpu", "account": "synthetic",
                      "qos": "synthetic", "mem": "4G"}}
    context = {
        "schema": protocol.CONTEXT_SCHEMA, "id": "0"*64, "enabled": True,
        "owner_sub": "synthetic-owner", "project_id": "synthetic-project",
        "site": "karolina", "science_revision": "f"*40,
        "source_binding_sha256": "d"*64, "adapter_sha256": digest(_SOURCE.read_bytes()),
        "policy_sha256": "e"*64, "runtime_profile_sha256": native.checksum(operator),
        "native_root": str(tmp_path / "native"), "archive_root": str(tmp_path / "archive"),
        "native_user": "synthetic-user", "native_cpus": 4, "native_gpus": 1,
        "maximum_artifact_bytes": 67_108_864, "maximum_total_artifact_bytes": 536_870_912,
        "maximum_artifact_files": 64,
        "capabilities": sorted(protocol.REQUIRED_CAPABILITIES),
        "allowed_artifacts": {"guide_preparation": []}}
    context["id"] = protocol.context_id(context)
    protocol.validate_context(context)
    scientific = protocol.wire.scientific_request_checksum(request)
    transport = protocol.wire.transport_request_checksum(request)
    allocation = {"allocation_id": "allocation-unicode", "attempt": 1,
        "method": "hnpe", "kind": "guide_preparation", "factor": None,
        "status": "claimed", "slurm_job_id": None, "allocated_gpu_seconds": None,
        "allocated_cpu_seconds": None, "reserved_gpu_seconds": 60, "not_submitted": False}
    accounting = {"schema": protocol.SNAPSHOT_SCHEMA,
        "campaign_instance_id": "a"*32, "scientific_request_hash": scientific,
        "transport_request_hash": transport, "source_binding_sha256": "d"*64,
        "case_plan_sha256": None, "guide_case_set_sha256": None,
        "revision": 0, "allocations": [allocation]}
    manifest = {"schema": protocol.MANIFEST_SCHEMA,
        "campaign_instance_id": "a"*32, "task_id": "t00000001-"+"b"*32,
        "attempt": 1, "kind": "guide_preparation", "method": "hnpe",
        "factor": None, "effort_target": None, "generation": 0,
        "expected_state_sha256": None, "request": request,
        "scientific_request_hash": scientific, "transport_request_hash": transport,
        "context_id": context["id"], "source_binding_sha256": "d"*64,
        "policy_sha256": "e"*64, "accounting": accounting,
        "input_ledger_sha256": protocol.checksum(accounting),
        "reservation": {"allocation_id": allocation["allocation_id"],
                        "gpu_seconds": 60, "native_cpus": 4, "native_gpus": 1},
        "pins": {key: None for key in protocol.PINS}, "inputs": {}}
    manifest = protocol.validate_manifest(manifest)
    expected = protocol.checksum(manifest)
    assert expected != native.checksum(manifest)  # Independent real-domain witness.
    claim = {"task": {"manifest": manifest, "manifest_sha256": expected,
                     "marker": "mqacc-"+manifest["campaign_instance_id"]+"-"+manifest["task_id"]}}
    class Guard:
        def install(self): return self
        def check(self): return None
    monkeypatch.setattr(native, "_verify_profile", lambda *a, **kw: Guard())
    # Placement/quota are exercised independently by their existing tests.
    monkeypatch.setattr(native, "_require_scratch", lambda path: Path(path))
    monkeypatch.setattr(native, "_home_preflight", lambda *a, **kw: parse_quota(quota_text()))
    clock_module = ModuleType("aws_mesouq.adapters.benchmark")
    clock_module.allocation_clock = lambda value: {
        "slurm_job_id": "123", "allocation_start_epoch": 1000,
        "allocation_deadline_epoch": 1060, "watchdog_deadline_epoch": 1055}
    clock_module.run_science = lambda *a, **kw: pytest.fail("No source/model subprocess permitted")
    monkeypatch.setitem(sys.modules, "aws_mesouq.adapters.benchmark", clock_module)
    reached = []
    monkeypatch.setattr(native, "_dispatch",
        lambda control, clock: reached.append(control["manifest"]["request"]["idempotency_key"]) or 0)
    window = {"allocated_seconds": 60, "internal_seconds": 60,
              "runtime_clock_policy": "allocation_deadline_v1", "time": "00:01:00"}
    preparation = native._prepare({"context": context, "operator": operator,
                                  "claim": claim, "window": window})
    path = Path(preparation["script_path"]).parent / "control.json"
    control = native._load_control(str(path))
    assert control["manifest_sha256"] == expected
    assert control["manifest"]["request"]["idempotency_key"] == nonce
    assert any(ord(ch) > 127 and ch.encode() in path.read_bytes() for ch in nonce)
    # Native file bytes retain UTF-8 canonical identity; protocol hashes differ.
    assert native.checksum(control) == digest(path.read_bytes())
    assert native.checksum(control) != protocol.checksum(control)
    assert nonce not in str(path)
    return protocol, manifest, control, path, reached


@pytest.mark.parametrize("nonce", ["résumé", "new\nλ", "🔬-reference"],
                         ids=["accent", "newline-unicode", "astral"])
def test_real_protocol_unicode_nonce_prepares_loads_and_reaches_native_entry(tmp_path, monkeypatch, nonce):
    protocol, manifest, control, path, reached = real_protocol_native_control(tmp_path, monkeypatch, nonce)
    assert native.main(["--execute-stage", str(path)]) == 0
    assert reached == [nonce]


def test_native_entry_rejects_wrong_utf8_variant_of_protocol_manifest_hash(tmp_path, monkeypatch):
    protocol, manifest, control, path, reached = real_protocol_native_control(tmp_path, monkeypatch, "référence")
    control["manifest_sha256"] = native.checksum(manifest)
    private(path, native.canonical(control))
    with pytest.raises(native.NativeContractError, match="manifest checksum changed"):
        native.main(["--execute-stage", str(path)])
    assert reached == []


def test_native_entry_keeps_real_wire_nonce_binding_check(tmp_path, monkeypatch):
    protocol, manifest, control, path, reached = real_protocol_native_control(tmp_path, monkeypatch, "étude")
    control["manifest"]["request"]["idempotency_key"] = "changed\nλ"
    private(path, native.canonical(control))
    with pytest.raises(ValueError, match="scientific/transport identity mismatch"):
        native.main(["--execute-stage", str(path)])
    assert reached == []
