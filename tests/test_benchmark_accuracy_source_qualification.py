"""Synthetic source-derived admission and legacy-context recovery; no actual inputs."""
from copy import deepcopy
from types import ModuleType, SimpleNamespace
import sys

import pytest

from aws_mesouq import benchmark_accuracy_protocol as p
from aws_mesouq.benchmark_accuracy_contexts import AccuracyExecutionContexts, REGISTRY_SCHEMA
import importlib.util
from pathlib import Path


def fixture_module(name, filename):
    spec = importlib.util.spec_from_file_location(name, Path(__file__).with_name(filename))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


profile = fixture_module("source_qualification_context_fixture", "test_benchmark_accuracy_contexts_optimization.py").profile
adapter_fixture = fixture_module("source_qualification_native_fixture", "test_benchmark_accuracy_adapter.py")
native = adapter_fixture.native
real_protocol_native_control = adapter_fixture.real_protocol_native_control


def record(pin="d"*64):
    return {"schema": p.SOURCE_QUALIFICATION_SCHEMA, "science_revision": "a"*40,
        "source_binding_sha256": "b"*64, "semantic_policy_sha256": pin}


def registry(*, qualified=True, enabled=True, semantic="d"*64):
    context, operator = profile()
    context["enabled"] = enabled
    if qualified:
        context["source_qualification"] = record(semantic)
    context["runtime_profile_sha256"] = p.checksum(operator)
    context["id"] = p.context_id(context)
    return {"schema": REGISTRY_SCHEMA, "contexts": [{"context": context, "operator": operator}]}, context


class Guard:
    def __init__(self, context, *, installed=True):
        self.context, self.installed, self.checks = context, installed, 0
    def check(self):
        self.checks += 1


def scientific_policy(monkeypatch, pin):
    package = ModuleType("meso_uq.benchmark_lab.accuracy_v2")
    module = ModuleType(package.__name__ + ".policy")
    module.policy_fingerprint = lambda: pin
    package.policy = module
    for name in ("meso_uq", "meso_uq.benchmark_lab"):
        parent = ModuleType(name)
        parent.__path__ = []
        monkeypatch.setitem(sys.modules, name, parent)
    monkeypatch.setitem(sys.modules, package.__name__, package)
    monkeypatch.setitem(sys.modules, module.__name__, module)
    return module


@pytest.mark.parametrize("qualified,enabled,expected", [
    (False, True, False), (False, False, False), (True, False, False), (True, True, True)])
def test_only_new_work_is_filtered_without_rewriting_legacy_id(qualified, enabled, expected):
    doc, context = registry(qualified=qualified, enabled=enabled)
    before = deepcopy(doc)
    contexts = AccuracyExecutionContexts(doc, project="project", owner_sub="owner")
    assert contexts.context(context["id"]) == context
    assert doc == before
    assert (context["id"] in contexts.supported()) is expected
    if expected:
        assert contexts.require_new_work(context["id"]) == context
    else:
        with pytest.raises(ValueError):
            contexts.require_new_work(context["id"])
    if not qualified:
        assert "source_qualification" not in contexts.context(context["id"])


def test_bad_historical_context_does_not_block_good_admission():
    doc, old = registry(qualified=False)
    good_doc, good = registry()
    doc["contexts"].extend(good_doc["contexts"])
    contexts = AccuracyExecutionContexts(doc, project="project", owner_sub="owner")
    assert contexts.supported() == [good["id"]]
    executor = native.AccuracyNativeExecutor(SimpleNamespace(transport=None), contexts)
    calls = []
    executor._rpc = lambda identity, operation, **kw: calls.append((identity, operation)) or {"verified": True}
    assert executor.admitted_contexts() == [good["id"]]
    assert calls == [(good["id"], "verify_profile")]


@pytest.mark.parametrize("field,value", [
    ("science_revision", "f"*40), ("source_binding_sha256", "f"*64),
    ("semantic_policy_sha256", "f"*64)])
def test_well_formed_but_unqualified_record_is_new_work_ineligible(field, value):
    doc, context = registry()
    context["source_qualification"][field] = value
    context["id"] = p.context_id(context)
    contexts = AccuracyExecutionContexts(doc, project="project", owner_sub="owner")
    assert contexts.supported() == []
    assert contexts.context(context["id"]) == context


def test_qualification_computes_normal_source_policy_instead_of_copying_context(monkeypatch):
    _, context = registry(qualified=False)
    scientific_policy(monkeypatch, "e"*64)
    guard = Guard(context)
    result = native._source_qualification(context, guard)
    assert result == record("e"*64)
    assert result["semantic_policy_sha256"] != context["policy_sha256"]
    assert guard.checks == 2
    assert "source_qualification" not in context


@pytest.mark.parametrize("installed,same_context", [(False, True), (True, False)])
def test_qualification_requires_exact_installed_guard(monkeypatch, installed, same_context):
    _, context = registry()
    guard = Guard(context if same_context else {}, installed=installed)
    scientific_policy(monkeypatch, "d"*64)
    with pytest.raises(native.NativeContractError):
        native._source_qualification(context, guard)
    assert guard.checks == 0


