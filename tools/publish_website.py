#!/usr/bin/env python3
"""Publish a reviewed, bounded static website delta using a scoped AWS role."""
import argparse
import base64
import hashlib
import json
from pathlib import Path
import re
import subprocess
import tempfile

BASE_KEYS = (
    "index.html", "src/app.js", "src/client.js", "src/defaults.js",
    "src/plots.js", "src/style.css", "src/curve-editor.js",
    "presets/indentation-v1.json", "runtime-config.json",
)
ORDER = ("src/posterior.js", "src/style.css", "src/app.js")
CONFIG = "runtime-config.json"
BENCHMARK_NEW_KEYS = ("src/benchmark-figures.js", "src/benchmark-lab.js", "src/benchmark-lab.css")
BENCHMARK_BASE_KEYS = (*BASE_KEYS, "src/posterior.js")
BENCHMARK_ORDER = (*BENCHMARK_NEW_KEYS, "src/app.js", "index.html")
REMEMBERED_BASE_KEYS = (*BENCHMARK_BASE_KEYS, *BENCHMARK_NEW_KEYS)
REMEMBERED_NEW_KEYS = ("src/site-session.js",)
REMEMBERED_ORDER = (*REMEMBERED_NEW_KEYS, "src/client.js", "src/style.css", "src/app.js")
PRIVATE_ENTRY_KEYS = (*REMEMBERED_BASE_KEYS, *REMEMBERED_NEW_KEYS)
ACCURACY_NEW_KEYS = ("src/benchmark-accuracy.js", "src/benchmark-workspace.js")
ACCURACY_ORDER = ("src/benchmark-figures.js", *ACCURACY_NEW_KEYS, "src/benchmark-lab.css", "src/app.js")
PROFILES = {
    "accuracy": (PRIVATE_ENTRY_KEYS, ACCURACY_NEW_KEYS, ACCURACY_ORDER),
    "private-entry": (PRIVATE_ENTRY_KEYS, (), ("src/app.js",)),
    "posterior": (BASE_KEYS, (ORDER[0],), ORDER),
    "benchmark": (BENCHMARK_BASE_KEYS, BENCHMARK_NEW_KEYS, BENCHMARK_ORDER),
    "remembered-session": (REMEMBERED_BASE_KEYS, REMEMBERED_NEW_KEYS, REMEMBERED_ORDER),
}


class PublicationError(RuntimeError):
    """Contains only a public stage/category, never destination data."""


def require(condition, category):
    if not condition:
        raise PublicationError(category)


def fingerprint(raw):
    return {"size": len(raw), "sha256": hashlib.sha256(raw).hexdigest()}


def checksum(raw):
    return base64.b64encode(hashlib.sha256(raw).digest()).decode("ascii")


def content_type(key):
    return ("text/javascript" if key.endswith(".js") else
            "text/css" if key.endswith(".css") else
            "text/html" if key.endswith(".html") else "application/json")


class AwsCli:
    def __call__(self, *args, absent_ok=False):
        try:
            result = subprocess.run(
                ["aws", "--no-cli-pager", "--output", "json", *map(str, args)],
                capture_output=True, text=True, timeout=120, check=False,
            )
        except (OSError, subprocess.TimeoutExpired):
            raise PublicationError("aws-command-failed") from None
        if result.returncode:
            if (absent_ok and args[:2] == ("s3api", "head-object") and
                    re.search(r"An error occurred \(404\) when calling the HeadObject operation:",
                              result.stderr)):
                return None
            raise PublicationError("aws-command-failed")
        try:
            value = json.loads(result.stdout or "{}")
            require(isinstance(value, dict), "aws-response-invalid")
            return value
        except (ValueError, TypeError):
            raise PublicationError("aws-response-invalid") from None


def read_build(directory, keys):
    directory = Path(directory)
    require(directory.is_dir() and not directory.is_symlink(), "build-invalid")
    entries = list(directory.rglob("*"))
    require(not any(path.is_symlink() for path in entries), "build-invalid")
    actual = {path.relative_to(directory).as_posix() for path in entries if path.is_file()}
    require(actual == set(keys), "build-assets-invalid")
    return {key: (directory / key).read_bytes() for key in keys}


def verify_identity(aws, expected):
    role = re.fullmatch(r"arn:(aws|aws-us-gov|aws-cn):iam::([0-9]{12}):role/([A-Za-z0-9_+=,.@/-]+)", expected)
    require(role is not None, "publication-role-invalid")
    identity = aws("sts", "get-caller-identity")
    partition, account, path = role.groups()
    role_name = path.rsplit("/", 1)[-1]
    require(bool(role_name), "publication-role-invalid")
    assumed = re.fullmatch(
        r"arn:" + re.escape(partition) + r":sts::" + account +
        r":assumed-role/" + re.escape(role_name) + r"/[A-Za-z0-9_+=,.@-]+",
        str(identity.get("Arn", "")),
    )
    require(identity.get("Account") == account and assumed is not None,
            "publication-identity-rejected")


