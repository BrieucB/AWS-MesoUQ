"""Explicit own-user local or SSH transport; never evaluates user-provided shell code."""
from __future__ import annotations

import re
import shlex
import subprocess
import sys
from pathlib import Path


PRIVATE_ROOT_PROBE = r'''import os,stat,sys,uuid
path=sys.argv[1]
if os.path.realpath(path)!=path:
    raise ValueError('Storage root must be canonical without symlinks')
root=os.open(path,os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW)
probe=None
fd=None
previous_umask=os.umask(0o077)
try:
    info=os.fstat(root)
    if not stat.S_ISDIR(info.st_mode) or info.st_uid!=os.getuid() or stat.S_IMODE(info.st_mode)!=0o700:
        raise ValueError('Storage root must be an existing account-owned directory with exact mode 0700')
    probe='.aws-mesouq-private-probe-'+uuid.uuid4().hex
    # Match Path.open's creation mode: a permissive default ACL can override
    # umask and create mode0660 files even under an exact0700 parent.
    fd=os.open(probe,os.O_WRONLY|os.O_CREAT|os.O_EXCL|os.O_NOFOLLOW,0o666,dir_fd=root)
    created=os.fstat(fd)
    if not stat.S_ISREG(created.st_mode) or created.st_uid!=os.getuid() or created.st_nlink!=1 or stat.S_IMODE(created.st_mode)!=0o600:
        raise ValueError('Storage root creates nonprivate files despite umask077; operator must review inherited default ACLs')
    os.fsync(fd)
finally:
    try:
        if fd is not None:
            created=os.fstat(fd)
            try:
                current=os.stat(probe,dir_fd=root,follow_symlinks=False)
            except FileNotFoundError:
                current=None
            if current is not None:
                if (current.st_dev,current.st_ino,current.st_uid)!=(created.st_dev,created.st_ino,os.getuid()) or not stat.S_ISREG(current.st_mode):
                    raise ValueError('Unique storage probe changed; refusing to remove another entry')
                os.unlink(probe,dir_fd=root)
                os.fsync(root)
    finally:
        if fd is not None:
            os.close(fd)
        os.close(root)
        os.umask(previous_umask)
'''


