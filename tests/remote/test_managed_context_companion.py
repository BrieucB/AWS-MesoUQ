"""Synthetic managed-child registry activation with unchanged operator/owner inputs."""
import argparse
import copy
import hashlib
import json
import os
from pathlib import Path
import runpy
from types import SimpleNamespace

import pytest

from aws_mesouq import adapters, benchmark_cli as cli, benchmark_dispatch, benchmark_worker
from aws_mesouq.benchmark_contexts import DEADLINE_POLICY, REGISTRY_ENV, REGISTRY_SCHEMA
from aws_mesouq.connector import api as api_module, transport as transport_module
from aws_mesouq.connector.auth import SessionExpired
from aws_mesouq.connector.benchmark_supervisor import source_binding
from aws_mesouq.connector.journal import Journal
from test_auth_broker import stage_package
from test_managed_startup_diagnostic import broker_directory, enrolled


@pytest.fixture
def managed(tmp_path, monkeypatch, enrolled, broker_directory):
    monkeypatch.delenv(REGISTRY_ENV, raising=False)
    operator = tmp_path / "operator.json"
    operator.write_text(json.dumps(enrolled.operator))
    operator.chmod(0o600)
    companion = Path(str(operator) + ".benchmark-context-registry.json")
    additional = copy.deepcopy(enrolled.operator)
    repository = tmp_path / "deadline-science"
    repository.mkdir(mode=0o700)
    additional.update(repository=str(repository), science_revision="b" * 40,
                      adapter_sha256=hashlib.sha256(
                          Path(adapters.__file__).with_name("benchmark.py").read_bytes()).hexdigest())
    document = {"schema": REGISTRY_SCHEMA, "legacy_operator": copy.deepcopy(enrolled.operator),
                "contexts": [{"operator": additional, "runtime_clock_policy": DEADLINE_POLICY}]}
    companion.write_text(json.dumps(document))
    companion.chmod(0o600)
    journal_path = tmp_path / "journal/journal.json"
    journal = Journal(journal_path.parent)
    try:
        with monkeypatch.context() as scoped:
            scoped.setenv(REGISTRY_ENV, str(companion))
            benchmark_worker.BenchmarkWorker(
                enrolled.api, enrolled.slurm, journal, project="p", operator=enrolled.operator)
    finally:
        journal.close()
    enrolled.store.state["benchmark_campaigns"].clear()
    monkeypatch.setattr(api_module, "API", lambda *_args: enrolled.api)
    monkeypatch.setattr(transport_module, "Transport", lambda **_kwargs: enrolled.cluster)
    argv = ["managed-worker", "--api-url", "https://api.example.invalid", "--project", "p",
            "--operator-config", str(operator), "--journal", str(journal_path.parent),
            "--local-cluster", "--once"]
    return SimpleNamespace(operator=operator, operator_bytes=operator.read_bytes(),
                           companion=companion, document=document, journal=journal_path,
                           before=journal_path.read_bytes(), argv=argv, enrolled=enrolled)


def assert_preserved(managed):
    before = json.loads(managed.before)
    after = json.loads(managed.journal.read_bytes())
    for key in ("archive_device", "connector_id", "owner_sub", "jobs"):
        assert after[key] == before[key]
    for key in ("context", "legacy_context", "contexts", "stages", "campaigns"):
        assert after["benchmark_lab"][key] == before["benchmark_lab"][key]
    assert managed.operator.read_bytes() == managed.operator_bytes
    assert managed.enrolled.cluster.submit_count == 0


def test_normal_main_loads_companion_for_existing_versioned_journal(managed):
    assert cli.main(managed.argv) == 0
    assert REGISTRY_ENV not in os.environ
    assert_preserved(managed)


def test_matching_ambient_registry_stays_unchanged(managed, monkeypatch):
    monkeypatch.setenv(REGISTRY_ENV, str(managed.companion))
    assert cli.main(managed.argv) == 0
    assert os.environ[REGISTRY_ENV] == str(managed.companion)
    assert_preserved(managed)


