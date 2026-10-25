"""Prepare a private, per-session snapshot of a remote workspace."""
from __future__ import annotations

import hashlib
import json
import re


_SESSION_ID = re.compile(r"^[a-f0-9]{32}$")


# Sent to the remote account's ordinary Python interpreter. It deliberately
# uses only the standard library and writes into a temporary sibling before
# atomically publishing the completed project directory.
_BOOTSTRAP = r'''import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import stat
import subprocess
import sys
import uuid

SESSION_RE = re.compile(r"^[a-f0-9]{32}$")
TOOL_OWNED = {".git", ".remote-workbench-state", ".remote-workbench-sessions"}
ENVIRONMENT_DIRS = {
    ".venv", "venv", "node_modules", "__pycache__", ".pytest_cache",
    ".mypy_cache", ".ruff_cache", ".tox", ".nox", ".cache",
}

def fail(message, stage="prepare"):
    raise RuntimeError(stage + ": " + str(message))

def run(argv, cwd=None, input_data=None):
    try:
        return subprocess.run(argv, cwd=cwd, input=input_data,
                              stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                              check=False)
    except OSError as exc:
        fail(exc, "run " + argv[0])

def checked(argv, cwd=None, input_data=None, stage=None):
    result = run(argv, cwd, input_data)
    if result.returncode:
        detail = result.stderr.decode("utf-8", "replace").strip()
        fail(detail or ("exit code " + str(result.returncode)),
             stage or "run " + argv[0])
    return result.stdout

def inside(path, root):
    try:
        return os.path.commonpath([os.path.abspath(path), os.path.abspath(root)]) == os.path.abspath(root)
    except ValueError:
        return False

def path_parts(relative):
    return relative.replace(os.sep, "/").split("/")

def tool_owned(relative):
    parts = path_parts(relative)
    return any(part in TOOL_OWNED or part.startswith(".remote-workbench-write-")
               or part.startswith(".remote-workbench-download-") for part in parts)

def environment_path(relative):
    return any(part in ENVIRONMENT_DIRS for part in path_parts(relative))

def excluded_from_directory_copy(relative):
    return tool_owned(relative) or environment_path(relative)

def excluded_from_gitlink(relative):
    return tool_owned(relative)

def atomic_json(path, value):
    temporary = str(path) + ".tmp-" + uuid.uuid4().hex
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
    fd = os.open(temporary, flags, 0o600)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as output:
            json.dump(value, output, ensure_ascii=False, separators=(",", ":"))
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.lexists(temporary):
            os.unlink(temporary)

def content_state(root, relative, project_root=None, source_root=None):
    path = os.path.join(root, relative)
    try:
        info = os.lstat(path)
    except FileNotFoundError:
        return {"kind": "missing"}
    if stat.S_ISLNK(info.st_mode):
        target = os.readlink(path)
        if os.path.isabs(target) and project_root and source_root:
            resolved = os.path.realpath(path)
            if inside(resolved, source_root):
                target = os.path.join(project_root, os.path.relpath(resolved, source_root))
        return {"kind": "symlink", "target": target}
    if not stat.S_ISREG(info.st_mode):
        return {"kind": "other"}
    digest = hashlib.sha256()
    fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    with os.fdopen(fd, "rb") as source_file:
        opened = os.fstat(source_file.fileno())
        if (opened.st_dev, opened.st_ino, opened.st_size, opened.st_mtime_ns) != (
                info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns):
            return {"kind": "unstable"}
        for block in iter(lambda: source_file.read(1024 * 1024), b""):
            digest.update(block)
        current = os.fstat(source_file.fileno())
    if (current.st_dev, current.st_ino, current.st_size, current.st_mtime_ns) != (
            opened.st_dev, opened.st_ino, opened.st_size, opened.st_mtime_ns):
        return {"kind": "unstable"}
    current = os.lstat(path)
    if (current.st_dev, current.st_ino, current.st_size, current.st_mtime_ns) != (
            info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns):
        return {"kind": "unstable"}
    return {"kind": "file", "sha256": digest.hexdigest(),
            "mode": stat.S_IMODE(info.st_mode) & 0o777,
            "special_mode": bool(info.st_mode & (stat.S_ISUID | stat.S_ISGID | stat.S_ISVTX))}

def directory_file_paths(root):
    result = []
    for current, dirs, files in os.walk(root, topdown=True, followlinks=False):
        relative_dir = os.path.relpath(current, root)
        if relative_dir == ".":
            relative_dir = ""
        kept_dirs = []
        for name in dirs:
            relative = os.path.join(relative_dir, name) if relative_dir else name
            path = os.path.join(current, name)
            if excluded_from_directory_copy(relative):
                continue
            if os.path.islink(path):
                result.append(relative.replace(os.sep, "/"))
                continue
            kept_dirs.append(name)
        dirs[:] = kept_dirs
        for name in files:
            relative = os.path.join(relative_dir, name) if relative_dir else name
            if not excluded_from_directory_copy(relative):
                result.append(relative.replace(os.sep, "/"))
    return sorted(set(result))

def baseline_manifest(source_root, project_root, snapshot_root, source_paths, session_id):
    files = {}
    for relative in source_paths:
        if excluded_from_directory_copy(relative):
            continue
        source_before = content_state(source_root, relative, project_root, source_root)
        project_state = content_state(snapshot_root, relative)
        source_after = content_state(source_root, relative, project_root, source_root)
        files[relative] = {"source": source_before, "project": project_state,
                           "stable_at_copy": source_before == source_after == project_state}
    value = {"version": 1, "status": "ready", "session_id": session_id,
             "source_root": source_root, "project_root": project_root,
             "files": files}
    value["baseline_id"] = hashlib.sha256(json.dumps(
        value, sort_keys=True, ensure_ascii=True, separators=(",", ":")
    ).encode("utf-8")).hexdigest()
    return value

def copy_file(source, target, source_root, target_root):
    os.makedirs(os.path.dirname(target), mode=0o700, exist_ok=True)
    if os.path.islink(source):
        link = os.readlink(source)
        if os.path.isabs(link):
            resolved = os.path.realpath(source)
            if inside(resolved, source_root):
                link = os.path.join(target_root, os.path.relpath(resolved, source_root))
        os.symlink(link, target)
    elif os.path.isfile(source):
        shutil.copy2(source, target)
    elif os.path.isdir(source):
        fail("encountered a directory where Git listed a file; submodules are not supported", "copy")

def copy_tree(source, target, source_root, target_root):
    os.makedirs(target, mode=0o700, exist_ok=True)
    for entry in os.scandir(source):
        if excluded_from_directory_copy(entry.name):
            continue
        src = entry.path
        dst = os.path.join(target, entry.name)
        if entry.is_symlink():
            copy_file(src, dst, source_root, target_root)
        elif entry.is_dir(follow_symlinks=False):
            copy_tree(src, dst, source_root, target_root)
        elif entry.is_file(follow_symlinks=False):
            copy_file(src, dst, source_root, target_root)
    os.chmod(target, 0o700)

def config_values(repo, key):
    result = run(["git", "-C", repo, "config", "--null", "--get-all", key])
    if result.returncode == 1:
        return []
    if result.returncode:
        fail(result.stderr.decode("utf-8", "replace"), "read Git remotes")
    return [os.fsdecode(item) for item in result.stdout.split(b"\0") if item]

def capture_remotes(repo):
    names = checked(["git", "-C", repo, "remote"], stage="read Git remotes").decode().splitlines()
    remotes = {}
    for name in names:
        prefix = "remote." + name + "."
        remotes[name] = {
            "url": config_values(repo, prefix + "url"),
            "pushurl": config_values(repo, prefix + "pushurl"),
            "fetch": config_values(repo, prefix + "fetch"),
        }
    return remotes

def set_config_values(repo, key, values):
    run(["git", "-C", repo, "config", "--unset-all", key])
    for value in values:
        checked(["git", "-C", repo, "config", "--add", key, value], stage="copy Git remote settings")

def restore_remotes(repo, remotes):
    current = checked(["git", "-C", repo, "remote"], stage="read cloned remotes").decode().splitlines()
    for name in current:
        if name not in remotes:
            checked(["git", "-C", repo, "remote", "remove", name], stage="remove local clone source")
    for name, values in remotes.items():
        if name not in current:
            first = values["url"][0] if values["url"] else ""
            checked(["git", "-C", repo, "remote", "add", name, first], stage="copy Git remotes")
        for key in ("url", "pushurl", "fetch"):
            config_key = "remote." + name + "." + key
            set_config_values(repo, config_key, values[key])
        if not values["url"]:
            run(["git", "-C", repo, "config", "--unset-all", "remote." + name + ".url"])

def read_marker(path):
    if not os.path.isfile(path) or os.path.islink(path):
        return None
    try:
        with open(path, "r", encoding="utf-8") as source:
            value = json.load(source)
    except (OSError, ValueError):
        return None
    return value if isinstance(value, dict) else None

def main():
    if len(sys.argv) != 4:
        fail("expected source, destination, and session ID", "arguments")
    source_arg, destination_arg, session_id = sys.argv[1:]
    if not SESSION_RE.fullmatch(session_id):
        fail("session ID must be 32 lowercase hexadecimal characters", "arguments")
    if not os.path.isabs(source_arg) or not os.path.isabs(destination_arg):
        fail("source and destination must be absolute paths", "arguments")

    source = os.path.realpath(source_arg)
    destination = os.path.abspath(destination_arg)
    parent = os.path.dirname(destination)
    sessions_root = os.path.dirname(parent)
    if os.path.basename(parent) != session_id:
        fail("destination parent must be named with this session ID", "arguments")
    if os.path.lexists(parent) and (os.path.islink(parent) or not os.path.isdir(parent)):
        fail("session destination parent is not a private directory", "reserve destination")
    os.makedirs(sessions_root, mode=0o700, exist_ok=True)
    if os.path.islink(sessions_root) or not os.path.isdir(sessions_root):
        fail("session root is not a directory", "reserve destination")

    parent_created = False
    if not os.path.exists(parent):
        try:
            os.mkdir(parent, 0o700)
            parent_created = True
        except FileExistsError:
            pass
    if not os.path.isdir(parent) or os.path.islink(parent):
        fail("session destination parent is not a directory", "reserve destination")
    if parent_created:
        os.chmod(parent, 0o700)

    marker_path = os.path.join(parent, "session.json")
    baseline_path = os.path.join(parent, "baseline.json")
    lock_path = os.path.join(parent, ".prepare.lock")
    lock_fd = os.open(lock_path, os.O_CREAT | os.O_RDWR | getattr(os, "O_NOFOLLOW", 0), 0o600)
    temporary = None
    metadata = None
    try:
        fcntl.flock(lock_fd, fcntl.LOCK_EX)
        existing = read_marker(marker_path)
        if existing is not None:
            expected = (existing.get("version") == 1
                        and existing.get("session_id") == session_id
                        and existing.get("source_cwd") == source
                        and existing.get("destination") == destination)
            if not expected:
                fail("destination belongs to a different session or source; refusing to overwrite", "reserve destination")
            metadata = existing
        elif not parent_created:
            fail("existing destination has no valid Remote Workbench session marker; refusing to overwrite", "reserve destination")
        elif os.path.lexists(destination):
            fail("destination already exists; refusing to overwrite", "reserve destination")

        if metadata and metadata.get("status") == "ready":
            if not os.path.isdir(destination) or os.path.islink(destination):
                fail("prepared session project is missing or is not a directory", "reuse project")
            result = dict(metadata)
            baseline = read_marker(baseline_path)
            result["baseline_available"] = bool(
                baseline and baseline.get("version") == 1
                and baseline.get("session_id") == session_id
                and baseline.get("project_root") == destination
                and baseline.get("baseline_id") == metadata.get("baseline_id")
            )
            result["created"] = False
            print(json.dumps(result, ensure_ascii=False, separators=(",", ":")))
            return

        if os.path.lexists(destination):
            if not metadata or metadata.get("status") not in ("preparing", "failed") or not os.path.isdir(destination) or os.path.islink(destination):
                fail("destination already exists; refusing to overwrite", "reserve destination")
            # Atomic rename completed before an interrupted metadata update.
            metadata["status"] = "ready"
            baseline = read_marker(baseline_path)
            if (baseline and baseline.get("version") == 1
                    and baseline.get("session_id") == session_id
                    and baseline.get("project_root") == destination):
                metadata["baseline_id"] = baseline.get("baseline_id")
                metadata["baseline_file_count"] = len(baseline.get("files", {}))
            else:
                metadata.pop("baseline_id", None)
                metadata["baseline_file_count"] = 0
            metadata.pop("error", None)
            atomic_json(marker_path, metadata)
            result = dict(metadata)
            result["baseline_available"] = bool(metadata.get("baseline_id"))
            result["created"] = False
            print(json.dumps(result, ensure_ascii=False, separators=(",", ":")))
            return

        if metadata is None:
            metadata = {"version": 1, "session_id": session_id,
                        "source_cwd": source, "destination": destination,
                        "status": "preparing"}
        else:
            metadata["status"] = "preparing"
            metadata.pop("error", None)
        atomic_json(marker_path, metadata)

        if not os.path.isdir(source):
            fail("source workspace does not exist or is not a directory", "inspect source")
        source = os.path.realpath(source)
        probe = run(["git", "-C", source, "rev-parse", "--show-toplevel"])
        if probe.returncode == 0:
            project_type = "git"
            repo_root = os.path.realpath(os.fsdecode(probe.stdout).strip())
            if inside(destination, repo_root):
                fail("destination must be outside the source Git repository", "validate destination")
            relative_cwd = os.path.relpath(source, repo_root)
            head = checked(["git", "-C", repo_root, "rev-parse", "HEAD"], stage="inspect Git HEAD").decode().strip()
            remotes = capture_remotes(repo_root)
            staged = checked(["git", "-C", repo_root, "ls-files", "--stage", "--full-name", "-z"], stage="list Git files")
            submodules = []
            for record in staged.split(b"\0"):
                if not record:
                    continue
                header, rel_bytes = record.split(b"\t", 1)
                if header.split(b" ", 1)[0] == b"160000" and not excluded_from_gitlink(os.fsdecode(rel_bytes)):
                    submodules.append(os.fsdecode(rel_bytes))
            if submodules:
                fail("Git submodules are not supported in session snapshots: " + ", ".join(submodules[:5]), "inspect Git files")
        else:
            marker = source
            while marker != os.path.dirname(marker):
                if os.path.lexists(os.path.join(marker, ".git")):
                    fail(probe.stderr.decode("utf-8", "replace").strip() or "could not inspect source Git repository", "inspect Git repository")
                marker = os.path.dirname(marker)
            project_type = "directory"
            repo_root = source
            relative_cwd = "."
            head = None
            remotes = {}
            if inside(destination, source):
                fail("destination must be outside the source workspace", "validate destination")
            source_paths = directory_file_paths(repo_root)

        cwd = destination if relative_cwd in (".", "") else os.path.normpath(os.path.join(destination, relative_cwd))
        metadata.update({"project_type": project_type, "source_root": repo_root,
                         "source_head": head, "cwd": cwd, "status": "preparing"})
        atomic_json(marker_path, metadata)

        # A prior process may have exited while populating its private temp tree.
        for name in os.listdir(parent):
            if name.startswith(".project.tmp-"):
                stale = os.path.join(parent, name)
                if os.path.islink(stale) or not os.path.isdir(stale):
                    os.unlink(stale)
                else:
                    shutil.rmtree(stale)

        temporary = os.path.join(parent, ".project.tmp-" + uuid.uuid4().hex)
        os.mkdir(temporary, 0o700)
        if project_type == "git":
            checked(["git", "clone", "--no-hardlinks", "--no-checkout", "--local",
                     "--", repo_root, temporary], stage="clone Git history")
            checked(["git", "-C", temporary, "reset", "--mixed", "HEAD"], stage="initialize Git index")
            restore_remotes(temporary, remotes)
            entries = checked(["git", "-C", repo_root, "ls-files", "--full-name",
                               "--cached", "--others", "--exclude-standard", "-z"], stage="list source files")
            paths = sorted(set(os.fsdecode(path) for path in entries.split(b"\0") if path))
            source_paths = [path for path in paths
                            if not excluded_from_directory_copy(path)]
            tracked = checked(["git", "-C", repo_root, "ls-files", "--full-name", "--cached", "-z"], stage="list tracked files")
            tracked_paths = set(os.fsdecode(item) for item in tracked.split(b"\0") if item)
            skip = [path for path in tracked_paths if tool_owned(path)]
            if skip:
                payload = b"\0".join(os.fsencode(path) for path in skip) + b"\0"
                checked(["git", "-C", temporary, "update-index", "--skip-worktree", "-z", "--stdin"], input_data=payload, stage="exclude tracked runtime files")
            for relative in paths:
                if (tool_owned(relative)
                        or (relative not in tracked_paths and environment_path(relative))
                        or relative.startswith("../") or os.path.isabs(relative)):
                    continue
                source_file = os.path.join(repo_root, relative)
                if not os.path.lexists(source_file):
                    continue  # Preserve deletions against the cloned HEAD index.
                target_file = os.path.join(temporary, relative)
                copy_file(source_file, target_file, repo_root, destination)
        else:
            copy_tree(repo_root, temporary, repo_root, destination)

        baseline = baseline_manifest(repo_root, destination, temporary, source_paths, session_id)
        os.chmod(temporary, 0o700)
        if os.path.lexists(destination):
            fail("destination appeared during preparation; refusing to overwrite", "publish project")
        os.rename(temporary, destination)
        temporary = None
        atomic_json(baseline_path, baseline)
        metadata["baseline_id"] = baseline["baseline_id"]
        metadata["baseline_file_count"] = len(baseline["files"])
        metadata["status"] = "ready"
        metadata.pop("error", None)
        atomic_json(marker_path, metadata)
        result = dict(metadata)
        result["baseline_available"] = True
        result["created"] = True
        print(json.dumps(result, ensure_ascii=False, separators=(",", ":")))
    except Exception as exc:
        if temporary and os.path.lexists(temporary):
            if os.path.islink(temporary) or not os.path.isdir(temporary):
                os.unlink(temporary)
            else:
                shutil.rmtree(temporary, ignore_errors=True)
        if metadata is not None:
            metadata["status"] = "failed"
            metadata["error"] = str(exc)[:1000]
            try:
                atomic_json(marker_path, metadata)
            except OSError:
                pass
        print(json.dumps({"error": str(exc)[:1200]}, ensure_ascii=False, separators=(",", ":")))
        raise SystemExit(1)
    finally:
        os.close(lock_fd)

if __name__ == "__main__":
    main()
'''


