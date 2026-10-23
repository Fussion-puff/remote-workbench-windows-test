"""Opt-in real SSH/MCP test. All remote writes stay in a fresh test directory.

Run: .venv/bin/python tests/live_remote_test.py --host example-host --report REPORT.json
Uses existing ControlMaster; does not authenticate, install remote packages, or
change the agent client's MCP configuration. Retains test artifacts for review.
"""
from __future__ import annotations
import argparse
import asyncio
from contextlib import asynccontextmanager
from dataclasses import asdict
from datetime import datetime
import difflib
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import shlex
import sys
import tempfile
import time
from zoneinfo import ZoneInfo

import yaml
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
from remote_workbench.config import get_config

PROJECT = Path(__file__).resolve().parents[1]


def py(code):
    return 'python3 -c '+shlex.quote(code)


@asynccontextmanager
async def connect(config_path=None):
    args=['-m','remote_workbench']
    if config_path:args+=['--config',str(config_path)]
    params=StdioServerParameters(command=sys.executable,args=args,cwd=str(PROJECT),env=dict(os.environ, FASTMCP_LOG_LEVEL="ERROR"))
    async with stdio_client(params) as streams:
        async with ClientSession(*streams) as session:
            await session.initialize()
            yield session


async def invoke(session,name,**arguments):
    response=await session.call_tool(name,arguments)
    if response.isError:
        raise RuntimeError(str(response.content)[:1600])
    if response.structuredContent is not None:
        return response.structuredContent
    return json.loads(next(c.text for c in response.content if c.type=='text'))


