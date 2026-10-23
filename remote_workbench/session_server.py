"""Opt-in Codex thread-aware MCP; control and persistent state stay local."""
from __future__ import annotations

import argparse
from dataclasses import asdict
import inspect
import json
from pathlib import Path
import re
import sys
import uuid

from mcp.server.fastmcp import Context, FastMCP
from mcp.server.fastmcp.exceptions import ToolError

from remote_workbench import audit
from remote_workbench.audit_web import handler_for_workspaces
from remote_workbench.config import load_config
from remote_workbench.session_context import get_codex_identity
from remote_workbench.workspace import TOOLS, bind_tool, _local_audit_viewer
from remote_workbench.workspace_router import SessionRouter, default_state_dir
from remote_workbench.workspace_registry import WorkspaceTarget

_LOCAL_TOOLS = {"audit_list", "audit_get", "read_output"}
_BUDGETS = {"exec_command": 2048, "read_file": 4096, "patch_file": 4096, "git_diff": 4096}


def create_server(router: SessionRouter, *, viewer_url: str | None = None) -> FastMCP:
    server = FastMCP("remote-workbench-session", instructions=(
        "Remote project execution with local control. Each Codex thread has a persistent private workspace. "
        "Use workspace_info to inspect it; when unbound, ask for a configured host and source directory, "
        "then workspace_switch creates a private copy. Use these tools for remote files, commands, Git and tests. "
        "Never fall back to local execution on identity or SSH failure. Relative paths start at the active remote copy. "
        "Every command starts a fresh account-default login shell; activate environments in that command. "
        "No remote Codex CLI runs. Missing MCP threadId stops routing; do not supply an ID as a tool argument. "
        "Switching resumes existing private targets and preserves old jobs. A fork with a new threadId starts unbound. "
        "This is workspace separation, not an operating-system sandbox. "
        "Whenever tools return operation_log, include that Markdown link with the exact bold title "
        "'Remote Workbench 操作记录' in user-facing updates and the final reply."
    ))

    def workspace_url(state):
        return f"{viewer_url}/w/{state.workspace_id}/" if viewer_url else None

    def pack(result, state):
        result = dict(result)
        result.setdefault("workspace_id", state.workspace_id)
        result.setdefault("revision", state.revision)
        url = workspace_url(state)
        if url:
            record_id = result.get("operation_id") or result.get("output_id") or result.get("id")
            if (isinstance(record_id, str) and re.fullmatch(r"[a-f0-9]{32}", record_id)
                    and (audit.directory(record_id) / "record.json").is_file()):
                url += "?id=" + record_id
            result["operation_log"] = f"[**Remote Workbench 操作记录**]({url})"
        return json.dumps(result, ensure_ascii=False, separators=(",", ":"), default=str)

    async def info(state, identity, **arguments):
        return dict(asdict(state), bound=state.host is not None, control="local", execution="remote",
                    project_isolated=True, identity_source="mcp._meta.threadId",
                    client_session_id=identity.session_id, allowed_hosts=sorted(router.config.hosts),
                    shell_state_reused=False)

    async def history(state, identity, limit=20):
        return {"targets": router.registry.history(state.workspace_id, limit)}

    async def switch(state, identity, host: str, cwd: str, expected_revision: int | None = None):
        record = audit.create(kind="tool", tool="workspace_switch", status="running",
                              arguments={"host": host, "cwd": cwd, "expected_revision": expected_revision})
        token = audit.operation_id.set(record)
        try:
            updated = await router.switch(state, host, cwd, expected_revision)
            audit.update(record, status="finished", host=updated.host, cwd=updated.cwd,
                         target_id=updated.target_id, revision=updated.revision, previous=asdict(state))
            return dict(asdict(updated), bound=True, project_isolated=True,
                        previous={"host": state.host, "cwd": state.cwd, "revision": state.revision},
                        operation_id=record)
        except BaseException as exc:
            audit.update(record, status="failed", error=str(exc))
            raise
        finally:
            audit.operation_id.reset(token)

    def register(name, signature, description, execute):
        # Context is injected by FastMCP and deliberately absent from JSON Schema.
        signature = signature.replace(parameters=[*signature.parameters.values(),
            inspect.Parameter("ctx", inspect.Parameter.KEYWORD_ONLY, annotation=Context, default=None)],
            return_annotation=str)

        async def invoke(**kwargs):
            bound = signature.bind(**kwargs)
            bound.apply_defaults()
            arguments = dict(bound.arguments)
            ctx = arguments.pop("ctx")
            try:
                identity = get_codex_identity(ctx)
            except ValueError as exc:
                raise ToolError(str(exc)) from exc
            state = router.registry.get_or_create(identity.thread_id)
            directory = router.audit_dir(state.workspace_id)
            directory.mkdir(parents=True, mode=0o700, exist_ok=True)
            try:
                async with router.lease(state.workspace_id):
                    state = router.registry.get(state.workspace_id)
                    metadata = dict(thread_id=identity.thread_id, workspace_id=state.workspace_id,
                                    revision=state.revision, target_id=state.target_id,
                                    client_session_id=identity.session_id)
                    with audit.scoped_root(directory), audit.scoped_metadata(**metadata):
                        result = await execute(state, identity, **arguments)
                        return pack(result, router.registry.get(state.workspace_id))
            except Exception as exc:
                url = workspace_url(state)
                link = f"\n\n[**Remote Workbench 操作记录**]({url})" if url else ""
                raise ToolError(str(exc) + link) from exc

        invoke.__name__ = name
        invoke.__doc__ = description
        invoke.__signature__ = signature
        invoke.__annotations__ = {p.name: p.annotation for p in signature.parameters.values()}
        invoke.__annotations__["return"] = str
        server.tool(structured_output=False)(invoke)

    for name, function, description in (
        ("workspace_info", info, "Inspect this thread's persistent workspace and metadata identity. No remote execution."),
        ("workspace_history", history, "Read this thread's previous host/source-directory bindings, newest first."),
        ("workspace_switch", switch, "Validate a configured host/source cwd, create or resume a private project copy, then atomically switch. Old jobs keep their original target."),
    ):
        signature = inspect.signature(function, eval_str=True)
        signature = signature.replace(parameters=[p for p in signature.parameters.values()
                                                  if p.name not in {"state", "identity", "arguments"}])
        register(name, signature, description, function)

    for name, function, description in TOOLS:
        signature = inspect.signature(function, eval_str=True)
        public = signature.replace(parameters=[
            p.replace(default=".") if p.name == "cwd" else p.replace(default=_BUDGETS[name])
            if p.name == "max_bytes" and name in _BUDGETS else p
            for p in signature.parameters.values() if p.name != "host"])

        async def execute(state, identity, _name=name, _function=function, **arguments):
            if _name in _LOCAL_TOOLS:
                return await _function(**arguments)
            target = router.registry.get_job(state.workspace_id, arguments["job_id"]) if _name == "job_status" else None
            snapshot = router.snapshot(state, target)
            if _name == "start_job":
                job_id = arguments.get("job_id") or uuid.uuid4().hex
                if not re.fullmatch(r"[A-Za-z0-9_-]{1,80}", job_id):
                    raise ValueError("Invalid job_id")
                arguments["job_id"] = job_id
                origin = WorkspaceTarget(snapshot.target_id, state.workspace_id, snapshot.host,
                                         snapshot.source_cwd, snapshot.cwd, snapshot.project_root)
                # Reserve the origin before submission, including ambiguous SSH
                # failures; never route a retry to another server after a switch.
                router.registry.register_job(state.workspace_id, job_id, origin)
            tool = bind_tool(snapshot.workspace, _name, _function, "")
            with audit.scoped_metadata(**snapshot.metadata(), client_session_id=identity.session_id):
                return await tool(**arguments)

        register(name, public, description, execute)
    return server


def main():
    parser = argparse.ArgumentParser(description="Local, persistent Codex thread-aware remote workspace MCP (prototype)")
    parser.add_argument("--config", help="Host allowlist YAML; uses the normal Remote Workbench configuration by default")
    parser.add_argument("--data-dir", type=Path, default=default_state_dir(), help="Private local registry/audit directory")
    parser.add_argument("--viewer-port", type=int, default=8769, help="Preferred localhost viewer port; falls back if occupied")
    args = parser.parse_args()
    router = SessionRouter(load_config(args.config), args.data_dir)
    with _local_audit_viewer(args.viewer_port, handler=handler_for_workspaces(router.state_dir / "audit")) as viewer:
        url = f"http://127.0.0.1:{viewer.server_port}"
        print(f"Local session audit viewer: {url}", file=sys.stderr, flush=True)
        create_server(router, viewer_url=url).run()


if __name__ == "__main__":
    main()
