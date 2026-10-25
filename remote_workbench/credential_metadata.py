"""Inspect only metadata for an optional local password-file path.

This helper never reads file contents, asks for file size, or computes a hash.
It checks the path without following symlinks and immediately closes the
read-only descriptor. A passing result describes ordinary file permission
checks only; it does not isolate the file from other processes running under
the same UID or claim that the credential is safe to use in every context.
"""
from __future__ import annotations

import errno
import os
import stat
from typing import Any


_SAME_UID_NOTE = (
    "Owner-only permissions do not isolate this file from other processes "
    "running under the same UID."
)


def inspect_password_path(path: os.PathLike[str] | str | None) -> dict[str, Any]:
    """Return bounded path metadata without reading the referenced file.

    ``path`` is optional so callers can report an unset credential path
    without touching the filesystem. The returned mapping deliberately omits
    the path itself, file contents, size and hashes.
    """
    current_uid = os.getuid() if hasattr(os, "getuid") else None
    result: dict[str, Any] = {
        "configured": path is not None and os.fspath(path) != "",
        "exists": False,
        "is_symlink": None,
        "is_regular_file": None,
        "owner_uid": None,
        "current_uid": current_uid,
        "mode": None,
        "owner_only": False,
        "readable": False,
        "nofollow_open": "not_configured",
        "file_permission_checks_passed": False,
        "same_uid_isolation": False,
        "note": _SAME_UID_NOTE,
    }
    if not result["configured"]:
        return result

    path_string = os.fspath(path)
    try:
        metadata = os.lstat(path_string)
    except FileNotFoundError:
        result["lstat_status"] = "missing"
    except OSError as exc:
        result["lstat_status"] = errno.errorcode.get(exc.errno, type(exc).__name__)
    else:
        mode = stat.S_IMODE(metadata.st_mode)
        is_symlink = stat.S_ISLNK(metadata.st_mode)
        is_regular = stat.S_ISREG(metadata.st_mode)
        result.update(
            exists=True,
            is_symlink=is_symlink,
            is_regular_file=is_regular,
            owner_uid=metadata.st_uid,
            mode=f"{mode:04o}",
            owner_only=(is_regular and not is_symlink and mode & 0o077 == 0 and
                        mode & stat.S_IRUSR != 0),
            lstat_status="ok",
        )

    # access() is a metadata/permission query. It may follow a symlink, which
    # is useful to report separately; symlink targets still fail the checks.
    result["readable"] = os.access(path_string, os.R_OK)

    nofollow = getattr(os, "O_NOFOLLOW", None)
    if nofollow is None:
        result["nofollow_open"] = "unsupported"
    else:
        flags = os.O_RDONLY | nofollow | getattr(os, "O_NONBLOCK", 0) | getattr(os, "O_CLOEXEC", 0)
        try:
            descriptor = os.open(path_string, flags)
        except OSError as exc:
            result["nofollow_open"] = errno.errorcode.get(exc.errno, type(exc).__name__)
        else:
            try:
                result["nofollow_open"] = "opened"
            finally:
                os.close(descriptor)

    result["file_permission_checks_passed"] = bool(
        result["exists"]
        and result["is_regular_file"]
        and not result["is_symlink"]
        and current_uid is not None
        and result["owner_uid"] == current_uid
        and result["owner_only"]
        and result["readable"]
        and result["nofollow_open"] == "opened"
    )
    return result
