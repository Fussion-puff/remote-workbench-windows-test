"""Bounded local lifecycle tests: at most two fixed workload processes.

No configured server is contacted, no recursion or resource exhaustion is used.
The process-group backend is deliberately explicit; it is not a hard PID cap.
"""
import asyncio
import json
import os
from pathlib import Path
import shlex
import signal
import subprocess
import sys
import time
from unittest.mock import AsyncMock

import pytest

from remote_workbench import audit
from remote_workbench import remote_task as task
from remote_workbench.config import HostConfig
from remote_workbench.ssh import SSHExecutor
from remote_workbench.tools import commands


async def wait_file(path, limit=5):
    end = time.monotonic() + limit
    while not path.exists():
        assert time.monotonic() < end, "bounded workload failed to start"
        await asyncio.sleep(0.03)
    return int(path.read_text())


def alive(pid):
    value = subprocess.run(["ps", "-p", str(pid), "-o", "stat="], capture_output=True, text=True, timeout=2)
    return value.returncode == 0 and bool(value.stdout.strip()) and not value.stdout.strip().startswith("Z")


def sleeper(path, child=False):
    source = "import os,signal,time; from pathlib import Path; signal.signal(signal.SIGTERM,signal.SIG_IGN); "
    source += "Path(" + repr(str(path)) + ").write_text(str(os.getpid())); "
    if child:
        nested = "import os,signal,time; from pathlib import Path; signal.signal(signal.SIGTERM,signal.SIG_IGN); Path(" + repr(str(path) + ".child") + ").write_text(str(os.getpid())); time.sleep(12)"
        source += "import subprocess; subprocess.Popen([" + repr(sys.executable) + ", '-c', " + repr(nested) + "]); "
    source += "time.sleep(12)"
    return "exec " + shlex.join([sys.executable, "-c", source])


async def test_disabled_host_never_launches_ssh_or_user_code(monkeypatch):
    launch = AsyncMock(side_effect=AssertionError("must not launch"))
    monkeypatch.setattr("remote_workbench.ssh.asyncio.create_subprocess_exec", launch)
    executor = SSHExecutor(HostConfig("never-contact.invalid", execution_enabled=False))
    with pytest.raises(ValueError, match="execution is disabled"):
        await executor.run("must never run")
    launch.assert_not_awaited()


async def test_deadline_must_be_positive(local):
    executor, _ = local
    with pytest.raises(ValueError, match="positive"):
        await executor.run("must never run", timeout=0)


async def test_timeout_kills_fixed_child_tree_in_owned_group(local):
    executor, root = local
    path = root / "owned-parent"
    result = await executor.run(sleeper(path, child=True), timeout=2)
    parent, child = int(path.read_text()), int(Path(str(path) + ".child").read_text())
    assert result.timed_out and result.exit_code == 124
    assert result.remote_task["reason"] == "timeout"
    assert result.remote_task["pgid"] == parent
    assert result.remote_task["cleanup_state"] == "process_group_signalled"
    assert result.remote_task["tree_confirmed"] is False
    assert not alive(parent) and not alive(child)


async def test_agent_cancellation_closes_remote_lease_and_retains_acknowledgement(local):
    executor, root = local
    path = root / "owned-cancel"
    running = asyncio.create_task(executor.run(sleeper(path), timeout=15))
    pid = await wait_file(path)
    running.cancel()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(running, 7)
    assert not alive(pid)
    records = [json.loads(p.read_text()) for p in (root / "audit").glob("*/record.json")]
    record = next(value for value in records if value.get("kind") == "command")
    assert record["status"] == "cancelled"
    assert record["remote_task"]["state"] == "finished"
    assert record["remote_task"]["reason"] == "controller_disconnected"


