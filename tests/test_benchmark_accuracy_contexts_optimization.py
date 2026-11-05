"""Exact opt-in execution profiles for factor and source-placement optimizations."""
from copy import deepcopy

import pytest
from aws_mesouq import benchmark_accuracy_protocol as protocol
from aws_mesouq.benchmark_accuracy_contexts import validate_operator


def profile():
    context = {
        "schema": protocol.CONTEXT_SCHEMA, "id": "0" * 64, "enabled": True,
        "owner_sub": "owner", "project_id": "project", "site": "karolina",
        "science_revision": "a" * 40, "source_binding_sha256": "b" * 64,
        "adapter_sha256": "c" * 64, "policy_sha256": "d" * 64,
        "runtime_profile_sha256": "0" * 64, "native_root": "/scratch/native",
        "archive_root": "/project/archive", "native_user": "user",
        "native_cpus": 16, "native_gpus": 1, "maximum_artifact_bytes": 1000,
        "maximum_total_artifact_bytes": 10000, "maximum_artifact_files": 8,
        "capabilities": sorted(protocol.REQUIRED_CAPABILITIES | {
            "cpu_reference_resources_v1", "verified_source_staging_v1"}),
        "reference_resources": {"native_cpus": 8, "native_gpus": 0},
        "allowed_artifacts": {},
    }
    sources = {"/aws-src/aws_mesouq/" + name: "c" * 64 for name in (
        "__init__.py", "benchmark.py", "benchmark_accuracy.py", "benchmark_accuracy_protocol.py",
        "adapters/__init__.py", "adapters/benchmark.py", "adapters/benchmark_accuracy.py",
        "source_staging.py")}
    sources.update({"/science/src/meso_uq/" + name: "e" * 64 for name in (
        "__init__.py", "benchmark_lab/__init__.py", "benchmark_lab/accuracy_v2/__init__.py")})
    operator = {
        "repository": "/science", "python_bin": "/runtime/bin/python", "python_sha256": "f" * 64,
        "adapter_path": "/aws-src/aws_mesouq/adapters/benchmark_accuracy.py",
        "environment_script": "/runtime/env.sh", "environment_sha256": "a" * 64,
        "dependency_manifest_path": "/runtime/dependencies.json", "dependency_manifest_sha256": "b" * 64,
        "native_python_sources": sources, "expected_host": "login4.karolina.it4i.cz",
        "scheduler": {"partition": "qgpu", "account": "project", "qos": "normal", "mem": "16G"},
        "reference_scheduler": {"partition": "qcpu", "account": "project", "qos": "normal", "mem": "16G"},
        "allowed_upload_origins": ["https://private.example"],
        "source_staging": {"root": "/tmp/mesouq-source", "max_bytes": 10000000, "max_namespaces": 4},
        "native_package_resources": {"/science/src/meso_uq/remote/README.md": "1" * 64},
    }
    return context, operator


def validate(context, operator):
    context["runtime_profile_sha256"] = protocol.checksum(operator)
    context["id"] = protocol.context_id(context)
    protocol.validate_context(context)
    return validate_operator(operator, context)


def test_explicit_cpu_and_staging_profiles_are_hash_bound():
    context, operator = profile()
    assert validate(context, operator) == operator
    assert protocol.task_resources(context, "guide_reference") == {"native_cpus": 8, "native_gpus": 0}
    assert protocol.task_resources(context, "hnpe_advance") == {"native_cpus": 16, "native_gpus": 1}
    altered = deepcopy(operator)
    altered["reference_scheduler"]["account"] = "different"
    with pytest.raises(ValueError, match="checksum"):
        validate_operator(altered, context)


@pytest.mark.parametrize("change", [
    lambda c, o: c["capabilities"].remove("cpu_reference_resources_v1"),
    lambda c, o: c.pop("reference_resources"),
    lambda c, o: c["reference_resources"].update(native_cpus=32),
    lambda c, o: c["reference_resources"].update(native_gpus=1),
    lambda c, o: o.pop("reference_scheduler"),
    lambda c, o: o["reference_scheduler"].update(partition="unsafe partition"),
])
def test_cpu_reference_profile_requires_complete_approved_bounds(change):
    context, operator = profile()
    change(context, operator)
    with pytest.raises(ValueError):
        validate(context, operator)


@pytest.mark.parametrize("change", [
    lambda c, o: c["capabilities"].remove("verified_source_staging_v1"),
    lambda c, o: o.pop("source_staging"),
    lambda c, o: o.pop("native_package_resources"),
    lambda c, o: o["source_staging"].update(root="/science/src/staged"),
    lambda c, o: o["source_staging"].update(max_namespaces=0),
    lambda c, o: o["source_staging"].update(max_bytes=True),
    lambda c, o: o["native_package_resources"].update({"/outside/data.csv": "a" * 64}),
    lambda c, o: o["native_package_resources"].update({"/science/src/meso_uq/injected.py": "a" * 64}),
    lambda c, o: o["native_python_sources"].pop("/aws-src/aws_mesouq/source_staging.py"),
])
def test_source_staging_requires_bounded_independent_authenticated_closure(change):
    context, operator = profile()
    change(context, operator)
    with pytest.raises(ValueError):
        validate(context, operator)


def test_cpu_profile_uses_explicit_resources_instead_of_inferring_from_queue_name():
    context, operator = profile()
    operator["reference_scheduler"]["partition"] = "qgpu"
    assert validate(context, operator)["reference_scheduler"]["partition"] == "qgpu"
    assert protocol.task_resources(context, "guide_reference")["native_gpus"] == 0


@pytest.mark.parametrize("change", [
    lambda c, o: o["execution_cache"].update(root="/science/src/cache"),
    lambda c, o: o["execution_cache"].update(max_bytes=1),
    lambda c, o: o["execution_cache"].update(max_namespaces=0),
    lambda c, o: o["execution_cache"].update(max_bytes=True),
    lambda c, o: c["capabilities"].remove("persistent_execution_cache_v1"),
])
def test_persistent_execution_cache_requires_explicit_bounded_private_policy(change):
    context, operator = profile()
    context["capabilities"].append("persistent_execution_cache_v1")
    operator["execution_cache"] = {"root": "/scratch/jax-cache", "max_bytes": 67108864, "max_namespaces": 4}
    assert validate(context, operator) == operator
    change(context, operator)
    with pytest.raises(ValueError):
        validate(context, operator)

@pytest.mark.parametrize("cache_root", [
    "/tmp/mesouq-source", "/tmp/mesouq-source/jax", "/tmp",
])
def test_source_staging_and_execution_cache_roots_must_be_disjoint(cache_root):
    context, operator = profile()
    context["capabilities"].append("persistent_execution_cache_v1")
    operator["execution_cache"] = {
        "root": cache_root, "max_bytes": 67108864, "max_namespaces": 4}
    with pytest.raises(ValueError, match="disjoint roots"):
        validate(context, operator)


def test_disjoint_sibling_staging_and_execution_cache_roots_remain_valid():
    context, operator = profile()
    context["capabilities"].append("persistent_execution_cache_v1")
    operator["execution_cache"] = {
        "root": "/tmp/mesouq-source-cache", "max_bytes": 67108864, "max_namespaces": 4}
    assert validate(context, operator) == operator
