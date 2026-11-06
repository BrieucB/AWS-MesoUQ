"""Synthetic definition-only recovery with unchanged normal context validators."""
from __future__ import annotations
import copy
import hashlib
import importlib.util
import io
from contextlib import redirect_stdout
import json
import os
from pathlib import Path
import stat
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[2]
SDK = ROOT / "src"
spec = importlib.util.spec_from_file_location(
    "recover_benchmark_context_registry", ROOT / "tools/recover_benchmark_context_registry.py")
recovery = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = recovery
spec.loader.exec_module(recovery)

class RecoveryTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="mesouq-registry-recovery-")
        self.root = Path(self.temporary.name).resolve()
        os.chmod(self.root, 0o700)
        self.contexts, self.validator = recovery.load_normal_sdk(SDK)
        import aws_mesouq.adapters as adapters
        current = hashlib.sha256(Path(adapters.__file__).with_name("benchmark.py").read_bytes()).hexdigest()
        self.operator = {
            "repository": "/synthetic/science-legacy", "science_revision": "a" * 40,
            "adapter_sha256": adapters.LEGACY_BENCHMARK_ADAPTER_SHA256,
            "python_bin": "/usr/bin/python3.11", "environment_script": "/synthetic/runtime.sh",
            "environment_sha256": "1" * 64, "run_root": "/synthetic/run-legacy",
            "archive_root": "/synthetic/archive-legacy", "expected_user": "fixtureuser",
            "expected_host": "fixture.cluster",
            "scheduler": {"account": "fixture-account", "partition": "qgpu_exp",
                          "qos": "fixture-qos", "cpus-per-task": 4, "mem": "16G"},
            "stage_seconds": {"reference": 300, "metrics": 300},
            "allowed_upload_origins": ["https://uploads.example.invalid"]}
        self.deadline = []
        for index, revision in enumerate(("b", "c")):
            operator = copy.deepcopy(self.operator)
            operator.update(repository="/synthetic/science-" + revision,
                            science_revision=revision * 40, adapter_sha256=current,
                            run_root="/synthetic/run-" + revision,
                            archive_root="/synthetic/archive-" + revision)
            self.deadline.append({"operator": operator,
                                  "runtime_clock_policy": self.contexts.DEADLINE_POLICY})
        self.original_registry = {"schema": self.contexts.REGISTRY_SCHEMA,
                                  "legacy_operator": self.operator, "contexts": self.deadline}
        self.initial_registry_path = self.root / "initial-registry.json"
        self.write(self.initial_registry_path, self.original_registry)
        registered = recovery.with_registry(self.contexts, self.validator,
                                            self.operator, "p", self.initial_registry_path)
        self.stored = copy.deepcopy(registered.contexts)
        self.legacy_context = {"project": "p", "owner_sub": "SYNTHETIC_PRIVATE_OWNER",
                               "api": "https://api.example.invalid", "operator": self.operator,
                               "connector_id": "SYNTHETIC_PRIVATE_ENROLLMENT"}
        sentinel = {key: value for key, value in self.legacy_context.items() if key != "operator"}
        sentinel["schema"] = self.contexts.JOURNAL_SCHEMA
        new_id = next(key for key, item in self.stored.items()
                      if item["operator"] == self.deadline[0]["operator"])
        campaign = {"id": "campaign", "project_id": "p", "owner_sub": self.legacy_context["owner_sub"],
                    "approved_adapter": {key: self.deadline[0]["operator"][key]
                                         for key in ("science_revision", "adapter_sha256", "archive_root")}}
        campaign["approved_adapter"]["execution_context_id"] = new_id
        namespace = {"context": sentinel, "legacy_context": self.legacy_context,
                     "contexts": self.stored,
                     "stages": {"stage": {"claim": {"campaign": campaign},
                                           "context_id": new_id,
                                           "history": ["SYNTHETIC_PRIVATE_STAGE_HISTORY"]}},
                     "campaigns": {"campaign": {
                         "root": self.deadline[0]["operator"]["run_root"] + "/campaign",
                         "context_id": new_id, "archive_event_id": "SYNTHETIC_PRIVATE_ARCHIVE"}},
                     "pending_event": {"body": "SYNTHETIC_PRIVATE_PENDING_EVENT"}}
        self.journal = {"benchmark_lab": namespace,
                        "jobs": {"calibration": "SYNTHETIC_PRIVATE_JOB"},
                        "archive_device": {"payload": "SYNTHETIC_PRIVATE_DEVICE"},
                        "owner_sub": "SYNTHETIC_PRIVATE_OWNER",
                        "history": ["SYNTHETIC_PRIVATE_GLOBAL_HISTORY"]}
        self.journal_dir = self.root / "journal"
        self.journal_dir.mkdir(mode=0o700)
        self.lock_path = self.journal_dir / "session.lock"
        self.lock_path.write_bytes(b"SYNTHETIC_PRIVATE_LOCK_CONTENT")
        os.chmod(self.lock_path, 0o600)
        self.lock_raw = self.lock_path.read_bytes()
        self.journal_path = self.journal_dir / "journal.json"
        self.operator_path = self.root / "operator.private.json"
        self.write(self.journal_path, self.journal)
        self.write(self.operator_path, self.operator)
        self.settings = {
            "schema": "aws_mesouq.private_v1_registry_recovery.v1", "sdk_root": str(SDK),
            "journal_path": str(self.journal_path), "legacy_operator_path": str(self.operator_path),
            "project": "p", "output_directory": str(self.root / "recovered")}
        self.journal_raw = self.journal_path.read_bytes()
        self.operator_raw = self.operator_path.read_bytes()

    def tearDown(self):
        self.temporary.cleanup()

    def write(self, path, value):
        path.write_text(json.dumps(value, sort_keys=True) + "\n")
        os.chmod(path, 0o600)

    def update_journal(self):
        self.write(self.journal_path, self.journal)

    def recover(self):
        return recovery.recover(self.settings)

    def assert_inputs_preserved(self):
        self.assertEqual(self.journal_path.read_bytes(), self.journal_raw)
        self.assertEqual(self.operator_path.read_bytes(), self.operator_raw)
        self.assertEqual(self.lock_path.read_bytes(), self.lock_raw)
        self.assertEqual(stat.S_IMODE(self.journal_path.stat().st_mode), 0o600)
        self.assertEqual(stat.S_IMODE(self.operator_path.stat().st_mode), 0o600)

    def test_complete_map_and_synthetic_namespace_are_exact(self):
        result = self.recover()
        self.assertEqual(result["context_count"], 3)
        self.assertEqual(result["nonlegacy_context_count"], 2)
        output = Path(self.settings["output_directory"])
        registry = json.loads((output / "benchmark-context-registry.json").read_text())
        normal = recovery.with_registry(self.contexts, self.validator, self.operator,
                                        "p", output / "benchmark-context-registry.json")
        self.assertEqual(normal.contexts, self.stored)
        self.assertEqual(normal.namespace(copy.deepcopy(self.journal["benchmark_lab"]),
                                          self.legacy_context), self.journal["benchmark_lab"])
        self.assertEqual(len(registry["contexts"]), 2)
        self.assert_inputs_preserved()

    def test_receipt_pins_original_and_companion_bytes(self):
        self.recover()
        output = Path(self.settings["output_directory"])
        receipt = json.loads((output / "installation-receipt.private.json").read_text())
        raw = (output / "benchmark-context-registry.json").read_bytes()
        self.assertEqual(receipt["registry_sha256"], hashlib.sha256(raw).hexdigest())
        self.assertEqual(receipt["original_operator_sha256"], hashlib.sha256(self.operator_raw).hexdigest())
        self.assertEqual(receipt["companion_installation_path"],
                         str(self.operator_path) + ".benchmark-context-registry.json")
        self.assertFalse(Path(receipt["companion_installation_path"]).exists())

    def test_no_job_identity_key_or_history_is_exported(self):
        self.recover()
        output = Path(self.settings["output_directory"])
        all_bytes = b"".join(path.read_bytes() for path in output.iterdir())
        self.assertNotIn(b"SYNTHETIC_PRIVATE_", all_bytes)
        self.assertEqual({p.name for p in output.iterdir()},
                         {"benchmark-context-registry.json", "installation-receipt.private.json"})

    def test_no_journal_or_identity_api_is_instantiated(self):
        from aws_mesouq.connector.journal import Journal
        import aws_mesouq.connector.signing as signing
        with mock.patch.object(Journal, "__init__", side_effect=AssertionError("forbidden Journal")), \
             mock.patch.object(signing, "device_identity", side_effect=AssertionError("forbidden identity")):
            self.assertEqual(self.recover()["status"], "prepared")

    def test_active_normal_journal_writer_blocks_recovery(self):
        from aws_mesouq.connector.journal import Journal
        active = Journal(self.journal_dir)
        try:
            with self.assertRaises(BlockingIOError):
                self.recover()
        finally:
            active.close()
        self.assert_inputs_preserved()
        self.assertFalse(Path(self.settings["output_directory"]).exists())

    def test_existing_lock_is_held_through_receipt_publication(self):
        from aws_mesouq.connector.journal import Journal
        original = recovery.write_new
        checked = []
        def observed(path, raw):
            if path.name == "installation-receipt.private.json":
                with self.assertRaises(BlockingIOError):
                    Journal(self.journal_dir)
                checked.append(True)
            return original(path, raw)
        with mock.patch.object(recovery, "write_new", side_effect=observed):
            self.recover()
        self.assertEqual(checked, [True])
        self.assert_inputs_preserved()

    def test_missing_lock_is_not_created(self):
        self.lock_path.unlink()
        with self.assertRaises(recovery.RecoveryRejected):
            self.recover()
        self.assertFalse(self.lock_path.exists())
        self.assertEqual(self.journal_path.read_bytes(), self.journal_raw)

    def test_unsafe_existing_lock_is_not_repaired(self):
        os.chmod(self.lock_path, 0o640)
        with self.assertRaises(recovery.RecoveryRejected):
            self.recover()
        self.assertEqual(stat.S_IMODE(self.lock_path.stat().st_mode), 0o640)
        self.assertEqual(self.lock_path.read_bytes(), self.lock_raw)

    def test_symlink_and_hardlink_lock_are_rejected(self):
        original = self.root / "original-lock"
        self.lock_path.rename(original)
        self.lock_path.symlink_to(original)
        with self.assertRaises(recovery.RecoveryRejected):
            self.recover()
        self.lock_path.unlink()
        os.link(original, self.lock_path)
        with self.assertRaises(recovery.RecoveryRejected):
            self.recover()
        self.assertEqual(original.read_bytes(), self.lock_raw)

    def test_existing_selector_environment_is_restored_without_read(self):
        name = self.contexts.REGISTRY_ENV
        with mock.patch.dict(os.environ, {name: "/synthetic/do-not-open.json"}):
            self.recover()
            self.assertEqual(os.environ[name], "/synthetic/do-not-open.json")

    def test_unknown_namespace_schema_is_rejected(self):
        self.journal["benchmark_lab"]["context"]["schema"] = "unsupported.v99"
        self.update_journal()
        with self.assertRaises(recovery.RecoveryRejected):
            self.recover()

    def test_missing_definition_map_is_not_reconstructed_from_history(self):
        del self.journal["benchmark_lab"]["contexts"]
        self.journal["benchmark_lab"]["history"] = {"would_be_operator": self.deadline[0]}
        self.update_journal()
        with self.assertRaises(recovery.RecoveryRejected):
            self.recover()
        self.assertFalse(Path(self.settings["output_directory"]).exists())

    def test_missing_legacy_definition_is_rejected(self):
        legacy = next(key for key, item in self.stored.items()
                      if item["runtime_clock_policy"] is None)
        del self.journal["benchmark_lab"]["contexts"][legacy]
        self.update_journal()
        with self.assertRaises(recovery.RecoveryRejected):
            self.recover()

    def test_no_nonlegacy_definition_is_rejected(self):
        self.journal["benchmark_lab"]["contexts"] = {
            key: item for key, item in self.stored.items() if item["runtime_clock_policy"] is None}
        self.update_journal()
        with self.assertRaises(recovery.RecoveryRejected):
            self.recover()

    def test_changed_stored_legacy_operator_is_rejected(self):
        legacy = next(item for item in self.stored.values() if item["runtime_clock_policy"] is None)
        legacy["operator"]["run_root"] = "/synthetic/changed"
        self.update_journal()
        with self.assertRaises(recovery.RecoveryRejected):
            self.recover()

    def test_wrong_context_identifier_is_rejected_by_normal_map_equality(self):
        key = next(key for key, item in self.stored.items() if item["runtime_clock_policy"] is not None)
        self.stored["d" * 64] = self.stored.pop(key)
        self.update_journal()
        with self.assertRaises(recovery.RecoveryRejected):
            self.recover()
        self.assertFalse((Path(self.settings["output_directory"]) / "benchmark-context-registry.json").exists())

    def test_unknown_context_fields_are_rejected(self):
        item = next(item for item in self.stored.values() if item["runtime_clock_policy"] is not None)
        item["unknown"] = "SYNTHETIC_PRIVATE_PAYLOAD"
        self.update_journal()
        with self.assertRaises(recovery.RecoveryRejected):
            self.recover()

    def test_unknown_context_policy_is_rejected(self):
        item = next(item for item in self.stored.values() if item["runtime_clock_policy"] is not None)
        item["runtime_clock_policy"] = "unknown-policy"
        self.update_journal()
        with self.assertRaises(recovery.RecoveryRejected):
            self.recover()

    def test_unsupported_adapter_is_rejected_by_existing_catalog(self):
        item = next(item for item in self.stored.values() if item["runtime_clock_policy"] is not None)
        item["operator"]["adapter_sha256"] = "e" * 64
        self.update_journal()
        with self.assertRaises(ValueError):
            self.recover()
        output = Path(self.settings["output_directory"])
        self.assertFalse((output / "benchmark-context-registry.json").exists())
        self.assertEqual(list(output.iterdir()), [])

    def test_unknown_operator_fields_are_rejected(self):
        item = next(item for item in self.stored.values() if item["runtime_clock_policy"] is not None)
        item["operator"]["credential"] = "SYNTHETIC_PRIVATE_TOKEN"
        self.update_journal()
        with self.assertRaises(ValueError):
            self.recover()

    def test_symlink_and_hardlink_journal_are_rejected(self):
        alias = self.journal_dir / "alias"
        alias.symlink_to(self.journal_path)
        with self.assertRaises(recovery.RecoveryRejected):
            recovery.ReadOnlySnapshot(alias, recovery.MAX_JOURNAL_BYTES)
        alias.unlink()
        os.link(self.journal_path, alias)
        with self.assertRaises(recovery.RecoveryRejected):
            self.recover()

    def test_unsafe_journal_permissions_are_rejected(self):
        os.chmod(self.journal_path, 0o640)
        with self.assertRaises(recovery.RecoveryRejected):
            self.recover()

    def test_non_journal_filename_is_rejected_before_read(self):
        self.settings["journal_path"] = str(self.journal_dir / "archive_ed25519.pem")
        with mock.patch.object(recovery, "ReadOnlySnapshot", side_effect=AssertionError("input read")):
            with self.assertRaises(recovery.RecoveryRejected):
                self.recover()

    def test_input_size_cap_is_enforced_before_json(self):
        self.assertEqual(recovery.MAX_JOURNAL_BYTES, 32 * 1024 * 1024)
        with self.journal_path.open("r+b") as stream:
            stream.truncate(recovery.MAX_JOURNAL_BYTES + 1)
        self.journal_raw = self.journal_path.read_bytes()
        with mock.patch.object(
                recovery, "parse", side_effect=AssertionError("oversized input was parsed")):
            with self.assertRaises(recovery.RecoveryRejected) as rejected:
                self.recover()
        self.assertEqual(rejected.exception.args, ("unsafe_private_input",))
        self.assertFalse(Path(self.settings["output_directory"]).exists())
        self.assert_inputs_preserved()

    def test_changed_source_journal_is_detected_before_publication(self):
        original = recovery.with_registry
        def changed(*args, **kwargs):
            value = original(*args, **kwargs)
            if args[-1] is not None:
                self.journal_path.write_bytes(self.journal_raw + b" ")
            return value
        with mock.patch.object(recovery, "with_registry", side_effect=changed):
            with self.assertRaises(recovery.RecoveryRejected):
                self.recover()
        self.assertFalse((Path(self.settings["output_directory"]) / "benchmark-context-registry.json").exists())

    def test_semantic_equal_raw_change_after_normal_read_is_rejected(self):
        original = recovery.with_registry
        output = Path(self.settings["output_directory"])
        def changed(*args, **kwargs):
            value = original(*args, **kwargs)
            path = args[-1]
            if path is not None and path.name == "benchmark-context-registry.json":
                path.write_bytes(path.read_bytes() + b" ")
            return value
        with mock.patch.object(recovery, "with_registry", side_effect=changed):
            with self.assertRaises(recovery.RecoveryRejected):
                self.recover()
        self.assertTrue((output / "benchmark-context-registry.json").exists())
        self.assertFalse((output / "installation-receipt.private.json").exists())
        self.assert_inputs_preserved()

    def test_receipt_failure_removes_only_owned_registry_output(self):
        original = recovery.write_new
        def fail_receipt(path, raw):
            if path.name == "installation-receipt.private.json":
                raise OSError("synthetic receipt failure")
            return original(path, raw)
        with mock.patch.object(recovery, "write_new", side_effect=fail_receipt):
            with self.assertRaises(OSError):
                self.recover()
        self.assertEqual(list(Path(self.settings["output_directory"]).iterdir()), [])
        self.assert_inputs_preserved()

    def test_replaced_output_symlink_never_reads_or_deletes_target(self):
        target = self.root / "new-output.json"
        excluded = self.root / "excluded-private.txt"
        excluded.write_bytes(b"SYNTHETIC_EXCLUDED_PRIVATE_BYTES")
        original_fsync = recovery.os.fsync
        original_open = recovery.os.open
        opened = []
        def tracked_open(path, *arguments, **keywords):
            opened.append(str(path))
            return original_open(path, *arguments, **keywords)
        def replaced(descriptor):
            target.unlink()
            target.symlink_to(excluded)
            original_fsync(descriptor)
        with (
            mock.patch.object(recovery.os, "open", side_effect=tracked_open),
            mock.patch.object(Path, "read_bytes",
                              side_effect=AssertionError("unexpected path-based output read")),
            mock.patch.object(recovery.os, "fsync", side_effect=replaced),
        ):
            with self.assertRaises(recovery.RecoveryRejected):
                recovery.write_new(target, b"owned output")
        self.assertNotIn(str(excluded), opened)
        self.assertTrue(target.is_symlink())
        self.assertEqual(excluded.read_bytes(), b"SYNTHETIC_EXCLUDED_PRIVATE_BYTES")

    def test_receipt_failure_preserves_replaced_foreign_registry(self):
        original = recovery.write_new
        output = Path(self.settings["output_directory"])
        target = output / "benchmark-context-registry.json"
        foreign = self.root / "foreign-registry.json"
        foreign.write_bytes(b"SYNTHETIC_FOREIGN_OUTPUT_PRESERVED")
        os.chmod(foreign, 0o600)
        def fail_receipt(path, raw):
            if path.name == "installation-receipt.private.json":
                os.replace(foreign, target)
                raise OSError("synthetic receipt failure after replacement")
            return original(path, raw)
        with mock.patch.object(recovery, "write_new", side_effect=fail_receipt):
            with self.assertRaises(OSError):
                self.recover()
        self.assertEqual(target.read_bytes(), b"SYNTHETIC_FOREIGN_OUTPUT_PRESERVED")
        self.assertFalse((output / "installation-receipt.private.json").exists())
        self.assert_inputs_preserved()

    def test_partial_write_failure_removes_own_new_file(self):
        target = self.root / "partial.json"
        original = recovery.os.fdopen
        class PartialWriter:
            def __init__(self, stream):
                self.stream = stream
            def __enter__(self):
                return self
            def __exit__(self, *arguments):
                self.stream.close()
            def write(self, raw):
                self.stream.write(raw[:1])
                raise OSError("synthetic partial write")
        def partial(descriptor, mode):
            return PartialWriter(original(descriptor, mode))
        with mock.patch.object(recovery.os, "fdopen", side_effect=partial):
            with self.assertRaises(OSError):
                recovery.write_new(target, b"synthetic private output")
        self.assertFalse(target.exists())
        self.assert_inputs_preserved()

    def test_failed_chmod_removes_own_exclusive_file(self):
        target = self.root / "chmod.json"
        with mock.patch.object(recovery.os, "fchmod", side_effect=OSError("synthetic chmod")):
            with self.assertRaises(OSError):
                recovery.write_new(target, b"synthetic")
        self.assertFalse(target.exists())

    def test_existing_output_directory_is_not_overwritten(self):
        output = Path(self.settings["output_directory"])
        output.mkdir(mode=0o700)
        marker = output / "retained.txt"
        marker.write_text("retained")
        with self.assertRaises(recovery.RecoveryRejected):
            self.recover()
        self.assertEqual(marker.read_text(), "retained")
        self.assert_inputs_preserved()

    def test_sdk_import_origin_mismatch_is_rejected(self):
        other = self.root / "wrong-sdk"
        other.mkdir(mode=0o700)
        self.settings["sdk_root"] = str(other)
        with self.assertRaises(recovery.RecoveryRejected):
            self.recover()
        self.assert_inputs_preserved()

    def test_duplicate_and_nonfinite_json_are_rejected(self):
        for raw in (b'{"a":1,"a":2}', b'{"a":NaN}', b'{"a":1e999}'):
            with self.assertRaises(recovery.RecoveryRejected):
                recovery.parse(raw)

    def test_known_rejection_emits_only_source_defined_error_code(self):
        settings = self.root / "invalid-settings.private.json"
        self.write(settings, {"private": "SYNTHETIC_PRIVATE_ERROR_CANARY"})
        captured = io.StringIO()
        with redirect_stdout(captured):
            status = recovery.main(["--settings", str(settings)])
        result = json.loads(captured.getvalue())
        self.assertEqual(status, 2)
        self.assertEqual(result["error_code"], "invalid_recovery_settings")
        self.assertNotIn("SYNTHETIC_PRIVATE_ERROR_CANARY", captured.getvalue())

    def test_other_or_payload_bearing_errors_never_expose_their_message(self):
        class Subclass(recovery.RecoveryRejected):
            pass
        canary = "SYNTHETIC_PRIVATE_ERROR_CANARY"
        errors = [ValueError(canary), type(canary, (Exception,), {})(canary),
                  recovery.RecoveryRejected(canary),
                  Subclass("invalid_recovery_settings"),
                  recovery.RecoveryRejected("invalid_recovery_settings", canary),
                  recovery.RecoveryRejected(123)]
        settings = self.root / "settings.private.json"
        self.write(settings, self.settings)
        for error in errors:
            with self.subTest(error_class=type(error).__name__):
                captured = io.StringIO()
                with mock.patch.object(recovery, "recover", side_effect=error), redirect_stdout(captured):
                    status = recovery.main(["--settings", str(settings)])
                result = json.loads(captured.getvalue())
                self.assertEqual(status, 2)
                self.assertEqual(result["error_code"], "registry_recovery_rejected")
                self.assertNotIn(canary, captured.getvalue())
                self.assertNotIn(str(self.journal_path), captured.getvalue())

    def test_large_journal_recovers_exact_contexts_without_exporting_unrelated_padding(self):
        marker = "SYNTHETIC_LARGE_UNRELATED_PADDING_"
        self.journal["unrelated_padding"] = marker + "x" * (16 * 1024 * 1024 + 4096)
        self.update_journal()
        self.journal_raw = self.journal_path.read_bytes()
        self.assertGreater(len(self.journal_raw), 16 * 1024 * 1024)
        self.assertLessEqual(len(self.journal_raw), recovery.MAX_JOURNAL_BYTES)
        settings_path = self.root / "large-settings.private.json"
        self.write(settings_path, self.settings)
        result = subprocess.run(
            [sys.executable, "-I", "-B",
             str(ROOT / "tools/recover_benchmark_context_registry.py"),
             "--settings", str(settings_path)],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False, timeout=20, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        summary = json.loads(result.stdout)
        self.assertEqual(summary["status"], "prepared")
        self.assertEqual(summary["context_count"], 3)
        self.assertTrue(summary["complete_context_map_equal"])
        output = Path(self.settings["output_directory"])
        registry_path = output / "benchmark-context-registry.json"
        registry_raw = registry_path.read_bytes()
        receipt_raw = (output / "installation-receipt.private.json").read_bytes()
        self.assertLess(len(registry_raw) + len(receipt_raw), 64 * 1024)
        for blob in (registry_raw, receipt_raw, result.stdout.encode(), result.stderr.encode()):
            self.assertNotIn(marker.encode(), blob)
        normal = recovery.with_registry(
            self.contexts, self.validator, self.operator, "p", registry_path)
        self.assertEqual(normal.contexts, self.stored)
        self.assertEqual(
            normal.namespace(copy.deepcopy(self.journal["benchmark_lab"]), self.legacy_context),
            self.journal["benchmark_lab"])
        self.assert_inputs_preserved()

    def test_normal_isolated_cli_keeps_all_actual_fields_private(self):
        settings = self.root / "settings.private.json"
        self.write(settings, self.settings)
        result = subprocess.run([sys.executable, "-I", "-B",
            str(ROOT / "tools/recover_benchmark_context_registry.py"), "--settings", str(settings)],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False, timeout=20, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        value = json.loads(result.stdout)
        self.assertEqual(value["status"], "prepared")
        self.assertEqual(value["context_count"], 3)
        self.assertNotIn("SYNTHETIC_PRIVATE_", result.stdout + result.stderr)
        self.assertNotIn(str(self.journal_path), result.stdout + result.stderr)
        self.assert_inputs_preserved()

if __name__ == "__main__":
    unittest.main()
