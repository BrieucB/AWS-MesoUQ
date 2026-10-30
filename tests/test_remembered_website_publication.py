"""Offline remembered-browser publication: bounded source builds and fake AWS only."""
import importlib.util
import json
import os
from pathlib import Path
import re
import tempfile
import textwrap
import unittest

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "remembered_website_fixtures", ROOT / "tests/test_website_publication.py")
fixtures = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(fixtures)
publication = fixtures.publication
WORKFLOW = (ROOT / ".github/workflows/publish-remembered-website.yml").read_text()
# Literal release contract also runs against the pre-fix publisher for the red proof.
BASE_KEYS = (*publication.BENCHMARK_BASE_KEYS, *publication.BENCHMARK_NEW_KEYS)
NEW_KEY = "src/site-session.js"
WRITE_ORDER = (NEW_KEY, "src/client.js", "src/style.css", "src/app.js")
PROFILE = "remembered-session"


def materialize(directory, assets):
    for key, raw in assets.items():
        path = directory / key
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(raw)


class RememberedPublicationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.baseline_dir, self.dist = self.root / "baseline", self.root / "dist"
        self.baseline = {key: ("baseline " + key).encode() for key in BASE_KEYS}
        self.target = dict(self.baseline)
        self.target.update({key: ("target " + key).encode() for key in WRITE_ORDER})
        materialize(self.baseline_dir, self.baseline)
        materialize(self.dist, self.target)
        self.aws = fixtures.FakeAws(self.baseline)

    def publish(self):
        return publication.publish(self.dist, self.baseline_dir, "synthetic-bucket",
                                   fixtures.ROLE, aws=self.aws, profile=PROFILE)

    def puts(self):
        return [call for call in self.aws.calls if call[:2] == ("s3api", "put-object")]

    def writes(self):
        return [call[call.index("--key") + 1] for call in self.puts()]

    def test_four_conditional_writes_dependencies_before_entry_and_all14_readback(self):
        runtime = self.aws.objects[publication.CONFIG][0]
        result = self.publish()
        self.assertEqual(result["conditional_writes"], 4)
        self.assertEqual(result["assets_verified"], 14)
        self.assertEqual(self.writes(), list(WRITE_ORDER))
        self.assertIn("--if-none-match", self.puts()[0])
        self.assertTrue(all("--if-match" in call for call in self.puts()[1:]))
        self.assertEqual(self.aws.objects[publication.CONFIG][0], runtime)
        self.assertEqual(sum(call[:2] == ("s3api", "get-object") for call in self.aws.calls), 27)
        for key in BASE_KEYS:
            if key not in WRITE_ORDER and key != publication.CONFIG:
                self.assertEqual(self.aws.objects[key][0], self.baseline[key])
        self.assertNotIn("private-live", json.dumps(result))
        self.assertNotIn("synthetic-bucket", json.dumps(result))

    def test_partial_and_exact_target_reruns_are_conditional_and_idempotent(self):
        self.aws.store(NEW_KEY, self.target[NEW_KEY])
        self.aws.store("src/client.js", self.target["src/client.js"])
        first = self.publish()
        self.assertEqual(first["conditional_writes"], 2)
        self.assertEqual(self.writes(), list(WRITE_ORDER[2:]))
        self.aws.calls.clear()
        again = self.publish()
        self.assertEqual(again["conditional_writes"], 0)
        self.assertEqual(first["manifest_sha256"], again["manifest_sha256"])

    def test_unreviewed_existing_or_extra_build_asset_fails_before_aws(self):
        for key in ("src/benchmark-lab.js", "src/posterior.js", "src/unreviewed.js"):
            with self.subTest(key=key):
                path = self.dist / key
                original = path.read_bytes() if path.exists() else None
                path.write_bytes(b"unreviewed")
                self.aws.calls.clear()
                with self.assertRaises(publication.PublicationError):
                    self.publish()
                self.assertEqual(self.aws.calls, [])
                if original is None:
                    path.unlink()
                else:
                    path.write_bytes(original)

    def test_stale_existing_or_new_live_bytes_fail_before_any_write(self):
        for key in (*WRITE_ORDER, "src/benchmark-lab.js"):
            with self.subTest(key=key):
                self.aws = fixtures.FakeAws(self.baseline)
                self.aws.store(key, b"x" * len(self.target[key]))
                with self.assertRaises(publication.PublicationError):
                    self.publish()
                self.assertEqual(self.puts(), [])

    def test_foreign_metadata_or_missing_baseline_object_fails_before_any_write(self):
        for mode in ("checksum", "missing"):
            with self.subTest(mode=mode):
                self.aws = fixtures.FakeAws(self.baseline)
                if mode == "checksum":
                    self.aws.objects["src/posterior.js"][1]["ChecksumSHA256"] = "invalid"
                else:
                    del self.aws.objects["index.html"]
                with self.assertRaises(publication.PublicationError):
                    self.publish()
                self.assertEqual(self.puts(), [])

    def test_dependency_conflict_keeps_app_entry_unchanged_and_can_resume(self):
        self.aws.race = "src/client.js"
        with self.assertRaisesRegex(publication.PublicationError, "conditional-write-failed"):
            self.publish()
        self.assertEqual(self.aws.objects["src/app.js"][0], self.baseline["src/app.js"])
        self.assertEqual(self.aws.objects["index.html"][0], self.baseline["index.html"])
        self.assertEqual(self.aws.objects[NEW_KEY][0], self.target[NEW_KEY])
        self.aws.race = None
        self.aws.calls.clear()
        self.assertEqual(self.publish()["conditional_writes"], 3)
        self.assertEqual(self.writes(), list(WRITE_ORDER[1:]))

    def test_private_config_race_is_detected_without_overwriting_config(self):
        original = self.aws
        def change(*args, **kwargs):
            response = original(*args, **kwargs)
            if args[:2] == ("s3api", "put-object") and args[args.index("--key") + 1] == "src/app.js":
                original.store(publication.CONFIG, b"concurrent-private-config")
            return response
        with self.assertRaises(publication.PublicationError):
            publication.publish(self.dist, self.baseline_dir, "synthetic-bucket",
                                fixtures.ROLE, aws=change, profile=PROFILE)
        self.assertNotIn(publication.CONFIG, self.writes())


