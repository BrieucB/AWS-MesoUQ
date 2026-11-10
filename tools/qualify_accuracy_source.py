"""Generate an accuracy source qualification through the normal native guard.

Run on the qualified HPC host. Inputs are trusted operator registry metadata;
this tool neither publishes policy nor modifies the registry or any campaign.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path
from types import SimpleNamespace
import subprocess
import sys

SOURCE_ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(SOURCE_ROOT / "src"), str(SOURCE_ROOT / "tools")]

from aws_mesouq import benchmark_accuracy_protocol as protocol
from aws_mesouq.benchmark_accuracy_contexts import AccuracyExecutionContexts, read_registry
from aws_mesouq.adapters.benchmark_accuracy import AccuracyNativeExecutor
from recover_benchmark_context_registry import private_directory, write_new

RECEIPT_SCHEMA = "aws_mesouq.benchmark_accuracy_source_qualification_receipt.v1"


class LocalNativeTransport:
    """Use the unchanged native RPC process boundary without opening SSH."""
    staging_python = "/usr/bin/python3.11"

    def run_bytes(self, argv, *, data):
        return subprocess.run(argv, input=data, capture_output=True, check=True,
                              timeout=60).stdout


def qualify(contexts, context_id, native):
    """Return source-derived metadata only; no caller-supplied expected pin."""
    context = contexts.context(context_id)
    record = protocol.validate_source_qualification(native.qualify_source(context_id))
    if (record["science_revision"] != context["science_revision"]
            or record["source_binding_sha256"] != context["source_binding_sha256"]):
        raise ValueError("Qualified source tuple differs from the trusted context")
    if record["semantic_policy_sha256"] != context["policy_sha256"]:
        raise ValueError("Proposed context policy differs from authenticated science")
    qualified = dict(context, source_qualification=record)
    qualified["id"] = protocol.context_id(qualified)
    protocol.validate_context(qualified)
    return {"schema": RECEIPT_SCHEMA, "qualification": record,
            "input_context_id": context_id,
            "qualified_context_id": qualified["id"],
            "runtime_profile_sha256": context["runtime_profile_sha256"],
            "adapter_sha256": context["adapter_sha256"]}


class _PrivateArgumentParser(argparse.ArgumentParser):
    def error(self, _message):
        raise ValueError("Invalid source qualification arguments")


def main(argv=None):
    parser = _PrivateArgumentParser(description=__doc__, allow_abbrev=False)
    parser.add_argument("--registry", required=True)
    parser.add_argument("--project", required=True)
    parser.add_argument("--owner-sub", required=True)
    parser.add_argument("--context-id", required=True)
    parser.add_argument("--output", required=True)
    try:
        args = parser.parse_args(argv)
        registry_path = Path(args.registry)
        document = read_registry(registry_path)
        contexts = AccuracyExecutionContexts(document, project=args.project, owner_sub=args.owner_sub)
        native = AccuracyNativeExecutor(SimpleNamespace(transport=LocalNativeTransport()), contexts)
        receipt = qualify(contexts, args.context_id, native)
        # The source-derived record is private metadata, never stdout.
        output = Path(args.output)
        if not output.is_absolute():
            raise ValueError("An explicit private output path is required")
        private_directory(output.parent)
        raw = protocol.canonical(receipt) + b"\n"
        write_new(output, raw)
    except Exception:
        print(json.dumps({"status": "rejected", "error_code": "source_qualification_failed"}))
        return 2
    print(json.dumps({"status": "qualified"}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
