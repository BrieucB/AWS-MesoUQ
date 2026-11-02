"""Fixed source-bound Karolina executor for the accuracy-v2 protocol."""
from __future__ import annotations

from contextlib import contextmanager
from copy import deepcopy
import argparse
import errno
import hashlib
import importlib.abc
import importlib.machinery
import importlib.metadata
import importlib.util
import json
import os
from pathlib import Path
import pwd
import re
import shlex
import socket
import stat
import subprocess
import sys
sys.dont_write_bytecode = True
import time
import uuid

CONTROL_SCHEMA = "aws_mesouq.benchmark_accuracy_native_control.v1"
DEPENDENCY_SCHEMA = "aws_mesouq.benchmark_accuracy_dependencies.v2"
WRAPPER_SCHEMA = "aws_mesouq.benchmark_accuracy_native_receipt.v1"
PUBLICATION_SCHEMA = "aws_mesouq.benchmark_accuracy_native_publication.v1"
MAX_FILE, MAX_CURATED, MAX_SOURCE = 67_108_864, 536_870_912, 134_217_728
MAX_CONTROL, MAX_METADATA = 4_194_304, 1_048_576
_SHA = re.compile(r"[a-f0-9]{64}")
_TERMINAL = {"COMPLETED", "FAILED", "CANCELLED", "TIMEOUT", "OUT_OF_MEMORY",
             "NODE_FAIL", "PREEMPTED", "BOOT_FAIL", "DEADLINE", "REVOKED"}


class NativeContractError(RuntimeError):
    pass


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"),
                      ensure_ascii=False, allow_nan=False).encode()


def checksum(value):
    return hashlib.sha256(canonical(value)).hexdigest()


def _object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise NativeContractError("Duplicate native metadata field")
        result[key] = value
    return result


def _constant(value):
    raise NativeContractError("Nonfinite native metadata rejected")


def _json(raw):
    return json.loads(raw, object_pairs_hook=_object, parse_constant=_constant)


def _keys(value, required, optional=()):
    if not isinstance(value, dict) or set(value) - set(required) - set(optional) or set(required) - set(value):
        raise NativeContractError("Native metadata fields differ from their schema")


def _sha(value):
    if not isinstance(value, str) or _SHA.fullmatch(value) is None:
        raise NativeContractError("Native SHA256 required")
    return value


def _safe_absolute(value):
    if not isinstance(value, str) or re.fullmatch(r"/[A-Za-z0-9_./-]+", value) is None:
        raise NativeContractError("Canonical approved absolute native path required")
    path = Path(value)
    if str(path) != value or ".." in path.parts or value == "/":
        raise NativeContractError("Native path is not canonical")
    return path


def _identity(info):
    return (info.st_dev, info.st_ino, info.st_uid, stat.S_IMODE(info.st_mode),
            info.st_nlink, info.st_size, info.st_mtime_ns, info.st_ctime_ns)


def _open_parent(path):
    path = _safe_absolute(str(path))
    current = os.open("/", os.O_RDONLY | os.O_DIRECTORY)
    try:
        for part in path.parts[1:-1]:
            child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=current)
            os.close(current)
            current = child
        return current, path.name
    except BaseException:
        os.close(current)
        raise


@contextmanager
def stable_bytes(path, *, maximum=MAX_FILE, private=False, single_link=True, owned=True):
    parent, name = _open_parent(path)
    fd = None
    try:
        fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=parent)
        info = os.fstat(fd)
        if (not stat.S_ISREG(info.st_mode) or info.st_size > maximum
                or (owned and info.st_uid != os.getuid()) or (single_link and info.st_nlink != 1)
                or stat.S_IMODE(info.st_mode) & (0o077 if private else 0o022)):
            raise NativeContractError("Native file is unsafe, aliased or exceeds its bound")
        with os.fdopen(fd, "rb") as stream:
            fd = None
            raw = stream.read(maximum + 1)
            if len(raw) != info.st_size or len(raw) > maximum or _identity(os.fstat(stream.fileno())) != _identity(info):
                raise NativeContractError("Native file changed while read")
            if _identity(os.stat(name, dir_fd=parent, follow_symlinks=False)) != _identity(info):
                raise NativeContractError("Native file was replaced while read")
            yield raw, _identity(info)
            stream.seek(0)
            if stream.read(maximum + 1) != raw:
                raise NativeContractError("Native file bytes changed during authenticated use")
            if (_identity(os.fstat(stream.fileno())) != _identity(info)
                    or _identity(os.stat(name, dir_fd=parent, follow_symlinks=False)) != _identity(info)):
                raise NativeContractError("Native file changed during authenticated use")
    finally:
        if fd is not None:
            os.close(fd)
        os.close(parent)


def _read_json(path, *, maximum=MAX_METADATA, private=True):
    with stable_bytes(path, maximum=maximum, private=private) as (raw, _):
        return _json(raw)


def _acl_absent(fd, name):
    try:
        os.getxattr(fd, name)
    except OSError as error:
        if error.errno == errno.ENODATA:
            return True
        raise NativeContractError("Private-directory ACL metadata unavailable") from error
    return False


def _normalize_new_private_directory(fd):
    # Only invoked for directories this call just created, before descendants.
    if os.fstat(fd).st_uid != os.getuid():
        raise NativeContractError("New private directory owner changed")
    for name in ("system.posix_acl_default", "system.posix_acl_access"):
        try:
            os.removexattr(fd, name)
        except OSError as error:
            if error.errno != errno.ENODATA:
                raise NativeContractError("Cannot isolate new private-directory ACL") from error
    os.fchmod(fd, 0o700)
    _check_private_directory(fd)


def _check_private_directory(fd):
    info = os.fstat(fd)
    if (info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) != 0o700
            or not _acl_absent(fd, "system.posix_acl_default")
            or not _acl_absent(fd, "system.posix_acl_access")):
        raise NativeContractError("Native private leaf requires owned mode0700 without inherited ACL")


def _mkdir(path):
    path = _safe_absolute(str(path))
    current = os.open("/", os.O_RDONLY | os.O_DIRECTORY)
    try:
        for index, part in enumerate(path.parts[1:]):
            created = False
            try:
                child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=current)
            except FileNotFoundError:
                try:
                    os.mkdir(part, 0o700, dir_fd=current)
                    created = True
                except FileExistsError:
                    pass
                child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=current)
            if created:
                _normalize_new_private_directory(child)
            if index == len(path.parts) - 2:
                _check_private_directory(child)
            os.close(current)
            current = child
    finally:
        os.close(current)
    return path


def immutable(path, raw):
    """Publish exact bytes once; recover only a closed own temporary alias."""
    if not isinstance(raw, bytes):
        raise TypeError("Immutable native write requires bytes")
    import fcntl
    path = _safe_absolute(str(path))
    _mkdir(path.parent)
    parent, name = _open_parent(path)
    lock_name = "write-lock-" + hashlib.sha256(name.encode()).hexdigest() + ".lock"
    lock = os.open(lock_name, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_NONBLOCK,
                   0o600, dir_fd=parent)
    temporary = "pending-" + name + "-" + uuid.uuid4().hex + ".tmp"
    created = False
    try:
        info = os.fstat(lock)
        if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid()
                or info.st_nlink != 1 or info.st_size != 0
                or stat.S_IMODE(info.st_mode) != 0o600):
            raise NativeContractError("Unsafe immutable publication lock")
        fcntl.flock(lock, fcntl.LOCK_EX)
        try:
            existing = os.stat(name, dir_fd=parent, follow_symlinks=False)
        except FileNotFoundError:
            existing = None
        if existing is not None and existing.st_nlink != 1:
            # A crash after link-before-unlink leaves exactly one temporary
            # alias. Remove only that exact closed, private, same-inode pair.
            pattern = re.compile("pending-" + re.escape(name) + "-[a-f0-9]{32}[.]tmp")
            aliases = []
            for candidate in os.listdir(parent):
                if pattern.fullmatch(candidate):
                    other = os.stat(candidate, dir_fd=parent, follow_symlinks=False)
                    if (other.st_dev, other.st_ino) == (existing.st_dev, existing.st_ino):
                        aliases.append(candidate)
            if existing.st_nlink != 2 or len(aliases) != 1:
                raise NativeContractError("Immutable publication has unclosed aliases")
            with stable_bytes(path, maximum=max(MAX_METADATA, len(raw)),
                              private=True, single_link=False) as (old, _):
                if old != raw:
                    raise NativeContractError("Immutable pending bytes changed")
            os.unlink(aliases[0], dir_fd=parent)
            os.fsync(parent)
        try:
            with stable_bytes(path, maximum=max(MAX_METADATA, len(raw)), private=True) as (old, _):
                if old != raw:
                    raise NativeContractError("Immutable native bytes changed")
                return str(path)
        except FileNotFoundError:
            pass
        fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                     0o600, dir_fd=parent)
        created = True
        with os.fdopen(fd, "wb") as stream:
            stream.write(raw)
            stream.flush()
            os.fsync(stream.fileno())
        os.link(temporary, name, src_dir_fd=parent, dst_dir_fd=parent,
                follow_symlinks=False)
        os.unlink(temporary, dir_fd=parent)
        created = False
        os.fsync(parent)
    finally:
        if created:
            os.unlink(temporary, dir_fd=parent)
        os.close(lock)
        os.close(parent)
    return str(path)

def immutable_json(path, value):
    return immutable(path, canonical(value))


def _file_sha(path, *, owned=True, private=False, single_link=True):
    with stable_bytes(path, maximum=MAX_SOURCE, private=private,
                      owned=owned, single_link=single_link) as (raw, _):
        return hashlib.sha256(raw).hexdigest()


@contextmanager
def _dependency_bytes(path, *, maximum):
    """Metadata-only group-write allowance behind a private owned ancestor."""
    path = _safe_absolute(str(path))
    descriptors = [os.open("/", os.O_RDONLY | os.O_DIRECTORY)]
    frames, private = [], False
    fd = None
    def directory_identity(info):
        return (info.st_dev, info.st_ino, info.st_uid, stat.S_IMODE(info.st_mode))
    try:
        for part in path.parts[1:-1]:
            parent = descriptors[-1]
            child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
                            dir_fd=parent)
            descriptors.append(child)
            info = os.fstat(child)
            expected = directory_identity(info)
            if directory_identity(os.stat(part, dir_fd=parent,
                                          follow_symlinks=False)) != expected:
                raise NativeContractError("Dependency metadata ancestor changed")
            frames.append((parent, part, child, expected))
            if info.st_uid == os.getuid() and stat.S_IMODE(info.st_mode) == 0o700:
                private = True
        if not private:
            raise NativeContractError("Dependency metadata requires owned private0700 ancestor")
        parent = descriptors[-1]
        fd = os.open(path.name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK,
                     dir_fd=parent)
        info = os.fstat(fd)
        if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid()
                or info.st_nlink != 1 or info.st_size > maximum
                or stat.S_IMODE(info.st_mode) & 0o002):
            raise NativeContractError("Unsafe dependency metadata file")
        with os.fdopen(fd, "rb") as stream:
            fd = None
            raw = stream.read(maximum + 1)
            if len(raw) != info.st_size or _identity(os.fstat(stream.fileno())) != _identity(info):
                raise NativeContractError("Dependency metadata changed while read")
            yield raw, _identity(info)
            stream.seek(0)
            if (stream.read(maximum + 1) != raw
                    or _identity(os.fstat(stream.fileno())) != _identity(info)
                    or _identity(os.stat(path.name, dir_fd=parent,
                                         follow_symlinks=False)) != _identity(info)):
                raise NativeContractError("Dependency metadata changed during authenticated use")
            for ancestor, part, child, expected in frames:
                if (directory_identity(os.fstat(child)) != expected
                        or directory_identity(os.stat(part, dir_fd=ancestor,
                            follow_symlinks=False)) != expected):
                    raise NativeContractError("Dependency metadata ancestor changed")
    finally:
        if fd is not None:
            os.close(fd)
        for descriptor in reversed(descriptors):
            os.close(descriptor)


def dependency_snapshot(names):
    """Interpreter/distribution metadata only; no ML or scientific model imports."""
    if not isinstance(names, list) or not 1 <= len(names) <= 512 or any(not isinstance(n, str) for n in names):
        raise NativeContractError("Bounded dependency names required")
    normalized = [re.sub(r"[-_.]+", "-", value).lower() for value in names]
    if normalized != sorted(set(normalized)) or any(re.fullmatch(r"[a-z0-9][a-z0-9-]*", n) is None for n in normalized):
        raise NativeContractError("Canonical dependency names required")
    real = Path(sys.executable).resolve(strict=True)
    pyvenv = Path(sys.prefix) / "pyvenv.cfg"
    if pyvenv.exists():
        with _dependency_bytes(pyvenv, maximum=65_536) as (raw_cfg, _):
            pyvenv_sha = hashlib.sha256(raw_cfg).hexdigest()
    else:
        pyvenv_sha = None
    rows = []
    for name in normalized:
        distribution = importlib.metadata.distribution(name)
        root = getattr(distribution, "_path", None)
        if not isinstance(root, Path) or not root.name.endswith(".dist-info"):
            raise NativeContractError("Reviewed unpacked dist-info metadata required")
        # Interpreter-selected site-packages may use the venv's legitimate
        # lib64 alias. Resolve that metadata directory, then enforce nofollow
        # and regular bytes on its direct real files; own Python sources never
        # receive this relaxation.
        root = root.resolve(strict=True)
        with _dependency_bytes(root / "METADATA", maximum=16_777_216) as (metadata, _):
            from email.parser import BytesParser
            parsed = BytesParser().parsebytes(metadata)
            versions = parsed.get_all("Version", [])
            declared_names = parsed.get_all("Name", [])
            if (len(versions) != 1 or not 1 <= len(versions[0]) <= 256
                    or len(declared_names) != 1
                    or re.sub(r"[-_.]+", "-", declared_names[0]).lower() != name):
                raise NativeContractError("Installed distribution identity is ambiguous")
            try:
                with _dependency_bytes(root / "RECORD", maximum=33_554_432) as (record, _):
                    record_sha = hashlib.sha256(record).hexdigest()
            except FileNotFoundError:
                record_sha = None
            rows.append({"name": name, "version": versions[0],
                "record_sha256": record_sha,
                "metadata_sha256": hashlib.sha256(metadata).hexdigest()})
    return {"schema": "aws_mesouq.benchmark_accuracy_distribution_metadata.v1",
        "python": {"version": sys.version.split()[0], "real_executable": str(real),
            "sha256": _file_sha(real, owned=False, single_link=False),
            "pyvenv_cfg_sha256": pyvenv_sha},
        "distributions": rows}


