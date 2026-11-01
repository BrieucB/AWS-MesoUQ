"""Trusted package registration and replaceable connector worker processes."""
from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import select
import stat
import subprocess
import sys
import threading
import time
from typing import Mapping

from .auth_broker import AuthBroker, BrokerError, BrokerSecurityError, _canonical_path, _open_directory
from .broker_client import CONTROLLER_ENV, GENERATION_ENV, SOCKET_ENV

SPEC_SCHEMA = "aws_mesouq.approved_source_worker.v1"
MAX_PACKAGE_FILES = 4096
MAX_FILE_BYTES = 32 * 1024 * 1024
MAX_PACKAGE_BYTES = 128 * 1024 * 1024
MAX_BOOTSTRAP_BYTES = 1024 * 1024


def _relative_name(value: str) -> str:
    if not isinstance(value, str) or "\\" in value or "\0" in value:
        raise BrokerSecurityError("invalid_package_path")
    path = PurePosixPath(value)
    if (
        path.is_absolute() or not path.parts or path.as_posix() != value
        or any(part in (".", "..", ".git", "__pycache__") for part in path.parts)
        or path.suffix in (".pyc", ".pyo")
        or len(value.encode("utf-8")) > 1024
    ):
        raise BrokerSecurityError("invalid_package_path")
    return value


def _sha256(value: object) -> bool:
    return (
        isinstance(value, str) and len(value) == 64
        and all(char in "0123456789abcdef" for char in value)
    )