def test_absent_companion_preserves_existing_ambient_registry(managed, tmp_path, monkeypatch):
    ambient = tmp_path / "existing-contexts.json"
    managed.companion.rename(ambient)
    monkeypatch.setenv(REGISTRY_ENV, str(ambient))
    assert cli.main(managed.argv) == 0
    assert os.environ[REGISTRY_ENV] == str(ambient)
    assert_preserved(managed)


@pytest.mark.parametrize("ambient", [None, ""])
def test_absent_companion_does_not_guess_versioned_contexts(managed, monkeypatch, ambient):
    managed.companion.unlink()
    if ambient is not None:
        monkeypatch.setenv(REGISTRY_ENV, ambient)
    assert cli.main(managed.argv) == 2
    assert os.environ.get(REGISTRY_ENV) == ambient
    assert managed.journal.read_bytes() == managed.before
    assert managed.operator.read_bytes() == managed.operator_bytes


@pytest.mark.parametrize("ambient", [None, "", "existing-relative-setting"])
def test_absent_companion_preserves_relative_operator_behavior(tmp_path, monkeypatch, ambient):
    monkeypatch.chdir(tmp_path)
    if ambient is None:
        monkeypatch.delenv(REGISTRY_ENV, raising=False)
    else:
        monkeypatch.setenv(REGISTRY_ENV, ambient)
    with cli._legacy_context_registry(Path("operator.json")):
        assert os.environ.get(REGISTRY_ENV) == ambient
    assert os.environ.get(REGISTRY_ENV) == ambient


@pytest.mark.parametrize("ambient_kind", ["other", "relative", "symlink"])
def test_present_companion_rejects_conflicting_ambient_registry(
    managed, tmp_path, monkeypatch, ambient_kind
):
    ambient = tmp_path / "other-contexts.json"
    ambient.write_bytes(managed.companion.read_bytes())
    ambient.chmod(0o600)
    if ambient_kind == "relative":
        value = ambient.name
    elif ambient_kind == "symlink":
        alias = tmp_path / "context-alias.json"
        alias.symlink_to(managed.companion)
        value = str(alias)
    else:
        value = str(ambient)
    monkeypatch.setenv(REGISTRY_ENV, value)
    assert cli.main(managed.argv) == 2
    assert os.environ[REGISTRY_ENV] == value
    assert managed.journal.read_bytes() == managed.before
    assert managed.operator.read_bytes() == managed.operator_bytes


@pytest.mark.parametrize("unsafe", [
    "permissions", "symlink", "dangling_symlink", "directory", "fifo", "hardlink",
    "oversized", "malformed", "wrong_legacy", "empty_contexts",
])
@pytest.mark.parametrize("versioned", [False, True])
def test_present_invalid_companion_fails_closed_before_namespace_save(
    managed, tmp_path, unsafe, versioned
):
    if not versioned:
        data = json.loads(managed.before)
        data["benchmark_lab"] = {"context": data["benchmark_lab"]["legacy_context"],
                                 "stages": {}, "campaigns": {}}
        managed.journal.write_text(json.dumps(data))
        managed.before = managed.journal.read_bytes()
    if unsafe == "permissions":
        managed.companion.chmod(0o640)
    elif unsafe in ("symlink", "dangling_symlink"):
        target = tmp_path / "target-contexts.json"
        if unsafe == "symlink":
            target.write_bytes(managed.companion.read_bytes())
            target.chmod(0o600)
        managed.companion.unlink()
        managed.companion.symlink_to(target)
    elif unsafe == "directory":
        managed.companion.unlink()
        managed.companion.mkdir(mode=0o700)
    elif unsafe == "fifo":
        managed.companion.unlink()
        os.mkfifo(managed.companion, 0o600)
    elif unsafe == "hardlink":
        os.link(managed.companion, tmp_path / "context-hardlink.json")
    elif unsafe == "oversized":
        managed.companion.write_bytes(b" " * (256 * 1024 + 1))
    elif unsafe == "malformed":
        managed.companion.write_text("{")
    else:
        document = copy.deepcopy(managed.document)
        if unsafe == "wrong_legacy":
            document["legacy_operator"]["python_bin"] = "/other/python"
        else:
            document["contexts"] = []
        managed.companion.write_text(json.dumps(document))
    assert cli.main(managed.argv) == 2
    assert REGISTRY_ENV not in os.environ
    assert managed.journal.read_bytes() == managed.before
    assert managed.operator.read_bytes() == managed.operator_bytes
    assert managed.enrolled.cluster.submit_count == 0


