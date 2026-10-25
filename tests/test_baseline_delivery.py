from __future__ import annotations

import os
from pathlib import Path
import subprocess
import sys
from types import SimpleNamespace

import pytest

from remote_workbench import audit
from remote_workbench.baseline import apply_delivery, preview_delivery, project_fingerprint
from remote_workbench.session_project import prepare_project
from remote_workbench.ssh import SSHResult


class LocalExecutor:
    """Run the stdlib-only remote scripts locally against temporary projects."""

    config = SimpleNamespace(file_timeout=10)

    async def run_program(self, argv, *, cwd, output_budget, timeout):
        result = subprocess.run(
            [sys.executable, *argv[1:]], cwd=cwd, stdout=subprocess.PIPE,
            stderr=subprocess.PIPE, timeout=timeout,
        )
        return SSHResult(
            stdout=result.stdout.decode("utf-8", "replace"),
            stderr=result.stderr.decode("utf-8", "replace"),
            exit_code=result.returncode,
        )


def git(path: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", "-C", str(path), *args], check=True,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
    )
    return result.stdout.rstrip("\n")


def project(path: Path, *, initial: str = "committed\n") -> None:
    path.mkdir()
    git(path, "init", "-q")
    git(path, "config", "user.name", "Test User")
    git(path, "config", "user.email", "test@example.invalid")
    (path / "src").mkdir()
    (path / "src" / "sample.py").write_text(initial, encoding="utf-8")
    git(path, "add", ".")
    git(path, "commit", "-qm", "initial")


def session_path(tmp_path: Path, name: str = "sample") -> Path:
    return tmp_path / "sessions" / name / ("a" * 32) / "project"


@pytest.mark.asyncio
async def test_preview_is_relative_to_initial_uncommitted_copy_and_applies_when_source_matches(tmp_path):
    source = tmp_path / "source"
    project(source)
    (source / "src" / "sample.py").write_text("user's preexisting edit\n", encoding="utf-8")
    destination = session_path(tmp_path)
    executor = LocalExecutor()

    prepared = await prepare_project(executor, str(source), "a" * 32, str(destination))
    assert prepared["baseline_available"] is True
    assert prepared["baseline_file_count"] >= 1
    (destination / "src" / "sample.py").write_text("agent's change\n", encoding="utf-8")

    changes = await preview_delivery(executor, str(destination))
    assert [item["path"] for item in changes["files"]] == ["src/sample.py"]
    assert changes["files"][0]["source_conflict"] is False

    preview = await preview_delivery(executor, str(destination), ["src/sample.py"])
    assert "-user's preexisting edit" in preview["files"][0]["diff"]
    assert "+agent's change" in preview["files"][0]["diff"]
    assert "committed" not in preview["files"][0]["diff"]

    applied = await apply_delivery(executor, str(destination), preview["files"])
    assert applied["files"][0]["status"] == "applied"
    assert (source / "src" / "sample.py").read_text(encoding="utf-8") == "agent's change\n"
    assert applied["transaction_scope"] == "independent_files"


@pytest.mark.asyncio
async def test_source_conflict_omits_diff_and_preserves_new_source_content(tmp_path):
    source = tmp_path / "source"
    project(source)
    destination = session_path(tmp_path)
    executor = LocalExecutor()
    await prepare_project(executor, str(source), "a" * 32, str(destination))
    (destination / "src" / "sample.py").write_text("agent change\n", encoding="utf-8")
    (source / "src" / "sample.py").write_text("newer user change\n", encoding="utf-8")

    preview = await preview_delivery(executor, str(destination), ["src/sample.py"])
    file = preview["files"][0]
    assert file["source_conflict"] is True
    assert file.get("diff") is None
    assert "source_changed_since_session_start" in file["reason"]

    applied = await apply_delivery(executor, str(destination), [file])
    assert applied["files"][0]["status"] == "conflict"
    assert (source / "src" / "sample.py").read_text(encoding="utf-8") == "newer user change\n"


@pytest.mark.asyncio
async def test_hash_guard_rejects_session_changes_after_preview(tmp_path):
    source = tmp_path / "source"
    project(source)
    destination = session_path(tmp_path)
    executor = LocalExecutor()
    await prepare_project(executor, str(source), "a" * 32, str(destination))
    private_file = destination / "src" / "sample.py"
    private_file.write_text("reviewed change\n", encoding="utf-8")
    preview = await preview_delivery(executor, str(destination), ["src/sample.py"])
    private_file.write_text("unreviewed later change\n", encoding="utf-8")

    applied = await apply_delivery(executor, str(destination), preview["files"])
    assert applied["files"][0]["status"] == "conflict"
    assert applied["files"][0]["reason"] == "preview_guard_stale"
    assert (source / "src" / "sample.py").read_text(encoding="utf-8") == "committed\n"


