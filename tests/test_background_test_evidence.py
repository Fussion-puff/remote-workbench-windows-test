import asyncio
import json
import shlex
import uuid
from pathlib import Path

import pytest

from remote_workbench import audit, job_collector, test_evidence
from remote_workbench.remote_client import edit
from remote_workbench.tools import commands
from remote_workbench.workspace_registry import Registry, WorkspaceTarget


async def eventually(predicate, timeout=12):
    deadline = asyncio.get_running_loop().time() + timeout
    while asyncio.get_running_loop().time() < deadline:
        if predicate():
            return
        await asyncio.sleep(0.05)
    raise AssertionError("background test attempt did not reach its terminal state")


def prepare_gated_attempt(executor, root, job_id):
    project = root / "project"
    project.mkdir()
    source = root / "source"
    source.mkdir()
    executor.config.default_cwd = str(project)
    executor.remote_state_dir = str(root / "remote-state")
    ready = root / "test-ready"
    release = root / "test-release"
    started = root / "test-started"
    module = project / "test_case.py"
    module.write_text(
        "import os, time, unittest\n"
        "from pathlib import Path\n"
        "Path(os.environ['RWB_TEST_READY']).write_text('ready')\n"
        "with Path(os.environ['RWB_TEST_STARTED']).open('a') as output: output.write('started\\n')\n"
        "gate = Path(os.environ['RWB_TEST_GATE'])\n"
        "while not gate.exists(): time.sleep(0.02)\n"
        "class TestGated(unittest.TestCase):\n"
        "    def test_after_gate(self): self.assertTrue(True)\n"
    )
    command = (f"RWB_TEST_READY={shlex.quote(str(ready))} "
               f"RWB_TEST_GATE={shlex.quote(str(release))} "
               f"RWB_TEST_STARTED={shlex.quote(str(started))} "
               "python3 -m unittest -v test_case")
    registry = Registry(root / "registry.sqlite3")
    state = registry.get_or_create("gated-background-test-thread")
    target = WorkspaceTarget(uuid.uuid4().hex, state.workspace_id, "a", str(source),
                             str(project), str(project))
    registry.commit_switch(state.workspace_id, 0, target)
    before = {"code": {"complete": True, "digest": "gated"},
              "config_digest": "gated-config", "observed_at": 1.0}
    with audit.scoped_root(root / "audit"):
        attempt_id = audit.create(
            kind="tool", tool="background_test_attempt", workspace_id=state.workspace_id,
            target_id=target.target_id, revision=1, host="a", cwd=str(project),
            command=command, job_id=job_id, status="running",
        )
        attempt = registry.begin_test_attempt(
            state.workspace_id, target, attempt_id=attempt_id, job_id=job_id,
            command=command, cwd=str(project), execution_kind="background",
            before=before, revision=1,
        )
        nonce = uuid.uuid4().hex
        registry.register_job(
            state.workspace_id, job_id, target, origin_nonce=nonce,
            launch_cwd=str(project), remote_state_dir=executor.remote_state_dir,
            ssh_target=executor.config.ssh_target,
        )
    origin = {
        "workspace_id": state.workspace_id, "job_id": job_id,
        "target_id": target.target_id, "host": target.host,
        "source_cwd": target.source_cwd, "target_cwd": target.cwd,
        "project_root": target.project_root, "origin_nonce": nonce,
        "launch_cwd": str(project), "actual_cwd": None,
        "remote_state_dir": executor.remote_state_dir,
        "ssh_target": executor.config.ssh_target,
        "test_attempt_id": attempt["attempt_id"],
        "registry_path": str(registry.path),
    }
    return registry, state, target, attempt, origin, project, ready, release, started, command


