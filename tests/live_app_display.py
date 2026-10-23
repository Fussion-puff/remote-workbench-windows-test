"""Real SSH/MCP evidence for bounded model replies and full human audit output.

No Codex/model process is installed remotely. The optional HTTP/UI checks are
separate so viewing full logs never requires a model tool call.
"""
from __future__ import annotations
import argparse
import asyncio
from contextlib import asynccontextmanager
from datetime import datetime
import hashlib
import json
import os
from pathlib import Path
import sys
import tempfile

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
import yaml
from remote_workbench import audit
from remote_workbench.config import load_config
from dataclasses import asdict

if __package__:
    from .live_remote_test import connect, invoke, py
else:
    from live_remote_test import connect, invoke, py

PROJECT = Path(__file__).resolve().parents[1]


@asynccontextmanager
async def workspace(path):
    params = StdioServerParameters(command=sys.executable,
        args=["-m", "remote_workbench.workspace", "--workspace", str(path)],
        cwd=str(PROJECT), env=dict(os.environ, FASTMCP_LOG_LEVEL="ERROR"))
    async with stdio_client(params) as streams:
        async with ClientSession(*streams) as session:
            await session.initialize()
            yield session


def unpack(response):
    if response.isError:
        raise AssertionError(str(response.content)[:1000])
    if response.structuredContent is not None:
        return response.structuredContent
    return json.loads(next(c.text for c in response.content if c.type == "text"))


