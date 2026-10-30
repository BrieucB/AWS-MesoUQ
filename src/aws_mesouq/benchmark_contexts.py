"""Trusted execution contexts and an atomic, same-journal benchmark migration.

The registry is operator input, never an API response. It contains no credentials.
"""
from __future__ import annotations

import copy
import json
import os
from pathlib import Path
import stat

from .benchmark import campaign_execution_context_id, operator_execution_context_id


REGISTRY_ENV = "AWS_MESOUQ_BENCHMARK_CONTEXT_REGISTRY"
REGISTRY_SCHEMA = "aws_mesouq.benchmark_context_registry.v1"
JOURNAL_SCHEMA = "aws_mesouq.benchmark_journal.v2"
DEADLINE_POLICY = "allocation_deadline_v1"


def _unique_object(pairs):
    value = {}
    for key, item in pairs:
        if key in value:
            raise ValueError("Duplicate trusted registry field")
        value[key] = item
    return value


def _registry_document(path):
    path = Path(path)
    if not path.is_absolute() or Path(os.path.abspath(path)) != path.resolve():
        raise ValueError("Trusted context registry path must be canonical and absolute")
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    try:
        before = os.fstat(fd)
        if (not stat.S_ISREG(before.st_mode) or before.st_uid != os.getuid()
                or stat.S_IMODE(before.st_mode) != 0o600 or before.st_nlink != 1
                or before.st_size > 256 * 1024):
            raise ValueError("Trusted context registry must be a bounded private owner-only file")
        with os.fdopen(fd, "rb", closefd=False) as stream:
            blob = stream.read(256 * 1024 + 1)
        after = os.fstat(fd)
        if (len(blob) != before.st_size
                or (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns, before.st_ctime_ns)
                != (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns, after.st_ctime_ns)):
            raise ValueError("Trusted context registry changed while reading")
        def reject_constant(value):
            raise ValueError("Nonfinite trusted registry number")
        return json.loads(blob, object_pairs_hook=_unique_object, parse_constant=reject_constant)
    finally:
        os.close(fd)


