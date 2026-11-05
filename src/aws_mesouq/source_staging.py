"""Bounded immutable staging of authenticated scientific package sources."""
from __future__ import annotations

from contextlib import contextmanager
import fcntl
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import stat
import time
import uuid

_SCHEMA = "aws_mesouq.scientific_source_staging.v1"
_MAX_FILES = 10000
_MAX_MANIFEST_BYTES = 16*1024*1024


class SourceStagingError(RuntimeError):
    pass


def _require(condition, message):
    if not condition:
        raise SourceStagingError(message)


def _canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()


def _name(value):
    _require(isinstance(value, str) and len(value.encode()) <= 1024
             and "\\" not in value and "\x00" not in value,
             "bounded POSIX source member required")
    path = PurePosixPath(value)
    _require(not path.is_absolute() and path.as_posix() == value
             and len(path.parts) >= 2 and path.parts[0] == "meso_uq"
             and ".." not in path.parts and "__pycache__" not in path.parts
             and not value.endswith((".pyc", ".pyo")),
             "canonical scientific package member required")
    return value


def _directory(path, *, create=False, immutable=False):
    _require(path.is_absolute() and ".." not in path.parts
             and path == path.resolve(strict=False), "canonical source staging path required")
    for ancestor in (path, *path.parents):
        if ancestor.exists() or ancestor.is_symlink():
            _require(stat.S_ISDIR(ancestor.lstat().st_mode),
                     "source staging paths may not contain symlinks or files")
    if create and not path.exists():
        try:
            path.mkdir(mode=0o700)
        except FileExistsError:
            pass
        else:
            path.chmod(0o700)
    value = path.lstat()
    _require(stat.S_ISDIR(value.st_mode) and value.st_uid == os.getuid()
             and value.st_mode & 0o777 == (0o500 if immutable else 0o700),
             "owned private source staging directory required")


def _regular(path, *, immutable=False):
    value = path.lstat()
    _require(stat.S_ISREG(value.st_mode) and value.st_uid == os.getuid()
             and value.st_nlink == 1
             and value.st_mode & 0o777 == (0o400 if immutable else 0o600),
             "owned private regular source staging file required")
    return value


def _write(path, value, mode):
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    with os.fdopen(descriptor, "wb") as output:
        os.fchmod(output.fileno(), mode)
        output.write(value)
        output.flush()
        os.fsync(output.fileno())


def _read(path, *, immutable=False, maximum=_MAX_MANIFEST_BYTES):
    before = _regular(path, immutable=immutable)
    _require(before.st_size <= maximum, "bounded source staging metadata required")
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    with os.fdopen(descriptor, "rb") as source:
        value = source.read(maximum+1)
        after = os.fstat(source.fileno())
    _require((before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns, before.st_ctime_ns)
             == (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns, after.st_ctime_ns)
             and len(value) == before.st_size, "source staging metadata changed during reading")
    return value


