"""Read locally stored command records without executing remote commands."""
import json
from remote_workbench import audit
from remote_workbench.server import mcp


@mcp.tool()
async def ssh_read_output(output_id: str, stream: str = "stdout", cursor: int = 0, max_bytes: int = 8192) -> dict:
    """Read bounded stdout/stderr/diff/stdin or separate startup_stdout/startup_stderr."""
    return audit.read_output(output_id, stream, cursor, max_bytes)


@mcp.tool()
async def ssh_audit_list(limit: int = 30) -> dict:
    """List recent operation metadata; retrieve a record only when needed."""
    return {"records": audit.recent(limit)}


@mcp.tool()
async def ssh_audit_get(operation_id: str) -> dict:
    """Get command, wrapper, arguments, execution status and child command IDs."""
    return json.loads((audit.directory(operation_id) / "record.json").read_text())
