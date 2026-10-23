from __future__ import annotations

import hashlib
import os
from pathlib import Path
import subprocess
import sys
from types import SimpleNamespace

import pytest

from remote_workbench.session_project import prepare_project
from remote_workbench.ssh import SSHResult


def git(path: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", "-C", str(path), *args],
        check=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    return result.stdout.rstrip("\n")


class LocalExecutor:
    """Run the stdlib-only remote bootstrap against a local test directory."""

    def __init__(self, timeout: int = 30):
        self.config = SimpleNamespace(file_timeout=timeout)
        self.calls = []

    async def run_program(self, argv, *, cwd, output_budget, timeout):
        self.calls.append((argv, cwd, output_budget, timeout))
        result = subprocess.run(
            [sys.executable, *argv[1:]],
            cwd=cwd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=timeout,
        )
        return SSHResult(
            stdout=result.stdout.decode("utf-8", "replace"),
            stderr=result.stderr.decode("utf-8", "replace"),
            exit_code=result.returncode,
        )


def make_git_project(path: Path) -> None:
    path.mkdir()
    git(path, "init", "-q")
    git(path, "config", "user.name", "Test User")
    git(path, "config", "user.email", "test@example.invalid")
    (path / ".gitignore").write_text(
        ".venv/\nvenv/\nnode_modules/\ncache/\n.remote-workbench-state/\n"
        ".remote-workbench-sessions/\n",
        encoding="utf-8",
    )
    (path / "tracked.txt").write_text("committed\n", encoding="utf-8")
    (path / "deleted.txt").write_text("remove in working tree\n", encoding="utf-8")
    (path / "tests").mkdir()
    (path / "tests" / "test_existing.py").write_text("def test_existing(): pass\n", encoding="utf-8")
    (path / "src" / "cache").mkdir(parents=True)
    (path / "src" / "cache" / "module.py").write_text("tracked cache source\n", encoding="utf-8")
    (path / "vendor" / "node_modules").mkdir(parents=True)
    (path / "vendor" / "node_modules" / "module.js").write_text("tracked vendor source\n", encoding="utf-8")
    git(path, "add", ".")
    git(path, "add", "-f", "src/cache/module.py", "vendor/node_modules/module.js")
    git(path, "commit", "-qm", "initial project")
    git(path, "remote", "add", "origin", "https://example.invalid/team/project.git")


def session_destination(base: Path, session_id: str) -> Path:
    return base / session_id / "project"


@pytest.mark.asyncio
async def test_git_snapshot_preserves_uncommitted_files_and_never_changes_source(tmp_path):
    source = tmp_path / "source"
    make_git_project(source)
    (source / "tracked.txt").write_text("working tree edit\n", encoding="utf-8")
    (source / "deleted.txt").unlink()
    (source / "untracked.txt").write_text("new work\n", encoding="utf-8")
    os.symlink(source / "tracked.txt", source / "absolute-link")
    for name in (".venv", "venv", "node_modules", "cache", ".remote-workbench-state", ".remote-workbench-sessions"):
        runtime = source / name
        runtime.mkdir()
        (runtime / "must-not-copy").write_text("runtime", encoding="utf-8")

    status_before = git(source, "status", "--porcelain=v1", "--untracked-files=all")
    index_before = hashlib.sha256((source / ".git" / "index").read_bytes()).hexdigest()
    source_bytes_before = (source / "tracked.txt").read_bytes()
    executor = LocalExecutor()
    session_id = "a" * 32
    destination = session_destination(tmp_path / "sessions", session_id)

    result = await prepare_project(executor, str(source), session_id, str(destination))

    assert result["status"] == "ready"
    assert result["created"] is True
    assert result["project_type"] == "git"
    assert result["cwd"] == str(destination)
    assert (destination / "tracked.txt").read_text(encoding="utf-8") == "working tree edit\n"
    assert not (destination / "deleted.txt").exists()
    assert (destination / "untracked.txt").read_text(encoding="utf-8") == "new work\n"
    assert (destination / "tests" / "test_existing.py").is_file()
    assert (destination / "src" / "cache" / "module.py").read_text(encoding="utf-8") == "tracked cache source\n"
    assert (destination / "vendor" / "node_modules" / "module.js").read_text(encoding="utf-8") == "tracked vendor source\n"
    assert "working tree edit" in git(destination, "diff", "--", "tracked.txt")
    assert os.readlink(destination / "absolute-link") == str(destination / "tracked.txt")
    assert (destination / "absolute-link").read_text(encoding="utf-8") == "working tree edit\n"
    status = git(destination, "status", "--porcelain=v1", "--untracked-files=all")
    assert " M tracked.txt" in status
    assert " D deleted.txt" in status
    assert "?? untracked.txt" in status
    assert git(destination, "remote", "get-url", "origin") == "https://example.invalid/team/project.git"
    for name in (".venv", "venv", "node_modules", "cache", ".remote-workbench-state", ".remote-workbench-sessions"):
        assert not (destination / name).exists()

    assert git(source, "status", "--porcelain=v1", "--untracked-files=all") == status_before
    assert hashlib.sha256((source / ".git" / "index").read_bytes()).hexdigest() == index_before
    assert (source / "tracked.txt").read_bytes() == source_bytes_before
    assert executor.calls[0][2:] == (4096, 30)


@pytest.mark.asyncio
async def test_session_destinations_are_independent_and_same_session_is_idempotent(tmp_path):
    source = tmp_path / "source"
    make_git_project(source)
    (source / "tracked.txt").write_text("source snapshot\n", encoding="utf-8")
    executor = LocalExecutor()
    first_id, second_id = "b" * 32, "c" * 32
    first = session_destination(tmp_path / "sessions", first_id)
    second = session_destination(tmp_path / "sessions", second_id)

    initial = await prepare_project(executor, str(source), first_id, str(first))
    (first / "tracked.txt").write_text("session one edit\n", encoding="utf-8")
    repeated = await prepare_project(executor, str(source), first_id, str(first))
    other = await prepare_project(executor, str(source), second_id, str(second))

    assert initial["created"] is True
    assert repeated["created"] is False
    assert repeated["cwd"] == str(first)
    assert (first / "tracked.txt").read_text(encoding="utf-8") == "session one edit\n"
    assert (second / "tracked.txt").read_text(encoding="utf-8") == "source snapshot\n"
    assert other["session_id"] == second_id
    assert (source / "tracked.txt").read_text(encoding="utf-8") == "source snapshot\n"
    assert (first.parent / "session.json").is_file()
    assert (second.parent / "session.json").is_file()
    assert stat_mode(first.parent) == 0o700
    assert stat_mode(first) == 0o700


def stat_mode(path: Path) -> int:
    return path.stat().st_mode & 0o777


@pytest.mark.asyncio
async def test_git_index_uses_head_baseline_without_restoring_deleted_files(tmp_path):
    source = tmp_path / "repo"
    make_git_project(source)
    (source / "tracked.txt").write_text("changed\n", encoding="utf-8")
    (source / "deleted.txt").unlink()
    destination = session_destination(tmp_path / "sessions", "d" * 32)

    result = await prepare_project(LocalExecutor(), str(source), "d" * 32, str(destination))

    assert result["status"] == "ready"
    assert "changed" in git(destination, "diff", "--", "tracked.txt")
    assert " D deleted.txt" in git(destination, "status", "--porcelain")
    assert not (destination / "deleted.txt").exists()


@pytest.mark.asyncio
async def test_git_subdirectory_source_returns_matching_snapshot_cwd(tmp_path):
    source = tmp_path / "repo"
    make_git_project(source)
    session_id = "9" * 32
    destination = session_destination(tmp_path / "sessions", session_id)

    result = await prepare_project(LocalExecutor(), str(source / "tests"), session_id, str(destination))

    assert result["cwd"] == str(destination / "tests")
    assert (Path(result["cwd"]) / "test_existing.py").is_file()


@pytest.mark.asyncio
async def test_directory_snapshot_excludes_runtime_and_preserves_relative_symlinks(tmp_path):
    source = tmp_path / "plain-source"
    source.mkdir()
    (source / "src").mkdir()
    (source / "src" / "main.py").write_text("print('ok')\n", encoding="utf-8")
    (source / "empty-directory").mkdir()
    os.symlink("src/main.py", source / "main-link")
    os.symlink(source / "src" / "main.py", source / "absolute-link")
    for name in (".venv", "node_modules", ".cache", "cache", ".remote-workbench-state", ".remote-workbench-sessions"):
        runtime = source / name
        runtime.mkdir()
        (runtime / "secret").write_text("skip", encoding="utf-8")
    destination = session_destination(tmp_path / "sessions", "e" * 32)

    result = await prepare_project(LocalExecutor(), str(source), "e" * 32, str(destination))

    assert result["project_type"] == "directory"
    assert (destination / "src" / "main.py").read_text(encoding="utf-8") == "print('ok')\n"
    assert (destination / "empty-directory").is_dir()
    assert os.readlink(destination / "main-link") == "src/main.py"
    assert os.readlink(destination / "absolute-link") == str(destination / "src" / "main.py")
    assert (destination / "absolute-link").read_text(encoding="utf-8") == "print('ok')\n"
    assert (destination / "cache" / "secret").read_text(encoding="utf-8") == "skip"
    for name in (".venv", "node_modules", ".cache", ".remote-workbench-state", ".remote-workbench-sessions"):
        assert not (destination / name).exists()
    assert (source / "node_modules" / "secret").read_text(encoding="utf-8") == "skip"


@pytest.mark.asyncio
async def test_unknown_destination_is_never_overwritten(tmp_path):
    source = tmp_path / "repo"
    make_git_project(source)
    session_id = "f" * 32
    destination = session_destination(tmp_path / "sessions", session_id)
    destination.mkdir(parents=True)
    sentinel = destination / "keep.txt"
    sentinel.write_text("user data", encoding="utf-8")

    with pytest.raises(ValueError, match="refusing to overwrite"):
        await prepare_project(LocalExecutor(), str(source), session_id, str(destination))

    assert sentinel.read_text(encoding="utf-8") == "user data"
    assert not (destination.parent / "session.json").exists()


@pytest.mark.asyncio
async def test_invalid_remote_result_fails_closed(tmp_path):
    source = tmp_path / "source"
    source.mkdir()
    destination = session_destination(tmp_path / "sessions", "1" * 32)

    class FailedExecutor:
        config = SimpleNamespace(file_timeout=5)

        async def run_program(self, *args, **kwargs):
            return SSHResult(stdout="", stderr="permission denied", exit_code=255)

    with pytest.raises(ValueError, match="permission denied"):
        await prepare_project(FailedExecutor(), str(source), "1" * 32, str(destination))
    assert not destination.exists()

    class InvalidMetadataExecutor:
        config = SimpleNamespace(file_timeout=5)

        async def run_program(self, *args, **kwargs):
            return SSHResult(
                stdout='{"status":"ready","session_id":"11111111111111111111111111111111"}',
                stderr="",
                exit_code=0,
            )

    with pytest.raises(ValueError, match="invalid project cwd"):
        await prepare_project(InvalidMetadataExecutor(), str(source), "1" * 32, str(destination))


@pytest.mark.asyncio
async def test_bootstrap_output_id_is_returned_for_parent_operation_link():
    class AuditedExecutor:
        config = SimpleNamespace(file_timeout=5)

        async def run_program(self, *args, **kwargs):
            return SSHResult(
                stdout=(
                    '{"status":"ready","session_id":"11111111111111111111111111111111",'
                    '"cwd":"/remote/project"}'
                ),
                stderr="",
                exit_code=0,
                output_id="bootstrap-record-id",
            )

    result = await prepare_project(
        AuditedExecutor(), "/remote/source", "1" * 32, "/remote/sessions/" + "1" * 32 + "/project"
    )

    assert result["output_id"] == "bootstrap-record-id"


@pytest.mark.asyncio
async def test_prepare_validates_session_and_remote_paths_before_ssh(tmp_path):
    class NoCallExecutor:
        config = SimpleNamespace(file_timeout=5)

        async def run_program(self, *args, **kwargs):
            raise AssertionError("should not start a remote command")

    executor = NoCallExecutor()
    with pytest.raises(ValueError, match="session_id"):
        await prepare_project(executor, "/remote/source", "not-a-session", "/remote/sessions/not-a-session/project")
    with pytest.raises(ValueError, match="absolute remote path"):
        await prepare_project(executor, "relative", "2" * 32, "/remote/sessions/" + "2" * 32 + "/project")