def _open_lock(path):
    descriptor = os.open(path, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    value = os.fstat(descriptor)
    if not (stat.S_ISREG(value.st_mode) and value.st_uid == os.getuid()
            and value.st_nlink == 1 and not value.st_mode & 0o077):
        os.close(descriptor)
        raise SourceStagingError("private regular source lease lock required")
    os.fchmod(descriptor, 0o600)
    return descriptor


@contextmanager
def _admission_lock(path):
    descriptor = _open_lock(path)
    deadline = time.monotonic()+5.
    try:
        while True:
            try:
                fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                _require(time.monotonic() < deadline, "source staging admission is busy")
                time.sleep(.01)
        try:
            yield
        finally:
            fcntl.flock(descriptor, fcntl.LOCK_UN)
    finally:
        os.close(descriptor)


def _manifest(qualification, files, max_bytes):
    _require(isinstance(files, dict) and 0 < len(files) <= _MAX_FILES
             and all(isinstance(name, str) for name in files)
             and "meso_uq/__init__.py" in files,
             "explicit complete scientific package declaration required")
    records = {}
    total = 0
    for name, value in sorted(files.items()):
        _name(name)
        _require(type(value) is bytes, "immutable source/resource bytes required")
        total += len(value)
        _require(total <= max_bytes, "source payload exceeds staging budget")
        records[name] = {"sha256": hashlib.sha256(value).hexdigest(), "size_bytes": len(value)}
    _require(all(parent.as_posix() not in records
                 for name in records for parent in PurePosixPath(name).parents),
             "source members may not shadow package directories")
    manifest = {"schema": _SCHEMA, "qualification_sha256": qualification, "files": records}
    raw = _canonical(manifest)
    _require(len(raw) <= _MAX_MANIFEST_BYTES and total+len(raw) <= max_bytes,
             "source namespace and manifest exceed staging budget")
    return manifest


def _verify(namespace, manifest, qualification=None):
    _directory(namespace, immutable=True)
    _require(set(manifest) == {"schema", "qualification_sha256", "files"}
             and manifest["schema"] == _SCHEMA
             and manifest["qualification_sha256"] == (qualification or namespace.name)
             and isinstance(manifest["files"], dict)
             and 0 < len(manifest["files"]) <= _MAX_FILES,
             "source namespace manifest changed")
    expected_manifest = _canonical(manifest)
    _require(_read(namespace/"manifest.json", immutable=True) == expected_manifest,
             "source namespace manifest bytes changed")
    directories = {"src"}
    expected = {"manifest.json"}
    for name, record in manifest["files"].items():
        _name(name)
        _require(set(record) == {"sha256", "size_bytes"}
                 and isinstance(record["sha256"], str)
                 and re.fullmatch("[0-9a-f]{64}", record["sha256"]) is not None
                 and type(record["size_bytes"]) is int and record["size_bytes"] >= 0,
                 "source member descriptor changed")
        relative = PurePosixPath("src")/name
        expected.add(relative.as_posix())
        directories.update(parent.as_posix() for parent in relative.parents
                           if parent.as_posix() != ".")
    seen_files = set()
    seen_directories = set()
    pending = [namespace]
    while pending:
        parent = pending.pop()
        for entry in parent.iterdir():
            relative = entry.relative_to(namespace).as_posix()
            value = entry.lstat()
            if stat.S_ISDIR(value.st_mode):
                _require(relative in directories, "unexpected staged source directory")
                _directory(entry, immutable=True)
                seen_directories.add(relative)
                pending.append(entry)
            else:
                _require(relative in expected, "unexpected staged source member")
                _regular(entry, immutable=True)
                seen_files.add(relative)
    _require(seen_files == expected and seen_directories == directories,
             "staged source membership changed")
    total = len(expected_manifest)
    for name, record in manifest["files"].items():
        path = namespace/"src"/name
        before = _regular(path, immutable=True)
        _require(before.st_size == record["size_bytes"], "staged source member size changed")
        digest = hashlib.sha256()
        descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
        with os.fdopen(descriptor, "rb") as source:
            while chunk := source.read(1048576):
                digest.update(chunk)
            after = os.fstat(source.fileno())
        _require((before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns, before.st_ctime_ns)
                 == (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns, after.st_ctime_ns)
                 and digest.hexdigest() == record["sha256"],
                 "staged source member bytes changed")
        total += before.st_size
    return total


def _sync_directory(path):
    descriptor = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _discard_pending(path):
    """Remove only the unpublished private directory created by this call."""
    _require(stat.S_ISDIR(path.lstat().st_mode)
             and re.fullmatch(r"\.pending-[0-9a-f]{64}-[0-9]+-[0-9a-f]{32}", path.name),
             "owned unpublished source directory required")
    for current, directories, files in os.walk(path, topdown=False, followlinks=False):
        current = Path(current)
        current.chmod(0o700)
        for name in files:
            (current/name).unlink()
        for name in directories:
            child = current/name
            if child.is_symlink():
                child.unlink()
            else:
                child.rmdir()
    path.rmdir()


def _build_namespace(path, manifest, files):
    _directory(path)
    _write(path/"manifest.json", _canonical(manifest), 0o400)
    directories = {path, path/"src"}
    for name, value in files.items():
        destination = path/"src"/name
        destination.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        directories.update(parent for parent in destination.parents
                           if parent == path or parent.is_relative_to(path))
        _write(destination, value, 0o400)
    for directory in sorted(directories, key=lambda item: len(item.parts), reverse=True):
        directory.chmod(0o500)
        _sync_directory(directory)
    _verify(path, manifest, manifest["qualification_sha256"])


class SourceLease:
    def __init__(self, namespace, manifest, descriptor):
        self._namespace = namespace
        self._manifest = manifest
        self._descriptor = descriptor
        self._process = os.getpid()

    @property
    def source_root(self):
        return self._namespace/"src"

    def verify(self):
        _require(self._descriptor is not None and self._process == os.getpid(),
                 "active process-owned source lease required")
        size = _verify(self._namespace, self._manifest)
        return {"qualification_sha256": self._namespace.name,
                "manifest_sha256": hashlib.sha256(_canonical(self._manifest)).hexdigest(),
                "source_files": len(self._manifest["files"]), "logical_bytes": size}

    def close(self):
        if self._descriptor is not None:
            _require(self._process == os.getpid(), "source lease belongs to its opening process")
            descriptor, self._descriptor = self._descriptor, None
            try:
                fcntl.flock(descriptor, fcntl.LOCK_UN)
            finally:
                os.close(descriptor)

    def __enter__(self):
        self.verify()
        return self

    def __exit__(self, *exception):
        self.close()


def stage_sources(root, qualification_sha256, files, *, max_bytes, max_namespaces):
    """Stage declared source/resource bytes under a lifetime shared lease.

    max_bytes caps the TOTAL logical bytes of all namespace payloads and their
    manifests. max_namespaces also bounds admission. Capacity errors raise.
    Crash-left pending directories are preserved and require explicit recovery
    before further admission. They count toward the namespace capacity.
    """
    _require(isinstance(qualification_sha256, str)
             and re.fullmatch("[0-9a-f]{64}", qualification_sha256) is not None,
             "qualified runtime SHA256 required")
    _require(type(max_bytes) is int and max_bytes > 0
             and type(max_namespaces) is int and max_namespaces > 0,
             "literal positive source staging limits required")
    files = dict(files) if isinstance(files, dict) else files
    manifest = _manifest(qualification_sha256, files, max_bytes)
    incoming_bytes = len(_canonical(manifest))+sum(len(value) for value in files.values())
    root = Path(root)
    _directory(root, create=True)
    with _admission_lock(root/".admission.lock"):
        policy = {"schema": _SCHEMA, "max_bytes": max_bytes, "max_namespaces": max_namespaces}
        policy_path = root/"policy.json"
        if policy_path.exists():
            _require(_read(policy_path) == _canonical(policy), "source staging root policy changed")
        else:
            _write(policy_path, _canonical(policy), 0o600)
        leases = root/".leases"
        _directory(leases, create=True)
        namespaces = []
        pending_namespaces = []
        for entry in root.iterdir():
            if entry.name in ("policy.json", ".admission.lock"):
                _regular(entry)
            elif entry.name == ".leases":
                _directory(entry)
            elif re.fullmatch(r"\.pending-[0-9a-f]{64}-[0-9]+-[0-9a-f]{32}", entry.name):
                value = entry.lstat()
                _require(stat.S_ISDIR(value.st_mode) and value.st_uid == os.getuid()
                         and not value.st_mode & 0o077,
                         "unsafe unpublished source namespace")
                pending_namespaces.append(entry)
            else:
                _require(re.fullmatch("[0-9a-f]{64}", entry.name) is not None,
                         "unexpected source staging root entry")
                _directory(entry, immutable=True)
                namespaces.append(entry)
        _require(len(namespaces)+len(pending_namespaces) <= max_namespaces,
                 "source namespace count exceeded")
        _require(not pending_namespaces, "unpublished source namespace requires explicit recovery")
        stored = 0
        selected = root/qualification_sha256
        for namespace in namespaces:
            old = json.loads(_read(namespace/"manifest.json", immutable=True))
            stored += _verify(namespace, old)
        _require(stored <= max_bytes, "source staging byte budget exceeded")
        for entry in leases.iterdir():
            _require(entry.name.endswith(".lock")
                     and root/entry.name.removesuffix(".lock") in namespaces,
                     "unexpected source lease lock")
            _regular(entry)
        if selected.exists():
            _verify(selected, manifest)
        else:
            _require(len(namespaces) < max_namespaces and stored+incoming_bytes <= max_bytes,
                     "source staging capacity exhausted")
            pending = root/(".pending-"+qualification_sha256+"-"+str(os.getpid())+"-"+uuid.uuid4().hex)
            created = False
            try:
                pending.mkdir(mode=0o700)
                pending.chmod(0o700)
                created = True
                _build_namespace(pending, manifest, files)
                os.rename(pending, selected)
                _sync_directory(root)
            except BaseException:
                if created and pending.exists():
                    _discard_pending(pending)
                    _sync_directory(root)
                raise
        descriptor = _open_lock(leases/(qualification_sha256+".lock"))
        try:
            fcntl.flock(descriptor, fcntl.LOCK_SH | fcntl.LOCK_NB)
        except BaseException:
            os.close(descriptor)
            raise
    return SourceLease(selected, manifest, descriptor)