QUOTA_SCHEMA = "aws_mesouq.benchmark_accuracy_home_quota.v1"
QUOTA_MINIMUM_FREE_BYTES = 1_073_741_824
QUOTA_WARNING_FREE_BYTES = 5_368_709_120
QUOTA_MINIMUM_FREE_ENTRIES = 10_000


def parse_home_quota(raw, *, user, uid, observed_at, now):
    """Parse assigned user HOME limits, never filesystem-wide free capacity."""
    import math
    if (not isinstance(raw, str) or len(raw.encode()) > 16_384
            or type(uid) is not int or uid < 1
            or type(observed_at) not in (int, float)
            or type(now) not in (int, float)
            or not math.isfinite(observed_at) or not math.isfinite(now)
            or observed_at > now + 1 or now - observed_at > 30):
        raise NativeContractError("Fresh bounded assigned HOME quota metadata required")
    lines = [line.strip() for line in raw.splitlines() if line.strip()]
    if len(lines) != 3:
        raise NativeContractError("Exactly one assigned HOME quota row required")
    banner = re.fullmatch(r"Disk quotas for user ([A-Za-z0-9_.-]+) [(]uid ([0-9]+)[)]:",
                          lines[0])
    if banner is None or banner.group(1) != user or int(banner.group(2)) != uid:
        raise NativeContractError("Assigned HOME quota UID/user differs")
    if lines[1].split() != ["Filesystem", "blocks", "quota", "limit", "grace",
                            "files", "quota", "limit", "grace"]:
        raise NativeContractError("Assigned HOME quota columns differ")
    fields = lines[2].split()
    if (len(fields) != 10 or fields[1] != "/home"
            or re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.:/-]*:/home", fields[0]) is None
            or any(re.fullmatch(r"[0-9]+", value) is None for value in fields[2:])):
        raise NativeContractError("Assigned HOME quota row is ambiguous or nonnumeric")
    used, soft, hard, grace, entries, soft_entries, hard_entries, entries_grace = map(int, fields[2:])
    if (hard <= 0 or hard_entries <= 0 or grace != 0 or entries_grace != 0
            or (soft != 0 and soft > hard)
            or (soft_entries != 0 and soft_entries > hard_entries)):
        raise NativeContractError("Assigned HOME quota cap is unknown or grace is active")
    free_bytes, free_entries = (hard - used) * 1024, hard_entries - entries
    return {"schema": QUOTA_SCHEMA, "source": "quota -w -v -p --show-mntpoint -f /home",
        "user": user, "uid": uid, "filesystem": fields[0], "mount": "/home",
        "observed_at": observed_at, "raw_sha256": hashlib.sha256(raw.encode()).hexdigest(),
        "used_bytes": used * 1024, "hard_bytes": hard * 1024, "free_bytes": free_bytes,
        "used_entries": entries, "hard_entries": hard_entries, "free_entries": free_entries,
        "warning": free_bytes < QUOTA_WARNING_FREE_BYTES,
        "eligible": free_bytes >= QUOTA_MINIMUM_FREE_BYTES and
                    free_entries >= QUOTA_MINIMUM_FREE_ENTRIES}


def _preflight_snapshot(context, manifest_sha256, quota):
    result = {"schema": "aws_mesouq.benchmark_accuracy_preflight.v1",
        "context_id": context["id"], "manifest_sha256": _sha(manifest_sha256),
        "checked_epoch": quota["observed_at"], "home_quota": deepcopy(quota)}
    if len(canonical(result)) > 65_536:
        raise NativeContractError("Native preflight exceeds its scalar metadata bound")
    return result


def _home_preflight(context, *, manifest_sha256=None):
    # No username is passed: -f selects HOME, while the command queries this
    # actual authenticated UID. It must run afresh at every side-effect boundary.
    observed = subprocess.run(["quota", "-w", "-v", "-p", "--show-mntpoint", "-f", "/home"],
        env={"PATH": os.defpath, "LC_ALL": "C"},
        capture_output=True, text=True, timeout=15)
    if observed.returncode != 0 or observed.stderr:
        raise NativeContractError("Authoritative assigned HOME quota is unavailable")
    now = time.time()
    result = parse_home_quota(observed.stdout, user=context["native_user"],
                              uid=os.getuid(), observed_at=now, now=now)
    if not result["eligible"]:
        error = NativeContractError("Assigned HOME byte/inode headroom blocks new native work")
        if manifest_sha256 is not None:
            error.preflight = _preflight_snapshot(context, manifest_sha256, result)
        raise error
    return result



def _dependency_spec(spec):
    _keys(spec, {"python", "stdlib_paths", "overlays", "bootstrap", "snapshot", "distribution_names"})
    _keys(spec["python"], {"requested_executable", "pyvenv_cfg_path"})
    _safe_absolute(spec["python"]["requested_executable"])
    _safe_absolute(spec["python"]["pyvenv_cfg_path"])
    if not isinstance(spec["stdlib_paths"], list) or not 1 <= len(spec["stdlib_paths"]) <= 4:
        raise NativeContractError("Bounded explicit standard-library paths required")
    for name in spec["stdlib_paths"]:
        _safe_absolute(name)
    if len(set(spec["stdlib_paths"])) != len(spec["stdlib_paths"]):
        raise NativeContractError("Duplicate standard-library path")
    if not isinstance(spec["overlays"], list) or not 1 <= len(spec["overlays"]) <= 8:
        raise NativeContractError("Bounded explicit private dependency sites required")
    paths = []
    for row in spec["overlays"]:
        _keys(row, {"path", "origin", "source_content_sha256", "target_content_sha256"})
        path, origin = _safe_absolute(row["path"]), _safe_absolute(row["origin"])
        if any(path == prior or path in prior.parents or prior in path.parents for prior in paths):
            raise NativeContractError("Dependency sites overlap or repeat")
        if path == origin or origin in path.parents or path in origin.parents:
            raise NativeContractError("Independent dependency copy required")
        paths.append(path)
        _sha(row["source_content_sha256"])
        _sha(row["target_content_sha256"])
    _keys(spec["bootstrap"], {"path", "sha256", "declared_overlay_origins", "excluded_source_roots"})
    bootstrap = spec["bootstrap"]
    _safe_absolute(bootstrap["path"])
    _sha(bootstrap["sha256"])
    if bootstrap["declared_overlay_origins"] != [row["origin"] for row in spec["overlays"]]:
        raise NativeContractError("Bootstrap declaration differs from reviewed dependency origins")
    excluded = bootstrap["excluded_source_roots"]
    if not isinstance(excluded, list) or not 1 <= len(excluded) <= 8 or len(set(excluded)) != len(excluded):
        raise NativeContractError("Explicit excluded legacy source roots required")
    for name in excluded:
        root = _safe_absolute(name)
        if any(root == path or root in path.parents or path in root.parents for path in paths):
            raise NativeContractError("Legacy scientific root is present in dependency search paths")
    _keys(spec["snapshot"], {"receipt_path", "receipt_sha256",
                            "normalization_receipt_path", "normalization_receipt_sha256"})
    for key in ("receipt", "normalization_receipt"):
        _safe_absolute(spec["snapshot"][key + "_path"])
        _sha(spec["snapshot"][key + "_sha256"])
    names = spec["distribution_names"]
    if (not isinstance(names, list) or not 1 <= len(names) <= 512
            or names != sorted(set(names))
            or any(not isinstance(name, str) or re.fullmatch(r"[a-z0-9][a-z0-9-]*", name) is None for name in names)):
        raise NativeContractError("Canonical bounded distribution names required")
    return deepcopy(spec)


def _dependency_directory(path, *, private):
    path = _safe_absolute(str(path))
    descriptors = [os.open("/", os.O_RDONLY | os.O_DIRECTORY)]
    frames, private_seen = [], False
    def directory_identity(info):
        return (info.st_dev, info.st_ino, info.st_uid, stat.S_IMODE(info.st_mode))
    try:
        for part in path.parts[1:]:
            parent = descriptors[-1]
            child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=parent)
            descriptors.append(child)
            info = os.fstat(child)
            expected = directory_identity(info)
            if directory_identity(os.stat(part, dir_fd=parent, follow_symlinks=False)) != expected:
                raise NativeContractError("Dependency directory ancestor changed")
            frames.append((parent, part, child, expected))
            mode = stat.S_IMODE(info.st_mode)
            if info.st_uid == os.getuid() and mode == 0o700:
                private_seen = True
            if private_seen:
                if info.st_uid != os.getuid() or mode & 0o077:
                    raise NativeContractError("Private dependency directory permissions changed")
                if (not _acl_absent(child, "system.posix_acl_default")
                        or not _acl_absent(child, "system.posix_acl_access")):
                    raise NativeContractError("Dependency directory has inherited ACL")
        if (private and not private_seen) or (not private and (info.st_uid != 0 or mode & 0o022)):
            raise NativeContractError("Dependency or standard-library directory authority changed")
        if (not _acl_absent(child, "system.posix_acl_default")
                or not _acl_absent(child, "system.posix_acl_access")):
            raise NativeContractError("Dependency directory has inherited ACL")
        for parent, part, child, expected in frames:
            if (directory_identity(os.fstat(child)) != expected
                    or directory_identity(os.stat(part, dir_fd=parent, follow_symlinks=False)) != expected):
                raise NativeContractError("Dependency directory ancestor changed")
        return {"path": str(path), "device": info.st_dev, "inode": info.st_ino,
                "uid": info.st_uid, "mode": mode,
                "default_acl_absent": True, "extended_access_acl_absent": True}
    finally:
        for descriptor in reversed(descriptors):
            os.close(descriptor)


def _effective_distribution_metadata(names, paths):
    selected = {}
    for site_index, site in enumerate(paths):
        for distribution in importlib.metadata.distributions(path=[site]):
            root = getattr(distribution, "_path", None)
            if not isinstance(root, Path) or not root.name.endswith(".dist-info"):
                raise NativeContractError("Unpacked reviewed dist-info metadata required")
            root = _safe_absolute(str(root))
            if root.parent != Path(site):
                raise NativeContractError("Distribution metadata escaped its reviewed dependency site")
            with _dependency_bytes(root / "METADATA", maximum=16_777_216) as (metadata, _):
                from email.parser import BytesParser
                parsed = BytesParser().parsebytes(metadata)
                declared_names, versions = parsed.get_all("Name", []), parsed.get_all("Version", [])
                if (len(declared_names) != 1 or len(versions) != 1 or not 1 <= len(versions[0]) <= 256):
                    raise NativeContractError("Installed distribution identity is ambiguous")
                name = re.sub(r"[-_.]+", "-", declared_names[0]).lower()
                if re.fullmatch(r"[a-z0-9][a-z0-9-]*", name) is None:
                    raise NativeContractError("Installed distribution name is ambiguous")
                if name in selected:
                    continue  # Explicit ordered-site import precedence.
                try:
                    with _dependency_bytes(root / "RECORD", maximum=33_554_432) as (raw, _):
                        record_sha = hashlib.sha256(raw).hexdigest()
                except FileNotFoundError:
                    record_sha = None
                selected[name] = {"name": name, "version": versions[0],
                    "site_index": site_index, "metadata_path": str(root / "METADATA"),
                    "metadata_sha256": hashlib.sha256(metadata).hexdigest(),
                    "record_sha256": record_sha}
    if sorted(selected) != names:
        raise NativeContractError("Installed dependency inventory differs from reviewed names")
    return [selected[name] for name in names]


def isolated_dependency_snapshot(spec):
    """Observed metadata, not a claim that all installed binary bytes are immutable."""
    spec = _dependency_spec(spec)
    if not (sys.flags.isolated and sys.flags.no_site and sys.dont_write_bytecode):
        raise NativeContractError("Dependency metadata requires Python -I -S -B")
    python = spec["python"]
    real = Path(python["requested_executable"]).resolve(strict=True)
    if real != Path(sys.executable).resolve(strict=True):
        raise NativeContractError("Dependency probe executed a different interpreter")
    cfg = _file_sha(python["pyvenv_cfg_path"])
    snapshot = spec["snapshot"]
    for key in ("receipt", "normalization_receipt"):
        if _file_sha(snapshot[key + "_path"], private=True) != snapshot[key + "_sha256"]:
            raise NativeContractError("Dependency snapshot provenance changed")
    if _file_sha(spec["bootstrap"]["path"], owned=False) != spec["bootstrap"]["sha256"]:
        raise NativeContractError("Dependency bootstrap changed")
    stdlib = [_dependency_directory(path, private=False) for path in spec["stdlib_paths"]]
    overlays = [_dependency_directory(row["path"], private=True) for row in spec["overlays"]]
    effective = [row["path"] for row in overlays]
    rows = _effective_distribution_metadata(spec["distribution_names"], effective)
    return {"schema": DEPENDENCY_SCHEMA, "spec": spec,
        "observation": {
            "python": {"requested_executable": python["requested_executable"],
                "real_executable": str(real),
                "sha256": _file_sha(real, owned=False, single_link=False),
                "version": sys.version.split()[0], "pyvenv_cfg_sha256": cfg,
                "prefix": sys.prefix, "base_prefix": sys.base_prefix,
                "isolated": True, "site_disabled": True, "dont_write_bytecode": True},
            "stdlib": stdlib, "overlays": overlays, "distributions": rows}}


def _dependency_search_path(manifest, operator, guard):
    _keys(manifest, {"schema", "spec", "observation"})
    if manifest["schema"] != DEPENDENCY_SCHEMA:
        raise NativeContractError("Versioned isolated dependency metadata manifest required")
    spec = _dependency_spec(manifest["spec"])
    paths = [row["path"] for row in spec["overlays"]]
    if operator.get("dependency_overlays", []) != paths:
        raise NativeContractError("Runtime profile dependency sites differ from reviewed order")
    if (spec["python"]["requested_executable"] != operator["python_bin"]
            or spec["bootstrap"]["path"] != operator["environment_script"]
            or spec["bootstrap"]["sha256"] != operator["environment_sha256"]):
        raise NativeContractError("Dependency manifest differs from reviewed runtime profile")
    for name in [*paths, *spec["stdlib_paths"]]:
        path = Path(name)
        if any(path == root or path in root.parents or root in path.parents for root in guard.roots):
            raise NativeContractError("Dependency search path overlaps own source")
    _keys(manifest["observation"], {"python", "stdlib", "overlays", "distributions"})
    python = manifest["observation"]["python"]
    _keys(python, {"requested_executable", "real_executable", "sha256", "version",
                  "pyvenv_cfg_sha256", "prefix", "base_prefix", "isolated",
                  "site_disabled", "dont_write_bytecode"})
    if not all(python[key] is True for key in ("isolated", "site_disabled", "dont_write_bytecode")):
        raise NativeContractError("Isolated site-free dependency metadata required")
    real = Path(operator["python_bin"]).resolve(strict=True)
    if (str(real) != python["real_executable"]
            or _file_sha(real, owned=False, single_link=False) != operator["python_sha256"]
            or operator["python_sha256"] != python["sha256"]):
        raise NativeContractError("Reviewed real interpreter path/hash changed")
    guard.search_path = [*paths, *spec["stdlib_paths"]]
    return spec