def source_binding(entrypoint: str, files: Mapping[str, str]) -> str:
    """Compute the reproducible binding; approval of it is the owner's action."""
    entrypoint = _relative_name(entrypoint)
    if not isinstance(files, Mapping) or not 0 < len(files) <= MAX_PACKAGE_FILES:
        raise BrokerSecurityError("invalid_package_manifest")
    normalized = {}
    for name, digest in files.items():
        normalized[_relative_name(name)] = digest
        if not _sha256(digest):
            raise BrokerSecurityError("invalid_package_manifest")
    if entrypoint not in normalized:
        raise BrokerSecurityError("entrypoint_not_bound")
    body = {"schema": SPEC_SCHEMA, "entrypoint": entrypoint, "files": normalized}
    return hashlib.sha256(
        json.dumps(body, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def _package_names(root: Path) -> set[str]:
    names = set()
    directory_count = 0
    for parent, directories, files in os.walk(root, followlinks=False):
        directory_count += len(directories)
        if directory_count > MAX_PACKAGE_FILES * 2:
            raise BrokerSecurityError("package_tree_too_large")
        for name in directories:
            path = Path(parent) / name
            info = os.lstat(path)
            if (
                not stat.S_ISDIR(info.st_mode)
                or info.st_uid not in (0, os.geteuid())
                or stat.S_IMODE(info.st_mode) & 0o022
            ):
                raise BrokerSecurityError("unsafe_package_directory")
        for name in files:
            names.add(_relative_name((Path(parent) / name).relative_to(root).as_posix()))
            if len(names) > MAX_PACKAGE_FILES:
                raise BrokerSecurityError("package_tree_too_large")
    return names


def _package_file(rootfd: int, name: str) -> tuple[str, int]:
    parts = PurePosixPath(name).parts
    fd = os.dup(rootfd)
    try:
        for part in parts[:-1]:
            child = os.open(
                part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd
            )
            os.close(fd)
            fd = child
            info = os.fstat(fd)
            if info.st_uid not in (0, os.geteuid()) or stat.S_IMODE(info.st_mode) & 0o022:
                raise BrokerSecurityError("unsafe_package_directory")
        filefd = os.open(parts[-1], os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=fd)
        try:
            before = os.fstat(filefd)
            if (
                not stat.S_ISREG(before.st_mode) or before.st_nlink != 1
                or before.st_uid not in (0, os.geteuid())
                or stat.S_IMODE(before.st_mode) & 0o022
                or before.st_size > MAX_FILE_BYTES
            ):
                raise BrokerSecurityError("unsafe_package_file")
            digest = hashlib.sha256()
            size = 0
            while data := os.read(filefd, 1024 * 1024):
                size += len(data)
                if size > MAX_FILE_BYTES:
                    raise BrokerSecurityError("package_file_too_large")
                digest.update(data)
            after = os.fstat(filefd)
            fields = ("st_dev", "st_ino", "st_mode", "st_uid", "st_nlink", "st_size", "st_mtime_ns", "st_ctime_ns")
            if any(getattr(before, key) != getattr(after, key) for key in fields):
                raise BrokerSecurityError("package_changed")
            return digest.hexdigest(), size
        finally:
            os.close(filefd)
    finally:
        os.close(fd)


@dataclass(frozen=True)
class SourceWorkerSpec:
    """A complete approved source-only package, not a shell command.

    The approved binding must come from the trusted owner/release workflow.
    A worker cannot register or replace this object through broker RPC.
    """

    package_root: Path
    entrypoint: str
    files: tuple[tuple[str, str], ...]
    approved_binding_sha256: str
    arguments: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        root = _canonical_path(self.package_root)
        files = tuple(self.files)
        arguments = tuple(self.arguments)
        if (
            len({name for name, _ in files}) != len(files)
            or not _sha256(self.approved_binding_sha256)
            or source_binding(self.entrypoint, dict(files)) != self.approved_binding_sha256
            or len(arguments) > 128
            or any(not isinstance(value, str) or "\0" in value for value in arguments)
            or sum(len(value.encode("utf-8")) for value in arguments) > 65536
        ):
            raise BrokerSecurityError("invalid_worker_specification")
        object.__setattr__(self, "package_root", root)
        object.__setattr__(self, "files", tuple(sorted(files)))
        object.__setattr__(self, "arguments", arguments)

    @classmethod
    def from_manifest(
        cls, package_root: str | Path, manifest: dict,
        approved_binding_sha256: str, *, arguments: tuple[str, ...] = (),
    ) -> "SourceWorkerSpec":
        if (
            not isinstance(manifest, dict)
            or set(manifest) != {"schema", "entrypoint", "files", "binding_sha256"}
            or manifest["schema"] != SPEC_SCHEMA
            or manifest["binding_sha256"] != approved_binding_sha256
            or not isinstance(manifest["files"], dict)
        ):
            raise BrokerSecurityError("invalid_package_manifest")
        return cls(
            Path(package_root), manifest["entrypoint"],
            tuple(manifest["files"].items()), approved_binding_sha256, arguments,
        )

    def validate(self) -> None:
        self.bootstrap_payload()  # Bound gate size before auth or replacing a worker.
        rootfd = _open_directory(self.package_root)
        try:
            if stat.S_IMODE(os.fstat(rootfd).st_mode) & 0o022:
                raise BrokerSecurityError("unsafe_package_directory")
            actual = _package_names(self.package_root)
            if actual != {name for name, _ in self.files}:
                raise BrokerSecurityError("package_tree_not_bound")
            total = 0
            for name, expected in self.files:
                digest, size = _package_file(rootfd, name)
                if digest != expected:
                    raise BrokerSecurityError("package_checksum_mismatch")
                total += size
                if total > MAX_PACKAGE_BYTES:
                    raise BrokerSecurityError("package_too_large")
            if _package_names(self.package_root) != actual:
                raise BrokerSecurityError("package_changed")
        except OSError:
            raise BrokerSecurityError("unsafe_package") from None
        finally:
            os.close(rootfd)

    def bootstrap_payload(self) -> bytes:
        payload = json.dumps(
            {
                "schema": SPEC_SCHEMA, "root": str(self.package_root),
                "entrypoint": self.entrypoint, "files": dict(self.files),
                "binding": self.approved_binding_sha256,
                "arguments": list(self.arguments),
            },
            sort_keys=True, separators=(",", ":"), allow_nan=False,
        ).encode()
        if len(payload) > MAX_BOOTSTRAP_BYTES:
            raise BrokerSecurityError("worker_specification_too_large")
        return len(payload).to_bytes(4, "big") + payload


# This fixed stdlib-only bootstrap executes in a fresh isolated interpreter.
# The bounded stdin frame is the startup gate: the parent registers PID and
# generation before releasing it. No package code is imported before verifying
# every file and the complete reviewed binding again in the child.
_BOOTSTRAP = r'''
import hashlib, json, os, pathlib, stat, sys
def fail():
    raise SystemExit(78)
def exact(n):
    out=b""
    while len(out)<n:
        chunk=sys.stdin.buffer.read(n-len(out))
        if not chunk: fail()
        out+=chunk
    return out
def unique(pairs):
    out={}
    for k,v in pairs:
        if k in out: fail()
        out[k]=v
    return out
def relative(s):
    if not isinstance(s,str) or "\\" in s or "\0" in s: fail()
    p=pathlib.PurePosixPath(s)
    if p.is_absolute() or not p.parts or p.as_posix()!=s: fail()
    if any(v in (".","..",".git","__pycache__") for v in p.parts): fail()
    if p.suffix in (".pyc",".pyo") or len(s.encode())>1024: fail()
    return p.parts
def directory(path):
    if not path.is_absolute() or str(path)!=os.path.abspath(path): fail()
    fd=os.open("/",os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW)
    try:
        for part in path.parts[1:]:
            child=os.open(part,os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW,dir_fd=fd)
            os.close(fd);fd=child
            s=os.fstat(fd);mode=stat.S_IMODE(s.st_mode)
            if s.st_uid not in (0,os.geteuid()): fail()
            if mode&0o022 and not(s.st_uid==0 and mode&stat.S_ISVTX): fail()
        if stat.S_IMODE(os.fstat(fd).st_mode)&0o022: fail()
        return fd
    except BaseException:
        os.close(fd);raise
try:
    length=int.from_bytes(exact(4),"big")
    if not 0<length<=1048576: fail()
    spec=json.loads(exact(length),object_pairs_hook=unique)
    if set(spec)!={"schema","root","entrypoint","files","binding","arguments"}: fail()
    if spec["schema"]!="aws_mesouq.approved_source_worker.v1": fail()
    root=pathlib.Path(spec["root"]);files=spec["files"];entry=spec["entrypoint"]
    relative(entry)
    if not isinstance(files,dict) or not 0<len(files)<=4096 or entry not in files: fail()
    args=spec["arguments"]
    if not isinstance(args,list) or len(args)>128: fail()
    if any(not isinstance(v,str) or "\0" in v for v in args): fail()
    if sum(len(v.encode()) for v in args)>65536: fail()
    binding=hashlib.sha256(json.dumps({"schema":spec["schema"],"entrypoint":entry,"files":files},sort_keys=True,separators=(",",":")).encode()).hexdigest()
    if binding!=spec["binding"]: fail()
    rootfd=directory(root)
    try:
        actual=set();directory_count=0
        for parent,dirs,names in os.walk(root,followlinks=False):
            directory_count+=len(dirs)
            if directory_count>8192: fail()
            for name in dirs:
                info=os.lstat(pathlib.Path(parent)/name)
                if not stat.S_ISDIR(info.st_mode) or info.st_uid not in (0,os.geteuid()) or stat.S_IMODE(info.st_mode)&0o022: fail()
            for name in names:
                rel=(pathlib.Path(parent)/name).relative_to(root).as_posix()
                relative(rel);actual.add(rel)
                if len(actual)>4096: fail()
        if actual!=set(files): fail()
        total=0;entrybytes=None
        for name,expected in sorted(files.items()):
            parts=relative(name)
            if not isinstance(expected,str) or len(expected)!=64 or any(v not in "0123456789abcdef" for v in expected): fail()
            fd=os.dup(rootfd)
            try:
                for part in parts[:-1]:
                    child=os.open(part,os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW,dir_fd=fd)
                    os.close(fd);fd=child
                    s=os.fstat(fd)
                    if s.st_uid not in (0,os.geteuid()) or stat.S_IMODE(s.st_mode)&0o022: fail()
                filefd=os.open(parts[-1],os.O_RDONLY|os.O_NOFOLLOW|os.O_NONBLOCK,dir_fd=fd)
                try:
                    before=os.fstat(filefd)
                    if not stat.S_ISREG(before.st_mode) or before.st_nlink!=1 or before.st_uid not in (0,os.geteuid()) or stat.S_IMODE(before.st_mode)&0o022 or before.st_size>33554432: fail()
                    h=hashlib.sha256();size=0;chunks=[]
                    while True:
                        data=os.read(filefd,1048576)
                        if not data: break
                        size+=len(data)
                        if size>33554432: fail()
                        h.update(data)
                        if name==entry: chunks.append(data)
                    after=os.fstat(filefd)
                    fields=("st_dev","st_ino","st_mode","st_uid","st_nlink","st_size","st_mtime_ns","st_ctime_ns")
                    if any(getattr(before,k)!=getattr(after,k) for k in fields): fail()
                    if h.hexdigest()!=expected: fail()
                    total+=size
                    if total>134217728: fail()
                    if name==entry: entrybytes=b"".join(chunks)
                finally: os.close(filefd)
            finally: os.close(fd)
        final_names=set();directory_count=0
        for parent,dirs,names in os.walk(root,followlinks=False):
            directory_count+=len(dirs)
            if directory_count>8192: fail()
            for name in dirs:
                info=os.lstat(pathlib.Path(parent)/name)
                if not stat.S_ISDIR(info.st_mode) or info.st_uid not in (0,os.geteuid()) or stat.S_IMODE(info.st_mode)&0o022: fail()
            for name in names:
                rel=(pathlib.Path(parent)/name).relative_to(root).as_posix()
                relative(rel);final_names.add(rel)
                if len(final_names)>4096: fail()
        if final_names!=actual: fail()
    finally: os.close(rootfd)
    if entrybytes is None: fail()
except SystemExit:
    raise
except BaseException:
    raise SystemExit(78) from None
sys.dont_write_bytecode=True
sys.path.insert(0,str(root))
sys.argv=[str(root/entry),*args]
scope={"__name__":"__main__","__file__":str(root/entry),"__package__":None,"__spec__":None}
exec(compile(entrybytes,str(root/entry),"exec"),scope)
'''


class WorkerSupervisor:
    """Owner-controlled worker replacement without restarting authentication."""

    def __init__(self, broker: AuthBroker, specification: SourceWorkerSpec):
        self.broker = broker
        self._spec = specification
        self._lock = threading.RLock()
        self._process: subprocess.Popen | None = None
        self._generation = 0

    def start(self) -> int:
        """Cold controller start; only this operation can invoke the login factory."""
        with self._lock:
            self._spec.validate()
            if not self.broker.running:
                self.broker.start()
            return self.start_worker()

    def start_worker(self) -> int:
        """Start a worker against an already-running controller; never signs in."""
        with self._lock:
            if not self.broker.running:
                raise BrokerError("controller_not_running")
            if self._process is not None and self._process.poll() is None:
                raise BrokerError("worker_already_running")
            self._spec.validate()
            if self._generation >= 2**63 - 1:
                raise BrokerError("worker_generation_exhausted")
            self._generation += 1
            env = {
                **os.environ,
                SOCKET_ENV: str(self.broker.socket_path),
                CONTROLLER_ENV: str(os.getpid()),
                GENERATION_ENV: str(self._generation),
                "PYTHONDONTWRITEBYTECODE": "1",
            }
            process = None
            try:
                process = subprocess.Popen(
                    [sys.executable, "-I", "-B", "-c", _BOOTSTRAP],
                    stdin=subprocess.PIPE, stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL, close_fds=True,
                    start_new_session=True, env=env,
                )
                self._process = process
                self.broker._bind_worker(
                    process, self._generation, self._spec.approved_binding_sha256
                )
                assert process.stdin is not None
                fd = process.stdin.fileno()
                os.set_blocking(fd, False)
                data = self._spec.bootstrap_payload()
                offset = 0
                deadline = time.monotonic() + 5.0
                while offset < len(data):
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        raise BrokerError("worker_startup_gate_timeout")
                    if not select.select([], [fd], [], min(0.1, remaining))[1]:
                        continue
                    try:
                        offset += os.write(fd, data[offset:])
                    except BlockingIOError:
                        continue
                process.stdin.close()
                return process.pid
            except BaseException as error:
                self.broker.fence_worker()
                if process is not None:
                    if process.stdin is not None:
                        process.stdin.close()
                    if process.poll() is None:
                        process.kill()
                    process.wait(timeout=5)
                self._process = None
                if isinstance(error, (KeyboardInterrupt, SystemExit)):
                    raise
                raise BrokerError("worker_start_failed") from None

    def stop_worker(self, *, graceful_timeout: float = 15.0) -> int | None:
        if not 0 <= graceful_timeout <= 60:
            raise BrokerError("invalid_stop_timeout")
        with self._lock:
            process = self._process
            self.broker.fence_worker()
            if process is None:
                return None
            if process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=graceful_timeout)
                except subprocess.TimeoutExpired:
                    # Exact owned PID only. Never kill a process group, SSH
                    # master, scheduler allocation, or another user's process.
                    process.kill()
                    process.wait(timeout=5)
            result = process.returncode
            self._process = None
            return result

    def restart_worker(
        self, specification: SourceWorkerSpec | None = None, *,
        graceful_timeout: float = 15.0,
    ) -> int:
        with self._lock:
            candidate = specification or self._spec
            # A bad upgrade leaves the existing worker and auth controller alone.
            candidate.validate()
            self.stop_worker(graceful_timeout=graceful_timeout)
            self._spec = candidate
            return self.start_worker()

    def apply_spec(
        self, specification: SourceWorkerSpec, *, graceful_timeout: float = 15.0
    ) -> int:
        """Hook for a trusted owner-controlled reviewed-package loader/CLI loop."""
        with self._lock:
            specification.validate()
            if (
                specification == self._spec and self._process is not None
                and self._process.poll() is None
            ):
                return self._process.pid
            return self.restart_worker(
                specification, graceful_timeout=graceful_timeout
            )

    def safe_status(self) -> dict:
        with self._lock:
            return {
                **self.broker.safe_status(),
                "worker_exit_code": self._process.poll() if self._process else None,
                "registered_package_binding": self._spec.approved_binding_sha256,
            }

    def close(self, *, graceful_timeout: float = 15.0) -> None:
        self.stop_worker(graceful_timeout=graceful_timeout)
        self.broker.close()

    def __enter__(self) -> "WorkerSupervisor":
        self.start()
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()
