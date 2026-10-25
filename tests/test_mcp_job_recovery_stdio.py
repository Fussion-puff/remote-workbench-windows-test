"""End-to-end recovery through real MCP stdio processes and a local SSH shim.

The shim runs the repository's real worker and supervisor locally. It drops one
successful job_start response after observing the persisted remote job files.
No SSH credentials, remote host, product fault flag, or spawn monkeypatch is used.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
import pytest
import yaml


THREAD_ID = "stdio-recovery-thread"
IDEMPOTENCY_KEY = "stdio-recovery-once"


def _write_ssh_shim(bin_dir: Path, *, remote_home: Path, safe_shell: Path,
                    launch_counter: Path, event_log: Path, drop_gate: Path,
                    dropped_log: Path) -> None:
    source = f"""#!{sys.executable}
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import threading

REMOTE_HOME = {str(remote_home)!r}
SAFE_SHELL = {str(safe_shell)!r}
LAUNCH_COUNTER = {str(launch_counter)!r}
EVENT_LOG = {str(event_log)!r}
DROP_GATE = {str(drop_gate)!r}
DROPPED_LOG = {str(dropped_log)!r}

def append_json(path, value):
    with open(path, "a", encoding="utf-8") as output:
        output.write(json.dumps(value, separators=(",", ":")) + "\\n")

args = sys.argv[1:]
if args[:2] == ["-O", "check"]:
    raise SystemExit(0)
if len(args) < 2:
    raise SystemExit(2)
target, command = args[-2], args[-1]
env = dict(os.environ)
env.update(HOME=REMOTE_HOME, SHELL=SAFE_SHELL,
           REMOTE_WORKBENCH_LOGIN_SHELL=SAFE_SHELL,
           RWB_TEST_LAUNCH_COUNTER=LAUNCH_COUNTER)
python_dir = {str(Path(sys.executable).parent)!r}
env["PATH"] = {str(bin_dir)!r} + os.pathsep + python_dir + os.pathsep + env.get("PATH", "")
process = subprocess.Popen(["/bin/bash", "--noprofile", "--norc", "-c", command],
                           stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                           stderr=subprocess.PIPE, env=env, bufsize=0)
request = {{"op": None}}
request_bytes = bytearray()
captured_stdout = bytearray()

def relay_input():
    reader = getattr(sys.stdin.buffer, "read1", sys.stdin.buffer.read)
    while True:
        chunk = reader(4096)
        if not chunk:
            break
        if request["op"] is None:
            request_bytes.extend(chunk)
            try:
                decoded = request_bytes.decode("utf-8")
                value, _ = json.JSONDecoder().raw_decode(decoded.lstrip())
                if isinstance(value, dict):
                    request["op"] = value.get("op")
                    request["job_id"] = value.get("job_id")
            except (UnicodeDecodeError, ValueError):
                pass
        try:
            process.stdin.write(chunk)
            process.stdin.flush()
        except BrokenPipeError:
            break
    try:
        process.stdin.close()
    except BrokenPipeError:
        pass

def relay_stderr():
    reader = getattr(process.stderr, "read1", process.stderr.read)
    while chunk := reader(65536):
        sys.stderr.buffer.write(chunk)
        sys.stderr.buffer.flush()

def capture_stdout():
    reader = getattr(process.stdout, "read1", process.stdout.read)
    while chunk := reader(65536):
        captured_stdout.extend(chunk)

threads = [threading.Thread(target=relay_input, daemon=True),
           threading.Thread(target=relay_stderr, daemon=True),
           threading.Thread(target=capture_stdout, daemon=True)]
for thread in threads:
    thread.start()
return_code = process.wait()
for thread in threads:
    thread.join(timeout=3)
