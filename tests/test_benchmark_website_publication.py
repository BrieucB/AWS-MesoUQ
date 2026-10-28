"""Offline Benchmark Lab static rollout: synthetic destinations and fake AWS only."""
import importlib.util
import json
from pathlib import Path
import re
import tempfile
import textwrap
import unittest

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("website_publication_fixtures", ROOT / "tests/test_website_publication.py")
fixtures = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(fixtures)
publication = fixtures.publication
WORKFLOW = (ROOT / ".github/workflows/publish-benchmark-website.yml").read_text()


class BenchmarkPublicationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.baseline_dir, self.dist = self.root / "baseline", self.root / "dist"
        self.baseline = {key: ("baseline " + key).encode() for key in publication.BENCHMARK_BASE_KEYS}
        self.target = dict(self.baseline)
        self.target.update({key: ("target " + key).encode() for key in publication.BENCHMARK_ORDER})
        for directory, assets in ((self.baseline_dir, self.baseline), (self.dist, self.target)):
            for key, raw in assets.items():
                (directory / key).parent.mkdir(parents=True, exist_ok=True)
                (directory / key).write_bytes(raw)
        self.aws = fixtures.FakeAws(self.baseline)

    def publish(self):
        return publication.publish(self.dist, self.baseline_dir, "synthetic-bucket", fixtures.ROLE,
                                   aws=self.aws, profile="benchmark")

    def writes(self):
        return [args[args.index("--key") + 1] for args in self.aws.calls
                if args[:2] == ("s3api", "put-object")]

    def test_exact_five_conditional_writes_dependencies_first_entries_last(self):
        private = self.aws.objects[publication.CONFIG][0]
        summary = self.publish()
        self.assertEqual(summary["assets_verified"], 13)
        self.assertEqual(summary["conditional_writes"], 5)
        self.assertEqual(self.writes(), list(publication.BENCHMARK_ORDER))
        puts = [args for args in self.aws.calls if args[:2] == ("s3api", "put-object")]
        self.assertTrue(all("--if-none-match" in args for args in puts[:3]))
        self.assertTrue(all("--if-match" in args for args in puts[3:]))
        self.assertEqual(self.aws.objects[publication.CONFIG][0], private)
        for key in publication.BENCHMARK_BASE_KEYS:
            if key not in publication.BENCHMARK_ORDER and key != publication.CONFIG:
                self.assertEqual(self.aws.objects[key][0], self.baseline[key])
        self.assertNotIn("synthetic-bucket", json.dumps(summary))
        self.assertNotIn("private-live", json.dumps(summary))

    def test_partial_and_idempotent_reruns(self):
        for key in publication.BENCHMARK_NEW_KEYS[:2]:
            self.aws.store(key, self.target[key])
        first = self.publish()
        self.assertEqual(first["conditional_writes"], 3)
        self.assertEqual(self.writes(), list(publication.BENCHMARK_ORDER[2:]))
        self.aws.calls.clear()
        second = self.publish()
        self.assertEqual(second["conditional_writes"], 0)
        self.assertEqual(first["manifest_sha256"], second["manifest_sha256"])

    def test_app_already_target_index_baseline_recovery(self):
        for key in publication.BENCHMARK_ORDER[:-1]:
            self.aws.store(key, self.target[key])
        self.publish()
        self.assertEqual(self.writes(), ["index.html"])

    def test_stale_existing_or_new_asset_rejected_before_writes(self):
        for key in ("src/posterior.js", "src/app.js", *publication.BENCHMARK_NEW_KEYS):
            with self.subTest(key=key):
                self.aws = fixtures.FakeAws(self.baseline)
                self.aws.store(key, b"x" * len(self.target[key]))
                with self.assertRaises(publication.PublicationError):
                    self.publish()
                self.assertEqual(self.writes(), [])

    def test_untouched_build_change_rejected_before_identity(self):
        (self.dist / "src/style.css").write_bytes(b"unreviewed-change")
        with self.assertRaisesRegex(publication.PublicationError, "unexpected-build-delta"):
            self.publish()
        self.assertEqual(self.aws.calls, [])

    def test_root_and_iam_user_rejected_before_s3(self):
        for arn in ("arn:aws:iam::123456789012:root", "arn:aws:iam::123456789012:user/offline"):
            self.aws.calls.clear()
            self.aws.identity["Arn"] = arn
            with self.assertRaisesRegex(publication.PublicationError, "identity-rejected"):
                self.publish()
            self.assertEqual(len(self.aws.calls), 1)

    def test_dependency_failure_keeps_both_entries_unchanged(self):
        self.aws.race = publication.BENCHMARK_NEW_KEYS[1]
        with self.assertRaisesRegex(publication.PublicationError, "conditional-write-failed"):
            self.publish()
        for key in ("src/app.js", "index.html"):
            self.assertEqual(self.aws.objects[key][0], self.baseline[key])

    def test_private_config_concurrent_change_rejected_not_overwritten(self):
        original = self.aws
        def change(*args, **kwargs):
            response = original(*args, **kwargs)
            if args[:2] == ("s3api", "put-object") and args[args.index("--key") + 1] == "index.html":
                original.store(publication.CONFIG, b"concurrent-private-config")
            return response
        with self.assertRaises(publication.PublicationError):
            publication.publish(self.dist, self.baseline_dir, "synthetic-bucket", fixtures.ROLE,
                                aws=change, profile="benchmark")
        self.assertNotIn(publication.CONFIG, self.writes())

    def test_bad_metadata_rejected_before_writes(self):
        for field, value in (("ChecksumSHA256", "invalid"), ("ContentType", "image/svg+xml"),
                             ("ServerSideEncryption", "aws:kms"), ("CacheControl", "public")):
            self.aws = fixtures.FakeAws(self.baseline)
            self.aws.objects["src/posterior.js"][1][field] = value
            with self.assertRaises(publication.PublicationError):
                self.publish()
            self.assertEqual(self.writes(), [])

    def test_unknown_profile_rejected_without_aws_access(self):
        with self.assertRaisesRegex(publication.PublicationError, "profile-invalid"):
            publication.publish(self.dist, self.baseline_dir, "synthetic-bucket", fixtures.ROLE,
                                aws=self.aws, profile="unreviewed")
        self.assertEqual(self.aws.calls, [])


