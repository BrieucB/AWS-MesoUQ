"""Deployment tests use fake Lambda/ECR responses; no cloud or container daemon."""
import copy
import hashlib
import importlib.util
import json
from pathlib import Path
import sys
from types import SimpleNamespace
import unittest

TOOLS = Path(__file__).resolve().parents[1] / "tools"
sys.path.insert(0, str(TOOLS))
import publish_backend as backend

ACCOUNT = "000000000000"
ROLE = f"arn:aws:iam::{ACCOUNT}:role/publisher"
URI = f"{ACCOUNT}.dkr.ecr.eu-central-1.amazonaws.com/backend"
OLD = URI + "@sha256:" + "a" * 64
NEW = URI + "@sha256:" + "b" * 64
SHA = "c" * 40
SOURCE = "https://github.com/Example/Fixture"
POLICIES = {"fixture": {"adapter_sha256": "d" * 64, "science_revision": "e" * 40,
                       "scenario_families": ["baseline"], "archive_root": "/project/fixture"}}


class FakeAws:
    def __init__(self):
        self.cfg = {"FunctionName": "fixture", "FunctionArn":
                    f"arn:aws:lambda:eu-central-1:{ACCOUNT}:function:fixture",
                    "PackageType": "Image", "Architectures": ["x86_64"], "State": "Active",
                    "LastUpdateStatus": "Successful", "RevisionId": "revision-1",
                    "MemorySize": 256, "Timeout": 28, "Role": "unchanged-role",
                    "Environment": {"Variables": {"KEEP": "private-value"}}}
        self.image = OLD
        self.identity = {"Account": ACCOUNT, "Arn": f"arn:aws:sts::{ACCOUNT}:assumed-role/publisher/test"}
        self.calls = []
        self.fail_code = False
        self.drift = False

    def __call__(self, *args):
        self.calls.append(args)
        if args[:2] == ("sts", "get-caller-identity"):
            return self.identity
        if args[:2] == ("lambda", "get-function"):
            return {"Configuration": copy.deepcopy(self.cfg),
                    "Code": {"ResolvedImageUri": self.image}}
        if args[0] != "lambda":
            raise AssertionError(args)
        assert args[args.index("--revision-id") + 1] == self.cfg["RevisionId"]
        if args[1] == "update-function-configuration":
            path = args[args.index("--environment") + 1].removeprefix("file://")
            assert Path(path).stat().st_mode & 0o777 == 0o600
            self.cfg["Environment"] = json.loads(Path(path).read_text())
            if self.drift:
                self.cfg["Timeout"] = 999
            self.cfg["RevisionId"] = "revision-2"
        elif args[1] == "update-function-code":
            if self.fail_code:
                raise backend.PublicationError("aws-command-failed")
            self.image = args[args.index("--image-uri") + 1]
            self.cfg["RevisionId"] = "revision-3"
        else:
            raise AssertionError(args)
        return copy.deepcopy(self.cfg)


def publish(aws, **kwargs):
    values = dict(function="fixture", repository_uri=URI, source_sha=SHA,
                  source_repository=SOURCE, baseline_image=OLD, policies=POLICIES,
                  expected_policy_sha256=hashlib.sha256(b"").hexdigest(),
                  expected_role_arn=ROLE, aws=aws,
                  image_verifier=lambda *args: NEW, sleep=lambda seconds: None)
    values.update(kwargs)
    return backend.publish(**values)


