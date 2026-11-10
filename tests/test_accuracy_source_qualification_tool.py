"""Source-only qualification generation contract, with a synthetic native RPC."""
from copy import deepcopy
import importlib.util
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("qualify_accuracy_source", ROOT / "tools/qualify_accuracy_source.py")
tool = importlib.util.module_from_spec(spec)
spec.loader.exec_module(tool)


def context():
    return {
        "schema": tool.protocol.CONTEXT_SCHEMA, "id": "0" * 64, "enabled": False,
        "owner_sub": "alice", "project_id": "p", "site": "karolina",
        "science_revision": "a" * 40, "source_binding_sha256": "b" * 64,
        "adapter_sha256": "c" * 64, "policy_sha256": "d" * 64,
        "runtime_profile_sha256": "e" * 64, "native_root": "/scratch/test-native",
        "archive_root": "/project/test-archive", "native_user": "tester",
        "native_cpus": 16, "native_gpus": 1, "maximum_artifact_bytes": 10,
        "maximum_total_artifact_bytes": 100, "maximum_artifact_files": 4,
        "capabilities": sorted(tool.protocol.REQUIRED_CAPABILITIES), "allowed_artifacts": {},
    }


def fixture():
    value = context()
    value["id"] = tool.protocol.context_id(value)
    record = {"schema": tool.protocol.SOURCE_QUALIFICATION_SCHEMA,
              "science_revision": "a" * 40, "source_binding_sha256": "b" * 64,
              "semantic_policy_sha256": "d" * 64}
    calls = []
    def source_rpc(identifier):
        calls.append(identifier)
        return deepcopy(record)
    contexts = SimpleNamespace(context=lambda identifier: deepcopy(value)
        if identifier == value["id"] else (_ for _ in ()).throw(ValueError()))
    return value, record, calls, contexts, SimpleNamespace(qualify_source=source_rpc)


def test_record_is_native_generated_and_old_context_not_rewritten_or_enabled():
    value, record, calls, contexts, native = fixture()
    original = deepcopy(value)
    result = tool.qualify(contexts, value["id"], native)
    assert result["qualification"] == record
    assert calls == [value["id"]]
    assert value == original and not value["enabled"]
    assert "source_qualification" not in value
    assert result["qualified_context_id"] != value["id"]
    expected = dict(value, source_qualification=record)
    assert result["qualified_context_id"] == tool.protocol.context_id(expected)


@pytest.mark.parametrize("field", ["science_revision", "source_binding_sha256", "semantic_policy_sha256"])
def test_wrong_rpc_tuple_or_source_semantic_pin_rejected(field):
    value, record, calls, contexts, native = fixture()
    record[field] = "0" * (40 if field == "science_revision" else 64)
    with pytest.raises(ValueError):
        tool.qualify(contexts, value["id"], native)
    assert "source_qualification" not in value


def test_no_arbitrary_expected_hash_argument_and_closed_record():
    value, record, calls, contexts, native = fixture()
    with pytest.raises(TypeError):
        tool.qualify(contexts, value["id"], native, expected_hash="d" * 64)
    record["expected_hash"] = "d" * 64
    with pytest.raises(ValueError):
        tool.qualify(contexts, value["id"], native)


def test_local_transport_never_opens_ssh_and_uses_bounded_rpc(monkeypatch):
    observed = []
    def run(argv, **kwargs):
        observed.append((argv, kwargs))
        return SimpleNamespace(stdout=b"{}")
    monkeypatch.setattr(tool.subprocess, "run", run)
    assert tool.LocalNativeTransport().run_bytes(["python", "-I", "-S", "-B", "-c", "pass"], data=b"{}") == b"{}"
    assert observed[0][1] == dict(input=b"{}", capture_output=True, check=True, timeout=60)


def test_cli_private_receipt_no_pin_output_and_no_overwrite(tmp_path, monkeypatch, capsys):
    tmp_path = tmp_path.resolve()
    tmp_path.chmod(0o700)
    value, record, calls, contexts, native = fixture()
    monkeypatch.setattr(tool, "read_registry", lambda path: {})
    monkeypatch.setattr(tool, "AccuracyExecutionContexts", lambda *a, **kw: contexts)
    monkeypatch.setattr(tool, "AccuracyNativeExecutor", lambda *a, **kw: native)
    output = tmp_path / "qualification.private.json"
    args = ["--registry", str(tmp_path / "synthetic-registry.json"), "--project", "p",
            "--owner-sub", "alice", "--context-id", value["id"], "--output", str(output)]
    assert tool.main(args) == 0
    import json, stat
    assert json.loads(capsys.readouterr().out) == {"status": "qualified"}
    result = json.loads(output.read_bytes())
    assert result["qualification"] == record
    assert stat.S_IMODE(output.stat().st_mode) == 0o600
    before = output.read_bytes()
    assert tool.main(args) == 2
    assert json.loads(capsys.readouterr().out) == {
        "status": "rejected", "error_code": "source_qualification_failed"}
    assert output.read_bytes() == before


def test_native_error_never_exports_arbitrary_messages(tmp_path, monkeypatch, capsys):
    tmp_path = tmp_path.resolve()
    tmp_path.chmod(0o700)
    value, record, calls, contexts, native = fixture()
    def fail(_identifier):
        raise type("private-canary-class", (Exception,), {})("private-canary-message")
    native.qualify_source = fail
    monkeypatch.setattr(tool, "read_registry", lambda path: {})
    monkeypatch.setattr(tool, "AccuracyExecutionContexts", lambda *a, **kw: contexts)
    monkeypatch.setattr(tool, "AccuracyNativeExecutor", lambda *a, **kw: native)
    assert tool.main(["--registry", str(tmp_path / "r"), "--project", "p",
        "--owner-sub", "alice", "--context-id", value["id"],
        "--output", str(tmp_path / "out")]) == 2
    captured = capsys.readouterr()
    assert "private-canary" not in captured.out + captured.err
    assert not (tmp_path / "out").exists()


def test_unknown_cli_option_never_echoes_private_value():
    import json
    import subprocess
    import sys
    result = subprocess.run([sys.executable, "-I", "-S", "-B",
        str(ROOT / "tools/qualify_accuracy_source.py"), "--expected-hash",
        "private-cli-canary"], capture_output=True, text=True, timeout=10)
    assert result.returncode == 2
    assert result.stderr == ""
    assert json.loads(result.stdout) == {
        "status": "rejected", "error_code": "source_qualification_failed"}
    assert "private-cli-canary" not in result.stdout
