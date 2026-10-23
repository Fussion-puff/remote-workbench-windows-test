"""A local MCP with one immutable remote default workspace; no remote agent."""
from __future__ import annotations

import argparse
from dataclasses import dataclass, replace
from contextlib import contextmanager
import functools
from http.server import ThreadingHTTPServer
import inspect
import json
from pathlib import Path
import posixpath
import re
import sys
from threading import Thread
from typing import Any

from mcp.server.fastmcp import FastMCP

from remote_workbench.config import load_config
from remote_workbench.audit_web import Handler as AuditViewerHandler
from remote_workbench.server import workspace_executor
from remote_workbench.ssh import SSHExecutor
from remote_workbench.tools import commands, files, git, search, patch, audit as audit_tools


@dataclass(frozen=True)
class Workspace:
    name: str
    host: str
    cwd: str
    executor: SSHExecutor

    @classmethod
    def load(cls, path: str | Path) -> "Workspace":
        path = Path(path).resolve()
        data = json.loads(path.read_text())
        allowed = {"version", "name", "host", "cwd", "config"}
        if not isinstance(data, dict) or set(data) - allowed:
            raise ValueError("Unknown workspace manifest fields")
        if data.get("version") != 1:
            raise ValueError("Workspace version must be 1")
        name, host, cwd = (data.get(key) for key in ("name", "host", "cwd"))
        if not isinstance(name, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", name):
            raise ValueError("Workspace name must contain 1..64 ASCII letters, digits, '-' or '_'")
        if not isinstance(host, str) or not host:
            raise ValueError("Workspace host is required")
        if not isinstance(cwd, str) or not cwd.startswith("/") or "\x00" in cwd:
            raise ValueError("Workspace cwd must be a remote absolute path")
        config_path = data.get("config")
        if config_path is not None:
            if not isinstance(config_path, str):
                raise ValueError("config must be a path string")
            config_path = path.parent / Path(config_path).expanduser()
        base = load_config(config_path).get_host(host)
        cwd = posixpath.normpath(cwd)
        # Default shells load their own configuration. Do not carry a configured
        # host preamble/activated environment into a workspace automatically.
        config = replace(base, default_cwd=cwd, preamble="")
        return cls(name, host, cwd, SSHExecutor(config, remote_state_dir=cwd + "/.remote-workbench-state"))

    def resolve_cwd(self, cwd: str | None = None) -> str:
        if cwd is None or cwd == "":
            return self.cwd
        if "\x00" in cwd or cwd.startswith("~"):
            raise ValueError("cwd must be absolute or relative to the workspace; use an absolute home path")
        return posixpath.normpath(posixpath.join(self.cwd, cwd))


# Short names and descriptions avoid repeating SSH setup and host parameters in
# every model-facing schema. Original tool validation/audit code remains shared.
TOOLS = [
    ("exec_command", commands.ssh_run, "Execute in the remote workspace. Each call starts a fresh default login shell; max_bytes bounds returned output. trace saves expanded commands."),
    ("read_file", files.ssh_read_file, "Read remote UTF-8 lines, numbered, with a SHA256 for guarded edits. offset is zero-based; limit/max_bytes bound output."),
    ("write_file", files.ssh_write_file, "Atomically write remote UTF-8 content. Optional expected_sha256 rejects stale edits."),
    ("patch_file", files.ssh_patch_file, "Apply unique old/new replacements, or sequential edits=[{old,new}], remotely. Optional expected_sha256 and check_only; returns this edit's diff."),
    ("apply_patch", patch.ssh_apply_patch, "Check and apply a unified Git diff remotely. Paths are repository-root relative. check_only previews validation; does not stage or commit."),
    ("list_dir", files.ssh_list_dir, "List remote entries. Relative paths start at the workspace root."),
    ("grep", search.ssh_grep, "Search remote file contents by regex, with bounded results."),
    ("find_files", search.ssh_find_files, "Find remote paths by filename glob, type and depth, with bounded results."),
    ("git_status", git.ssh_git_status, "Read remote repository status, including untracked files."),
    ("git_diff", git.ssh_git_diff, "Default compact numstat; mode='patch' returns paged hunks. Continue with output_id/next_cursor to reuse the snapshot."),
    ("start_job", commands.ssh_run_background, "Start an explicit detached remote job in a fresh default shell. Returns job_id; poll using job_status."),
    ("job_status", commands.ssh_job_status, "Read detached job state and incremental output. Reuse returned byte cursors."),
    ("read_output", audit_tools.ssh_read_output, "Page saved output by output_id, stream and byte cursor; full command output and edit diffs are retained locally."),
    ("audit_get", audit_tools.ssh_audit_get, "Read a local operation record, including actual remote commands and child IDs."),
    ("audit_list", audit_tools.ssh_audit_list, "List recent local operation metadata."),
]


def bind_tool(workspace: Workspace, name: str, function, description: str):
    signature = inspect.signature(function, eval_str=True)
    parameters = [p.replace(default=".") if p.name == "cwd" else p
                  for p in signature.parameters.values() if p.name != "host"]
    # All cwd parameters occur after required non-cwd parameters in these tools.
    public_signature = signature.replace(parameters=parameters, return_annotation=dict[str, Any])

    @functools.wraps(function)
    async def invoke(**kwargs):
        # Bind explicitly even for direct Python callers: do not allow **kwargs
        # to smuggle a different host past the public schema.
        bound = public_signature.bind(**kwargs)
        arguments = dict(bound.arguments)
        if "host" in signature.parameters:
            arguments["host"] = workspace.host
        if "cwd" in signature.parameters:
            arguments["cwd"] = workspace.resolve_cwd(arguments.get("cwd"))
        token = workspace_executor.set((workspace.host, workspace.executor))
        try:
            return await function(**arguments)
        finally:
            workspace_executor.reset(token)

    invoke.__name__ = name
    invoke.__doc__ = description
    invoke.__signature__ = public_signature
    invoke.__annotations__ = {k: v for k, v in function.__annotations__.items() if k != "host"}
    invoke.__annotations__["return"] = dict[str, Any]
    return invoke


def _workspace_response(tool, default_max_bytes: int | None = None):
    """Serialize a workspace tool result once, as compact JSON text.

    FastMCP's default structured-output mode sends dict results twice: once as
    pretty-printed text for older clients and once as structuredContent. Keep
    the text form for broad MCP client compatibility, but suppress the second
    copy and whitespace. The audit log remains the source for full output.
    """
    signature = inspect.signature(tool)
    if default_max_bytes is not None and "max_bytes" in signature.parameters:
        signature = signature.replace(parameters=[
            parameter.replace(default=default_max_bytes) if parameter.name == "max_bytes" else parameter
            for parameter in signature.parameters.values()
        ])

    @functools.wraps(tool)
    async def invoke(**kwargs):
        if default_max_bytes is not None:
            kwargs.setdefault("max_bytes", default_max_bytes)
        result = await tool(**kwargs)
        return json.dumps(result, ensure_ascii=False, separators=(",", ":"), default=str)

    invoke.__name__ = tool.__name__
    invoke.__doc__ = tool.__doc__
    invoke.__signature__ = signature.replace(return_annotation=str)
    invoke.__annotations__ = dict(getattr(tool, "__annotations__", {}))
    invoke.__annotations__["return"] = str
    return invoke


def create_server(workspace: Workspace) -> FastMCP:
    server = FastMCP("remote-workbench-workspace-" + workspace.name, instructions=(
        f"Default project environment: {workspace.host}:{workspace.cwd}. "
        "Use these tools for all project commands, files, search, edits, Git and tests. "
        "Paths are remote; omitted/relative cwd starts at the workspace root. "
        "A cd/export in one call does not change subsequent calls. Explicitly activate "
        "the required environment in the same command. No automatic environment reuse. "
        "Control and audit records stay local; only shell/Python workers run remotely. "
        "On SSH failure stop and report; never fall back to local project execution. "
        "This is a default directory, not a filesystem sandbox."
    ))

    async def workspace_info() -> dict[str, Any]:
        """Verify the bound remote directory and default shell; report local control placement."""
        connected, message = await workspace.executor.check_connection(force=True)
        result = {"name": workspace.name, "host": workspace.host, "cwd": workspace.cwd,
                  "control": "local", "execution": "remote", "connected": connected,
                  "shell_state_reused": False, "connection_message": message}
        if connected:
            probe_code = ('import json,os; print(json.dumps({"cwd":os.getcwd(), '
                          '"shell":os.environ.get("REMOTE_WORKBENCH_LOGIN_SHELL", "")}))')
            probe = await workspace.executor.run_program(["python3", "-c", probe_code], output_budget=2048)
            result.update(ready=probe.exit_code == 0, probe=probe.stdout,
                          stderr=probe.stderr, output_id=probe.output_id)
            if probe.exit_code == 0:
                try:
                    detail = json.loads(probe.stdout)
                    result.update(shell=detail["shell"], probe=detail["cwd"] + "\n" + detail["shell"] + "\n")
                except (ValueError, KeyError, TypeError):
                    result.update(ready=False, stderr="Invalid remote shell probe")
        else:
            result["ready"] = False
        return result

    server.tool(structured_output=False)(_workspace_response(workspace_info))

    for name, function, description in TOOLS:
        default_max_bytes = {
            "exec_command": 2048,
            "read_file": 4096,
            "patch_file": 4096,
            "git_diff": 4096,
        }.get(name)
        tool = bind_tool(workspace, name, function, description)
        server.tool(structured_output=False)(_workspace_response(tool, default_max_bytes))
    return server


@contextmanager
def _local_audit_viewer(port: int | None):
    """Optionally serve the audit viewer on loopback for this MCP lifetime."""
    if port is None:
        yield None
        return
    if isinstance(port, bool) or not 1 <= port <= 65535:
        raise ValueError("viewer port must be between 1 and 65535")

    viewer = ThreadingHTTPServer(("127.0.0.1", port), AuditViewerHandler)
    thread = Thread(
        target=viewer.serve_forever,
        kwargs={"poll_interval": 0.1},
        name=f"remote-workbench-audit-viewer-{port}",
        daemon=True,
    )
    started = False
    try:
        thread.start()
        started = True
        yield viewer
    finally:
        try:
            if started:
                viewer.shutdown()
        finally:
            viewer.server_close()
            if started:
                thread.join()


def main():
    parser = argparse.ArgumentParser(description="Local Remote Workbench MCP bound to one remote workspace")
    parser.add_argument("--workspace", required=True, help="Local workspace JSON manifest")
    parser.add_argument("--viewer-port", type=int, help="Also serve the local audit viewer on 127.0.0.1:<port>")
    args = parser.parse_args()
    workspace = Workspace.load(args.workspace)
    with _local_audit_viewer(args.viewer_port) as viewer:
        if viewer is not None:
            print(f"Local audit viewer: http://127.0.0.1:{viewer.server_port}", file=sys.stderr, flush=True)
        create_server(workspace).run()


if __name__ == "__main__":
    main()
