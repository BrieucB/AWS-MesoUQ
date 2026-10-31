"""One-file private-entry publication; fake AWS only and actual Node build gate."""
import importlib.util
import json
import os
from pathlib import Path
import re
import tempfile
import textwrap
import unittest

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("publication_fixtures", ROOT / "tests/test_website_publication.py")
fixtures = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(fixtures)
publication = fixtures.publication
KEYS = (*publication.REMEMBERED_BASE_KEYS, *publication.REMEMBERED_NEW_KEYS)
PROFILE = "private-entry"
WORKFLOW = (ROOT / ".github/workflows/publish-private-entry-website.yml").read_text()


def materialize(directory, assets):
    for key, raw in assets.items():
        path = directory / key
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(raw)


class PrivateEntryPublicationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.baseline_dir, self.dist = self.root / "baseline", self.root / "target"
        self.baseline = {key: ("baseline " + key).encode() for key in KEYS}
        self.target = dict(self.baseline, **{"src/app.js": b"target history normalization"})
        materialize(self.baseline_dir, self.baseline)
        materialize(self.dist, self.target)
        self.aws = fixtures.FakeAws(self.baseline)

    def publish(self, aws=None):
        return publication.publish(self.dist, self.baseline_dir, "synthetic-bucket",
                                   fixtures.ROLE, aws=aws or self.aws, profile=PROFILE)

    def puts(self):
        return [call for call in self.aws.calls if call[:2] == ("s3api", "put-object")]

    def test_one_conditional_write_preserves_other13_bytes_etags_and_reads_all14_twice(self):
        before = {key: (raw, dict(meta)) for key, (raw, meta) in self.aws.objects.items()}
        result = self.publish()
        self.assertEqual(result["assets_verified"], 14)
        self.assertEqual(result["conditional_writes"], 1)
        self.assertEqual(len(self.puts()), 1)
        self.assertIn("--if-match", self.puts()[0])
        self.assertNotIn("--if-none-match", self.puts()[0])
        self.assertEqual(self.puts()[0][self.puts()[0].index("--key") + 1], "src/app.js")
        for key in KEYS:
            if key != "src/app.js":
                self.assertEqual(self.aws.objects[key], list(before[key]))
        gets = [call for call in self.aws.calls if call[:2] == ("s3api", "get-object")]
        self.assertEqual(len(gets), 28)
        self.assertEqual({call[call.index("--key") + 1] for call in gets}, set(KEYS))

    def test_idempotent_target_rerun_has_zero_writes(self):
        first = self.publish()
        self.aws.calls.clear()
        second = self.publish()
        self.assertEqual(second["conditional_writes"], 0)
        self.assertEqual(first["manifest_sha256"], second["manifest_sha256"])
        self.assertEqual(self.puts(), [])

    def test_unreviewed_build_deltas_including_runtime_extra_or_missing_fail_before_aws(self):
        for key in ("index.html", "runtime-config.json", "src/site-session.js", "extra.js"):
            with self.subTest(key=key):
                path = self.dist / key
                previous = path.read_bytes() if path.exists() else None
                path.write_bytes(b"unreviewed")
                self.aws.calls.clear()
                with self.assertRaises(publication.PublicationError):
                    self.publish()
                self.assertEqual(self.aws.calls, [])
                if previous is None:
                    path.unlink()
                else:
                    path.write_bytes(previous)
        (self.dist / "src/app.js").write_bytes(self.baseline["src/app.js"])
        with self.assertRaisesRegex(publication.PublicationError, "expected-build-delta-missing"):
            self.publish()
        self.assertEqual(self.aws.calls, [])

    def test_stale_live_bytes_metadata_or_missing_object_fail_before_write(self):
        for mode in ("bytes", "checksum", "missing"):
            with self.subTest(mode=mode):
                self.aws = fixtures.FakeAws(self.baseline)
                if mode == "bytes":
                    self.aws.store("src/app.js", b"unreviewed")
                elif mode == "checksum":
                    self.aws.objects["src/site-session.js"][1]["ChecksumSHA256"] = "bad"
                else:
                    del self.aws.objects["index.html"]
                with self.assertRaises(publication.PublicationError):
                    self.publish()
                self.assertEqual(self.puts(), [])

    def test_conditional_write_race_keeps_original_entry(self):
        self.aws.race = "src/app.js"
        with self.assertRaisesRegex(publication.PublicationError, "conditional-write-failed"):
            self.publish()
        self.assertEqual(self.aws.objects["src/app.js"][0], self.baseline["src/app.js"])

    def test_readback_config_etag_race_is_detected_without_any_config_write(self):
        original = self.aws
        def race(*args, **kwargs):
            response = original(*args, **kwargs)
            if args[:2] == ("s3api", "put-object"):
                original.objects["runtime-config.json"][1]["ETag"] = '"concurrent-etag"'
            return response
        with self.assertRaisesRegex(publication.PublicationError, "object-changed"):
            self.publish(aws=race)
        self.assertTrue(all(call[call.index("--key") + 1] == "src/app.js" for call in self.puts()))

    def test_existing_profiles_reject_the_new_single_file_contract_before_credentials(self):
        for profile in ("posterior", "benchmark", "remembered-session"):
            self.aws.calls.clear()
            with self.assertRaises(publication.PublicationError):
                publication.publish(self.dist, self.baseline_dir, "synthetic-bucket",
                    fixtures.ROLE, aws=self.aws, profile=profile)
            self.assertEqual(self.aws.calls, [])