async def launch_gated_attempt(executor, root, job_id):
    (registry, state, target, attempt, origin, project, ready, release, started,
     command) = prepare_gated_attempt(executor, root, job_id)
    operation_token = audit.operation_id.set(attempt["attempt_id"])
    try:
        with job_collector.scoped_origin(origin):
            launched = await edit(
                executor,
                {"op": "job_start", "job_id": job_id, "command": command, "timeout": 30},
                cwd=str(project), max_bytes=8192,
            )
    finally:
        audit.operation_id.reset(operation_token)
    try:
        assert launched.get("job_output_id"), launched
        assert launched["job_id"] == job_id
        await eventually(ready.exists)
    except BaseException:
        release.touch(exist_ok=True)
        cleanup_token = audit.operation_id.set(attempt["attempt_id"])
        with job_collector.scoped_origin(origin):
            try:
                await asyncio.wait_for(commands.ssh_stop_job(job_id), 5)
            except Exception:
                pass
        audit.operation_id.reset(cleanup_token)
        await job_collector.close(executor)
        raise
    return (registry, state, target, attempt, origin, project, ready, release, started,
            command, launched)


@pytest.mark.parametrize(("fails", "expected_process", "expected_result", "fail_first_write"), [
    (False, "success", "success", False),
    (True, "failed", "failed", False),
    (False, "success", "success", True),
])
@pytest.mark.asyncio
async def test_background_test_completion_is_persisted_automatically(local, monkeypatch,
                                                                      fails, expected_process,
                                                                      expected_result,
                                                                      fail_first_write):
    executor, root = local
    project = root / "project"
    project.mkdir()
    executor.config.default_cwd = str(project)
    executor.remote_state_dir = str(root / "remote-state")
    source = root / "source"
    source.mkdir()
    module = project / "test_case.py"
    assertion = "self.fail('expected failure')" if fails else "self.assertTrue(True)"
    module.write_text(
        "import time, unittest\n"
        "class TestObserved(unittest.TestCase):\n"
        "    def test_result(self):\n"
        "        time.sleep(0.4)\n"
        f"        {assertion}\n"
    )
    command = "python3 -m unittest -v test_case"
    job_id = "background-test-" + uuid.uuid4().hex[:12]
    nonce = uuid.uuid4().hex
    registry = Registry(root / "registry.sqlite3")
    state = registry.get_or_create("background-test-thread")
    target = WorkspaceTarget(uuid.uuid4().hex, state.workspace_id, "a", str(source),
                             str(project), str(project))
    registry.commit_switch(state.workspace_id, 0, target)
    before = {"code": {"complete": True, "digest": "known"},
              "config_digest": "known-config", "observed_at": 1.0}
    with audit.scoped_root(root / "audit"):
        attempt_id = audit.create(
            kind="tool", tool="background_test_attempt", workspace_id=state.workspace_id,
            target_id=target.target_id, revision=1, host="a", cwd=str(project),
            command=command, job_id=job_id, status="running",
        )
        attempt = registry.begin_test_attempt(
            state.workspace_id, target, attempt_id=attempt_id, job_id=job_id,
            command=command, cwd=str(project), execution_kind="background",
            before=before, revision=1,
        )
        # Model the first job_start acknowledgement being lost. The same saved
        # attempt must return to running only after the collector verifies the
        # remote process identity, and it must learn the stable output handle.
        attempt = registry.update_test_attempt(
            state.workspace_id, attempt["attempt_id"], state="unknown",
            error="invalid_worker_output", expected_target_id=target.target_id,
            expected_job_id=job_id,
        )
        registry.register_job(
            state.workspace_id, job_id, target, origin_nonce=nonce,
            launch_cwd=str(project), remote_state_dir=executor.remote_state_dir,
            ssh_target=executor.config.ssh_target,
        )
        origin = {
            "workspace_id": state.workspace_id, "job_id": job_id,
            "target_id": target.target_id, "host": target.host,
            "source_cwd": target.source_cwd, "target_cwd": target.cwd,
            "project_root": target.project_root, "origin_nonce": nonce,
            "launch_cwd": str(project), "actual_cwd": None,
            "remote_state_dir": executor.remote_state_dir,
            "ssh_target": executor.config.ssh_target,
            "test_attempt_id": attempt["attempt_id"],
            "registry_path": str(registry.path),
        }
        captures = []

        async def capture_after(*args):
            captures.append(True)
            return {**before, "observed_at": 2.0}

        monkeypatch.setattr(test_evidence, "capture_target", capture_after)
        if fail_first_write:
            original_update = Registry.update_test_attempt
            failed = [False]

            def fail_once(registry_arg, *args, **kwargs):
                if kwargs.get("observation") is not None and not failed[0]:
                    failed[0] = True
                    import sqlite3
                    raise sqlite3.OperationalError("injected evidence write failure")
                return original_update(registry_arg, *args, **kwargs)

            monkeypatch.setattr(Registry, "update_test_attempt", fail_once)
        operation_token = audit.operation_id.set(attempt_id)
        try:
            with job_collector.scoped_origin(origin):
                launched = await edit(
                    executor,
                    {"op": "job_start", "job_id": job_id, "command": command, "timeout": 15},
                    cwd=str(project), max_bytes=8192,
                )
            assert launched.get("job_output_id"), launched
            assert launched["job_id"] == job_id
            await eventually(lambda: (
                (current := registry.test_attempt(state.workspace_id, attempt_id))["state"] in
                {"running", "finished", "failed", "cancelled", "timed_out"} and
                current.get("output_id") == launched["job_output_id"]
            ))
            running_recovered = registry.test_attempt(state.workspace_id, attempt_id)
            assert running_recovered["error"] is None

            # Poll only the local durable attempt row: no job_status/wait call
            # drives the collector or the evidence writeback.
            if fail_first_write:
                await eventually(lambda: (
                    job_collector.local_info(executor, job_id).get("sync_state") == "complete" and
                    json.loads((audit.directory(launched["job_output_id"]) / "record.json").read_text())
                    .get("test_evidence_state") == "unknown"
                ))
                saved_audit = json.loads(
                    (audit.directory(launched["job_output_id"]) / "record.json").read_text())
                assert saved_audit["test_evidence_state"] == "unknown"
                assert saved_audit["test_evidence_error"].startswith("attempt_evidence_persist_failed:")
                assert registry.test_attempt(state.workspace_id, attempt_id)["state"] == "running"
                retried = await job_collector.ensure(executor, job_id, origin=origin)
                assert retried["sync_state"] == "complete"
            await eventually(lambda: registry.test_attempt(
                state.workspace_id, attempt_id)["state"] in
                {"finished", "failed", "cancelled", "timed_out"})
            if fail_first_write:
                assert job_collector.local_info(executor, job_id)["sync_state"] == "complete"
            saved = registry.test_attempt(state.workspace_id, attempt_id)
            observation = registry.last_test(state.workspace_id, target.target_id)
            assert saved["process_exit_status"] == expected_process
            assert saved["test_result"] == expected_result
            assert saved["output_id"] == launched["job_output_id"]
            assert saved["after"]["observed_at"] == 2.0
            assert observation["attempt_id"] == attempt_id
            assert observation["process_exit_status"] == expected_process
            assert observation["test_result"] == expected_result
            assert observation["reliable_completion"] is True
            assert len(captures) == (2 if fail_first_write else 1)
            assert job_collector.local_info(executor, job_id)["sync_state"] == "complete"

            # Repeated recovery/status synchronization is idempotent and must
            # not replace the completion-time fingerprint.
            recovered = await job_collector.ensure(executor, job_id, origin=origin)
            assert recovered["sync_state"] == "complete"
            assert len(captures) == (2 if fail_first_write else 1)
            saved_audit = json.loads(
                (audit.directory(launched["job_output_id"]) / "record.json").read_text())
            assert saved_audit.get("test_evidence_state") == "complete"
            assert registry.last_test(state.workspace_id, target.target_id) == observation
        finally:
            audit.operation_id.reset(operation_token)
            await job_collector.close(executor)


