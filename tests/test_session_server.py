import asyncio
from pathlib import Path
import json
import subprocess
import sqlite3
from unittest.mock import AsyncMock

import pytest
from mcp import types
from mcp.server.fastmcp.exceptions import ToolError
from mcp.server.lowlevel.server import request_ctx
from mcp.shared.context import RequestContext

from remote_workbench import audit
from remote_workbench.config import HostConfig, ServerConfig
from remote_workbench.server import require_connection, workspace_executor
from remote_workbench.session_server import create_server
from remote_workbench.ssh import SSHExecutor
from remote_workbench.tools import commands, files, git, search, patch
from remote_workbench.workspace_router import SessionRouter


async def call(server, thread, name, **arguments):
    meta = types.RequestParams.Meta(**({"threadId": thread, "sessionId": "test-transport"} if thread else {}))
    token = request_ctx.set(RequestContext(request_id=1, meta=meta, session=None, lifespan_context=None))
    try:
        blocks = await server.call_tool(name, arguments)
        assert len(blocks) == 1
        return json.loads(blocks[0].text)
    finally:
        request_ctx.reset(token)


def router_at(path):
    return SessionRouter(ServerConfig(hosts={"a": HostConfig("host-a"), "b": HostConfig("host-b")}), path)


async def test_schema_context_injection_and_missing_identity_fail_closed(tmp_path):
    router = router_at(tmp_path)
    server = create_server(router)
    tools = await server.list_tools()
    assert len(tools) == 19
    for tool in tools:
        fields = tool.inputSchema["properties"]
        assert not {"ctx", "thread_id", "threadId", "workspace_id", "session_id"} & fields.keys()
        assert ("host" in fields) == (tool.name == "workspace_switch")
        assert tool.outputSchema is None
    with pytest.raises(ToolError, match="threadId is unavailable"):
        await call(server, None, "exec_command", command="must never run")
    assert not router._executors
    info = await call(server, "a", "workspace_info")
    assert info["bound"] is False
    with pytest.raises(ToolError, match="no remote workspace"):
        await call(server, "a", "exec_command", command="must never run")
    assert not router._executors


async def test_history_limit_schema_and_validation(tmp_path):
    router = router_at(tmp_path)
    server = create_server(router)
    tool = next(tool for tool in await server.list_tools() if tool.name == "workspace_history")
    limit = tool.inputSchema["properties"]["limit"]
    assert limit["type"] == "integer"
    assert limit["default"] == 20
    assert (await call(server, "a", "workspace_history", limit=0))["targets"] == []
    with pytest.raises(ToolError, match="non-negative integer"):
        await call(server, "a", "workspace_history", limit=-1)
    assert not router._executors


@pytest.fixture
async def project(local, monkeypatch):
    _, root = local
    for module in (commands, files, git, search, patch):
        monkeypatch.setattr(module, "require_connection", require_connection)
    monkeypatch.setattr(SSHExecutor, "check_connection", AsyncMock(return_value=(True, "connected")))
    source = root / "source"
    source.mkdir()
    (source / "value.txt").write_text("original\n")
    other = root / "other"
    other.mkdir()
    (other / "value.txt").write_text("other\n")
    router = router_at(root / "local-state")
    server = create_server(router, viewer_url="http://127.0.0.1:9999")
    return router, server, source, other


