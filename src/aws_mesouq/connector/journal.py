"""Atomic private submission journal; lock prevents two local session processes."""
from __future__ import annotations

import fcntl
import json
import os
import stat
from pathlib import Path
import tempfile


class Journal:
    def __init__(self, directory: Path):
        directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        if Path(os.path.abspath(directory)) != directory.resolve() or directory.is_symlink() or directory.stat().st_uid != os.getuid() or stat.S_IMODE(directory.stat().st_mode) != 0o700:
            raise ValueError("Journal directory must be private and owned by this user")
        self.directory = directory
        self.lock = os.open(directory / "session.lock", os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
        try:
            self._private_file(self.lock)
            fcntl.flock(self.lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except Exception:
            os.close(self.lock)
            raise
        self.path = directory / "journal.json"
        try:
            fd = os.open(self.path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        except FileNotFoundError:
            self.data = {"jobs": {}}
        except Exception:
            self.close()
            raise
        else:
            try:
                with os.fdopen(fd) as stream:
                    self._private_file(stream.fileno())
                    self.data = json.load(stream)
            except Exception:
                self.close()
                raise

    @staticmethod
    def _private_file(fd):
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode) or st.st_uid != os.getuid() or stat.S_IMODE(st.st_mode) != 0o600 or st.st_nlink != 1:
            raise ValueError("Journal and device key files must be private regular owner-only files")

    def save(self):
        fd, temporary = tempfile.mkstemp(dir=self.directory)
        try:
            with os.fdopen(fd, "w") as stream:
                json.dump(self.data, stream, allow_nan=False)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, self.path)
            directory_fd = os.open(self.directory, os.O_DIRECTORY)
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)

    def close(self):
        os.close(self.lock)