class _FrozenLoader(importlib.abc.Loader):
    def __init__(self, path, raw, package):
        self.path, self.raw, self.package = path, raw, package

    def create_module(self, spec):
        return None

    def exec_module(self, module):
        module.__file__ = self.path
        if self.package:
            module.__path__ = [str(Path(self.path).parent)]
        exec(compile(self.raw, self.path, "exec", dont_inherit=True), module.__dict__)


class SourceGuard(importlib.abc.MetaPathFinder):
    def __init__(self, context, operator):
        self.context, self.operator = deepcopy(context), deepcopy(operator)
        self.snapshots, self.identities = {}, {}
        if checksum(operator) != context["runtime_profile_sha256"]:
            raise NativeContractError("Runtime profile checksum changed")
        entry = _safe_absolute(operator["adapter_path"])
        if entry.parts[-3:] != ("aws_mesouq", "adapters", "benchmark_accuracy.py"):
            raise NativeContractError("Fixed native entrypoint required")
        self.aws_root, self.science_root = entry.parents[2], _safe_absolute(operator["repository"]) / "src"
        self.roots = (self.aws_root, self.science_root)
        sources = operator["native_python_sources"]
        if not isinstance(sources, dict) or not 1 <= len(sources) <= 4096:
            raise NativeContractError("Bounded native source map required")
        total = 0
        for name, expected in sorted(sources.items()):
            path = _safe_absolute(name)
            if path.suffix != ".py" or not any(root in path.parents for root in self.roots):
                raise NativeContractError("Unreviewed native source path")
            _sha(expected)
            with stable_bytes(path, maximum=MAX_SOURCE) as (raw, identity):
                if hashlib.sha256(raw).hexdigest() != expected:
                    raise NativeContractError("Native source checksum changed")
                total += len(raw)
                if total > MAX_SOURCE:
                    raise NativeContractError("Native source closure exceeds its bound")
                self.snapshots[str(path)], self.identities[str(path)] = raw, identity
        if sources.get(str(entry)) != context["adapter_sha256"]:
            raise NativeContractError("Entrypoint differs from reviewed adapter")
        package_root = self.science_root / "meso_uq"
        actual = {str(path) for path in package_root.rglob("*.py")}
        declared = {name for name in sources if package_root in Path(name).parents}
        if actual != declared:
            raise NativeContractError("Complete scientific package source closure required")
        binding = {str(Path(name).relative_to(package_root)): sources[name] for name in sorted(declared)}
        if checksum(binding) != context["source_binding_sha256"]:
            raise NativeContractError("Scientific package binding changed")
        for package, root in (("aws_mesouq", self.aws_root), ("meso_uq", self.science_root)):
            if str(root / package / "__init__.py") not in sources:
                raise NativeContractError("Unbound native package initializer")
        self.installed = False

    def install(self):
        if not self.installed:
            for name, module in tuple(sys.modules.items()):
                if name == "meso_uq" or name.startswith("meso_uq.") or name == "aws_mesouq" or name.startswith("aws_mesouq."):
                    raise NativeContractError("Own-package module imported before source verification")
            if not hasattr(self, "search_path"):
                raise NativeContractError("Verified isolated dependency search path required")
            sys.path[:] = [str(self.aws_root), str(self.science_root), *self.search_path]
            for variable in ("PYTHONHOME", "PYTHONSTARTUP", "PYTHONUSERBASE", "PYTHONPLATLIBDIR"):
                os.environ.pop(variable, None)
            os.environ["PYTHONPATH"] = os.pathsep.join(sys.path)
            os.environ["PYTHONNOUSERSITE"] = "1"
            sys.meta_path.insert(0, self)
            sys.dont_write_bytecode = True
            self.installed = True
        return self

    def find_spec(self, fullname, path=None, target=None):
        if not (fullname == "aws_mesouq" or fullname.startswith("aws_mesouq.")
                or fullname == "meso_uq" or fullname.startswith("meso_uq.")):
            return None
        spec = importlib.machinery.PathFinder.find_spec(fullname, path)
        if spec is None or spec.origin is None or str(Path(spec.origin)) not in self.snapshots:
            raise ImportError("Unbound native own-package code/bytecode rejected")
        origin, package = str(Path(spec.origin)), spec.submodule_search_locations is not None
        return importlib.util.spec_from_loader(fullname,
            _FrozenLoader(origin, self.snapshots[origin], package), origin=origin, is_package=package)

    def check(self):
        for name, raw in self.snapshots.items():
            with stable_bytes(name, maximum=MAX_SOURCE) as (current, identity):
                if current != raw or identity != self.identities[name]:
                    raise NativeContractError("Native source changed during execution")


def _dependency_equivalent(current, expected):
    # st_dev is observer-local on shared mounts. /usr inode numbers can also
    # differ across login/compute nodes despite the same pinned interpreter ABI.
    def portable(value):
        value = deepcopy(value)
        for row in value["observation"]["overlays"]:
            row.pop("device", None)
        for row in value["observation"]["stdlib"]:
            row.pop("device", None)
            row.pop("inode", None)
        return value
    return portable(current) == portable(expected)


def _verify_profile(context, operator, *, allocated=False, dependencies=True):
    guard = SourceGuard(context, operator)
    if pwd.getpwuid(os.getuid()).pw_name != context["native_user"]:
        raise NativeContractError("Native user differs from reviewed identity")
    if not allocated:
        if operator["expected_host"] not in (socket.getfqdn(), socket.gethostname().split(".")[0] + ".karolina.it4i.cz"):
            raise NativeContractError("Native login node differs from reviewed identity")
    if _file_sha(operator["environment_script"], owned=False) != operator["environment_sha256"]:
        raise NativeContractError("Environment script changed")
    with stable_bytes(operator["dependency_manifest_path"], maximum=MAX_METADATA, private=True) as (raw, _):
        if hashlib.sha256(raw).hexdigest() != operator["dependency_manifest_sha256"]:
            raise NativeContractError("Dependency metadata manifest changed")
        manifest = _json(raw)
    spec = _dependency_search_path(manifest, operator, guard)
    commit = subprocess.run(["git", "-C", operator["repository"], "rev-parse", "HEAD"],
                            capture_output=True, text=True, timeout=15)
    if commit.returncode or commit.stdout.strip() != context["science_revision"]:
        raise NativeContractError("Scientific source revision changed")
    if dependencies:
        probe = ("import hashlib,json,os,stat,sys;"
                 "sys.dont_write_bytecode=True;"
                 "fd=os.open(sys.argv[1],os.O_RDONLY|os.O_NOFOLLOW|os.O_NONBLOCK);"
                 "f=os.fdopen(fd,'rb');i=os.fstat(f.fileno());"
                 "assert stat.S_ISREG(i.st_mode) and i.st_size<=67108864;"
                 "raw=f.read(67108865);"
                 "assert hashlib.sha256(raw).hexdigest()==sys.argv[3];"
                 "d={'__name__':'__dependency_probe__','__file__':sys.argv[1]};"
                 "exec(compile(raw,sys.argv[1],'exec',dont_inherit=True),d);"
                 "print(json.dumps(d['isolated_dependency_snapshot'](json.loads(sys.argv[2])),sort_keys=True))")
        environment = {key: value for key, value in os.environ.items()
                       if key in {"PATH", "HOME", "USER", "LOGNAME", "TMPDIR", "LANG", "LC_ALL"}}
        result = subprocess.run(["/bin/bash", "--noprofile", "--norc", "-c",
            'set -euo pipefail; source "$1" >/dev/null; exec "$2" -I -S -B -c "$3" "$4" "$5" "$6"',
            "accuracy-dependency-probe", operator["environment_script"], operator["python_bin"],
            probe, operator["adapter_path"], json.dumps(spec), context["adapter_sha256"]],
            env=environment, capture_output=True, timeout=45)
        if result.returncode:
            raise NativeContractError("Dependency environment metadata verification failed")
        if len(result.stdout) > MAX_METADATA:
            raise NativeContractError("Dependency probe metadata exceeds its bound")
        try:
            current = _json(result.stdout)
        except Exception:
            raise NativeContractError("Dependency probe did not return bounded metadata") from None
        if not _dependency_equivalent(current, manifest):
            raise NativeContractError("Dependency environment differs from reviewed metadata")
    guard.check()
    return guard


def _require_scratch(path):
    path = _safe_absolute(str(path))
    if path.parts[1:3] != ("scratch", "project") or len(path.parts) < 7:
        raise NativeContractError("New native calculations/staging require private assigned SCRATCH")
    return path


def _paths(context, manifest):
    cid, tid, attempt = manifest["campaign_instance_id"], manifest["task_id"], manifest["attempt"]
    if re.fullmatch(r"[a-f0-9]{32}", cid) is None or re.fullmatch(r"t[0-9]{8}-[a-f0-9]{32}", tid) is None:
        raise NativeContractError("Server-generated native execution IDs required")
    if type(attempt) is not int or attempt < 1:
        raise NativeContractError("Native attempt identity required")
    campaign = _safe_absolute(context["native_root"]) / cid
    return campaign, campaign / "tasks" / tid / ("attempt-%04d" % attempt)


def _runtime_environment(context, manifest):
    cache = (_safe_absolute(context["native_root"]) / "runtime-cache" /
             manifest["campaign_instance_id"] / manifest["task_id"])
    return {key: str(cache / leaf) for key, leaf in {
        "TMPDIR": "tmp", "TMP": "tmp", "TEMP": "tmp",
        "XDG_CACHE_HOME": "xdg", "MPLCONFIGDIR": "matplotlib",
        "KERAS_HOME": "keras", "TORCH_HOME": "torch",
        "TORCH_EXTENSIONS_DIR": "torch-extensions", "TORCHINDUCTOR_CACHE_DIR": "torch-inductor",
        "JAX_COMPILATION_CACHE_DIR": "jax", "CUDA_CACHE_PATH": "cuda",
        "TRITON_CACHE_DIR": "triton", "NUMBA_CACHE_DIR": "numba",
        "PIP_CACHE_DIR": "pip"}.items()}