async def prepare_project(executor, source_cwd: str, session_id: str, destination: str) -> dict:
    """Create or reuse the current session's private project snapshot.

    Git snapshots have independent object storage and refs, with the cloned
    ``HEAD`` as the index baseline and the source's current non-ignored files
    copied into the worktree. Non-Git workspaces are copied as directories.
    The source is only read; incomplete preparations never fall back to it.
    """
    if not isinstance(session_id, str) or not _SESSION_ID.fullmatch(session_id):
        raise ValueError("session_id must be 32 lowercase hexadecimal characters")
    if not isinstance(source_cwd, str) or not source_cwd.startswith("/") or "\x00" in source_cwd:
        raise ValueError("source_cwd must be an absolute remote path")
    if not isinstance(destination, str) or not destination.startswith("/") or "\x00" in destination:
        raise ValueError("destination must be an absolute remote path")

    result = await executor.run_program(
        ["python3", "-c", _BOOTSTRAP, source_cwd, destination, session_id],
        cwd=source_cwd,
        output_budget=4096,
        timeout=executor.config.file_timeout,
    )
    if result.timed_out:
        raise ValueError("Session project preparation timed out; remote completion state is unknown. Do not use the source workspace as a fallback.")
    if result.exit_code != 0:
        try:
            detail = json.loads(result.stdout).get("error")
        except (TypeError, ValueError, AttributeError):
            detail = None
        detail = detail or result.stderr.strip() or "remote project preparation failed"
        raise ValueError(str(detail)[:1200])
    if result.truncated:
        raise ValueError("Session project preparation returned truncated metadata; do not use the source workspace as a fallback.")
    try:
        metadata = json.loads(result.stdout)
    except (TypeError, ValueError) as exc:
        raise ValueError("Session project preparation returned invalid metadata") from exc
    if not isinstance(metadata, dict) or metadata.get("error") or metadata.get("status") != "ready":
        raise ValueError(str(metadata.get("error", "Session project preparation did not complete"))[:1200]
                         if isinstance(metadata, dict) else "Session project preparation returned invalid metadata")
    if metadata.get("session_id") != session_id:
        raise ValueError("Session project preparation returned metadata for a different session")
    cwd = metadata.get("cwd")
    if not isinstance(cwd, str) or not cwd.startswith("/") or "\x00" in cwd:
        raise ValueError("Session project preparation returned invalid project cwd")
    output_id = getattr(result, "output_id", "")
    if output_id:
        metadata["output_id"] = output_id
    return metadata
