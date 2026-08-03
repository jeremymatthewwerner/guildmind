"""Small fail-closed filesystem namespace primitives for storage internals.

The helpers in this module operate on already-open directory descriptors.  They do
not resolve paths, do not replace an existing destination, and deliberately have no
portable overwrite-capable fallback.
"""

from __future__ import annotations

import ctypes
import errno
import os
import sys

_RENAME_NOREPLACE = 1
_RENAME_EXCL = 0x00000004
_C_INT_MAX = (1 << (ctypes.sizeof(ctypes.c_int) * 8 - 1)) - 1


def rename_noreplace_at(
    source_directory_fd: int,
    source_name: str,
    destination_directory_fd: int,
    destination_name: str,
) -> None:
    """Atomically move one directory entry without replacing a destination.

    Both names must be single path components. They are encoded with
    :func:`os.fsencode`, preserving surrogate-escaped filesystem bytes. Darwin uses
    ``renameatx_np(RENAME_EXCL)`` and Linux uses
    ``renameat2(RENAME_NOREPLACE)``. Kernel errors, including ``EEXIST``, ``EXDEV``,
    and ``ENOSYS``, remain ordinary :class:`OSError` instances with their original
    ``errno`` values so the higher-level protocol can classify them.
    """

    source_descriptor = _directory_descriptor(source_directory_fd, label="source")
    destination_descriptor = _directory_descriptor(
        destination_directory_fd,
        label="destination",
    )
    source_bytes = _single_component(source_name, label="source")
    destination_bytes = _single_component(destination_name, label="destination")

    try:
        libc = ctypes.CDLL(None, use_errno=True)
    except OSError as error:
        raise OSError(errno.ENOSYS, "host libc is unavailable") from error

    if sys.platform == "darwin":
        try:
            rename = libc.renameatx_np
        except AttributeError as error:
            raise OSError(errno.ENOSYS, "libc renameatx_np is unavailable") from error
        rename.argtypes = (
            ctypes.c_int,
            ctypes.c_char_p,
            ctypes.c_int,
            ctypes.c_char_p,
            ctypes.c_uint,
        )
        rename.restype = ctypes.c_int
        arguments = (
            source_descriptor,
            source_bytes,
            destination_descriptor,
            destination_bytes,
            _RENAME_EXCL,
        )
    elif sys.platform.startswith("linux"):
        try:
            rename = libc.renameat2
        except AttributeError as error:
            raise OSError(errno.ENOSYS, "libc renameat2 is unavailable") from error
        rename.argtypes = (
            ctypes.c_int,
            ctypes.c_char_p,
            ctypes.c_int,
            ctypes.c_char_p,
            ctypes.c_uint,
        )
        rename.restype = ctypes.c_int
        arguments = (
            source_descriptor,
            source_bytes,
            destination_descriptor,
            destination_bytes,
            _RENAME_NOREPLACE,
        )
    else:
        raise OSError(
            errno.ENOSYS,
            f"descriptor-relative atomic no-replace rename is unsupported on {sys.platform}",
        )

    ctypes.set_errno(0)
    result = rename(*arguments)
    if result != 0:
        error_number = ctypes.get_errno() or errno.EIO
        raise OSError(error_number, os.strerror(error_number), destination_name)


def _directory_descriptor(value: int, *, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0 or value > _C_INT_MAX:
        raise ValueError(
            f"{label} directory descriptor must be an integer from 0 through {_C_INT_MAX}"
        )
    return value


def _single_component(value: str, *, label: str) -> bytes:
    if not isinstance(value, str):
        raise TypeError(f"{label} name must be a string")
    try:
        encoded = os.fsencode(value)
    except UnicodeEncodeError as error:
        raise ValueError(
            f"{label} name cannot be represented by the filesystem encoding"
        ) from error
    if not encoded or encoded in {b".", b".."} or b"/" in encoded or b"\x00" in encoded:
        raise ValueError(f"{label} name must be one non-special path component")
    return encoded
