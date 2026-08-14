from __future__ import annotations

import os
from pathlib import Path

import pytest

from hermes_cli import runbook_secure_io as secure_io


@pytest.fixture
def secure_directory(tmp_path: Path):
    directory = tmp_path / "activation"
    directory.mkdir(mode=0o700)
    with secure_io.open_anchor(directory, owner_uid=os.geteuid()) as anchor:
        yield directory, anchor


@pytest.mark.parametrize("progress", [0, -1, True, False, "1", 4])
def test_replace_file_rejects_invalid_write_progress_without_replacing_leaf(
    secure_directory, monkeypatch, progress
):
    directory, anchor = secure_directory
    target = directory / "artifact"
    target.write_bytes(b"previous")
    target.chmod(0o600)
    monkeypatch.setattr(secure_io.os, "write", lambda _fd, _value: progress)

    with pytest.raises(OSError):
        secure_io.replace_file(anchor, "artifact", b"new", owner_uid=os.geteuid())

    assert target.read_bytes() == b"previous"
    assert not list(directory.glob(".artifact.*.tmp"))


def test_replace_file_write_error_preserves_existing_leaf_and_removes_temporary_file(
    secure_directory, monkeypatch
):
    directory, anchor = secure_directory
    target = directory / "artifact"
    target.write_bytes(b"previous")
    target.chmod(0o600)

    def fail_write(_fd, _value):
        raise OSError("injected write failure")

    monkeypatch.setattr(secure_io.os, "write", fail_write)
    with pytest.raises(OSError, match="injected write failure"):
        secure_io.replace_file(anchor, "artifact", b"new", owner_uid=os.geteuid())

    assert target.read_bytes() == b"previous"
    assert not list(directory.glob(".artifact.*.tmp"))


def test_replace_file_close_after_release_preserves_leaf_and_removes_temporary_file(
    secure_directory, monkeypatch
):
    directory, anchor = secure_directory
    target = directory / "artifact"
    target.write_bytes(b"previous")
    target.chmod(0o600)
    original_close = secure_io.os.close
    released = False

    def close_after_release(fd: int) -> None:
        nonlocal released
        original_close(fd)
        if not released:
            released = True
            raise OSError("injected close-after-release failure")

    monkeypatch.setattr(secure_io.os, "close", close_after_release)
    with pytest.raises(OSError, match="close-after-release"):
        secure_io.replace_file(anchor, "artifact", b"new", owner_uid=os.geteuid())

    assert released is True
    assert target.read_bytes() == b"previous"
    assert not list(directory.glob(".artifact.*.tmp"))