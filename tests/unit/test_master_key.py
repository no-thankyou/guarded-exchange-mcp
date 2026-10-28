import base64
import os
import stat
from pathlib import Path

import pytest

from guarded_exchange.application.ports import MasterKeyStore
from guarded_exchange.infrastructure.master_key import FileMasterKeyStore, MasterKeyError


@pytest.mark.req("NFR-1")
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

    with pytest.raises(MasterKeyError, match="refusing to overwrite"):
        store.create()

    assert (tmp_path / "master.key").read_bytes() == before
    assert sorted(p.name for p in tmp_path.iterdir()) == ["master.key"]  # no temp file left


def test_create_does_not_follow_a_planted_symlink(tmp_path: Path) -> None:
    target = tmp_path / "elsewhere"
    link = tmp_path / "master.key"
    link.symlink_to(target)

    with pytest.raises(MasterKeyError):
        FileMasterKeyStore(link).create()
    assert not target.exists()
    assert link.is_symlink()


@pytest.mark.req("NFR-1")
def test_key_readable_by_others_is_refused(tmp_path: Path) -> None:
    path = tmp_path / "master.key"
    FileMasterKeyStore(path).create()
    os.chmod(path, 0o644)

    with pytest.raises(MasterKeyError, match="chmod 600"):
        FileMasterKeyStore(path).exists()


@pytest.mark.req("NFR-1")
def test_symlink_key_is_refused(tmp_path: Path) -> None:
    real = tmp_path / "real.key"
    FileMasterKeyStore(real).create()
    link = tmp_path / "master.key"
    link.symlink_to(real)

    with pytest.raises(MasterKeyError, match="not a regular file"):
        FileMasterKeyStore(link).exists()


@pytest.mark.req("NFR-1")
@pytest.mark.parametrize("content", [b"", b"not base64 !!!", base64.urlsafe_b64encode(b"short")])
def test_corrupt_key_is_refused(tmp_path: Path, content: bytes) -> None:
    path = tmp_path / "master.key"
    path.write_bytes(content)
    path.chmod(0o600)

    with pytest.raises(MasterKeyError, match="corrupt"):
        FileMasterKeyStore(path).exists()


def test_create_leaves_only_the_complete_key_and_no_temp_files(tmp_path: Path) -> None:
    path = tmp_path / "conf" / "master.key"

    FileMasterKeyStore(path).create()

    assert [p.name for p in path.parent.iterdir()] == ["master.key"]
    assert path.stat().st_nlink == 1  # the temporary name was removed after link()


def test_failed_write_leaves_no_key_and_no_temp_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    path = tmp_path / "master.key"

    def broken_fsync(fd: int) -> None:
        raise OSError("disk full")

    monkeypatch.setattr(os, "fsync", broken_fsync)

    with pytest.raises(OSError, match="disk full"):
        FileMasterKeyStore(path).create()

    assert list(tmp_path.iterdir()) == []


# --- load (ticket 007: serve and key commands read the key) --------------------------------


@pytest.mark.req("NFR-1")
def test_load_returns_the_32_key_bytes(tmp_path: Path) -> None:
    store = FileMasterKeyStore(tmp_path / "master.key")
    store.create()

    key = store.load()

    assert key == base64.urlsafe_b64decode((tmp_path / "master.key").read_bytes().strip())
    assert len(key) == 32


@pytest.mark.req("NFR-1")
def test_load_refuses_a_missing_open_linked_or_corrupt_key(tmp_path: Path) -> None:
    good = FileMasterKeyStore(tmp_path / "master.key")
    good.create()

    with pytest.raises(MasterKeyError, match="not found"):
        FileMasterKeyStore(tmp_path / "missing.key").load()

    link = tmp_path / "link.key"
    link.symlink_to(tmp_path / "master.key")
    with pytest.raises(MasterKeyError, match="cannot be opened safely"):
        FileMasterKeyStore(link).load()

    (tmp_path / "master.key").chmod(0o640)
    with pytest.raises(MasterKeyError, match="group/others"):
        good.load()

    corrupt = tmp_path / "corrupt.key"
    corrupt.write_bytes(b"not a key\n")
    corrupt.chmod(0o600)
    with pytest.raises(MasterKeyError, match="corrupt"):
        FileMasterKeyStore(corrupt).load()

    directory = tmp_path / "dir.key"
    directory.mkdir(mode=0o700)
    with pytest.raises(MasterKeyError, match="not a regular file"):
        FileMasterKeyStore(directory).load()


@pytest.mark.req("NFR-1")
def test_load_refuses_a_fifo_without_blocking(tmp_path: Path) -> None:
    fifo = tmp_path / "master.key"
    os.mkfifo(fifo, 0o600)  # nobody writes to it: a blocking open would hang the server

    with pytest.raises(MasterKeyError, match="not a regular file"):
        FileMasterKeyStore(fifo).load()
