"""Synthetic Lambda configuration activation checks; no AWS or numerical imports."""
from contextlib import redirect_stderr, redirect_stdout
from copy import deepcopy
import hashlib
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))
import publish_accuracy_policy as publication
from aws_mesouq import benchmark_accuracy_protocol as protocol

ACCOUNT = "000000000000"
URI = ACCOUNT + ".dkr.ecr.eu-central-1.amazonaws.com/backend"
IMAGE = URI + "@sha256:" + "a" * 64


def context(project="p", *, revision="a", owner="alice"):
    value = {
        "schema": protocol.CONTEXT_SCHEMA, "id": "0" * 64, "enabled": True,
        "owner_sub": owner, "project_id": project, "site": "karolina",
        "science_revision": revision * 40, "source_binding_sha256": "b" * 64,
        "adapter_sha256": "c" * 64, "policy_sha256": "d" * 64,
        "runtime_profile_sha256": "e" * 64, "native_root": "/scratch/fixture/" + project + revision,
        "archive_root": "/project/fixture/" + project + revision, "native_user": "fixture",
        "native_cpus": 16, "native_gpus": 1, "maximum_artifact_bytes": 10000000,
        "maximum_total_artifact_bytes": 100000000, "maximum_artifact_files": 64,
        "capabilities": sorted(protocol.REQUIRED_CAPABILITIES),
        "allowed_artifacts": {kind: ["stage-receipt.json"] for kind in protocol.KINDS},
    }
    value["id"] = protocol.context_id(value)
    return value


def target_policy():
    old = context()
    old["enabled"] = False
    return {"p": [old, context(revision="f")]}


class FakeAws:
    def __init__(self, *, compressed=False):
        existing = {"p": [context()], "other": [context("other", owner="bob")]}
        literal = (publication.encode_policies(existing) if compressed
                   else publication.canonical(existing))
        self.cfg = {
            "FunctionName": "fixture",
            "FunctionArn": "arn:aws:lambda:eu-central-1:" + ACCOUNT + ":function:fixture",
            "PackageType": "Image", "Architectures": ["x86_64"], "State": "Active",
            "LastUpdateStatus": "Successful", "RevisionId": "revision-1",
            "MemorySize": 256, "Timeout": 28, "Role": "unchanged-role",
            "Environment": {"Variables": {"KEEP": "unchanged",
                "BENCHMARK_POLICIES": '{"fixture":"legacy"}',
                "BENCHMARK_ACCURACY_POLICIES": literal}},
        }
        self.image = IMAGE
        self.account = ACCOUNT
        self.calls = []
        self.ambiguous = self.pending = self.failed = self.drift = self.env_drift = False
        self.image_drift = False

    def __call__(self, *arguments):
        assert arguments[:2] == ("--region", "eu-central-1")
        args = arguments[2:]
        self.calls.append(args)
        if args[:2] == ("sts", "get-caller-identity"):
            return {"Account": self.account}
        if args[:2] == ("lambda", "get-function"):
            return {"Configuration": deepcopy(self.cfg),
                    "Code": {"ResolvedImageUri": self.image}}
        if args[:2] != ("lambda", "update-function-configuration"):
            raise AssertionError(args)
        assert args[args.index("--revision-id") + 1] == self.cfg["RevisionId"]
        path = Path(args[args.index("--environment") + 1].removeprefix("file://"))
        assert path.stat().st_mode & 0o777 == 0o600
        self.cfg["Environment"] = json.loads(path.read_text())
        self.cfg["RevisionId"] = "revision-2"
        if self.pending:
            self.cfg["LastUpdateStatus"] = "InProgress"
        if self.failed:
            self.cfg["LastUpdateStatus"] = "Failed"
        if self.drift:
            self.cfg["Timeout"] = 999
        if self.env_drift:
            self.cfg["Environment"]["Variables"]["KEEP"] = "drift"
        if self.image_drift:
            self.image = URI + "@sha256:" + "b" * 64
        if self.ambiguous:
            raise publication.PublicationError("aws-command-failed")
        return deepcopy(self.cfg)

    @property
    def update_calls(self):
        return [args for args in self.calls if args[:2] ==
                ("lambda", "update-function-configuration")]


def guard(aws, policy=None):
    values = aws.cfg["Environment"]["Variables"]
    return {
        "schema": publication.GUARD_SCHEMA, "function_name": "fixture",
        "repository_uri": URI, "source_commit": "c" * 40,
        "source_repository": "https://github.com/Example/Fixture",
        "resolved_image_uri": IMAGE, "revision_id": aws.cfg["RevisionId"],
        "v1_policy_sha256": publication.literal_checksum(values["BENCHMARK_POLICIES"]),
        "v2_policy_sha256": publication.literal_checksum(values["BENCHMARK_ACCURACY_POLICIES"]),
        "environment_sha256": publication.checksum(values),
        "project_policy_sha256": publication.checksum(policy or target_policy()),
        "codec_sha256": "d" * 64,
    }