def _validate_window(window, reservation):
    import math
    _keys(window, {"allocated_seconds", "internal_seconds",
                   "runtime_clock_policy", "time"})
    allocated = window["allocated_seconds"]
    internal = window["internal_seconds"]
    if (type(reservation) is not int or reservation < 60
            or type(allocated) is not int
            or allocated != min(reservation // 60 * 60, 172800)
            or type(internal) not in (int, float) or not math.isfinite(internal)
            or not 0 < internal <= allocated
            or window["runtime_clock_policy"] != "allocation_deadline_v1"
            or window["time"] != f"{allocated // 3600:02d}:{allocated // 60 % 60:02d}:{allocated % 60:02d}"):
        raise NativeContractError("Native window differs from funded original whole minutes")
    return deepcopy(window)


def _validate_control_identity(control):
    m, context = control["manifest"], control["context"]
    campaign, directory = _paths(context, m)
    if (checksum(control["operator"]) != context["runtime_profile_sha256"]
            or control["marker"] != "mqacc-" + m["campaign_instance_id"] + "-" + m["task_id"]
            or control["campaign_directory"] != str(campaign)
            or control["control_directory"] != str(directory)
            or m["context_id"] != context["id"]
            or m["source_binding_sha256"] != context["source_binding_sha256"]
            or m["policy_sha256"] != context["policy_sha256"]
            or m["reservation"]["native_cpus"] != context["native_cpus"]
            or m["reservation"]["native_gpus"] != context["native_gpus"]):
        raise NativeContractError("Native control differs from source-bound context/task")
    _validate_window(control["window"], m["reservation"]["gpu_seconds"])
    if control["runtime_environment"] != _runtime_environment(context, m):
        raise NativeContractError("Native cache/temp environment differs from source-bound policy")
    return control


def _script(control, context, operator, window):
    directives = {**operator["scheduler"], "cpus-per-task": context["native_cpus"],
                  "gres": "gpu:1", "time": window["time"]}
    directory = Path(control["control_directory"])
    lines = ["#!/bin/bash", "#SBATCH --job-name=" + control["marker"],
        "#SBATCH --chdir=" + str(directory),
        "#SBATCH --output=" + str(directory / "stdout.log"),
        "#SBATCH --error=" + str(directory / "stderr.log")]
    lines.extend("#SBATCH --" + name + "=" + str(value) for name, value in directives.items())
    dollar = chr(36)
    lines.extend(["set -euo pipefail", "umask 077",
        'readonly AWS_MESOUQ_ALLOCATION_JOB_ID="' + dollar + '{SLURM_JOB_ID:?Missing native job ID}"',
        'readonly AWS_MESOUQ_ALLOCATION_START_EPOCH="' + dollar + '{SLURM_JOB_START_TIME:?Missing native start time}"',
        'readonly AWS_MESOUQ_ALLOCATION_DEADLINE_EPOCH="' + dollar + '{SLURM_JOB_END_TIME:?Missing native deadline}"',
        "export AWS_MESOUQ_ALLOCATION_JOB_ID AWS_MESOUQ_ALLOCATION_START_EPOCH AWS_MESOUQ_ALLOCATION_DEADLINE_EPOCH",
        "test \"" + dollar + "(sha256sum " + shlex.quote(operator["environment_script"]) +
            " | cut -d' ' -f1)\" = " + shlex.quote(operator["environment_sha256"]),
        "source " + shlex.quote(operator["environment_script"]),
        "cd -- " + shlex.quote(str(directory)),
        "unset PYTHONHOME PYTHONSTARTUP PYTHONUSERBASE PYTHONPLATLIBDIR",
        "export PYTHONNOUSERSITE=1",
        "export PYTHONPATH=" + shlex.quote(os.pathsep.join([
            str(Path(operator["adapter_path"]).parents[2]),
            str(Path(operator["repository"]) / "src"),
            *operator.get("dependency_overlays", [])])),
        "export MESOUQ_SITE=karolina", "export PYTHONDONTWRITEBYTECODE=1"])
    for variable, path in _runtime_environment(context, control["manifest"]).items():
        lines.extend(["install -d -m 0700 " + shlex.quote(path),
                      "export " + variable + "=" + shlex.quote(path)])
    if control["manifest"]["kind"] in ("hnpe_initialize", "hnpe_advance"):
        lines.append("export KERAS_BACKEND=jax")
    elif control["manifest"]["kind"] in ("guide_hnpe_inference", "confirmation_hnpe_inference"):
        lines.append("export KERAS_BACKEND=torch")
    command = [operator["python_bin"], "-I", "-S", "-B", "-c", _NATIVE_BOOTSTRAP,
        operator["adapter_path"], context["adapter_sha256"], str(directory / "control.json"),
        checksum(control), "--manifest"]
    lines.append("exec " + shlex.join(command))
    return "\n".join(lines) + "\n"


def _prepare(payload):
    context, operator, claim, window = (payload[name] for name in ("context", "operator", "claim", "window"))
    if context.get("enabled") is not True:
        raise NativeContractError("Disabled context cannot stage new native work")
    guard = _verify_profile(context, operator)
    guard.install()
    from aws_mesouq import benchmark_accuracy_protocol as protocol
    protocol.validate_context(context)
    manifest = protocol.validate_manifest(claim["task"]["manifest"])
    if protocol.checksum(manifest) != claim["task"]["manifest_sha256"]:
        raise NativeContractError("Prepared manifest checksum changed")
    _validate_window(window, manifest["reservation"]["gpu_seconds"])
    _require_scratch(context["native_root"])
    quota = _home_preflight(context, manifest_sha256=claim["task"]["manifest_sha256"])
    campaign, directory = _paths(context, manifest)
    _mkdir(directory)
    for path in _runtime_environment(context, manifest).values():
        _mkdir(path)
    control = {"schema": CONTROL_SCHEMA, "context": deepcopy(context), "operator": deepcopy(operator),
        "manifest": deepcopy(manifest), "manifest_sha256": claim["task"]["manifest_sha256"],
        "marker": claim["task"]["marker"], "window": deepcopy(window),
        "control_directory": str(directory), "campaign_directory": str(campaign),
        "runtime_environment": _runtime_environment(context, manifest)}
    _validate_control_identity(control)
    immutable_json(directory / "control.json", control)
    immutable_json(directory / "request.json", manifest["request"])
    immutable_json(directory / "task-manifest.json", manifest)
    script = _script(control, context, operator, window).encode()
    path = immutable(directory / "run.sh", script)
    guard.check()
    sha = hashlib.sha256(script).hexdigest()
    return {"script_path": path, "script_sha256": sha,
        "intent_sha256": checksum({"manifest_sha256": control["manifest_sha256"],
            "script_sha256": sha, "marker": control["marker"], "window": window}),
        "marker": control["marker"], "window": deepcopy(window),
        "preflight": _preflight_snapshot(context, control["manifest_sha256"], quota)}


# Fixed standard-library bootstrap. It authenticates the entrypoint before
# executing it, and never adds browser-selected import paths.
_BOOTSTRAP = r"""import hashlib,json,os,stat,sys
raw=sys.stdin.buffer.read(4194305)
if len(raw)>4194304: raise ValueError('Native control request exceeds bound')
def pairs(items):
 d={}
 for k,v in items:
  if k in d: raise ValueError('Duplicate native control field')
  d[k]=v
 return d
request=json.loads(raw,object_pairs_hook=pairs)
context,operator=request['context'],request['operator']
path=operator['adapter_path']
if not isinstance(path,str) or not path.startswith('/') or '..' in path.split('/'):
 raise ValueError('Invalid native entrypoint')
parent=os.open('/',os.O_RDONLY|os.O_DIRECTORY)
try:
 parts=path.split('/')[1:]
 for part in parts[:-1]:
  child=os.open(part,os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW,dir_fd=parent)
  os.close(parent);parent=child
 fd=os.open(parts[-1],os.O_RDONLY|os.O_NOFOLLOW|os.O_NONBLOCK,dir_fd=parent)
 with os.fdopen(fd,'rb') as f:
  before=os.fstat(f.fileno())
  def identity(s):
   return (s.st_dev,s.st_ino,s.st_uid,s.st_mode,s.st_nlink,s.st_size,s.st_mtime_ns,s.st_ctime_ns)
  if not stat.S_ISREG(before.st_mode) or before.st_uid!=os.getuid() or before.st_mode&0o022 or before.st_nlink!=1 or before.st_size>67108864:
   raise ValueError('Unsafe native entrypoint')
  source=f.read(67108865)
  if hashlib.sha256(source).hexdigest()!=context['adapter_sha256'] or identity(before)!=identity(os.fstat(f.fileno())):
   raise ValueError('Native entrypoint checksum changed')
  namespace={'__name__':'__accuracy_verified_rpc__','__file__':path}
  exec(compile(source,path,'exec',dont_inherit=True),namespace)
  try:
   result=namespace['remote_request'](request)
  except Exception as error:
   preflight=getattr(error,'preflight',None)
   if preflight is None: raise
   result={'schema':'aws_mesouq.benchmark_accuracy_native_error.v1','code':'home_quota_unhealthy','preflight':preflight}
  if identity(before)!=identity(os.fstat(f.fileno())) or identity(before)!=identity(os.stat(parts[-1],dir_fd=parent,follow_symlinks=False)):
   raise ValueError('Native entrypoint changed during request')
  if request['operation']=='read_artifact':
   if not isinstance(result,bytes): raise TypeError('Native artifact bytes required')
   sys.stdout.buffer.write(result)
  else:
   output=json.dumps(result,sort_keys=True,separators=(',',':'),ensure_ascii=False,allow_nan=False).encode()
   if len(output)>1048576: raise ValueError('Native response exceeds bound')
   sys.stdout.buffer.write(output)
finally: os.close(parent)
"""



_NATIVE_BOOTSTRAP = r"""import hashlib,os,stat,sys
sys.dont_write_bytecode=True
entry,expected,path,control_sha,action=sys.argv[1:]
if action not in ('--manifest','--execute-stage'):
 raise ValueError('Invalid fixed native action')
def ident(s):
 return(s.st_dev,s.st_ino,s.st_uid,s.st_mode,s.st_nlink,s.st_size,s.st_mtime_ns,s.st_ctime_ns)
def opened(path,maximum):
 if not path.startswith('/') or '..' in path.split('/'): raise ValueError('Invalid native path')
 parts=path.split('/')[1:];parent=os.open('/',os.O_RDONLY|os.O_DIRECTORY)
 try:
  for part in parts[:-1]:
   child=os.open(part,os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW,dir_fd=parent)
   os.close(parent);parent=child
  fd=os.open(parts[-1],os.O_RDONLY|os.O_NOFOLLOW|os.O_NONBLOCK,dir_fd=parent)
  f=os.fdopen(fd,'rb');before=os.fstat(f.fileno())
  if not stat.S_ISREG(before.st_mode) or before.st_uid!=os.getuid() or before.st_mode&0o022 or before.st_nlink!=1 or before.st_size>maximum:
   f.close();raise ValueError('Unsafe native source/control')
  raw=f.read(maximum+1)
  if len(raw)!=before.st_size or ident(before)!=ident(os.fstat(f.fileno())):
   f.close();raise ValueError('Native bytes changed while read')
  return parent,parts[-1],f,before,raw
 except BaseException: os.close(parent);raise
cp,cn,cf,ci,cr=opened(path,4194304)
ep,en,ef,ei,er=opened(entry,67108864)
try:
 if hashlib.sha256(cr).hexdigest()!=control_sha or hashlib.sha256(er).hexdigest()!=expected:
  raise ValueError('Native source/control checksum changed before execution')
 namespace={'__name__':'__accuracy_verified_native__','__file__':entry}
 exec(compile(er,entry,'exec',dont_inherit=True),namespace)
 status=namespace['main']([action,path])
 for parent,name,f,before,raw in ((cp,cn,cf,ci,cr),(ep,en,ef,ei,er)):
  f.seek(0)
  if f.read(len(raw)+1)!=raw or ident(before)!=ident(os.fstat(f.fileno())) or ident(before)!=ident(os.stat(name,dir_fd=parent,follow_symlinks=False)):
   raise ValueError('Native source/control changed during execution')
finally:
 cf.close();ef.close();os.close(cp);os.close(ep)
raise SystemExit(status)
"""


def _prepared_preflight(payload):
    context, entry = payload["context"], payload["entry"]
    if context.get("enabled") is not True:
        raise NativeContractError("Disabled context cannot authorize new submission")
    control = _entry_control(context, entry)
    preparation = entry["native_preparation"]
    _keys(preparation, {"script_path", "script_sha256", "intent_sha256", "marker", "window"},
          {"preflight"})
    script_path = Path(control["control_directory"]) / "run.sh"
    if (preparation["script_path"] != str(script_path)
            or preparation["marker"] != control["marker"]
            or preparation["window"] != control["window"]):
        raise NativeContractError("Prepared execution identity changed before submission")
    with stable_bytes(script_path, maximum=MAX_CONTROL, private=True) as (raw, _):
        sha = hashlib.sha256(raw).hexdigest()
        expected = checksum({"manifest_sha256": control["manifest_sha256"],
            "script_sha256": sha, "marker": control["marker"], "window": control["window"]})
        if sha != preparation["script_sha256"] or expected != preparation["intent_sha256"]:
            raise NativeContractError("Prepared native script/intent bytes changed")
        quota = _home_preflight(context, manifest_sha256=control["manifest_sha256"])
        return _preflight_snapshot(context, control["manifest_sha256"], quota)

def _no_coordinator_secrets(value):
    if isinstance(value, dict):
        forbidden = {"lease", "lease_token", "refresh_token", "access_token",
            "id_token", "authorization", "cookie", "private_key", "signature",
            "pending_event", "pending_refresh", "upload_plan", "upload_progress",
            "signed_body", "upload_url", "presigned_url", "nonce", "raw_nonce",
            "confirmation_nonce"}
        if {str(key).lower() for key in value} & forbidden:
            raise NativeContractError("Coordinator credentials are not native inputs")
        for child in value.values():
            _no_coordinator_secrets(child)
    elif isinstance(value, list):
        for child in value:
            _no_coordinator_secrets(child)


def _native_claim(claim):
    task = claim["task"]
    result = {"task": {key: deepcopy(task[key]) for key in
                      ("manifest", "manifest_sha256", "marker")}}
    _no_coordinator_secrets(result)
    return result


def _native_entry(entry):
    claim = _native_claim(entry["claim"])
    claim["task"]["status"] = entry["claim"]["task"]["status"]
    claim["campaign"] = {"status": entry["claim"]["campaign"]["status"]}
    preparation = entry.get("native_preparation")
    if preparation is not None:
        # Dynamic quota proofs and coordinator histories are not native inputs.
        preparation = {key: deepcopy(preparation[key]) for key in
            ("script_path", "script_sha256", "intent_sha256", "marker", "window")}
    result = {"context_id": entry["context_id"],
        "manifest_sha256": entry["manifest_sha256"], "job_id": entry.get("job_id"),
        "claim": claim, "native_preparation": preparation}
    _no_coordinator_secrets(result)
    return result


def _native_payload(operation, payload):
    if operation == "verify_profile":
        _keys(payload, set())
        return {}
    if operation == "prepare":
        _keys(payload, {"claim", "window"})
        result = {"claim": _native_claim(payload["claim"]),
                  "window": deepcopy(payload["window"])}
    elif operation in {"preflight", "observe"}:
        _keys(payload, {"entry"})
        result = {"entry": _native_entry(payload["entry"])}
    elif operation == "publication":
        _keys(payload, {"entry", "accounting"})
        ledger = payload["accounting"]
        if checksum(ledger["snapshot"]) != _sha(ledger["sha256"]):
            raise NativeContractError("Authoritative closed native ledger checksum differs")
        result = {"entry": _native_entry(payload["entry"]),
            "accounting": {key: deepcopy(ledger[key]) for key in ("snapshot", "sha256")}}
    elif operation == "read_artifact":
        _keys(payload, {"entry", "spec"})
        _keys(payload["spec"], {"name", "sha256", "size_bytes", "media_type", "kind"})
        result = {"entry": _native_entry(payload["entry"]),
                  "spec": deepcopy(payload["spec"])}
    else:
        raise NativeContractError("Unsupported fixed native operation")
    _no_coordinator_secrets(result)
    return result


class AccuracyNativeExecutor:
    """Fixed HPC metadata RPCs; the worker alone submits and signs requests."""

    def __init__(self, slurm, contexts):
        self.slurm, self.contexts = slurm, contexts
        self.transport = slurm.transport

    def _rpc(self, context_id, operation, **payload):
        context = self.contexts.context(context_id)
        operator = self.contexts.operator(context_id)
        request = {"operation": operation, "context": context,
                   "operator": operator, **_native_payload(operation, payload)}
        _no_coordinator_secrets(request)
        raw = canonical(request)
        if len(raw) > MAX_CONTROL:
            raise NativeContractError("Native request exceeds its metadata bound")
        result = self.transport.run_bytes(
            [getattr(self.transport, "staging_python", "/usr/bin/python3.11"),
             "-I", "-S", "-B", "-c", _BOOTSTRAP], data=raw)
        if operation == "read_artifact":
            return result
        if len(result) > MAX_METADATA:
            raise NativeContractError("Native response exceeds its metadata bound")
        value = _json(result)
        if isinstance(value, dict) and value.get("schema") == "aws_mesouq.benchmark_accuracy_native_error.v1":
            _keys(value, {"schema", "code", "preflight"})
            proof = value["preflight"]
            expected = payload.get("entry", {}).get("manifest_sha256")
            if expected is None and "claim" in payload:
                expected = payload["claim"]["task"]["manifest_sha256"]
            _keys(proof, {"schema", "context_id", "manifest_sha256", "checked_epoch", "home_quota"})
            if (value["code"] != "home_quota_unhealthy"
                    or proof["schema"] != "aws_mesouq.benchmark_accuracy_preflight.v1"
                    or proof["context_id"] != context_id
                    or proof["manifest_sha256"] != expected
                    or len(canonical(proof)) > 65_536):
                raise NativeContractError("Unbound native quota denial")
            error = NativeContractError("Assigned HOME quota blocks new native work")
            error.preflight = deepcopy(proof)
            raise error
        return value

    def admitted_contexts(self):
        admitted = []
        for context_id in self.contexts.supported():
            if self._rpc(context_id, "verify_profile") == {"verified": True}:
                admitted.append(context_id)
        return admitted

    def prepare(self, claim, window):
        claim = self.contexts.validate_claim(claim)
        return self._rpc(claim["task"]["manifest"]["context_id"],
                         "prepare", claim=claim, window=window)

    def preflight(self, entry):
        self.contexts.validate_claim(entry["claim"])
        return self._rpc(entry["context_id"], "preflight", entry=entry)

    def observe(self, entry):
        return self._rpc(entry["context_id"], "observe", entry=entry)

    def cancel(self, entry, *, authorize):
        self.contexts.validate_claim(entry["claim"])
        if not callable(authorize):
            raise NativeContractError("Live cancellation authorization required")
        def checked_authorization():
            if authorize() is not True:
                raise NativeContractError("Cancellation is no longer authoritative")
            return True
        return self.slurm.cancel(entry["job_id"],
            entry["claim"]["task"]["marker"], authorize=checked_authorization)

    def publication(self, entry, authoritative_closed_ledger):
        return self._rpc(entry["context_id"], "publication", entry=entry,
                         accounting=authoritative_closed_ledger)

    def read_artifact(self, entry, spec):
        raw = self._rpc(entry["context_id"], "read_artifact",
                        entry=entry, spec=spec)
        if (len(raw) != spec["size_bytes"]
                or hashlib.sha256(raw).hexdigest() != spec["sha256"]):
            raise NativeContractError("Native curated artifact differs from manifest")
        return raw


def _entry_control(context, entry):
    manifest = entry["claim"]["task"]["manifest"]
    if entry["context_id"] != context["id"]:
        raise NativeContractError("Native entry context changed")
    _, directory = _paths(context, manifest)
    control = _read_json(directory / "control.json", maximum=MAX_CONTROL)
    old_context = {k: v for k, v in control["context"].items() if k != "enabled"}
    current_context = {k: v for k, v in context.items() if k != "enabled"}
    if (control["schema"] != CONTROL_SCHEMA or old_context != current_context
            or control["manifest"] != manifest
            or control["manifest_sha256"] != entry["manifest_sha256"]
            or control["marker"] != entry["claim"]["task"]["marker"]
            or control["control_directory"] != str(directory)):
        raise NativeContractError("Native immutable control identity changed")
    return _validate_control_identity(control)


def _job(value):
    if not isinstance(value, str) or re.fullmatch(r"[1-9][0-9]{0,19}", value) is None:
        raise NativeContractError("Actual native job ID required")
    return value


def _decimal(value, name):
    if not isinstance(value, str) or re.fullmatch(r"[0-9]+", value) is None:
        raise NativeContractError("Exact native integer required: " + name)
    return int(value)


def _parse_sacct(raw, *, job_id, marker, user, cpus, cancelling):
    """Parse only the exact root allocation; unknown costs remain unknown."""
    if not isinstance(raw, str) or len(raw) > MAX_METADATA:
        raise NativeContractError("Bounded SACCT output required")
    records = []
    for line in raw.splitlines():
        if not line.strip():
            continue
        fields = line.strip().split("|")
        if len(fields) == 11 and fields[-1] == "":
            fields.pop()
        if len(fields) != 10:
            raise NativeContractError("Native SACCT column count changed")
        if fields[0] != job_id:
            if fields[0].startswith(job_id + "."):
                continue
            raise NativeContractError("Foreign SACCT allocation returned")
        if fields[1] != marker or fields[2] != user:
            raise NativeContractError("SACCT marker or user differs")
        records.append(fields)
    if not records:
        return None
    if len(records) != 1:
        raise NativeContractError("Ambiguous root SACCT allocation")
    f = records[0]
    state = f[3].split()[0].rstrip("+")
    if state not in _TERMINAL:
        if state in {"PENDING", "CONFIGURING", "REQUEUED", "RESIZING"}:
            return {"job_id": job_id, "state": "queued", "accounting": None}
        if state in {"RUNNING", "COMPLETING", "SUSPENDED", "STAGE_OUT"}:
            return {"job_id": job_id, "state": "running", "accounting": None}
        raise NativeContractError("Unsupported native allocation state")
    elapsed = _decimal(f[4], "elapsed")
    cpu_seconds = _decimal(f[6], "CPU time")
    allocated_cpus = _decimal(f[8], "allocated CPUs")
    tres = {}
    if f[5]:
        for item in f[5].split(","):
            key, separator, amount = item.partition("=")
            if not separator or key in tres:
                raise NativeContractError("Malformed or duplicate allocated TRES")
            tres[key] = amount
    # GPU model-specific TRES are alternative dimensions of the same GPU.
    generic = tres.get("gres/gpu")
    models = [_decimal(v, "GPU TRES") for k, v in tres.items()
              if k.startswith("gres/gpu:")]
    gpus = _decimal(generic, "GPU TRES") if generic is not None else sum(models)
    if models and generic is not None and sum(models) != gpus:
        raise NativeContractError("Conflicting native GPU dimensions")
    account = {"source": "sacct", "site": "karolina", "slurm_job_id": job_id,
        "state": state, "elapsed_seconds": elapsed,
        "allocated_gpus": gpus, "cpu_seconds": cpu_seconds}
    if gpus == 0:
        if not (cancelling and state == "CANCELLED" and elapsed == 0
                and cpu_seconds == 0 and allocated_cpus == 0
                and f[5] == "" and f[7] in ("Unknown", "None", "")):
            raise NativeContractError("Unknown or nonzero allocation cannot become zero")
        account["preallocation_cancellation"] = {
            "start": None, "allocated_cpus": 0, "allocated_tres": ""}
    elif gpus != 1 or allocated_cpus != cpus or cpu_seconds != elapsed * cpus:
        raise NativeContractError("Terminal resource accounting differs from context")
    if state == "REVOKED":
        raise NativeContractError("Unadmitted terminal native state")
    return {"job_id": job_id, "state": "terminal", "accounting": account,
            "mechanical_failure": state != "COMPLETED" or f[9] != "0:0"}


def _observe(payload):
    context, entry = payload["context"], payload["entry"]
    job = _job(entry["job_id"])
    control = _entry_control(context, entry)
    result = subprocess.run(["sacct", "--noheader", "--parsable2",
        "--user", context["native_user"], "--jobs", job,
        "--format=JobIDRaw,JobName%128,User%128,State,ElapsedRaw,AllocTRES%512,CPUTimeRAW,Start,AllocCPUS,ExitCode"],
        capture_output=True, text=True, timeout=30, check=True)
    task = entry["claim"]["task"]
    cancelling = (entry["claim"]["campaign"]["status"] == "cancelling"
                  or task["status"] == "cancelling")
    observation = _parse_sacct(result.stdout, job_id=job,
        marker=control["marker"], user=context["native_user"],
        cpus=context["native_cpus"], cancelling=cancelling)
    if observation is not None and observation["state"] == "running":
        try:
            clock = _read_json(Path(control["control_directory"]) / "allocation-clock.json")
        except FileNotFoundError:
            return None
        if clock["slurm_job_id"] != job:
            raise NativeContractError("Running clock belongs to another allocation")
        observation.update(allocation_start_epoch=clock["allocation_start_epoch"],
            allocation_deadline_epoch=clock["allocation_deadline_epoch"])
    return observation


def remote_request(payload):
    _keys(payload, {"operation", "context", "operator"},
          {"claim", "window", "entry", "accounting", "spec"})
    operation = payload["operation"]
    if operation not in {"verify_profile", "preflight", "prepare", "observe", "publication", "read_artifact"}:
        raise NativeContractError("Unsupported fixed native operation")
    if operation == "prepare":
        return _prepare(payload)
    guard = _verify_profile(payload["context"], payload["operator"])
    try:
        if operation == "verify_profile":
            _home_preflight(payload["context"])
            return {"verified": True}
        if operation == "preflight":
            return _prepared_preflight(payload)
        if operation == "observe":
            return _observe(payload)
        if operation == "publication":
            return _publication(payload, guard)
        return _read_artifact(payload)
    finally:
        guard.check()


def _inside_campaign(control, path):
    path = _safe_absolute(str(path))
    root = Path(control["campaign_directory"])
    if root not in path.parents:
        raise NativeContractError("Receipt escaped this native campaign")
    return path


def _descriptor(control, directory, filename):
    directory = _inside_campaign(control, directory)
    with stable_bytes(directory / filename, private=True) as (raw, _):
        return {"directory": str(directory), "sha256": hashlib.sha256(raw).hexdigest()}


def _pinned(control, descriptor, filename, *, maximum=MAX_FILE):
    _keys(descriptor, {"directory", "sha256"})
    directory = _inside_campaign(control, descriptor["directory"])
    _sha(descriptor["sha256"])
    with stable_bytes(directory / filename, private=True, maximum=maximum) as (raw, _):
        if hashlib.sha256(raw).hexdigest() != descriptor["sha256"]:
            raise NativeContractError("Pinned native receipt bytes changed")
        return _json(raw)


def _binding(control, job_id):
    m = control["manifest"]
    return {**{k: m[k] for k in ("campaign_instance_id", "task_id", "attempt",
        "kind", "context_id", "source_binding_sha256", "scientific_request_hash",
        "transport_request_hash", "input_ledger_sha256")},
        "manifest_sha256": control["manifest_sha256"], "slurm_job_id": job_id}


def _unwrap(control, descriptor, role):
    wrapper = _pinned(control, descriptor, "native-receipt.json",
                      maximum=MAX_CONTROL)
    _keys(wrapper, {"schema", "binding", "source_receipts", "outputs",
        "output_pins", "proposal", "scientific_status", "qualification",
        "checks", "artifacts"})
    if wrapper["schema"] != WRAPPER_SCHEMA:
        raise NativeContractError("Native wrapper schema differs")
    binding, manifest = wrapper["binding"], control["manifest"]
    for key in ("campaign_instance_id", "context_id", "source_binding_sha256",
                "scientific_request_hash", "transport_request_hash"):
        if binding.get(key) != manifest[key]:
            raise NativeContractError("Native wrapper identity/source changed")
    _job(binding.get("slurm_job_id"))
    receipts = wrapper["source_receipts"]
    if not isinstance(receipts, dict) or role not in receipts:
        raise NativeContractError("Native wrapper lacks required source role")
    entry = receipts[role]
    _keys(entry, {"directory", "sha256", "file"})
    filenames = {"factor_action": "factor-action.json", "stage": "receipt.json",
        "guide_evidence": "guide-evidence.json", "planner": "source-plan.json",
        "comparison": "comparison.json"}
    if role not in filenames or entry["file"] != filenames[role]:
        raise NativeContractError("Native wrapper source filename differs")
    pin = {k: entry[k] for k in ("directory", "sha256")}
    raw = _pinned(control, pin, entry["file"])
    if role == "stage" and raw.get("slurm_job_id") != binding["slurm_job_id"]:
        raise NativeContractError("Source stage producer differs from wrapper")
    return pin, raw


def _source_entry(control, descriptor, *, wrapped=True):
    if wrapped:
        pin, raw = _unwrap(control, descriptor, "stage")
    else:
        pin, raw = descriptor, _pinned(control, descriptor, "receipt.json")
    entry = {"directory": pin["directory"], "receipt_sha256": pin["sha256"]}
    if raw.get("not_run_reason") is not None:
        entry["not_run_reason"] = raw["not_run_reason"]
    return entry, raw


def _guide(control, custody, runner):
    m = control["manifest"]
    pin = m["inputs"]["phase_receipts"]["guide_preparation"]
    receipt = _pinned(control, pin, "guide-receipt.json")
    plan = receipt["case_plan"]
    custody.validate_case_plan(plan, m["request"], m["campaign_instance_id"],
                               m["inputs"]["confirmation_commitment"])
    runner._guide_preparation(m["request"], plan,
        {"directory": pin["directory"], "receipt_sha256": pin["sha256"]})
    if (plan["source_binding_sha256"] != m["source_binding_sha256"]
            or receipt["case_plan_sha256"] != m["pins"]["case_plan_sha256"]
            or plan["artifacts"]["guide"]["sha256"] != m["pins"]["guide_case_set_sha256"]
            or plan["artifacts"]["normalization"]["sha256"] != m["pins"]["normalization_sha256"]
            or plan["artifacts"]["validation"]["sha256"] != m["pins"]["validation_sha256"]):
        raise NativeContractError("Guide plan or artifact pins differ")
    return Path(pin["directory"]), plan, pin


def _history(control, runner, plan, method):
    pins = control["manifest"]["inputs"]["guide_history"][method]
    result = []
    for pin in pins:
        raw = _pinned(control, pin, "guide-evidence.json")
        descriptor = {"file": "guide-evidence.json", "sha256": pin["sha256"],
            "size_bytes": (Path(pin["directory"]) / "guide-evidence.json").stat().st_size}
        result.append({**raw, "evidence_artifact": {
            "directory": pin["directory"], "descriptor": descriptor}})
    validated, _ = runner._guide_history(control["manifest"]["request"], plan,
                                         method, result)
    return validated


def _factor_actions(control, runner, plan, guide_root, costs):
    from aws_mesouq.benchmark_accuracy_protocol import FACTORS
    result = {}
    progress = control["manifest"]["inputs"]["progress"]
    for factor in FACTORS:
        row = progress.get(factor)
        if row is None:
            result[factor] = None
            continue
        pin, action = _unwrap(control, row["native_receipt"], "factor_action")
        line = Path(control["campaign_directory"]) / "lineages" / factor
        receipt = action["factor_receipt"]
        if (receipt["factor"] != factor or receipt["state"]["sha256"] != row["state_sha256"]
                or receipt["completed_updates"] != row["optimizer_updates"]):
            raise NativeContractError("Cloud progress differs from pinned native factor action")
        result[factor] = {"lineage_dir": str(line), "factor_action": action}
    projected = runner._current_factor_actions(control["manifest"]["request"],
        plan, guide_root, result, costs)
    return result, projected


def _tmcmc_actions(control, runner, plan, bank, costs):
    entries, projected, candidate = [], [], None
    for pin in control["manifest"]["inputs"]["tmcmc_attempts"]:
        entry, raw = _source_entry(control, pin)
        _, receipt = runner._read_stage(control["manifest"]["request"],
                                       plan, bank, entry, "tmcmc")
        runner._match_stage_job(costs, receipt["slurm_job_id"],
                               "tmcmc", "guide_tmcmc_attempt")
        status = ("completed" if receipt["candidate_sha256"] is not None else
                  "budget_exhausted" if any(row["status"] == "budget_exhausted"
                      for row in receipt["cases"]) else "failed")
        entries.append(entry)
        projected.append({"population_size": receipt["population_size"],
            "attempt_id": receipt["attempt_id"], "status": status,
            "candidate_sha256": receipt["candidate_sha256"]})
        if receipt["candidate_sha256"] is not None:
            candidate = entry
    return entries, projected, candidate


def _planner(control, runner, custody, plan, guide_root, costs, clock):
    m, method = control["manifest"], control["manifest"]["method"]
    history = _history(control, runner, plan, method)
    progress = {"sealed": False, "confirmation_opened": False,
                "guide_history": history}
    if method == "hnpe":
        actions, projected = _factor_actions(control, runner, plan, guide_root, costs)
        positive = all(row is not None and row["selected_update"] is not None
                       for row in projected.values())
        bundle = (runner.make_hnpe_bundle(m["request"], plan,
            normalization_path=guide_root / "normalization.npz", factors=actions)
            if positive else None)
        progress.update(factors=projected,
            candidate_sha256=None if bundle is None else bundle["sha256"])
        extra = {"factor_actions": actions}
        candidate = bundle
    else:
        _, bank, _ = runner._load_bank(m["request"], plan,
            guide_root / "guide-cases.npz", "guide",
            m["inputs"]["phase_receipts"]["guide_preparation"]["sha256"])
        entries, projected, candidate = _tmcmc_actions(control, runner, plan, bank, costs)
        progress["attempts"] = projected
        extra = {"attempts": entries}
    decision = runner.plan_next_effort(m["request"], plan, method,
                                       progress=progress, accounting=costs)
    target = decision.get("effort_target", decision.get("population_size"))
    if decision["action"] == "record_failed_guide":
        # This branch retains a failed native population in the source guide
        # denominator; no success-only filtering or resampling is permitted.
        stage_entry = extra["attempts"][-1]
        reference, _ = _source_entry(control,
            m["inputs"]["phase_receipts"]["guide_reference"])
        evidence = runner.compile_guide(m["request"], plan,
            guide_root / "guide-cases.npz", stage_entry, reference,
            Path(control["control_directory"]) / "failed-guide",
            method=method, effort_target=target, clock=clock,
            bank_receipt_sha256=m["inputs"]["phase_receipts"]["guide_preparation"]["sha256"])
        history.append(evidence)
    eligible = []
    for row in decision.get("eligible_factors", []):
        factor = row["factor"]
        previous = m["inputs"]["progress"].get(factor)
        eligible.append({"factor": factor, "operation": row["action"],
            "to_update": None if row["action"] == "initialize" else row["requested_to_update"],
            "expected_state_sha256": row["expected_parent_state_sha256"],
            "generation": 0 if previous is None else previous["generation"]})
    maximum = (decision["eligible_factors"][0]["maximum_reserved_gpu_seconds"]
               if eligible else decision.get("maximum_reserved_gpu_seconds", 0))
    proposal = {"method": method, "action": decision["action"],
        "effort_target": target, "candidate_sha256": decision.get("candidate_sha256"),
        "eligible_factors": eligible, "maximum_reserved_gpu_seconds": maximum,
        "status": decision.get("status"), "reason": decision.get("reason")}
    outputs = {"guide_history": [_descriptor(control,
        row["evidence_artifact"]["directory"], "guide-evidence.json") for row in history]}
    if decision["action"] == "freeze":
        wrapper = {"status": decision["status"], "candidate": candidate,
                   "guide_history": history, **extra}
        directory = Path(control["control_directory"])
        immutable_json(directory / "method-state.json", wrapper)
        outputs["method_states"] = {method: _descriptor(control, directory, "method-state.json")}
    directory = Path(control["control_directory"])
    immutable_json(directory / "source-plan.json", decision)
    return proposal, outputs, {"planner": {**_descriptor(control, directory,
        "source-plan.json"), "file": "source-plan.json"}}


def _source_receipt(control, directory, filename, role):
    return {role: {**_descriptor(control, directory, filename), "file": filename}}


def _sealed(control, custody, runner, plan):
    pin = control["manifest"]["inputs"]["method_seal"]
    raw = _pinned(control, pin, "method-seal.json")
    verified = runner.validate_sealing_receipt(control["manifest"]["request"], plan, pin)
    authority = custody.sealed_evidence(control["manifest"]["request"], plan)
    if (authority["sealing_receipt"] != pin
            or verified["seal_sha256"] != control["manifest"]["pins"]["method_seal_sha256"]
            or verified["accounting_anchor"] != control["manifest"]["inputs"]["sealed_allocation_anchor"]
            or raw["method_states"] != verified["method_states"]):
        raise NativeContractError("Global sealing authority or full source states differ")
    return raw, verified, authority


def _bank(control, custody, runner, plan):
    seal, verified, authority = _sealed(control, custody, runner, plan)
    pin = control["manifest"]["inputs"]["phase_receipts"]["confirmation_bank"]
    raw = _pinned(control, pin, "confirmation-receipt.json")
    if (authority["confirmation_receipt_sha256"] != pin["sha256"]
            or authority["confirmation_output"] != pin["directory"]
            or raw["opening_id"] != control["manifest"]["pins"]["opening_id"]
            or raw["method_states"] != verified["method_states"]):
        raise NativeContractError("Confirmation bank differs from first opening authority")
    path = Path(pin["directory"]) / raw["case_bank"]["file"]
    return seal, raw, path, pin


def _native_publication(control, job, *, outputs, pins, receipts, checks,
                        proposal=None, scientific_status=None, qualification=None,
                        artifacts=None, artifact_roles=None, files=None):
    """Bind the actual source receipt chain to one immutable native producer."""
    from aws_mesouq import benchmark_accuracy_protocol as protocol
    m = control["manifest"]
    checks = sorted(set(checks))
    if not protocol.required_validation_checks(m["kind"]) <= set(checks):
        raise NativeContractError("Native source validation is incomplete")
    artifacts = [] if artifacts is None else artifacts
    wrapper = {"schema": WRAPPER_SCHEMA, "binding": _binding(control, job),
        "source_receipts": receipts, "outputs": deepcopy(outputs),
        "output_pins": deepcopy(pins), "proposal": deepcopy(proposal),
        "scientific_status": scientific_status, "qualification": qualification,
        "checks": checks, "artifacts": deepcopy(artifacts)}
    if m["kind"] in {"hnpe_initialize", "hnpe_advance"}:
        wrapper["outputs"]["progress"].pop("native_receipt")
    directory = Path(control["control_directory"])
    immutable_json(directory / "native-receipt.json", wrapper)
    native = _descriptor(control, directory, "native-receipt.json")
    if m["kind"] in {"hnpe_initialize", "hnpe_advance"}:
        outputs = deepcopy(outputs)
        outputs["progress"]["native_receipt"] = deepcopy(native)
    result = {"schema": protocol.RESULT_SCHEMA, **_binding(control, job),
        "native_receipt": native, "input_pins": deepcopy(m["pins"]),
        "output_pins": deepcopy(pins), "outputs": deepcopy(outputs),
        "source_validation": {
            "schema": "aws_mesouq.benchmark_accuracy_native_validation.v1",
            "verified": True, "producer_job_id": job, "receipt_sha256": native["sha256"],
            "manifest_sha256": control["manifest_sha256"],
            "verified_inputs_sha256": protocol.checksum(m["inputs"]),
            "artifact_manifest_sha256": protocol.checksum(sorted(artifacts, key=lambda x: x["name"])),
            "checks": checks},
        "proposal": proposal, "scientific_status": scientific_status,
        "qualification": qualification, "artifacts": []}
    # Server artifact IDs are intentionally absent until private object upload.
    # Validate the no-artifact metadata template now; final references are
    # resolved and validated again by the worker and server.
    if not artifacts:
        protocol.validate_result(result, m,
            native_root=control["context"]["native_root"])
    publication = {"schema": PUBLICATION_SCHEMA, "binding": _binding(control, job),
        "result": result, "artifacts": deepcopy(artifacts),
        "artifact_roles": {} if artifact_roles is None else artifact_roles,
        "files": {} if files is None else files}
    if len(canonical(publication)) > MAX_CONTROL:
        raise NativeContractError("Native publication metadata exceeds its bound")
    immutable_json(directory / "native-publication.json", publication)
    return publication


def _load_control(path):
    control = _read_json(_safe_absolute(path), maximum=MAX_CONTROL)
    _keys(control, {"schema", "context", "operator", "manifest", "manifest_sha256",
        "marker", "window", "control_directory", "campaign_directory", "runtime_environment"})
    if control["schema"] != CONTROL_SCHEMA:
        raise NativeContractError("Native control schema differs")
    if str(Path(path).parent) != control["control_directory"]:
        raise NativeContractError("Native control relocated")
    return _validate_control_identity(control)


def _allocated(control, *, execute=False, factor_cli=False):
    guard = _verify_profile(control["context"], control["operator"], allocated=True)
    guard.install()
    from aws_mesouq import benchmark_accuracy_protocol as protocol
    from aws_mesouq.adapters.benchmark import allocation_clock, run_science
    m, window = protocol.validate_manifest(control["manifest"]), control["window"]
    if protocol.checksum(m) != control["manifest_sha256"]:
        raise NativeContractError("Allocated native manifest checksum changed")
    _, directory = _paths(control["context"], m)
    _require_scratch(directory)
    if str(directory) != control["control_directory"]:
        raise NativeContractError("Allocated native path differs")
    quota = _home_preflight(control["context"],
                            manifest_sha256=control["manifest_sha256"])
    clock = allocation_clock({"runtime_clock_policy": "allocation_deadline_v1",
        "allocated_seconds": window["allocated_seconds"],
        "internal_seconds": window["internal_seconds"]})
    immutable_json(directory / "allocation-clock.json", clock)
    immutable_json(directory / ("quota-check-" + uuid.uuid4().hex + ".json"),
        {**_preflight_snapshot(control["context"], control["manifest_sha256"], quota),
         "slurm_job_id": clock["slurm_job_id"],
         "boundary": "allocated_stage" if execute else "allocated_supervisor"})
    try:
        if execute:
            return _dispatch(control, clock)
        command = [control["operator"]["python_bin"], "-I", "-S", "-B", "-c",
            _NATIVE_BOOTSTRAP, control["operator"]["adapter_path"],
            control["context"]["adapter_sha256"], str(directory / "control.json"),
            checksum(control), "--execute-stage"]
        return run_science(command, str(directory),
                           clock["watchdog_deadline_epoch"])
    finally:
        guard.check()


def _training(control, clock, plan, guide_root, runner, custody):
    """Use precisely the maintained initialize-factor/advance-factor CLI."""
    from meso_uq.benchmark_lab.accuracy_v2 import cli, training
    m = control["manifest"]
    factor = m["factor"]
    lineage = Path(control["campaign_directory"]) / "lineages" / factor
    directory = Path(control["control_directory"])
    action_path = directory / "factor-action.json"
    _mkdir(lineage.parent)
    operation = "initialize-factor" if m["kind"] == "hnpe_initialize" else "advance-factor"
    args = [operation, "--request", str(directory / "request.json"),
        "--campaign-instance-id", m["campaign_instance_id"], "--factor", factor,
        "--lineage-dir", str(lineage), "--case-plan", str(guide_root / "case-plan.json"),
        "--case-plan-sha256", m["pins"]["case_plan_sha256"],
        "--normalization", str(guide_root / "normalization.npz"),
        "--normalization-sha256", m["pins"]["normalization_sha256"],
        "--validation", str(guide_root / "validation.npz"),
        "--validation-sha256", m["pins"]["validation_sha256"],
        "--reserved-seconds", str(control["window"]["internal_seconds"]),
        "--allocation-job-id", clock["slurm_job_id"],
        "--allocation-start-epoch", str(clock["allocation_start_epoch"]),
        "--allocation-deadline-epoch", str(clock["allocation_deadline_epoch"]),
        "--receipt-output", str(action_path)]
    if operation == "advance-factor":
        args.extend(["--expected-state-sha256", m["expected_state_sha256"],
                     "--to-update", str(m["effort_target"])])
    status = cli.main(args)
    if status not in (None, 0):
        raise NativeContractError("Maintained scientific CLI failed")
    action = _read_json(action_path, maximum=MAX_FILE)
    receipt = action["factor_receipt"]
    normalization = custody._authenticate_arrays(guide_root,
                                                 plan["artifacts"]["normalization"])
    context = training.context_for_factor(m["request"], m["campaign_instance_id"],
        factor, plan, normalization, m["pins"]["normalization_sha256"],
        m["pins"]["validation_sha256"])
    verified = training.validate_factor_receipt(lineage, receipt, context)
    document = verified["state"]
    core = document["core"]
    if (receipt["producer_allocation"]["slurm_job_id"] != clock["slurm_job_id"]
            or core["generation"] != (0 if operation == "initialize-factor" else m["generation"] + 1)
            or (operation == "advance-factor"
                and core["predecessor_state_sha256"] != m["expected_state_sha256"])):
        raise NativeContractError("Native factor producer/generation/predecessor differs")
    selected = receipt["selected_checkpoint"]
    history = receipt["history"]
    last = history[-1] if history else None
    nll = (last.get("validation_nll", last.get("nll"))
           if isinstance(last, dict) else None)
    progress = {"factor": factor, "state_sha256": receipt["state"]["sha256"],
        "generation": core["generation"], "optimizer_updates": receipt["completed_updates"],
        "candidate_sha256": None if selected is None else selected["sha256"],
        "native_receipt": None, "best_validation_nll": receipt["best_validation_nll"],
        "validation_nll": nll,
        "stop_reason": receipt["terminal_reason"] or receipt["slice_reason"],
        "native_generation": core["generation"]}
    return progress, _source_receipt(control, directory, "factor-action.json",
                                      "factor_action")


def _dispatch(control, clock):
    """Run one fixed native stage inside the already-funded allocation."""
    from aws_mesouq import benchmark_accuracy_protocol as protocol
    from meso_uq.benchmark_lab import provenance
    from meso_uq.benchmark_lab.accuracy_v2 import custody, runner, policy
    m, directory = control["manifest"], Path(control["control_directory"])
    job, kind = clock["slurm_job_id"], m["kind"]
    if provenance.source_binding()["sha256"] != m["source_binding_sha256"]:
        raise NativeContractError("Allocated scientific source binding changed")
    source_clock = provenance.StageClock(control["window"]["internal_seconds"],
        allocation_job_id=job, allocation_start_epoch=clock["allocation_start_epoch"],
        allocation_deadline_epoch=clock["allocation_deadline_epoch"])
    snapshot = protocol.bind_current_allocation(m, job)
    pins, outputs, receipts = deepcopy(m["pins"]), {}, {}
    proposal, scientific_status = None, None
    checks = {"source", "producer", "inputs", "curated_publication"}
    if kind == "guide_preparation":
        if not hasattr(runner, "plan_guide_preparation"):
            raise NativeContractError("Source guide funding admission is not frozen")
        secret_path = Path(control["campaign_directory"]) / "private" / "confirmation-nonce.json"
        try:
            secret = _read_json(secret_path)
            _keys(secret, {"nonce"})
            nonce = secret["nonce"]
        except FileNotFoundError:
            import secrets
            nonce = secrets.token_urlsafe(48)
        commitment = custody.nonce_commitment(nonce)
        specification = custody.make_case_plan(m["request"],
            m["campaign_instance_id"], commitment)
        admission = runner.plan_guide_preparation(m["request"], specification,
                                                  accounting=snapshot)
        common = {"schema", "binding", "preparation_spec_sha256",
            "accounting_snapshot_sha256", "native_execution_contract",
            "native_quantum_seconds", "method_allocated_gpu_seconds",
            "method_reserved_gpu_seconds", "method_remaining_gpu_seconds",
            "action", "new_reservation", "allocation", "maximum_reserved_gpu_seconds", "funding"}
        _keys(admission, common)
        funding = admission["funding"]
        _keys(funding, {"schema", "native_execution_contract", "gpu_count",
            "native_quantum_seconds", "reservation_gpu_seconds", "funded_gpu_seconds",
            "unallocated_remainder_gpu_seconds"})
        reserved = m["reservation"]["gpu_seconds"]
        funded = reserved // 60 * 60
        if (funding["schema"] != "mesouq.benchmark_lab.accuracy_v2.native_funding.v1"
                or funding["native_execution_contract"] != "allocation_deadline_v1"
                or type(funding["gpu_count"]) is not int or funding["gpu_count"] != 1
                or type(funding["native_quantum_seconds"]) is not int
                or funding["native_quantum_seconds"] != 60
                or type(funding["reservation_gpu_seconds"]) not in (int, float)
                or funding["reservation_gpu_seconds"] != reserved
                or type(funding["funded_gpu_seconds"]) is not int
                or funding["funded_gpu_seconds"] != funded
                or type(funding["unallocated_remainder_gpu_seconds"]) not in (int, float)
                or funding["unallocated_remainder_gpu_seconds"] != reserved - funded
                or admission["maximum_reserved_gpu_seconds"] != funded):
            raise NativeContractError("Source native funding descriptor differs from original reservation")
        expected_allocation = {"allocation_id": m["reservation"]["allocation_id"],
            "attempt": m["attempt"], "slurm_job_id": job, "status": "running"}
        expected_binding = {key: m[key] for key in ("campaign_instance_id",
            "scientific_request_hash", "transport_request_hash", "source_binding_sha256")}
        if (admission["schema"] != "mesouq.benchmark_lab.accuracy_v2.preparation_admission.v1"
                or admission["binding"] != expected_binding
                or admission["preparation_spec_sha256"] != provenance.fingerprint(specification)
                or admission["accounting_snapshot_sha256"] != protocol.checksum(snapshot)
                or admission["native_execution_contract"] != "allocation_deadline_v1"
                or type(admission["native_quantum_seconds"]) is not int
                or admission["native_quantum_seconds"] != 60
                or admission["action"] != "continue_reserved_preparation"
                or admission["new_reservation"] is not False
                or admission["allocation"] != expected_allocation
                or type(admission["maximum_reserved_gpu_seconds"]) is not int
                or admission["maximum_reserved_gpu_seconds"] < control["window"]["allocated_seconds"]):
            raise NativeContractError("Source preparation admission differs from current reservation")
        # Persist the secret only after source admission. The cloud receives its
        # commitment only; the secret remains in the private native campaign.
        immutable_json(secret_path, {"nonce": nonce})
        immutable_json(directory / "preparation-admission.json", admission)
        guide_root = directory / "source-guide"
        receipt = custody.prepare_guide_bank(m["request"], m["campaign_instance_id"],
                                              commitment, guide_root)
        plan = receipt["case_plan"]
        guide_pin = _descriptor(control, guide_root, "guide-receipt.json")
        runner._guide_preparation(m["request"], plan, {
            "directory": str(guide_root), "receipt_sha256": guide_pin["sha256"]})
        for role in ("guide", "normalization", "validation"):
            custody._authenticate_arrays(guide_root, plan["artifacts"][role])
        pins.update(case_plan_sha256=receipt["case_plan_sha256"],
            guide_case_set_sha256=receipt["guide_case_set_sha256"],
            normalization_sha256=plan["artifacts"]["normalization"]["sha256"],
            validation_sha256=plan["artifacts"]["validation"]["sha256"])
        outputs = {"guide_preparation": guide_pin, "confirmation_commitment": commitment}
        receipts = {"guide_preparation": {**guide_pin, "file": "guide-receipt.json"},
            "preparation_admission": {**_descriptor(control, directory,
                "preparation-admission.json"), "file": "preparation-admission.json"}}
        checks.update(protocol.required_validation_checks(kind))
        _native_publication(control, job, outputs=outputs, pins=pins,
                            receipts=receipts, checks=checks)
        return 0
    guide_root, plan, guide_pin = _guide(control, custody, runner)
    preparation = {"directory": str(guide_root), "receipt_sha256": guide_pin["sha256"]}
    costs = runner.validate_allocation_snapshot(m["request"], plan, snapshot)
    if kind in {"hnpe_initialize", "hnpe_advance"}:
        progress, receipts = _training(control, clock, plan, guide_root, runner, custody)
        checks.update({"campaign_authority", "native_state", "optimizer_predecessor",
                       "case_plan", "normalization", "validation"})
        # The wrapper's own SHA is not embedded in itself: the result-level
        # factor pointer is filled only after the immutable wrapper is written.
        outputs["progress"] = {**progress, "native_receipt": {
            "directory": str(directory), "sha256": "0" * 64}}
    elif kind == "plan":
        proposal, outputs, receipts = _planner(control, runner, custody, plan,
                                               guide_root, costs, source_clock)
        pins["candidate_sha256"] = proposal["candidate_sha256"]
        checks.update({"planner", "accounting", "native_state", "guide_history"})
    elif kind == "guide_reference":
        stage = runner.run_reference_cases(m["request"], plan,
            guide_root / "guide-cases.npz", directory / "source-stage",
            role="guide", clock=source_clock, device="cpu",
            bank_receipt_sha256=guide_pin["sha256"])
        receipts = _source_receipt(control, str(directory / "source-stage"), "receipt.json", "stage")
        outputs["guide_reference"] = _descriptor(control, str(directory / "source-stage"), "receipt.json")
        checks.update({"reference", "case_plan", "bank"})
    elif kind in {"guide_hnpe_inference", "guide_tmcmc_attempt"}:
        if kind == "guide_hnpe_inference":
            actions, projected = _factor_actions(control, runner, plan, guide_root, costs)
            bundle = runner.make_hnpe_bundle(m["request"], plan,
                normalization_path=guide_root / "normalization.npz", factors=actions)
            if bundle["sha256"] != m["pins"]["candidate_sha256"]:
                raise NativeContractError("Native HNPE bundle differs from source proposal")
            stage = runner.evaluate_hnpe_bundle(m["request"], plan,
                guide_root / "guide-cases.npz", bundle, directory / "source-stage",
                role="guide", clock=source_clock, device="cuda",
                bank_receipt_sha256=guide_pin["sha256"])
            checks.update({"candidate", "native_state"})
        else:
            stage = runner.run_tmcmc_attempt(m["request"], plan,
                guide_root / "guide-cases.npz", directory / "source-stage",
                role="guide", population_size=m["effort_target"],
                attempt_id=m["task_id"], clock=source_clock, device="cuda",
                bank_receipt_sha256=guide_pin["sha256"])
            pins["candidate_sha256"] = stage.get("candidate_sha256")
            checks.add("fresh_attempt")
        raw = _read_json(Path(str(directory / "source-stage")) / "receipt.json", maximum=MAX_FILE)
        if kind == "guide_tmcmc_attempt":
            pins["candidate_sha256"] = raw["candidate_sha256"]
        receipts = _source_receipt(control, str(directory / "source-stage"), "receipt.json", "stage")
        checks.update({"case_plan", "bank"})
    elif kind == "guide_compile":
        method = m["method"]
        method_result, _ = _source_entry(control, m["inputs"]["method_result"])
        reference, _ = _source_entry(control,
            m["inputs"]["phase_receipts"]["guide_reference"])
        evidence = runner.compile_guide(m["request"], plan,
            guide_root / "guide-cases.npz", method_result, reference,
            directory / "source-guide", method=method,
            effort_target=m["effort_target"], clock=source_clock,
            bank_receipt_sha256=guide_pin["sha256"])
        history = list(m["inputs"]["guide_history"][method])
        history.append(_descriptor(control, evidence["evidence_artifact"]["directory"],
                                   "guide-evidence.json"))
        outputs = {"guide_history": history,
                   "guide_result": history[-1]}
        scientific_status = "passed" if evidence["verdict"]["guide_passed"] else "failed"
        receipts = _source_receipt(control,
            evidence["evidence_artifact"]["directory"], "guide-evidence.json", "guide_evidence")
        checks.update({"candidate", "reference", "case_plan", "guide_history"})
    elif kind == "method_seal":
        wrappers = {method: _pinned(control, m["inputs"]["method_wrappers"][method],
                                    "method-state.json")
                    for method in ("hnpe", "tmcmc")}
        runner.seal_method_states(m["request"], plan, hnpe=wrappers["hnpe"],
            tmcmc=wrappers["tmcmc"], accounting=costs,
            guide_preparation=preparation)
        seal = runner.sealing_receipt_descriptor(m["request"], plan, preparation)
        verified = runner.validate_sealing_receipt(m["request"], plan, seal)
        authority = custody.sealed_evidence(m["request"], plan)
        if (authority["sealing_receipt"] != seal
                or verified["accounting_anchor"] != snapshot):
            raise NativeContractError("Source seal authority or full input ledger differs")
        outputs = {"method_seal": seal, "sealed_allocation_anchor": snapshot}
        pins.update(method_seal_sha256=seal["sha256"],
                    sealed_allocation_anchor_sha256=protocol.checksum(snapshot))
        checks.update(protocol.required_validation_checks(kind))
    elif kind == "confirmation_preparation":
        seal, verified, authority = _sealed(control, custody, runner, plan)
        authorization = m["inputs"]["confirmation_authorization"]
        for name in ("campaign_instance_id", "source_binding_sha256",
                     "scientific_request_hash", "transport_request_hash"):
            if authorization.get(name) != m[name]:
                raise NativeContractError("Confirmation authorization identity differs")
        for name in ("case_plan_sha256", "method_seal_sha256",
                     "sealed_allocation_anchor_sha256"):
            if authorization.get(name) != m["pins"][name]:
                raise NativeContractError("Confirmation authorization custody differs")
        secret = _read_json(Path(control["campaign_directory"]) / "private" / "confirmation-nonce.json")
        if custody.nonce_commitment(secret["nonce"]) != m["inputs"]["confirmation_commitment"]:
            raise NativeContractError("Private confirmation nonce differs from commitment")
        receipt = custody.prepare_confirmation_bank(m["request"], m["campaign_instance_id"],
            m["inputs"]["confirmation_commitment"], nonce=secret["nonce"],
            method_states=verified["method_states"], output=directory / "source-bank",
            case_plan=plan, sealing_receipt=m["inputs"]["method_seal"])
        authority = custody.sealed_evidence(m["request"], plan)
        output = authority["confirmation_output"]
        bank_pin = _descriptor(control, output, "confirmation-receipt.json")
        if authority["confirmation_receipt_sha256"] != bank_pin["sha256"]:
            raise NativeContractError("Confirmation output differs from global first opening")
        pins["opening_id"] = receipt["opening_id"]
        outputs["confirmation_bank"] = bank_pin
        checks.update(protocol.required_validation_checks(kind))
    elif kind in {"confirmation_hnpe_inference", "confirmation_tmcmc_attempt",
                  "confirmation_reference", "record_confirmation_outcome",
                  "confirmation_compile"}:
        seal, bank, bank_path, bank_pin = _bank(control, custody, runner, plan)
        if kind == "confirmation_compile":
            entries = {}
            for method in ("hnpe", "tmcmc", "reference"):
                pin = m["inputs"]["phase_receipts"]["confirmation_" + method]
                entries[method], _ = _source_entry(control, pin, wrapped=method == "reference")
            compile_result = runner.compile_confirmation(m["request"], plan,
                {"bank_path": str(bank_path), "sha256": bank_pin["sha256"]},
                seal["method_states"], entries, costs, directory / "source-comparison",
                clock=source_clock, guide_preparation=preparation,
                guide_history={method: _history(control, runner, plan, method)
                               for method in ("hnpe", "tmcmc")})
            # The original comparison intentionally retains this reporting
            # allocation as active. A source-owned metadata-only finalizer must
            # close it after SACCT; never rerun numerical compile on login.
            first = custody.read_confirmation_execution(m["request"], plan, "comparison")
            if (first is None or first["receipt"] is None
                    or first["receipt"]["sha256"] != compile_result["comparison_sha256"]):
                raise NativeContractError("First comparison authority was not completed")
            immutable_json(directory / "comparison-first.json", {
                "schema": "aws_mesouq.benchmark_accuracy_first_comparison.v1",
                "binding": _binding(control, job),
                "source_comparison": first["receipt"]})
            return 0
        if kind == "record_confirmation_outcome":
            if not hasattr(runner, "record_confirmation_outcome"):
                raise NativeContractError("Source-owned no-run first authority is unavailable")
            stage = runner.record_confirmation_outcome(m["request"], plan,
                {"bank_path": str(bank_path), "sha256": bank_pin["sha256"]},
                m["method"], costs, directory / "source-stage", clock=source_clock)
        elif kind == "confirmation_reference":
            stage = runner.run_reference_cases(m["request"], plan, bank_path,
                directory / "source-stage", role="confirmation", clock=source_clock,
                device="cpu", bank_receipt_sha256=bank_pin["sha256"])
        elif kind == "confirmation_tmcmc_attempt":
            candidate = seal["method_states"]["methods"]["tmcmc"]["candidate"]
            stage = runner.run_tmcmc_attempt(m["request"], plan, bank_path,
                directory / "source-stage", role="confirmation",
                population_size=candidate["population_size"], attempt_id=m["task_id"],
                clock=source_clock, device="cuda", bank_receipt_sha256=bank_pin["sha256"])
        else:
            bundle = seal["hnpe"]["candidate"]
            stage = runner.evaluate_hnpe_bundle(m["request"], plan, bank_path,
                bundle, directory / "source-stage", role="confirmation",
                clock=source_clock, device="cuda", bank_receipt_sha256=bank_pin["sha256"])
        slot = custody.read_confirmation_execution(m["request"], plan, m["method"])
        if slot is None or slot["receipt"] is None:
            raise NativeContractError("Source first confirmation authority is incomplete")
        actual_root = slot["receipt"]["directory"]
        receipts = _source_receipt(control, actual_root, "receipt.json", "stage")
        pin = {k: receipts["stage"][k] for k in ("directory", "sha256")}
        if pin != slot["receipt"]:
            raise NativeContractError("First confirmation receipt differs from source bytes")
        outputs["confirmation_outcomes"] = {m["method"]: pin}
        checks.update(protocol.required_validation_checks(kind))
    else:
        raise NativeContractError("Unsupported fixed source stage")
    publication = _native_publication(control, job, outputs=outputs, pins=pins,
        receipts=receipts, checks=checks, proposal=proposal,
        scientific_status=scientific_status)
    return 0



class _MetadataOnlyFinder(importlib.abc.MetaPathFinder):
    blocked = ("numpy", "scipy", "torch", "jax", "jaxlib", "keras",
               "tensorflow", "matplotlib", "sklearn")

    def find_spec(self, fullname, path=None, target=None):
        if fullname.split(".", 1)[0] in self.blocked:
            raise NativeContractError("Numerical/model imports are forbidden in postexit metadata finalization")
        return None


@contextmanager
def _metadata_only():
    blocker = _MetadataOnlyFinder()
    if any(name.split(".", 1)[0] in blocker.blocked for name in sys.modules):
        raise NativeContractError("Postexit metadata process already imported a numerical backend")
    sys.meta_path.insert(0, blocker)
    try:
        yield
    finally:
        sys.meta_path.remove(blocker)


def _role_document(control, root, descriptor, filename, *, maximum):
    _keys(descriptor, {"file", "sha256", "size_bytes", "content_type", "kind"})
    if descriptor["file"] != filename or descriptor["content_type"] != "application/json":
        raise NativeContractError("Source publication role substituted another file/type")
    with stable_bytes(root / filename, maximum=maximum, private=True) as (raw, _):
        if len(raw) != descriptor["size_bytes"] or hashlib.sha256(raw).hexdigest() != descriptor["sha256"]:
            raise NativeContractError("Source publication role bytes changed")
        return _json(raw)


def _curated_specs(control, root, descriptors):
    context = control["context"]
    mime = {".json": "application/json", ".csv": "text/csv",
        ".npz": "application/octet-stream", ".png": "image/png",
        ".svg": "image/svg+xml", ".md": "text/markdown"}
    if not isinstance(descriptors, list) or not 1 <= len(descriptors) <= context["maximum_artifact_files"]:
        raise NativeContractError("Source curated artifact count exceeds context")
    allowed = set(context["allowed_artifacts"].get("confirmation_compile", []))
    specs, files, total = [], {}, 0
    for descriptor in descriptors:
        if not isinstance(descriptor, dict):
            raise NativeContractError("Source curated descriptor required")
        name = descriptor["file"]
        if (not isinstance(name, str) or re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,159}", name) is None
                or Path(name).name != name or name not in allowed or name in files):
            raise NativeContractError("Source curated filename is duplicate or unapproved")
        kind = descriptor.get("kind", "scientific_artifact")
        if not isinstance(kind, str) or re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,159}", kind) is None:
            raise NativeContractError("Source curated artifact kind differs")
        size = descriptor["size_bytes"]
        if type(size) is not int or not 0 < size <= min(MAX_FILE, context["maximum_artifact_bytes"]):
            raise NativeContractError("Source curated file exceeds context allowance")
        total += size
        if total > min(MAX_CURATED, context["maximum_total_artifact_bytes"]):
            raise NativeContractError("Source complete curated publication exceeds context")
        media = descriptor["content_type"]
        if mime.get(Path(name).suffix) != media:
            raise NativeContractError("Source curated extension and MIME differ")
        _sha(descriptor["sha256"])
        path = _inside_campaign(control, root / name)
        with stable_bytes(path, maximum=context["maximum_artifact_bytes"], private=True) as (raw, _):
            if len(raw) != size or hashlib.sha256(raw).hexdigest() != descriptor["sha256"]:
                raise NativeContractError("Source curated bytes differ from source admission")
        specs.append({"name": name, "sha256": descriptor["sha256"],
            "size_bytes": size, "media_type": media, "kind": kind})
        files[name] = str(path)
    return specs, files


