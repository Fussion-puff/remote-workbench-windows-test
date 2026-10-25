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
from remote_workbench.compute_review import REVIEW_INSTRUCTIONS, check_compute_review
from remote_workbench import baseline, test_evidence
from remote_workbench.auth_boundary import authentication_command

_LOCAL_TOOLS = {"audit_list", "audit_get", "read_output", "search_output"}
_JOB_CONTROL_TOOLS = {"job_status", "stop_job", "job_wait"}
# Read-only checks and controls must remain available while a foreground
# command owns the workspace lease. Remote checks may observe intermediate
# files while a command is running; they never mutate the workspace.
_READ_ONLY_REMOTE_TOOLS = {"workspace_snapshot", "workspace_changes", "workspace_preview"}
_SESSION_LOCAL_TOOLS = {"workspace_resume", "workspace_note"}
_UNLOCKED_TOOLS = _LOCAL_TOOLS | _JOB_CONTROL_TOOLS | {
    "workspace_info", "workspace_history", "resolve_operation"
} | _READ_ONLY_REMOTE_TOOLS | _SESSION_LOCAL_TOOLS
_BUDGETS = {"exec_command": 2048, "read_file": 4096, "patch_file": 4096,
            "git_diff": 4096, "start_job": 8192}


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
        "Codex's native local tools always remain available and are never redirected by this MCP. "
        "Use this MCP only for operations the user explicitly assigns to a remote host in the current chat. "
        "Selecting a remote workspace affects only Remote Workbench tools, not all project commands. "
        "Never infer remote intent from an existing SSH connection, another chat/session, or an unrelated saved binding. "
        "Resume this thread's saved binding for its continuing explicitly remote task; unrelated local work stays local. "
        "Each Codex thread has an independent private remote workspace. "
        "workspace_info reads local metadata only. When the user requests remote work, ask for a configured host "
        "and source directory if needed, then workspace_switch creates or resumes this thread's private copy. "
        "When this chat no longer needs remote routing, use workspace_use_local to clear its Remote Workbench binding. "
        "Use these tools for remote files, commands, Git and tests only after that explicit selection. "
        "Never fall back to local execution on identity or SSH failure. Relative paths start at the active remote copy. "
        "Every command starts a fresh account-default login shell; activate environments in that command. "
        "No remote Codex CLI runs. Missing MCP threadId stops routing; do not supply an ID as a tool argument. "
        "Switching resumes existing private targets and preserves old jobs. A fork with a new threadId starts unbound. "
        "For background jobs, pass idempotency_key so a repeated call can recover the same job_id; use "
        "resolve_operation(idempotency_key) after a lost response. Without a key, cross-call retries can start another job. "
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
        mode = "remote" if state.host is not None else "local"
        return dict(asdict(state), mode=mode, bound=state.host is not None, control="local",
                    execution="remote-workspace-tools" if state.host is not None else "native-local-tools",
                    project_isolated=state.host is not None, identity_source="mcp._meta.threadId",
                    client_session_id=identity.session_id, allowed_hosts=sorted(router.config.hosts),
                    shell_state_reused=False)

    async def history(state, identity, limit: int = 20):
        return {"targets": router.registry.history(state.workspace_id, limit)}

    async def resume(state, identity, job_offset: int = 0, limit: int = 20):
        """Build a local-only recovery summary; never reconnect or poll SSH."""
        if isinstance(job_offset, bool) or not isinstance(job_offset, int) or job_offset < 0:
            raise ValueError("job_offset must be a non-negative integer")
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 50:
            raise ValueError("limit must be between 1 and 50")
        recent_delivery = []
        for item in audit.recent(limit=100):
            if item.get("tool") not in {"workspace_changes", "workspace_preview", "workspace_apply"}:
                continue
            try:
                record = json.loads((audit.directory(item["id"]) / "record.json").read_text())
            except (OSError, ValueError, KeyError):
                continue
            detail = record.get("delivery_result")
            files = detail.get("files", []) if isinstance(detail, dict) else []
            if not isinstance(files, list):
                files = []
            recent_delivery.append({
                key: item[key] for key in ("id", "tool", "host", "cwd", "created_at", "status")
                if key in item
            } | {
                "files": [{
                    key: file[key] for key in (
                        "path", "status", "change", "source_conflict", "reason", "diff_omitted",
                    ) if key in file
                } for file in files[:20] if isinstance(file, dict)],
                "files_total": detail.get("files_total", len(files)),
                "truncated": detail.get("truncated", False) or len(files) > 20,
                "source": "local_audit_cache",
            })
            if len(recent_delivery) >= 3:
                break
        job_page = router.registry.list_jobs(
            state.workspace_id, limit=limit + 1, offset=job_offset)
        jobs_has_more = len(job_page) > limit
        jobs = []
        for item in job_page[:limit]:
            target = item["target"]
            local = None
            lookup_error = None
            if target.host in router.config.hosts and target.cwd:
                try:
                    executor = router.executor(target.host, target.cwd, target.project_root)
                    lookup = getattr(executor, "job_local_info", None)
                    value = lookup(item["job_id"]) if lookup is not None else None
                    if isinstance(value, dict):
                        local = value
                except (OSError, ValueError, sqlite3.Error) as exc:
                    lookup_error = type(exc).__name__
            local_summary = None
            if local is not None:
                local_summary = {
                    key: local[key] for key in (
                        "job_output_id", "running", "exit_code", "execution_state",
                        "sync_state", "stdout_bytes", "stderr_bytes", "synced_at", "error",
                    ) if key in local
                }
                if "execution_state" not in local_summary:
                    local_summary["execution_state"] = (
                        "running" if local.get("running") is True else "unknown"
                    )
            jobs.append({
                "job_id": item["job_id"],
                "target": asdict(target),
                "local_status": local_summary,
                "local_cache_age": "unknown",
                "lookup_error": lookup_error,
            })
        current_test = (router.registry.last_test(state.workspace_id, state.target_id)
                        if state.target_id else None)
        if isinstance(current_test, dict):
            current_test = dict(current_test)
            command = current_test.pop("command", None)
            if isinstance(command, str):
                current_test["command_preview"] = command[:240] + ("…" if len(command) > 240 else "")
            current_test["local_record_id"] = (
                current_test.get("operation_id") or current_test.get("output_id")
            )
            for field in ("before", "after"):
                evidence = current_test.get(field)
                if not isinstance(evidence, dict):
                    continue
                code = evidence.get("code")
                current_test[field] = {
                    "code": ({
                        key: code[key] for key in ("complete", "digest", "scope", "files", "bytes", "reason")
                        if key in code
                    } if isinstance(code, dict) else None),
                    "config_digest": evidence.get("config_digest"),
                    "observed_at": evidence.get("observed_at"),
                }
        return {
            "binding": dict(asdict(state), mode="remote" if state.host is not None else "local"),
            "recent_bindings": router.registry.history(state.workspace_id, 10),
            "last_test": current_test,
            "jobs": jobs,
            "jobs_offset": job_offset,
            "jobs_limit": limit,
            "jobs_has_more": jobs_has_more,
            "jobs_next_offset": job_offset + limit if jobs_has_more else None,
            "jobs_truncated": jobs_has_more,
            "recent_delivery": recent_delivery,
            "recent_delivery_freshness": "local audit history; does not query the remote project",
            "note": router.registry.workspace_note(state.workspace_id),
            "remote_query_performed": False,
            "job_collection_restarted": False,
            "local_cache_freshness": "unknown; no remote status query was made",
        }

    async def note(state, identity, goal: str | None = None, constraints: str | None = None,
                   decisions: str | None = None, next_step: str | None = None):
        values = {"goal": goal, "constraints": constraints,
                  "decisions": decisions, "next_step": next_step}
        updates = {key: value for key, value in values.items() if value is not None}
        if any(len(value) > 2000 for value in updates.values()):
            raise ValueError("Each workspace note field must be at most 2000 characters")
        saved = (router.registry.workspace_note(state.workspace_id, updates) if updates
                 else router.registry.workspace_note(state.workspace_id))
        return {"note": saved, "updated": bool(updates), "storage": "local_workspace_registry"}

    async def workspace_changes(state, identity, offset: int = 0, limit: int = 20):
        if isinstance(offset, bool) or not isinstance(offset, int) or offset < 0:
            raise ValueError("offset must be a non-negative integer")
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 50:
            raise ValueError("limit must be between 1 and 50")
        return await delivery(state, identity, "changes", offset=offset, limit=limit)

    async def workspace_preview(state, identity, paths: list[str], max_bytes: int = 8192):
        if len(paths) > 20:
            raise ValueError("Preview at most 20 selected paths per call")
        return await delivery(state, identity, "preview", paths=paths, max_bytes=max_bytes)

    async def workspace_apply(state, identity, files: list[dict]):
        return await delivery(state, identity, "apply", files=files)

    async def delivery(state, identity, action, **arguments):
        snapshot = router.snapshot(state)
        record = audit.create(
            kind="tool",
            tool={"changes": "workspace_changes", "preview": "workspace_preview",
                  "apply": "workspace_apply"}[action],
            host=snapshot.host,
            cwd=snapshot.cwd,
            arguments={
                key: ([item.get("path") for item in value if isinstance(item, dict)]
                      if key == "files" and isinstance(value, list) else value)
                for key, value in arguments.items()
            },
            status="running",
        )
        token = audit.operation_id.set(record)
        try:
            if action == "changes":
                result = await baseline.preview_delivery(snapshot.workspace.executor,
                                                         snapshot.project_root)
                files = result.get("files", [])
                offset, limit = arguments["offset"], arguments["limit"]
                keys = {"path", "change", "mode_change", "source_conflict", "supported", "reason"}
                result = dict(result, files=[{key: value for key, value in item.items() if key in keys}
                    for item in files[offset:offset + limit]], files_total=len(files),
                    next_offset=offset + limit if offset + limit < len(files) else None,
                    truncated=offset + limit < len(files),
                    page_scope="fresh inspection; concurrent edits may change later pages")
            elif action == "preview":
                result = await baseline.preview_delivery(
                    snapshot.workspace.executor, snapshot.project_root,
                    paths=arguments["paths"], max_bytes=arguments["max_bytes"])
                for item in result.get("files", []):
                    # Guards are sufficient for reviewed delivery. The full
                    # states remain in the saved worker output for inspection.
                    for key in ("baseline_state", "source_state", "session_state"):
                        item.pop(key, None)
            else:
                result = await baseline.apply_delivery(
                    snapshot.workspace.executor, snapshot.project_root, arguments["files"])
            audit.update(record, status="failed" if result.get("error") else "finished",
                         output_id=result.get("output_id"), delivery_result=result)
            return dict(result, operation_id=record)
        except BaseException as exc:
            audit.update(record, status="failed", error=str(exc))
            raise
        finally:
            audit.operation_id.reset(token)

    async def resolve_operation(state, identity, idempotency_key: str):
        """Recover a durable job handle after an MCP response was lost."""
        if not isinstance(idempotency_key, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", idempotency_key):
            raise ValueError("idempotency_key must contain 1..128 ASCII letters, digits, '-' or '_'")
        operation = router.registry.get_job_operation(state.workspace_id, idempotency_key)
        local_job = None
        local_job_error = False
        if operation.target.host in router.config.hosts and operation.target.cwd:
            try:
                executor = router.executor(operation.target.host, operation.target.cwd,
                                           operation.target.project_root)
                lookup = getattr(executor, "job_local_info", None)
                local_job = lookup(operation.job_id) if lookup is not None else None
            except (OSError, ValueError, sqlite3.Error):
                local_job_error = True
        result = {
            "idempotency_key": operation.idempotency_key,
            "operation_state": operation.state,
            "job_id": operation.job_id,
            "target": asdict(operation.target),
            "created_at": operation.created_at,
            "updated_at": operation.updated_at,
            "execution_state": (local_job.get("execution_state", "unknown")
                                 if isinstance(local_job, dict) else "unknown"),
            "next_step": "Call job_status with this job_id; it is routed to the saved target.",
        }
        if isinstance(local_job, dict):
            result["local_job"] = local_job
        elif local_job_error:
            result["local_job_state"] = "unavailable"
        if operation.state in {"reserved", "unknown"}:
            result["warning"] = (
                "Submission may have reached the remote worker. Do not restart it under a new key "
                "until its state is checked."
            )
        return result

    async def workspace_snapshot(state, identity):
        snapshot = router.snapshot(state)
        record = audit.create(kind="tool", tool="workspace_snapshot", host=snapshot.host,
                              cwd=snapshot.cwd, status="running")
        token = audit.operation_id.set(record)
        try:
            result = await collect_snapshot(snapshot)
            current = await test_evidence.capture(snapshot)
            comparison = test_evidence.compare(
                router.registry.last_test(state.workspace_id, snapshot.target_id), current)
            result["content_fingerprint"] = current["code"]
            result["test_evidence_comparison"] = comparison
            result["last_test"] = comparison
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

    async def use_local(state, identity, expected_revision: int | None = None):
        record = audit.create(kind="tool", tool="workspace_use_local", status="running",
                              arguments={"expected_revision": expected_revision})
        token = audit.operation_id.set(record)
        try:
            updated = router.use_local(state, expected_revision)
            audit.update(record, status="finished", revision=updated.revision,
                         previous=asdict(state), mode="local")
            return dict(asdict(updated), mode="local", bound=False,
                        previous={"host": state.host, "cwd": state.cwd,
                                  "revision": state.revision}, operation_id=record)
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
                async def run_scoped(current):
                    metadata = dict(thread_id=identity.thread_id, workspace_id=current.workspace_id,
                                    revision=current.revision, target_id=current.target_id,
                                    client_session_id=identity.session_id)
                    with audit.scoped_root(directory), audit.scoped_metadata(**metadata):
                        result = await execute(current, identity, **arguments)
                        text = pack(result, router.registry.get(current.workspace_id))
                        failed = bool(result.get("error") or result.get("timed_out") or
                                      result.get("exit_code") not in (None, 0))
                        record_metrics(current, name, arguments, text, started, result=result, failed=failed)
                        return text

                if name in _UNLOCKED_TOOLS:
                    # Re-read current workspace state without waiting on a
                    # command's lease. Job tools resolve their immutable origin
                    # below, while local tools stay within this workspace audit.
                    state = router.registry.get(state.workspace_id)
                    return await run_scoped(state)
                async with router.lease(state.workspace_id):
                    state = router.registry.get(state.workspace_id)
                    return await run_scoped(state)
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
        ("workspace_resume", resume, "Build a local-only recovery summary of this thread's binding, last test, saved jobs and note; local cache freshness is unknown and no SSH query is made."),
        ("workspace_note", note, "Read or update this thread's small local recovery note (goal, constraints, decisions, next_step)."),
        ("resolve_operation", resolve_operation, "Recover a background job handle by its idempotency_key after a lost response. This reads local state only; use job_status to inspect the job."),
        ("workspace_snapshot", workspace_snapshot, "Inspect current remote Git state, last test evidence for this target, and measured response bytes. Last test evidence does not certify the current code."),
        ("workspace_changes", workspace_changes, "List files changed in this private session copy relative to its initial content baseline; no diff text is returned."),
        ("workspace_preview", workspace_preview, "Preview explicitly selected changed files against their original source with bounded unified diffs and apply guards."),
        ("workspace_apply", workspace_apply, "Apply selected, reviewed workspace_preview file entries back to the original source with per-file hash guards."),
        ("workspace_switch", switch, "Validate a configured host/source cwd, create or resume a private project copy, then atomically switch. Old jobs keep their original target."),
        ("workspace_use_local", use_local, "Clear this thread's active remote binding and return to native local tools. Remote targets, private copies, history, and job origins remain available."),
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
            target = router.registry.get_job(state.workspace_id, arguments["job_id"]) if _name in _JOB_CONTROL_TOOLS else None
            snapshot = router.snapshot(state, target)
            job_operation = None
            idempotency_key = arguments.pop("idempotency_key", None) if _name == "start_job" else None
            if _name == "start_job":
                origin = WorkspaceTarget(snapshot.target_id, state.workspace_id, snapshot.host,
                                         snapshot.source_cwd, snapshot.cwd, snapshot.project_root)
                if idempotency_key is not None:
                    if (not isinstance(idempotency_key, str) or
                            not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", idempotency_key)):
                        raise ValueError("idempotency_key must contain 1..128 ASCII letters, digits, '-' or '_'")
                    requested_job_id = arguments.get("job_id") or None
                    effective_cwd = snapshot.workspace.resolve_cwd(arguments.get("cwd"))
                    timeout = arguments.get("timeout")
                    if timeout is None:
                        timeout = snapshot.workspace.executor.config.job_timeout
                    fingerprint_value = {
                        "command": arguments.get("command"),
                        "cwd": effective_cwd,
                        "timeout": timeout,
                        "requested_job_id": requested_job_id,
                    }
                    fingerprint = hashlib.sha256(json.dumps(
                        fingerprint_value, ensure_ascii=False, sort_keys=True,
                        separators=(",", ":"),
                    ).encode()).hexdigest()
                    job_id = requested_job_id or uuid.uuid4().hex
                    job_operation, created = router.registry.reserve_job_operation(
                        state.workspace_id, idempotency_key, origin, job_id, fingerprint)
                    if not created:
                        if job_operation.state == "rejected":
                            reopened = router.registry.reopen_rejected_job_operation(
                                state.workspace_id, idempotency_key)
                            if reopened:
                                job_operation = router.registry.get_job_operation(
                                    state.workspace_id, idempotency_key)
                            else:
                                job_operation = router.registry.get_job_operation(
                                    state.workspace_id, idempotency_key)
                                return {
                                    "job_id": job_operation.job_id,
                                    "idempotency_key": idempotency_key,
                                    "operation_state": job_operation.state,
                                    "execution_state": "unknown",
                                    "deduplicated": True,
                                    "message": "No new job was submitted. Use resolve_operation to recover this handle.",
                                }
                        else:
                            # A reservation may have reached the server even if
                            # its response was lost. Never start it a second time.
                            return {
                                "job_id": job_operation.job_id,
                                "idempotency_key": idempotency_key,
                                "operation_state": job_operation.state,
                                "execution_state": "unknown",
                                "deduplicated": True,
                                "message": "No new job was submitted. Use resolve_operation to recover this handle.",
                            }
                    arguments["job_id"] = job_operation.job_id
                else:
                    job_id = arguments.get("job_id") or uuid.uuid4().hex
                    if not re.fullmatch(r"[A-Za-z0-9_-]{1,80}", job_id):
                        raise ValueError("Invalid job_id")
                    arguments["job_id"] = job_id
                    # Even without a request key, reserve the immutable origin
                    # before submission so later job controls cannot drift.
                    router.registry.register_job(state.workspace_id, job_id, origin)
            tool = bind_tool(snapshot.workspace, _name, _function, "")
            with audit.scoped_metadata(**snapshot.metadata(), client_session_id=identity.session_id):
                test_command = (_name == "exec_command" and
                                is_test_command(arguments["command"]) and
                                not authentication_command(arguments["command"]))
                evidence_before = None
                evidence_record = None
                evidence_token = None
                if test_command:
                    effective_cwd = snapshot.workspace.resolve_cwd(arguments.get("cwd"))
                    pending = check_compute_review(
                        arguments["command"], effective_cwd, snapshot.workspace.executor.config.ssh_target,
                        arguments.get("compute_review"),
                        getattr(snapshot.workspace.executor.config, "compute_review_threshold", 8),
                    )
                    if pending is None:
                        evidence_record = audit.create(
                            kind="tool", tool="test_evidence_capture",
                            host=snapshot.host, cwd=effective_cwd,
                            command=arguments["command"], status="running",
                        )
                        evidence_token = audit.operation_id.set(evidence_record)
                try:
                    try:
                        if evidence_record is not None:
                            evidence_before = await test_evidence.capture(snapshot)
                        result = await tool(**arguments)
                    except BaseException:
                        if job_operation is not None:
                            try:
                                router.registry.update_job_operation(
                                    state.workspace_id, idempotency_key, "unknown")
                            except (sqlite3.Error, OSError, ValueError):
                                print("Remote Workbench job retry state unavailable", file=sys.stderr)
                        if evidence_record is not None:
                            audit.update(evidence_record, status="unknown",
                                         error="Command response was unavailable; test execution state is unknown")
                        raise
                    if evidence_record is not None:
                        if result.get("executed") is False:
                            audit.update(evidence_record, status="rejected",
                                         message=result.get("message", "Test command was not executed"))
                        else:
                            evidence_after = await test_evidence.capture(snapshot)
                            observation = test_evidence.observation(
                                evidence_before, evidence_after, result, arguments["command"], state.revision)
                            try:
                                router.registry.save_test(
                                    state.workspace_id, snapshot.target_id, observation)
                            except (sqlite3.Error, OSError):
                                print("Remote Workbench test observation unavailable", file=sys.stderr)
                            if result.get("operation_id"):
                                audit.update(evidence_record, parent_id=result["operation_id"])
                                audit.add_child(result["operation_id"], evidence_record)
                            audit.update(evidence_record, status="finished",
                                         test_observation=observation,
                                         output_id=result.get("output_id"))
                            result = dict(result, test_evidence={
                                "status": observation["status"],
                                "applies_to_current_code": observation["applies_to_current_code"],
                                "evidence": observation["evidence"],
                                "environment_evidence": observation["environment_evidence"],
                                "environment_limit": observation["environment_limit"],
                            }, test_evidence_operation_id=evidence_record)
                finally:
                    if evidence_token is not None:
                        audit.operation_id.reset(evidence_token)
            if job_operation is not None:
                if result.get("executed") is False:
                    operation_state = "rejected"
                elif not result.get("error") and result.get("job_id") == job_operation.job_id:
                    operation_state = "submitted"
                else:
                    operation_state = "unknown"
                try:
                    router.registry.update_job_operation(
                        state.workspace_id, idempotency_key, operation_state)
                except (sqlite3.Error, OSError, ValueError):
                    # Do not turn a possibly successful remote start into a
                    # tool error that encourages an unkeyed duplicate retry.
                    print("Remote Workbench job retry state unavailable", file=sys.stderr)
                result = dict(result, idempotency_key=idempotency_key,
                              operation_state=operation_state, job_id=job_operation.job_id)
            return result

        register(name, public, description, execute)
    return server


def main():
    parser = argparse.ArgumentParser(description="Local, persistent Codex thread-aware remote workspace MCP (prototype)")
    parser.add_argument("--config", help="Host allowlist YAML; uses the normal Remote Workbench configuration by default")
    parser.add_argument("--data-dir", type=Path, default=default_state_dir(), help="Private local registry/audit directory")
    parser.add_argument("--viewer-port", type=int, default=0, help="Local viewer port (default 0: OS-assigned); falls back if occupied")
    parser.add_argument("--identity-probe", action="store_true", help="Expose only a local metadata diagnostic, without host configuration or remote tools")
    args = parser.parse_args()
    if args.identity_probe:
        directory = args.data_dir.expanduser().resolve() / "identity-probe"
        with _local_audit_viewer(args.viewer_port, audit_dir=directory) as viewer:
            url = viewer.viewer_url
            create_identity_probe(viewer_url=url, audit_dir=directory).run()
        return
    router = SessionRouter(load_config(args.config), args.data_dir)
    with _local_audit_viewer(args.viewer_port, handler=handler_for_workspaces(router.state_dir / "audit")) as viewer:
        url = viewer.viewer_url
        print(f"Local session audit viewer: {url}", file=sys.stderr, flush=True)
        create_server(router, viewer_url=url).run()


if __name__ == "__main__":
    main()