def test_present_companion_requires_canonical_operator_path(managed, tmp_path):
    alias = tmp_path / "operator-directory-alias"
    alias.symlink_to(tmp_path, target_is_directory=True)
    argv = list(managed.argv)
    argv[argv.index("--operator-config") + 1] = str(alias / managed.operator.name)
    assert cli.main(argv) == 2
    assert REGISTRY_ENV not in os.environ
    assert managed.journal.read_bytes() == managed.before


@pytest.mark.parametrize("failure,expected", [
    (RuntimeError("synthetic"), 2), (SessionExpired("synthetic"), 3),
    (KeyboardInterrupt(), None), (SystemExit(7), None),
])
def test_constructor_failures_restore_environment_and_existing_exit_contract(
    managed, monkeypatch, failure, expected
):
    observed = []
    def fail(*_args, **_kwargs):
        observed.append(os.environ.get(REGISTRY_ENV))
        raise failure
    monkeypatch.setattr(benchmark_worker, "BenchmarkWorker", fail)
    parser = argparse.ArgumentParser()
    cli._runtime_parser(parser)
    args = parser.parse_args(managed.argv[1:])
    if expected is None:
        with pytest.raises(type(failure)):
            cli.run_managed_worker(args)
    else:
        assert cli.main(managed.argv) == expected
    assert observed == [str(managed.companion)]
    assert REGISTRY_ENV not in os.environ
    assert managed.journal.read_bytes() == managed.before
    assert managed.enrolled.cluster._closed is True


@pytest.mark.parametrize("expired", [False, True])
def test_real_api_failure_restores_environment_and_auth_exit(
    managed, monkeypatch, expired
):
    from aws_mesouq.connector.api import APIError

    observed = []
    class DeniedAPI:
        base = "https://api.example.invalid"
        def request(self, method, path, payload=None):
            observed.append((method, path, os.environ.get(REGISTRY_ENV)))
            if expired:
                raise SessionExpired("synthetic")
            raise APIError(403, "synthetic", "synthetic")
    monkeypatch.setattr(api_module, "API", lambda *_args: DeniedAPI())
    assert cli.main(managed.argv) == (3 if expired else 2)
    assert observed == [("GET", "/v1/me", str(managed.companion))]
    assert REGISTRY_ENV not in os.environ
    assert managed.journal.read_bytes() == managed.before
    assert managed.enrolled.cluster._closed is True


def test_accuracy_initialization_and_polling_see_original_environment(managed, monkeypatch, tmp_path):
    observed = []
    class Worker:
        def sweep(self):
            observed.append(("polling", os.environ.get(REGISTRY_ENV)))
    def accuracy(*_args, **_kwargs):
        observed.append(("accuracy", os.environ.get(REGISTRY_ENV)))
        return object()
    monkeypatch.setattr(benchmark_dispatch, "build_accuracy_worker", accuracy)
    monkeypatch.setattr(benchmark_dispatch, "FairBenchmarkWorker", lambda *_a, **_k: Worker())
    argv = managed.argv + ["--worker-kind", "benchmark-all",
                           "--accuracy-contexts", str(tmp_path / "accuracy.json")]
    assert cli.main(argv) == 0
    assert observed == [("accuracy", None), ("polling", None)]
    assert REGISTRY_ENV not in os.environ
    assert_preserved(managed)