def _validate_capsule_projection(control, final, capsule, summary, ledger, job):
    m = control["manifest"]
    expected_binding = {key: m[key] for key in ("campaign_instance_id",
        "scientific_request_hash", "transport_request_hash", "source_binding_sha256",
        "policy_sha256")}
    expected_binding.update(case_plan_sha256=m["pins"]["case_plan_sha256"],
                            guide_case_set_sha256=m["pins"]["guide_case_set_sha256"])
    if (capsule.get("schema") != "mesouq.benchmark_lab.accuracy_v2.validation_capsule.v1"
            or summary.get("schema") != "mesouq.benchmark_lab.accuracy_v2.display_summary.v1"
            or any(capsule.get("binding", {}).get(key) != value for key, value in expected_binding.items())
            or summary.get("binding") != capsule.get("binding")
            or capsule.get("comparison") != final["comparison"]
            or capsule.get("accounting") != final["accounting"]
            or capsule.get("summary") != final["summary"]
            or summary.get("comparison") != final["comparison"]
            or summary.get("accounting") != final["accounting"]):
        raise NativeContractError("Source bounded summary/capsule identity or roles differ")
    if (type(capsule["final_snapshot_revision"]) is not int
            or type(capsule["allocation_count"]) is not int
            or capsule["final_snapshot_sha256"] != ledger["sha256"]
            or capsule["final_snapshot_revision"] != ledger["snapshot"]["revision"]
            or capsule["allocation_count"] != len(ledger["snapshot"]["allocations"])
            or capsule["comparison_producer"] != {
                "allocation_id": m["reservation"]["allocation_id"],
                "attempt": m["attempt"], "slurm_job_id": job}
            or capsule["closed_numerical_prefix_preserved"] is not True
            or capsule["numerical_results_recomputed"] is not False
            or summary["numerical_statistics_recomputed"] is not False):
        raise NativeContractError("Source postexit accounting producer/closure differs")
    curation = capsule["curation"]
    _keys(curation, {"excludes_validation_capsule_itself", "artifacts_sha256", "file_count", "size_bytes"})
    # Preserve SOURCE list order and descriptor extras; the source hash is not
    # the transport projection hash and intentionally excludes capsule itself.
    without_capsule = [*summary["downloads"], capsule["summary"]]
    if (type(curation["file_count"]) is not int or type(curation["size_bytes"]) is not int
            or curation["excludes_validation_capsule_itself"] is not True
            or checksum(without_capsule) != curation["artifacts_sha256"]
            or len(without_capsule) != curation["file_count"]
            or sum(item["size_bytes"] for item in without_capsule) != curation["size_bytes"]
            or final["artifacts"] != [*without_capsule, final["validation_capsule"]]
            or final["receipt_sha256"] != final["accounting"]["sha256"]):
        raise NativeContractError("Source complete curated manifest order/digest differs")
    if (capsule["scientific_status"] != summary["scientific_status"]
            or capsule["source_assessments"] != summary["methods"]
            or type(capsule["confirmation_case_count"]) is not int
            or capsule["confirmation_case_count"] != 256
            or type(summary["evaluation"]["confirmation_case_count"]) is not int
            or summary["evaluation"]["confirmation_case_count"] != capsule["confirmation_case_count"]):
        raise NativeContractError("Source summary changed scientific assessments or denominator")