class ExecutionContexts:
    def __init__(self, legacy_operator, project, validator):
        self.project = project
        self.legacy_operator = validator(legacy_operator)
        from .adapters import LEGACY_BENCHMARK_ADAPTER_SHA256
        if self.legacy_operator["adapter_sha256"] != LEGACY_BENCHMARK_ADAPTER_SHA256:
            raise ValueError("Original benchmark operator requires the exact legacy adapter")
        legacy_campaign = {"project_id": project, "approved_adapter": {
            key: legacy_operator[key] for key in ("science_revision", "adapter_sha256", "archive_root")}}
        self.legacy_id = campaign_execution_context_id(legacy_campaign)
        self.contexts = {self.legacy_id: {"operator": copy.deepcopy(self.legacy_operator),
                                         "runtime_clock_policy": None}}
        path = os.environ.get(REGISTRY_ENV)
        self.versioned = path is not None
        if path is None:
            return
        document = _registry_document(path)
        if (not isinstance(document, dict)
                or set(document) != {"schema", "legacy_operator", "contexts"}
                or document["schema"] != REGISTRY_SCHEMA
                or document["legacy_operator"] != self.legacy_operator
                or not isinstance(document["contexts"], list)
                or not 1 <= len(document["contexts"]) <= 15):
            raise ValueError("Trusted context registry differs from the original legacy operator")
        for entry in document["contexts"]:
            if (not isinstance(entry, dict) or set(entry) != {"operator", "runtime_clock_policy"}
                    or entry["runtime_clock_policy"] != DEADLINE_POLICY):
                raise ValueError("Unsupported trusted execution context")
            operator = validator(entry["operator"])
            if any(operator[key] != self.legacy_operator[key] for key in ("expected_user", "expected_host")):
                raise ValueError("All contexts must use the same verified HPC identity")
            from .adapters import adapter_snapshot, LEGACY_BENCHMARK_ADAPTER_SHA256
            if operator["adapter_sha256"] in (
                    self.legacy_operator["adapter_sha256"], LEGACY_BENCHMARK_ADAPTER_SHA256):
                raise ValueError("Allocation deadline policy cannot use the legacy adapter")
            adapter_snapshot(operator["adapter_sha256"])
            context_id = operator_execution_context_id(operator)
            if context_id in self.contexts:
                raise ValueError("Duplicate trusted execution context")
            self.contexts[context_id] = {"operator": operator, "runtime_clock_policy": DEADLINE_POLICY}

    def operator(self, context_id):
        try:
            return self.contexts[context_id]["operator"]
        except KeyError:
            raise ValueError("Execution context is absent from the trusted registry") from None

    def policy(self, context_id):
        self.operator(context_id)
        return self.contexts[context_id]["runtime_clock_policy"]

    def campaign(self, campaign, owner_sub):
        if campaign.get("project_id") != self.project or campaign.get("owner_sub") != owner_sub:
            raise ValueError("Campaign context differs from the authenticated owner/project")
        context_id = campaign_execution_context_id(campaign)
        operator = self.operator(context_id)
        approved = campaign["approved_adapter"]
        if any(approved.get(key) != operator[key]
               for key in ("science_revision", "adapter_sha256", "archive_root")):
            raise ValueError("Campaign context differs from trusted source/storage pins")
        return context_id

    def namespace(self, namespace, legacy_context):
        """Validate a copy completely; the caller performs one normal atomic save."""
        legacy_context = copy.deepcopy(legacy_context)
        candidate = copy.deepcopy(namespace) if namespace is not None else {
            "context": legacy_context, "stages": {}, "campaigns": {}}
        if not isinstance(candidate, dict) or not isinstance(candidate.get("stages"), dict) or not isinstance(candidate.get("campaigns"), dict):
            raise ValueError("Invalid benchmark journal namespace")
        was_legacy = candidate.get("context") == legacy_context
        if self.versioned:
            sentinel = {key: value for key, value in legacy_context.items() if key != "operator"}
            sentinel["schema"] = JOURNAL_SCHEMA
            if was_legacy:
                candidate["legacy_context"] = copy.deepcopy(candidate["context"])
                candidate["context"] = sentinel
                candidate["contexts"] = {}
            elif candidate.get("context") != sentinel or candidate.get("legacy_context") != legacy_context:
                raise ValueError("Benchmark journal belongs to another immutable execution context")
            stored = candidate.get("contexts")
            if not isinstance(stored, dict) or any(
                    key not in self.contexts or value != self.contexts[key]
                    for key, value in stored.items()):
                raise ValueError("Trusted registry must retain exact stored execution contexts")
            if not was_legacy:
                referenced = {entry.get("context_id") for entry in candidate["stages"].values()}
                referenced |= {record.get("context_id") for record in candidate["campaigns"].values()}
                if not referenced <= set(stored):
                    raise ValueError("Referenced execution context is missing from the journal registry")
            for key, value in self.contexts.items():
                stored.setdefault(key, copy.deepcopy(value))
        elif not was_legacy:
            raise ValueError("Versioned benchmark journal requires its trusted context registry")
        for entry in candidate["stages"].values():
            context_id = self.campaign(entry["claim"]["campaign"], legacy_context["owner_sub"])
            old_id = entry.get("context_id")
            if old_id != context_id and (old_id is not None or not was_legacy):
                raise ValueError("Stage execution context binding differs")
            entry["context_id"] = context_id
        for campaign_id, record in candidate["campaigns"].items():
            context_id = record.get("context_id", self.legacy_id if was_legacy else None)
            operator = self.operator(context_id)
            if record.get("root") != operator["run_root"] + "/" + campaign_id:
                raise ValueError("Campaign journal root differs from execution context")
            record["context_id"] = context_id
        for entry in candidate["stages"].values():
            campaign_id = entry["claim"]["campaign"]["id"]
            record = candidate["campaigns"].get(campaign_id)
            if record is not None and record["context_id"] != entry["context_id"]:
                raise ValueError("Stage and campaign execution contexts differ")
        return candidate