async def test_threads_private_copies_switch_restart_and_guarded_edits(project):
    router, server, source, other = project
    a, b = await asyncio.gather(*(call(server, thread, "workspace_switch", host="a", cwd=str(source)) for thread in ("a", "b")))
    assert a["cwd"] != b["cwd"]
    assert a["workspace_id"] != b["workspace_id"]
    assert a["revision"] == b["revision"] == 1
    assert f"/w/{a['workspace_id']}/?id=" in a["operation_log"]
    read = await call(server, "a", "read_file", path="value.txt")
    changed = await call(server, "a", "patch_file", path="value.txt", old="original", new="edited", expected_sha256=read["sha256"])
    assert not changed.get("error"), changed
    conflict = await call(server, "a", "patch_file", path="value.txt", old="edited", new="stale", expected_sha256=read["sha256"])
    assert conflict.get("error")
    assert (source / "value.txt").read_text() == "original\n"
    assert Path(b["cwd"], "value.txt").read_text() == "original\n"
    moved = await call(server, "a", "workspace_switch", host="b", cwd=str(other), expected_revision=1)
    assert moved["host"] == "b" and moved["revision"] == 2
    assert (await call(server, "b", "workspace_info"))["cwd"] == b["cwd"]
    restarted = create_server(router_at(router.state_dir))
    assert (await call(restarted, "a", "workspace_info"))["cwd"] == moved["cwd"]
    restored = await call(restarted, "a", "workspace_switch", host="a", cwd=str(source))
    assert restored["cwd"] == a["cwd"] and restored["revision"] == 3
    assert Path(restored["cwd"], "value.txt").read_text() == "edited\n"
    assert len((await call(restarted, "a", "workspace_history"))["targets"]) == 3
    limited = (await call(restarted, "a", "workspace_history", limit=2))["targets"]
    assert [target["revision"] for target in limited] == [3, 2]
    assert workspace_executor.get() is None
    own = await call(server, "a", "audit_get", operation_id=changed["operation_id"])
    assert own["thread_id"] == "a" and own["revision"] == 1
    with pytest.raises(ToolError):
        await call(server, "b", "audit_get", operation_id=changed["operation_id"])


async def test_failed_switch_keeps_previous_target(project):
    router, server, source, _ = project
    before = await call(server, "a", "workspace_switch", host="a", cwd=str(source))
    for host, cwd, revision in [("unknown", str(source), 1), ("a", str(source / "missing"), 1), ("a", str(source), 0)]:
        with pytest.raises(ToolError):
            await call(server, "a", "workspace_switch", host=host, cwd=cwd, expected_revision=revision)
        after = await call(server, "a", "workspace_info")
        assert after["cwd"] == before["cwd"] and after["revision"] == 1


async def test_repository_subdirectory_keeps_copy_outside_source_repo(project):
    router, server, source, _ = project
    subdir = source / "src"
    subdir.mkdir()
    (subdir / "module.py").write_text("VALUE = 1\n")
    subprocess.run(["git", "init", "-q", str(source)], check=True)
    subprocess.run(["git", "-C", str(source), "add", "."], check=True)
    subprocess.run(["git", "-C", str(source), "-c", "user.name=Test", "-c", "user.email=test@example.invalid", "commit", "-qm", "baseline"], check=True)
    result = await call(server, "a", "workspace_switch", host="a", cwd=str(subdir))
    assert result["cwd"].endswith("/project/src")
    assert not Path(result["project_root"]).is_relative_to(source)
    assert Path(result["cwd"], "module.py").read_text() == "VALUE = 1\n"


async def test_job_uses_origin_after_switch_and_cannot_cross_thread(project):
    router, server, source, other = project
    original = await call(server, "a", "workspace_switch", host="a", cwd=str(source))
    job = await call(server, "a", "start_job", command="printf origin-job", job_id="same")
    assert not job.get("error"), job
    await call(server, "a", "workspace_switch", host="b", cwd=str(other))
    for _ in range(40):
        status = await call(server, "a", "job_status", job_id="same")
        if not status.get("running"):
            break
        await asyncio.sleep(0.05)
    assert status["exit_code"] == 0 and status["stdout_tail"] == "origin-job"
    origin = router.registry.get_job(original["workspace_id"], "same")
    assert origin.host == "a" and origin.cwd == original["cwd"]
    with pytest.raises(ToolError, match="Unknown job"):
        await call(server, "b", "job_status", job_id="same")


