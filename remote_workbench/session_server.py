"""Opt-in Codex thread-aware MCP; control and persistent state stay local."""
from __future__ import annotations

import argparse
from dataclasses import asdict
import hashlib
import inspect
import json
from pathlib import Path
import re
import sqlite3
import sys
import time
import uuid

from mcp.server.fastmcp import Context, FastMCP
from mcp.server.fastmcp.exceptions import ToolError

from remote_workbench import audit
from remote_workbench.audit_web import handler_for_workspaces
from remote_workbench.config import load_config
from remote_workbench.session_context import get_codex_identity, request_metadata, SessionIdentityUnavailable
from remote_workbench.workspace import TOOLS, bind_tool, _local_audit_viewer
from remote_workbench.workspace_router import SessionRouter, default_state_dir
from remote_workbench.workspace_registry import WorkspaceTarget
from remote_workbench.presentation import is_test_command
from remote_workbench.workspace_snapshot import collect as collect_snapshot
from remote_workbench.compute_review import REVIEW_INSTRUCTIONS

_LOCAL_TOOLS = {"audit_list", "audit_get", "read_output"}
_BUDGETS = {"exec_command": 2048, "read_file": 4096, "patch_file": 4096, "git_diff": 4096}


def create_identity_probe(*, viewer_url: str | None = None, audit_dir: Path) -> FastMCP:
    """A local diagnostic with no host configuration or remote execution tools."""
    server = FastMCP("remote-workbench-identity-probe", instructions=(
        "Read-only local MCP metadata diagnostic. session_identity_probe reports the conversation "
        "identity supplied by the actual client. It does not connect to SSH or select a workspace. "
        "Include the returned operation_log link in your user-facing result."
    ))

    @server.tool(structured_output=False)
    async def session_identity_probe(ctx: Context) -> str:
        meta = request_metadata(ctx)
        result = {"identity_available": False, "identity_source": "mcp._meta.threadId",
                  "metadata_keys": sorted(k for k in meta if isinstance(k, str))[:32],
                  "remote_execution": False}
        try:
            identity = get_codex_identity(ctx)
            result.update(identity_available=True, thread_id=identity.thread_id,
                          client_session_id=identity.session_id)
        except SessionIdentityUnavailable:
            result["message"] = "The client did not supply a usable threadId; session routing remains disabled"
        with audit.scoped_root(audit_dir):
            record = audit.create(kind="tool", tool="session_identity_probe", status="finished", **result)
            result["operation_id"] = record
            if viewer_url:
                result["operation_log"] = f"[**Remote Workbench 操作记录**]({viewer_url}/?id={record})"
        return json.dumps(result, ensure_ascii=False, separators=(",", ":"))

    return server


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
        + REVIEW_INSTRUCTIONS
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

    async def history(state, identity, limit: int = 20):
        return {"targets": router.registry.history(state.workspace_id, limit)}

    async def workspace_snapshot(state, identity):
        snapshot = router.snapshot(state)
        record = audit.create(kind="tool", tool="workspace_snapshot", host=snapshot.host,
                              cwd=snapshot.cwd, status="running")
        token = audit.operation_id.set(record)
        try:
            result = await collect_snapshot(snapshot)
            result["last_test"] = router.registry.last_test(state.workspace_id, snapshot.target_id)
            result["communication"] = router.registry.metrics(state.workspace_id)
            result["communication"]["scope"] = "recorded_workspace_responses_before_this_call"
            result["operation_id"] = record
            audit.update(record, status="finished", output_id=result["output_id"])
            return result
        except BaseException as exc:
            audit.update(record, status="failed", error=str(exc))
            raise
        finally:
            audit.operation_id.reset(token)

    def record_metrics(state, name, arguments, text, started, *, result=None, failed=False):
        read_key = None
        if name == "read_output" and result is not None:
            # Repeat detection concerns identical saved-output pages only.
            # New bytes appended to a running stream produce a different key.
            fields = [arguments.get("output_id"), arguments.get("stream"),
                      arguments.get("cursor"), result.get("content")]
            read_key = hashlib.sha256(json.dumps(fields, ensure_ascii=False).encode()).hexdigest()
        try:
            router.registry.record_call(state.workspace_id, name,
                len(json.dumps(arguments, ensure_ascii=False, separators=(",", ":")).encode()),
                len(text.encode()), round((time.monotonic() - started) * 1000), failed, read_key)
        except (sqlite3.Error, OSError):
            # An accounting failure must not turn an already executed write
            # into an apparent tool failure that invites a dangerous retry.
            print("Remote Workbench communication accounting unavailable", file=sys.stderr)

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
            started = time.monotonic()
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
                        text = pack(result, router.registry.get(state.workspace_id))
                        failed = bool(result.get("error") or result.get("timed_out") or
                                      result.get("exit_code") not in (None, 0))
                        record_metrics(state, name, arguments, text, started, result=result, failed=failed)
                        return text
            except Exception as exc:
                url = workspace_url(state)
                link = f"\n\n[**Remote Workbench 操作记录**]({url})" if url else ""
                text = str(exc) + link
                record_metrics(state, name, arguments, text, started, failed=True)
                raise ToolError(text) from exc

        invoke.__name__ = name
        invoke.__doc__ = description
        invoke.__signature__ = signature
        invoke.__annotations__ = {p.name: p.annotation for p in signature.parameters.values()}
        invoke.__annotations__["return"] = str
        server.tool(structured_output=False)(invoke)

    for name, function, description in (
        ("workspace_info", info, "Inspect this thread's persistent workspace and metadata identity. No remote execution."),
        ("workspace_history", history, "Read this thread's previous host/source-directory bindings, newest first."),
        ("workspace_snapshot", workspace_snapshot, "Inspect current remote Git state, last test evidence for this target, and measured response bytes. Last test evidence does not certify the current code."),
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
            target = router.registry.get_job(state.workspace_id, arguments["job_id"]) if _name in {"job_status", "stop_job"} else None
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
                result = await tool(**arguments)
            if _name == "exec_command" and is_test_command(arguments["command"]) and result.get("executed") is not False:
                state_known = result.get("execution_state") == "finished" and not result.get("timed_out")
                observation = {"operation_id": result.get("operation_id"), "exit_code": result.get("exit_code"),
                    "status": ("passed" if result.get("exit_code") == 0 else "failed") if state_known else "unknown",
                    "observed_at": time.time(), "revision": state.revision,
                    "evidence": "command_exit_only", "applies_to_current_code": None}
                try:
                    router.registry.save_test(state.workspace_id, snapshot.target_id, observation)
                except (sqlite3.Error, OSError):
                    print("Remote Workbench test observation unavailable", file=sys.stderr)
            return result

        register(name, public, description, execute)
    return server


def main():
    parser = argparse.ArgumentParser(description="Local, persistent Codex thread-aware remote workspace MCP (prototype)")
    parser.add_argument("--config", help="Host allowlist YAML; uses the normal Remote Workbench configuration by default")
    parser.add_argument("--data-dir", type=Path, default=default_state_dir(), help="Private local registry/audit directory")
    parser.add_argument("--viewer-port", type=int, default=8769, help="Preferred localhost viewer port; falls back if occupied")
    parser.add_argument("--identity-probe", action="store_true", help="Expose only a local metadata diagnostic, without host configuration or remote tools")
    args = parser.parse_args()
    if args.identity_probe:
        directory = args.data_dir.expanduser().resolve() / "identity-probe"
        with _local_audit_viewer(args.viewer_port, audit_dir=directory) as viewer:
            url = f"http://127.0.0.1:{viewer.server_port}"
            create_identity_probe(viewer_url=url, audit_dir=directory).run()
        return
    router = SessionRouter(load_config(args.config), args.data_dir)
    with _local_audit_viewer(args.viewer_port, handler=handler_for_workspaces(router.state_dir / "audit")) as viewer:
        url = f"http://127.0.0.1:{viewer.server_port}"
        print(f"Local session audit viewer: {url}", file=sys.stderr, flush=True)
        create_server(router, viewer_url=url).run()


if __name__ == "__main__":
    main()
