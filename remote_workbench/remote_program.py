"""Build shell-neutral commands for running remote programs with argv semantics."""

from __future__ import annotations

from collections.abc import Iterable, Sequence

from remote_workbench.remote_shell import python_command


def program_command(
    argv: Sequence[str],
    max_lines: int | None = None,
    path_indices: Iterable[int] = (),
) -> str:
    """Return a command that executes *argv* remotely without shell parsing.

    Selected path arguments are expanded using the remote account's HOME and
    prefixed with ``./`` when a relative path begins with a character that
    command-line tools may otherwise interpret specially. When ``max_lines``
    is set, stdout is copied through the requested newline count while the
    child's remaining output is drained so its real exit status is retained.
    """
    if isinstance(argv, (str, bytes)) or not isinstance(argv, Sequence) or not argv:
        raise ValueError("argv must be a non-empty sequence of strings")
    if any(not isinstance(value, str) or "\x00" in value for value in argv):
        raise ValueError("argv entries must be strings without NUL bytes")
    if max_lines is not None and (
        isinstance(max_lines, bool) or not isinstance(max_lines, int) or max_lines < 0
    ):
        raise ValueError("max_lines must be a non-negative integer or None")

    indices = tuple(path_indices)
    if any(
        isinstance(index, bool) or not isinstance(index, int) or index < 0 or index >= len(argv)
        for index in indices
    ):
        raise ValueError("path_indices must refer to entries in argv")

    source = f"""import errno
import os
import subprocess
import sys

argv = {list(argv)!r}
for index in {indices!r}:
    path = os.path.expanduser(argv[index])
    if not os.path.isabs(path) and path.startswith(("-", "!", "(")):
        path = "./" + path
    argv[index] = path

def launch_error(error):
    print(str(error), file=sys.stderr)
    raise SystemExit(127 if error.errno == errno.ENOENT else 126)

max_lines = {max_lines!r}
if max_lines is None:
    try:
        os.execvp(argv[0], argv)
    except OSError as error:
        launch_error(error)

try:
    process = subprocess.Popen(argv, stdout=subprocess.PIPE)
except OSError as error:
    launch_error(error)

remaining = max_lines
copy_output = remaining > 0
while True:
    chunk = process.stdout.read1(65536)
    if not chunk:
        break
    if not copy_output:
        continue

    end = len(chunk)
    offset = 0
    while remaining:
        newline = chunk.find(b"\\n", offset)
        if newline < 0:
            break
        remaining -= 1
        offset = newline + 1
        if remaining == 0:
            end = offset
    try:
        sys.stdout.buffer.write(chunk[:end])
    except OSError:
        # Keep consuming the child pipe even if the SSH output channel closed.
        copy_output = False
    if remaining == 0:
        copy_output = False

process.stdout.close()
status = process.wait()
raise SystemExit(status if status >= 0 else 128 - status)
"""
    return python_command(source)
