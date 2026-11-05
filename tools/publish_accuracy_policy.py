#!/usr/bin/env python3
"""Activate one reviewed accuracy-policy project using fresh Lambda guards."""
from __future__ import annotations

import argparse
from copy import deepcopy
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import subprocess
import sys
import tempfile
import time

SOURCE_ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(SOURCE_ROOT / "tools"), str(SOURCE_ROOT / "src"),
                str(SOURCE_ROOT / "services" / "aws_publishing")]

from publish_backend import CONFIG_KEYS, canonical, verify_source
from publish_website import PublicationError, require
from aws_publishing.accuracy import AccuracyApplication
from aws_publishing.accuracy_policy_env import decode_policies, encode_policies

GUARD_SCHEMA = "aws_mesouq.accuracy_policy_activation_guard.v1"
ENVIRONMENT_LIMIT = 4096
GUARD_KEYS = {
    "schema", "function_name", "repository_uri", "source_commit",
    "source_repository", "resolved_image_uri", "revision_id",
    "v1_policy_sha256", "v2_policy_sha256", "environment_sha256",
    "project_policy_sha256", "codec_sha256",
}
SOURCE_FILES = (
    "tools/publish_accuracy_policy.py",
    "tools/publish_backend.py",
    "tools/publish_website.py",
    "services/aws_publishing/aws_publishing/accuracy.py",
    "services/aws_publishing/aws_publishing/accuracy_policy_env.py",
)


class _SingleAttemptAws:
    """Run one AWS CLI transport attempt without changing process-wide settings."""
    def __call__(self, *arguments):
        environment = dict(os.environ, AWS_MAX_ATTEMPTS="1", AWS_RETRY_MODE="standard")
        try:
            result = subprocess.run(
                ["aws", "--no-cli-pager", "--output", "json", *map(str, arguments)],
                env=environment, capture_output=True, text=True, timeout=120, check=False)
        except (OSError, subprocess.TimeoutExpired):
            raise PublicationError("accuracy-policy-aws-command-failed") from None
        require(result.returncode == 0, "accuracy-policy-aws-command-failed")
        try:
            value = json.loads(result.stdout or "{}")
            require(isinstance(value, dict), "accuracy-policy-aws-response-invalid")
            return value
        except (ValueError, TypeError):
            raise PublicationError("accuracy-policy-aws-response-invalid") from None


def checksum(value):
    return hashlib.sha256(canonical(value).encode("utf-8")).hexdigest()


def literal_checksum(value):
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def environment_size(variables):
    return sum(len(key.encode("utf-8")) + len(value.encode("utf-8"))
               for key, value in variables.items())


def validate_policies(value):
    try:
        AccuracyApplication(None, None, accuracy_policies=value)
    except (TypeError, ValueError):
        raise PublicationError("accuracy-policy-invalid") from None
    return deepcopy(value)


def verify_import_origins():
    expected = {
        "publish_backend": SOURCE_ROOT / "tools" / "publish_backend.py",
        "publish_website": SOURCE_ROOT / "tools" / "publish_website.py",
        "aws_mesouq": SOURCE_ROOT / "src" / "aws_mesouq" / "__init__.py",
        "aws_publishing.accuracy": SOURCE_ROOT / "services" / "aws_publishing" / "aws_publishing" / "accuracy.py",
        "aws_publishing.accuracy_policy_env": SOURCE_ROOT / "services" / "aws_publishing" / "aws_publishing" / "accuracy_policy_env.py",
    }
    require(all(Path(getattr(sys.modules.get(name), "__file__", "")).resolve() == path
                for name, path in expected.items()), "accuracy-policy-import-origin-conflict")


def verify_checkout(guard, *, root=SOURCE_ROOT):
    """Bind the used helper, codec and normal loader to the approved image source."""
    try:
        head = subprocess.run(["git", "-C", str(root), "rev-parse", "HEAD"],
                              capture_output=True, timeout=10, check=False)
        require(head.returncode == 0 and head.stdout.decode().strip() == guard["source_commit"],
                "accuracy-policy-checkout-conflict")
        clean = subprocess.run(["git", "-C", str(root), "diff", "--quiet", "HEAD", "--"],
                               capture_output=True, timeout=10, check=False)
        require(clean.returncode == 0, "accuracy-policy-checkout-conflict")
        for name in SOURCE_FILES:
            blob = subprocess.run(["git", "-C", str(root), "show", guard["source_commit"] + ":" + name],
                                  capture_output=True, timeout=10, check=False)
            require(blob.returncode == 0 and blob.stdout == (root / name).read_bytes(),
                    "accuracy-policy-source-conflict")
        codec = root / SOURCE_FILES[-1]
        require(hashlib.sha256(codec.read_bytes()).hexdigest() == guard["codec_sha256"],
                "accuracy-policy-codec-conflict")
    except (OSError, UnicodeError, subprocess.TimeoutExpired):
        raise PublicationError("accuracy-policy-source-unavailable") from None


