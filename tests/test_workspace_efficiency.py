import json
from unittest.mock import MagicMock, patch

from mcp.types import CallToolResult, TextContent
import pytest

from remote_workbench.ssh import SSHExecutor
from remote_workbench import workspace as workspace_module
from remote_workbench.workspace import Workspace, create_server


def decode_content(blocks):
    assert len(blocks) == 1
    assert isinstance(blocks[0], TextContent)
    return json.loads(blocks[0].text)


def wire_size(result):
    return len(result.model_dump_json(by_alias=True, exclude_none=True).encode("utf-8"))


async def test_workspace_result_is_single_compact_payload_and_output_is_pageable(local):
    executor, root = local
    workspace = Workspace("efficiency", "remote", str(root), executor)
    server = create_server(workspace)

    # A typical long command output should be sent once, bounded to the
    # workspace default, while the complete stream remains locally pageable.
    blocks = await server.call_tool(
        "exec_command",
        {"command": "python3 -c 'print(\"x\" * 5000, end=\"\")'"},
    )
    command_result = decode_content(blocks)
    assert command_result["exit_code"] == 0
    assert command_result["truncated"] is True
    assert command_result["output_id"]
    assert "[truncated bytes]" in command_result["stdout"]

    # FastMCP's previous default emitted pretty JSON text and the same object
    # again as structuredContent. Compare that exact envelope with this one.
    after = CallToolResult(content=list(blocks))
    legacy = CallToolResult(
        content=[TextContent(type="text", text=json.dumps(command_result, ensure_ascii=False, indent=2))],
        structuredContent=command_result,
    )
    after_bytes, legacy_bytes = wire_size(after), wire_size(legacy)
    assert after_bytes < legacy_bytes * 0.60

    pages = []
    cursor = 0
    while True:
        page = decode_content(await server.call_tool(
            "read_output",
            {"output_id": command_result["output_id"], "stream": "stdout", "cursor": cursor, "max_bytes": 4096},
        ))
        pages.append(page["content"])
        cursor = page["next_cursor"]
        if not page["truncated"]:
            break
    assert "".join(pages) == "x" * 5000

    # A nonzero command retains its diagnostic fields in the compact response.
    failure = decode_content(await server.call_tool(
        "exec_command", {"command": "printf diagnostic >&2; exit 23"}
    ))
    assert failure["exit_code"] == 23
    assert failure["stderr"].strip() == "diagnostic"
    assert failure["execution_state"] == "finished"
    assert failure["output_id"]

    # Edit diffs remain immediately visible and can also be read from the
    # saved diff stream if their preview is capped.
    written = decode_content(await server.call_tool(
        "write_file", {"path": "review.txt", "content": "before\n"}
    ))
    assert written["success"]
    diff = decode_content(await server.call_tool(
        "patch_file", {"path": "review.txt", "old": "before", "new": "after"}
    ))
    assert "-before" in diff["diff"] and "+after" in diff["diff"]
    assert diff["output_id"]
    saved_diff = decode_content(await server.call_tool(
        "read_output", {"output_id": diff["output_id"], "stream": "diff", "max_bytes": 4096}
    ))
    assert "-before" in saved_diff["content"] and "+after" in saved_diff["content"]


def test_local_audit_viewer_is_optional_loopback_and_closes_on_exit():
    events = []
    viewer = MagicMock()
    viewer.server_port = 8767
    thread = MagicMock()
    server_factory = MagicMock(return_value=viewer)
    thread_factory = MagicMock(return_value=thread)
    viewer.shutdown.side_effect = lambda: events.append("shutdown")
    viewer.server_close.side_effect = lambda: events.append("close")
    thread.join.side_effect = lambda: events.append("join")

    with patch.object(workspace_module, "ThreadingHTTPServer", server_factory), \
         patch.object(workspace_module, "Thread", thread_factory):
        with workspace_module._local_audit_viewer(None) as disabled:
            assert disabled is None
        server_factory.assert_not_called()

        with pytest.raises(RuntimeError, match="leave MCP"):
            with workspace_module._local_audit_viewer(8767) as active:
                assert active is viewer
                server_factory.assert_called_once_with(("127.0.0.1", 8767), workspace_module.AuditViewerHandler)
                thread_factory.assert_called_once_with(
                    target=viewer.serve_forever,
                    kwargs={"poll_interval": 0.1},
                    name="remote-workbench-audit-viewer-8767",
                    daemon=True,
                )
                thread.start.assert_called_once_with()
                raise RuntimeError("leave MCP")

    assert events == ["shutdown", "close", "join"]


@pytest.mark.parametrize("port", [0, -1, 65536, True])
def test_local_audit_viewer_rejects_invalid_port(port):
    with patch.object(workspace_module, "ThreadingHTTPServer") as server_factory:
        with pytest.raises(ValueError, match="viewer port"):
            with workspace_module._local_audit_viewer(port):
                pass
        server_factory.assert_not_called()
