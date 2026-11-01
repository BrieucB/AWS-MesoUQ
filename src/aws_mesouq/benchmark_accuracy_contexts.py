"""Explicit, source-bound accuracy execution contexts; no legacy context mutation."""
from __future__ import annotations

from copy import deepcopy
import json
import os
from pathlib import Path, PurePosixPath
import re
import stat
from urllib.parse import urlsplit

from . import benchmark_accuracy as wire
from . import benchmark_accuracy_protocol as protocol

REGISTRY_SCHEMA = "aws_mesouq.benchmark_accuracy_registry.v1"
REGISTRY_ENV = "AWS_MESOUQ_ACCURACY_CONTEXTS"


def _object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("Duplicate accuracy registry field")
        result[key] = value
    return result


def _reject_constant(value):
    raise ValueError("Finite JSON registry values required")


def read_registry(path):
    path = Path(path)
    if not path.is_absolute() or str(path) != str(path.resolve(strict=True)) or path.is_symlink():
        raise ValueError("Accuracy registry requires a direct absolute path")
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(fd, "rb") as stream:
        info = os.fstat(stream.fileno())
        if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid()
                or info.st_nlink != 1 or stat.S_IMODE(info.st_mode) & 0o077
                or info.st_size > 1_048_576):
            raise ValueError("Accuracy registry must be private, owned and bounded")
        raw = stream.read(1_048_577)
    if len(raw) > 1_048_576:
        raise ValueError("Accuracy registry exceeds its size bound")
    return json.loads(raw, object_pairs_hook=_object, parse_constant=_reject_constant)


def validate_operator(value, context):
    required = {"repository", "python_bin", "python_sha256", "adapter_path", "environment_script",
                "environment_sha256", "dependency_manifest_path", "dependency_manifest_sha256",
                "native_python_sources", "expected_host", "scheduler", "allowed_upload_origins"}
    protocol.keys(value, required, {"dependency_overlays"})
    for name in ("repository", "python_bin", "adapter_path", "environment_script", "dependency_manifest_path"):
        if not isinstance(value[name], str) or not re.fullmatch(r"/[A-Za-z0-9_./-]+", value[name]):
            raise ValueError("Reviewed native runtime paths must be safe absolute paths")
        path = PurePosixPath(value[name])
        if ".." in path.parts or str(path) != value[name] or path == PurePosixPath("/"):
            raise ValueError("Native runtime path is not canonical")
    for field in ("environment_sha256", "python_sha256", "dependency_manifest_sha256"):
        protocol.sha(value[field], field)
    sources = value["native_python_sources"]
    if not isinstance(sources, dict) or not 1 <= len(sources) <= 4096:
        raise ValueError("Exact bounded native Python source map required")
    entry = PurePosixPath(value["adapter_path"])
    if entry.parts[-3:] != ("aws_mesouq", "adapters", "benchmark_accuracy.py"):
        raise ValueError("The native adapter must use its fixed reviewed package entrypoint")
    aws_root, science_root = entry.parents[2], PurePosixPath(value["repository"]) / "src"
    overlays = value.get("dependency_overlays", [])
    if not isinstance(overlays, list) or len(overlays) > 8:
        raise ValueError("Bounded explicit dependency overlay paths required")
    seen_overlays = []
    for overlay in overlays:
        if not isinstance(overlay, str) or not re.fullmatch(r"/[A-Za-z0-9_./-]+", overlay):
            raise ValueError("Dependency overlay must be a reviewed safe absolute path")
        path = PurePosixPath(overlay)
        if (str(path) != overlay or ".." in path.parts or path == PurePosixPath("/")
                or any(path == own or path in own.parents or own in path.parents
                       for own in (aws_root, science_root))
                or any(path == prior or path in prior.parents or prior in path.parents
                       for prior in seen_overlays)):
            raise ValueError("Dependency overlay overlaps/replaces a reviewed source or dependency root")
        seen_overlays.append(path)
    required_sources = {str(aws_root / "aws_mesouq" / name) for name in (
        "__init__.py", "benchmark.py", "benchmark_accuracy.py", "benchmark_accuracy_protocol.py",
        "adapters/__init__.py", "adapters/benchmark.py", "adapters/benchmark_accuracy.py")}
    required_sources |= {str(science_root / "meso_uq" / name) for name in (
        "__init__.py", "benchmark_lab/__init__.py", "benchmark_lab/accuracy_v2/__init__.py")}
    if not required_sources <= set(sources):
        raise ValueError("Native entrypoint/package dependency closure is incomplete")
    for source, digest in sources.items():
        if not isinstance(source, str):
            raise ValueError("Invalid native Python source path")
        path = PurePosixPath(source)
        if (not path.is_absolute() or str(path) != source or ".." in path.parts
                or path.suffix != ".py" or not (aws_root in path.parents or science_root in path.parents)):
            raise ValueError("Native source map escaped its exact approved roots")
        protocol.sha(digest, "native source SHA")
    if sources.get(value["adapter_path"]) != context["adapter_sha256"]:
        raise ValueError("Native entrypoint differs from the context adapter checksum")
    if (not isinstance(value["expected_host"], str)
            or not re.fullmatch(r"login[14]\.karolina\.it4i\.cz", value["expected_host"])):
        raise ValueError("An explicitly verified Karolina login node is required")
    scheduler = value["scheduler"]
    protocol.keys(scheduler, {"partition", "account", "qos", "mem"})
    if scheduler["partition"] not in ("qgpu", "qgpu_exp"):
        raise ValueError("A reviewed Karolina GPU partition is required")
    for field in ("account", "qos", "mem"):
        if not isinstance(scheduler[field], str) or not re.fullmatch(r"[A-Za-z0-9_:.+-]+", scheduler[field]):
            raise ValueError("Unsafe native scheduler profile")
    origins = value["allowed_upload_origins"]
    if not isinstance(origins, list) or not 1 <= len(origins) <= 8 or len(set(origins)) != len(origins):
        raise ValueError("Exact reviewed private upload origins are required")
    for origin in origins:
        if not isinstance(origin, str):
            raise ValueError("Invalid upload origin")
        parsed = urlsplit(origin)
        if (parsed.scheme != "https" or not parsed.netloc or parsed.username
                or parsed.password or parsed.path or parsed.query or parsed.fragment
                or parsed.hostname is None):
            raise ValueError("Exact HTTPS upload origins are required")
    if protocol.checksum(value) != context["runtime_profile_sha256"]:
        raise ValueError("Native runtime profile differs from its context checksum")
    return deepcopy(value)


