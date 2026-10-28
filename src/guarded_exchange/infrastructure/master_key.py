"""The local master key file (PLAN ADR-009, ADR-015).

32 random bytes, stored base64url-encoded in a file readable only by its owner (0600), in a
directory outside the database directory. Docker and 0600 protect against accidental reads,
not against an agent running as the same OS user (PLAN D-22).
"""

import base64
import binascii
import os
import secrets
import stat
from pathlib import Path
from typing import Final

KEY_BYTES: Final = 32
FILE_MODE: Final = 0o600
DIR_MODE: Final = 0o700


class MasterKeyError(Exception):
    """The key file exists but is unsafe or unreadable; refuse to use it (fail closed)."""


class FileMasterKeyStore:
    def __init__(self, path: Path) -> None:
        self._path = path

    @property
    def path(self) -> Path:
        return self._path

    def exists(self) -> bool:
        try:
            info = self._path.lstat()
        except FileNotFoundError:
            return False
        self._check_mode(info)
        self._decode(self._path.read_bytes())
        return True

    def load(self) -> bytes:
        """The key bytes, after the same safety checks as ``exists``; missing -> ``MasterKeyError``.

        Read through one ``O_NOFOLLOW`` descriptor whose ``fstat`` is checked, so the file that is
        checked is the file that is read. ``O_NONBLOCK``: a FIFO planted at the path is refused as
        "not a regular file" instead of hanging the server on open.
        """
        try:
            fd = os.open(self._path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        except FileNotFoundError:
            raise MasterKeyError(
                f"master key not found at {self._path}; run `guarded-exchange init` on a new installation "
                "or restore the key from backup (or point GX_MASTER_KEY_FILE at it)"
            ) from None
        except OSError as exc:  # ELOOP: a symlink was planted there; EACCES: not ours
            raise MasterKeyError(
                f"master key {self._path} cannot be opened safely ({exc.strerror})"
            ) from None
        try:
            info = os.fstat(fd)
            self._check_mode(info)
            with os.fdopen(fd, "rb", closefd=False) as handle:
                raw = handle.read(1024)
        finally:
            os.close(fd)
        return self._decode(raw)

    def _check_mode(self, info: os.stat_result) -> None:
        if not stat.S_ISREG(info.st_mode):
            raise MasterKeyError(f"master key {self._path} is not a regular file")
        if info.st_mode & 0o077:
            raise MasterKeyError(
                f"master key {self._path} is accessible by group/others "
                f"(mode {stat.S_IMODE(info.st_mode):o}); run `chmod 600 {self._path}`"
            )

    def create(self) -> None:
        """Write the key atomically: a crash leaves either no key file or a complete one.

        The key goes to a temporary file in the same directory (fsync), is published with
        ``link()`` - which, unlike ``rename()``, fails if the target exists, so a key is never
        overwritten - and the directory is fsynced so the new name survives a power loss.
        """
        directory = self._path.parent
        directory.mkdir(mode=DIR_MODE, parents=True, exist_ok=True)
        encoded = base64.urlsafe_b64encode(secrets.token_bytes(KEY_BYTES)) + b"\n"
        tmp = directory / f".{self._path.name}.{secrets.token_hex(8)}.tmp"
        # O_EXCL + O_NOFOLLOW: never reuse or write through something planted at the temp name.
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, FILE_MODE)
        try:
            try:
                os.fchmod(fd, FILE_MODE)  # the umask may only have removed bits, but be explicit
                os.write(fd, encoded)
                os.fsync(fd)
            finally:
                os.close(fd)
            try:
                os.link(tmp, self._path, follow_symlinks=False)
            except FileExistsError as exc:
                raise MasterKeyError(
                    f"master key {self._path} already exists (or a link was planted there); "
                    "refusing to overwrite it"
                ) from exc
        finally:
            tmp.unlink(missing_ok=True)
        _fsync_dir(directory)

    def _decode(self, raw: bytes) -> bytes:
        try:
            key = base64.urlsafe_b64decode(raw.strip())
        except (binascii.Error, ValueError):
            key = b""
        if len(key) != KEY_BYTES:
            raise MasterKeyError(f"master key {self._path} is corrupt (expected {KEY_BYTES} bytes)")
        return key


def _fsync_dir(directory: Path) -> None:
    fd = os.open(directory, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)