class PrivateEntryWorkflowTests(unittest.TestCase):
    def test_exact14_read_resources_and_one_write_without_list_wildcards_or_delete(self):
        raw = WORKFLOW.split("          PUBLICATION_SESSION_POLICY: >-\n", 1)[1].split("        run:", 1)[0]
        policy = json.loads(textwrap.dedent(raw))
        self.assertEqual(len(policy["Statement"]), 2)
        statements = {item["Action"]: item for item in policy["Statement"]}
        prefix = "arn:aws:s3:::" + chr(36) + "{{ env.WEBSITE_BUCKET }}/"
        self.assertEqual(set(statements), {"s3:GetObject", "s3:PutObject"})
        self.assertEqual(set(statements["s3:GetObject"]["Resource"]), {prefix + key for key in KEYS})
        self.assertEqual(statements["s3:PutObject"]["Resource"], [prefix + "src/app.js"])
        self.assertNotIn("*", raw)
        self.assertNotIn("Delete", raw)
        self.assertEqual(publication.PROFILES[PROFILE], (KEYS, (), ("src/app.js",)))

    def test_fixed_baseline_actual_build_gate_and_existing_main_environment_oidc_guards(self):
        self.assertIn("or baseline != 'dace4a501cb539d61ecad42e592f36b8666f2cc1'", WORKFLOW)
        gate = "python3 tests/test_private_entry_website_publication.py PrivateEntryBuildTests -v"
        self.assertLess(WORKFLOW.index(gate), WORKFLOW.index("Obtain ephemeral website-only publication credentials privately"))
        for text in ("environment: aws-publishing", "group: aws-website-publication",
                     "cancel-in-progress: false", "--phase cleanup", "--profile private-entry",
                     "if: github.ref == 'refs/heads/main' && github.event_name == 'workflow_dispatch'",
                     'git merge-base --is-ancestor "$BASELINE_COMMIT" "$GITHUB_SHA"'):
            self.assertIn(text, WORKFLOW)
        self.assertNotIn("upload-artifact", WORKFLOW)
        self.assertNotIn("terraform apply", WORKFLOW)
        self.assertTrue(all(re.fullmatch(r"[a-f0-9]{40}", pin)
                            for pin in re.findall(r"uses: [^@\n]+@([^\s]+)", WORKFLOW)))


@unittest.skipUnless(os.environ.get("AWS_MESOUQ_WEBSITE_CURRENT_DIST") and
                     os.environ.get("AWS_MESOUQ_WEBSITE_BASELINE_DIST"),
                     "Actual Node build pair is supplied by allocated QA/publication preflight")
class PrivateEntryBuildTests(unittest.TestCase):
    def test_actual_node_build_pair_has_exactly_one_reviewed_delta_and_old_profile_is_red(self):
        baseline_dir = Path(os.environ["AWS_MESOUQ_WEBSITE_BASELINE_DIST"])
        dist = Path(os.environ["AWS_MESOUQ_WEBSITE_CURRENT_DIST"])
        baseline = publication.read_build(baseline_dir, KEYS)
        target = publication.read_build(dist, KEYS)
        self.assertEqual({key for key in KEYS if baseline[key] != target[key]}, {"src/app.js"})
        fake = fixtures.FakeAws(baseline)
        result = publication.publish(dist, baseline_dir, "synthetic-bucket", fixtures.ROLE,
                                     aws=fake, profile=PROFILE)
        self.assertEqual(result["conditional_writes"], 1)
        self.assertEqual(result["assets_verified"], 14)
        old = fixtures.FakeAws(baseline)
        with self.assertRaises(publication.PublicationError):
            publication.publish(dist, baseline_dir, "synthetic-bucket", fixtures.ROLE,
                                aws=old, profile="remembered-session")
        self.assertEqual(old.calls, [])


if __name__ == "__main__":
    unittest.main()