@pytest.mark.asyncio
async def test_gated_background_test_stop_is_recorded_without_status_polling(local):
    executor, root = local
    job_id = "cancelled-test-" + uuid.uuid4().hex[:12]
    (registry, state, target, attempt, origin, project, ready, release, started,
     command, launched) = await launch_gated_attempt(executor, root, job_id)
    try:
        stop_token = audit.operation_id.set(attempt["attempt_id"])
        try:
            with job_collector.scoped_origin(origin):
                stopped = await commands.ssh_stop_job(job_id)
        finally:
            audit.operation_id.reset(stop_token)
        assert stopped.get("execution_state") == "finished", stopped
        assert stopped.get("cleanup_state") != "unconfirmed", stopped

        await eventually(lambda: registry.test_attempt(
            state.workspace_id, attempt["attempt_id"])["state"] == "cancelled")
        saved = registry.test_attempt(state.workspace_id, attempt["attempt_id"])
        observation = registry.last_test(state.workspace_id, target.target_id)
        assert saved["process_exit_status"] == "cancelled"
        assert saved["test_result"] == "unknown"
        assert saved["finished_at"] is not None
        assert saved["output_id"] == launched["job_output_id"]
        assert observation["attempt_id"] == attempt["attempt_id"]
        assert observation["process_exit_status"] == "cancelled"
        assert observation["reliable_completion"] is True
        assert started.read_text().splitlines() == ["started"]
        assert job_collector.local_info(executor, job_id)["sync_state"] == "complete"
    finally:
        release.touch(exist_ok=True)
        cleanup_token = audit.operation_id.set(attempt["attempt_id"])
        try:
            if registry.test_attempt(state.workspace_id, attempt["attempt_id"])["state"] not in {
                    "finished", "failed", "cancelled", "timed_out"}:
                with job_collector.scoped_origin(origin):
                    try:
                        await asyncio.wait_for(commands.ssh_stop_job(job_id), 5)
                    except Exception:
                        pass
        finally:
            audit.operation_id.reset(cleanup_token)
            await job_collector.close(executor)


