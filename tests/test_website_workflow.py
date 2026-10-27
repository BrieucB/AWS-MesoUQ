"""Offline checks for manual OIDC static publication admission and permissions."""
import json
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import textwrap
import unittest

WORKFLOW = Path(__file__).resolve().parents[1] / ".github/workflows/publish-website.yml"
SOURCE = WORKFLOW.read_text()
MATCH = re.search(r"python3 - <<'PY'\n(.*?)\n          PY", SOURCE, re.S)
ADMISSION = textwrap.dedent(MATCH[1]) if MATCH else ""
ROLE = "arn:aws:iam::123456789012:role/scoped/static-publisher"
BUCKET = "synthetic-website-bucket"
SHA = "a" * 40
BASELINE = "b" * 40


class WebsiteWorkflowTests(unittest.TestCase):
    def run_admission(self, patch=None, **environment):
        inputs = {"publication_role_arn": ROLE, "website_bucket": BUCKET,
                  "baseline_commit": BASELINE, "expected_source_commit": SHA}
        inputs.update(patch or {})
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            event = root / "event.json"
            event.write_text(json.dumps({"inputs": inputs}))
            destination = root / "environment"
            env = {"GITHUB_REF": "refs/heads/main", "GITHUB_EVENT_NAME": "workflow_dispatch",
                   "GITHUB_SHA": SHA, "GITHUB_EVENT_PATH": str(event),
                   "GITHUB_ENV": str(destination)}
            env.update(environment)
            result = subprocess.run([sys.executable, "-c", ADMISSION], env=env,
                                    capture_output=True, text=True, timeout=5)
            return result, destination.read_text() if destination.exists() else ""

    def test_private_inputs_are_masked_before_environment_export(self):
        self.assertTrue(ADMISSION)
        result, environment = self.run_admission()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.splitlines(),
                         ["::add-mask::" + ROLE, "::add-mask::123456789012", "::add-mask::" + BUCKET])
        self.assertEqual(environment.splitlines(),
                         ["PUBLICATION_ROLE_ARN=" + ROLE, "WEBSITE_BUCKET=" + BUCKET,
                          "BASELINE_COMMIT=" + BASELINE])
        self.assertNotIn(chr(36) + "{{ inputs.", SOURCE)
        self.assertNotIn("print(inputs", ADMISSION)

    def test_wrong_ref_event_and_unreviewed_source_fail_without_export(self):
        for environment in [
            {"GITHUB_REF": "refs/heads/feature/unreviewed"},
            {"GITHUB_EVENT_NAME": "push"},
            {"GITHUB_SHA": "c" * 40},
        ]:
            with self.subTest(environment=environment):
                result, exported = self.run_admission(**environment)
                self.assertNotEqual(result.returncode, 0)
                self.assertEqual(exported, "")
                self.assertNotIn(ROLE, result.stdout + result.stderr)
                self.assertNotIn(BUCKET, result.stdout + result.stderr)

    def test_malformed_destinations_and_injections_fail_without_export(self):
        for patch in [
            {"publication_role_arn": "arn:aws:iam::123456789012:root"},
            {"publication_role_arn": ROLE + "\nUNSAFE=value"},
            {"website_bucket": "invalid/bucket"},
            {"website_bucket": "127.0.0.1"},
            {"website_bucket": "invalid..bucket"},
            {"website_bucket": "invalid\nUNSAFE=value"},
            {"baseline_commit": "main"},
            {"expected_source_commit": None},
        ]:
            with self.subTest(patch=patch):
                result, exported = self.run_admission(patch)
                self.assertNotEqual(result.returncode, 0)
                self.assertEqual(exported, "")
                self.assertNotIn("Traceback", result.stderr)
                self.assertNotIn("UNSAFE", result.stdout + result.stderr)

    def test_oidc_role_session_can_write_only_three_approved_objects(self):
        marker = "          PUBLICATION_SESSION_POLICY: >-\n"
        policy_text = SOURCE.split(marker, 1)[1].split("        run:", 1)[0]
        policy = json.loads(textwrap.dedent(policy_text))
        statements = {item["Action"]: item for item in policy["Statement"]}
        self.assertEqual(len(statements), 3)
        prefix = "arn:aws:s3:::" + chr(36) + "{{ env.WEBSITE_BUCKET }}"
        self.assertEqual(statements["s3:ListBucket"], {"Effect": "Allow", "Action": "s3:ListBucket", "Resource": prefix})
        keys = ["index.html", "src/app.js", "src/client.js", "src/defaults.js", "src/plots.js",
                "src/style.css", "src/curve-editor.js", "presets/indentation-v1.json",
                "runtime-config.json", "src/posterior.js"]
        self.assertEqual(statements["s3:GetObject"]["Effect"], "Allow")
        self.assertEqual(set(statements["s3:GetObject"]["Resource"]), {prefix + "/" + key for key in keys})
        self.assertEqual(statements["s3:PutObject"], {"Effect": "Allow", "Action": "s3:PutObject",
                                        "Resource": [prefix + "/src/posterior.js",
                                                     prefix + "/src/style.css",
                                                     prefix + "/src/app.js"]})
        self.assertNotIn("Delete", policy_text)
        self.assertNotIn("ecr:", policy_text)
        self.assertNotIn("cloudfront:", policy_text)
        self.assertNotIn("iam:", policy_text)

    def test_manual_main_environment_and_pinned_action_boundary(self):
        self.assertIn("on:\n  workflow_dispatch:", SOURCE)
        self.assertIn("if: github.ref == 'refs/heads/main' && github.event_name == 'workflow_dispatch'", SOURCE)
        self.assertIn("environment: aws-publishing", SOURCE)
        self.assertIn("cancel-in-progress: false", SOURCE)
        self.assertIn("timeout-minutes: 10", SOURCE)
        self.assertIn("contents: read\n  id-token: write", SOURCE)
        uses = re.findall(r"uses: [^@\n]+@([^\s]+)", SOURCE)
        self.assertEqual(len(uses), 4)
        self.assertTrue(all(re.fullmatch(r"[a-f0-9]{40}", value) for value in uses))
        self.assertIn("persist-credentials: false", SOURCE)
        self.assertIn("repository: aws-actions/configure-aws-credentials", SOURCE)
        self.assertIn("ref: e1253824e5c10ff9df46874f81ed3ec929e19cfd", SOURCE)
        self.assertIn("node-version: '24'", SOURCE)
        self.assertIn("--phase configure", SOURCE)
        self.assertIn("--phase cleanup", SOURCE)
        self.assertIn("if: always() && steps.aws_action.outcome == 'success' && steps.aws_node.outcome == 'success'", SOURCE)
        self.assertLess(SOURCE.index("Validate dispatch and mask private destination"),
                        SOURCE.index("uses: actions/checkout"))
        self.assertLess(SOURCE.index("Validate ancestry and build only public frontend sources"),
                        SOURCE.index("Obtain ephemeral website-only publication credentials privately"))
        self.assertNotIn("upload-artifact", SOURCE)
        self.assertNotIn("terraform apply", SOURCE)


if __name__ == "__main__":
    unittest.main()
