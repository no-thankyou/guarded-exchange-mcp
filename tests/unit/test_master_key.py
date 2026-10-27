import base64
import os
import stat
from pathlib import Path

import pytest

from guarded_exchange.application.ports import MasterKeyStore
from guarded_exchange.infrastructure.master_key import FileMasterKeyStore, MasterKeyError

pytestmark = pytest.mark.req("NFR-1")


def test_create_writes_32_random_bytes_with_mode_0600_in_a_private_directory(tmp_path: Path) -> None:
    path = tmp_path / "conf" / "master.key"
    store: MasterKeyStore = FileMasterKeyStore(path)

    assert store.exists() is False
    store.create()

    assert store.exists() is True
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert stat.S_IMODE(path.parent.stat().st_mode) == 0o700
    assert len(base64.urlsafe_b64decode(path.read_bytes().strip())) == 32


def test_two_keys_differ(tmp_path: Path) -> None:
    FileMasterKeyStore(tmp_path / "a.key").create()
    FileMasterKeyStore(tmp_path / "b.key").create()

    assert (tmp_path / "a.key").read_bytes() != (tmp_path / "b.key").read_bytes()


def test_create_never_overwrites(tmp_path: Path) -> None:
    store = FileMasterKeyStore(tmp_path / "master.key")
    store.create()
    before = (tmp_path / "master.key").read_bytes()

    with pytest.raises(FileExistsError):
        store.create()

    assert (tmp_path / "master.key").read_bytes() == before


def test_create_does_not_follow_a_planted_symlink(tmp_path: Path) -> None:
    target = tmp_path / "elsewhere"
    link = tmp_path / "master.key"
    link.symlink_to(target)

    with pytest.raises(FileExistsError):
        FileMasterKeyStore(link).create()
    assert not target.exists()


def test_key_readable_by_others_is_refused(tmp_path: Path) -> None:
    path = tmp_path / "master.key"
    FileMasterKeyStore(path).create()
    os.chmod(path, 0o644)

    with pytest.raises(MasterKeyError, match="chmod 600"):
        FileMasterKeyStore(path).exists()


def test_symlink_key_is_refused(tmp_path: Path) -> None:
    real = tmp_path / "real.key"
    FileMasterKeyStore(real).create()
    link = tmp_path / "master.key"
    link.symlink_to(real)

    with pytest.raises(MasterKeyError, match="not a regular file"):
        FileMasterKeyStore(link).exists()


@pytest.mark.parametrize("content", [b"", b"not base64 !!!", base64.urlsafe_b64encode(b"short")])
def test_corrupt_key_is_refused(tmp_path: Path, content: bytes) -> None:
    path = tmp_path / "master.key"
    path.write_bytes(content)
    path.chmod(0o600)

    with pytest.raises(MasterKeyError, match="corrupt"):
        FileMasterKeyStore(path).exists()