class AccuracyExecutionContexts:
    def __init__(self, document, *, project, owner_sub):
        self.project = protocol.identifier(project, "project")
        self.owner_sub = protocol.identifier(owner_sub, "owner subject")
        protocol.keys(document, {"schema", "contexts"})
        if document["schema"] != REGISTRY_SCHEMA or not isinstance(document["contexts"], list):
            raise ValueError("Unsupported accuracy context registry")
        if not 1 <= len(document["contexts"]) <= 16:
            raise ValueError("A bounded explicit accuracy context registry is required")
        self.entries = {}
        for entry in document["contexts"]:
            protocol.keys(entry, {"context", "operator"})
            context = protocol.validate_context(entry["context"])
            if context["owner_sub"] != owner_sub or context["project_id"] != project:
                raise ValueError("Accuracy context belongs to another owner or project")
            if context["id"] in self.entries:
                raise ValueError("Duplicate accuracy execution context")
            native, archive = PurePosixPath(context["native_root"]), PurePosixPath(context["archive_root"])
            if (native == PurePosixPath("/") or archive == PurePosixPath("/")
                    or native == archive or native in archive.parents or archive in native.parents):
                raise ValueError("Native and archival roots must be independent")
            for root in (native, archive):
                if not re.fullmatch(r"/[A-Za-z0-9_./-]+", str(root)) or ".." in root.parts:
                    raise ValueError("Reviewed native roots must be canonical safe paths")
            operator = validate_operator(entry["operator"], context)
            self.entries[context["id"]] = {"context": context, "operator": operator}

    @classmethod
    def from_file(cls, path, *, project, owner_sub):
        return cls(read_registry(path), project=project, owner_sub=owner_sub)

    def supported(self):
        return sorted(key for key, value in self.entries.items() if value["context"]["enabled"])

    def context(self, context_id):
        try:
            return deepcopy(self.entries[context_id]["context"])
        except KeyError:
            raise ValueError("Accuracy context is absent from the trusted registry") from None

    def operator(self, context_id):
        self.context(context_id)
        return deepcopy(self.entries[context_id]["operator"])

    def validate_claim(self, claim):
        protocol.keys(claim, {"campaign", "task"})
        if claim["campaign"] is None or claim["task"] is None:
            if claim["campaign"] is not None or claim["task"] is not None:
                raise ValueError("Incomplete empty accuracy claim")
            return deepcopy(claim)
        campaign, task = claim["campaign"], claim["task"]
        manifest = protocol.validate_manifest(task["manifest"])
        context = self.context(manifest["context_id"])
        if not context["enabled"] and not task["reconcile_only"]:
            raise ValueError("Disabled accuracy context cannot start new native work")
        if (campaign["id"] != manifest["campaign_instance_id"]
                or task["id"] != manifest["task_id"]
                or task["attempt"] != manifest["attempt"]
                or task["manifest_sha256"] != protocol.checksum(manifest)
                or campaign["execution_context_id"] != context["id"]
                or campaign["configuration_hash"] != manifest["transport_request_hash"]
                or campaign["scientific_request_hash"] != manifest["scientific_request_hash"]
                or campaign["configuration"] != wire.scientific_request(manifest["request"])):
            raise ValueError("Claim campaign, request or task identity changed")
        if (manifest["source_binding_sha256"] != context["source_binding_sha256"]
                or manifest["policy_sha256"] != context["policy_sha256"]
                or manifest["reservation"]["native_cpus"] != context["native_cpus"]
                or manifest["reservation"]["native_gpus"] != context["native_gpus"]):
            raise ValueError("Claim differs from its reviewed scientific execution context")
        protocol.integer(task["revision"], "task revision")
        lease = task["lease"]
        protocol.keys(lease, {"token", "epoch", "connector_id", "expires_at"})
        protocol.integer(lease["epoch"], "lease epoch", 1)
        protocol.identifier(lease["connector_id"], "connector")
        if not isinstance(lease["token"], str) or not 1 <= len(lease["token"]) <= 256:
            raise ValueError("Invalid task lease")
        if task["lease_epoch"] != lease["epoch"]:
            raise ValueError("Claim lease fence mismatch")
        if type(task["reconcile_only"]) is not bool:
            raise ValueError("Typed reconciliation state required")
        return deepcopy(claim)
