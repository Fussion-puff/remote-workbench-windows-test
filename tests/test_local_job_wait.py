import asyncio
import json
from unittest.mock import AsyncMock

import pytest

from remote_workbench import audit
from remote_workbench.tools import commands


class LocalJobHandle:
    def __init__(self, info):
        self.info = info

    def job_local_info(self, job_id):
        if self.info.get("job_id") != job_id:
            return None
        return dict(self.info)

    async def ensure_job_collection(self, job_id):
        return self.job_local_info(job_id)


def create_job_audit(root, job_id="job-a"):
    with audit.scoped_root(root):
        output_id = audit.create(kind="background_job", job_id=job_id)
        for stream in ("stdout", "stderr"):
            with audit.open_stream(output_id, stream):
                pass
        return output_id


def setup(monkeypatch, tmp_path, info):
    monkeypatch.setenv("REMOTE_WORKBENCH_AUDIT_DIR", str(tmp_path / "audit"))
    handle = LocalJobHandle(info)
    monkeypatch.setattr(commands, "get_executor", lambda host=None: handle)
    return handle


@pytest.mark.asyncio
async def test_job_wait_wakes_for_local_output_and_returns_incremental_cursors(monkeypatch, tmp_path):
    output_id = create_job_audit(tmp_path / "audit")
    info = {"job_id": "job-a", "job_output_id": output_id, "running": True,
            "exit_code": None, "execution_state": "running", "sync_state": "syncing",
            "remote_task": {"reason": None}}
    setup(monkeypatch, tmp_path, info)

    async def append_log():
        await asyncio.sleep(0.05)
        with (audit.directory(output_id) / "stdout").open("ab") as output:
            output.write(b"ready\n")
        info["stdout_bytes"] = 6

    writer = asyncio.create_task(append_log())
    result = await commands.ssh_job_wait("job-a", wait_seconds=2)
    await writer

    assert result["wait_reason"] == "output"
    assert result["wait_expired"] is False
    assert result["stdout_tail"] == "ready\n"
    assert result["next_stdout_cursor"] == 6
    assert result["running"] is True
    assert "timed_out" not in result

    repeated = await commands.ssh_job_wait("job-a", wait_seconds=0,
                                           stdout_cursor=result["next_stdout_cursor"],
                                           stderr_cursor=result["next_stderr_cursor"])
    assert repeated["stdout_tail"] == repeated["stderr_tail"] == ""
    assert repeated["next_stdout_cursor"] == 6


@pytest.mark.asyncio
async def test_job_wait_deadline_is_not_reported_as_job_timeout(monkeypatch, tmp_path):
    output_id = create_job_audit(tmp_path / "audit")
    info = {"job_id": "job-a", "job_output_id": output_id, "running": True,
            "exit_code": None, "execution_state": "running", "sync_state": "syncing",
            "remote_task": {"reason": None}}
    setup(monkeypatch, tmp_path, info)

    result = await commands.ssh_job_wait("job-a", wait_seconds=0.03)
    assert result["wait_reason"] == "wait_deadline"
    assert result["wait_expired"] is True
    assert result["running"] is True
    assert result["remote_task"]["reason"] is None


@pytest.mark.asyncio
async def test_job_wait_wakes_for_completion_without_new_output(monkeypatch, tmp_path):
    output_id = create_job_audit(tmp_path / "audit")
    info = {"job_id": "job-a", "job_output_id": output_id, "running": True,
            "exit_code": None, "execution_state": "running", "sync_state": "syncing",
            "remote_task": {"reason": None}}
    setup(monkeypatch, tmp_path, info)

    async def finish():
        await asyncio.sleep(0.05)
        info.update(running=False, exit_code=0, execution_state="finished", sync_state="complete",
                    remote_task={"reason": "finished", "exit_code": 0})

    task = asyncio.create_task(finish())
    result = await commands.ssh_job_wait("job-a", wait_seconds=2)
    await task
    assert result["wait_reason"] == "state_changed"
    assert result["wait_expired"] is False
    assert result["running"] is False and result["exit_code"] == 0
    assert result["stdout_tail"] == result["stderr_tail"] == ""


@pytest.mark.asyncio
async def test_job_wait_without_local_handle_returns_unknown_without_ssh(monkeypatch, tmp_path):
    setup(monkeypatch, tmp_path, {"job_id": "different"})
    result = await commands.ssh_job_wait("missing", wait_seconds=30)
    assert result["error"] == "local_job_unavailable"
    assert result["running"] is None
    assert result["execution_state"] == "unknown"