stdout = bytes(captured_stdout)
drop = False
persisted = False
job_dir = None
if request.get("op") == "job_start" and Path(DROP_GATE).exists() and return_code == 0:
    marker = re.search(rb"\\x1eREMOTE_WORKBENCH_RESULT_[0-9a-f]+:\\x1f", stdout)
    if marker:
        try:
            result, _ = json.JSONDecoder().raw_decode(stdout[marker.end():].decode("utf-8"))
            job_dir = result.get("job_dir")
            root = Path(job_dir) if isinstance(job_dir, str) else None
            persisted = bool(root and (root / "status.json").is_file()
                             and (root / "origin.json").is_file())
        except (ValueError, TypeError, OSError):
            persisted = False
    if persisted:
        try:
            os.unlink(DROP_GATE)
            drop = True
        except FileNotFoundError:
            pass
if drop:
    append_json(DROPPED_LOG, {{"op": request.get("op"), "job_id": request.get("job_id"),
                               "job_dir": job_dir, "persisted": persisted,
                               "response_was_present": b"REMOTE_WORKBENCH_RESULT_" in stdout}})
else:
    sys.stdout.buffer.write(stdout)
    sys.stdout.buffer.flush()
append_json(EVENT_LOG, {{"target": target, "op": request.get("op"),
                        "job_id": request.get("job_id"), "dropped": drop,
                        "server_pid": os.getppid(), "exit_code": return_code}})