class RememberedWorkflowTests(unittest.TestCase):
    def test_role_session_reads14_and_writes_only_four_exact_objects(self):
        text = WORKFLOW.split("          PUBLICATION_SESSION_POLICY: >-\n", 1)[1].split("        run:", 1)[0]
        policy = json.loads(textwrap.dedent(text))
        statements = {item["Action"]: item for item in policy["Statement"]}
        prefix = "arn:aws:s3:::" + chr(36) + "{{ env.WEBSITE_BUCKET }}"
        self.assertEqual(set(statements), {"s3:ListBucket", "s3:GetObject", "s3:PutObject"})
        self.assertEqual(statements["s3:ListBucket"]["Resource"], prefix)
        self.assertEqual(set(statements["s3:GetObject"]["Resource"]),
                         {prefix + "/" + key for key in (*BASE_KEYS, NEW_KEY)})
        self.assertEqual(statements["s3:PutObject"]["Resource"],
                         [prefix + "/" + key for key in WRITE_ORDER])
        self.assertNotIn("*", text)
        self.assertNotIn("Delete", text)
        self.assertNotIn("cloudfront:", text)
        self.assertNotIn("iam:", text)
        base, new, order = publication.PROFILES[PROFILE]
        self.assertEqual(set(base), set(BASE_KEYS))
        self.assertEqual(tuple(new), (NEW_KEY,))
        self.assertEqual(tuple(order), WRITE_ORDER)

    def test_existing_admission_credentials_and_concurrency_are_preserved(self):
        original = (ROOT / ".github/workflows/publish-website.yml").read_text()
        admission = lambda source: re.search(r"python3 - <<'PY'\n(.*?)\n          PY", source, re.S)[1]
        self.assertEqual(admission(WORKFLOW), admission(original))
        self.assertIn("--profile remembered-session", WORKFLOW)
        self.assertIn("7cf0e5307ddcbc804d0b93ce4fb8d3fe4e7fa37c", WORKFLOW)
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

    def test_actual_build_preflight_runs_before_credentials(self):
        build_check = "python3 tests/test_remembered_website_publication.py RememberedBuildTests -v"
        self.assertIn(build_check, WORKFLOW)
        self.assertIn("AWS_MESOUQ_WEBSITE_CURRENT_DIST=", WORKFLOW)
        self.assertIn("AWS_MESOUQ_WEBSITE_BASELINE_DIST=", WORKFLOW)
        self.assertLess(WORKFLOW.index(build_check),
                        WORKFLOW.index("Obtain ephemeral website-only publication credentials privately"))


@unittest.skipUnless(os.environ.get("AWS_MESOUQ_WEBSITE_CURRENT_DIST") and
                     os.environ.get("AWS_MESOUQ_WEBSITE_BASELINE_DIST"),
                     "Actual Node build pair is supplied by allocated QA/publication preflight")
class RememberedBuildTests(unittest.TestCase):
    def test_actual_node_build_pair_has_only_the_reviewed_delta(self):
        current = Path(os.environ["AWS_MESOUQ_WEBSITE_CURRENT_DIST"])
        previous = Path(os.environ["AWS_MESOUQ_WEBSITE_BASELINE_DIST"])
        baseline = publication.read_build(previous, BASE_KEYS)
        target = publication.read_build(current, (*BASE_KEYS, NEW_KEY))
        changed = {key for key in target if key not in baseline or target[key] != baseline[key]}
        self.assertEqual(changed, set(WRITE_ORDER))
        aws = fixtures.FakeAws(baseline)
        live_config = aws.objects[publication.CONFIG][0]
        result = publication.publish(current, previous, "synthetic-bucket", fixtures.ROLE,
                                     aws=aws, profile=PROFILE)
        self.assertEqual(result["assets_verified"], 14)
        self.assertEqual(result["conditional_writes"], 4)
        self.assertEqual([call[call.index("--key") + 1] for call in aws.calls
                          if call[:2] == ("s3api", "put-object")], list(WRITE_ORDER))
        self.assertEqual(aws.objects[publication.CONFIG][0], live_config)


if __name__ == "__main__":
    unittest.main()
