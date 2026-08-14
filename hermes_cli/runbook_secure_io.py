"""Descriptor-anchored I/O for privileged runbook activation.

Activation touches files which can be supplied or swapped by a local caller.
This module keeps path traversal below one application-selected anchor and
never validates a pathname then re-opens it by name.
"""

from __future__ import annotations

import contextlib
import errno
import os
import secrets
import stat
from dataclasses import dataclass
from pathlib import Path
from typing import Iterator


@dataclass(frozen=True)
class SecureDir:
    fd: int
    path: Path

    def close(self) -> None:
        os.close(self.fd)


class SecurePathError(PermissionError):
    """A privileged activation path failed its descriptor-level policy."""


def _check_owner_mode(metadata: os.stat_result, *, directory: bool, owner_uid: int) -> None:
    expected_type = stat.S_ISDIR if directory else stat.S_ISREG
    if not expected_type(metadata.st_mode):
        raise SecurePathError("trusted activation path has an unsafe file type")
    if metadata.st_uid != owner_uid:
        raise SecurePathError("trusted activation path has an unexpected owner")
    if metadata.st_mode & 0o022:
        raise SecurePathError("trusted activation path is group/world writable")


def _open_directory(
    fd: int, name: str, *, owner_uid: int, create: bool, verify: bool = True
) -> int:
    if not name or name in {".", ".."} or "/" in name:
        raise SecurePathError("invalid activation path component")
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
    try:
        child_fd = os.open(name, flags, dir_fd=fd)
    except FileNotFoundError:
        if not create:
            raise SecurePathError("trusted activation directory is unavailable") from None
        try:
            os.mkdir(name, 0o700, dir_fd=fd)
        except FileExistsError:
            pass
        try:
            child_fd = os.open(name, flags, dir_fd=fd)
        except OSError as exc:
            raise SecurePathError("trusted activation directory is unavailable or unsafe") from exc
    except OSError as exc:
        raise SecurePathError("trusted activation directory is unavailable or unsafe") from exc
    try:
        if verify:
            _check_owner_mode(os.fstat(child_fd), directory=True, owner_uid=owner_uid)
    except BaseException:
        os.close(child_fd)
        raise
    return child_fd


@contextlib.contextmanager
def open_anchor(path: Path, *, owner_uid: int, create: bool = False) -> Iterator[SecureDir]:
    """Open an application-selected absolute root without following symlinks."""
    if not path.is_absolute():
        raise SecurePathError("trusted activation root must be absolute")
    root_fd = os.open("/", os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)
    current_fd = root_fd
    try:
        components = path.parts[1:]
        for index, component in enumerate(components):
            # The caller-selected anchor is checked below.  Ancestors such as
            # /tmp are allowed to be shared, but each is still opened by FD
            # with O_NOFOLLOW; any attacker-controlled descendant must pass
            # the final anchor's ownership/mode check before it is trusted.
            next_fd = _open_directory(
                current_fd, component, owner_uid=owner_uid, create=create and index == len(components) - 1,
                verify=index == len(components) - 1,
            )
            if current_fd != root_fd:
                os.close(current_fd)
            current_fd = next_fd
        if current_fd == root_fd:
            _check_owner_mode(os.fstat(root_fd), directory=True, owner_uid=owner_uid)
        yield SecureDir(current_fd, path)
    finally:
        if current_fd != root_fd:
            os.close(current_fd)
        os.close(root_fd)


def open_descendant(
    anchor: SecureDir,
    components: tuple[str, ...],
    *,
    owner_uid: int,
    create: bool = False,
) -> SecureDir:
    """Descriptor-walk direct descendants from an already checked anchor."""
    fd = os.dup(anchor.fd)
    try:
        for component in components:
            next_fd = _open_directory(fd, component, owner_uid=owner_uid, create=create)
            os.close(fd)
            fd = next_fd
        return SecureDir(fd, anchor.path.joinpath(*components))
    except BaseException:
        os.close(fd)
        raise


def read_file(directory: SecureDir, name: str, *, owner_uid: int) -> bytes:
    flags = os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC
    try:
        fd = os.open(name, flags, dir_fd=directory.fd)
    except OSError as exc:
        raise SecurePathError("trusted activation file is unavailable") from exc
    try:
        _check_owner_mode(os.fstat(fd), directory=False, owner_uid=owner_uid)
        chunks: list[bytes] = []
        while True:
            chunk = os.read(fd, 1024 * 1024)
            if not chunk:
                return b"".join(chunks)
            chunks.append(chunk)
    finally:
        os.close(fd)


def read_optional_file(directory: SecureDir, name: str, *, owner_uid: int) -> bytes | None:
    try:
        return read_file(directory, name, owner_uid=owner_uid)
    except SecurePathError as exc:
        try:
            os.stat(name, dir_fd=directory.fd, follow_symlinks=False)
        except FileNotFoundError:
            return None
        except OSError:
            pass
        raise exc


def _safe_existing_leaf(directory: SecureDir, name: str, *, owner_uid: int) -> None:
    try:
        metadata = os.stat(name, dir_fd=directory.fd, follow_symlinks=False)
    except FileNotFoundError:
        return
    _check_owner_mode(metadata, directory=False, owner_uid=owner_uid)


def replace_file(
    directory: SecureDir,
    name: str,
    value: bytes,
    *,
    owner_uid: int,
    mode: int = 0o600,
) -> None:
    """Atomically replace a checked leaf using only descriptor-relative syscalls."""
    if not name or "/" in name or name in {".", ".."}:
        raise SecurePathError("invalid activation filename")
    _safe_existing_leaf(directory, name, owner_uid=owner_uid)
    temp_name = f".{name}.{secrets.token_hex(12)}.tmp"
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC
    fd = os.open(temp_name, flags, mode, dir_fd=directory.fd)
    try:
        metadata = os.fstat(fd)
        _check_owner_mode(metadata, directory=False, owner_uid=owner_uid)
        view = memoryview(value)
        written = 0
        while written < len(view):
            progress = os.write(fd, view[written:])
            if not isinstance(progress, int) or progress <= 0:
                raise OSError("short write made no progress")
            written += progress
        os.fsync(fd)
        os.close(fd)
        fd = -1
        os.replace(temp_name, name, src_dir_fd=directory.fd, dst_dir_fd=directory.fd)
        os.fsync(directory.fd)
    except BaseException:
        if fd >= 0:
            os.close(fd)
        try:
            os.unlink(temp_name, dir_fd=directory.fd)
        except OSError as exc:
            if exc.errno != errno.ENOENT:
                raise
        raise


def unlink_optional(directory: SecureDir, name: str, *, owner_uid: int) -> None:
    _safe_existing_leaf(directory, name, owner_uid=owner_uid)
    try:
        os.unlink(name, dir_fd=directory.fd)
    except FileNotFoundError:
        return
    os.fsync(directory.fd)


@contextlib.contextmanager
def exclusive_lock(directory: SecureDir, name: str, *, owner_uid: int) -> Iterator[None]:
    """Open and hold a descriptor-checked advisory lock for an activation."""
    import fcntl

    flags = os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_CLOEXEC
    fd = os.open(name, flags, 0o600, dir_fd=directory.fd)
    try:
        _check_owner_mode(os.fstat(fd), directory=False, owner_uid=owner_uid)
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        try:
            fcntl.flock(fd, fcntl.LOCK_UN)
        finally:
            os.close(fd)
