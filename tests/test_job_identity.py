import json
from unittest.mock import Mock

import pytest

from remote_workbench import remote_worker as worker
from remote_workbench import remote_task as task


@pytest.mark.parametrize("change,reason", [
    ({"node": "other-node"}, "different_node"),
    ({"boot_id": "new-boot"}, "different_boot"),
    ({"start_token": "reused-pid"}, "process_unconfirmed"),
    ({"start_token": None}, "process_unconfirmed"),
])
def test_pid_alone_never_proves_liveness(monkeypatch, change, reason):
    origin = {"node": "node-a", "boot_id": "boot-a", "pid": 123, "start_token": "birth-a"}
    observed = dict(origin, **change)
    monkeypatch.setattr(task, "machine_identity", lambda: observed)
    reader = Mock(return_value=observed["start_token"])
    monkeypatch.setattr(task, "process_start", reader)
    running, detail = task.job_liveness(origin)
    assert running is None and detail["state_reason"] == reason
    if reason in {"different_node", "different_boot"}:
        reader.assert_not_called()


def test_matching_node_boot_and_birth_marker_verify_running(monkeypatch):
    origin = {"node": "node-a", "boot_id": "boot-a", "pid": 123, "start_token": "birth-a"}
    monkeypatch.setattr(task, "machine_identity", lambda: origin)
    monkeypatch.setattr(task, "process_start", lambda pid: "birth-a" if pid == 123 else None)
    assert task.job_liveness(origin) == (True, {})


def test_completed_job_remains_readable_from_another_node_and_legacy_job_is_unknown(tmp_path, monkeypatch):
    monkeypatch.setenv("REMOTE_WORKBENCH_REMOTE_STATE_DIR", str(tmp_path))
    path = tmp_path / "jobs" / "legacy"
    path.mkdir(parents=True)
    (path / "pid").write_text("123")
    for stream in ("stdout", "stderr"):
        (path / (stream + ".log")).write_text("")
    monkeypatch.setattr(task, "machine_identity", lambda: {"node": "other-node", "boot_id": "boot-b"})
    reader = Mock(side_effect=AssertionError("must not probe an unverified PID"))
    monkeypatch.setattr(task, "process_start", reader)
    request = {"op": "job_status", "job_id": "legacy", "stdout_cursor": 0, "stderr_cursor": 0, "budget": 256, "tail_lines": 50}
    result = worker.job(request)
    assert result["running"] is None and result["execution_state"] == "unknown"
    (path / "exit_code").write_text("7")
    result = worker.job(request)
    assert result["running"] is False and result["execution_state"] == "finished" and result["exit_code"] == 7
    reader.assert_not_called()
