"""Boundary regressions; only disposable test files/processes are changed."""
import asyncio
import hashlib
import json
from pathlib import Path
import shlex
import stat
from unittest.mock import patch as mock_patch

import pytest
from remote_workbench import audit
from remote_workbench.tools import files, search, commands


@pytest.mark.asyncio
@pytest.mark.parametrize('operation',['mkdir','info','move','delete','download'])
async def test_option_like_filename_is_a_path(local,operation):
    _,root=local
    target=root/'-source.txt';target.write_text('payload')
    if operation=='mkdir':
        result=await files.ssh_mkdir('-folder')
        assert result.get('success'),result
        assert (root/'-folder').is_dir()
    elif operation=='info':
        result=await files.ssh_file_info('-source.txt')
        assert result.get('exists') and result['size']==7,result
    elif operation=='move':
        result=await files.ssh_move('-source.txt','-dest.txt')
        assert result.get('success'),result
        assert (root/'-dest.txt').read_text()=='payload' and not target.exists()
    elif operation=='delete':
        result=await files.ssh_delete('-source.txt')
        assert result.get('success'),result
        assert not target.exists()
    else:
        with mock_patch('remote_workbench.tools.files.os.path.expanduser',side_effect=lambda p:str(root) if p=='~' else p):
            result=await files.ssh_download('-source.txt',local_dir=str(root/'downloads'))
        assert result.get('success'),result
        assert Path(result['local_path']).read_text()=='payload'


@pytest.mark.asyncio
async def test_search_errors_and_option_like_pattern(local):
    _,root=local
    (root/'text.txt').write_text('-needle\nplain\n')
    value=await search.ssh_grep('-needle',path='text.txt')
    assert value.get('count')==1,value
    value=await search.ssh_grep('anything',path='absent')
    assert value.get('error')=='grep_failed',value
    value=await search.ssh_find_files(path='absent')
    assert value.get('error')=='find_failed',value
    value=await files.ssh_list_dir(path='absent')
    assert value.get('error')=='list_failed',value


@pytest.mark.asyncio
async def test_background_strict_utf8_budget(local):
    job=await commands.ssh_run_background("python3 -c 'import sys;sys.stdout.write(\"😀\"*100);sys.stderr.write(\"中\"*100)'")
    oc=ec=0;out=err=''
    for _ in range(40):
        page=await commands.ssh_job_status(job['job_id'],stdout_cursor=oc,stderr_cursor=ec,max_bytes=258)
        assert len(page['stdout_tail'].encode())+len(page['stderr_tail'].encode())<=258,page
        out+=page['stdout_tail'];err+=page['stderr_tail'];oc=page['next_stdout_cursor'];ec=page['next_stderr_cursor']
        if not page['running'] and not page['stdout_has_more'] and not page['stderr_has_more']:break
        await asyncio.sleep(0.03)
    assert out=='😀'*100 and err=='中'*100


@pytest.mark.asyncio
async def test_concurrent_hash_compare_and_append_no_lost_updates(local):
    _,root=local
    target=root/'shared';target.write_text('base\n')
    digest=hashlib.sha256(target.read_bytes()).hexdigest()
    a,b=await asyncio.gather(files.ssh_patch_file('shared',old='base',new='first',expected_sha256=digest),files.ssh_patch_file('shared',old='base',new='second',expected_sha256=digest))
    assert sum(bool(value.get('success')) for value in (a,b))==1
    assert sum(value.get('error')=='conflict' for value in (a,b))==1
    results=await asyncio.gather(*(files.ssh_append_file('shared',f'token-{i}\n') for i in range(8)))
    assert all(value.get('success') for value in results)
    lines=target.read_text().splitlines()
    assert sorted(lines[1:])==[f'token-{i}' for i in range(8)]


@pytest.mark.asyncio
async def test_signal_exit_code_and_poll_before_finish(local):
    job=await commands.ssh_run_background("python3 -c 'import os,signal,sys,time;sys.stdout.write(\"ready\");sys.stdout.flush();time.sleep(1);os.kill(os.getpid(),signal.SIGTERM)'")
    output='';oc=0;running=False
    for _ in range(50):
        page=await commands.ssh_job_status(job['job_id'],stdout_cursor=oc)
        output+=page['stdout_tail'];oc=page['next_stdout_cursor']
        running|=page['running']
        if not page['running']:break
        await asyncio.sleep(0.03)
    assert running and page['exit_code']==143 and output=='ready',page


@pytest.mark.asyncio
async def test_binary_download_roundtrip(local):
    _,root=local
    raw=bytes(range(256))*256
    (root/'binary.bin').write_bytes(raw)
    with mock_patch('remote_workbench.tools.files.os.path.expanduser',side_effect=lambda p:str(root) if p=='~' else p):
        result=await files.ssh_download('binary.bin',local_dir=str(root/'downloads'))
    assert result.get('success'),result
    assert Path(result['local_path']).read_bytes()==raw


@pytest.mark.asyncio
async def test_crlf_no_eof_and_shell_metacharacter_path(local):
    _,root=local
    name="空 格 ' $(touch unintended)\nfile"
    original='one\r\ntwo'
    (root/name).write_bytes(original.encode())
    (root/name).chmod(0o750)
    result=await files.ssh_patch_file(name,old='two',new='中文')
    assert result.get('success'),result
    assert (root/name).read_bytes()=='one\r\n中文'.encode()
    assert stat.S_IMODE((root/name).stat().st_mode)==0o750
    assert not (root/'unintended').exists()
    assert '\\ No newline at end of file' in result['diff']


@pytest.mark.asyncio
async def test_download_rejects_home_prefix_sibling_and_replaces_symlink(local):
    _,root=local
    home=root/'fake-home';home.mkdir()
    target=root/'remote.txt';target.write_text('download')
    outside=root/'protected.txt';outside.write_text('protected')
    with mock_patch('remote_workbench.tools.files.os.path.expanduser',side_effect=lambda p:str(home) if p=='~' else p):
        rejected=await files.ssh_download('remote.txt',local_dir=str(root/'fake-home-sibling'))
        assert rejected.get('error')=='invalid_path',rejected
        assert not (root/'fake-home-sibling').exists()
        (home/'remote.txt').symlink_to(outside)
        result=await files.ssh_download('remote.txt',local_dir=str(home))
    assert result.get('success'),result
    assert not (home/'remote.txt').is_symlink()
    assert (home/'remote.txt').read_text()=='download'
    assert outside.read_text()=='protected'


@pytest.mark.asyncio
async def test_grep_limits_and_invalid_regex_are_not_success(local):
    _,root=local
    (root/'many.txt').write_text('match\n'*20000)
    value=await search.ssh_grep('match',path='many.txt',max_results=2)
    assert value.get('count')==2 and value['truncated'],value
    value=await search.ssh_grep('[',path='many.txt')
    assert value.get('error')=='grep_failed',value
    value=await search.ssh_grep('absent',path='many.txt')
    assert value.get('count')==0 and not value.get('error'),value