@pytest.mark.asyncio
async def test_gated_background_test_reconnects_after_local_collector_disconnect(local):
    executor, root = local
    job_id = "reconnected-test-" + uuid.uuid4().hex[:12]
    (registry, state, target, attempt, origin, project, ready, release, started,
     command, launched) = await launch_gated_attempt(executor, root, job_id)
    attempt_id = attempt["attempt_id"]
    output_id = launched["job_output_id"]
    try:
        # Closing the collector only interrupts this local log reader; it must
        # leave the remote supervised unittest running behind its gate.
        await job_collector.close(executor)
        await eventually(lambda: (
            registry.test_attempt(state.workspace_id, attempt_id)["state"] == "unknown" and
            registry.test_attempt(state.workspace_id, attempt_id)["finished_at"] is None and
            job_collector.local_info(executor, job_id).get("sync_state") == "disconnected"
        ))
        unknown = registry.test_attempt(state.workspace_id, attempt_id)
        assert unknown["output_id"] == output_id
        assert unknown["process_exit_status"] == "unknown"
        assert unknown["finished_at"] is None
        assert registry.last_test(state.workspace_id, target.target_id) is None

        reconnect_token = audit.operation_id.set(attempt_id)
        try:
            with job_collector.scoped_origin(origin):
                reconnected = await job_collector.ensure(executor, job_id, origin=origin)
        finally:
            audit.operation_id.reset(reconnect_token)
        assert reconnected.get("job_output_id") == output_id, reconnected
        await eventually(lambda: registry.test_attempt(
            state.workspace_id, attempt_id)["state"] == "running")
        assert registry.test_attempt(state.workspace_id, attempt_id)["output_id"] == output_id

        release.touch(exist_ok=True)
        await eventually(lambda: registry.test_attempt(
            state.workspace_id, attempt_id)["state"] == "finished")
        saved = registry.test_attempt(state.workspace_id, attempt_id)
        observation = registry.last_test(state.workspace_id, target.target_id)
        assert saved["process_exit_status"] == "success"
        assert saved["test_result"] == "success"
        assert saved["output_id"] == output_id
        assert saved["finished_at"] is not None
        assert observation["attempt_id"] == attempt_id
        assert observation["reliable_completion"] is True
        assert job_collector.local_info(executor, job_id)["job_output_id"] == output_id
        assert started.read_text().splitlines() == ["started"]
    finally:
        release.touch(exist_ok=True)
        cleanup_token = audit.operation_id.set(attempt_id)
        try:
            if registry.test_attempt(state.workspace_id, attempt_id)["state"] not in {
                    "finished", "failed", "cancelled", "timed_out"}:
                with job_collector.scoped_origin(origin):
                    try:
                        await asyncio.wait_for(commands.ssh_stop_job(job_id), 5)
                    except Exception:
                        pass
        finally:
            audit.operation_id.reset(cleanup_token)
            await job_collector.close(executor)