async def run(args):
    host=args.host
    report={'host':host,'time':datetime.now(ZoneInfo('Asia/Shanghai')).isoformat(),'checks':[],
            'audit_directory':os.environ['REMOTE_WORKBENCH_AUDIT_DIR']}
    report_path=Path(args.report).resolve()
    report_path.parent.mkdir(parents=True,exist_ok=True)
    def save():
        report['passed']=sum(c['status']=='PASS' for c in report['checks'])
        report['failed']=sum(c['status']=='FAIL' for c in report['checks'])
        report_path.write_text(json.dumps(report,ensure_ascii=False,indent=2)+'\n')
    async def check(name,function):
        started=time.monotonic()
        try:
            details=await function()
            report['checks'].append({'name':name,'status':'PASS','details':details})
            print('PASS '+name,flush=True)
        except Exception as exc:
            report['checks'].append({'name':name,'status':'FAIL','error':str(exc)[:2000]})
            print('FAIL '+name+': '+str(exc)[:600],flush=True)
        report['checks'][-1]['elapsed_seconds']=round(time.monotonic()-started,3)
        save()
    try:
        async with connect() as session:
            connection=await invoke(session,'ssh_check_connection',host=host)
            if not connection.get('connected'):raise RuntimeError(connection)
            parent=repr(args.parent) if args.parent else 'os.path.expanduser("~")'
            code='import tempfile,os,sys,platform,subprocess,json; root=tempfile.mkdtemp(prefix="remote-workbench-test-",dir='+parent+'); print(json.dumps({"workspace":root,"python":sys.version.split()[0],"platform":platform.system(),"git":subprocess.check_output(["git","--version"]).decode().strip()}))'
            created=await invoke(session,'ssh_run',host=host,command=py(code),cwd='~',timeout=30,max_bytes=4096)
            assert created['exit_code']==0,created
            environment=json.loads(created['stdout'])
            report.update(environment)
            root=environment['workspace']
            print('WORKSPACE '+root,flush=True)
            save()
        host_config=asdict(get_config().get_host(host))
        host_config['default_cwd']=root
        state=root+'/.remote-workbench-state'
        host_config['preamble']=(host_config['preamble']+'\nexport REMOTE_WORKBENCH_REMOTE_STATE_DIR='+shlex.quote(state)).strip()
        with tempfile.TemporaryDirectory(prefix='remote-workbench-live-config-') as temporary:
            config_path=Path(temporary)/'config.yaml'
            config_path.write_text(yaml.safe_dump({'default_host':host,'hosts':{host:host_config}}))
            async with connect(config_path) as session:
                tools=await session.list_tools()
                report['tool_count']=len(tools.tools)
                async def call(name,**kw):return await invoke(session,name,host=host,**kw)
                async def command(text,**kw):return await call('ssh_run',command=text,**kw)
                async def read(path):return await call('ssh_read_file',path=path)
                async def write(path,content):
                    value=await call('ssh_write_file',path=path,content=content)
                    assert value.get('success'),value
                    return value
                async def context_check():
                    result=await command('pwd',trace=True)
                    assert result['exit_code']==0 and result['stdout'].strip()==root,result
                    record=await invoke(session,'ssh_audit_get',operation_id=result['output_id'])
                    assert record['actual_cwd']==root and record['shell_trace']
                    return {'operation_id':result['operation_id'],'output_id':result['output_id'],'cwd':root}
                await check('bound_workspace_and_shell_trace',context_check)
                if args.expect_zsh:
                    async def native_zsh():
                        result=await command('[[ -n "$ZSH_VERSION" && -o interactive && -o login ]] || exit 91\nprint -r -- "$ZSH_VERSION"')
                        assert result['exit_code']==0,result
                        startup=await invoke(session,'ssh_read_output',output_id=result['output_id'],stream='startup_stdout')
                        report['zsh_version']=result['stdout'].strip()
                        exported=await command('export REMOTE_WORKBENCH_TEST_EPHEMERAL=present')
                        assert exported['exit_code']==0,exported
                        fresh=await command('print -r -- "${REMOTE_WORKBENCH_TEST_EPHEMERAL-unset}"')
                        assert fresh['stdout'].strip()=='unset',fresh
                        return {'zsh_version':report['zsh_version'],'interactive':True,'login':True,
                                'fresh_shell_per_call':True,'startup_stdout_bytes':startup['total_bytes']}
                    await check('native_zsh_config_and_fresh_shell',native_zsh)
                async def setup():
                    result=await command('mkdir repo && git -C repo init -q')
                    assert result['exit_code']==0,result
                    source='value = 1\nname = "示例"\n'+''.join('# filler %d\n'%i for i in range(60))+'tail = 1\n'
                    await write('repo/alpha.py',source)
                    await write('repo/note.txt','before\n')
                    await write('repo/.gitignore','__pycache__/\n')
                    result=await command('git add alpha.py note.txt .gitignore && git -c core.hooksPath=/dev/null -c commit.gpgsign=false -c user.name=Remote-Workbench-Test -c user.email=remote-workbench-test@localhost commit -qm baseline',cwd=root+'/repo')
                    assert result['exit_code']==0,result
                    await write('TEST_INFO.json',json.dumps({'purpose':'isolated remote-workbench enhanced live test','host':host,'created':report['time']},ensure_ascii=False)+'\n')
                    return {'repository':root+'/repo','relative_paths':True}
                await check('relative_file_operations_and_git_baseline',setup)
                memo={}
                async def batch():
                    before=await read('repo/alpha.py');memo['original_hash']=before['sha256']
                    result=await call('ssh_patch_file',path='repo/alpha.py',edits=[{'old':'value = 1','new':'value = 2'},{'old':'tail = 1','new':'tail = 2'}],expected_sha256=before['sha256'])
                    assert result.get('success'),result
                    after=await read('repo/alpha.py')
                    assert 'value = 2' in after['content'] and 'tail = 2' in after['content']
                    info=await command(py('import os,json;print(json.dumps({"mode":oct(os.stat("repo/alpha.py").st_mode & 0o777),"lock_exists":os.path.exists(".remote-workbench-state/locks/writer.lock")}))'))
                    assert json.loads(info['stdout'])['lock_exists']
                    memo['patch_result']=result;memo['changed_hash']=after['sha256']
                    return {'operation_id':result['operation_id'],'output_id':result['output_id'],'file_mode':json.loads(info['stdout'])['mode']}
                await check('remote_batch_replace_hash_and_isolated_lock',batch)
                async def rejected():
                    stale=await call('ssh_patch_file',path='repo/alpha.py',old='value = 2',new='value = 99',expected_sha256=memo['original_hash'])
                    assert stale.get('error')=='conflict',stale
                    failed=await call('ssh_patch_file',path='repo/alpha.py',edits=[{'old':'value = 2','new':'value = 99'},{'old':'ABSENT_TEXT','new':'bad'}])
                    assert failed.get('error')=='not_found',failed
                    after=await read('repo/alpha.py');assert after['sha256']==memo['changed_hash']
                    return {'stale_hash':'rejected','invalid_batch':'no partial write'}
                await check('stale_hash_and_failed_batch_preserve_content',rejected)
                async def unified():
                    patch1='--- a/note.txt\n+++ b/note.txt\n@@ -1 +1 @@\n-before\n+after\n'
                    dry=await call('ssh_apply_patch',cwd=root+'/repo',patch=patch1,check_only=True)
                    assert dry.get('success'),dry
                    assert (await read('repo/note.txt'))['content']=='1\tbefore'
                    invalid=patch1+'--- a/alpha.py\n+++ b/alpha.py\n@@ -1 +1 @@\n-ABSENT\n+bad\n'
                    failed=await call('ssh_apply_patch',cwd=root+'/repo',patch=invalid)
                    assert failed.get('error')=='patch_conflict',failed
                    assert (await read('repo/note.txt'))['content']=='1\tbefore'
                    actual=patch1+'--- a/alpha.py\n+++ b/alpha.py\n@@ -1,2 +1,2 @@\n-value = 2\n+value = 3\n name = "示例"\n'
                    applied=await call('ssh_apply_patch',cwd=root+'/repo',patch=actual)
                    assert applied.get('success'),applied
                    assert (await read('repo/note.txt'))['content']=='1\tafter'
                    assert 'value = 3' in (await read('repo/alpha.py'))['content']
                    assert (await command('git diff --cached --exit-code',cwd=root+'/repo'))['exit_code']==0
                    return {'operation_id':applied['operation_id'],'output_id':applied['output_id'],'index_unchanged':True}
                await check('git_patch_dry_run_conflict_and_multi_file_apply',unified)
                async def diff():
                    summary=await call('ssh_git_diff',cwd=root+'/repo')
                    assert 'alpha.py' in summary['summary'] and 'note.txt' in summary['summary'],summary
                    page=await call('ssh_git_diff',cwd=root+'/repo',mode='patch',max_bytes=256)
                    complete='';pages=0
                    while True:
                        assert 'diff' in page,page
                        complete+=page['diff'];pages+=1
                        if not page['truncated']:break
                        assert pages<30
                        page=await call('ssh_git_diff',mode='patch',output_id=page['output_id'],cursor=page['next_cursor'],max_bytes=256)
                    baseline=await command('git --no-pager diff --no-ext-diff --no-textconv --no-color',cwd=root+'/repo')
                    assert baseline['exit_code']==0 and complete==baseline['stdout']
                    assert pages>1
                    return {'pages':pages,'output_id':page['output_id'],'complete_bytes':len(complete.encode())}
                await check('diff_summary_and_complete_hunk_pagination',diff)
                async def output():
                    result=await command(py('import sys;sys.stdout.write("中"*5000);sys.stderr.write("error"*5000)'),max_bytes=1024)
                    assert result['truncated'] and len(result['stdout'].encode())+len(result['stderr'].encode())<=1024,result
                    hashes={}
                    for stream,expected in [('stdout','中'*5000),('stderr','error'*5000)]:
                        cursor=0;full=''
                        while True:
                            page=await invoke(session,'ssh_read_output',output_id=result['output_id'],stream=stream,cursor=cursor,max_bytes=4096)
                            full+=page['content'];cursor=page['next_cursor']
                            if not page['truncated']:break
                        assert full==expected
                        hashes[stream]=hashlib.sha256(full.encode()).hexdigest()
                    return {'output_id':result['output_id'],'preview_budget':1024,'stdout_bytes':15000,'stderr_bytes':25000,'hashes':hashes}
                await check('bounded_preview_full_spool_and_utf8_pages',output)
                async def background():
                    launch=await call('ssh_run_background',command="printf 'first\\n'; printf error >&2; sleep 0.3; printf 'second\\n'; exit 7")
                    assert 'job_dir' in launch,launch
                    assert PurePosixPath(launch['job_dir']).is_relative_to(PurePosixPath(root)),launch
                    out=err='';oc=ec=0
                    for _ in range(20):
                        status=await call('ssh_job_status',job_id=launch['job_id'],stdout_cursor=oc,stderr_cursor=ec)
                        out+=status['stdout_tail'];err+=status['stderr_tail'];oc=status['next_stdout_cursor'];ec=status['next_stderr_cursor']
                        if not status['running']:break
                        await asyncio.sleep(0.2)
                    assert status['exit_code']==7 and out=='first\nsecond\n' and err=='error',status
                    repeated=await call('ssh_job_status',job_id=launch['job_id'],stdout_cursor=oc,stderr_cursor=ec)
                    assert repeated['stdout_tail']==repeated['stderr_tail']==''
                    memo['job_id']=launch['job_id']
                    return {'job_id':launch['job_id'],'job_dir':launch['job_dir'],'exit_code':7,'repeated_output_bytes':0}
                await check('background_exit_code_incremental_logs_in_test_folder',background)
                async def guard():
                    result=await command('printf bad > MUST_NOT_EXIST',cwd=root+'/absent-directory')
                    assert result['exit_code']==125,result
                    assert (await command('test ! -e MUST_NOT_EXIST'))['exit_code']==0
                    return {'exit_code':125,'unexpected_write':False}
                await check('failed_cwd_prevents_command_execution',guard)
                async def timeout():
                    result=await command(py('import time;print("before-timeout",flush=True);time.sleep(4)'),timeout=2)
                    assert result['timed_out'] and result['execution_state']=='remote_state_unknown',result
                    return {'output_id':result['output_id'],'execution_state':result['execution_state'],'partial_output':result['stdout']}
                await check('timeout_keeps_partial_output_and_reports_unknown',timeout)
                async def audit_check():
                    value=memo['patch_result']
                    parent=await invoke(session,'ssh_audit_get',operation_id=value['operation_id'])
                    child=await invoke(session,'ssh_audit_get',operation_id=value['output_id'])
                    assert value['output_id'] in parent['child_commands']
                    assert child['parent_id']==value['operation_id'] and child['actual_cwd']==root
                    assert 'python3 -c' in child['wrapped_command'] and child['exit_code']==0
                    payload=await invoke(session,'ssh_read_output',output_id=value['output_id'],stream='stdin',max_bytes=4096)
                    assert json.loads(payload['content'])['op']=='replace'
                    return {'parent_id':value['operation_id'],'command_id':value['output_id'],'actual_cwd':child['actual_cwd']}
                await check('audit_command_stdin_and_parent_child_provenance',audit_check)
                async def verify():
                    result=await command('python3 -m py_compile alpha.py && git diff --check && git status --porcelain=v1',cwd=root+'/repo')
                    assert result['exit_code']==0,result
                    assert 'M alpha.py' in result['stdout'] and 'M note.txt' in result['stdout']
                    await write('RESULT.json',json.dumps({'passed':report['passed'],'failed':report['failed'],'local_report':str(report_path)},ensure_ascii=False,indent=2)+'\n')
                    return {'syntax_check':True,'git_diff_check':True,'git_status':result['stdout'].strip()}
                await check('final_syntax_diff_and_scoped_artifacts',verify)
                await write('RESULT.json',json.dumps({'passed':report['passed'],'failed':report['failed'],'local_report':str(report_path)},ensure_ascii=False,indent=2)+'\n')
    except Exception as exc:
        report['checks'].append({'name':'test_harness','status':'FAIL','error':str(exc)[:3000]})
        save()
        raise
    finally:
        save()
    print(json.dumps({'passed':report['passed'],'failed':report['failed'],'workspace':report.get('workspace'),'report':str(report_path)},ensure_ascii=False),flush=True)
    return report['failed']


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--host',required=True)
    parser.add_argument('--report',required=True)
    parser.add_argument('--parent',help='Create the new test directory inside this existing remote directory')
    parser.add_argument('--expect-zsh',action='store_true',help='Also verify interactive login zsh and no environment carry-over')
    options=parser.parse_args()
    os.environ.setdefault('REMOTE_WORKBENCH_AUDIT_DIR',str(PROJECT/'.remote-workbench-audit'/'live-remote'))
    raise SystemExit(bool(asyncio.run(run(options))))