def verify_metadata(head, raw, key):
    require(isinstance(head, dict), "object-metadata-invalid")
    require(isinstance(head.get("ETag"), str) and bool(head["ETag"]), "object-etag-invalid")
    require(type(head.get("ContentLength")) is int and head["ContentLength"] == len(raw),
            "object-size-invalid")
    require(head.get("ChecksumSHA256") == checksum(raw), "object-checksum-invalid")
    require(head.get("ServerSideEncryption") == "AES256", "object-encryption-invalid")
    require(head.get("CacheControl") == "no-store", "object-cache-invalid")
    require(head.get("ContentType") == content_type(key), "object-type-invalid")
    require(not head.get("Metadata") and not head.get("ContentEncoding") and
            not head.get("WebsiteRedirectLocation"), "object-metadata-invalid")


def publish(dist, baseline_dist, bucket, expected_role_arn, aws=None, profile="posterior"):
    """Verify scoped identity, preflight exact profile objects, conditionally write/read back."""
    aws = aws or AwsCli()
    require(profile in PROFILES, "publication-profile-invalid")
    base_keys, new_keys, order = PROFILES[profile]
    baseline = read_build(baseline_dist, base_keys)
    target = read_build(dist, (*base_keys, *new_keys))
    require(isinstance(bucket, str) and re.fullmatch(r"[a-z0-9][a-z0-9.-]{1,61}[a-z0-9]", bucket),
            "bucket-invalid")
    for key in base_keys:
        if key not in order and key != CONFIG:
            require(target[key] == baseline[key], "unexpected-build-delta")
    require(all(target[key] != baseline[key] for key in order if key not in new_keys), "expected-build-delta-missing")
    if profile == "private-entry":
        require(target[CONFIG] == baseline[CONFIG], "unexpected-build-delta")
    verify_identity(aws, expected_role_arn)
    with tempfile.TemporaryDirectory(prefix="mesouq-static-") as temporary:
        scratch = Path(temporary)
        scratch.chmod(0o700)

        def inspect(key, allowed, absent=False):
            head = aws("s3api", "head-object", "--bucket", bucket, "--key", key,
                       "--checksum-mode", "ENABLED", absent_ok=absent)
            if head is None:
                require(absent, "object-missing")
                return None, None
            size = head.get("ContentLength")
            require(type(size) is int and 0 <= size <= 16 * 1024 * 1024,
                    "object-size-invalid")
            if allowed is not None:
                require(size in {len(raw) for raw in allowed if raw is not None},
                        "object-size-invalid")
            output = scratch / "download"
            output.touch(mode=0o600, exist_ok=True)
            got = aws("s3api", "get-object", "--bucket", bucket, "--key", key,
                      "--if-match", head.get("ETag", ""), "--checksum-mode", "ENABLED", str(output))
            raw = output.read_bytes()
            verify_metadata(head, raw, key)
            verify_metadata(got, raw, key)
            require(got["ETag"] == head["ETag"], "object-changed")
            require(allowed is None or raw in allowed, "unexpected-live-bytes")
            output.unlink()
            return head, raw

        before = {}
        for key in target:
            allowed = None if key == CONFIG else (baseline.get(key), target[key])
            before[key] = inspect(key, allowed, absent=key in new_keys)
        runtime = before[CONFIG][1]
        require(runtime is not None, "runtime-config-missing")
        writes = 0
        expected_etags = {}
        for key in order:
            head, raw = before[key]
            if raw == target[key]:
                expected_etags[key] = head["ETag"]
                continue
            body = scratch / "upload"
            body.touch(mode=0o600, exist_ok=False)
            body.write_bytes(target[key])
            condition = ("--if-match", head["ETag"]) if head else ("--if-none-match", "*")
            result = aws(
                "s3api", "put-object", "--bucket", bucket, "--key", key,
                "--body", str(body), "--cache-control", "no-store",
                "--content-type", content_type(key), "--server-side-encryption", "AES256",
                "--checksum-algorithm", "SHA256", "--checksum-sha256", checksum(target[key]),
                *condition,
            )
            require(result.get("ChecksumSHA256") == checksum(target[key]), "write-checksum-invalid")
            require(isinstance(result.get("ETag"), str) and bool(result["ETag"]), "write-etag-invalid")
            expected_etags[key] = result["ETag"]
            writes += 1
            body.unlink()
        manifest = {}
        for key in target:
            expected = runtime if key == CONFIG else target[key]
            head, raw = inspect(key, (expected,))
            etag = expected_etags[key] if key in expected_etags else before[key][0]["ETag"]
            require(head["ETag"] == etag, "object-changed")
            manifest[key] = fingerprint(raw)
        digest = hashlib.sha256(json.dumps(manifest, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
        return {"assets_verified": len(manifest), "conditional_writes": writes,
                "manifest_sha256": digest}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dist", required=True)
    parser.add_argument("--baseline-dist", required=True)
    parser.add_argument("--bucket", required=True)
    parser.add_argument("--expected-role-arn", required=True)
    parser.add_argument("--profile", choices=tuple(PROFILES), default="posterior")
    args = parser.parse_args()
    try:
        summary = publish(args.dist, args.baseline_dist, args.bucket, args.expected_role_arn, profile=args.profile)
    except PublicationError as error:
        print(json.dumps({"status": "failed", "category": str(error)}))
        return 1
    except Exception:
        print(json.dumps({"status": "failed", "category": "publication-failed"}))
        return 1
    print(json.dumps({"status": "verified", **summary}, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