def _validate_guard(guard, project_policies):
    require(isinstance(guard, dict) and set(guard) == GUARD_KEYS
            and guard["schema"] == GUARD_SCHEMA, "accuracy-policy-guard-invalid")
    for name in ("v1_policy_sha256", "v2_policy_sha256", "environment_sha256",
                 "project_policy_sha256", "codec_sha256"):
        require(isinstance(guard[name], str) and re.fullmatch(r"[a-f0-9]{64}", guard[name]),
                "accuracy-policy-guard-invalid")
    require(isinstance(guard["function_name"], str)
            and re.fullmatch(r"[A-Za-z0-9_-]{1,64}", guard["function_name"])
            and isinstance(guard["source_commit"], str)
            and re.fullmatch(r"[a-f0-9]{40}", guard["source_commit"])
            and isinstance(guard["source_repository"], str)
            and re.fullmatch(r"https://github.com/[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+",
                             guard["source_repository"])
            and isinstance(guard["revision_id"], str)
            and 1 <= len(guard["revision_id"]) <= 256
            and "\n" not in guard["revision_id"] and "\r" not in guard["revision_id"],
            "accuracy-policy-guard-invalid")
    match = re.fullmatch(
        r"([0-9]{12})\.dkr\.ecr\.([a-z]{2}-[a-z]+-[0-9]+)\.amazonaws\.com/([a-z0-9][a-z0-9._/-]*)",
        str(guard["repository_uri"]))
    require(match is not None and isinstance(guard["resolved_image_uri"], str)
            and re.fullmatch(re.escape(guard["repository_uri"]) + r"@sha256:[a-f0-9]{64}",
                             guard["resolved_image_uri"]), "accuracy-policy-guard-invalid")
    require(isinstance(project_policies, dict) and len(project_policies) == 1,
            "accuracy-policy-project-scope-invalid")
    validated = validate_policies(project_policies)
    require(checksum(validated) == guard["project_policy_sha256"],
            "accuracy-policy-project-conflict")
    return match[1], match[2], validated


def _merge_policies(existing, update):
    merged = deepcopy(existing)
    for project, contexts in update.items():
        previous = existing.get(project, [])
        by_id = {context["id"]: context for context in contexts}
        for context in previous:
            candidate = by_id.get(context["id"])
            require(candidate is not None and
                    {k: v for k, v in candidate.items() if k != "enabled"} ==
                    {k: v for k, v in context.items() if k != "enabled"},
                    "accuracy-policy-history-conflict")
        merged[project] = deepcopy(contexts)
    return validate_policies(merged)


