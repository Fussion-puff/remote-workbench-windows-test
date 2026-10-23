import json
import sys

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client


async def test_real_stdio_metadata_and_process_restart(tmp_path):
    config = tmp_path / "hosts.yaml"
    config.write_text('hosts:\n  test:\n    ssh_host: never-connect.invalid\n')
    params = StdioServerParameters(command=sys.executable, args=[
        "-m", "remote_workbench.session_server", "--config", str(config),
        "--data-dir", str(tmp_path / "state"), "--viewer-port", "19869"])

    async def inspect_process():
        async with stdio_client(params) as (reader, writer):
            async with ClientSession(reader, writer) as client:
                await client.initialize()
                missing = await client.call_tool("workspace_info")
                assert missing.isError and "threadId is unavailable" in missing.content[0].text
                first = await client.call_tool("workspace_info", meta={"threadId": "thread-a"})
                second = await client.call_tool("workspace_info", meta={"threadId": "thread-b"})
                a, b = json.loads(first.content[0].text), json.loads(second.content[0].text)
                assert not first.isError and not second.isError
                assert a["workspace_id"] != b["workspace_id"]
                assert not a["bound"] and not b["bound"]
                assert "operation_log" in a
                return a["workspace_id"], b["workspace_id"]

    assert await inspect_process() == await inspect_process()