async def run(args):
    output = Path(args.output).resolve()
    output.mkdir(parents=True, exist_ok=True)
    report = {"time": datetime.now().astimezone().isoformat(), "host": args.host,
              "remote_codex_installed_or_started": False, "checks": [], "comparisons": []}
    wire = {}
    def save():
        report["passed"] = sum(c["status"] == "PASS" for c in report["checks"])
        report["failed"] = sum(c["status"] == "FAIL" for c in report["checks"])
        (output / "APP_DISPLAY_TEST.json").write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n")
        (output / "APP_DISPLAY_WIRE.json").write_text(json.dumps(wire, ensure_ascii=False, indent=2) + "\n")
    def passed(name, details=None):
        report["checks"].append({"name": name, "status": "PASS", "details": details})
        print("PASS " + name, flush=True)
        save()
    async with connect() as bootstrap:
        created = await invoke(bootstrap, "ssh_run", host=args.host, cwd=args.parent,
            command=py("import tempfile,json; print(json.dumps({'root':tempfile.mkdtemp(prefix='remote-workbench-app-display-', dir=" + repr(args.parent) + ")}))"))
        assert created["exit_code"] == 0, created
        root = json.loads(created["stdout"])["root"]
    report.update(remote_workspace=root, audit_directory=str(audit.root()))
    manifest = output / "app-display-workspace.json"
    manifest.write_text(json.dumps({"version": 1, "name": "example-host-app-display", "host": args.host, "cwd": root}, indent=2) + "\n")
    base = asdict(load_config().get_host(args.host))
    base.update(default_cwd=root, preamble="")
    with tempfile.TemporaryDirectory(prefix="remote-workbench-display-") as temporary:
        legacy_config = Path(temporary) / "hosts.yaml"
        legacy_config.write_text(yaml.safe_dump({"default_host": args.host, "hosts": {args.host: base}}))
        async with connect(legacy_config) as legacy, workspace(manifest) as bound:
            async def call(name, **kw):
                response = await bound.call_tool(name, kw)
                wire.setdefault("calls", []).append({"tool": name, "result": response.model_dump(mode="json", exclude_none=True)})
                return unpack(response)
            async def cmd(text, **kw):
                return await call("exec_command", command=text, **kw)
            tools = await bound.list_tools()
            oldtools = await legacy.list_tools()
            wire["tool_schemas"] = {"legacy": oldtools.model_dump(mode="json", exclude_none=True),
                                    "workspace": tools.model_dump(mode="json", exclude_none=True)}
            info = await call("workspace_info")
            assert info["ready"] and info["probe"].splitlines() == [root, info["shell"]], info
            assert info["shell"].startswith("/"), info
            passed("bound_remote_account_shell_without_remote_codex", {"root": root, "shell": info["shell"]})
            large = py("import sys; [print('OUTPUT-%05d: '%i+'远端测试-'*8) for i in range(3000)]; print('stderr-visible-marker',file=sys.stderr)")
            failed = "printf '%s\\n' 'EXPECTED_FAILURE_VISIBLE' >&2; exit 7"
            for label, text in [("large_output", large), ("failure", failed)]:
                old = await legacy.call_tool("ssh_run", {"host": args.host, "command": text})
                new = await bound.call_tool("exec_command", {"command": text})
                previous, current = unpack(old), unpack(new)
                wire[label] = {"arguments": {"command": text}, "legacy": old.model_dump(mode="json", exclude_none=True),
                               "workspace": new.model_dump(mode="json", exclude_none=True)}
                before = len(old.model_dump_json(exclude_none=True).encode())
                after = len(new.model_dump_json(exclude_none=True).encode())
                report["comparisons"].append({"scenario": label, "legacy_wire_bytes": before, "workspace_wire_bytes": after})
                if label == "large_output":
                    assert current["truncated"] and len((current["stdout"] + current["stderr"]).encode()) <= 2048, current
                    stdout = audit.directory(current["output_id"]) / "stdout"
                    assert stdout.stat().st_size > 300000
                    assert b"OUTPUT-02999" in stdout.read_bytes()
                    report.update(command_operation_id=current["operation_id"], command_output_id=current["output_id"],
                                  full_output_bytes=stdout.stat().st_size, full_output_sha256=hashlib.sha256(stdout.read_bytes()).hexdigest())
                    passed("bounded_reply_retains_complete_local_output", {"bytes": stdout.stat().st_size, "wire_bytes": after})
                else:
                    assert current["exit_code"] == 7 and "EXPECTED_FAILURE_VISIBLE" in current["stderr"], current
                    passed("nonzero_exit_and_stderr_preserved")
                assert new.structuredContent is None, "Duplicate structured channel still present"
            passed("one_model_content_channel")
            create = await call("write_file", path="sample.py", content="FACTOR = 2\nprint(FACTOR * 3)\n")
            assert create["success"], create
            setup = await cmd("git init -q && git add sample.py")
            assert setup["exit_code"] == 0, setup
            patch = "--- a/sample.py\n+++ b/sample.py\n@@ -1,2 +1,2 @@\n-FACTOR = 2\n+FACTOR = 5\n print(FACTOR * 3)\n"
            changed = await call("apply_patch", patch=patch)
            assert changed["success"], changed
            diff_path = audit.directory(changed["output_id"]) / "diff"
            assert diff_path.read_text() == patch
            report.update(diff_operation_id=changed["operation_id"], diff_output_id=changed["output_id"])
            passed("submitted_patch_available_to_viewer_without_echo", {"output_id": changed["output_id"]})
            result = await cmd("python3 sample.py")
            assert result["stdout"].strip() == "15", result
            passed("remote_program_result_after_patch")
            diff = await call("git_diff", mode="patch")
            assert "+FACTOR = 5" in diff["diff"] and "-FACTOR = 2" in diff["diff"], diff
            report["git_diff_output_id"] = diff["output_id"]
            passed("actual_worktree_diff_verified")
            stale = await call("patch_file", path="sample.py", old="FACTOR = 5", new="FACTOR = 8", expected_sha256="0" * 64)
            assert stale.get("error") == "conflict", stale
            passed("compact_reply_preserves_conflict")
            for key in ["command_output_id", "diff_output_id"]:
                record = json.loads((audit.directory(report[key]) / "record.json").read_text())
                assert record["actual_cwd"] == root and record["exit_code"] == 0, record
            passed("record_contains_actual_remote_command_and_cwd")
    save()
    print(json.dumps({k:report[k] for k in ["passed", "failed", "remote_workspace", "command_operation_id", "diff_operation_id"]}), flush=True)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--host", required=True)
    parser.add_argument("--parent", required=True)
    parser.add_argument("--output", required=True)
    args = parser.parse_args()
    os.environ.setdefault("REMOTE_WORKBENCH_AUDIT_DIR", str(PROJECT / ".remote-workbench-audit" / "app-display-live"))
    asyncio.run(run(args))