def publish(guard, project_policies, *, apply=False, aws=None,
            image_verifier=None, checkout_verifier=None, sleep=time.sleep):
    """Perform at most one configuration update; ambiguous replies require reconciliation."""
    verify_import_origins()
    account, region, update = _validate_guard(guard, project_policies)
    (checkout_verifier or verify_checkout)(guard)
    raw_aws = aws or _SingleAttemptAws()

    def call(*arguments):
        return raw_aws("--region", region, *arguments)

    identity = call("sts", "get-caller-identity")
    require(identity.get("Account") == account, "accuracy-policy-account-conflict")
    expected_arn = ("arn:aws:lambda:" + region + ":" + account +
                    ":function:" + guard["function_name"])

    def read():
        value = call("lambda", "get-function", "--function-name", guard["function_name"])
        cfg = value.get("Configuration", {})
        require(cfg.get("FunctionArn") == expected_arn
                and cfg.get("FunctionName") == guard["function_name"]
                and cfg.get("PackageType") == "Image" and cfg.get("Architectures") == ["x86_64"]
                and cfg.get("State") == "Active" and cfg.get("LastUpdateStatus") == "Successful"
                and isinstance(cfg.get("RevisionId"), str) and bool(cfg["RevisionId"])
                and not cfg.get("Environment", {}).get("Error"),
                "accuracy-policy-function-state-invalid")
        require(value.get("Code", {}).get("ResolvedImageUri") == guard["resolved_image_uri"],
                "accuracy-policy-image-conflict")
        return value, cfg

    initial, cfg = read()
    variables = cfg.get("Environment", {}).get("Variables", {})
    require(isinstance(variables, dict) and
            all(isinstance(k, str) and isinstance(v, str) for k, v in variables.items()),
            "accuracy-policy-environment-invalid")
    require(cfg["RevisionId"] == guard["revision_id"]
            and literal_checksum(variables.get("BENCHMARK_POLICIES", "")) == guard["v1_policy_sha256"]
            and literal_checksum(variables.get("BENCHMARK_ACCURACY_POLICIES", "")) == guard["v2_policy_sha256"]
            and checksum(variables) == guard["environment_sha256"],
            "accuracy-policy-guard-conflict")
    try:
        existing = validate_policies(decode_policies(
            variables.get("BENCHMARK_ACCURACY_POLICIES", "{}")))
        merged = _merge_policies(existing, update)
        encoded = encode_policies(merged)
        require(decode_policies(encoded) == merged, "accuracy-policy-codec-roundtrip-failed")
    except (ValueError, TypeError):
        raise PublicationError("accuracy-policy-invalid") from None
    target = dict(variables, BENCHMARK_ACCURACY_POLICIES=encoded)
    size = environment_size(target)
    require(size <= ENVIRONMENT_LIMIT, "accuracy-policy-environment-too-large")
    verified_image = (image_verifier or verify_source)(
        call, guard["repository_uri"], guard["source_commit"], guard["source_repository"])
    require(verified_image == guard["resolved_image_uri"], "accuracy-policy-source-image-conflict")
    stable = {key: cfg.get(key) for key in CONFIG_KEYS}
    result = {
        "status": "planned", "configuration_updates": 0,
        "environment_utf8_bytes": size, "project_count": len(merged),
        "updated_project_count": len(update),
        "source_commit": guard["source_commit"],
        "accuracy_policy_sha256": literal_checksum(encoded),
        "environment_sha256": checksum(target),
    }
    if not apply:
        return result
    if target != variables:
        with tempfile.TemporaryDirectory(prefix="mesouq-accuracy-policy-") as temporary:
            path = Path(temporary) / "environment.json"
            fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY | os.O_NOFOLLOW, 0o600)
            os.fchmod(fd, 0o600)
            with os.fdopen(fd, "w") as stream:
                stream.write(canonical({"Variables": target}))
                stream.flush()
                os.fsync(stream.fileno())
            try:
                call("lambda", "update-function-configuration",
                     "--function-name", guard["function_name"],
                     "--revision-id", cfg["RevisionId"], "--environment", "file://" + str(path))
            except (PublicationError, OSError):
                raise PublicationError("accuracy-policy-update-unknown-reconcile") from None
        result["configuration_updates"] = 1
        for _ in range(60):
            response = call("lambda", "get-function", "--function-name", guard["function_name"])
            state = response.get("Configuration", {})
            require(state.get("LastUpdateStatus") in ("InProgress", "Successful"),
                    "accuracy-policy-update-failed-reconcile")
            if state["LastUpdateStatus"] == "Successful":
                break
            sleep(2)
        else:
            raise PublicationError("accuracy-policy-update-pending-reconcile")
    _, final = read()
    require(final.get("Environment", {}).get("Variables", {}) == target
            and {key: final.get(key) for key in CONFIG_KEYS} == stable,
            "accuracy-policy-readback-mismatch-reconcile")
    result["status"] = "verified"
    return result


def _private_json(path):
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(fd, "rb") as stream:
        info = os.fstat(stream.fileno())
        require(stat.S_ISREG(info.st_mode) and info.st_uid == os.getuid()
                and not stat.S_IMODE(info.st_mode) & 0o077
                and info.st_nlink == 1 and info.st_size <= 262144,
                "accuracy-policy-input-unsafe")
        try:
            return json.loads(stream.read(262145))
        except (ValueError, UnicodeError, RecursionError):
            raise PublicationError("accuracy-policy-input-invalid") from None


def _save_private(path, value):
    fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY | os.O_NOFOLLOW, 0o600)
    os.fchmod(fd, 0o600)
    with os.fdopen(fd, "w") as stream:
        stream.write(canonical(value) + "\n")
        stream.flush()
        os.fsync(stream.fileno())


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--guard", required=True)
    parser.add_argument("--project-policy", required=True)
    parser.add_argument("--receipt-directory", required=True)
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args(argv)
    try:
        guard, policy = _private_json(args.guard), _private_json(args.project_policy)
        directory = Path(args.receipt_directory).absolute()
        require(directory.resolve() == directory, "accuracy-policy-receipt-path-invalid")
        directory.mkdir(mode=0o700)
        directory.chmod(0o700)
        _save_private(directory / "intent.json", {
            "schema": "aws_mesouq.accuracy_policy_activation_intent.v1",
            "guard_sha256": checksum(guard), "project_policy_sha256": checksum(policy),
            "apply": args.apply,
        })
        try:
            result = publish(guard, policy, apply=args.apply)
        except (PublicationError, OSError, ValueError) as error:
            _save_private(directory / "failure.json", {
                "schema": "aws_mesouq.accuracy_policy_activation_failure.v1",
                "status": "requires_reconciliation" if args.apply else "failed",
                "error_class": type(error).__name__,
            })
            raise
        _save_private(directory / "receipt.json", result)
        print(canonical({key: result[key] for key in
                         ("status", "configuration_updates", "environment_utf8_bytes")}))
        return 0
    except (PublicationError, OSError, ValueError):
        print("Accuracy policy activation failed; inspect the private receipt and reconcile before retrying.",
              file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