def _final_publication(payload, guard):
    """Pure source-owned metadata closure; never rerun numerical compilation."""
    control = _entry_control(payload["context"], payload["entry"])
    job = _job(payload["entry"]["job_id"])
    directory, m = Path(control["control_directory"]), control["manifest"]
    ledger = payload["accounting"]
    if checksum(ledger["snapshot"]) != ledger["sha256"]:
        raise NativeContractError("Closed authoritative allocation snapshot checksum differs")
    guard.install()
    with _metadata_only():
        from aws_mesouq import benchmark_accuracy_protocol as protocol
        from meso_uq.benchmark_lab.accuracy_v2 import publication
        protocol.validate_snapshot(ledger["snapshot"])
        if any(row["status"] not in protocol.TERMINAL or row["reserved_gpu_seconds"] != 0
               or row["allocated_gpu_seconds"] is None or row["allocated_cpu_seconds"] is None
               for row in ledger["snapshot"]["allocations"]):
            raise NativeContractError("Postexit publication requires all actual native costs")
        guide = _pinned(control, m["inputs"]["phase_receipts"]["guide_preparation"],
                        "guide-receipt.json")
        plan = guide["case_plan"]
        if (checksum(plan) != m["pins"]["case_plan_sha256"]
                or plan["campaign_instance_id"] != m["campaign_instance_id"]
                or plan["source_binding_sha256"] != m["source_binding_sha256"]):
            raise NativeContractError("Postexit case plan differs from native source pin")
        first = _read_json(directory / "comparison-first.json")
        if first["binding"] != _binding(control, job):
            raise NativeContractError("First native compilation binding changed")
        final = publication.finalize_publication(plan, ledger["snapshot"])
        _keys(final, {"directory", "receipt_sha256", "comparison", "accounting",
                      "summary", "validation_capsule", "artifacts"})
        root = _inside_campaign(control, final["directory"])
        if first["source_comparison"] != {
                "directory": str(root), "sha256": final["comparison"]["sha256"]}:
            raise NativeContractError("Source finalizer changed the original first comparison")
        summary = _role_document(control, root, final["summary"], "display-summary.json",
                                 maximum=262_144)
        capsule = _role_document(control, root, final["validation_capsule"],
                                 "validation-capsule.json", maximum=262_144)
        _validate_capsule_projection(control, final, capsule, summary, ledger, job)
        expected_prior = protocol.checksum(protocol.bind_current_allocation(m, job))
        if capsule["prior_snapshot_sha256"] != expected_prior:
            raise NativeContractError("Source comparison input allocation anchor changed")
        specs, files = _curated_specs(control, root, final["artifacts"])
        roles = {"result_artifact_id": final["comparison"]["file"],
            "accounting_artifact_id": final["accounting"]["file"],
            "summary_artifact_id": final["summary"]["file"],
            "validation_capsule_artifact_id": final["validation_capsule"]["file"]}
        if len(set(roles.values())) != 4:
            raise NativeContractError("Source publication roles cannot alias")
        fixed = {"comparison": ("comparison.json", "scientific_comparison"),
            "accounting": ("allocation-accounting-final.json", "allocation_accounting_final"),
            "summary": ("display-summary.json", "display_summary"),
            "validation_capsule": ("validation-capsule.json", "validation_capsule")}
        receipts = {}
        for role, (name, kind) in fixed.items():
            descriptor = final[role]
            _keys(descriptor, {"file", "sha256", "size_bytes", "content_type", "kind"})
            if descriptor["file"] != name or descriptor["content_type"] != "application/json" or descriptor["kind"] != kind:
                raise NativeContractError("Source final publication role filename/kind differs")
            if not any(d == descriptor for d in final["artifacts"]):
                raise NativeContractError("Source final role is absent from curated artifact manifest")
            receipts[role] = {"directory": str(root), "sha256": descriptor["sha256"], "file": name}
        qualification = {"confirmation_case_count": capsule["confirmation_case_count"],
            "hnpe": deepcopy(capsule["source_assessments"]["hnpe"]),
            "tmcmc": deepcopy(capsule["source_assessments"]["tmcmc"])}
        checks = protocol.required_validation_checks(m["kind"]) | {
            "bounded_source_summary", "source_validation_capsule", "curated_export_coverage"}
        result = _native_publication(control, job, outputs={
                "final_accounting_receipt": {"directory": str(root),
                                            "sha256": final["receipt_sha256"]}},
            pins=deepcopy(m["pins"]), receipts=receipts, checks=checks,
            scientific_status=capsule["scientific_status"], qualification=qualification,
            artifacts=specs, artifact_roles=roles, files=files)
        return {key: result[key] for key in ("result", "artifacts", "artifact_roles")}

