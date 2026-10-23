"""Opt-in test of a local MCP bound to a fresh remote folder. No remote agent."""
from __future__ import annotations
import argparse
import asyncio
from contextlib import asynccontextmanager
from datetime import datetime
import json
import os
from pathlib import Path
import sys

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
if __package__:
    from .live_remote_test import connect, invoke, py
else:
    from live_remote_test import connect, invoke, py

PROJECT = Path(__file__).resolve().parents[1]


@asynccontextmanager
async def bound(path):
    parameters = StdioServerParameters(command=sys.executable,
        args=["-m", "remote_workbench.workspace", "--workspace", str(path)],
        cwd=str(PROJECT), env=dict(os.environ, FASTMCP_LOG_LEVEL="ERROR"))
    async with stdio_client(parameters) as streams:
        async with ClientSession(*streams) as session:
            await session.initialize()
            yield session


async def run(args):
    target = Path(args.directory).resolve()
    target.mkdir(parents=True, exist_ok=True)
    report = {"host": args.host, "time": datetime.now().astimezone().isoformat(), "checks": [],
              "control": "local", "remote_codex_installed_or_started": False}
    def save():
        report["passed"] = sum(item["status"] == "PASS" for item in report["checks"])
        report["failed"] = sum(item["status"] == "FAIL" for item in report["checks"])
        (target / "WORKSPACE_TEST.json").write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n")
    async def check(name, function):
        try:
            detail = await function()
            report["checks"].append({"name": name, "status": "PASS", "details": detail})
            print("PASS " + name, flush=True)
        except Exception as exc:
            report["checks"].append({"name": name, "status": "FAIL", "error": str(exc)[:1500]})
            print("FAIL " + name + ": " + str(exc)[:400], flush=True)
        save()
    async with connect() as bootstrap:
        parent = repr(args.parent)
        result = await invoke(bootstrap, "ssh_run", host=args.host, cwd=args.parent,
            command=py('import tempfile,json; root=tempfile.mkdtemp(prefix="remote-workbench-workspace-", dir=' + parent + '); print(json.dumps({"root":root}))'))
        assert result["exit_code"] == 0, result
        root = json.loads(result["stdout"])["root"]
        baseline = await bootstrap.list_tools()
        report["original_schema_bytes"] = len(json.dumps([t.model_dump(exclude_none=True) for t in baseline.tools]).encode())
    report["remote_workspace"] = root
    path = target / "workspace.json"
    if path.exists():
        raise RuntimeError("Refusing to replace an existing workspace manifest")
    path.write_text(json.dumps({"version": 1, "name": "remote-test", "host": args.host, "cwd": root}, indent=2) + "\n")
    save()
    async with bound(path) as session:
        async def call(name, **kwargs):
            return await invoke(session, name, **kwargs)
        async def command(text, **kwargs):
            return await call("exec_command", command=text, **kwargs)
        async def info():
            value = await call("workspace_info")
            assert value["ready"] and value["control"] == "local", value
            assert value["probe"].splitlines() == [root, value["shell"]], value
            assert value["shell"].startswith("/"), value
            return value
        await check("local_control_remote_directory_and_account_shell", info)
        async def schema():
            tools = await session.list_tools()
            report["bound_schema_bytes"] = len(json.dumps([t.model_dump(exclude_none=True) for t in tools.tools]).encode())
            assert len(tools.tools) == 16
            assert all("host" not in t.inputSchema["properties"] for t in tools.tools)
            assert report["bound_schema_bytes"] < report["original_schema_bytes"]
            return {"tools": len(tools.tools), "schema_bytes": report["bound_schema_bytes"]}
        await check("compact_tools_without_host_arguments", schema)
        async def write_read():
            value = await call("write_file", path="default.txt", content="before\n")
            assert value["success"], value
            value = await call("read_file", path="default.txt")
            assert "before" in str(value) and value.get("sha256"), value
            return {"relative_path": "default.txt", "output_id": value["output_id"]}
        await check("relative_files_default_to_remote_root", write_read)
        async def no_reuse():
            first = await command("mkdir -p child && cd child && export REMOTE_WORKBENCH_WORKSPACE_TEST=carry && pwd")
            assert first["stdout"].strip() == root + "/child", first
            second = await command("printf '%s\\n' \"$PWD\" \"${REMOTE_WORKBENCH_WORKSPACE_TEST-unset}\"")
            assert second["stdout"].splitlines() == [root, "unset"], second
            third = await command("pwd", cwd="child")
            assert third["stdout"].strip() == root + "/child", third
            return {"fresh_shell": True, "relative_cwd": root + "/child"}
        await check("fresh_shell_and_relative_cwd", no_reuse)
        async def git_patch():
            result = await command("git init -q && git add default.txt")
            assert result["exit_code"] == 0, result
            value = await call("apply_patch", patch="--- a/default.txt\n+++ b/default.txt\n@@ -1 +1 @@\n-before\n+after\n")
            assert not value.get("error"), value
            value = await call("git_diff", mode="patch")
            assert "+after" in value["diff"], value
            return {"omitted_cwd_works": True, "output_id": value["output_id"]}
        await check("patch_and_diff_share_default_repository", git_patch)
        async def guards():
            value = await call("read_file", path="default.txt")
            stale = await call("patch_file", path="default.txt", old="after", new="wrong", expected_sha256="0" * 64)
            assert stale.get("error"), stale
            changed = await call("patch_file", path="default.txt", old="after", new="final", expected_sha256=value["sha256"])
            assert changed.get("success"), changed
            return {"stale_edit_refused": True}
        await check("guarded_remote_edit", guards)
        async def search():
            found = await call("grep", pattern="final", path="default.txt")
            assert found["count"] == 1, found
            found = await call("find_files", pattern="default.txt", max_depth=1)
            assert found["count"] == 1, found
            return {"same_default_directory": True}
        await check("search_defaults_to_bound_root", search)
        async def jobs():
            job = await call("start_job", command="pwd", cwd="child")
            assert not job.get("error"), job
            for _ in range(30):
                value = await call("job_status", job_id=job["job_id"])
                if value.get("exit_code") is not None:
                    break
                await asyncio.sleep(0.1)
            assert value.get("exit_code") == 0 and root + "/child" in str(value), value
            return {"job_id": job["job_id"], "exit_code": value["exit_code"]}
        await check("explicit_job_uses_bound_workspace", jobs)
        async def audit():
            value = await command("pwd")
            saved = await call("audit_get", operation_id=value["output_id"])
            assert saved["actual_cwd"] == root and saved["command"] == "pwd", saved
            return {"actual_cwd": saved["actual_cwd"], "command": saved["command"]}
        await check("audit_has_actual_remote_command_and_cwd", audit)
        async def isolation():
            result = await command("mkdir -p other")
            assert result["exit_code"] == 0
            alternate = target / "other-workspace.json"
            data = json.loads(path.read_text())
            data.update(name="example-host-other-test", cwd=root + "/other")
            alternate.write_text(json.dumps(data, indent=2) + "\n")
            async with bound(alternate) as second:
                a, b = await asyncio.gather(command("pwd"), invoke(second, "exec_command", command="pwd"))
                assert a["stdout"].strip() == root and b["stdout"].strip() == root + "/other", (a, b)
            return {"independent_defaults_same_host": True}
        await check("two_workspaces_on_same_host_remain_independent", isolation)
    save()
    if report["failed"]:
        raise SystemExit(1)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--host", required=True)
    parser.add_argument("--parent", required=True)
    parser.add_argument("--directory", required=True)
    args = parser.parse_args()
    os.environ.setdefault("REMOTE_WORKBENCH_AUDIT_DIR", str(PROJECT / ".remote-workbench-audit" / "example-host-live"))
    asyncio.run(run(args))
