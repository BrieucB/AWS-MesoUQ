#!/usr/bin/env python3
"""Offline, metadata-only staged cold-entry CAS planner; no AWS/apply mode.

Inputs are strict nonsecret projections of live AWS responses. The complete
initial DistributionConfig is pinned to the already reviewed canonical hash.
No token, cookie, session row, signing key or credential belongs in this input.
"""
from __future__ import annotations

import argparse
import copy
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess

ROOT = Path(__file__).resolve().parents[1]
SCHEMA = "aws_mesouq.secure_cold_entry_metadata.v1"
BASELINE = "dace4a501cb539d61ecad42e592f36b8666f2cc1"
INITIAL_SHA256 = "16f33e77424ec38c75f77456033dc133dd77a57f9bed1ee8f7d02c207cb40ad3"
DISTRIBUTION_ID = "ETMWO787JIPRL"
FUNCTIONS = {"legacy": "remembered-entry", "private": "private-entry", "public": "public-entry"}
SOURCE_FILES = (
    "tools/prepare_cold_entry_release.py", "tools/publish_website.py",
    "infra/aws_publishing/functions/remembered-entry.js",
    "infra/aws_publishing/functions/private-entry.js",
    "infra/aws_publishing/functions/public-entry.js",
    ".github/workflows/publish-private-entry-website.yml",
)
CONFIG_FIELDS = set(("CallerReference Aliases DefaultRootObject Origins OriginGroups "
    "DefaultCacheBehavior CacheBehaviors CustomErrorResponses Comment Logging "
    "PriceClass Enabled ViewerCertificate Restrictions WebACLId HttpVersion "
    "IsIPV6Enabled ContinuousDeploymentPolicyId Staging").split())


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()


def sha(value):
    return hashlib.sha256(canonical(value)).hexdigest()


def require(condition, category):
    if not condition:
        raise ValueError(category)


def exact(value, fields, category):
    require(isinstance(value, dict) and set(value) == set(fields), category)
    return value


def text(value, category):
    require(isinstance(value, str) and bool(value) and len(value) <= 2048
            and not any(ord(char) < 32 for char in value), category)
    return value


def digest(value, category):
    require(isinstance(value, str) and re.fullmatch(r"[a-f0-9]{64}", value), category)
    return value


def collection(value, category):
    require(isinstance(value, dict) and set(value) <= {"Quantity", "Items"}
            and type(value.get("Quantity")) is int, category)
    items = value.get("Items", [])
    require(isinstance(items, list) and value["Quantity"] == len(items), category)
    return items