@pytest.mark.asyncio
async def test_source_change_after_preview_is_rejected_without_overwriting_user_edit(tmp_path):
    source = tmp_path / "source"
    project(source)
    destination = session_path(tmp_path)
    executor = LocalExecutor()
    await prepare_project(executor, str(source), "a" * 32, str(destination))
    (destination / "src" / "sample.py").write_text("agent review\n", encoding="utf-8")
    preview = await preview_delivery(executor, str(destination), ["src/sample.py"])

    (source / "src" / "sample.py").write_text("user edit after preview\n", encoding="utf-8")
    applied = await apply_delivery(executor, str(destination), preview["files"])

    assert applied["files"][0]["status"] == "conflict"
    assert applied["files"][0]["reason"] == "preview_guard_stale"
    assert (source / "src" / "sample.py").read_text(encoding="utf-8") == "user edit after preview\n"


@pytest.mark.asyncio
async def test_preview_diff_budget_is_total_and_full_diff_goes_to_local_audit(tmp_path):
    source = tmp_path / "source"
    project(source, initial="a" * 9000 + "\n")
    destination = session_path(tmp_path)
    executor = LocalExecutor()
    await prepare_project(executor, str(source), "a" * 32, str(destination))
    (destination / "src" / "sample.py").write_text("b" * 9000 + "\n", encoding="utf-8")

    with audit.scoped_root(tmp_path / "audit"):
        parent = audit.create(kind="tool", tool="workspace_preview", status="running")
        token = audit.operation_id.set(parent)
        try:
            preview = await preview_delivery(
                executor, str(destination), ["src/sample.py"], max_bytes=8192,
            )
        finally:
            audit.operation_id.reset(token)
        file = preview["files"][0]
        assert file.get("diff") is None
        assert file["diff_omitted"] is True
        assert preview["diff_bytes_returned"] <= 8192
        stored = audit.read_output(parent, "diff", max_bytes=65536)
        assert stored["total_bytes"] > 8192
        assert "-" + "a" * 9000 in stored["content"]
        assert "+" + "b" * 9000 in stored["content"]


@pytest.mark.asyncio
async def test_project_fingerprint_is_bounded_and_does_not_follow_external_symlink(tmp_path):
    root = tmp_path / "project"
    root.mkdir()
    (root / "code.py").write_text("safe code\n", encoding="utf-8")
    (root / ".venv").mkdir()
    (root / ".venv" / "ignored.py").write_text("ignored\n", encoding="utf-8")
    external = tmp_path / "external"
    external.mkdir()
    secret = external / "secret.txt"
    secret.write_text("secret version one", encoding="utf-8")
    os.symlink(external, root / "outside-link")
    executor = LocalExecutor()

    first = await project_fingerprint(executor, str(root), max_files=10, max_bytes=1024)
    secret.write_text("secret version two", encoding="utf-8")
    second = await project_fingerprint(executor, str(root), max_files=10, max_bytes=1024)
    assert first["complete"] is True and first["digest"] == second["digest"]
    assert first["files"] == 2
    assert "secret.txt" not in str(first)
    assert "secret version" not in str(first)

    too_small = await project_fingerprint(executor, str(root), max_files=10, max_bytes=2)
    assert too_small["complete"] is False
    assert too_small["digest"] is None
    assert too_small["reason"] == "byte_budget_exceeded"


@pytest.mark.asyncio
async def test_directory_baseline_captures_added_and_deleted_files(tmp_path):
    source = tmp_path / "plain"
    source.mkdir()
    (source / "delete.txt").write_text("remove\n", encoding="utf-8")
    destination = session_path(tmp_path, "plain")
    executor = LocalExecutor()
    await prepare_project(executor, str(source), "a" * 32, str(destination))
    (destination / "delete.txt").unlink()
    (destination / "nested").mkdir()
    (destination / "nested" / "new.txt").write_text("new\n", encoding="utf-8")

    preview = await preview_delivery(executor, str(destination), ["delete.txt", "nested/new.txt"])
    assert {item["path"]: item["change"] for item in preview["files"]} == {
        "delete.txt": "deleted", "nested/new.txt": "added",
    }
    result = await apply_delivery(executor, str(destination), preview["files"])
    assert all(item["status"] == "applied" for item in result["files"])
    assert not (source / "delete.txt").exists()
    assert (source / "nested" / "new.txt").read_text(encoding="utf-8") == "new\n"
