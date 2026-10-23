"""Command tool contracts using the actual bounded execution layer."""
import pytest
from remote_workbench.tools.commands import ssh_run


@pytest.mark.asyncio
async def test_success(local):
    value=await ssh_run('printf hello')
    assert value['stdout']=='hello' and value['exit_code']==0
    assert value['operation_id'] and value['output_id']


@pytest.mark.asyncio
async def test_nonzero(local):
    value=await ssh_run('printf error >&2; exit 9')
    assert value['exit_code']==9 and value['stderr']=='error'


@pytest.mark.asyncio
async def test_run_budget(local):
    value=await ssh_run("python3 -c 'print(\"x\"*10000)'",max_bytes=256)
    assert value['truncated'] and len(value['stdout'].encode())<=256


@pytest.mark.asyncio
async def test_invalid_budget(local):
    with pytest.raises(ValueError):await ssh_run('true',max_bytes=-1)