class BackendPublicationTests(unittest.TestCase):
    def test_preserves_environment_and_uses_revision_preconditions(self):
        aws = FakeAws()
        result = publish(aws)
        self.assertEqual((result["configuration_updates"], result["code_updates"]), (1, 1))
        self.assertEqual(aws.cfg["Environment"]["Variables"]["KEEP"], "private-value")
        self.assertEqual(aws.image, NEW)
        self.assertFalse(any("iam" in args for args in aws.calls))

    def test_idempotent_same_source_and_policy(self):
        aws = FakeAws()
        publish(aws)
        aws.calls.clear()
        result = publish(aws)
        self.assertEqual((result["configuration_updates"], result["code_updates"]), (0, 0))
        self.assertFalse(any(args[1].startswith("update-") for args in aws.calls))

    def test_retry_reconciles_partial_configuration_update(self):
        aws = FakeAws()
        aws.fail_code = True
        with self.assertRaisesRegex(backend.PublicationError, "aws-command-failed"):
            publish(aws)
        self.assertEqual(aws.image, OLD)
        aws.fail_code = False
        result = publish(aws)
        self.assertEqual((result["configuration_updates"], result["code_updates"]), (0, 1))

    def test_root_identity_rejected_before_cloud_writes(self):
        aws = FakeAws()
        aws.identity["Arn"] = f"arn:aws:iam::{ACCOUNT}:root"
        with self.assertRaisesRegex(backend.PublicationError, "identity-rejected"):
            publish(aws)
        self.assertFalse(any(args[0] == "lambda" for args in aws.calls))

    def test_unknown_code_baseline_rejected(self):
        aws = FakeAws()
        aws.image = URI + "@sha256:" + "f" * 64
        with self.assertRaisesRegex(backend.PublicationError, "baseline-mismatch"):
            publish(aws)
        self.assertFalse(any(args[1].startswith("update-") for args in aws.calls))

    def test_existing_policy_conflict_rejected(self):
        aws = FakeAws()
        aws.cfg["Environment"]["Variables"]["BENCHMARK_POLICIES"] = '{"other":"policy"}'
        with self.assertRaisesRegex(backend.PublicationError, "policy-conflict"):
            publish(aws)

    def test_existing_expected_policy_can_be_replaced_explicitly(self):
        aws = FakeAws()
        old_policy = '{"other":"policy"}'
        aws.cfg["Environment"]["Variables"]["BENCHMARK_POLICIES"] = old_policy
        result = publish(aws, expected_policy_sha256=hashlib.sha256(old_policy.encode()).hexdigest())
        self.assertEqual(result["configuration_updates"], 1)

    def test_configuration_drift_stops_before_code_update(self):
        aws = FakeAws()
        aws.drift = True
        with self.assertRaisesRegex(backend.PublicationError, "configuration-changed"):
            publish(aws)
        self.assertEqual(aws.image, OLD)

    def test_versioned_function_and_cross_account_image_rejected(self):
        for changes in ({"function": "fixture:1"},
                        {"repository_uri": URI.replace(ACCOUNT, "111111111111")}):
            with self.subTest(changes=changes):
                aws = FakeAws()
                with self.assertRaises(backend.PublicationError):
                    publish(aws, **changes)
                self.assertEqual(aws.calls, [])

    def test_policy_binding_requires_pins_archive_and_known_families(self):
        for key, value in (("adapter_sha256", "wrong"), ("archive_root", "/tmp/../escape"),
                           ("scenario_families", ["arbitrary"]), ("max_concurrent_stages", True), ("max_concurrent_stages", 65),
                           ("archive_root", "/bad path"), ("archive_root", "/a//b"),
                           ("archive_root", "/a/./b"), ("archive_root", "/a/")):
            policies = copy.deepcopy(POLICIES)
            policies["fixture"][key] = value
            with self.subTest(key=key), self.assertRaises(backend.PublicationError):
                backend.validate_policies(policies)

    def test_source_image_checked_without_logging_credentials(self):
        config_digest = "sha256:" + "1" * 64
        manifest = json.dumps({"schemaVersion": 2, "mediaType": backend.OCI,
                               "config": {"digest": config_digest}, "layers": []})
        digest = "sha256:" + hashlib.sha256(manifest.encode()).hexdigest()
        immutable = URI + "@" + digest
        calls = []
        def aws(*args):
            if args[:2] == ("ecr", "describe-repositories"):
                return {"repositories": [{"repositoryUri": URI, "imageTagMutability": "IMMUTABLE"}]}
            return {"images": [{"imageManifest": manifest, "imageManifestMediaType": backend.OCI,
                                "imageId": {"imageDigest": digest}}], "failures": []}
        def command(args, **kwargs):
            calls.append((args, kwargs))
            if args[0] == "aws":
                return SimpleNamespace(returncode=0, stdout=b"test-password")
            if args[1:3] == ["image", "inspect"]:
                config = {"Os": "linux", "Architecture": "amd64", "Id": config_digest,
                          "RepoDigests": [immutable], "Config": {"Labels": {
                              "org.opencontainers.image.revision": SHA,
                              "org.opencontainers.image.source": SOURCE}}}
                return SimpleNamespace(returncode=0, stdout=json.dumps([config]).encode())
            return SimpleNamespace(returncode=0, stdout=b"")
        self.assertEqual(backend.verify_source(aws, URI, SHA, SOURCE, command), immutable)
        self.assertFalse(any("test-password" in str(args) for args, _ in calls))
        self.assertEqual(calls[1][1]["input"], b"test-password")

    def test_workflow_includes_contract_in_image_context_and_restricts_main(self):
        root = TOOLS.parent
        image = (root / ".github/workflows/publish.yml").read_text()
        self.assertIn("'src/aws_mesouq/benchmark.py'", image)
        workflow = (root / ".github/workflows/publish-benchmark-backend.yml").read_text()
        self.assertIn("github.ref == 'refs/heads/main'", workflow)
        self.assertIn("'Resource':function_arn", workflow)
        self.assertNotIn("iam:PassRole", workflow)
        self.assertNotIn("dynamodb:", workflow)


