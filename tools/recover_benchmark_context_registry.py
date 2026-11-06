"""Recover a complete private v1 registry from stored definition objects.

Inputs are read-only. The new output contains complete operator definitions and
a byte-pinned installation receipt for the deterministic companion file.
"""
from __future__ import annotations
import argparse
from copy import deepcopy
import hashlib
import fcntl
import importlib
import json
import math
import os
from pathlib import Path
import re
import stat
import sys
import tempfile

MAX_JOURNAL_BYTES = 16 * 1024 * 1024
MAX_OPERATOR_BYTES = 256 * 1024
COMPANION_SUFFIX = ".benchmark-context-registry.json"
RECOVERY_ERROR_CODES = frozenset((
    'duplicate_json_field',
    'existing_journal_lock_changed',
    'explicit_normal_journal_required',
    'explicit_operator_json_required',
    'input_changed',
    'input_paths_overlap',
    'input_size_changed',
    'invalid_journal_root',
    'invalid_known_project',
    'invalid_path',
    'invalid_recovery_settings',
    'invalid_stored_context_fields',
    'invalid_stored_context_identifier',
    'missing_complete_stored_contexts',
    'missing_direct_path',
    'missing_nonlegacy_contexts',
    'missing_versioned_namespace',
    'noncanonical_path',
    'nonfinite_json',
    'normal_context_map_changed',
    'output_directory_exists',
    'output_readback_failed',
    'published_context_map_changed',
    'published_output_changed',
    'registry_size_exceeded',
    'sdk_import_origin_changed',
    'stored_legacy_operator_changed',
    'unsafe_created_output',
    'unsafe_existing_journal_lock',
    'unsafe_private_directory',
    'unsafe_private_input',
    'unsupported_namespace_schema',
    'unsupported_recovery_settings',
    'unsupported_stored_context_policy',
))

class RecoveryRejected(RuntimeError):
    pass

def require(condition, code):
    if not condition:
        raise RecoveryRejected(code)

def digest(raw):
    return hashlib.sha256(raw).hexdigest()

def canonical(value, exists=True):
    require(isinstance(value, str), "invalid_path")
    path = Path(value)
    require(path.is_absolute() and str(path) == os.path.abspath(path)
            and str(path) == os.path.realpath(path), "noncanonical_path")
    if exists:
        require(path.exists() and not path.is_symlink(), "missing_direct_path")
    return path

def private_directory(path):
    path = canonical(str(path))
    info = path.lstat()
    require(stat.S_ISDIR(info.st_mode) and info.st_uid == os.geteuid()
            and stat.S_IMODE(info.st_mode) == 0o700, "unsafe_private_directory")
    return path

def stamp(info):
    return tuple(getattr(info, key) for key in (
        "st_dev", "st_ino", "st_mode", "st_uid", "st_nlink",
        "st_size", "st_mtime_ns", "st_ctime_ns"))

def parse(raw):
    def unique(pairs):
        result = {}
        for key, value in pairs:
            require(key not in result, "duplicate_json_field")
            result[key] = value
        return result
    def finite_float(value):
        result = float(value)
        require(math.isfinite(result), "nonfinite_json")
        return result
    def constant(_):
        raise RecoveryRejected("nonfinite_json")
    return json.loads(raw, object_pairs_hook=unique, parse_float=finite_float,
                      parse_constant=constant)

