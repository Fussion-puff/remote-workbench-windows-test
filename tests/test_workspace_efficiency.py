import errno
import json
import socket
from unittest.mock import AsyncMock, MagicMock, patch
from urllib.request import urlopen

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


def test_local_audit_viewer_falls_back_only_when_requested_port_is_occupied():
    viewer = MagicMock()
    viewer.server_port = 43210
    bind_error = OSError(errno.EADDRINUSE, "Address already in use")
    server_factory = MagicMock(side_effect=[bind_error, viewer])
    thread = MagicMock()

    with patch.object(workspace_module, "ThreadingHTTPServer", server_factory), \
         patch.object(workspace_module, "Thread", return_value=thread) as thread_factory:
        with workspace_module._local_audit_viewer(8768) as active:
            assert active is viewer

    assert [call.args for call in server_factory.call_args_list] == [
        (("127.0.0.1", 8768), workspace_module.AuditViewerHandler),
        (("127.0.0.1", 0), workspace_module.AuditViewerHandler),
    ]
    thread_factory.assert_called_once_with(
        target=viewer.serve_forever,
        kwargs={"poll_interval": 0.1},
        name="remote-workbench-audit-viewer-43210",
        daemon=True,
    )
    thread.start.assert_called_once_with()
    viewer.shutdown.assert_called_once_with()
    viewer.server_close.assert_called_once_with()
    thread.join.assert_called_once_with()


@pytest.mark.parametrize("error_number", [errno.EACCES, errno.EADDRNOTAVAIL])
def test_local_audit_viewer_preserves_unrelated_bind_errors(error_number):
    bind_error = OSError(error_number, "bind failed")
    server_factory = MagicMock(side_effect=bind_error)
    thread_factory = MagicMock()

    with patch.object(workspace_module, "ThreadingHTTPServer", server_factory), \
         patch.object(workspace_module, "Thread", thread_factory):
        with pytest.raises(OSError) as caught:
            with workspace_module._local_audit_viewer(8768):
                pytest.fail("viewer must not start when bind fails")

    assert caught.value is bind_error
    server_factory.assert_called_once_with(("127.0.0.1", 8768), workspace_module.AuditViewerHandler)
    thread_factory.assert_not_called()


def test_local_audit_viewer_preserves_fallback_bind_error():
    occupied_error = OSError(errno.EADDRINUSE, "Address already in use")
    fallback_error = OSError(errno.EMFILE, "Too many open files")
    server_factory = MagicMock(side_effect=[occupied_error, fallback_error])
    thread_factory = MagicMock()

    with patch.object(workspace_module, "ThreadingHTTPServer", server_factory), \
         patch.object(workspace_module, "Thread", thread_factory):
        with pytest.raises(OSError) as caught:
            with workspace_module._local_audit_viewer(8768):
                pytest.fail("viewer must not start when the fallback bind fails")

    assert caught.value is fallback_error
    assert [call.args for call in server_factory.call_args_list] == [
        (("127.0.0.1", 8768), workspace_module.AuditViewerHandler),
        (("127.0.0.1", 0), workspace_module.AuditViewerHandler),
    ]
    thread_factory.assert_not_called()


def test_local_audit_viewer_uses_separate_live_listener_when_port_is_occupied():
    # Release an OS-selected loopback port, then let the first managed viewer
    # claim it. If another process wins that tiny race, its own fallback port
    # becomes the port deliberately occupied by the second managed viewer.
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as reservation:
        reservation.bind(("127.0.0.1", 0))
        requested_port = reservation.getsockname()[1]

    def assert_serving(port):
        with urlopen(f"http://127.0.0.1:{port}/", timeout=2) as response:
            assert response.status == 200
            assert b"Remote Workbench" in response.read(4096)

    with workspace_module._local_audit_viewer(requested_port) as first:
        occupied_port = first.server_port
        assert_serving(occupied_port)

        with workspace_module._local_audit_viewer(occupied_port) as second:
            assert second.server_port != occupied_port
            assert_serving(occupied_port)
            assert_serving(second.server_port)

        assert second.fileno() == -1
        assert first.fileno() >= 0
        assert_serving(occupied_port)

    assert first.fileno() == -1


async def test_workspace_info_includes_only_the_viewer_url_for_its_mcp(local):
    executor, root = local
    executor.check_connection = AsyncMock(return_value=(False, "Disconnected"))
    workspace = Workspace("viewer-link", "remote", str(root), executor)
    viewer_url = "http://127.0.0.1:43210"

    with_viewer = create_server(workspace, viewer_url=viewer_url)
    response = decode_content(await with_viewer.call_tool("workspace_info", {}))
    assert response["viewer_url"] == viewer_url

    without_viewer = create_server(workspace)
    response = decode_content(await without_viewer.call_tool("workspace_info", {}))
    assert "viewer_url" not in response


@pytest.mark.parametrize("port", [0, -1, 65536, True])
def test_local_audit_viewer_rejects_invalid_port(port):
    with patch.object(workspace_module, "ThreadingHTTPServer") as server_factory:
        with pytest.raises(ValueError, match="viewer port"):
            with workspace_module._local_audit_viewer(port):
                pass
        server_factory.assert_not_called()