raise SystemExit(return_code)
"""
    shim = bin_dir / "ssh"
    shim.write_text(source, encoding="utf-8")
    shim.chmod(0o700)
    (bin_dir / "python3").symlink_to(sys.executable)


def _value(result):
    if result.structuredContent is not None:
        return result.structuredContent
    assert result.content, f"MCP returned no content: {result!r}"
    try:
        return json.loads(result.content[0].text)
    except (ValueError, AttributeError) as exc:
        raise AssertionError(f"MCP returned non-JSON content: {result!r}") from exc


async def _call(client: ClientSession, tool: str, arguments: dict | None = None):
    result = await client.call_tool(tool, arguments or {}, meta={"threadId": THREAD_ID})
    return _value(result), result


def _events(path: Path) -> list[dict]:
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text().splitlines() if line]


def _write_acceptance_evidence(path: Path, value: dict) -> None:
    path.mkdir(parents=True, exist_ok=True)
    destination = path / "c2-stdio-evidence.json"
    temporary = destination.with_suffix(".json.tmp")
    temporary.write_text(json.dumps(value, indent=2) + "\n", encoding="utf-8")
    temporary.replace(destination)


def _reserved_job_id(db_path: Path) -> str | None:
    if not db_path.exists():
        return None
    with sqlite3.connect(db_path) as db:
        row = db.execute(
            "SELECT job_id FROM job_operations WHERE idempotency_key = ?",
            (IDEMPOTENCY_KEY,),
        ).fetchone()
    return row[0] if row else None


def _init_fixture_repo(path: Path) -> None:
    """Keep each fixture's Git boundary independent of pytest's temp parent."""
    subprocess.run(["git", "init", "-q", str(path)], check=True)
    subprocess.run(["git", "-C", str(path), "add", "."], check=True)
    subprocess.run([
        "git", "-C", str(path), "-c", "user.name=stdio acceptance",
        "-c", "user.email=stdio-acceptance@example.invalid", "commit", "-qm",
        "fixture",
    ], check=True)


@pytest.mark.asyncio
async def test_lost_job_start_reply_recovers_after_real_stdio_restart_and_target_switch(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """Exercise C2's lost-ack recovery without a real SSH connection."""
    source_a = tmp_path / "source-a"
    source_b = tmp_path / "source-b"
    source_a.mkdir()
    source_b.mkdir()
    source_b.joinpath("test_b_only.py").write_text(
        "import os, unittest\n"
        "class WrongProject(unittest.TestCase):\n"
        " def test_must_not_run(self):\n"
        "  open(os.environ['RWB_TEST_LAUNCH_COUNTER'], 'a').write('B\\n')\n",
        encoding="utf-8",
    )
    source_a.joinpath("test_slow.py").write_text(
        "import os, time, unittest\n"
        "class OriginalProject(unittest.TestCase):\n"
        " def test_background_result(self):\n"
        "  time.sleep(1.2)\n"
        "  with open(os.environ['RWB_TEST_LAUNCH_COUNTER'], 'a') as output: output.write('A\\n')\n"
        "  print('ORIGINAL_PROJECT_RESULT')\n"
        "  print('CURSOR_PAYLOAD_START' + 'x' * 400 + 'CURSOR_PAYLOAD_END')\n",
        encoding="utf-8",
    )
    _init_fixture_repo(source_a)
    _init_fixture_repo(source_b)

    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    remote_home = tmp_path / "remote-home"
    remote_home.mkdir()
    safe_shell = tmp_path / "safe-login-shell"
    safe_shell.write_text(
        "#!/bin/sh\n"
        "[ \"$1\" = \"-i\" ] && [ \"$2\" = \"-c\" ] || exit 99\n"
        "shift 2\n"
        "exec /bin/bash --noprofile --norc -c \"$1\"\n",
        encoding="utf-8",
    )
    safe_shell.chmod(0o700)
    launch_counter = tmp_path / "launch-count.txt"
    event_log = tmp_path / "ssh-events.jsonl"
    drop_gate = tmp_path / "drop-first-job-start"
    dropped_log = tmp_path / "dropped-success.jsonl"
    _write_ssh_shim(bin_dir, remote_home=remote_home, safe_shell=safe_shell,
                    launch_counter=launch_counter, event_log=event_log,
                    drop_gate=drop_gate, dropped_log=dropped_log)
    monkeypatch.setenv("PATH", str(bin_dir) + os.pathsep + os.environ.get("PATH", ""))
    evidence_dir = Path(os.environ.get("RWB_ACCEPTANCE_EVIDENCE_DIR", str(tmp_path)))
    if os.environ.get("RWB_ACCEPTANCE_EVIDENCE_DIR"):
        (evidence_dir / "c2-stdio-evidence.json").unlink(missing_ok=True)

    config = tmp_path / "hosts.yaml"
    config.write_text(yaml.safe_dump({
        "default_host": "a",
        "hosts": {
            "a": {"ssh_host": "simulated-a", "default_cwd": str(source_a),
                  "execution_enabled": True, "job_timeout": 20,
                  "terminate_grace_seconds": 1},
            "b": {"ssh_host": "simulated-b", "default_cwd": str(source_b),
                  "execution_enabled": True, "job_timeout": 20,
                  "terminate_grace_seconds": 1},
        },
    }, sort_keys=True), encoding="utf-8")
    state_dir = tmp_path / "mcp-state"
    params = StdioServerParameters(command=sys.executable, args=[
        "-m", "remote_workbench.session_server", "--config", str(config),
        "--data-dir", str(state_dir), "--viewer-port", "0",
    ])
    drop_gate.touch()
    job_id = None
    completed = False

    try:
        # First real stdio MCP process binds A and submits the real detached worker.
        async with stdio_client(params) as (reader, writer):
            async with ClientSession(reader, writer) as client:
                await client.initialize()
                bound_a, _ = await _call(client, "workspace_switch", {
                    "host": "a", "cwd": str(source_a),
                })
                assert bound_a["bound"] is True
                target_a_id = bound_a["target_id"]
                project_a = Path(bound_a["project_root"])

                raw_start = await client.call_tool("start_job", {
                    "command": "python3 -m unittest -v",
                    "idempotency_key": IDEMPOTENCY_KEY,
                }, meta={"threadId": THREAD_ID})
                try:
                    start_result = _value(raw_start)
                except AssertionError:
                    start_result = {}
                job_id = start_result.get("job_id") or _reserved_job_id(
                    state_dir / "workspaces.sqlite3")
                assert job_id, start_result
                assert (raw_start.isError or start_result.get("error") or
                        start_result.get("execution_state") == "unknown"), (
                    "the test SSH shim must lose the successful worker reply"
                )
                dropped = _events(dropped_log)
                assert len(dropped) == 1 and dropped[0]["op"] == "job_start"
                assert dropped[0]["job_id"] == job_id
                assert dropped[0]["persisted"] is True
                assert dropped[0]["response_was_present"] is True
                assert not drop_gate.exists(), "the one-shot response-loss gate must be released"
                expected_job_dir = project_a / ".remote-workbench-state" / "jobs" / job_id
                assert Path(dropped[0]["job_dir"]) == expected_job_dir
                assert (expected_job_dir / "status.json").is_file()
                assert (expected_job_dir / "origin.json").is_file()
                initial_start_event = next(
                    event for event in _events(event_log)
                    if event.get("op") == "job_start" and event.get("job_id") == job_id
                )

        # stdio_client has now stopped the first MCP process; the detached local
        # worker persists independently in A's remote-state directory.
        assert _reserved_job_id(state_dir / "workspaces.sqlite3") == job_id

        # A second actual MCP process reuses the registry. Resume itself is local.
        async with stdio_client(params) as (reader, writer):
            async with ClientSession(reader, writer) as client:
                await client.initialize()
                before_resume_events = len(_events(event_log))
                resumed, _ = await _call(client, "workspace_resume")
                assert resumed["binding"]["workspace_id"]
                assert resumed["remote_query_performed"] is False
                assert resumed["job_collection_restarted"] is False
                assert len(_events(event_log)) == before_resume_events

                bound_b, _ = await _call(client, "workspace_switch", {
                    "host": "b", "cwd": str(source_b),
                })
                assert bound_b["target_id"] != target_a_id

                # Reusing the key from B may be rejected because its target
                # differs. Either way, it must never launch a second worker.
                retry_raw = await client.call_tool("start_job", {
                    "command": "python3 -m unittest -v", "idempotency_key": IDEMPOTENCY_KEY,
                }, meta={"threadId": THREAD_ID})
                if retry_raw.isError:
                    retry = {}
                else:
                    retry = _value(retry_raw)
                assert retry_raw.isError or retry.get("deduplicated") or retry.get("error")
                start_events = [e for e in _events(event_log) if e.get("op") == "job_start"]
                assert len(start_events) == 1

                before_resolve_events = len(_events(event_log))
                recovered, _ = await _call(client, "resolve_operation", {
                    "idempotency_key": IDEMPOTENCY_KEY,
                })
                assert recovered["job_id"] == job_id
                assert recovered["target"]["target_id"] == target_a_id
                assert recovered.get("local_job") or recovered.get("execution_state") in {
                    "running", "finished", "unknown",
                }
                resolve_events = _events(event_log)[before_resolve_events:]
                assert resolve_events, "recovery should contact the original target"
                assert all(e["target"] == "simulated-a" for e in resolve_events), (
                    "recovery must use A's saved target after switching to B; "
                    f"events={resolve_events!r}"
                )
                assert all(e["server_pid"] != initial_start_event["server_pid"]
                           for e in resolve_events), "recovery must run in a new MCP process"
                assert len({e["server_pid"] for e in resolve_events}) == 1

                # Wait/query/stop remain routed to A after the active workspace
                # has moved to B. Output pages use byte cursors without repeats.
                waited, _ = await _call(client, "job_wait", {
                    "job_id": job_id, "wait_seconds": 10,
                    "stdout_cursor": 0, "stderr_cursor": 0,
                })
                assert waited.get("error") is None, waited
                assert waited.get("execution_state") == "finished", waited
                assert waited.get("sync_state") == "complete", waited

                status, _ = await _call(client, "job_status", {
                    "job_id": job_id, "stdout_cursor": 0, "stderr_cursor": 0,
                    "max_bytes": 256,
                })
                assert status["execution_state"] == "finished"
                assert status["stdout_has_more"], status
                stdout_pages = [status["stdout_tail"]]
                stderr_pages = [status["stderr_tail"]]
                stdout_cursor = status["next_stdout_cursor"]
                first_next_stdout_cursor = stdout_cursor
                stderr_cursor = status["next_stderr_cursor"]
                for _ in range(10):
                    if not status["stdout_has_more"] and not status["stderr_has_more"]:
                        break
                    status, _ = await _call(client, "job_status", {
                        "job_id": job_id, "stdout_cursor": stdout_cursor,
                        "stderr_cursor": stderr_cursor, "max_bytes": 256,
                    })
                    stdout_pages.append(status["stdout_tail"])
                    stderr_pages.append(status["stderr_tail"])
                    stdout_cursor = status["next_stdout_cursor"]
                    stderr_cursor = status["next_stderr_cursor"]
                assert not status["stdout_has_more"] and not status["stderr_has_more"], status
                captured_stdout = "".join(stdout_pages)
                assert captured_stdout.count("ORIGINAL_PROJECT_RESULT") == 1
                assert captured_stdout.count("CURSOR_PAYLOAD_START") == 1
                assert captured_stdout.count("x" * 400) == 1
                repeated, _ = await _call(client, "job_status", {
                    "job_id": job_id, "stdout_cursor": stdout_cursor,
                    "stderr_cursor": stderr_cursor, "max_bytes": 256,
                })
                assert repeated["stdout_tail"] == repeated["stderr_tail"] == ""
                assert repeated["next_stdout_cursor"] == stdout_cursor
                assert repeated["next_stderr_cursor"] == stderr_cursor

                stopped, _ = await _call(client, "stop_job", {"job_id": job_id})
                assert stopped.get("cancel_requested") is False
                assert stopped.get("execution_state") == "finished"
                assert Path(stopped["task_dir"]) == expected_job_dir
                completed = True

                start_events = [e for e in _events(event_log) if e.get("op") == "job_start"]
                assert len(start_events) == 1
                _write_acceptance_evidence(evidence_dir, {
                    "classification": "real MCP stdio subprocesses; test-only simulated SSH; real local worker; no live SSH or App-loaded claim",
                    "fault": {
                        "dropped_success_reply_after_persist": True,
                        "job_id": job_id,
                        "job_dir": dropped[0]["job_dir"],
                    },
                    "mcp_server_pids": [
                        initial_start_event["server_pid"], resolve_events[0]["server_pid"],
                    ],
                    "target_recovery": {
                        "original_target_id": target_a_id,
                        "original_ssh_target": "simulated-a",
                        "current_target_id": bound_b["target_id"],
                        "current_ssh_target": "simulated-b",
                        "recovered_target_id": recovered["target"]["target_id"],
                    },
                    "worker_launches": {
                        "job_start_requests": len(start_events),
                        "command_counter_lines": launch_counter.read_text().splitlines(),
                    },
                    "handles": {
                        "job_id": job_id,
                        "job_output_id": status.get("job_output_id"),
                        "execution_state": status["execution_state"],
                        "sync_state": waited.get("sync_state"),
                        "stop_result_state": stopped["execution_state"],
                    },
                    "output_paging": {
                        "first_stdout_cursor": 0,
                        "first_next_stdout_cursor": first_next_stdout_cursor,
                        "final_stdout_cursor": stdout_cursor,
                        "stdout_pages": len(stdout_pages),
                        "marker_counts": {
                            "original_project": captured_stdout.count("ORIGINAL_PROJECT_RESULT"),
                            "cursor_payload": captured_stdout.count("CURSOR_PAYLOAD_START"),
                        },
                        "final_page_empty": repeated["stdout_tail"] == repeated["stderr_tail"] == "",
                    },
                    "ssh_events": [
                        {key: event.get(key) for key in ("target", "op", "server_pid", "dropped")}
                        for event in _events(event_log)
                    ],
                })

        # Exactly one command ran, and it ran from the saved A project after
        # recovery, even though the active target was B.
        assert launch_counter.read_text().splitlines() == ["A"]
        assert len([e for e in _events(dropped_log) if e["op"] == "job_start"]) == 1
    finally:
        # Keep failures safe too: ask the isolated simulated host to stop only
        # the test's known job ID, then let stdio_client terminate the MCP child.
        job_id = job_id or _reserved_job_id(state_dir / "workspaces.sqlite3")
        if job_id and not completed:
            try:
                async with stdio_client(params) as (reader, writer):
                    async with ClientSession(reader, writer) as client:
                        await client.initialize()
                        await _call(client, "stop_job", {"job_id": job_id})
            except Exception:
                # The worker has a bounded job_timeout; the normal path above
                # has already confirmed completion and stop_job.
                pass