PREVIOUS_SHA = "9" * 40


class SourceBaselinePublicationTests(unittest.TestCase):
    def from_source(self, aws, **overrides):
        seen = []
        def verifier(client, repository, sha, source):
            self.assertIs(client, aws)
            self.assertEqual((repository, source), (URI, SOURCE))
            self.assertEqual(aws.calls[0][:2], ("sts", "get-caller-identity"))
            self.assertFalse(any(args[0] == "lambda" for args in aws.calls))
            seen.append(sha)
            self.assertIn(sha, (PREVIOUS_SHA, SHA))
            return OLD if sha == PREVIOUS_SHA else NEW
        values = dict(previous_source_sha=PREVIOUS_SHA, baseline_image=None,
                      image_verifier=verifier)
        values.update(overrides)
        return publish(aws, **values), seen

    def test_reviewed_source_derives_baseline_after_identity_and_preserves_cas(self):
        aws = FakeAws()
        result, seen = self.from_source(aws)
        self.assertEqual(seen, [PREVIOUS_SHA, SHA])
        self.assertEqual(result["previous_source_commit"], PREVIOUS_SHA)
        self.assertEqual((result["configuration_updates"], result["code_updates"]), (1, 1))
        self.assertEqual(aws.cfg["Environment"]["Variables"]["KEEP"], "private-value")
        updates = [args for args in aws.calls if args[0] == "lambda" and args[1].startswith("update-")]
        self.assertEqual([args[args.index("--revision-id") + 1] for args in updates],
                         ["revision-1", "revision-2"])

    def test_explicit_source_supersedes_stale_legacy_uri(self):
        aws = FakeAws()
        result, _ = self.from_source(aws, baseline_image="unused-stale-legacy-value")
        self.assertEqual(result["status"], "verified")
        aws = FakeAws()
        aws.image = URI + "@sha256:" + "f" * 64
        with self.assertRaisesRegex(backend.PublicationError, "baseline-mismatch"):
            self.from_source(aws, baseline_image=aws.image)
        self.assertFalse(any(args[1].startswith("update-") for args in aws.calls))

    def test_malformed_source_or_absent_both_baselines_rejected_before_aws(self):
        for previous in ("", "short", "A" * 40, True, 42, None):
            with self.subTest(previous=previous):
                aws = FakeAws()
                with self.assertRaisesRegex(backend.PublicationError, "source-input-invalid"):
                    self.from_source(aws, previous_source_sha=previous)
                self.assertEqual(aws.calls, [])

    def test_root_identity_rejected_before_any_source_verification(self):
        aws = FakeAws()
        aws.identity["Arn"] = f"arn:aws:iam::{ACCOUNT}:root"
        def verifier(*args):
            self.fail("Unverified identity reached image verification")
        with self.assertRaisesRegex(backend.PublicationError, "identity-rejected"):
            self.from_source(aws, image_verifier=verifier)
        self.assertEqual([args[:2] for args in aws.calls], [("sts", "get-caller-identity")])

    def test_previous_source_failure_stops_before_function_access(self):
        for category in ("source-image-missing", "source-image-invalid",
                         "source-image-verification-failed"):
            with self.subTest(category=category):
                aws = FakeAws()
                seen = []
                def verifier(client, repository, sha, source):
                    seen.append(sha)
                    raise backend.PublicationError(category)
                with self.assertRaisesRegex(backend.PublicationError, category):
                    self.from_source(aws, image_verifier=verifier)
                self.assertEqual(seen, [PREVIOUS_SHA])
                self.assertFalse(any(args[0] == "lambda" for args in aws.calls))

    def test_cross_repository_baseline_from_verifier_rejected_before_function_access(self):
        aws = FakeAws()
        def verifier(*args):
            return OLD.replace(ACCOUNT, "111111111111")
        with self.assertRaisesRegex(backend.PublicationError, "source-image-invalid"):
            self.from_source(aws, image_verifier=verifier)
        self.assertFalse(any(args[0] == "lambda" for args in aws.calls))

    def test_already_deployed_target_and_policy_remain_zero_write_idempotent(self):
        aws = FakeAws()
        aws.image = NEW
        aws.cfg["Environment"]["Variables"]["BENCHMARK_POLICIES"] = backend.validate_policies(POLICIES)
        result, seen = self.from_source(aws)
        self.assertEqual(seen, [PREVIOUS_SHA, SHA])
        self.assertEqual((result["configuration_updates"], result["code_updates"]), (0, 0))
        self.assertFalse(any(args[1].startswith("update-") for args in aws.calls))

    def test_missing_previous_image_tag_fails_native_source_verifier_before_docker(self):
        def aws(*args):
            if args[:2] == ("ecr", "describe-repositories"):
                return {"repositories": [{"repositoryUri": URI, "imageTagMutability": "IMMUTABLE"}]}
            self.assertIn("imageTag=source-" + PREVIOUS_SHA, args)
            return {"images": [], "failures": [{"failureCode": "ImageNotFound"}]}
        def command(*args, **kwargs):
            self.fail("Missing source tag reached Docker authentication")
        with self.assertRaisesRegex(backend.PublicationError, "source-image-missing"):
            backend.verify_source(aws, URI, PREVIOUS_SHA, SOURCE, command)

    def test_previous_image_with_wrong_source_label_is_rejected(self):
        config_digest = "sha256:" + "1" * 64
        manifest = json.dumps({"schemaVersion": 2, "mediaType": backend.OCI,
                               "config": {"digest": config_digest}, "layers": []})
        digest = "sha256:" + hashlib.sha256(manifest.encode()).hexdigest()
        immutable = URI + "@" + digest
        def aws(*args):
            if args[:2] == ("ecr", "describe-repositories"):
                return {"repositories": [{"repositoryUri": URI, "imageTagMutability": "IMMUTABLE"}]}
            self.assertIn("imageTag=source-" + PREVIOUS_SHA, args)
            return {"images": [{"imageManifest": manifest, "imageManifestMediaType": backend.OCI,
                                "imageId": {"imageDigest": digest}}], "failures": []}
        def command(args, **kwargs):
            if args[0] == "aws":
                return SimpleNamespace(returncode=0, stdout=b"synthetic-password")
            if args[1:3] == ["image", "inspect"]:
                config = {"Os": "linux", "Architecture": "amd64", "Id": config_digest,
                          "RepoDigests": [immutable], "Config": {"Labels": {
                              "org.opencontainers.image.revision": SHA,
                              "org.opencontainers.image.source": SOURCE}}}
                return SimpleNamespace(returncode=0, stdout=json.dumps([config]).encode())
            return SimpleNamespace(returncode=0, stdout=b"")
        with self.assertRaisesRegex(backend.PublicationError, "source-image-invalid"):
            backend.verify_source(aws, URI, PREVIOUS_SHA, SOURCE, command)

    def test_cli_uses_source_baseline_without_requiring_opaque_secret(self):
        import io
        import os
        from contextlib import redirect_stdout
        from unittest.mock import patch
        environment = {
            "BACKEND_FUNCTION_NAME": "fixture", "AWS_ECR_REPOSITORY_URL": URI,
            "GITHUB_REPOSITORY": "Example/Fixture", "BENCHMARK_POLICIES": json.dumps(POLICIES),
            "EXPECTED_POLICY_SHA256": hashlib.sha256(b"").hexdigest(),
            "PUBLICATION_ROLE_ARN": ROLE, "EXPECTED_PREVIOUS_SOURCE_COMMIT": PREVIOUS_SHA,
        }
        with patch.dict(os.environ, environment, clear=True), patch.object(
                sys, "argv", ["publish_backend.py", "--source-commit", SHA]), patch.object(
                backend, "publish", return_value={"status": "verified"}) as invoke, redirect_stdout(io.StringIO()):
            self.assertEqual(backend.main(), 0)
        self.assertIsNone(invoke.call_args.args[4])
        self.assertEqual(invoke.call_args.kwargs, {"previous_source_sha": PREVIOUS_SHA})

    def test_workflow_requires_typed_previous_source_and_executes_validation(self):
        import os
        import subprocess
        import tempfile
        import textwrap
        workflow = (TOOLS.parent / ".github/workflows/publish-benchmark-backend.yml").read_text()
        self.assertRegex(workflow, r"expected_previous_source_commit:\n(?:.*\n){1,3}        type: string\n        required: true")
        self.assertNotIn("AWS_BACKEND_BASELINE_IMAGE_URI", workflow)
        code = textwrap.dedent(workflow.split("          python3 - <<'PY'\n", 1)[1].split("\n          PY", 1)[0])
        for previous, requested, expected in (
                (PREVIOUS_SHA, SHA, 0), ("bad", SHA, 1), (None, SHA, 1),
                (42, SHA, 1), (PREVIOUS_SHA, "f" * 40, 1)):
            with self.subTest(previous=previous, requested=requested), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                inputs = {"expected_source_commit": requested, "expected_existing_policy_sha256": "f" * 64}
                if previous is not None:
                    inputs["expected_previous_source_commit"] = previous
                event = root / "event.json"
                event.write_text(json.dumps({"inputs": inputs}))
                output = root / "environment"
                environment = {
                    "PATH": os.defpath, "GITHUB_REF": "refs/heads/main",
                    "GITHUB_EVENT_NAME": "workflow_dispatch", "GITHUB_SHA": SHA,
                    "GITHUB_EVENT_PATH": str(event), "GITHUB_ENV": str(output),
                    "ROLE_INPUT": ROLE, "REGION_INPUT": "eu-central-1",
                    "FUNCTION_INPUT": "fixture", "REPOSITORY_INPUT": URI,
                    "POLICIES_INPUT": json.dumps(POLICIES),
                }
                result = subprocess.run([sys.executable, "-c", code], env=environment,
                                        capture_output=True, text=True, timeout=10)
                self.assertEqual(result.returncode, expected, result.stderr)
                if expected == 0:
                    self.assertIn("EXPECTED_PREVIOUS_SOURCE_COMMIT=" + PREVIOUS_SHA, output.read_text())
                    self.assertNotIn("BACKEND_BASELINE_IMAGE_URI", output.read_text())
                else:
                    self.assertFalse(output.exists())


if __name__ == "__main__":
    unittest.main()
