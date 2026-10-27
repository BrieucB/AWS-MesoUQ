"""Offline tests for the bounded static publication protocol; no AWS calls."""
import base64
import hashlib
import importlib.util
import json
from pathlib import Path
import stat
import tempfile
import unittest
from unittest.mock import patch
from types import SimpleNamespace

SPEC = importlib.util.spec_from_file_location("publish_website", Path(__file__).resolve().parents[1] / "tools/publish_website.py")
publication = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(publication)
ROLE = "arn:aws:iam::123456789012:role/scoped/static-publisher"
ASSUMED = "arn:aws:sts::123456789012:assumed-role/static-publisher/offline-test"


class FakeAws:
    def __init__(self, baseline):
        self.calls = []
        self.objects = {}
        self.identity = {"Account": "123456789012", "Arn": ASSUMED}
        self.race = None
        for key, raw in baseline.items():
            self.store(key, b"private-live-config" if key == publication.CONFIG else raw)

    def store(self, key, raw):
        metadata = {
            "ContentLength": len(raw), "ETag": '"' + hashlib.md5(raw).hexdigest() + '"',
            "ChecksumSHA256": publication.checksum(raw), "ServerSideEncryption": "AES256",
            "CacheControl": "no-store", "ContentType": publication.content_type(key), "Metadata": {},
        }
        self.objects[key] = [raw, metadata]

    def __call__(self, *args, absent_ok=False):
        self.calls.append(args)
        if args[:2] == ("sts", "get-caller-identity"):
            return self.identity
        key = args[args.index("--key") + 1]
        operation = args[1]
        if operation == "head-object":
            if key not in self.objects:
                if absent_ok:
                    return None
                raise publication.PublicationError("object-missing")
            return dict(self.objects[key][1])
        if operation == "get-object":
            raw, meta = self.objects[key]
            if args[args.index("--if-match") + 1] != meta["ETag"]:
                raise publication.PublicationError("conditional-read-failed")
            Path(args[-1]).write_bytes(raw)
            return dict(meta)
        if operation == "put-object":
            if self.race == key:
                raise publication.PublicationError("conditional-write-failed")
            if "--if-none-match" in args:
                assert args[args.index("--if-none-match") + 1] == "*"
                assert key not in self.objects
            else:
                assert args[args.index("--if-match") + 1] == self.objects[key][1]["ETag"]
            path = Path(args[args.index("--body") + 1])
            assert stat.S_IMODE(path.stat().st_mode) == 0o600
            raw = path.read_bytes()
            assert args[args.index("--checksum-algorithm") + 1] == "SHA256"
            assert args[args.index("--checksum-sha256") + 1] == publication.checksum(raw)
            assert args[args.index("--server-side-encryption") + 1] == "AES256"
            assert args[args.index("--cache-control") + 1] == "no-store"
            assert args[args.index("--content-type") + 1] == publication.content_type(key)
            self.store(key, raw)
            return dict(self.objects[key][1])
        raise AssertionError("unexpected operation")


class PublicationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.baseline_dir, self.dist = self.root / "baseline", self.root / "dist"
        self.baseline = {key: ("baseline " + key).encode() for key in publication.BASE_KEYS}
        self.target = dict(self.baseline)
        self.target.update({key: ("target " + key).encode() for key in publication.ORDER})
        for directory, assets in ((self.baseline_dir, self.baseline), (self.dist, self.target)):
            for key, raw in assets.items():
                (directory / key).parent.mkdir(parents=True, exist_ok=True)
                (directory / key).write_bytes(raw)
        self.aws = FakeAws(self.baseline)

    def publish(self):
        return publication.publish(self.dist, self.baseline_dir, "synthetic-bucket", ROLE, aws=self.aws)

    def writes(self):
        return [args for args in self.aws.calls if args[:2] == ("s3api", "put-object")]

    def test_root_user_wrong_role_and_wrong_account_rejected_before_s3(self):
        for arn, account in [
            ("arn:aws:iam::123456789012:root", "123456789012"),
            ("arn:aws:iam::123456789012:user/offline", "123456789012"),
            (ASSUMED.replace("static-publisher", "other"), "123456789012"),
            (ASSUMED, "999999999999"),
        ]:
            with self.subTest(arn=arn):
                self.aws.calls.clear()
                self.aws.identity = {"Arn": arn, "Account": account}
                with self.assertRaisesRegex(publication.PublicationError, "identity-rejected"):
                    self.publish()
                self.assertEqual(len(self.aws.calls), 1)

    def test_conditional_order_checksums_private_config_and_readback(self):
        original = self.aws.objects[publication.CONFIG][0]
        summary = self.publish()
        self.assertEqual(summary["conditional_writes"], 3)
        self.assertEqual(summary["assets_verified"], 10)
        self.assertRegex(summary["manifest_sha256"], r"^[a-f0-9]{64}$")
        writes = self.writes()
        self.assertEqual([args[args.index("--key") + 1] for args in writes], list(publication.ORDER))
        self.assertIn("--if-none-match", writes[0])
        self.assertTrue(all("--if-match" in args for args in writes[1:]))
        self.assertEqual(self.aws.objects[publication.CONFIG][0], original)
        self.assertEqual(sum(args[:2] == ("s3api", "get-object") for args in self.aws.calls), 19)
        self.assertNotIn("synthetic-bucket", json.dumps(summary))
        self.assertNotIn("private-live", json.dumps(summary))

    def test_exact_target_rerun_performs_zero_writes(self):
        first = self.publish()
        self.aws.calls.clear()
        second = self.publish()
        self.assertEqual(second["conditional_writes"], 0)
        self.assertEqual(second["manifest_sha256"], first["manifest_sha256"])

    def test_partial_rollout_skips_verified_new_module(self):
        self.aws.store(publication.ORDER[0], self.target[publication.ORDER[0]])
        result = self.publish()
        self.assertEqual(result["conditional_writes"], 2)
        self.assertEqual([args[args.index("--key") + 1] for args in self.writes()], list(publication.ORDER[1:]))

    def test_unexpected_bytes_or_metadata_reject_before_writes(self):
        for mode in ("bytes", "mime", "checksum", "cache", "encryption", "metadata"):
            with self.subTest(mode=mode):
                self.aws = FakeAws(self.baseline)
                key = "src/client.js"
                if mode == "bytes":
                    self.aws.store(key, b"unexpected")
                else:
                    field, value = {
                        "mime": ("ContentType", "text/html"),
                        "checksum": ("ChecksumSHA256", "invalid"),
                        "cache": ("CacheControl", "public"),
                        "encryption": ("ServerSideEncryption", "aws:kms"),
                        "metadata": ("Metadata", {"unexpected": "value"}),
                    }[mode]
                    self.aws.objects[key][1][field] = value
                with self.assertRaises(publication.PublicationError):
                    self.publish()
                self.assertEqual(self.writes(), [])

    def test_existing_unexpected_new_module_rejects_without_overwriting(self):
        self.aws.store(publication.ORDER[0], b"x" * len(self.target[publication.ORDER[0]]))
        with self.assertRaisesRegex(publication.PublicationError, "unexpected-live-bytes"):
            self.publish()
        self.assertEqual(self.writes(), [])

    def test_conditional_failure_leaves_application_entry_unchanged(self):
        self.aws.race = "src/style.css"
        with self.assertRaisesRegex(publication.PublicationError, "conditional-write-failed"):
            self.publish()
        self.assertEqual(self.aws.objects["src/app.js"][0], self.baseline["src/app.js"])

    def test_runtime_config_changed_during_rollout_fails_readback(self):
        original_aws = self.aws
        def concurrent_change(*args, **kwargs):
            response = original_aws(*args, **kwargs)
            if args[:2] == ("s3api", "put-object") and args[args.index("--key") + 1] == "src/app.js":
                original_aws.store(publication.CONFIG, b"changed-private-config")
            return response
        with self.assertRaises(publication.PublicationError):
            publication.publish(self.dist, self.baseline_dir, "synthetic-bucket", ROLE, aws=concurrent_change)
        self.assertNotIn(publication.CONFIG, [args[args.index("--key") + 1] for args in self.writes()])

    def test_unexpected_head_size_fails_before_downloading_object(self):
        key = "index.html"
        self.aws.objects[key][1]["ContentLength"] = 1024 * 1024 * 1024
        with self.assertRaisesRegex(publication.PublicationError, "object-size-invalid"):
            self.publish()
        self.assertEqual(sum(args[:2] == ("s3api", "get-object") for args in self.aws.calls), 0)
        self.assertEqual(self.writes(), [])

    def test_unreviewed_build_asset_or_delta_rejected_before_identity(self):
        (self.dist / "src/client.js").write_bytes(b"unreviewed")
        with self.assertRaisesRegex(publication.PublicationError, "unexpected-build-delta"):
            self.publish()
        self.assertEqual(self.aws.calls, [])

    def test_symlink_build_rejected(self):
        path = self.dist / "src/posterior.js"
        path.unlink()
        path.symlink_to(self.dist / "src/app.js")
        with self.assertRaisesRegex(publication.PublicationError, "build-invalid"):
            self.publish()
        self.assertEqual(self.aws.calls, [])

    def test_cli_404_is_the_only_absence_and_failures_are_sanitized(self):
        cli = publication.AwsCli()
        errors = [
            "An error occurred (AccessDenied) when calling the HeadObject operation: secret",
            "An error occurred (404) when calling the GetObject operation: secret",
        ]
        for error in errors:
            with patch.object(publication.subprocess, "run", return_value=SimpleNamespace(returncode=1, stdout="", stderr=error)):
                with self.assertRaisesRegex(publication.PublicationError, "^aws-command-failed$"):
                    cli("s3api", "head-object", absent_ok=True)
        error = "An error occurred (404) when calling the HeadObject operation: Not Found"
        with patch.object(publication.subprocess, "run", return_value=SimpleNamespace(returncode=1, stdout="", stderr=error)) as run:
            self.assertIsNone(cli("s3api", "head-object", absent_ok=True))
            self.assertEqual(run.call_args.args[0][:4], ["aws", "--no-cli-pager", "--output", "json"])


if __name__ == "__main__":
    unittest.main()
