"""Standalone stdlib-only worker, sent to remote Python through SSH.

Locks coordinate enhanced MCP writers, not arbitrary editors. Hash/stat checks
detect stale reads; no claim of a transaction against uncooperative processes.
"""
import difflib
import fcntl
import hashlib
import json
import os
from pathlib import Path
import stat
import subprocess
import sys
import tempfile
from contextlib import contextmanager

if "login_spec" not in globals():
    from remote_workbench.remote_shell import login_spec


def state_root():
    default = Path.home() / ".cache" / "remote-workbench-ssh"
    return Path(os.environ.get("REMOTE_WORKBENCH_REMOTE_STATE_DIR", str(default))).expanduser().absolute()


@contextmanager
def lock(path):
    # One writer lock per remote user also coordinates Git patches with file
    # replacements. Keep bookkeeping outside repositories.
    base = state_root() / "locks"
    base.mkdir(parents=True, exist_ok=True, mode=0o700)
    lockpath = base / "writer.lock"
    fd = os.open(lockpath, os.O_CREAT | os.O_RDWR | getattr(os, "O_NOFOLLOW", 0), 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        os.close(fd)


def sha(data):
    return hashlib.sha256(data).hexdigest()


def atomic_write(path, data, previous):
    info = path.stat() if path.exists() else None
    if path.is_symlink():
        raise ValueError("Refusing to replace a symbolic link")
    fd, name = tempfile.mkstemp(prefix=".remote-workbench-write-", dir=str(path.parent))
    try:
        with os.fdopen(fd, "wb") as output:
            output.write(data)
            if info:
                # Preserve ownership when allowed; fail rather than silently change it.
                if (os.fstat(output.fileno()).st_uid, os.fstat(output.fileno()).st_gid) != (info.st_uid, info.st_gid):
                    os.fchown(output.fileno(), info.st_uid, info.st_gid)
                os.fchmod(output.fileno(), stat.S_IMODE(info.st_mode))
            output.flush()
            os.fsync(output.fileno())
        now = path.read_bytes() if path.exists() else None
        if now != previous:
            raise ValueError("Conflict: file changed before replacement")
        os.replace(name, path)
    finally:
        if os.path.exists(name):
            os.unlink(name)


def edit(request):
    path = Path(request["path"]).expanduser().absolute()
    if request.get("create_dirs"):
        path.parent.mkdir(parents=True, exist_ok=True)
    if path.is_symlink():
        raise ValueError("Refusing to edit a symbolic link")
    with lock(path):
        original = path.read_bytes() if path.exists() else None
        if original is not None and len(original) > request.get("max_file_size", 10485760):
            raise ValueError("File exceeds configured size limit")
        old = original or b""
        expected = request.get("expected_sha256")
        if expected is not None and sha(old) != expected:
            return {"error": "conflict", "actual_sha256": sha(old)}
        before = old.decode("utf-8")
        text = before
        op = request["op"]
        if op == "replace":
            if original is None:
                raise FileNotFoundError(str(path))
            # Sequential edits are explicit; validate all before writing anything.
            for item in request["edits"]:
                needle = item["old"]
                if not needle:
                    raise ValueError("old must not be empty")
                count = text.count(needle)
                if count != 1:
                    return {"error": "not_found" if count == 0 else "ambiguous", "matches": count, "path": str(path)}
                text = text.replace(needle, item["new"], 1)
        elif op == "lines":
            if original is None:
                raise FileNotFoundError(str(path))
            lines = text.splitlines(keepends=True)
            start, end = request["start"], request["end"]
            if start < 0 or end < start or end > len(lines):
                raise ValueError("Line range outside file")
            if "expected_old" in request and "".join(lines[start:end]) != request["expected_old"]:
                return {"error": "conflict", "message": "Line contents changed"}
            text = "".join(lines[:start]) + request["content"] + "".join(lines[end:])
        elif op == "append":
            text += request["content"]
        elif op == "write":
            text = request["content"]
        else:
            raise ValueError("Unknown edit operation")
        after = text.encode("utf-8")
        if len(after) > request.get("max_file_size", 10485760):
            raise ValueError("Result exceeds configured size limit")
        diff_lines = difflib.unified_diff(before.splitlines(keepends=True), text.splitlines(keepends=True), fromfile=str(path), tofile=str(path), n=3)
        patch = "".join(line if line.endswith("\n") else line + "\n\\ No newline at end of file\n" for line in diff_lines)
        if not request.get("check_only") and (original is None or old != after):
            atomic_write(path, after, original)
        return {"success": True, "path": str(path), "check_only": bool(request.get("check_only")),
                "changed": old != after or original is None, "before_sha256": sha(old), "after_sha256": sha(after), "diff": patch}


def apply_patch(request):
    patch = request["patch"].encode("utf-8")
    top = subprocess.run(["git", "rev-parse", "--show-toplevel"], capture_output=True)
    if top.returncode:
        return {"error": "not_repository", "message": top.stderr.decode("utf-8", "replace")}
    repo = Path(os.fsdecode(top.stdout).strip())
    os.chdir(repo)
    with lock(repo / ".remote-workbench-patch"):
        # Refuse paths outside the worktree using git's default path protections.
        check = subprocess.run(["git", "apply", "--check", "--whitespace=nowarn", "-"], input=patch, capture_output=True)
        if check.returncode:
            return {"error": "patch_conflict", "message": check.stderr.decode("utf-8", "replace")}
        summary = subprocess.run(["git", "apply", "--numstat", "-"], input=patch, capture_output=True, check=True)
        if not request.get("check_only"):
            result = subprocess.run(["git", "apply", "--whitespace=nowarn", "-"], input=patch, capture_output=True)
            if result.returncode:
                return {"error": "apply_failed", "message": result.stderr.decode("utf-8", "replace")}
        return {"success": True, "check_only": bool(request.get("check_only")), "summary": summary.stdout.decode("utf-8", "replace"), "patch_sha256": sha(patch)}


def read_lines(request):
    path = Path(request["path"]).expanduser()
    maximum = request.get("max_file_size", 10485760)
    if path.stat().st_size > maximum:
        raise ValueError("File exceeds configured size limit")
    # Hash and selected lines refer to exactly the same bounded snapshot.
    with path.open("rb") as source:
        snapshot = source.read(maximum + 1)
    if len(snapshot) > maximum:
        raise ValueError("File exceeds configured size limit")
    lines = snapshot.decode("utf-8").splitlines(keepends=True)
    selected = []
    total = len(lines)
    used = 0
    truncated = False
    next_offset = request["offset"]
    for index in range(request["offset"], min(total, request["offset"] + request["limit"])):
        value = f"{index+1}\t{lines[index]}"
        size = len(value.encode("utf-8"))
        if used + size > request["budget"]:
            if not selected:
                return {"error": "line_exceeds_budget", "line": index+1, "required_bytes": size, "sha256": sha(snapshot)}
            truncated = True
            break
        selected.append(value)
        used += size
        next_offset = index + 1
    return {"content": "".join(selected).rstrip("\n"), "total_lines": total,
            "sha256": sha(snapshot), "next_offset": next_offset,
            "truncated": truncated or total > next_offset}



# Detached Python supervisor: startup output stays separate from command logs.
JOB_SUPERVISOR = r"""
import os, sys, subprocess, threading, shutil
from pathlib import Path
root = Path(settings['job_dir'])
def forward(reader, name):
    marker = settings['markers'][name]
    output = getattr(sys, name).buffer
    pending = b''
    started = False
    startup_path = root / ('startup_' + name + '.log')
    with open(startup_path, 'wb', opener=lambda p,f: os.open(p,f,0o600)) as startup:
        for chunk in iter(lambda: reader.read1(65536), b''):
            if started:
                output.write(chunk)
                output.flush()
                continue
            pending += chunk
            offset = pending.find(marker)
            if offset >= 0:
                startup.write(pending[:offset])
                output.write(pending[offset+len(marker):])
                output.flush()
                pending = b''
                started = True
            elif len(pending) > 65536:
                startup.write(pending[:-1024])
                pending = pending[-1024:]
        if not started:
            startup.flush()
            with startup_path.open('rb') as prefix:
                shutil.copyfileobj(prefix, output)
            output.write(pending)
            output.flush()
try:
    process = subprocess.Popen(settings['argv'], executable=settings['shell'],
        env=dict(os.environ, **settings['env_updates']),
        stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    threads = [threading.Thread(target=forward, args=(getattr(process, name), name)) for name in ('stdout','stderr')]
    for thread in threads: thread.start()
    code = process.wait()
    for thread in threads: thread.join()
except OSError as exc:
    print(str(exc), file=sys.stderr)
    code = 127
code = code if code >= 0 else 128 - code
with open(root/'exit_code.tmp', 'w', opener=lambda p,f: os.open(p,f,0o600)) as output:
    output.write(str(code))
os.replace(str(root/'exit_code.tmp'), str(root/'exit_code'))
sys.exit(code)
"""


def machine_identity():
    value = {"node": os.uname().nodename, "boot_id": None}
    try:
        value["boot_id"] = Path("/proc/sys/kernel/random/boot_id").read_text().strip() or None
    except OSError:
        pass
    return value


def process_start(pid):
    """A birth marker, never just PID existence. Missing proof means unknown."""
    if not isinstance(pid, int) or isinstance(pid, bool) or pid <= 0:
        return None
    if Path("/proc").is_dir():
        try:
            data = Path(f"/proc/{pid}/stat").read_text()
            fields = data[data.rindex(")") + 2:].split()
            return "proc:" + fields[19] if fields[0] != "Z" else None
        except (OSError, ValueError, IndexError):
            return None
    # BSD/macOS fallback. Linux cluster nodes use the stronger /proc marker.
    try:
        result = subprocess.run(["ps", "-p", str(pid), "-o", "lstart="],
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=2)
        return "ps:" + result.stdout.decode().strip() if result.returncode == 0 and result.stdout.strip() else None
    except (OSError, subprocess.TimeoutExpired):
        return None


def job_liveness(origin):
    current = machine_identity()
    detail = {"origin_node": origin.get("node"), "observed_node": current["node"]}
    if not isinstance(origin.get("node"), str) or not isinstance(origin.get("start_token"), str) or not origin["start_token"]:
        return None, dict(detail, state_reason="identity_unavailable")
    if origin["node"] != current["node"]:
        return None, dict(detail, state_reason="different_node")
    if origin.get("boot_id") and origin["boot_id"] != current["boot_id"]:
        return None, dict(detail, state_reason="different_boot")
    if origin["start_token"].startswith("proc:") and not origin.get("boot_id"):
        return None, dict(detail, state_reason="boot_identity_unavailable")
    observed = process_start(origin.get("pid"))
    if observed != origin["start_token"]:
        return None, dict(detail, state_reason="process_unconfirmed")
    return True, {}


def job(request):
    import re
    if not re.fullmatch(r"[a-zA-Z0-9_-]{1,80}", request["job_id"]):
        raise ValueError("Invalid job ID")
    base = state_root() / "jobs"
    path = base / request["job_id"]
    if request["op"] == "job_start":
        base.mkdir(parents=True, exist_ok=True, mode=0o700)
        path.mkdir(mode=0o700)  # Exclusive: never overwrite another job.
        runner = path / "runner.py"
        # A separate supervisor captures even an explicit `exit` in the user
        # command, while the command itself uses the same native shell as exec.
        prefix = "\x1eREMOTE_WORKBENCH_JOB_" + request["job_id"]
        markers = {name: (prefix + "_" + name + "\x1f\n").encode() for name in ("stdout", "stderr")}
        spec = login_spec(request["command"], os.getcwd(),
                          markers={key: value.decode() for key, value in markers.items()})
        settings = dict(spec, job_dir=str(path), markers=markers)
        script = "settings = " + repr(settings) + "\n" + JOB_SUPERVISOR
        with open(runner, "w", opener=lambda p, f: os.open(p, f, 0o600)) as output:
            output.write(script)
        with open(path / "stdout.log", "wb", opener=lambda p, f: os.open(p, f, 0o600)) as out, open(path / "stderr.log", "wb", opener=lambda p, f: os.open(p, f, 0o600)) as err:
            process = subprocess.Popen([sys.executable, str(runner)], stdin=subprocess.DEVNULL,
                stdout=out, stderr=err, start_new_session=True)
        origin = dict(machine_identity(), version=1, pid=process.pid, start_token=process_start(process.pid))
        atomic_write(path / "origin.json", json.dumps(origin).encode(), None)
        atomic_write(path / "pid", str(process.pid).encode(), None)
        return {"job_id": request["job_id"], "pid": process.pid, "job_dir": str(path), "node": origin["node"]}
    if not path.is_dir():
        return {"error": "job_not_found"}
    exit_path = path / "exit_code"
    if exit_path.exists():
        running, exit_code = False, int(exit_path.read_text())
        detail = {}
    else:
        try:
            origin = json.loads((path / "origin.json").read_text())
            if not isinstance(origin, dict):
                origin = {}
        except (OSError, ValueError):
            origin = {}
        running, detail = job_liveness(origin)
        exit_code = None
    result = {"job_id": request["job_id"], "running": running, "exit_code": exit_code,
              "execution_state": "running" if running else ("finished" if exit_code is not None else "unknown"), **detail}
    for stream in ("stdout", "stderr"):
        cursor = request[stream + "_cursor"]
        logfile = path / (stream + ".log")
        with logfile.open("rb") as source:
            if cursor > logfile.stat().st_size:
                raise ValueError("Log cursor exceeds available bytes")
            source.seek(cursor)
            data = source.read(request["budget"])
            # Don't discard lines or consume an incomplete UTF-8 character.
            # The next page rereads boundary bytes, keeping the strict budget.
            lines = data.splitlines(keepends=True)
            if len(lines) > request["tail_lines"]:
                data = b"".join(lines[:request["tail_lines"]])
            try:
                data.decode("utf-8")
            except UnicodeDecodeError as exc:
                if exc.reason == "unexpected end of data":
                    data = data[:exc.start]
        result[stream + "_tail"] = data.decode("utf-8", "replace")
        result["next_" + stream + "_cursor"] = cursor + len(data)
        result[stream + "_has_more"] = cursor + len(data) < logfile.stat().st_size
    return result


def main():
    request = json.load(sys.stdin)
    try:
        if request["op"] in {"job_start", "job_status"}:
            result = job(request)
        elif request["op"] == "apply_patch":
            result = apply_patch(request)
        elif request["op"] == "read_lines":
            result = read_lines(request)
        else:
            result = edit(request)
    except (OSError, ValueError, UnicodeError, subprocess.SubprocessError) as exc:
        result = {"error": "remote_operation_failed", "message": str(exc)}
    marker = "\x1eREMOTE_WORKBENCH_RESULT_" + request.get("_response_id", "") + ":\x1f"
    print(marker + json.dumps(result, ensure_ascii=False))


if __name__ == "__main__":
    main()
