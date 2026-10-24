"""Bounded command execution and detached jobs with incremental output."""
from __future__ import annotations
import re
import uuid
from remote_workbench.server import mcp, require_connection, get_executor
from remote_workbench.remote_client import edit
from remote_workbench.compute_review import check_compute_review

_SAFE_JOB_ID = re.compile(r"^[a-zA-Z0-9_-]{1,80}$")


@mcp.tool()
async def ssh_run(command: str, cwd: str | None = None, timeout: int | None = None,
                  host: str | None = None, max_bytes: int = 16384, trace: bool = False,
                  compute_review: dict | None = None) -> dict:
    """Execute using the remote default shell's interactive login configuration.

    Return head/tail previews bounded by a shared stdout+stderr byte budget.
    A remote supervisor enforces the deadline and cleans its workload scope.
    Inspect remote_task.cleanup_state; transport loss can leave state unknown.
    trace=True saves expanded shell command steps in stderr, including arguments.
    Substantial computation requires explicit user confirmation of command,
    concurrency and environment; record it in compute_review. No OS PID cap.
    """
    config = get_executor(host).config
    pending = check_compute_review(command, cwd or config.default_cwd, config.ssh_target,
                                   compute_review, config.compute_review_threshold)
    if pending:
        return pending
    executor = await require_connection(host)
    result = await executor.run(command, cwd=cwd, timeout=timeout, output_budget=max_bytes, trace=trace)
    return {"stdout": result.stdout, "stderr": result.stderr, "exit_code": result.exit_code,
            "timed_out": result.timed_out, "truncated": result.truncated,
            "output_id": result.output_id, "execution_state": result.execution_state,
            "remote_task": result.remote_task}


@mcp.tool()
async def ssh_run_background(command: str, cwd: str | None = None, job_id: str = "", host: str | None = None,
                             timeout: int | None = None, compute_review: dict | None = None) -> dict:
    """Start a supervised detached job with a finite remote deadline.

    Job directories are private and exclusive. Poll using returned byte cursors
    to avoid repeatedly receiving the same output.
    """
    job_id = job_id or uuid.uuid4().hex
    if not _SAFE_JOB_ID.fullmatch(job_id):
        return {"error": "invalid_job_id"}
    config = get_executor(host).config
    pending = check_compute_review(command, cwd or config.default_cwd, config.ssh_target,
                                   compute_review, config.compute_review_threshold)
    if pending:
        return pending
    executor = await require_connection(host)
    if timeout is not None and timeout <= 0:
        raise ValueError("timeout must be positive")
    return await edit(executor, {"op": "job_start", "job_id": job_id, "command": command, "timeout": timeout},
                      cwd=cwd or executor.config.default_cwd)


@mcp.tool()
async def ssh_stop_job(job_id: str, host: str | None = None) -> dict:
    """Request TERM -> KILL of a supervised job, even with execution disabled.

    Refuses foreign-node, reused-PID and legacy handles. Acknowledgement is
    separate from a request; inspect cleanup_state and tree_confirmed.
    """
    if not _SAFE_JOB_ID.fullmatch(job_id):
        return {"error": "invalid_job_id"}
    return await get_executor(host).cancel_task(job_id)


@mcp.tool()
async def ssh_job_status(job_id: str, tail_lines: int = 50, host: str | None = None,
                         stdout_cursor: int = 0, stderr_cursor: int = 0, max_bytes: int = 8192) -> dict:
    """Get job state and incremental output. Reuse next_stdout_cursor/next_stderr_cursor.

    tail_lines is a compatibility display cap; cursors count bytes in the remote
    logs. Per-stream pages are limited to max_bytes/2 before the display line cap.
    running=None and execution_state='unknown' mean node/process identity could
    not be verified. Missing completion is never proof that a job has stopped.
    """
    if not _SAFE_JOB_ID.fullmatch(job_id):
        return {"error": "invalid_job_id"}
    if min(stdout_cursor, stderr_cursor) < 0 or not 1 <= tail_lines <= 1000 or not 256 <= max_bytes <= 65536:
        raise ValueError("Invalid cursor/tail_lines/max_bytes")
    executor = await require_connection(host)
    return await edit(executor, {"op": "job_status", "job_id": job_id, "stdout_cursor": stdout_cursor,
        "stderr_cursor": stderr_cursor, "budget": max_bytes // 2, "tail_lines": tail_lines}, max_bytes=max_bytes)
