"""One explicit remote inspection; no hidden Git probes on ordinary tool calls."""
import json


_PROBE = r'''import json,os,subprocess,time
env=dict(os.environ,GIT_OPTIONAL_LOCKS="0")
def git(*args):
    return subprocess.run(["git","-c","core.fsmonitor=false",*args],env=env,
                          stdout=subprocess.PIPE,stderr=subprocess.PIPE)
value={"observed_at":time.time(),"node":os.uname().nodename,"cwd":os.getcwd(),"git":None}
try:
    root=git("rev-parse","--show-toplevel")
    if root.returncode==0:
        branch=git("symbolic-ref","--short","-q","HEAD")
        head=git("rev-parse","--verify","HEAD")
        status=git("status","--porcelain=v1","-z","--untracked-files=all")
        detail={"branch":branch.stdout.decode("utf-8","replace").strip() or None,
                "head":head.stdout.decode("ascii","replace").strip() if head.returncode==0 else None,
                "modified_files":None,"status_complete":False}
        if status.returncode==0 and len(status.stdout)<=1048576:
            entries=status.stdout.split(b"\0"); count=0; index=0
            while index<len(entries):
                entry=entries[index]; index+=1
                if not entry: continue
                count+=1
                if b"R" in entry[:2] or b"C" in entry[:2]: index+=1
            detail.update(modified_files=count,status_complete=True)
        value["git"]=detail
    else:
        value["git_unavailable"]="not_a_repository_or_inaccessible"
except FileNotFoundError:
    value["git_unavailable"]="git_not_installed"
print(json.dumps(value,separators=(",",":")))
'''


async def collect(snapshot):
    executor = snapshot.workspace.executor
    connected, message = await executor.check_connection()
    if not connected:
        raise ValueError(message)
    result = await executor.run_program(["python3", "-c", _PROBE], cwd=snapshot.cwd, output_budget=4096)
    if result.exit_code != 0:
        raise ValueError("Remote snapshot failed: " + result.stderr)
    try:
        value = json.loads(result.stdout)
    except (ValueError, TypeError) as exc:
        raise ValueError("Invalid remote snapshot result") from exc
    value.update(host=snapshot.host, target_id=snapshot.target_id, revision=snapshot.revision)
    value["output_id"] = result.output_id
    return value
