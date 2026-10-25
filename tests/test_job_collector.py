import asyncio
import json
import os
from pathlib import Path
from types import SimpleNamespace

import pytest

from remote_workbench import audit
from remote_workbench import job_collector
from remote_workbench.config import HostConfig
from remote_workbench.ssh import SSHExecutor
from remote_workbench.tools import commands


async def eventually(predicate, timeout=12):
    end = asyncio.get_running_loop().time() + timeout
    while asyncio.get_running_loop().time() < end:
        if predicate():
            return
        await asyncio.sleep(0.05)
    raise AssertionError('condition not reached')


@pytest.mark.asyncio
async def test_collector_lock_failure_marks_status_unknown(tmp_path, monkeypatch):
    monkeypatch.setenv("REMOTE_WORKBENCH_AUDIT_DIR", str(tmp_path / "audit"))
    executor = SimpleNamespace(
        config=SimpleNamespace(ssh_target="host", default_cwd="/project"),
        remote_state_dir=None,
    )
    with audit.scoped_root(tmp_path / "audit"):
        job_id = "lock-failure"
        record_id = audit.create(kind="job", status="running", running=True,
                                 execution_state="running", sync_state="queued")
        for stream in ("stdout", "stderr"):
            with audit.open_stream(record_id, stream):
                pass
        path = job_collector._handle_path(executor, job_id)
        path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        path.write_text(json.dumps({"job_id": job_id, "job_dir": "/remote/job",
                                    "job_output_id": record_id}))

        real_open = os.open

        def fail_collector_lock(name, flags, *args, **kwargs):
            if Path(name).parent.name == ".job-handles" and Path(name).suffix == ".lock":
                raise PermissionError("simulated lock permission failure")
            return real_open(name, flags, *args, **kwargs)

        monkeypatch.setattr(job_collector.os, "open", fail_collector_lock)
        await job_collector._collect(executor, path)
        info = job_collector.local_info(executor, job_id)

    assert info["sync_state"] == "disconnected"
    assert info["running"] is None and info["execution_state"] == "unknown"
    assert "permission failure" in info["sync_error"]


async def test_collects_without_agent_polling_and_preserves_handle_across_restart(local):
    executor, root = local
    result = await commands.ssh_run_background("printf first; sleep 0.3; printf second; printf problem >&2", timeout=8)
    job_id = result['job_id']
    output_id = result['job_output_id']
    try:
        # No status/wait tool call drives this collection.
        await eventually(lambda: executor.job_local_info(job_id).get('sync_state') == 'complete')
        assert audit.read_output(output_id)['content'] == 'firstsecond'
        assert audit.read_output(output_id, 'stderr')['content'] == 'problem'
        record = json.loads((audit.directory(output_id) / 'record.json').read_text())
        assert record['exit_code'] == 0
        assert record['remote_task']['node']
        replacement = SSHExecutor(executor.config, executor.remote_state_dir)
        restored = await replacement.ensure_job_collection(job_id)
        assert restored['job_output_id'] == output_id
        assert restored['stdout_bytes'] == 11
        assert not getattr(replacement, '_log_collectors', {})
    finally:
        await executor.close_collectors()


async def test_resumed_collection_appends_without_duplicate_bytes(local):
    executor, root = local
    job_dir = root / 'completed'
    job_dir.mkdir()
    data = b'a' * 70000 + '终点'.encode()
    (job_dir / 'stdout.log').write_bytes(data)
    (job_dir / 'stderr.log').write_bytes(b'')
    (job_dir / 'status.json').write_text(json.dumps({'state': 'finished', 'node': 'former-node'}))
    (job_dir / 'exit_code').write_text('0')
    # Reserve a persistent handle without opening the transport yet.
    original = executor.ensure_job_collection
    async def no_collection(job_id):
        return executor.job_local_info(job_id)
    executor.ensure_job_collection = no_collection
    from remote_workbench import job_collector
    original_ensure = job_collector.ensure
    job_collector.ensure = no_collection_adapter = lambda ex, job_id: no_collection(job_id)
    try:
        result = await executor.track_job({'job_id': 'resume', 'job_dir': str(job_dir)}, 'done', str(root))
    finally:
        job_collector.ensure = original_ensure
        executor.ensure_job_collection = original
    output_id = result['job_output_id']
    # Simulate bytes already synced before the local process restarted.
    (audit.directory(output_id) / 'stdout').write_bytes(data[:12345])
    replacement = SSHExecutor(executor.config, executor.remote_state_dir)
    try:
        await replacement.ensure_job_collection('resume')
        await eventually(lambda: replacement.job_local_info('resume').get('sync_state') == 'complete')
        assert (audit.directory(output_id) / 'stdout').read_bytes() == data
        assert replacement.job_local_info('resume')['exit_code'] == 0
    finally:
        await replacement.close_collectors()


async def test_stopping_local_collector_does_not_cancel_remote_workload(local):
    executor, _ = local
    result = await commands.ssh_run_background("sleep 1; printf survived", timeout=8)
    job_id = result["job_id"]
    output_id = result["job_output_id"]
    try:
        await asyncio.sleep(0.1)
        await executor.close_collectors()
        interrupted = executor.job_local_info(job_id)
        assert interrupted["sync_state"] == "disconnected"
        assert interrupted["execution_state"] == "unknown"

        # Reopening the log follower is a read-only connection. The detached
        # workload must still complete and the local byte cursor must resume.
        await executor.ensure_job_collection(job_id)
        await eventually(lambda: executor.job_local_info(job_id).get("sync_state") == "complete")
        record = json.loads((audit.directory(output_id) / "record.json").read_text())
        assert record["exit_code"] == 0
        assert audit.read_output(output_id)["content"] == "survived"
    finally:
        await executor.close_collectors()


async def test_foreign_node_stays_unknown_and_scoped_handles_are_independent(local):
    executor, root = local
    job_dir = root / 'foreign'
    job_dir.mkdir()
    for name in ('stdout.log', 'stderr.log'):
        (job_dir / name).write_text('')
    (job_dir / 'status.json').write_text(json.dumps({'state': 'running', 'node': 'different-node'}))
    (job_dir / 'origin.json').write_text(json.dumps({'node': 'different-node', 'start_token': 'other', 'pid': 1}))
    try:
        value = await executor.track_job({'job_id': 'same', 'job_dir': str(job_dir)}, 'foreign', str(root))
        await eventually(lambda: executor.job_local_info('same').get('sync_state') == 'disconnected')
        info = executor.job_local_info('same')
        assert info['running'] is None and info['execution_state'] == 'unknown'
        with audit.scoped_root(root / 'another-session'):
            assert executor.job_local_info('same')['error'] == 'local_job_not_found'
        assert executor.job_local_info('same')['job_output_id'] == value['job_output_id']
    finally:
        await executor.close_collectors()
