"""File worker errors and guarded edits against actual temporary files."""
import pytest
from remote_workbench.tools.files import ssh_read_file, ssh_patch_file, ssh_write_file, ssh_replace_lines


@pytest.mark.asyncio
async def test_missing_and_binary(local):
    _,path=local
    assert (await ssh_read_file(str(path/'missing')))['error']=='remote_operation_failed'
    binary=path/'binary';binary.write_bytes(b'\xff\x00')
    assert (await ssh_read_file(str(binary)))['error']=='remote_operation_failed'


@pytest.mark.asyncio
async def test_ambiguous_and_empty_old(local):
    _,path=local
    file=path/'text';file.write_text('aa aa')
    assert (await ssh_patch_file(str(file),old='aa',new='bb'))['error']=='ambiguous'
    with pytest.raises(ValueError):await ssh_patch_file(str(file),old='',new='bb')
    assert file.read_text()=='aa aa'


@pytest.mark.asyncio
async def test_preserve_other_files_and_shell_characters(local):
    _,path=local
    file=path/'$(touch surprise).txt'
    assert (await ssh_write_file(str(file),'first'))['success']
    assert file.read_text()=='first' and not (path/'surprise').exists()
    link=path/'symlink';link.symlink_to(file)
    assert (await ssh_write_file(str(link),'changed'))['error']=='remote_operation_failed'
    assert file.read_text()=='first'


@pytest.mark.asyncio
async def test_invalid_range_no_write(local):
    _,path=local
    file=path/'text';file.write_text('one\n')
    with pytest.raises(ValueError):await ssh_replace_lines(str(file),0,1,'bad')
    assert (await ssh_replace_lines(str(file),1,99,'bad'))['error']=='remote_operation_failed'
    assert file.read_text()=='one\n'


@pytest.mark.asyncio
async def test_max_file_size_no_partial_write(local):
    executor,path=local
    executor.config.max_file_size=10
    file=path/'text';file.write_text('original')
    assert (await ssh_write_file(str(file),'x'*11))['error']=='remote_operation_failed'
    assert file.read_text()=='original'
