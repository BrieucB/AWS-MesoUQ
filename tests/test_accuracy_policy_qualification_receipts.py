"""Synthetic qualification-publication receipts; no AWS, native runtime or policy values."""
from copy import deepcopy
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / "tools"), str(ROOT / "src"),
                str(ROOT / "services" / "aws_publishing")]
import publish_accuracy_policy as publication
import qualify_accuracy_source as generator

_spec = importlib.util.spec_from_file_location("qualification_publication_fixtures",
    ROOT / "tests" / "test_accuracy_policy_publication.py")
fixtures = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(fixtures)

RECORD = {"schema": publication.protocol.SOURCE_QUALIFICATION_SCHEMA,
          "science_revision": "1" * 40, "source_binding_sha256": "2" * 64,
          "semantic_policy_sha256": "4" * 64}


class QualificationReceiptTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.directory = Path(self.temporary.name).resolve()
        self.directory.chmod(0o700)
        self.counter = 0
        self.aws = fixtures.FakeAws()
        old = fixtures.context("p")
        old["enabled"] = False
        self.context = fixtures.context("p")
        self.context.update(science_revision="1" * 40, source_binding_sha256="2" * 64,
            adapter_sha256="3" * 64, policy_sha256="4" * 64,
            runtime_profile_sha256="5" * 64, source_qualification=deepcopy(RECORD))
        self.context["id"] = publication.protocol.context_id(self.context)
        bare = deepcopy(self.context)
        bare.pop("source_qualification")
        bare["id"] = publication.protocol.context_id(bare)
        contexts = SimpleNamespace(context=lambda identifier: deepcopy(bare))
        # The synthetic source RPC returns an independent fixed semantic fixture,
        # rather than mirroring a context/client expected-policy argument.
        native = SimpleNamespace(qualify_source=lambda identifier: deepcopy(RECORD))
        self.receipt = generator.qualify(contexts, bare["id"], native)
        self.policy = {"p": [old, self.context]}
        self.guard = fixtures.guard(self.aws, self.policy)
        self.path = self.write(self.receipt)
        self.guard["source_qualification_receipts"] = {
            self.context["id"]: hashlib.sha256(self.path.read_bytes()).hexdigest()}

    def tearDown(self):
        self.temporary.cleanup()

    def write(self, value=None, *, raw=None):
        self.counter += 1
        path = self.directory / f"receipt-{self.counter:02d}.json"
        raw = publication.protocol.canonical(value) + b"\n" if raw is None else raw
        with path.open("xb") as stream:
            stream.write(raw)
        path.chmod(0o600)
        return path

    def check(self, guard=None, policy=None, paths=None):
        return publication._validate_qualification_receipts(
            self.guard if guard is None else guard,
            self.policy if policy is None else policy,
            [self.path] if paths is None else paths)

    def repin(self, path):
        value = deepcopy(self.guard)
        value["source_qualification_receipts"] = {
            self.context["id"]: hashlib.sha256(path.read_bytes()).hexdigest()}
        return value

    def test_real_generator_contract_and_read_only_receipt_reuse(self):
        original = self.path.read_bytes()
        self.check()
        self.path.chmod(0o400)
        self.check()
        self.assertEqual(self.path.read_bytes(), original)
        self.assertEqual(self.receipt["qualified_context_id"], self.context["id"])

    def test_copied_context_pin_without_trusted_receipt_rejected_before_aws(self):
        bad = deepcopy(self.guard)
        bad.pop("source_qualification_receipts")
        with self.assertRaises(publication.PublicationError):
            publication.publish(bad, self.policy, apply=True, aws=self.aws,
                checkout_verifier=lambda guard: None)
        self.assertEqual(self.aws.update_calls, [])
        self.assertEqual(getattr(self.aws, "calls", []), [])

    def test_missing_extra_or_duplicate_receipt_inputs_are_rejected(self):
        for paths in ([], [self.path, self.path]):
            with self.subTest(count=len(paths)):
                with self.assertRaises(publication.PublicationError):
                    self.check(paths=paths)
        bad = deepcopy(self.guard)
        bad["source_qualification_receipts"]["9" * 64] = "a" * 64
        with self.assertRaises(publication.PublicationError):
            self.check(guard=bad)

    def test_exact_context_profile_adapter_source_and_semantic_bindings(self):
        changes = [
            ("qualified_context_id", "9" * 64),
            ("runtime_profile_sha256", "9" * 64),
            ("adapter_sha256", "9" * 64),
            ("input_context_id", "not-a-sha"),
            ("schema", "unknown"),
        ]
        for field, value in changes:
            with self.subTest(field=field):
                receipt = deepcopy(self.receipt)
                receipt[field] = value
                path = self.write(receipt)
                with self.assertRaises(publication.PublicationError):
                    self.check(guard=self.repin(path), paths=[path])
        for field in ("science_revision", "source_binding_sha256", "semantic_policy_sha256"):
            with self.subTest(field=field):
                receipt = deepcopy(self.receipt)
                receipt["qualification"][field] = "9" * (40 if field == "science_revision" else 64)
                path = self.write(receipt)
                with self.assertRaises(publication.PublicationError):
                    self.check(guard=self.repin(path), paths=[path])

    def test_extra_receipt_and_record_fields_are_rejected(self):
        for inside in (False, True):
            receipt = deepcopy(self.receipt)
            (receipt["qualification"] if inside else receipt)["private-canary"] = "private-canary"
            path = self.write(receipt)
            with self.assertRaises(publication.PublicationError):
                self.check(guard=self.repin(path), paths=[path])

    def test_raw_hash_is_checked_before_json_interpretation(self):
        path = self.write(raw=b"private-canary-not-json")
        with mock.patch.object(publication.json, "loads", side_effect=AssertionError("must-not-parse")):
            with self.assertRaises(publication.PublicationError) as error:
                self.check(paths=[path])
        self.assertNotIn("private-canary", str(error.exception))

    def test_only_exact_generator_canonical_bytes_are_accepted(self):
        noncanonical = json.dumps(self.receipt, indent=2).encode()
        duplicate = publication.protocol.canonical(self.receipt)[:-1] + b',"schema":"extra"}\n'
        for raw in (noncanonical, duplicate):
            path = self.write(raw=raw)
            with self.assertRaises(publication.PublicationError):
                self.check(guard=self.repin(path), paths=[path])

    def test_context_record_must_match_its_own_tuple_even_with_receipt(self):
        for field in ("science_revision", "source_binding_sha256", "semantic_policy_sha256"):
            policy = deepcopy(self.policy)
            policy["p"][1]["source_qualification"][field] = "9" * (40 if field == "science_revision" else 64)
            with self.assertRaises(publication.PublicationError):
                self.check(policy=policy)

    def test_disabled_contexts_need_no_receipts_and_absence_is_not_defaulted(self):
        context = deepcopy(self.context)
        context["enabled"] = False
        legacy = fixtures.context("p")
        before = publication.protocol.canonical(legacy)
        self.check(guard={}, policy={"p": [context, legacy]}, paths=[])
        self.assertEqual(publication.protocol.canonical(legacy), before)
        self.assertNotIn("source_qualification", legacy)

    def test_unchanged_enabled_legacy_can_be_published_without_qualification(self):
        old = fixtures.context("p")
        before = publication.protocol.canonical(old)
        merged = publication._merge_policies({"p": [old]}, {"p": [deepcopy(old)]})
        self.assertEqual(publication.protocol.canonical(merged["p"][0]), before)
        self.assertNotIn("source_qualification", merged["p"][0])

    def test_new_or_reenabled_unqualified_candidate_is_rejected(self):
        candidate = fixtures.context("p")
        with self.assertRaises(publication.PublicationError):
            publication._merge_policies({}, {"p": [candidate]})
        disabled = dict(candidate, enabled=False)
        with self.assertRaises(publication.PublicationError):
            publication._merge_policies({"p": [disabled]}, {"p": [candidate]})

    def test_existing_disabled_legacy_stays_loadable_and_byte_bound(self):
        legacy = fixtures.context("p")
        legacy["enabled"] = False
        merged = publication._merge_policies({"p": [legacy]}, {"p": [deepcopy(legacy)]})
        self.assertEqual(publication.protocol.canonical(merged["p"][0]),
                         publication.protocol.canonical(legacy))

    def test_private_input_mode_symlink_hardlink_parent_alias_and_size(self):
        cases = []
        public = self.write(self.receipt)
        public.chmod(0o640)
        cases.append(public)
        link = self.directory / "symlink.json"
        link.symlink_to(self.path)
        cases.append(link)
        hard = self.directory / "hard.json"
        os.link(self.path, hard)
        cases.append(hard)
        oversized = self.write(raw=b"x" * 4097)
        cases.append(oversized)
        for path in cases:
            with self.subTest(kind=path.name):
                with self.assertRaises(publication.PublicationError):
                    self.check(paths=[path])
        hard.unlink()
        alias = self.directory / "alias"
        alias.symlink_to(self.directory, target_is_directory=True)
        with self.assertRaises(publication.PublicationError):
            self.check(paths=[alias / self.path.name])

    def test_fifo_is_rejected_without_blocking(self):
        fifo = self.directory / "fifo.json"
        os.mkfifo(fifo, 0o600)
        with self.assertRaises(publication.PublicationError):
            self.check(paths=[fifo])

    def test_private_parent_required_without_changing_its_mode(self):
        self.directory.chmod(0o755)
        try:
            with self.assertRaises(publication.PublicationError):
                self.check()
            self.assertEqual(self.directory.stat().st_mode & 0o777, 0o755)
        finally:
            self.directory.chmod(0o700)


if __name__ == "__main__":
    unittest.main()