class Transport:
    def __init__(self, *, host: str | None = None, local: bool = False):
        if local == bool(host):
            raise ValueError("Select exactly one of verified local mode or an SSH host")
        if host and not re.fullmatch(r"[A-Za-z0-9_][A-Za-z0-9_.@-]*", host):
            raise ValueError("Invalid SSH host alias")
        self.host = host
        # Local helpers use the explicitly selected connector interpreter. SSH
        # helpers retain the verified cluster-side Python policy, never a bare
        # system python3 (which is unsupported on Karolina).
        self.staging_python = sys.executable if local else "/usr/bin/python3.11"

    def run_bytes(self, argv: list[str], *, data: bytes | None = None) -> bytes:
        command = argv if self.host is None else ["ssh", "-oBatchMode=yes", "-oForwardAgent=no", self.host, shlex.join(argv)]
        result = subprocess.run(command, input=data, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                check=True, timeout=90)
        return result.stdout

    def run(self, argv: list[str], *, data: bytes | None = None) -> str:
        return self.run_bytes(argv, data=data).decode()

    def identity(self) -> str:
        return self.run(["id", "-un"]).strip()

    def private_root_probe(self, root: str) -> None:
        """Check effective file privacy without changing the root or its ACLs."""
        self.run([getattr(self, "staging_python", "/usr/bin/python3.11"), "-I", "-c",
                  PRIVATE_ROOT_PROBE, absolute_path(root)])

    def write(self, path: str, data: bytes) -> None:
        # noclobber plus quoted positional argv gives immutable staging on both transports.
        self.run(["sh", "-c", 'umask 077; set -C; cat > "$1"', "stage", path], data=data)

    def read(self, path: str) -> bytes:
        return self.run_bytes([self.staging_python, "-I", "-c",
            "import os,stat,sys; f=os.open(sys.argv[1],os.O_RDONLY|os.O_NOFOLLOW|os.O_NONBLOCK); "
            "assert stat.S_ISREG(os.fstat(f).st_mode); "
            "sys.stdout.buffer.write(os.fdopen(f,'rb').read())", path])

    def check_files(self, root: str, names: list[str]) -> None:
        self._beneath(root, names, read=False)

    def archive_prepare(self, root: str, token: str) -> None:
        from .archive import ARCHIVE_HELPER
        self.run([self.staging_python, "-I", "-c", ARCHIVE_HELPER,
                  absolute_path(root), safe_id(token), "prepare"])

    def archive_copy(self, root: str, token: str, item: dict) -> None:
        import json
        from .archive import ARCHIVE_HELPER
        absolute_path(item["source_root"])
        safe_relative(item["source"])
        safe_relative(item["destination"])
        self.run([self.staging_python, "-I", "-c", ARCHIVE_HELPER,
                  absolute_path(root), safe_id(token), "copy", json.dumps(item)])

    def archive_verify(self, root: str, token: str, item: dict) -> None:
        import json
        from .archive import ARCHIVE_HELPER
        safe_relative(item["destination"])
        self.run([self.staging_python, "-I", "-c", ARCHIVE_HELPER,
                  absolute_path(root), safe_id(token), "verify", json.dumps(item)])

    def _archive_batch(self, root: str, token: str, items: list[dict], operation: str) -> None:
        import json
        from .archive import ARCHIVE_HELPER
        absolute_path(root)
        safe_id(token)
        if operation not in ("copy-batch", "verify-batch") or not isinstance(items, list) or not 1 <= len(items) <= 32:
            raise ValueError("Archive batches require between1 and32 items")
        allowed = {"source_root", "source", "destination", "sha256", "size_bytes", "source_link_group"}
        required = {"destination", "sha256", "size_bytes"}
        if operation == "copy-batch":
            required |= {"source_root", "source"}
        seen = set()
        for item in items:
            if not isinstance(item, dict) or not required <= set(item) <= allowed:
                raise ValueError("Unexpected archive batch fields")
            safe_relative(item["destination"])
            if item["destination"] in seen:
                raise ValueError("Duplicate archive batch destination")
            seen.add(item["destination"])
            if not isinstance(item["sha256"], str) or not re.fullmatch(r"[a-f0-9]{64}", item["sha256"]) or type(item["size_bytes"]) is not int or item["size_bytes"] < 0:
                raise ValueError("Invalid archive batch checksum or size")
            if operation == "copy-batch":
                absolute_path(item["source_root"])
                safe_relative(item["source"])
                if "source_link_group" in item and not isinstance(item["source_link_group"], dict):
                    raise ValueError("Invalid archive source proof")
        payload = json.dumps(items, allow_nan=False, separators=(",", ":")).encode()
        if len(payload) > 4 * 1024 * 1024:
            raise ValueError("Archive batch payload exceeds4MiB")
        self.run([self.staging_python, "-I", "-c", ARCHIVE_HELPER,
                  absolute_path(root), safe_id(token), operation], data=payload)

    def archive_copy_batch(self, root: str, token: str, items: list[dict]) -> None:
        """Copy and reopen/verify at most32 files in one bounded private RPC."""
        self._archive_batch(root, token, items, "copy-batch")

    def archive_verify_batch(self, root: str, token: str, items: list[dict]) -> None:
        """Verify one bounded batch; corruption takes priority over missing files."""
        self._archive_batch(root, token, items, "verify-batch")


    def read_beneath(self, root: str, name: str) -> bytes:
        return self._beneath(root, [name], read=True)

    def _beneath(self, root: str, names: list[str], *, read: bool) -> bytes:
        import json
        for name in names:
            safe_relative(name)
        code = """import json,os,stat,sys
for name in json.loads(sys.argv[2]):
    parent=os.open(sys.argv[1],os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW)
    parts=name.split('/')
    try:
        for part in parts[:-1]:
            child=os.open(part,os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW,dir_fd=parent)
            os.close(parent)
            parent=child
        fd=os.open(parts[-1],os.O_RDONLY|os.O_NOFOLLOW|os.O_NONBLOCK,dir_fd=parent)
        with os.fdopen(fd,'rb') as stream:
            assert stat.S_ISREG(os.fstat(stream.fileno()).st_mode)
            if sys.argv[3]=='read':
                sys.stdout.buffer.write(stream.read())
    finally:
        os.close(parent)
"""
        return self.run_bytes([self.staging_python, "-I", "-c", code, root, json.dumps(names), "read" if read else "check"])


def safe_relative(value: str) -> str:
    if not isinstance(value, str) or not value or len(value) > 1024 or any(
        not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]*", part) or part in {".", ".."}
        for part in value.split("/")
    ):
        raise ValueError("Unsafe scientific publication filename")
    return value


def safe_id(value: str) -> str:
    if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,127}", value):
        raise ValueError("Invalid identifier")
    return value


def absolute_path(value: str) -> str:
    path = Path(value)
    if not path.is_absolute() or ".." in path.parts or "\n" in value or "\x00" in value:
        raise ValueError("Expected a trusted absolute path")
    return str(path)