@pytest.mark.asyncio
async def test_job_status_uses_local_spools_and_keeps_legacy_page_budget(monkeypatch, tmp_path):
    output_id = create_job_audit(tmp_path / "audit")
    info = {"job_id": "job-a", "job_output_id": output_id, "running": True,
            "exit_code": None, "execution_state": "running", "sync_state": "syncing",
            "remote_task": {"reason": None}}
    setup(monkeypatch, tmp_path, info)
    (audit.directory(output_id) / "stdout").write_bytes(b"first\nsecond\n")
    (audit.directory(output_id) / "stderr").write_bytes(b"warning\n")
    monkeypatch.setattr(commands, "require_connection",
                        lambda host=None: pytest.fail("local status must not open SSH"))

    result = await commands.ssh_job_status("job-a", tail_lines=1, max_bytes=256)
    assert result["stdout_tail"] == "first\n"
    assert result["stderr_tail"] == "warning\n"
    assert result["next_stdout_cursor"] == len(b"first\n")
    assert result["stdout_has_more"] is True
    assert len(result["stdout_tail"].encode()) + len(result["stderr_tail"].encode()) <= 256


@pytest.mark.asyncio
async def test_job_status_keeps_legacy_remote_path_without_local_manifest(monkeypatch, tmp_path):
    handle = setup(monkeypatch, tmp_path, {"job_id": "job-a"})
    require = AsyncMock(return_value=handle)
    legacy = {"job_id": "job-a", "stdout_tail": "legacy", "next_stdout_cursor": 6}
    edit = AsyncMock(return_value=legacy)
    monkeypatch.setattr(commands, "require_connection", require)
    monkeypatch.setattr(commands, "edit", edit)

    result = await commands.ssh_job_status("job-a")
    assert result["stdout_tail"] == "legacy"
    require.assert_awaited_once_with(None)
    edit.assert_awaited_once()


@pytest.mark.asyncio
async def test_background_job_rejects_unregistered_idempotency_key(monkeypatch):
    result = await commands.ssh_run_background("true", idempotency_key="retry-key")
    assert result["error"] == "idempotency_key_requires_session_workspace"
    assert result["executed"] is False


class BackgroundConfig:
    default_cwd = "/workspace"
    ssh_target = "fixture-host"
    compute_review_threshold = 10000


class BackgroundExecutor:
    config = BackgroundConfig()


@pytest.mark.asyncio
async def test_start_job_waits_locally_and_preserves_launch_fields(monkeypatch):
    executor = BackgroundExecutor()
    launched = {"job_id": "job-a", "job_output_id": "a" * 32,
                "launch_output_id": "b" * 32, "job_dir": "/private/job-a"}
    waited = {"job_id": "job-a", "job_output_id": "a" * 32,
              "stdout_tail": "ready\n", "wait_expired": False, "wait_reason": "output"}
    monkeypatch.setattr(commands, "get_executor", lambda host=None: executor)
    monkeypatch.setattr(commands, "check_compute_review", lambda *args: None)

    async def connected(host=None):
        return executor

    edit = AsyncMock(return_value=launched)
    wait = AsyncMock(return_value=waited)
    monkeypatch.setattr(commands, "require_connection", connected)
    monkeypatch.setattr(commands, "edit", edit)
    monkeypatch.setattr(commands, "ssh_job_wait", wait)

    result = await commands.ssh_run_background(
        "make test", job_id="job-a", host="fixture", wait_seconds=12, max_bytes=4096)

    assert result["job_id"] == "job-a"
    assert result["launch_output_id"] == "b" * 32
    assert result["job_dir"] == "/private/job-a"
    assert result["stdout_tail"] == "ready\n"
    assert result["wait_reason"] == "output"
    assert "timed_out" not in result
    wait.assert_awaited_once_with("job-a", host="fixture", wait_seconds=12,
                                  stdout_cursor=0, stderr_cursor=0, max_bytes=4096)
    assert edit.await_args.kwargs["max_bytes"] == 4096


@pytest.mark.asyncio
async def test_start_job_default_does_not_wait_and_validates_wait_budget(monkeypatch):
    executor = BackgroundExecutor()
    launched = {"job_id": "job-a", "job_output_id": "a" * 32}
    monkeypatch.setattr(commands, "get_executor", lambda host=None: executor)
    monkeypatch.setattr(commands, "check_compute_review", lambda *args: None)

    async def connected(host=None):
        return executor

    monkeypatch.setattr(commands, "require_connection", connected)
    edit = AsyncMock(return_value=launched)
    wait = AsyncMock(return_value={})
    monkeypatch.setattr(commands, "edit", edit)
    monkeypatch.setattr(commands, "ssh_job_wait", wait)

    result = await commands.ssh_run_background("true", job_id="job-a")
    assert result == launched
    wait.assert_not_awaited()

    for value in (-0.1, 30.1, True):
        with pytest.raises(ValueError, match="wait_seconds"):
            await commands.ssh_run_background("true", wait_seconds=value)
    for value in (255, 65537, True):
        with pytest.raises(ValueError, match="max_bytes"):
            await commands.ssh_run_background("true", max_bytes=value)