def publish(aws, **kwargs):
    values = dict(guard=guard(aws), project_policies=target_policy(), apply=True, aws=aws,
                  image_verifier=lambda *args: IMAGE, checkout_verifier=lambda value: None,
                  sleep=lambda seconds: None)
    values.update(kwargs)
    return publication.publish(**values)


class AccuracyPolicyPublicationTests(unittest.TestCase):
    def test_plaintext_and_compressed_preserve_other_project_history_and_environment(self):
        for compressed in (False, True):
            with self.subTest(compressed=compressed):
                aws = FakeAws(compressed=compressed)
                before = deepcopy(aws.cfg["Environment"]["Variables"])
                old_other = publication.decode_policies(before["BENCHMARK_ACCURACY_POLICIES"])["other"]
                result = publish(aws)
                final = aws.cfg["Environment"]["Variables"]
                decoded = publication.decode_policies(final["BENCHMARK_ACCURACY_POLICIES"])
                self.assertEqual(decoded["other"], old_other)
                self.assertEqual(decoded["p"], target_policy()["p"])
                self.assertEqual(final["BENCHMARK_POLICIES"], before["BENCHMARK_POLICIES"])
                self.assertEqual(final["KEEP"], before["KEEP"])
                self.assertEqual(result["configuration_updates"], 1)
                self.assertEqual(result["status"], "verified")
                self.assertEqual(len(aws.update_calls), 1)

    def test_plan_is_read_only_and_equal_encoded_target_needs_no_update(self):
        aws = FakeAws()
        result = publish(aws, apply=False)
        self.assertEqual(result["status"], "planned")
        self.assertFalse(aws.update_calls)
        publish(aws)
        aws.calls.clear()
        result = publish(aws)
        self.assertEqual(result["configuration_updates"], 0)
        self.assertFalse(aws.update_calls)

    def test_revision_v1_v2_and_complete_environment_guards_fail_before_write(self):
        for key in ("revision_id", "v1_policy_sha256", "v2_policy_sha256", "environment_sha256"):
            with self.subTest(key=key):
                aws = FakeAws()
                expected = guard(aws)
                expected[key] = "stale" if key == "revision_id" else "0" * 64
                with self.assertRaises(publication.PublicationError):
                    publish(aws, guard=expected)
                self.assertFalse(aws.update_calls)

    def test_project_payload_guard_rejects_changed_input(self):
        aws = FakeAws()
        expected = guard(aws)
        altered = target_policy()
        altered["p"][1]["enabled"] = False
        with self.assertRaisesRegex(publication.PublicationError, "project-conflict"):
            publish(aws, guard=expected, project_policies=altered)
        self.assertFalse(aws.calls)

    def test_history_removal_and_immutable_mutation_are_rejected(self):
        for mutate in (False, True):
            with self.subTest(mutate=mutate):
                aws = FakeAws()
                altered = target_policy()
                if mutate:
                    altered["p"][0]["native_root"] = "/scratch/changed"
                    altered["p"][0]["id"] = protocol.context_id(altered["p"][0])
                else:
                    altered["p"].pop(0)
                with self.assertRaisesRegex(publication.PublicationError, "history-conflict"):
                    publish(aws, project_policies=altered, guard=guard(aws, altered))
                self.assertFalse(aws.update_calls)

    def test_normal_backend_validator_rejects_invalid_context(self):
        aws = FakeAws()
        altered = target_policy()
        altered["p"][1]["native_gpus"] = 0
        with self.assertRaisesRegex(publication.PublicationError, "policy-invalid"):
            publish(aws, project_policies=altered)
        self.assertFalse(aws.calls)

    def test_multiple_updated_projects_are_rejected(self):
        aws = FakeAws()
        altered = target_policy()
        altered["extra"] = [context("extra")]
        with self.assertRaisesRegex(publication.PublicationError, "scope-invalid"):
            publish(aws, project_policies=altered)
        self.assertFalse(aws.calls)

    def test_utf8_environment_limit_includes_unchanged_keys_and_values(self):
        aws = FakeAws()
        aws.cfg["Environment"]["Variables"]["KEEP"] = "\u03bc" * 3000
        with self.assertRaisesRegex(publication.PublicationError, "environment-too-large"):
            publish(aws)
        self.assertFalse(aws.update_calls)

    def test_wrong_account_installed_image_and_verified_source_image_are_rejected(self):
        aws = FakeAws()
        aws.account = "111111111111"
        with self.assertRaisesRegex(publication.PublicationError, "account-conflict"):
            publish(aws)
        self.assertFalse(aws.update_calls)
        aws = FakeAws()
        aws.image = URI + "@sha256:" + "b" * 64
        with self.assertRaisesRegex(publication.PublicationError, "image-conflict"):
            publish(aws)
        self.assertFalse(aws.update_calls)
        aws = FakeAws()
        with self.assertRaisesRegex(publication.PublicationError, "source-image-conflict"):
            publish(aws, image_verifier=lambda *args: URI + "@sha256:" + "b" * 64)
        self.assertFalse(aws.update_calls)

    def test_checkout_source_conflict_stops_before_aws(self):
        aws = FakeAws()
        def stale(_guard):
            raise publication.PublicationError("source-conflict")
        with self.assertRaisesRegex(publication.PublicationError, "source-conflict"):
            publish(aws, checkout_verifier=stale)
        self.assertFalse(aws.calls)

    def test_ambiguous_update_is_not_retried(self):
        aws = FakeAws()
        aws.ambiguous = True
        with self.assertRaisesRegex(publication.PublicationError, "unknown-reconcile"):
            publish(aws)
        self.assertEqual(len(aws.update_calls), 1)

    def test_failed_or_pending_updates_are_not_retried(self):
        for flag in ("failed", "pending"):
            with self.subTest(flag=flag):
                aws = FakeAws()
                setattr(aws, flag, True)
                with self.assertRaisesRegex(publication.PublicationError, "reconcile"):
                    publish(aws)
                self.assertEqual(len(aws.update_calls), 1)

    def test_full_environment_configuration_and_image_readback_drift_are_rejected(self):
        for flag in ("drift", "env_drift", "image_drift"):
            with self.subTest(flag=flag):
                aws = FakeAws()
                setattr(aws, flag, True)
                with self.assertRaises(publication.PublicationError):
                    publish(aws)
                self.assertEqual(len(aws.update_calls), 1)

    def test_complete_guard_schema_is_required(self):
        aws = FakeAws()
        expected = guard(aws)
        expected["extra"] = "unreviewed"
        with self.assertRaises(publication.PublicationError):
            publish(aws, guard=expected)
        self.assertFalse(aws.calls)