async def test_background_deadline_and_cleanup_work_with_execution_disabled(local):
    executor, root = local
    path = root / "owned-job"
    value = await commands.ssh_run_background(sleeper(path), job_id="owned-job", timeout=15)
    assert value["job_id"] == "owned-job", value
    pid = await wait_file(path)
    assert value["remote_task"]["timeout_seconds"] == 15
    executor.config.execution_enabled = False
    stopped = await commands.ssh_stop_job("owned-job")
    assert stopped["cancel_requested"] is True
    assert stopped["execution_state"] == "finished"
    assert stopped["reason"] == "cancelled"
    assert stopped["cleanup_state"] == "process_group_signalled"
    assert not alive(pid)
    again = await commands.ssh_stop_job("owned-job")
    assert again["cancel_requested"] is False


async def test_detached_job_enforces_its_own_finite_deadline(local):
    _, root = local
    path = root / "owned-expiring"
    value = await commands.ssh_run_background(sleeper(path), job_id="expires", timeout=2)
    pid = await wait_file(path)
    status_path = Path(value["job_dir"]) / "status.json"
    end = time.monotonic() + 5
    while True:
        status = json.loads(status_path.read_text())
        if status["state"] == "finished":
            break
        assert time.monotonic() < end
        await asyncio.sleep(0.03)
    assert status["reason"] == "timeout" and status["exit_code"] == 124
    assert not alive(pid)


async def test_missing_heartbeat_cleans_up_without_controller_cancel(local, monkeypatch):
    executor, root = local
    path = root / "owned-no-heartbeat"
    # This uses the substituted local transport directly. Keep stdin OPEN but
    # send no heartbeats, simulating a controller which has stopped responding.
    process = await asyncio.create_subprocess_exec("ssh", executor._wrap_command(sleeper(path), str(root), timeout=12),
        stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
    pid = await wait_file(path)
    try:
        out, err = await asyncio.wait_for(asyncio.gather(process.stdout.read(), process.stderr.read()), 9)
        assert await process.wait() == 130
        assert b'"reason": "controller_lease_expired"' in err
        assert not alive(pid)
    finally:
        process.stdin.close()
        if process.returncode is None:
            process.kill()
            await process.wait()


async def test_missing_waitid_uses_portable_guardian_and_preserves_exit_code(local, monkeypatch):
    executor, _ = local
    read_text = Path.read_text
    def source_without_waitid(path, *args, **kwargs):
        source = read_text(path, *args, **kwargs)
        if path.name == "remote_task.py":
            source += "\nif hasattr(os, 'waitid'): del os.waitid\n"
        return source
    monkeypatch.setattr(Path, "read_text", source_without_waitid)
    result = await executor.run("printf portable; exit 7")
    assert result.exit_code == 7 and result.stdout == "portable"
    assert result.remote_task["pgid"] != result.remote_task["workload_pid"]
    assert result.remote_task["cleanup_state"] == "process_group_signalled"


@pytest.mark.parametrize("change,reason", [
    ({"node": "other-node"}, "different_node"),
    ({"boot_id": "other-boot"}, "different_boot"),
    ({"start_token": "another-process"}, "process_unconfirmed"),
])
def test_cleanup_refuses_foreign_or_reused_identity(tmp_path, monkeypatch, change, reason):
    origin = {"version": 2, "node": "node-a", "boot_id": "boot-a", "pid": 123, "start_token": "birth-a"}
    task.save_task_json(tmp_path / "origin.json", origin)
    observed = dict(origin, **change)
    monkeypatch.setattr(task, "machine_identity", lambda: observed)
    monkeypatch.setattr(task, "process_start", lambda pid: observed["start_token"])
    assert task.cancel_task(tmp_path)["state_reason"] == reason
    assert not (tmp_path / "cancel").exists()


@pytest.mark.parametrize("override", [
    {"execution_enabled": "yes"}, {"job_timeout": 0}, {"job_timeout": True},
    {"terminate_grace_seconds": 0}, {"compute_review_threshold": 0},
])
def test_invalid_policy_is_rejected_at_configuration(override):
    with pytest.raises(ValueError):
        HostConfig("unused", **override)
