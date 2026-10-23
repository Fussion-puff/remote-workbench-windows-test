"""File operation tools."""

from __future__ import annotations

import os
import shlex
import shutil
import tempfile
from remote_workbench.shell_paths import shell_path
from remote_workbench.remote_client import edit as remote_edit

from remote_workbench.server import mcp, require_connection


@mcp.tool()
async def ssh_read_file(path: str, offset: int = 0, limit: int = 160, host: str | None = None, max_bytes: int = 8192) -> dict:
    """Read a bounded range with line numbers and a hash for guarded edits. offset is 0-based."""
    if offset < 0 or not 1 <= limit <= 2000:
        raise ValueError("offset >= 0 and 1 <= limit <= 2000 required")
    executor = await require_connection(host)
    return await remote_edit(executor, {"op": "read_lines", "path": path, "offset": offset, "limit": limit, "budget": max_bytes}, max_bytes=max_bytes)


@mcp.tool()
async def ssh_write_file(path: str, content: str, create_dirs: bool = False, host: str | None = None, expected_sha256: str | None = None) -> dict:
    """Write UTF-8 content atomically on the remote host, preserving existing mode/owner."""
    executor = await require_connection(host)
    return await remote_edit(executor, {"op": "write", "path": path, "content": content, "create_dirs": create_dirs, "expected_sha256": expected_sha256})


@mcp.tool()
async def ssh_list_dir(
    path: str = ".",
    pattern: str = "*",
    recursive: bool = False,
    max_entries: int = 500,
    host: str | None = None,
) -> dict:
    """List directory contents on the remote server.

    Args:
        path: Directory path to list. Defaults to current working directory.
        pattern: Glob pattern to filter entries (e.g., "*.py", "Makefile").
        recursive: If true, list recursively.
        max_entries: Maximum entries to return (prevents overwhelming output).
        host: SSH host alias from config. Uses default if omitted.
    """
    executor = await require_connection(host)
    safe_path = shell_path(path)
    safe_pattern = shlex.quote(pattern)
    max_entries = int(max_entries)
    if not 1 <= max_entries <= 2000:
        raise ValueError("max_entries must be between 1 and 2000")

    if recursive:
        # Use -exec ls instead of -printf for BSD/macOS compatibility
        cmd = (
            f"find {safe_path} -name {safe_pattern} "
            f"-maxdepth 10 -exec ls -ld {{}} + 2>/dev/null | "
            f"head -n {max_entries + 1}"
        )
    else:
        # Use find instead of ls glob to avoid shell expansion of pattern
        if pattern == "*":
            cmd = f"ls -la {safe_path} 2>/dev/null | head -n {max_entries + 2}"
        else:
            cmd = f"find {safe_path} -maxdepth 1 -name {safe_pattern} -exec ls -ld {{}} + 2>/dev/null | head -n {max_entries + 1}"

    result = await executor.run("set -o pipefail\n" + cmd, cwd=None)

    if result.exit_code not in (0, 141):
        return {"error": "list_failed", "message": result.stderr.strip() or "Directory not found or empty"}

    lines = result.stdout.strip().split("\n") if result.stdout.strip() else []

    if recursive:
        # ls -ld output: just pass through the lines
        entries = []
        for line in lines[:max_entries]:
            if line.startswith("total "):
                continue
            entries.append(line)
        truncated = len(lines) > max_entries
    else:
        # ls -la output: skip the "total N" line
        entries = []
        for line in lines:
            if line.startswith("total "):
                continue
            entries.append(line)
        truncated = len(entries) > max_entries
        entries = entries[:max_entries]

    return {
        "entries": entries,
        "count": len(entries),
        "truncated": truncated or result.truncated,
        "output_id": result.output_id,
        "path": path,
    }


@mcp.tool()
async def ssh_file_info(
    path: str,
    host: str | None = None,
) -> dict:
    """Get detailed information about a file or directory on the remote server.

    Args:
        path: Path to inspect.
        host: SSH host alias from config. Uses default if omitted.
    """
    executor = await require_connection(host)
    safe_path = shell_path(path)

    # Try GNU stat first, fall back to BSD stat
    result = await executor.run(
        f"stat --format='%F|%s|%U|%G|%A|%y' {safe_path} 2>/dev/null || "
        f"stat -f '%HT|%z|%Su|%Sg|%Sp|%Sm' {safe_path}",
        cwd=None,
    )

    if result.exit_code != 0:
        return {"error": "not_found", "message": result.stderr.strip()}

    parts = result.stdout.strip().split("|")
    if len(parts) >= 6:
        return {
            "exists": True,
            "type": parts[0],
            "size": int(parts[1]) if parts[1].isdigit() else parts[1],
            "owner": parts[2],
            "group": parts[3],
            "permissions": parts[4],
            "modified": parts[5],
            "path": path,
        }

    return {
        "exists": True,
        "raw": result.stdout.strip(),
        "path": path,
    }


@mcp.tool()
async def ssh_patch_file(path: str, old: str | None = None, new: str | None = None, host: str | None = None,
                         edits: list[dict[str, str]] | None = None, expected_sha256: str | None = None,
                         check_only: bool = False, max_bytes: int = 8192) -> dict:
    """Apply unique old/new replacements remotely, sequentially, all-or-none per file.

    Supply either old/new or edits=[{old,new},...]. Optional expected_sha256 rejects
    stale content. Returns this operation's diff; full diff is saved by output_id.
    """
    if edits is not None and (old is not None or new is not None):
        raise ValueError("Use old/new OR edits")
    if edits is None:
        if old is None or new is None:
            raise ValueError("Both old and new are required")
        edits = [{"old": old, "new": new}]
    if not edits or any(set(item) != {"old", "new"} or not item["old"] or not all(isinstance(v, str) for v in item.values()) for item in edits):
        raise ValueError("edits must contain nonempty old strings and string new values")
    executor = await require_connection(host)
    return await remote_edit(executor, {"op": "replace", "path": path, "edits": edits,
        "expected_sha256": expected_sha256, "check_only": check_only}, max_bytes=max_bytes)