async def test_switch_waits_for_foreground_call_across_router_instances(project):
    router, server, source, other = project
    state = await call(server, "a", "workspace_switch", host="a", cwd=str(source))
    entered, release = asyncio.Event(), asyncio.Event()
    executor = router.snapshot(router.registry.get(state["workspace_id"])).workspace.executor
    original = executor.run
    async def blocked(*args, **kwargs):
        entered.set()
        await release.wait()
        return await original(*args, **kwargs)
    executor.run = blocked
    first = asyncio.create_task(call(server, "a", "exec_command", command="pwd"))
    await asyncio.wait_for(entered.wait(), 2)
    other_server = create_server(router_at(router.state_dir))
    switching = asyncio.create_task(call(other_server, "a", "workspace_switch", host="b", cwd=str(other)))
    await asyncio.sleep(0.1)
    assert not switching.done()
    release.set()
    result, switched = await asyncio.gather(first, switching)
    assert result["stdout"].strip() == state["cwd"]
    assert switched["host"] == "b"


async def test_snapshot_test_scope_and_response_byte_accounting(project):
    router, server, source, other = project
    (source / ".gitignore").write_text("__pycache__/\n.remote-workbench-state/\n")
    (source / "tests").mkdir()
    (source / "tests/test_one.py").write_text("import unittest\nclass Check(unittest.TestCase):\n def test_one(self): self.assertEqual(1+1,2)\n")
    subprocess.run(["git", "init", "-q", str(source)], check=True)
    subprocess.run(["git", "-C", str(source), "add", "."], check=True)
    subprocess.run(["git", "-C", str(source), "-c", "user.name=Test", "-c", "user.email=test@example.invalid", "commit", "-qm", "baseline"], check=True)
    state = await call(server, "a", "workspace_switch", host="a", cwd=str(source))
    workspace_id = state["workspace_id"]
    before = router.registry.metrics(workspace_id)
    output = await call(server, "a", "exec_command", command="printf '你好'")
    after = router.registry.metrics(workspace_id)
    expected = len(json.dumps(output, ensure_ascii=False, separators=(",", ":")).encode())
    assert after["result_text_bytes"] - before["result_text_bytes"] == expected
    assert after["recorded_responses"] - before["recorded_responses"] == 1
    await call(server, "a", "read_output", output_id=output["output_id"], max_bytes=256)
    await call(server, "a", "read_output", output_id=output["output_id"], max_bytes=256)
    assert router.registry.metrics(workspace_id)["repeated_output_reads"] == 1
    with audit.scoped_root(router.audit_dir(workspace_id)):
        with (audit.directory(output["output_id"]) / "stdout").open("ab") as stream:
            stream.write(b"new")  # Simulate a growing saved output stream.
    await call(server, "a", "read_output", output_id=output["output_id"], max_bytes=256)
    assert router.registry.metrics(workspace_id)["repeated_output_reads"] == 1
    tested = await call(server, "a", "exec_command", command="python3 -m unittest discover -s tests -v")
    assert tested["exit_code"] == 0
    renamed = await call(server, "a", "exec_command", command="git mv value.txt renamed.txt")
    assert renamed["exit_code"] == 0
    snapshot = await call(server, "a", "workspace_snapshot")
    assert snapshot["git"]["modified_files"] == 1 and snapshot["git"]["status_complete"]
    assert snapshot["last_test"]["operation_id"] == tested["operation_id"]
    assert snapshot["last_test"]["status"] == "passed"
    assert snapshot["last_test"]["applies_to_current_code"] is None
    assert snapshot["communication"]["recorded_responses"] > 0
    await call(server, "a", "workspace_switch", host="b", cwd=str(other))
    assert (await call(server, "a", "workspace_snapshot"))["last_test"] is None
    restarted = create_server(router_at(router.state_dir))
    await call(restarted, "a", "workspace_switch", host="a", cwd=str(source))
    assert (await call(restarted, "a", "workspace_snapshot"))["last_test"]["operation_id"] == tested["operation_id"]


async def test_accounting_failure_does_not_turn_a_completed_write_into_failure(project, monkeypatch):
    router, server, source, _ = project
    state = await call(server, "a", "workspace_switch", host="a", cwd=str(source))
    def broken(*args, **kwargs):
        raise sqlite3.OperationalError("accounting unavailable")
    monkeypatch.setattr(router.registry, "record_call", broken)
    result = await call(server, "a", "write_file", path="value.txt", content="written once\n")
    assert result.get("success")
    assert Path(state["cwd"], "value.txt").read_text() == "written once\n"