class BenchmarkWorkflowTests(unittest.TestCase):
    def test_exact_read13_write5_and_bucket_only_listing(self):
        policy_text = WORKFLOW.split("          PUBLICATION_SESSION_POLICY: >-\n", 1)[1].split("        run:", 1)[0]
        policy = json.loads(textwrap.dedent(policy_text))
        statements = {item["Action"]: item for item in policy["Statement"]}
        prefix = "arn:aws:s3:::" + chr(36) + "{{ env.WEBSITE_BUCKET }}"
        self.assertEqual(set(statements), {"s3:ListBucket", "s3:GetObject", "s3:PutObject"})
        self.assertEqual(statements["s3:ListBucket"]["Resource"], prefix)
        self.assertEqual(set(statements["s3:GetObject"]["Resource"]),
                         {prefix + "/" + key for key in (*publication.BENCHMARK_BASE_KEYS, *publication.BENCHMARK_NEW_KEYS)})
        self.assertEqual(statements["s3:PutObject"]["Resource"],
                         [prefix + "/" + key for key in publication.BENCHMARK_ORDER])
        self.assertNotIn("*", policy_text)
        self.assertNotIn("Delete", policy_text)

    def test_same_private_admission_and_auth_boundary_as_posterior(self):
        original = (ROOT / ".github/workflows/publish-website.yml").read_text()
        admission = lambda source: re.search(r"python3 - <<'PY'\n(.*?)\n          PY", source, re.S)[1]
        self.assertEqual(admission(WORKFLOW), admission(original))
        self.assertIn("--profile benchmark", WORKFLOW)
        self.assertIn("e1bd3f925389d1771d327924e357d2155c65c36a", WORKFLOW)
        self.assertIn("group: aws-website-publication", WORKFLOW)
        self.assertIn("cancel-in-progress: false", WORKFLOW)
        self.assertIn("environment: aws-publishing", WORKFLOW)
        self.assertIn("if: github.ref == 'refs/heads/main' && github.event_name == 'workflow_dispatch'", WORKFLOW)
        self.assertNotIn("upload-artifact", WORKFLOW)
        self.assertNotIn(chr(36) + "{{ inputs.", WORKFLOW)
        self.assertIn("--phase cleanup", WORKFLOW)
        self.assertIn("if: always() && steps.aws_action.outcome == 'success' && steps.aws_node.outcome == 'success'", WORKFLOW)
        self.assertEqual(re.findall(r"uses: [^@\n]+@([^\s]+)", WORKFLOW),
                         re.findall(r"uses: [^@\n]+@([^\s]+)", original))


if __name__ == "__main__":
    unittest.main()
