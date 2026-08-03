from __future__ import annotations

import ctypes
import errno
import os
import stat
import sys
from pathlib import Path

import pytest

from guildmind.storage._fsops import rename_noreplace_at

_SUPPORTED = sys.platform == "darwin" or sys.platform.startswith("linux")
_DIRECTORY_FLAGS = (
    os.O_RDONLY
    | getattr(os, "O_CLOEXEC", 0)
    | getattr(os, "O_DIRECTORY", 0)
    | getattr(os, "O_NOFOLLOW", 0)
)


def _open_directory(path: Path) -> int:
    return os.open(path, _DIRECTORY_FLAGS)


@pytest.mark.skipif(not _SUPPORTED, reason="requires Darwin or Linux no-replace rename")
def test_descriptor_relative_rename_never_overwrites_existing_destination(
    tmp_path: Path,
) -> None:
    source_directory = tmp_path / "source"
    destination_directory = tmp_path / "destination"
    source_directory.mkdir()
    destination_directory.mkdir()
    source = source_directory / "candidate"
    destination = destination_directory / "preserved"
    source.write_bytes(b"candidate bytes")
    destination.write_bytes(b"existing bytes")
    source_descriptor = _open_directory(source_directory)
    destination_descriptor = _open_directory(destination_directory)
    try:
        with pytest.raises(OSError) as raised:
            rename_noreplace_at(
                source_descriptor,
                source.name,
                destination_descriptor,
                destination.name,
            )
    finally:
        os.close(destination_descriptor)
        os.close(source_descriptor)

    assert raised.value.errno == errno.EEXIST
    assert source.read_bytes() == b"candidate bytes"
    assert destination.read_bytes() == b"existing bytes"


@pytest.mark.skipif(not _SUPPORTED, reason="requires Darwin or Linux no-replace rename")
def test_descriptor_relative_rename_moves_one_entry_across_directories(
    tmp_path: Path,
) -> None:
    source_directory = tmp_path / "source"
    destination_directory = tmp_path / "destination"
    source_directory.mkdir()
    destination_directory.mkdir()
    source = source_directory / "candidate"
    destination = destination_directory / "quarantined"
    source.write_bytes(b"preserved evidence")
    source_identity = (source.stat().st_dev, source.stat().st_ino)
    source_descriptor = _open_directory(source_directory)
    destination_descriptor = _open_directory(destination_directory)
    try:
        rename_noreplace_at(
            source_descriptor,
            source.name,
            destination_descriptor,
            destination.name,
        )
    finally:
        os.close(destination_descriptor)
        os.close(source_descriptor)

    assert not source.exists()
    assert destination.read_bytes() == b"preserved evidence"
    assert (destination.stat().st_dev, destination.stat().st_ino) == source_identity
    assert stat.S_ISREG(destination.stat().st_mode)


@pytest.mark.skipif(not _SUPPORTED, reason="requires Darwin or Linux no-replace rename")
def test_descriptor_relative_rename_round_trips_surrogate_escaped_name_bytes(
    tmp_path: Path,
) -> None:
    directory = tmp_path / "directory"
    directory.mkdir()
    source_bytes = b"source-\xff"
    destination_bytes = b"destination-\xfe"
    descriptor = _open_directory(directory)
    try:
        try:
            source_descriptor = os.open(
                source_bytes,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL,
                0o600,
                dir_fd=descriptor,
            )
        except OSError as error:
            pytest.skip(f"filesystem rejected a surrogate-escaped entry name: {error}")
        os.write(source_descriptor, b"raw-name evidence")
        os.close(source_descriptor)

        rename_noreplace_at(
            descriptor,
            os.fsdecode(source_bytes),
            descriptor,
            os.fsdecode(destination_bytes),
        )

        with pytest.raises(FileNotFoundError):
            os.stat(source_bytes, dir_fd=descriptor, follow_symlinks=False)
        destination_metadata = os.stat(
            destination_bytes,
            dir_fd=descriptor,
            follow_symlinks=False,
        )
        assert stat.S_ISREG(destination_metadata.st_mode)
    finally:
        os.close(descriptor)


@pytest.mark.parametrize(
    ("source_name", "destination_name"),
    [
        ("", "destination"),
        (".", "destination"),
        ("..", "destination"),
        ("nested/source", "destination"),
        ("source", "nested/destination"),
        ("source\x00suffix", "destination"),
    ],
)
def test_descriptor_relative_rename_rejects_noncomponent_names_before_syscall(
    monkeypatch: pytest.MonkeyPatch,
    source_name: str,
    destination_name: str,
) -> None:
    def unexpected_libc(*_: object, **__: object) -> object:
        raise AssertionError("invalid names must fail before libc is loaded")

    monkeypatch.setattr(ctypes, "CDLL", unexpected_libc)

    with pytest.raises(ValueError, match="one non-special path component"):
        rename_noreplace_at(0, source_name, 1, destination_name)


@pytest.mark.parametrize("oversized_argument", ["source", "destination"])
def test_descriptor_relative_rename_rejects_oversized_descriptor_aliases(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    oversized_argument: str,
) -> None:
    directory = tmp_path / "directory"
    directory.mkdir()
    descriptor = _open_directory(directory)
    c_int_modulus = 1 << (ctypes.sizeof(ctypes.c_int) * 8)
    aliased_descriptor = descriptor + c_int_modulus

    def unexpected_libc(*_: object, **__: object) -> object:
        raise AssertionError("oversized descriptors must fail before libc is loaded")

    monkeypatch.setattr(ctypes, "CDLL", unexpected_libc)
    source_descriptor = aliased_descriptor if oversized_argument == "source" else descriptor
    destination_descriptor = (
        aliased_descriptor if oversized_argument == "destination" else descriptor
    )
    try:
        with pytest.raises(ValueError, match="integer from 0 through"):
            rename_noreplace_at(
                source_descriptor,
                "source",
                destination_descriptor,
                "destination",
            )
    finally:
        os.close(descriptor)


@pytest.mark.parametrize("error_number", [errno.EEXIST, errno.EXDEV, errno.ENOSYS])
def test_descriptor_relative_rename_preserves_kernel_errno(
    monkeypatch: pytest.MonkeyPatch,
    error_number: int,
) -> None:
    class FailingRename:
        argtypes: object = None
        restype: object = None

        def __call__(self, *_: object) -> int:
            ctypes.set_errno(error_number)
            return -1

    class FakeLibc:
        renameatx_np = FailingRename()

    monkeypatch.setattr(sys, "platform", "darwin")
    monkeypatch.setattr(ctypes, "CDLL", lambda *_args, **_kwargs: FakeLibc())

    with pytest.raises(OSError) as raised:
        rename_noreplace_at(0, "source", 1, "destination")

    assert raised.value.errno == error_number


def test_descriptor_relative_rename_has_no_unsupported_platform_fallback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(sys, "platform", "unsupported-test-platform")

    with pytest.raises(OSError) as raised:
        rename_noreplace_at(0, "source", 1, "destination")

    assert raised.value.errno == errno.ENOSYS