def test_existing_owner_loader_accepts_new_package_with_unchanged_runtime_arguments(
    managed, tmp_path
):
    package, original_manifest = stage_package(tmp_path)
    hashes = original_manifest["files"]
    entrypoint = cli.WORKER_ENTRYPOINTS["benchmark-all"]
    binding = source_binding(entrypoint, hashes)
    manifest = {"schema": original_manifest["schema"], "entrypoint": entrypoint,
                "files": hashes, "binding_sha256": binding}
    manifest_path = tmp_path / "worker-manifest.json"
    manifest_path.write_text(json.dumps(manifest))
    manifest_path.chmod(0o600)
    parser = argparse.ArgumentParser()
    cli._runtime_parser(parser)
    args = parser.parse_args(managed.argv[1:] + [
        "--worker-kind", "benchmark-all", "--accuracy-contexts", str(tmp_path / "accuracy.json")])
    runtime = cli._runtime_arguments(args)
    assert runtime == (
        "managed-worker", "--api-url", "https://api.example.invalid", "--project", "p",
        "--operator-config", str(managed.operator), "--journal", str(managed.journal.parent),
        "--poll-seconds", "20", "--worker-kind", "benchmark-all",
        "--accuracy-contexts", str(tmp_path / "accuracy.json"), "--local-cluster", "--once")
    specification = cli.approved_worker(
        package, manifest_path, binding, runtime, kind="benchmark-all")
    assert specification.arguments == runtime
    assert dict(specification.files)["aws_mesouq/benchmark_cli.py"] == hashlib.sha256(
        Path(cli.__file__).read_bytes()).hexdigest()


def test_recovery_cli_candidate_install_and_managed_main_preserve_full_namespace(
    managed, tmp_path, monkeypatch, capsys
):
    source = Path(cli.__file__).resolve().parents[2]
    data = json.loads(managed.before)
    namespace = data["benchmark_lab"]
    context_id, definition = next(
        (key, value) for key, value in namespace["contexts"].items()
        if value["runtime_clock_policy"] == DEADLINE_POLICY)
    frozen = definition["operator"]
    campaign = {"id": "recovered-campaign", "project_id": "p",
                "owner_sub": data["owner_sub"], "approved_adapter": {
                    **{key: frozen[key] for key in
                       ("science_revision", "adapter_sha256", "archive_root")},
                    "execution_context_id": context_id}}
    namespace["stages"]["retained-stage"] = {
        "claim": {"campaign": campaign}, "context_id": context_id,
        "history": ["SYNTHETIC_RETAINED_STAGE_HISTORY"]}
    namespace["campaigns"][campaign["id"]] = {
        "root": frozen["run_root"] + "/" + campaign["id"], "context_id": context_id,
        "archive_event_id": "SYNTHETIC_RETAINED_ARCHIVE"}
    data["history"] = ["SYNTHETIC_RETAINED_GLOBAL_HISTORY"]
    managed.journal.write_text(json.dumps(data))
    managed.before = managed.journal.read_bytes()
    managed.companion.unlink()
    settings = {"schema": "aws_mesouq.private_v1_registry_recovery.v1",
                "sdk_root": str(source / "src"), "journal_path": str(managed.journal),
                "legacy_operator_path": str(managed.operator), "project": "p",
                "output_directory": str(tmp_path / "recovered")}
    settings_path = tmp_path / "recovery-settings.json"
    settings_path.write_text(json.dumps(settings))
    settings_path.chmod(0o600)
    recovery = runpy.run_path(str(source / "tools/recover_benchmark_context_registry.py"))
    assert recovery["main"](["--settings", str(settings_path)]) == 0
    output = capsys.readouterr()
    assert str(managed.journal) not in output.out
    assert "SYNTHETIC_RETAINED_" not in output.out + output.err
    assert managed.journal.read_bytes() == managed.before
    receipt = json.loads(
        (tmp_path / "recovered/installation-receipt.private.json").read_text())
    assert receipt["actual_companion_installed"] is False
    assert receipt["complete_context_map_equal"] is True
    assert receipt["companion_installation_path"] == str(managed.companion)
    assert receipt["original_operator_sha256"] == hashlib.sha256(managed.operator_bytes).hexdigest()
    candidate = Path(receipt["registry_path"]).read_bytes()
    assert hashlib.sha256(candidate).hexdigest() == receipt["registry_sha256"]
    fd = os.open(managed.companion, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, "wb") as stream:
        stream.write(candidate)
    observed = []
    monkeypatch.setattr(benchmark_worker.BenchmarkWorker, "sweep",
                        lambda worker: observed.append(copy.deepcopy(worker.state)))
    assert cli.main(managed.argv) == 0
    assert observed == [namespace]
    assert REGISTRY_ENV not in os.environ
    after = json.loads(managed.journal.read_bytes())
    assert after["benchmark_lab"] == namespace
    assert after["history"] == data["history"]
    assert_preserved(managed)
