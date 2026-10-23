"""Opt-in additional real MCP/SSH boundary tests. No SSH master is stopped."""
from __future__ import annotations
import argparse
import asyncio
from dataclasses import asdict
from datetime import datetime
import difflib
import hashlib
import json
import os
from pathlib import Path
import shlex
import tempfile
import time
from zoneinfo import ZoneInfo
import yaml
from remote_workbench.config import get_config
if __package__:
    from .live_remote_test import connect,invoke,py,PROJECT
else:
    from live_remote_test import connect,invoke,py,PROJECT


async def run(options):
    host=options.host
    report={'host':host,'time':datetime.now(ZoneInfo('Asia/Shanghai')).isoformat(),'checks':[],
            'audit_directory':os.environ['REMOTE_WORKBENCH_AUDIT_DIR']}
    output=Path(options.report).resolve();output.parent.mkdir(parents=True,exist_ok=True)
    def save():
        report['passed']=sum(c['status']=='PASS' for c in report['checks'])
        report['failed']=sum(c['status']=='FAIL' for c in report['checks'])
        output.write_text(json.dumps(report,ensure_ascii=False,indent=2)+'\n')
    async def check(name,function):
        if options.only and name != options.only:
            return
        started=time.monotonic()
        try:
            details=await function()
            value={'name':name,'status':'PASS','details':details}
            print('PASS '+name,flush=True)
        except Exception as exc:
            value={'name':name,'status':'FAIL','error':str(exc)[:2200]}
            print('FAIL '+name+': '+str(exc)[:1000],flush=True)
        value['elapsed_seconds']=round(time.monotonic()-started,3)
        report['checks'].append(value);save()
    try:
        async with connect() as session:
            ready=await invoke(session,'ssh_check_connection',host=host)
            assert ready.get('connected'),ready
            code='import tempfile,json;print(json.dumps({"workspace":tempfile.mkdtemp(prefix="remote-workbench-boundary-20261023-",dir='+repr(options.parent)+')}))'
            result=await invoke(session,'ssh_run',host=host,command=py(code),cwd=options.parent,timeout=30,max_bytes=2048)
            assert result['exit_code']==0,result
            root=json.loads(result['stdout'])['workspace'];report['workspace']=root
            print('WORKSPACE '+root,flush=True);save()
        cfg=asdict(get_config().get_host(host));cfg['default_cwd']=root
        cfg['preamble']=(cfg['preamble']+'\nexport REMOTE_WORKBENCH_REMOTE_STATE_DIR='+shlex.quote(root+'/.remote-workbench-state')).strip()
        cfg['max_file_size']=1048576
        with tempfile.TemporaryDirectory(prefix='remote-workbench-boundary-config-') as temporary:
            config_path=Path(temporary)/'config.yaml'
            config_path.write_text(yaml.safe_dump({'default_host':host,'hosts':{host:cfg}}))
            async with connect(config_path) as session:
                sem=asyncio.Semaphore(4)
                async def call(name,**args):
                    async with sem:return await invoke(session,name,host=host,**args)
                async def command(text,**args):return await call('ssh_run',command=text,**args)
                async def write(path,content):
                    value=await call('ssh_write_file',path=path,content=content)
                    assert value.get('success'),value
                    return value
                async def read(path,**args):return await call('ssh_read_file',path=path,**args)
                async def hash_file(path):
                    result=await command(py('import hashlib;print(hashlib.sha256(open('+repr(path)+',"rb").read()).hexdigest())'))
                    assert result['exit_code']==0,result
                    return result['stdout'].strip()
                async def wait_job(job_id,**kwargs):
                    out=err='';oc=ec=0;observed_running=False
                    for _ in range(50):
                        value=await call('ssh_job_status',job_id=job_id,stdout_cursor=oc,stderr_cursor=ec,**kwargs)
                        assert 'running' in value,value
                        if 'max_bytes' in kwargs:
                            assert len(value['stdout_tail'].encode())+len(value['stderr_tail'].encode())<=kwargs['max_bytes'],value
                        out+=value['stdout_tail'];err+=value['stderr_tail'];oc=value['next_stdout_cursor'];ec=value['next_stderr_cursor']
                        observed_running|=value['running']
                        if not value['running'] and not value['stdout_has_more'] and not value['stderr_has_more']:
                            return value,out,err,observed_running
                        await asyncio.sleep(0.1)
                    raise AssertionError('Job did not finish within bounded polling')
                async def simultaneous_hash():
                    await write('shared.txt','base\n')
                    digest=(await read('shared.txt'))['sha256']
                    values=await asyncio.gather(*(call('ssh_patch_file',path='shared.txt',old='base',new=name,expected_sha256=digest) for name in ('winner-a','winner-b')))
                    assert sum(bool(v.get('success')) for v in values)==1,values
                    assert sum(v.get('error')=='conflict' for v in values)==1,values
                    return {'successes':1,'conflicts':1}
                await check('concurrent_compare_and_swap_has_single_winner',simultaneous_hash)
                async def append():
                    values=await asyncio.gather(*(call('ssh_append_file',path='shared.txt',content=f'token-{i}\n') for i in range(8)))
                    assert all(v.get('success') for v in values),values
                    content=(await read('shared.txt'))['content']
                    assert all(content.count(f'token-{i}')==1 for i in range(8)),content
                    return {'concurrent_appends':8,'lost_updates':0}
                await check('concurrent_appends_preserve_all_updates',append)
                async def dash_paths():
                    assert (await call('ssh_mkdir',path='-folder')).get('success')
                    await write('-source.txt','payload')
                    info=await call('ssh_file_info',path='-source.txt');assert info.get('size')==7,info
                    assert (await call('ssh_move',source='-source.txt',dest='-dest.txt')).get('success')
                    assert 'payload' in (await read('-dest.txt'))['content']
                    assert (await call('ssh_delete',path='-dest.txt')).get('success')
                    assert (await read('-dest.txt')).get('error')
                    return {'operations':['mkdir','stat','move','delete'],'literal_paths':True}
                await check('leading_dash_paths_are_not_cli_options',dash_paths)
                async def grep_errors():
                    await write('search.txt','-needle\n'+'match\n'*10000)
                    value=await call('ssh_grep',pattern='-needle',path='search.txt');assert value.get('count')==1,value
                    value=await call('ssh_grep',pattern='anything',path='missing');assert value.get('error')=='grep_failed',value
                    value=await call('ssh_grep',pattern='[',path='search.txt');assert value.get('error')=='grep_failed',value
                    value=await call('ssh_find_files',path='missing');assert value.get('error')=='find_failed',value
                    value=await call('ssh_list_dir',path='missing');assert value.get('error')=='list_failed',value
                    value=await call('ssh_grep',pattern='match',path='search.txt',max_results=2)
                    assert value.get('count')==2 and value['truncated'],value
                    return {'invalid_searches':'explicit errors','valid_limited_search_matches':2}
                await check('search_errors_literal_patterns_and_truncation',grep_errors)
                async def unicode_pages():
                    job=await call('ssh_run_background',command=py('import sys;sys.stdout.write("😀"*100);sys.stderr.write("中"*100)'))
                    assert 'job_id' in job,job
                    value,out,err,_=await wait_job(job['job_id'],max_bytes=258)
                    assert value['exit_code']==0 and out=='😀'*100 and err=='中'*100
                    return {'budget':258,'stdout_bytes':len(out.encode()),'stderr_bytes':len(err.encode()),'corruption':False}
                await check('background_utf8_paging_obeys_strict_byte_budget',unicode_pages)
                async def signal():
                    job=await call('ssh_run_background',command=py('import os,signal,time,sys;sys.stdout.write("ready");sys.stdout.flush();time.sleep(2);os.kill(os.getpid(),signal.SIGTERM)'))
                    assert 'job_id' in job,job
                    value,out,err,running=await wait_job(job['job_id'])
                    assert value['exit_code']==143 and out=='ready' and running,(value,out,err,running)
                    return {'signal_exit_code':143,'observed_running':running,'job_id':job['job_id']}
                await check('background_signal_exit_and_live_polling',signal)
                async def binary():
                    value=await command(py('from pathlib import Path;Path("-binary.bin").write_bytes(bytes(range(256))*256)'))
                    assert value['exit_code']==0,value
                    expected=hashlib.sha256(bytes(range(256))*256).hexdigest()
                    before=await hash_file('-binary.bin');assert before==expected
                    failed=await call('ssh_patch_file',path='-binary.bin',old='abc',new='xyz')
                    assert failed.get('error'),failed
                    assert await hash_file('-binary.bin')==before
                    local=PROJECT/'.remote-workbench-audit'/'boundary-downloads'
                    result=await call('ssh_download',remote_path='-binary.bin',local_dir=str(local))
                    assert result.get('success'),result
                    actual=Path(result['local_path']).read_bytes()
                    assert hashlib.sha256(actual).hexdigest()==expected
                    return {'binary_bytes':len(actual),'sha256':expected,'text_edit_rejected_without_write':True}
                await check('binary_download_hash_and_text_edit_rejection',binary)
                async def unusual_name():
                    name="空 格 ' $(touch UNEXPECTED)\nfile"
                    await write(name,'one\r\ntwo')
                    result=await command(py('import os;os.chmod('+repr(name)+',0o750)'));assert result['exit_code']==0,result
                    result=await call('ssh_patch_file',path=name,old='two',new='中文')
                    assert result.get('success') and '\\ No newline at end of file' in result['diff'],result
                    assert await hash_file(name)==hashlib.sha256('one\r\n中文'.encode()).hexdigest()
                    info=await call('ssh_file_info',path=name);assert info.get('permissions','').endswith('rwxr-x---'),info
                    assert (await command('test ! -e UNEXPECTED'))['exit_code']==0
                    return {'crlf_preserved':True,'no_final_newline':True,'mode':'0750','literal_metacharacters':True}
                await check('unicode_quotes_newline_filename_crlf_and_mode',unusual_name)
                async def limits():
                    await write('limit.txt','original')
                    rejected=await call('ssh_write_file',path='limit.txt',content='x'*(1048576+1))
                    assert rejected.get('error'),rejected
                    assert (await read('limit.txt'))['content']=='1\toriginal'
                    value=await command(py('from pathlib import Path;Path("too-large.txt").write_bytes(b"x"*(1048576+1))'));assert value['exit_code']==0,value
                    assert (await read('too-large.txt')).get('error')
                    return {'limit_bytes':1048576,'oversize_read_write':'rejected','original_unchanged':True}
                await check('configured_size_limit_does_not_partially_write',limits)
                large={}
                async def big_patch():
                    result=await command('mkdir large-repo && git -C large-repo init -q');assert result['exit_code']==0,result
                    before=''.join(f'old-{i:04d} '+('x'*120)+'\n' for i in range(2400))
                    after=before.replace('old-','new-')
                    await write('large-repo/large.txt',before)
                    result=await command('git add large.txt && git -c core.hooksPath=/dev/null -c commit.gpgsign=false -c user.name=Remote-Workbench-Test -c user.email=remote-workbench-test@localhost commit -qm baseline',cwd=root+'/large-repo');assert result['exit_code']==0,result
                    patch=''.join(difflib.unified_diff(before.splitlines(True),after.splitlines(True),fromfile='a/large.txt',tofile='b/large.txt'))
                    result=await call('ssh_apply_patch',cwd=root+'/large-repo',patch=patch)
                    assert result.get('success'),result
                    assert await hash_file('large-repo/large.txt')==hashlib.sha256(after.encode()).hexdigest()
                    large.update(expected=after,patch_bytes=len(patch.encode()))
                    return {'patch_bytes':len(patch.encode()),'lines_changed':2400,'sha256':hashlib.sha256(after.encode()).hexdigest()}
                await check('large_multi_hundred_kib_patch_integrity',big_patch)
                async def oversized_hunk():
                    page=await call('ssh_git_diff',cwd=root+'/large-repo',mode='patch',max_bytes=512)
                    if 'diff' in page and page['truncated']:
                        page=await call('ssh_git_diff',mode='patch',output_id=page['output_id'],cursor=page['next_cursor'],max_bytes=512)
                    assert page.get('error')=='hunk_exceeds_budget',page
                    digest=hashlib.sha256();cursor=0;total=0;pages=0
                    while True:
                        part=await invoke(session,'ssh_read_output',output_id=page['output_id'],cursor=cursor,max_bytes=65536)
                        raw=part['content'].encode();digest.update(raw);total+=len(raw);pages+=1;cursor=part['next_cursor']
                        if not part['truncated']:break
                    value=await command(py('import subprocess,hashlib;print(hashlib.sha256(subprocess.check_output(["git","--no-pager","diff","--no-ext-diff","--no-textconv","--no-color"])).hexdigest())'),cwd=root+'/large-repo')
                    assert value['exit_code']==0 and digest.hexdigest()==value['stdout'].strip(),value
                    return {'oversized_hunk':'explicit error','complete_diff_bytes':total,'pages':pages,'sha256':digest.hexdigest()}
                await check('oversized_hunk_and_full_diff_recovery',oversized_hunk)
                async def patch_guards():
                    patch='--- /dev/null\n+++ b/../outside-repo.txt\n@@ -0,0 +1 @@\n+bad\n'
                    value=await call('ssh_apply_patch',cwd=root+'/large-repo',patch=patch)
                    assert value.get('error')=='patch_conflict',value
                    assert (await command('test ! -e outside-repo.txt'))['exit_code']==0
                    assert (await command('git diff --cached --exit-code',cwd=root+'/large-repo'))['exit_code']==0
                    return {'outside_repository_path':'rejected','index_unchanged':True}
                await check('patch_path_protection_and_index_preservation',patch_guards)
                async def restart():
                    async with connect(config_path) as isolated:
                        launch=await invoke(isolated,'ssh_run_background',host=host,command="printf before; sleep 2; printf after; exit 4")
                        assert 'job_id' in launch,launch
                    async with connect(config_path) as replacement:
                        cursor=0;out=''
                        for _ in range(30):
                            value=await invoke(replacement,'ssh_job_status',host=host,job_id=launch['job_id'],stdout_cursor=cursor)
                            out+=value['stdout_tail'];cursor=value['next_stdout_cursor']
                            if not value['running']:break
                            await asyncio.sleep(0.1)
                        assert value['exit_code']==4 and out=='beforeafter',value
                    return {'job_id':launch['job_id'],'independent_mcp_process_restarted':True,'ssh_master_stopped':False}
                await check('background_survives_independent_mcp_process_restart',restart)
                async def final():
                    value=await command('git diff --check',cwd=root+'/large-repo');assert value['exit_code']==0,value
                    recent=await invoke(session,'ssh_audit_list',limit=5);assert recent['records']
                    return {'git_diff_check':True,'audit_records_readable':True}
                await check('final_diff_and_audit_consistency',final)
                await write('RESULT.json',json.dumps({'passed':report['passed'],'failed':report['failed'],'local_report':str(output)},indent=2)+'\n')
    except Exception as exc:
        report['checks'].append({'name':'test_harness','status':'FAIL','error':str(exc)[:2000]});save();raise
    finally:save()
    print(json.dumps({'passed':report['passed'],'failed':report['failed'],'workspace':report.get('workspace'),'report':str(output)}),flush=True)
    return bool(report['failed'])


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--host',required=True);parser.add_argument('--parent',required=True);parser.add_argument('--report',required=True)
    parser.add_argument('--only',help='Run one named check for a targeted retest')
    args=parser.parse_args()
    os.environ.setdefault('REMOTE_WORKBENCH_AUDIT_DIR',str(PROJECT/'.remote-workbench-audit'/'example-host-live'))
    raise SystemExit(asyncio.run(run(args)))