def _publication(payload, guard):
    control = _entry_control(payload["context"], payload["entry"])
    job = _job(payload["entry"]["job_id"])
    directory = Path(control["control_directory"])
    if control["manifest"]["kind"] == "confirmation_compile":
        return _final_publication(payload, guard)
    publication = _read_json(directory / "native-publication.json", maximum=MAX_CONTROL)
    if (publication["schema"] != PUBLICATION_SCHEMA
            or publication["binding"] != _binding(control, job)
            or publication["result"]["native_receipt"] !=
               _descriptor(control, directory, "native-receipt.json")):
        raise NativeContractError("Native publication identity or wrapper changed")
    # Always authenticate wrapper bytes and their exact current producer.
    wrapper = _read_json(directory / "native-receipt.json", maximum=MAX_CONTROL)
    if wrapper["binding"] != _binding(control, job):
        raise NativeContractError("Native source validation producer changed")
    expected_outputs = deepcopy(wrapper["outputs"])
    if control["manifest"]["kind"] in {"hnpe_initialize", "hnpe_advance"}:
        expected_outputs["progress"]["native_receipt"] = publication["result"]["native_receipt"]
    for name, expected in (("outputs", expected_outputs),
                           ("output_pins", wrapper["output_pins"]),
                           ("proposal", wrapper["proposal"]),
                           ("scientific_status", wrapper["scientific_status"]),
                           ("qualification", wrapper["qualification"])):
        if publication["result"][name] != expected:
            raise NativeContractError("Publication differs from native validated source receipt")
    if publication["artifacts"] != wrapper["artifacts"]:
        raise NativeContractError("Publication artifact set changed")
    guard.install()
    from aws_mesouq import benchmark_accuracy_protocol as protocol
    protocol.validate_result(publication["result"], control["manifest"],
                              native_root=control["context"]["native_root"])
    return {k: publication[k] for k in ("result", "artifacts", "artifact_roles")}


def _read_artifact(payload):
    control = _entry_control(payload["context"], payload["entry"])
    publication = _read_json(Path(control["control_directory"]) /
                             "native-publication.json", maximum=MAX_CONTROL)
    spec = payload["spec"]
    if spec not in publication["artifacts"]:
        raise NativeContractError("Artifact is absent from immutable source publication")
    path = publication["files"].get(spec["name"])
    if path is None:
        raise NativeContractError("Curated artifact lacks authenticated native file")
    _inside_campaign(control, path)
    maximum = min(MAX_FILE, control["context"]["maximum_artifact_bytes"])
    with stable_bytes(path, maximum=maximum, private=True) as (raw, _):
        if len(raw) != spec["size_bytes"] or hashlib.sha256(raw).hexdigest() != spec["sha256"]:
            raise NativeContractError("Curated artifact checksum or size changed")
        return raw


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--manifest")
    group.add_argument("--execute-stage")
    args = parser.parse_args(argv)
    control = _load_control(args.manifest or args.execute_stage)
    return _allocated(control, execute=args.execute_stage is not None)


if __name__ == "__main__":
    raise SystemExit(main())
