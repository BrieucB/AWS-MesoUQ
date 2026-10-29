#!/usr/bin/env python3
"""Publish one reviewed metadata-only backend using an ephemeral scoped role."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import time

from publish_website import AwsCli, PublicationError, require, verify_identity

# The exact same stdlib-only validator is shipped in the Lambda image.
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from aws_mesouq.benchmark import validate_benchmark_policies

OCI = "application/vnd.oci.image.manifest.v1+json"
CONFIG_KEYS = ("FunctionArn", "FunctionName", "PackageType", "Role", "MemorySize",
               "Timeout", "Architectures", "VpcConfig", "KMSKeyArn", "ImageConfigResponse",
               "EphemeralStorage", "TracingConfig", "DeadLetterConfig", "LoggingConfig")


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)


def validate_policies(value):
    require(isinstance(value, dict) and bool(value), "benchmark-policy-invalid")
    try:
        validated = validate_benchmark_policies(value)
    except (TypeError, ValueError):
        raise PublicationError("benchmark-policy-invalid") from None
    # Publication must pin custody even though the cloud contract allows an
    # operator to leave archival unavailable in a non-production deployment.
    require(all("archive_root" in policy for policy in validated.values()),
            "benchmark-policy-invalid")
    return canonical(validated)


def verify_source(aws, repository_uri, sha, source, command=None):
    """Check immutable ECR source tag, OCI bytes, platform, and image source labels."""
    run = command or subprocess.run
    registry, repository = repository_uri.split("/", 1)
    account, region = registry.split(".")[0], registry.split(".")[3]
    described = aws("ecr", "describe-repositories", "--registry-id", account,
                    "--repository-names", repository)["repositories"]
    require(len(described) == 1 and described[0].get("repositoryUri") == repository_uri and
            described[0].get("imageTagMutability") == "IMMUTABLE" and
            not described[0].get("imageTagMutabilityExclusionFilters"), "image-repository-invalid")
    result = aws("ecr", "batch-get-image", "--registry-id", account, "--repository-name", repository,
                 "--image-ids", "imageTag=source-" + sha, "--accepted-media-types", OCI)
    require(len(result.get("images", [])) == 1 and not result.get("failures"), "source-image-missing")
    item = result["images"][0]
    raw = item["imageManifest"].encode()
    digest = item["imageId"]["imageDigest"]
    manifest = json.loads(raw)
    require(digest == "sha256:" + hashlib.sha256(raw).hexdigest() and
            item.get("imageManifestMediaType") == OCI and manifest.get("schemaVersion") == 2 and
            manifest.get("mediaType") == OCI and "manifests" not in manifest, "source-image-invalid")
    immutable = repository_uri + "@" + digest
    def docker(arguments, data=None):
        response = run(["docker", *arguments], input=data, capture_output=True,
                       timeout=240, check=False)
        require(response.returncode == 0, "source-image-verification-failed")
        return response.stdout
    with tempfile.TemporaryDirectory(prefix="mesouq-registry-") as temporary:
        # Credentials remain in this temporary private directory, never in runner HOME.
        previous = os.environ.get("DOCKER_CONFIG")
        os.environ["DOCKER_CONFIG"] = temporary
        Path(temporary).chmod(0o700)
        try:
            # AWS CLI password output is never printed or passed as an argument.
            response = run(["aws", "--no-cli-pager", "ecr", "get-login-password", "--region", region],
                           capture_output=True, timeout=60, check=False)
            require(response.returncode == 0 and bool(response.stdout), "image-authentication-failed")
            docker(["login", "--username", "AWS", "--password-stdin", registry], response.stdout)
            docker(["pull", "--platform", "linux/amd64", immutable])
            inspected = json.loads(docker(["image", "inspect", immutable]))
            require(len(inspected) == 1, "source-image-invalid")
            config = inspected[0]
            labels = config.get("Config", {}).get("Labels") or {}
            require(config.get("Os") == "linux" and config.get("Architecture") == "amd64" and
                    config.get("Id") == manifest["config"]["digest"] and
                    labels.get("org.opencontainers.image.revision") == sha and
                    labels.get("org.opencontainers.image.source") == source and
                    immutable in config.get("RepoDigests", []), "source-image-invalid")
        finally:
            if previous is None:
                os.environ.pop("DOCKER_CONFIG", None)
            else:
                os.environ["DOCKER_CONFIG"] = previous
    return immutable


def publish(function, repository_uri, source_sha, source_repository, baseline_image,
            policies, expected_policy_sha256, expected_role_arn, aws=None,
            image_verifier=None, sleep=time.sleep, previous_source_sha=None):
    aws = aws or AwsCli()
    require(isinstance(function, str) and re.fullmatch(r"[A-Za-z0-9_-]{1,64}", function),
            "function-input-invalid")
    role = re.fullmatch(r"arn:aws:iam::([0-9]{12}):role/[A-Za-z0-9_+=,.@/-]+", expected_role_arn)
    require(role is not None and re.fullmatch(r"[a-f0-9]{40}", source_sha) and
            re.fullmatch(r"https://github.com/[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", source_repository),
            "source-input-invalid")
    uri = re.fullmatch(role[1] + r"\.dkr\.ecr\.([a-z]{2}-[a-z]+-[0-9]+)\.amazonaws\.com/([a-z0-9][a-z0-9._/-]*)",
                       repository_uri)
    require(uri is not None and re.fullmatch(r"[a-f0-9]{64}", expected_policy_sha256),
            "source-input-invalid")
    image_pattern = re.escape(repository_uri) + r"@sha256:[a-f0-9]{64}"
    if previous_source_sha is None:
        # Preserve the original helper API for existing callers.
        require(isinstance(baseline_image, str) and re.fullmatch(image_pattern, baseline_image),
                "source-input-invalid")
    else:
        require(isinstance(previous_source_sha, str) and
                re.fullmatch(r"[a-f0-9]{40}", previous_source_sha), "source-input-invalid")
    policy_json = validate_policies(policies)
    verify_identity(aws, expected_role_arn)
    verifier = image_verifier or verify_source
    if previous_source_sha is not None:
        # An explicit reviewed source always supersedes a stale legacy URI.
        baseline_image = verifier(aws, repository_uri, previous_source_sha, source_repository)
        require(isinstance(baseline_image, str) and re.fullmatch(image_pattern, baseline_image),
                "source-image-invalid")
    immutable = verifier(aws, repository_uri, source_sha, source_repository)
    require(re.fullmatch(re.escape(repository_uri) + r"@sha256:[a-f0-9]{64}", immutable),
            "source-image-invalid")
    expected_arn = "arn:aws:lambda:" + uri[1] + ":" + role[1] + ":function:" + function
    def read():
        value = aws("lambda", "get-function", "--function-name", function)
        cfg = value.get("Configuration", {})
        require(cfg.get("FunctionArn") == expected_arn and cfg.get("FunctionName") == function and
                cfg.get("PackageType") == "Image" and cfg.get("Architectures") == ["x86_64"] and
                cfg.get("State") == "Active" and cfg.get("LastUpdateStatus") == "Successful" and
                isinstance(cfg.get("RevisionId"), str) and bool(cfg["RevisionId"]) and
                not cfg.get("Environment", {}).get("Error"), "function-state-invalid")
        return value, cfg
    initial, cfg = read()
    require(initial.get("Code", {}).get("ResolvedImageUri") in (baseline_image, immutable),
            "function-baseline-mismatch")
    variables = cfg.get("Environment", {}).get("Variables", {})
    require(isinstance(variables, dict) and all(isinstance(k, str) and isinstance(v, str)
                                              for k, v in variables.items()), "function-environment-invalid")
    existing_policy = variables.get("BENCHMARK_POLICIES", "")
    require(existing_policy == policy_json or
            hashlib.sha256(existing_policy.encode()).hexdigest() == expected_policy_sha256,
            "function-policy-conflict")
    target_environment = dict(variables, BENCHMARK_POLICIES=policy_json)
    stable = {key: cfg.get(key) for key in CONFIG_KEYS}
    def wait():
        for _ in range(60):
            value = aws("lambda", "get-function", "--function-name", function)
            state = value.get("Configuration", {})
            require(state.get("LastUpdateStatus") in ("InProgress", "Successful"), "function-update-failed")
            if state.get("LastUpdateStatus") == "Successful":
                return read()
            sleep(2)
        raise PublicationError("function-update-pending-reconcile")
    configuration_updates = code_updates = 0
    if existing_policy != policy_json:
        with tempfile.TemporaryDirectory(prefix="mesouq-lambda-") as temporary:
            path = Path(temporary) / "environment.json"
            path.touch(mode=0o600)
            path.write_text(canonical({"Variables": target_environment}))
            aws("lambda", "update-function-configuration", "--function-name", function,
                "--revision-id", cfg["RevisionId"], "--environment", "file://" + str(path))
        configuration_updates += 1
        initial, cfg = wait()
    require(cfg.get("Environment", {}).get("Variables", {}) == target_environment and
            {key: cfg.get(key) for key in CONFIG_KEYS} == stable,
            "function-configuration-changed")
    require(initial.get("Code", {}).get("ResolvedImageUri") in (baseline_image, immutable),
            "function-baseline-mismatch")
    if initial["Code"]["ResolvedImageUri"] != immutable:
        aws("lambda", "update-function-code", "--function-name", function, "--image-uri", immutable,
            "--revision-id", cfg["RevisionId"], "--no-publish")
        code_updates += 1
        initial, cfg = wait()
    require(initial.get("Code", {}).get("ResolvedImageUri") == immutable and
            cfg.get("Environment", {}).get("Variables", {}) == target_environment and
            {key: cfg.get(key) for key in CONFIG_KEYS} == stable,
            "function-readback-mismatch")
    result = {"status": "verified", "source_commit": source_sha,
              "configuration_updates": configuration_updates, "code_updates": code_updates,
              "benchmark_policy_sha256": hashlib.sha256(policy_json.encode()).hexdigest()}
    if previous_source_sha is not None:
        result["previous_source_commit"] = previous_source_sha
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-commit", required=True)
    args = parser.parse_args()
    try:
        result = publish(
            os.environ["BACKEND_FUNCTION_NAME"], os.environ["AWS_ECR_REPOSITORY_URL"],
            args.source_commit, "https://github.com/" + os.environ["GITHUB_REPOSITORY"],
            os.environ.get("BACKEND_BASELINE_IMAGE_URI"), json.loads(os.environ["BENCHMARK_POLICIES"]),
            os.environ["EXPECTED_POLICY_SHA256"], os.environ["PUBLICATION_ROLE_ARN"],
            previous_source_sha=os.environ.get("EXPECTED_PREVIOUS_SOURCE_COMMIT"))
        print(json.dumps(result, sort_keys=True))
        return 0
    except Exception as exc:
        category = str(exc) if isinstance(exc, PublicationError) else "backend-publication-failed"
        print(json.dumps({"status": "failed", "category": category}))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
