"""FastMCP server instance and executor lifecycle management."""

from __future__ import annotations

from mcp.server.fastmcp import FastMCP
import functools
import inspect
import time
from contextvars import ContextVar
from remote_workbench import audit

from remote_workbench.config import get_config, HostConfig
from remote_workbench.ssh import SSHExecutor

class AuditedMCP(FastMCP):
    def tool(self, *args, **kwargs):
        register = super().tool(*args, **kwargs)
        def decorate(function):
            @functools.wraps(function)
            async def call(*a, **kw):
                # Reading audit records should not generate recursive audit noise.
                if function.__name__ in {"ssh_read_output", "ssh_audit_list", "ssh_audit_get"}:
                    return await function(*a, **kw)
                bound = inspect.signature(function).bind(*a, **kw)
                record_id = audit.create(kind="tool", tool=function.__name__, arguments=dict(bound.arguments),
                                         host=bound.arguments.get("host"), cwd=bound.arguments.get("cwd"), status="running")
                token = audit.operation_id.set(record_id)
                started = time.monotonic()
                try:
                    result = await function(*a, **kw)
                    audit.update(record_id, status="failed" if isinstance(result, dict) and result.get("error") else "finished",
                                 output_id=result.get("output_id") if isinstance(result, dict) else None,
                                 elapsed_ms=round((time.monotonic()-started)*1000))
                    if isinstance(result, dict):
                        result.setdefault("operation_id", record_id)
                    return result
                except BaseException as exc:
                    audit.update(record_id, status="failed", error=str(exc))
                    raise
                finally:
                    audit.operation_id.reset(token)
            register(call)
            return call
        return decorate


mcp = AuditedMCP(
    "remote-workbench-ssh",
    instructions=(
        "Use explicit host and an existing ControlMaster. Inspect, edit and test on the remote. "
        "Use ssh_patch_file for unique/batch replacements and ssh_apply_patch for multi-file patches. "
        "ssh_git_diff defaults to summary; mode='patch' pages a saved snapshot. Use output_id "
        "with ssh_read_output for full saved output. Verify edits with diff/status and relevant checks. "
        "If disconnected, ask the user to reconnect."
    ),
)

# Executor cache: one per host
_executors: dict[str, SSHExecutor] = {}

# A bound workspace supplies its executor per request, without mutating global
# host configuration. Independent workspace servers/tasks cannot change it.
workspace_executor: ContextVar[tuple[str, SSHExecutor] | None] = ContextVar(
    "remote-workbench_workspace_executor", default=None
)


def get_executor(host: str | None = None) -> SSHExecutor:
    """Get or create an SSHExecutor for the given host."""
    binding = workspace_executor.get()
    if binding is not None:
        alias, executor = binding
        if host is not None and host != alias:
            raise ValueError("This workspace is bound to a different host")
        return executor
    config = get_config()
    host_config = config.get_host(host)
    key = host or config.default_host or next(iter(config.hosts))
    if key not in _executors:
        _executors[key] = SSHExecutor(host_config)
    return _executors[key]


async def require_connection(host: str | None = None) -> SSHExecutor:
    """Get an executor and verify the connection is alive.

    Raises ValueError with a helpful message if disconnected.
    """
    executor = get_executor(host)
    connected, message = await executor.check_connection()
    if not connected:
        raise ValueError(message)
    return executor


# Import tool modules to register them with the mcp instance.
# These modules use `from remote_workbench.server import mcp` and decorate functions with @mcp.tool().
from remote_workbench.tools import connection  # noqa: E402, F401
from remote_workbench.tools import commands  # noqa: E402, F401
from remote_workbench.tools import files  # noqa: E402, F401
from remote_workbench.tools import search  # noqa: E402, F401
from remote_workbench.tools import git  # noqa: E402, F401
from remote_workbench.tools import audit as audit_tools  # noqa: E402, F401
from remote_workbench.tools import patch  # noqa: E402, F401