def test_policy_source_change_during_computation_rejects(monkeypatch):
    _, context = registry()
    guard = Guard(context)
    module = scientific_policy(monkeypatch, "d"*64)
    def changed():
        guard.context = {}
        def reject():
            raise native.NativeContractError("synthetic authenticated source changed")
        guard.check = reject
        return "d"*64
    module.policy_fingerprint = changed
    with pytest.raises(native.NativeContractError):
        native._source_qualification(context, guard)


@pytest.mark.parametrize("qualified,actual,success", [
    (False, "d"*64, False), (True, "e"*64, False), (True, "d"*64, True)])
def test_native_new_work_requires_record_and_actual_source_agreement(monkeypatch, qualified, actual, success):
    _, context = registry(qualified=qualified)
    scientific_policy(monkeypatch, actual)
    if success:
        assert native._require_new_work_qualification(context, Guard(context)) is None
    else:
        with pytest.raises(native.NativeContractError):
            native._require_new_work_qualification(context, Guard(context))


def test_executor_generation_authenticates_tuple_but_does_not_rewrite_policy():
    doc, context = registry(qualified=False)
    contexts = AccuracyExecutionContexts(doc, project="project", owner_sub="owner")
    executor = native.AccuracyNativeExecutor(SimpleNamespace(transport=None), contexts)
    calls = []
    executor._rpc = lambda identity, operation, **kw: calls.append((identity, operation, kw)) or record("e"*64)
    assert executor.qualify_source(context["id"]) == record("e"*64)
    assert calls == [(context["id"], "qualify_source", {})]
    assert contexts.context(context["id"])["policy_sha256"] == "d"*64
    executor._rpc = lambda *a, **kw: {**record(), "science_revision": "f"*40}
    with pytest.raises(native.NativeContractError):
        executor.qualify_source(context["id"])


@pytest.mark.parametrize("operation", ["observe", "publication", "read_artifact"])
def test_legacy_rpc_routes_do_not_call_new_work_qualification(monkeypatch, operation):
    class LegacyGuard:
        def check(self): pass
        def close(self): pass
    monkeypatch.setattr(native, "_verify_profile", lambda *a, **kw: LegacyGuard())
    monkeypatch.setattr(native, "_require_new_work_qualification",
        lambda *a: pytest.fail("Legacy recovery was admission gated"))
    monkeypatch.setattr(native, "_observe", lambda payload: {"legacy": True})
    monkeypatch.setattr(native, "_publication", lambda payload, guard: {"legacy": True})
    monkeypatch.setattr(native, "_read_artifact", lambda payload: b"legacy")
    result = native.remote_request({"operation": operation, "context": {"enabled": False}, "operator": {}})
    assert result in ({"legacy": True}, b"legacy")


def test_prepare_rejects_record_source_mismatch_before_effects(tmp_path, monkeypatch):
    _, manifest, control, path, reached = real_protocol_native_control(
        tmp_path, monkeypatch, "qualification-λ")
    context = deepcopy(control["context"])
    context["source_qualification"]["semantic_policy_sha256"] = "f"*64
    context["id"] = p.context_id(context)
    manifest["context_id"] = context["id"]
    claim = {"task": {"manifest": manifest, "manifest_sha256": p.checksum(manifest),
        "marker": control["marker"]}}
    class PreparedGuard(Guard):
        def install(self): return self
        def close(self): pass
    monkeypatch.setattr(native, "_verify_profile", lambda *a, **kw: PreparedGuard(context))
    scientific_policy(monkeypatch, "e"*64)
    monkeypatch.setattr(native, "_mkdir", lambda *a: pytest.fail("Unqualified prepare wrote payload"))
    monkeypatch.setattr(native, "_home_preflight", lambda *a, **kw: pytest.fail("Unqualified prepare reached quota"))
    with pytest.raises(native.NativeContractError):
        native._prepare({"context": context, "operator": control["operator"],
            "claim": claim, "window": control["window"]})


def test_actual_source_mismatch_filters_one_context_but_admits_next(monkeypatch):
    doc, first = registry()
    second = deepcopy(first)
    second["native_root"] = "/scratch/another-native"
    second["archive_root"] = "/project/another-archive"
    second["id"] = p.context_id(second)
    doc["contexts"].append({"context": second, "operator": deepcopy(doc["contexts"][0]["operator"])})
    contexts = AccuracyExecutionContexts(doc, project="project", owner_sub="owner")
    executor = native.AccuracyNativeExecutor(SimpleNamespace(transport=None), contexts)
    scientific_policy(monkeypatch, "d"*64)
    calls = []
    def rpc(identity, operation):
        calls.append(identity)
        current = contexts.context(identity)
        if identity == first["id"]:
            scientific_policy(monkeypatch, "e"*64)
        else:
            scientific_policy(monkeypatch, "d"*64)
        guard = Guard(current)
        try:
            native._require_new_work_qualification(current, guard)
        except native.NativeContractError:
            return {"verified": False}
        return {"verified": True}
    executor._rpc = rpc
    assert executor.admitted_contexts() == [second["id"]]
    assert set(calls) == {first["id"], second["id"]}