@mcp.tool()
async def ssh_mkdir(
    path: str,
    parents: bool = True,
    host: str | None = None,
) -> dict:
    """Create a directory on the remote server.

    Args:
        path: Directory path to create.
        parents: If true, create parent directories as needed (mkdir -p).
        host: SSH host alias from config. Uses default if omitted.
    """
    executor = await require_connection(host)
    safe_path = shell_path(path)
    flag = "-p " if parents else ""
    result = await executor.run(f"mkdir {flag}{safe_path}", cwd=None)

    if result.exit_code != 0:
        return {"error": "mkdir_failed", "message": result.stderr.strip()}
    return {"success": True, "path": path}


@mcp.tool()
async def ssh_move(
    source: str,
    dest: str,
    host: str | None = None,
) -> dict:
    """Move or rename a file/directory on the remote server.

    Args:
        source: Current path.
        dest: New path.
        host: SSH host alias from config. Uses default if omitted.
    """
    executor = await require_connection(host)
    result = await executor.run(
        f"mv {shell_path(source)} {shell_path(dest)}", cwd=None
    )

    if result.exit_code != 0:
        return {"error": "move_failed", "message": result.stderr.strip()}
    return {"success": True, "source": source, "dest": dest}


@mcp.tool()
async def ssh_delete(
    path: str,
    recursive: bool = False,
    host: str | None = None,
) -> dict:
    """Delete a file or directory on the remote server.

    Args:
        path: Path to delete.
        recursive: If true, delete directories recursively (rm -r).
        host: SSH host alias from config. Uses default if omitted.
    """
    executor = await require_connection(host)
    flag = "-r " if recursive else ""
    result = await executor.run(f"rm {flag}{shell_path(path)}", cwd=None)

    if result.exit_code != 0:
        return {"error": "delete_failed", "message": result.stderr.strip()}
    return {"success": True, "path": path}


@mcp.tool()
async def ssh_download(
    remote_path: str,
    local_dir: str = "",
    host: str | None = None,
) -> dict:
    """Download raw file bytes through the full spool, preserving binary content.

    Args:
        remote_path: Path to the file on the remote server.
        local_dir: Local directory to save to. Defaults to ~/Downloads.
        host: SSH host alias from config. Uses default if omitted.
    """
    executor = await require_connection(host)
    safe_remote = shell_path(remote_path)

    if not local_dir:
        local_dir = os.path.expanduser("~/Downloads")

    # Resolve to absolute path and ensure it's under home directory
    local_dir = os.path.realpath(os.path.expanduser(local_dir))
    home = os.path.realpath(os.path.expanduser("~"))
    if os.path.commonpath([local_dir, home]) != home:
        return {"error": "invalid_path", "message": f"local_dir must be under your home directory ({home})."}

    os.makedirs(local_dir, exist_ok=True)
    filename = os.path.basename(remote_path)
    if not filename or filename.startswith("."):
        return {"error": "invalid_filename", "message": "Remote path must have a non-hidden filename."}
    local_path = os.path.join(local_dir, filename)

    # Read via SSH and write locally
    result = await executor.run(f"cat {safe_remote}", cwd=None, timeout=executor.config.file_timeout)

    if result.exit_code != 0:
        return {"error": "download_failed", "message": result.stderr.strip()}

    # Stream raw bytes into a private temporary file, then replace the local
    # target. Never follow a pre-existing destination symlink or save a preview.
    from remote_workbench import audit
    spool = audit.directory(result.output_id) / "stdout"
    fd, temporary = tempfile.mkstemp(prefix=".remote-workbench-download-", dir=local_dir)
    try:
        with os.fdopen(fd, "wb") as output, spool.open("rb") as source:
            shutil.copyfileobj(source, output)
        os.replace(temporary, local_path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)

    return {
        "success": True,
        "local_path": local_path,
        "size": spool.stat().st_size,
    }


@mcp.tool()
async def ssh_append_file(path: str, content: str, host: str | None = None, expected_sha256: str | None = None) -> dict:
    """Append remotely with a cooperative lock and atomic replacement."""
    executor = await require_connection(host)
    return await remote_edit(executor, {"op": "append", "path": path, "content": content, "expected_sha256": expected_sha256})


@mcp.tool()
async def ssh_insert_lines(path: str, after_line: int, content: str, host: str | None = None,
                           expected_sha256: str | None = None) -> dict:
    """Insert after a 1-based line, or 0 to prepend. Use expected_sha256 to guard line positions."""
    if after_line < 0:
        raise ValueError("after_line must be nonnegative")
    executor = await require_connection(host)
    return await remote_edit(executor, {"op": "lines", "path": path, "start": after_line, "end": after_line,
        "content": content, "expected_sha256": expected_sha256})


@mcp.tool()
async def ssh_replace_lines(path: str, start_line: int, end_line: int, content: str = "", host: str | None = None,
                            expected_sha256: str | None = None, expected_old: str | None = None) -> dict:
    """Replace inclusive 1-based lines, optionally checking the hash and exact old range."""
    if start_line < 1 or end_line < start_line:
        raise ValueError("Invalid line range")
    payload = {"op": "lines", "path": path, "start": start_line-1, "end": end_line,
               "content": content, "expected_sha256": expected_sha256}
    if expected_old is not None:
        payload["expected_old"] = expected_old
    executor = await require_connection(host)
    return await remote_edit(executor, payload)