class CheckoutAndCliTests(unittest.TestCase):
    def test_reviewed_checkout_requires_exact_head_clean_sources_and_codec_hash(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            environment = dict(os.environ, GIT_CONFIG_NOSYSTEM="1", GIT_CONFIG_GLOBAL=os.devnull)
            def git(*args):
                return subprocess.run(["git", "-C", str(root), *args], env=environment,
                                      capture_output=True, check=True, timeout=10).stdout
            git("init")
            for name in publication.SOURCE_FILES:
                path = root / name
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text("synthetic tracked source\n")
            git("add", ".")
            git("-c", "user.name=Fixture", "-c", "user.email=fixture@example.test",
                "-c", "commit.gpgsign=false", "commit", "-m", "fixture")
            expected = guard(FakeAws())
            expected["source_commit"] = git("rev-parse", "HEAD").decode().strip()
            expected["codec_sha256"] = hashlib.sha256(
                (root / publication.SOURCE_FILES[-1]).read_bytes()).hexdigest()
            publication.verify_checkout(expected, root=root)
            stale = dict(expected, source_commit="f" * 40)
            with self.assertRaises(publication.PublicationError):
                publication.verify_checkout(stale, root=root)
            bad_codec = dict(expected, codec_sha256="0" * 64)
            with self.assertRaises(publication.PublicationError):
                publication.verify_checkout(bad_codec, root=root)
            (root / publication.SOURCE_FILES[0]).write_text("changed tracked source\n")
            with self.assertRaises(publication.PublicationError):
                publication.verify_checkout(expected, root=root)

    def test_cli_uses_private_intent_receipt_and_refuses_reused_directory(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            expected, policy = guard(FakeAws()), target_policy()
            for name, value in (("guard.json", expected), ("policy.json", policy)):
                path = root / name
                path.write_text(json.dumps(value))
                path.chmod(0o600)
            arguments = ["--guard", str(root / "guard.json"), "--project-policy",
                         str(root / "policy.json"), "--receipt-directory", str(root / "receipts")]
            result = {"status": "planned", "configuration_updates": 0, "environment_utf8_bytes": 1000}
            with patch.object(publication, "publish", return_value=result) as invoke:
                with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
                    self.assertEqual(publication.main(arguments), 0)
                    self.assertEqual(publication.main(arguments), 1)
                self.assertEqual(invoke.call_count, 1)
            for name in ("intent.json", "receipt.json"):
                self.assertEqual((root / "receipts" / name).stat().st_mode & 0o777, 0o600)

    def test_private_input_mode_symlink_and_deep_json_are_rejected(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            path = root / "guard.json"
            path.write_text("{}")
            path.chmod(0o644)
            with self.assertRaises(publication.PublicationError):
                publication._private_json(path)
            path.chmod(0o600)
            link = root / "link.json"
            link.symlink_to(path)
            with self.assertRaises(OSError):
                publication._private_json(link)
            path.write_text("[" * 10000 + "]" * 10000)
            with self.assertRaises(publication.PublicationError):
                publication._private_json(path)