def verify_source(commit):
    require(isinstance(commit, str) and re.fullmatch(r"[a-f0-9]{40}", commit),
            "reviewed-source-invalid")
    env = dict(os.environ, GIT_SSH_COMMAND="/usr/bin/false", GIT_TERMINAL_PROMPT="0")
    try:
        head = subprocess.check_output(["git", "-C", str(ROOT), "rev-parse", "HEAD"],
                                       env=env, timeout=10, text=True).strip()
        require(head == commit, "reviewed-source-mismatch")
        subprocess.check_call(["git", "-C", str(ROOT), "merge-base", "--is-ancestor", BASELINE, commit],
                              env=env, timeout=10, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        for name in SOURCE_FILES:
            committed = subprocess.check_output(
                ["git", "-C", str(ROOT), "show", commit + ":" + name],
                env=env, timeout=10)
            require(committed == (ROOT / name).read_bytes(), "dirty-release-source")
    except (OSError, subprocess.SubprocessError):
        raise ValueError("source-verification-failed") from None


def function_metadata(value, kind, account):
    exact(value, {"ARN", "ETag", "Stage", "Runtime", "Code"}, "function-fields-invalid")
    suffix = "remembered-entry" if kind == "legacy" else FUNCTIONS[kind] + "-v1"
    arn = "arn:aws:cloudfront::" + account + ":function/aws-mesouq-" + suffix
    require(value["ARN"] == arn and value["Stage"] == "LIVE"
            and value["Runtime"] == "cloudfront-js-1.0", "function-identity-invalid")
    text(value["ETag"], "function-etag-required")
    source = (ROOT / ("infra/aws_publishing/functions/" + FUNCTIONS[kind] + ".js")).read_text()
    require(value["Code"] == source, "function-code-mismatch")
    return {"arn": arn, "etag": value["ETag"],
            "code_sha256": hashlib.sha256(source.encode()).hexdigest()}


def validate_topology(metadata, initial, distribution_arn):
    # The reviewed initial hash protects every unlisted nested field, including
    # auth behavior order, methods, policies, key groups and all origin settings.
    require(isinstance(initial, dict) and set(initial) <= CONFIG_FIELDS
            and sha(initial) == INITIAL_SHA256, "reviewed-initial-config-mismatch")
    require(initial["DefaultRootObject"] == "index.html", "initial-root-invalid")
    origins = collection(initial["Origins"], "origins-invalid")
    require(len(origins) == 2 and {item["Id"] for item in origins} == {"private-web", "regional-auth"},
            "origins-invalid")
    private = next(item for item in origins if item["Id"] == "private-web")
    auth = next(item for item in origins if item["Id"] == "regional-auth")
    default = initial["DefaultCacheBehavior"]
    trusted = default.get("TrustedKeyGroups", {})
    require(default["TargetOriginId"] == "private-web" and trusted.get("Enabled") is True
            and trusted.get("Quantity") == 1 and len(trusted.get("Items", [])) == 1
            and set(default["AllowedMethods"]["Items"]) == {"GET", "HEAD"}
            and default["ViewerProtocolPolicy"] == "redirect-to-https",
            "private-default-invalid")
    behaviors = collection(initial["CacheBehaviors"], "behaviors-invalid")
    require(len(behaviors) == 1 and behaviors[0]["PathPattern"] == "/auth/*"
            and behaviors[0]["TargetOriginId"] == "regional-auth", "auth-behavior-invalid")
    auth_behavior = behaviors[0]
    api = exact(metadata["auth_api"], {"ApiId", "ApiEndpoint", "ProtocolType", "Routes"}, "api-fields-invalid")
    require(re.fullmatch(r"[a-z0-9]+", api["ApiId"]) and api["ProtocolType"] == "HTTP"
            and api["ApiEndpoint"] == "https://" + auth["DomainName"]
            and re.fullmatch(api["ApiId"] + r"\.execute-api\.eu-central-1\.amazonaws\.com", auth["DomainName"])
            and not auth.get("OriginPath") and auth["CustomOriginConfig"]["OriginProtocolPolicy"] == "https-only"
            and not collection(auth.get("CustomHeaders", {"Quantity": 0}), "origin-headers-invalid"),
            "api-origin-invalid")
    require(isinstance(api["Routes"], list) and len(api["Routes"]) == 1, "api-fallback-not-proven")
    route = exact(api["Routes"][0], {"RouteId", "RouteKey", "Target", "AuthorizationType"}, "route-fields-invalid")
    require(route["RouteKey"] == "ANY /auth/{proxy+}" and route["AuthorizationType"] == "NONE"
            and re.fullmatch(r"[a-z0-9]+", route["RouteId"])
            and re.fullmatch(r"integrations/[a-z0-9]+", route["Target"]), "api-fallback-not-proven")
    cache = exact(metadata["cache_policy"], {"Id", "CachePolicyConfig"}, "cache-policy-fields-invalid")
    cfg = cache["CachePolicyConfig"]
    require(isinstance(cfg, dict) and set(cfg) <= {"Name", "Comment", "MinTTL", "MaxTTL", "DefaultTTL",
            "ParametersInCacheKeyAndForwardedToOrigin"} and
            cfg.get("MinTTL") == cfg.get("MaxTTL") == cfg.get("DefaultTTL") == 0 and
            cache["Id"] == default["CachePolicyId"] == auth_behavior["CachePolicyId"],
            "zero-cache-policy-required")
    params = exact(cfg["ParametersInCacheKeyAndForwardedToOrigin"],
        {"EnableAcceptEncodingGzip", "EnableAcceptEncodingBrotli", "HeadersConfig", "CookiesConfig", "QueryStringsConfig"},
        "cache-forwarding-fields-invalid")
    require(params["HeadersConfig"] == {"HeaderBehavior": "none"}
            and params["CookiesConfig"] == {"CookieBehavior": "none"}
            and params["QueryStringsConfig"] == {"QueryStringBehavior": "none"},
            "public-fallback-forwards-private-data")
    s3 = exact(metadata["s3"], {"Bucket", "PublicAccessBlockConfiguration", "Policy", "OriginAccessControl"}, "s3-fields-invalid")
    bucket = text(s3["Bucket"], "bucket-invalid")
    require(re.fullmatch(r"[a-z0-9][a-z0-9.-]{1,61}[a-z0-9]", bucket)
            and private["DomainName"] == bucket + ".s3.eu-central-1.amazonaws.com"
            and not private.get("OriginPath"), "s3-origin-invalid")
    require(s3["PublicAccessBlockConfiguration"] == {
        "BlockPublicAcls": True, "IgnorePublicAcls": True,
        "BlockPublicPolicy": True, "RestrictPublicBuckets": True}, "s3-public-access-not-blocked")
    bucket_arn = "arn:aws:s3:::" + bucket
    policy = {"Version": "2012-10-17", "Statement": [
        {"Sid": "CloudFrontOnly", "Effect": "Allow", "Principal": {"Service": "cloudfront.amazonaws.com"},
         "Action": "s3:GetObject", "Resource": bucket_arn + "/*",
         "Condition": {"StringEquals": {"AWS:SourceArn": distribution_arn}}},
        {"Sid": "TLSOnly", "Effect": "Deny", "Principal": "*", "Action": "s3:*",
         "Resource": [bucket_arn, bucket_arn + "/*"],
         "Condition": {"Bool": {"aws:SecureTransport": "false"}}}]}
    require(s3["Policy"] == policy, "s3-policy-not-cloudfront-getobject-only")
    oac = exact(s3["OriginAccessControl"], {"Id", "OriginAccessControlConfig"}, "oac-fields-invalid")
    oacc = oac["OriginAccessControlConfig"]
    require(isinstance(oacc, dict) and set(oacc) <= {
        "Name", "Description", "SigningProtocol", "SigningBehavior", "OriginAccessControlOriginType"}
        and oac["Id"] == private.get("OriginAccessControlId")
        and oacc.get("SigningProtocol") == "sigv4" and oacc.get("SigningBehavior") == "always"
        and oacc.get("OriginAccessControlOriginType") == "s3", "native-oac-required")
    return default, auth_behavior


def alias_candidate(initial, legacy_arn, private_arn):
    desired = copy.deepcopy(initial)
    associations = desired["DefaultCacheBehavior"]["FunctionAssociations"]
    items = collection(associations, "associations-invalid")
    require(items == [{"EventType": "viewer-request", "FunctionARN": legacy_arn}],
            "initial-function-association-invalid")
    desired["DefaultCacheBehavior"]["FunctionAssociations"] = {
        "Quantity": 1, "Items": [{"EventType": "viewer-request", "FunctionARN": private_arn}]}
    return desired


def public_candidate(alias, public_arn):
    desired = copy.deepcopy(alias)
    default = alias["DefaultCacheBehavior"]
    # Minimal API-only fallback: no OriginRequestPolicyId, cookies, query strings,
    # auth headers, private signing bypass, or S3 origin can reach this behavior.
    behaviors = [{
        "PathPattern": path, "TargetOriginId": "regional-auth",
        "TrustedSigners": {"Enabled": False, "Quantity": 0},
        "TrustedKeyGroups": {"Enabled": False, "Quantity": 0},
        "ViewerProtocolPolicy": default["ViewerProtocolPolicy"],
        "AllowedMethods": {"Quantity": 2, "Items": ["HEAD", "GET"],
                          "CachedMethods": {"Quantity": 2, "Items": ["HEAD", "GET"]}},
        "SmoothStreaming": False, "Compress": False,
        "LambdaFunctionAssociations": {"Quantity": 0},
        "FunctionAssociations": {"Quantity": 1, "Items": [
            {"EventType": "viewer-request", "FunctionARN": public_arn}]},
        "FieldLevelEncryptionId": "", "CachePolicyId": default["CachePolicyId"],
        "ResponseHeadersPolicyId": default["ResponseHeadersPolicyId"],
        "GrpcConfig": {"Enabled": False},
    } for path in ("/", "/index.html")]
    existing = collection(desired["CacheBehaviors"], "behaviors-invalid")
    desired["CacheBehaviors"] = {"Quantity": len(existing) + 2, "Items": behaviors + existing}
    desired["DefaultRootObject"] = ""
    return desired


def prepare(metadata):
    exact(metadata, {"schema", "reviewed_source_commit", "static_baseline_commit", "operation",
        "distribution", "initial_distribution_config", "functions", "auth_api", "cache_policy",
        "s3", "application_proof"}, "metadata-fields-invalid")
    require(metadata["schema"] == SCHEMA and metadata["static_baseline_commit"] == BASELINE,
            "release-contract-mismatch")
    verify_source(metadata["reviewed_source_commit"])
    operation = metadata["operation"]
    require(operation in {"private-alias", "public-entry", "rollback-public", "rollback-alias"},
            "operation-invalid")
    dist = exact(metadata["distribution"], {"Id", "ARN", "ETag", "Status", "DistributionConfig"},
                 "distribution-fields-invalid")
    match = re.fullmatch(r"arn:aws:cloudfront::([0-9]{12}):distribution/" + DISTRIBUTION_ID, str(dist["ARN"]))
    require(match and dist["Id"] == DISTRIBUTION_ID and dist["Status"] == "Deployed",
            "distribution-identity-invalid")
    text(dist["ETag"], "distribution-etag-required")
    exact(metadata["functions"], FUNCTIONS, "function-set-invalid")
    functions = {key: function_metadata(value, key, match[1]) for key, value in metadata["functions"].items()}
    initial = metadata["initial_distribution_config"]
    validate_topology(metadata, initial, dist["ARN"])
    alias = alias_candidate(initial, functions["legacy"]["arn"], functions["private"]["arn"])
    public = public_candidate(alias, functions["public"]["arn"])
    pairs = {"private-alias": (initial, alias), "public-entry": (alias, public),
             "rollback-public": (public, alias), "rollback-alias": (alias, initial)}
    before, candidate = pairs[operation]
    require(dist["DistributionConfig"] == before, "live-distribution-drift")
    proof = metadata["application_proof"]
    if operation == "public-entry":
        exact(proof, {"source_commit", "backend_revision_id", "backend_image_sha256", "app_js_sha256",
                      "report_sha256", "alias_config_sha256"}, "application-proof-required")
        require(proof["source_commit"] == metadata["reviewed_source_commit"]
                and proof["alias_config_sha256"] == sha(alias), "application-proof-mismatch")
        text(proof["backend_revision_id"], "backend-revision-required")
        for field in ("backend_image_sha256", "app_js_sha256", "report_sha256"):
            digest(proof[field], "application-proof-digest-invalid")
    else:
        require(proof is None, "unexpected-application-proof")
    # These copies are exact before/candidate images, not Terraform convergence.
    return {
        "schema": "aws_mesouq.secure_cold_entry_plan.v1",
        "operation": operation, "reviewed_source_commit": metadata["reviewed_source_commit"],
        "static_baseline_commit": BASELINE, "functions": functions,
        "expected_distribution_config_sha256": sha(before),
        "candidate_distribution_config_sha256": sha(candidate),
        "expected_distribution_etag": dist["ETag"],
        "request": {"Id": dist["Id"], "IfMatch": dist["ETag"], "DistributionConfig": candidate},
        "rollback_guard": {"expected_live_config_sha256": sha(candidate),
                           "restore_config_sha256": sha(before),
                           "recapture_live_etag_required": True,
                           "next_operation": {"private-alias": "rollback-alias",
                                "public-entry": "rollback-public",
                                "rollback-public": "public-entry",
                                "rollback-alias": "private-alias"}[operation]},
        "application_proof_sha256": sha(proof) if proof else None,
        "aws_calls_made": False, "owner_action_approval_required": True,
        "native_positive_and_negative_proof_required": True,
    }


def no_duplicates(pairs):
    value = {}
    for key, item in pairs:
        require(key not in value, "duplicate-json-field")
        value[key] = item
    return value


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--metadata", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    try:
        require(args.metadata.is_file() and not args.metadata.is_symlink()
                and args.metadata.stat().st_size <= 1024 * 1024, "bounded-metadata-required")
        value = json.loads(args.metadata.read_text(), object_pairs_hook=no_duplicates,
                           parse_constant=lambda _: (_ for _ in ()).throw(ValueError("nonfinite-json")))
        plan = prepare(value)
        path = args.output.absolute()
        require(path.parent.resolve() == path.parent and not path.is_symlink(), "canonical-output-required")
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        with os.fdopen(fd, "wb") as stream:
            stream.write(canonical(plan) + b"\n")
            stream.flush()
            os.fsync(stream.fileno())
    except (OSError, ValueError, KeyError, TypeError):
        print(json.dumps({"status": "rejected", "category": "cold-entry-metadata-invalid"}))
        return 1
    print(json.dumps({key: plan[key] for key in ("schema", "operation",
        "reviewed_source_commit", "expected_distribution_config_sha256",
        "candidate_distribution_config_sha256", "aws_calls_made")}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