class ReadOnlySnapshot:
    def __init__(self, path, maximum):
        self.path = canonical(str(path))
        private_directory(self.path.parent)
        self.maximum = maximum
        self.fd = os.open(self.path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        try:
            self.info = os.fstat(self.fd)
            require(stat.S_ISREG(self.info.st_mode) and self.info.st_uid == os.geteuid()
                    and stat.S_IMODE(self.info.st_mode) == 0o600
                    and self.info.st_nlink == 1 and 0 < self.info.st_size <= maximum,
                    "unsafe_private_input")
            self.raw = self._read()
            self.verify()
        except BaseException:
            os.close(self.fd)
            self.fd = None
            raise

    def _read(self):
        os.lseek(self.fd, 0, os.SEEK_SET)
        data = bytearray()
        while len(data) <= self.maximum:
            chunk = os.read(self.fd, min(65536, self.maximum + 1 - len(data)))
            if not chunk:
                break
            data.extend(chunk)
        require(len(data) <= self.maximum, "input_size_changed")
        return bytes(data)

    def verify(self):
        require(self._read() == self.raw and len(self.raw) == self.info.st_size
                and stamp(os.fstat(self.fd)) == stamp(self.info)
                and stamp(self.path.lstat()) == stamp(self.info), "input_changed")

    def close(self):
        if self.fd is not None:
            os.close(self.fd)
            self.fd = None

class ExistingJournalLock:
    def __init__(self, journal_path):
        self.path = canonical(str(Path(journal_path).parent / "session.lock"))
        private_directory(self.path.parent)
        self.fd = os.open(self.path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        try:
            self.info = os.fstat(self.fd)
            require(stat.S_ISREG(self.info.st_mode) and self.info.st_uid == os.geteuid()
                    and stat.S_IMODE(self.info.st_mode) == 0o600 and self.info.st_nlink == 1,
                    "unsafe_existing_journal_lock")
            fcntl.flock(self.fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            self.verify()
        except BaseException:
            os.close(self.fd)
            self.fd = None
            raise

    def verify(self):
        require(stamp(os.fstat(self.fd)) == stamp(self.info)
                and stamp(self.path.lstat()) == stamp(self.info),
                "existing_journal_lock_changed")

    def close(self):
        if self.fd is not None:
            try:
                fcntl.flock(self.fd, fcntl.LOCK_UN)
            finally:
                os.close(self.fd)
                self.fd = None

def write_new(path, raw):
    path = canonical(str(path), exists=False)
    private_directory(path.parent)
    fd = os.open(path, os.O_RDWR | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    created = None
    try:
        created = os.fstat(fd)
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w+b") as stream:
            fd = None
            stream.write(raw)
            stream.flush()
            os.fsync(stream.fileno())
            before = os.fstat(stream.fileno())
            require(stat.S_ISREG(before.st_mode) and before.st_uid == os.geteuid()
                    and before.st_nlink == 1 and stat.S_IMODE(before.st_mode) == 0o600
                    and before.st_size == len(raw), "unsafe_created_output")
            stream.seek(0)
            readback = stream.read(len(raw) + 1)
            after = os.fstat(stream.fileno())
            current = path.lstat()
            require(readback == raw and stamp(before) == stamp(after) == stamp(current)
                    and (after.st_dev, after.st_ino) == (created.st_dev, created.st_ino),
                    "output_readback_failed")
            return stamp(after)
    except BaseException:
        if created is not None:
            try:
                current = path.lstat()
                if (current.st_dev == created.st_dev and current.st_ino == created.st_ino
                        and current.st_uid == os.geteuid() and current.st_nlink == 1
                        and stat.S_ISREG(current.st_mode)):
                    path.unlink()
            except OSError:
                pass
        raise
    finally:
        if fd is not None:
            os.close(fd)

def load_normal_sdk(root):
    root = canonical(str(root))
    sys.path.insert(0, str(root))
    contexts = importlib.import_module("aws_mesouq.benchmark_contexts")
    worker = importlib.import_module("aws_mesouq.benchmark_worker")
    for name, module in tuple(sys.modules.items()):
        if name == "aws_mesouq" or name.startswith("aws_mesouq."):
            source = getattr(module, "__file__", None)
            require(isinstance(source, str) and root in Path(source).resolve().parents,
                    "sdk_import_origin_changed")
    return contexts, worker.validate_operator

def with_registry(contexts, validator, legacy, project, path):
    name = contexts.REGISTRY_ENV
    present = name in os.environ
    previous = os.environ.get(name)
    try:
        if path is None:
            os.environ.pop(name, None)
        else:
            os.environ[name] = str(path)
        return contexts.ExecutionContexts(legacy, project, validator)
    finally:
        if present:
            os.environ[name] = previous
        else:
            os.environ.pop(name, None)

def derive_document(journal, original_operator, project, contexts, validator):
    require(isinstance(journal, dict), "invalid_journal_root")
    namespace = journal.get("benchmark_lab")
    require(isinstance(namespace, dict), "missing_versioned_namespace")
    marker = namespace.get("context")
    require(isinstance(marker, dict) and marker.get("schema") == contexts.JOURNAL_SCHEMA,
            "unsupported_namespace_schema")
    stored = namespace.get("contexts")
    require(isinstance(stored, dict) and 2 <= len(stored) <= 16,
            "missing_complete_stored_contexts")
    require(all(isinstance(key, str) and re.fullmatch(r"[0-9a-f]{64}", key)
                for key in stored), "invalid_stored_context_identifier")
    legacy = validator(deepcopy(original_operator))
    base = with_registry(contexts, validator, legacy, project, None)
    require(base.legacy_id in stored and stored[base.legacy_id] == base.contexts[base.legacy_id],
            "stored_legacy_operator_changed")
    entries = []
    for context_id in sorted(stored):
        entry = stored[context_id]
        require(isinstance(entry, dict) and set(entry) == {"operator", "runtime_clock_policy"},
                "invalid_stored_context_fields")
        if context_id == base.legacy_id:
            continue
        require(entry["runtime_clock_policy"] == contexts.DEADLINE_POLICY,
                "unsupported_stored_context_policy")
        validator(deepcopy(entry["operator"]))
        entries.append(deepcopy(entry))
    require(1 <= len(entries) <= 15, "missing_nonlegacy_contexts")
    document = {"schema": contexts.REGISTRY_SCHEMA,
                "legacy_operator": deepcopy(legacy), "contexts": entries}
    return document, deepcopy(stored)

def recover(settings):
    required = {"schema", "sdk_root", "journal_path", "legacy_operator_path",
                "project", "output_directory"}
    require(isinstance(settings, dict) and set(settings) == required,
            "invalid_recovery_settings")
    require(settings["schema"] == "aws_mesouq.private_v1_registry_recovery.v1",
            "unsupported_recovery_settings")
    project = settings["project"]
    require(isinstance(project, str) and re.fullmatch(r"[A-Za-z0-9_-]{1,128}", project),
            "invalid_known_project")
    contexts, validator = load_normal_sdk(settings["sdk_root"])
    journal = None
    operator = None
    lock = None
    pending = None
    pending_identity = None
    pending_fd = None
    published = []
    completed = False
    require(Path(settings["journal_path"]).name == "journal.json",
            "explicit_normal_journal_required")
    require(Path(settings["legacy_operator_path"]).suffix == ".json"
            and Path(settings["legacy_operator_path"]).name != "journal.json",
            "explicit_operator_json_required")
    output = canonical(settings["output_directory"], exists=False)
    private_directory(output.parent)
    require(not output.exists(), "output_directory_exists")
    try:
        lock = ExistingJournalLock(settings["journal_path"])
        journal = ReadOnlySnapshot(settings["journal_path"], MAX_JOURNAL_BYTES)
        operator = ReadOnlySnapshot(settings["legacy_operator_path"], MAX_OPERATOR_BYTES)
        require(journal.path != operator.path, "input_paths_overlap")
        document, stored = derive_document(parse(journal.raw), parse(operator.raw),
                                           project, contexts, validator)
        raw = (json.dumps(document, sort_keys=True, separators=(",", ":"),
                          allow_nan=False) + "\n").encode()
        require(len(raw) <= 256 * 1024, "registry_size_exceeded")
        output.mkdir(mode=0o700)
        os.chmod(output, 0o700)
        pending_fd, name = tempfile.mkstemp(prefix=".candidate-", dir=output)
        pending = Path(name)
        pending_info = os.fstat(pending_fd)
        pending_identity = (pending_info.st_dev, pending_info.st_ino)
        os.fchmod(pending_fd, 0o600)
        with os.fdopen(pending_fd, "wb") as stream:
            pending_fd = None
            stream.write(raw)
            stream.flush()
            os.fsync(stream.fileno())
        validated = with_registry(contexts, validator, document["legacy_operator"],
                                  project, pending)
        require(validated.contexts == stored, "normal_context_map_changed")
        journal.verify()
        operator.verify()
        target = output / "benchmark-context-registry.json"
        target_stamp = write_new(target, raw)
        published.append((target, target_stamp))
        require(stamp(target.lstat()) == target_stamp, "published_output_changed")
        final = with_registry(contexts, validator, document["legacy_operator"], project, target)
        require(final.contexts == stored, "published_context_map_changed")
        require(stamp(target.lstat()) == target_stamp, "published_output_changed")
        journal.verify()
        operator.verify()
        lock.verify()
        receipt = {
            "schema": "aws_mesouq.private_v1_registry_installation_receipt.v1",
            "status": "prepared", "registry_schema": contexts.REGISTRY_SCHEMA,
            "namespace_schema": contexts.JOURNAL_SCHEMA,
            "registry_path": str(target), "registry_sha256": digest(raw),
            "registry_size_bytes": len(raw), "registry_mode": "0600",
            "original_operator_path": str(operator.path),
            "original_operator_sha256": digest(operator.raw),
            "original_operator_size_bytes": len(operator.raw),
            "companion_installation_path": str(operator.path) + COMPANION_SUFFIX,
            "journal_path": str(journal.path), "journal_sha256": digest(journal.raw),
            "journal_size_bytes": len(journal.raw),
            "journal_bytes_unchanged": True, "original_operator_bytes_unchanged": True,
            "existing_journal_lock_held": True, "journal_lock_path": str(lock.path),
            "complete_context_map_equal": True, "context_count": len(stored),
            "nonlegacy_context_count": len(document["contexts"]),
            "source_definition_fields_used": ["benchmark_lab.context.schema",
                                               "benchmark_lab.contexts"],
            "unrelated_journal_fields_exported": False,
            "actual_companion_installed": False, "runtime_selector_activated": False,
            "normal_validator_source": str(Path(contexts.__file__).resolve()),
        }
        receipt_path = output / "installation-receipt.private.json"
        receipt_stamp = write_new(receipt_path,
                                  (json.dumps(receipt, sort_keys=True, indent=2) + "\n").encode())
        published.append((receipt_path, receipt_stamp))
        journal.verify()
        operator.verify()
        lock.verify()
        for path, expected in published:
            require(stamp(path.lstat()) == expected, "published_output_changed")
        directory_fd = os.open(output, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
        completed = True
        return {"status": "prepared", "context_count": len(stored),
                "nonlegacy_context_count": len(document["contexts"]),
                "journal_bytes_unchanged": True, "original_operator_bytes_unchanged": True,
                "complete_context_map_equal": True, "runtime_selector_activated": False}
    finally:
        if not completed:
            for path, expected in reversed(published):
                try:
                    current = path.lstat()
                    if (stamp(current) == expected and stat.S_ISREG(current.st_mode)
                            and current.st_uid == os.geteuid() and current.st_nlink == 1):
                        path.unlink()
                except FileNotFoundError:
                    pass
        if pending_fd is not None:
            os.close(pending_fd)
        if pending is not None and pending_identity is not None:
            try:
                current = pending.lstat()
                if ((current.st_dev, current.st_ino) == pending_identity
                        and stat.S_ISREG(current.st_mode) and current.st_uid == os.geteuid()
                        and current.st_nlink == 1):
                    pending.unlink()
            except FileNotFoundError:
                pass
        if operator is not None:
            operator.close()
        if journal is not None:
            journal.close()
        if lock is not None:
            lock.close()

def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--settings", required=True)
    args = parser.parse_args(argv)
    snapshot = None
    try:
        snapshot = ReadOnlySnapshot(args.settings, 16384)
        settings = parse(snapshot.raw)
        snapshot.verify()
        result = recover(settings)
        snapshot.verify()
        print(json.dumps(result, sort_keys=True))
        return 0
    except Exception as error:
        code = "registry_recovery_rejected"
        if (type(error) is RecoveryRejected and len(error.args) == 1
                and type(error.args[0]) is str and error.args[0] in RECOVERY_ERROR_CODES):
            code = error.args[0]
        error_class = "RecoveryRejected" if type(error) is RecoveryRejected else "Exception"
        print(json.dumps({"status": "rejected", "error_class": error_class,
                          "error_code": code}, sort_keys=True))
        return 2
    finally:
        if snapshot is not None:
            snapshot.close()

if __name__ == "__main__":
    raise SystemExit(main())
