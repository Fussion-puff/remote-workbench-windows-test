"""End-to-end recovery through real MCP stdio processes and a local SSH shim.

The shim runs the repository's real worker and supervisor locally. It drops one
successful job_start response after observing the persisted remote job files.
No SSH credentials, remote host, product fault flag, or spawn monkeypatch is used.
"""
from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import time

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
import pytest
import yaml


THREAD_ID = "stdio-recovery-thread"
IDEMPOTENCY_KEY = "stdio-recovery-once"


def _write_ssh_shim(bin_dir: Path, *, remote_home: Path, safe_shell: Path,
                    launch_counter: Path, event_log: Path, drop_gate: Path,
                    dropped_log: Path, release_gate: Path) -> None:
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
RELEASE_GATE = {str(release_gate)!r}

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
           RWB_TEST_LAUNCH_COUNTER=LAUNCH_COUNTER,
           RWB_TEST_RELEASE_GATE=RELEASE_GATE)
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
        if request.get("op") == "job_start":
            # Only the one response under test is delayed, so we can inspect
            # persisted status/origin before deciding whether to drop it.
            captured_stdout.extend(chunk)
        else:
            # In particular, remote_log_follow is a live stream. Relay every
            # other SSH command as it arrives instead of waiting for EOF.
            sys.stdout.buffer.write(chunk)
            sys.stdout.buffer.flush()

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
    if request.get("op") == "job_start":
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


def _write_acceptance_evidence(path: Path, filename: str, value: dict) -> None:
    path.mkdir(parents=True, exist_ok=True)
    destination = path / filename
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


def _attempt_row(db_path: Path, *, attempt_id: str | None = None,
                 job_id: str | None = None) -> dict | None:
    if not db_path.exists():
        return None
    with sqlite3.connect(db_path) as db:
        db.row_factory = sqlite3.Row
        if attempt_id is not None:
            row = db.execute(
                "SELECT * FROM test_attempts WHERE attempt_id = ?", (attempt_id,)
            ).fetchone()
        else:
            row = db.execute(
                "SELECT * FROM test_attempts WHERE job_id = ? ORDER BY attempt_order DESC LIMIT 1",
                (job_id,),
            ).fetchone()
    if row is None:
        return None
    result = dict(row)
    for column, key in (("target_json", "target"), ("before_json", "before"),
                        ("after_json", "after"), ("summary_json", "summary"),
                        ("observation_json", "observation")):
        raw = result.pop(column, None)
        result[key] = json.loads(raw) if isinstance(raw, str) and raw else None
    return result


def _latest_attempt_row(db_path: Path, workspace_id: str, target_id: str) -> dict | None:
    if not db_path.exists():
        return None
    with sqlite3.connect(db_path) as db:
        row = db.execute(
            "SELECT attempt_id FROM test_attempts WHERE workspace_id = ? AND target_id = ? "
            "ORDER BY attempt_order DESC LIMIT 1",
            (workspace_id, target_id),
        ).fetchone()
    return _attempt_row(db_path, attempt_id=row[0]) if row else None


def _last_test_row(db_path: Path, workspace_id: str, target_id: str) -> dict | None:
    with sqlite3.connect(db_path) as db:
        row = db.execute(
            "SELECT observation, attempt_order, attempt_id FROM last_tests "
            "WHERE workspace_id = ? AND target_id = ?",
            (workspace_id, target_id),
        ).fetchone()
    if row is None:
        return None
    value = json.loads(row[0])
    value.setdefault("attempt_order", row[1])
    value.setdefault("attempt_id", row[2])
    return value


def _local_record(state_dir: Path, workspace_id: str, output_id: str) -> dict | None:
    path = state_dir / "audit" / workspace_id / output_id / "record.json"
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        return None


def _local_stdout(state_dir: Path, workspace_id: str, output_id: str) -> str:
    path = state_dir / "audit" / workspace_id / output_id / "stdout"
    try:
        return path.read_text(encoding="utf-8")
    except (FileNotFoundError, UnicodeDecodeError):
        return ""


async def _wait_for_local_collector(state_dir: Path, workspace_id: str, output_id: str,
                                    *, timeout: float, terminal: bool) -> dict:
    deadline = time.monotonic() + timeout
    last = None
    while time.monotonic() < deadline:
        last = _local_record(state_dir, workspace_id, output_id)
        if last is not None:
            if terminal:
                if last.get("sync_state") == "complete" and last.get("execution_state") == "finished":
                    return last
            elif (_local_stdout(state_dir, workspace_id, output_id).find(
                    "ORIGINAL_PROJECT_STARTED") >= 0):
                return last
        await asyncio.sleep(0.05)
    raise AssertionError(f"local collector did not reach expected state: {last!r}")


async def _wait_for_test_attempt(db_path: Path, *, attempt_id: str,
                                 timeout: float) -> dict:
    deadline = time.monotonic() + timeout
    last = None
    while time.monotonic() < deadline:
        last = _attempt_row(db_path, attempt_id=attempt_id)
        if last is not None and last.get("state") in {
                "finished", "failed", "cancelled", "timed_out", "unknown", "rejected"}:
            return last
        await asyncio.sleep(0.05)
    raise AssertionError(f"test attempt did not become terminal: {last!r}")


async def _wait_for_attempt_state(db_path: Path, *, attempt_id: str, expected: str,
                                  timeout: float) -> dict:
    deadline = time.monotonic() + timeout
    last = None
    while time.monotonic() < deadline:
        last = _attempt_row(db_path, attempt_id=attempt_id)
        if last is not None and last.get("state") == expected:
            return last
        await asyncio.sleep(0.05)
    raise AssertionError(
        f"test attempt did not reach {expected!r}: {last!r}")


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
        "  print('ORIGINAL_PROJECT_STARTED', flush=True)\n"
        "  print('CURSOR_PREFIX_START' + 'p' * 500 + 'CURSOR_PREFIX_END', flush=True)\n"
        "  if os.environ.get('RWB_TEST_BYPASS_GATE') != '1':\n"
        "   gate = os.environ['RWB_TEST_RELEASE_GATE']\n"
        "   deadline = time.monotonic() + 45\n"
        "   while os.path.exists(gate) and time.monotonic() < deadline: time.sleep(0.025)\n"
        "   if os.path.exists(gate): raise AssertionError('acceptance gate timed out')\n"
        "  if os.environ.get('RWB_TEST_COUNT_LAUNCH', '1') != '0':\n"
        "   label = os.environ.get('RWB_TEST_LAUNCH_LABEL', 'A')\n"
        "   with open(os.environ['RWB_TEST_LAUNCH_COUNTER'], 'a') as output: output.write(label + '\\n')\n"
        "  print('ORIGINAL_PROJECT_RESULT', flush=True)\n"
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
    gate_dir = tmp_path / "release-gates"
    gate_dir.mkdir()
    release_gate = gate_dir / "hold-original-unittest"
    release_gate.touch()
    _write_ssh_shim(bin_dir, remote_home=remote_home, safe_shell=safe_shell,
                    launch_counter=launch_counter, event_log=event_log,
                    drop_gate=drop_gate, dropped_log=dropped_log,
                    release_gate=release_gate)
    monkeypatch.setenv("PATH", str(bin_dir) + os.pathsep + os.environ.get("PATH", ""))
    evidence_dir = Path(os.environ.get("RWB_ACCEPTANCE_EVIDENCE_DIR", str(tmp_path)))
    config = tmp_path / "hosts.yaml"
    config.write_text(yaml.safe_dump({
        "default_host": "a",
        "hosts": {
            "a": {"ssh_host": "simulated-a", "default_cwd": str(source_a),
                  "execution_enabled": True, "job_timeout": 60,
                  "terminate_grace_seconds": 1},
            "b": {"ssh_host": "simulated-b", "default_cwd": str(source_b),
                  "execution_enabled": True, "job_timeout": 60,
                  "terminate_grace_seconds": 1},
        },
    }, sort_keys=True), encoding="utf-8")
    state_dir = tmp_path / "mcp-state"
    registry_path = state_dir / "workspaces.sqlite3"
    params = StdioServerParameters(command=sys.executable, args=[
        "-m", "remote_workbench.session_server", "--config", str(config),
        "--data-dir", str(state_dir), "--viewer-port", "0",
    ])
    drop_gate.touch()
    job_id = None
    background_attempt_id = None
    output_id = None
    completed = False
    fast_test_command = (
        "RWB_TEST_BYPASS_GATE=1 RWB_TEST_COUNT_LAUNCH=0 "
        "python3 -m unittest -v"
    )
    background_test_command = "python3 -m unittest -v"

    try:
        # The fast foreground run establishes a reliable prior observation for A.
        # The later gated background attempt must not replace it until completion.
        async with stdio_client(params) as (reader, writer):
            async with ClientSession(reader, writer) as client:
                await client.initialize()
                bound_a, _ = await _call(client, "workspace_switch", {
                    "host": "a", "cwd": str(source_a),
                })
                assert bound_a["bound"] is True
                target_a_id = bound_a["target_id"]
                project_a = Path(bound_a["project_root"])
                workspace_id = bound_a["workspace_id"]

                baseline_test, _ = await _call(client, "exec_command", {
                    "command": fast_test_command,
                })
                assert baseline_test.get("exit_code") == 0, baseline_test
                baseline_attempt = _latest_attempt_row(
                    registry_path, workspace_id, target_a_id)
                assert baseline_attempt is not None, "foreground test attempt was not persisted"
                assert baseline_attempt["execution_kind"] == "foreground"
                assert baseline_attempt["state"] == "finished"
                assert baseline_attempt["process_exit_status"] == "success"
                assert baseline_attempt["test_result"] == "success"
                baseline_last = _last_test_row(registry_path, workspace_id, target_a_id)
                assert baseline_last and baseline_last["attempt_id"] == baseline_attempt["attempt_id"]

                raw_start = await client.call_tool("start_job", {
                    "command": background_test_command,
                    "idempotency_key": IDEMPOTENCY_KEY,
                }, meta={"threadId": THREAD_ID})
                try:
                    start_result = _value(raw_start)
                except AssertionError:
                    start_result = {}
                job_id = start_result.get("job_id") or _reserved_job_id(
                    registry_path)
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
                background_attempt = _attempt_row(registry_path, job_id=job_id)
                assert background_attempt is not None, "background test attempt was not persisted"
                background_attempt_id = background_attempt["attempt_id"]
                assert background_attempt["attempt_order"] > baseline_attempt["attempt_order"]
                assert background_attempt["execution_kind"] == "background"
                assert background_attempt["state"] == "unknown"
                assert background_attempt["process_exit_status"] == "unknown"
                assert background_attempt["command"] == background_test_command
                assert background_attempt["cwd"] == str(project_a)
                assert background_attempt["target"]["target_id"] == target_a_id
                assert background_attempt["target"]["host"] == "a"
                assert background_attempt["target"]["source_cwd"] == str(source_a)
                assert background_attempt["target"]["project_root"] == str(project_a)
                assert background_attempt["started_at"] > 0
                assert isinstance(background_attempt["before"], dict)
                assert background_attempt["before"]["code"].get("complete") is True
                assert background_attempt["before"]["code"].get("digest")

        # stdio_client has now stopped the first MCP process; the detached local
        # worker persists independently in A's remote-state directory.
        assert _reserved_job_id(registry_path) == job_id

        # A second actual MCP process reuses the registry. It resumes locally,
        # then switches to B before recovering the original job from saved A.
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
                    "command": background_test_command,
                    "idempotency_key": IDEMPOTENCY_KEY,
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
                assert recovered["execution_state"] == "running", recovered
                assert recovered.get("local_job"), recovered
                assert recovered["local_job"]["execution_state"] == "running"
                output_id = recovered["local_job"].get("job_output_id")
                assert output_id
                recovered_attempt = recovered.get("test_attempt")
                assert recovered_attempt is not None
                assert recovered_attempt["attempt_id"] == background_attempt_id
                assert recovered_attempt["attempt_order"] == background_attempt["attempt_order"]
                resolve_events = _events(event_log)[before_resolve_events:]
                assert resolve_events, "recovery should contact the original target"
                assert all(e["target"] == "simulated-a" for e in resolve_events), (
                    "recovery must use A's saved target after switching to B; "
                    f"events={resolve_events!r}"
                )
                assert all(e["server_pid"] != initial_start_event["server_pid"]
                           for e in resolve_events), "recovery must run in a new MCP process"
                assert len({e["server_pid"] for e in resolve_events}) == 1

                # The worker is held outside both project trees. The real local
                # collector must be attached and have received only a prefix.
                local_record = await _wait_for_local_collector(
                    state_dir, workspace_id, output_id, timeout=12, terminal=False)
                assert local_record["execution_state"] == "running"
                assert Path(local_record["remote_job_dir"]) == expected_job_dir
                partial, _ = await _call(client, "job_status", {
                    "job_id": job_id, "stdout_cursor": 0, "stderr_cursor": 0,
                    "max_bytes": 256,
                })
                assert partial["execution_state"] == "running", partial
                assert partial["job_output_id"] == output_id
                assert partial["stdout_has_more"] is True, partial
                first_stdout_page = partial["stdout_tail"]
                first_stderr_page = partial["stderr_tail"]
                cursor_trace = [0, partial["next_stdout_cursor"]]
                assert "ORIGINAL_PROJECT_STARTED" in first_stdout_page
                assert "CURSOR_PREFIX_START" in first_stdout_page
                assert partial["next_stdout_cursor"] > 0
                verified_running_attempt = await _wait_for_attempt_state(
                    registry_path, attempt_id=background_attempt_id,
                    expected="running", timeout=8)

                # Switch back to A only to run a newer, quick foreground test.
                # Its order becomes the latest reliable observation while the
                # older background job remains blocked and unresolved.
                bound_a_again, _ = await _call(client, "workspace_switch", {
                    "host": "a", "cwd": str(source_a),
                })
                assert bound_a_again["target_id"] == target_a_id
                before_a_resume_events = len(_events(event_log))
                a_resume_while_blocked, _ = await _call(client, "workspace_resume")
                assert len(_events(event_log)) == before_a_resume_events
                latest_while_blocked = a_resume_while_blocked.get("latest_test_attempt")
                previous_while_blocked = a_resume_while_blocked.get("last_test")
                assert latest_while_blocked["attempt_id"] == background_attempt_id
                assert latest_while_blocked["state"] == "running"
                assert latest_while_blocked["attempt_id"] == verified_running_attempt["attempt_id"]
                assert previous_while_blocked["attempt_id"] == baseline_attempt["attempt_id"]
                assert previous_while_blocked["attempt_order"] < latest_while_blocked["attempt_order"]
                assert a_resume_while_blocked["remote_query_performed"] is False
                assert previous_while_blocked["historical"] is True
                assert previous_while_blocked["current_applicability"] == "unknown"
                assert previous_while_blocked["applies_to_current_code"] is None

                newer_test, _ = await _call(client, "exec_command", {
                    "command": fast_test_command,
                })
                assert newer_test.get("exit_code") == 0, newer_test
                newer_attempt = _latest_attempt_row(registry_path, workspace_id, target_a_id)
                assert newer_attempt is not None
                assert newer_attempt["execution_kind"] == "foreground"
                assert newer_attempt["state"] == "finished"
                assert newer_attempt["process_exit_status"] == "success"
                assert newer_attempt["test_result"] == "success"
                assert newer_attempt["attempt_order"] > background_attempt["attempt_order"]
                latest_last_test = _last_test_row(registry_path, workspace_id, target_a_id)
                assert latest_last_test["attempt_id"] == newer_attempt["attempt_id"]
                assert latest_last_test["attempt_order"] == newer_attempt["attempt_order"]

                # Return to B, demonstrate B has no A evidence, and only then
                # release the gate. Resume remains local; snapshot is fresh.
                bound_b_again, _ = await _call(client, "workspace_switch", {
                    "host": "b", "cwd": str(source_b),
                })
                assert bound_b_again["target_id"] == bound_b["target_id"]
                before_b_resume_events = len(_events(event_log))
                b_resume, _ = await _call(client, "workspace_resume")
                assert len(_events(event_log)) == before_b_resume_events
                assert b_resume["remote_query_performed"] is False
                assert b_resume.get("last_test") is None
                assert b_resume.get("latest_test_attempt") is None
                b_snapshot, _ = await _call(client, "workspace_snapshot")
                assert b_snapshot.get("last_test") is None
                assert b_snapshot.get("test_evidence_comparison") is None
                assert release_gate.exists()
                before_gate_release_events = len(_events(event_log))
                release_gate.unlink()

                # No job_status/job_wait call occurs between gate release and
                # this direct local check: the collector itself must persist
                # the background completion against its saved A attempt.
                terminal_record = await _wait_for_local_collector(
                    state_dir, workspace_id, output_id, timeout=20, terminal=True)
                terminal_attempt = await _wait_for_test_attempt(
                    registry_path, attempt_id=background_attempt_id, timeout=20)
                collector_events = _events(event_log)[before_gate_release_events:]
                assert collector_events, "collector should read A's finished output independently"
                assert all(event["target"] == "simulated-a" for event in collector_events)
                assert terminal_attempt["state"] == "finished"
                assert terminal_attempt["process_exit_status"] == "success"
                assert terminal_attempt["exit_code"] == 0
                assert terminal_attempt["test_result"] == "success"
                assert terminal_attempt["job_id"] == job_id
                assert terminal_attempt["attempt_id"] == background_attempt_id
                assert terminal_attempt["attempt_order"] == background_attempt["attempt_order"]
                assert terminal_attempt["command"] == background_test_command
                assert terminal_attempt["cwd"] == str(project_a)
                assert terminal_attempt["target"]["target_id"] == target_a_id
                assert terminal_attempt["target"]["host"] == "a"
                assert terminal_attempt["target"]["source_cwd"] == str(source_a)
                assert terminal_attempt["target"]["project_root"] == str(project_a)
                assert terminal_attempt["before"]["code"].get("complete") is True
                assert terminal_attempt["before"]["code"].get("digest")
                assert terminal_attempt["after"]["code"].get("complete") is True
                assert terminal_attempt["after"]["code"].get("digest")
                assert terminal_attempt["summary"]["tests_run"] == 1
                assert terminal_attempt["summary"]["reported_skips"] == 0
                assert terminal_attempt["summary"]["executed"] == 1
                assert terminal_attempt["summary"]["passed"] == 1
                assert terminal_attempt["observation"]["summary_stream"] == "stderr"
                assert terminal_attempt["output_id"] == output_id
                assert terminal_record["execution_state"] == "finished"
                assert terminal_record["sync_state"] == "complete"
                assert Path(terminal_record["remote_job_dir"]) == expected_job_dir
                last_after_late_finish = _last_test_row(registry_path, workspace_id, target_a_id)
                assert last_after_late_finish["attempt_id"] == newer_attempt["attempt_id"]
                assert last_after_late_finish["attempt_order"] == newer_attempt["attempt_order"]

                # job_wait and job_status now resume from the already-read byte
                # cursor, then page the completed output without duplication.
                before_job_control_events = len(_events(event_log))
                waited, _ = await _call(client, "job_wait", {
                    "job_id": job_id, "wait_seconds": 1,
                    "stdout_cursor": partial["next_stdout_cursor"],
                    "stderr_cursor": partial["next_stderr_cursor"],
                    "max_bytes": 256,
                })
                assert waited.get("error") is None, waited
                assert waited.get("execution_state") == "finished", waited
                assert waited.get("sync_state") == "complete", waited
                assert waited.get("job_output_id") == output_id
                assert waited.get("test_attempt", {}).get("attempt_id") == background_attempt_id
                stdout_pages = [first_stdout_page, waited["stdout_tail"]]
                stderr_pages = [first_stderr_page, waited["stderr_tail"]]
                stdout_cursor = waited["next_stdout_cursor"]
                stderr_cursor = waited["next_stderr_cursor"]
                cursor_trace.append(stdout_cursor)
                status = waited
                for _ in range(12):
                    if not status["stdout_has_more"] and not status["stderr_has_more"]:
                        break
                    status, _ = await _call(client, "job_status", {
                        "job_id": job_id, "stdout_cursor": stdout_cursor,
                        "stderr_cursor": stderr_cursor, "max_bytes": 256,
                    })
                    assert status.get("test_attempt", {}).get("attempt_id") == background_attempt_id
                    stdout_pages.append(status["stdout_tail"])
                    stderr_pages.append(status["stderr_tail"])
                    stdout_cursor = status["next_stdout_cursor"]
                    stderr_cursor = status["next_stderr_cursor"]
                    cursor_trace.append(stdout_cursor)
                assert not status["stdout_has_more"] and not status["stderr_has_more"], status
                assert all(left < right for left, right in zip(cursor_trace, cursor_trace[1:]))
                captured_stdout = "".join(stdout_pages)
                captured_stderr = "".join(stderr_pages)
                assert captured_stdout.count("ORIGINAL_PROJECT_STARTED") == 1
                assert captured_stdout.count("ORIGINAL_PROJECT_RESULT") == 1
                assert captured_stdout.count("CURSOR_PREFIX_START") == 1
                assert captured_stdout.count("CURSOR_PREFIX_END") == 1
                assert captured_stdout.count("CURSOR_PAYLOAD_START") == 1
                assert captured_stdout.count("x" * 400) == 1
                assert captured_stderr.count("Ran 1 test") == 1
                assert captured_stderr.rstrip().endswith("OK")
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
                job_control_events = _events(event_log)[before_job_control_events:]
                assert job_control_events
                assert all(event["target"] == "simulated-a" for event in job_control_events)
                completed = True

                # Resume A as local history, then explicitly take a fresh
                # snapshot to judge code applicability. Environment remains
                # unverified even when the bounded content fingerprint matches.
                bound_a_final, _ = await _call(client, "workspace_switch", {
                    "host": "a", "cwd": str(source_a),
                })
                assert bound_a_final["target_id"] == target_a_id
                final_resume_events = len(_events(event_log))
                final_resume, _ = await _call(client, "workspace_resume")
                assert len(_events(event_log)) == final_resume_events
                final_latest_attempt = final_resume["latest_test_attempt"]
                final_last_test = final_resume["last_test"]
                assert final_latest_attempt["attempt_id"] == newer_attempt["attempt_id"]
                assert final_latest_attempt["attempt_order"] == newer_attempt["attempt_order"]
                assert final_latest_attempt["cache_freshness"] == "unknown"
                assert final_last_test["attempt_id"] == newer_attempt["attempt_id"]
                assert final_last_test["attempt_order"] == newer_attempt["attempt_order"]
                assert final_last_test["historical"] is True
                assert final_last_test["current_applicability"] == "unknown"
                assert final_last_test["applies_to_current_code"] is None
                assert final_last_test["current_code_checked"] is False
                assert final_last_test["environment_limit"]
                assert final_last_test["environment_verified_current"] is False

                fresh_snapshot, _ = await _call(client, "workspace_snapshot")
                fresh_last_test = fresh_snapshot["last_test"]
                fresh_latest_attempt = fresh_snapshot["latest_test_attempt"]
                assert fresh_latest_attempt["attempt_id"] == newer_attempt["attempt_id"]
                assert fresh_latest_attempt["attempt_order"] == newer_attempt["attempt_order"]
                assert fresh_latest_attempt["cache_freshness"] == "unknown"
                assert fresh_latest_attempt["applies_to_current_code"] is True
                assert fresh_latest_attempt["current_applicability"] == "current"
                assert fresh_latest_attempt["historical"] is False
                assert fresh_latest_attempt["test_execution_is_historical"] is True
                assert fresh_latest_attempt["environment_verified_current"] is False
                assert fresh_last_test["attempt_id"] == newer_attempt["attempt_id"]
                assert fresh_last_test["attempt_order"] == newer_attempt["attempt_order"]
                assert fresh_last_test["applies_to_current_code"] is True
                assert fresh_last_test["historical"] is False
                assert fresh_last_test["test_execution_is_historical"] is True
                assert fresh_last_test["current_code_checked"] is True
                assert fresh_last_test["environment_verified_current"] is False
                assert fresh_last_test["environment_limit"]

                start_events = [e for e in _events(event_log) if e.get("op") == "job_start"]
                assert len(start_events) == 1
                assert launch_counter.read_text().splitlines() == ["A"]
                assert len([e for e in _events(dropped_log) if e["op"] == "job_start"]) == 1
                _write_acceptance_evidence(evidence_dir, "d3-stdio-evidence.json", {
                    "classification": "real MCP stdio subprocesses; test-only simulated SSH; real local worker; no live SSH, real server, or App-loaded claim",
                    "fault": {
                        "dropped_success_reply_after_persist": True,
                        "job_id": job_id,
                        "job_dir": dropped[0]["job_dir"],
                        "worker_status_and_origin_existed": True,
                    },
                    "mcp_server_pids": [
                        initial_start_event["server_pid"], resolve_events[0]["server_pid"],
                    ],
                    "target_recovery": {
                        "original_target_id": target_a_id,
                        "original_ssh_target": "simulated-a",
                        "current_target_during_recovery": bound_b["target_id"],
                        "current_target_during_completion": bound_b_again["target_id"],
                        "current_ssh_target_during_completion": "simulated-b",
                        "recovered_target_id": recovered["target"]["target_id"],
                        "recovered_target_host": recovered["target"]["host"],
                    },
                    "worker_launches": {
                        "job_start_requests": len(start_events),
                        "command_counter_lines": launch_counter.read_text().splitlines(),
                        "b_project_launch_absent": "B" not in launch_counter.read_text().splitlines(),
                    },
                    "test_attempts": {
                        "baseline": {
                            "attempt_id": baseline_attempt["attempt_id"],
                            "attempt_order": baseline_attempt["attempt_order"],
                            "state": baseline_attempt["state"],
                            "test_result": baseline_attempt["test_result"],
                        },
                        "background": {
                            "attempt_id": terminal_attempt["attempt_id"],
                            "attempt_order": terminal_attempt["attempt_order"],
                            "job_id": terminal_attempt["job_id"],
                            "state": terminal_attempt["state"],
                            "process_exit_status": terminal_attempt["process_exit_status"],
                            "exit_code": terminal_attempt["exit_code"],
                            "test_result": terminal_attempt["test_result"],
                            "command": terminal_attempt["command"],
                            "cwd": terminal_attempt["cwd"],
                            "target": terminal_attempt["target"],
                            "before": terminal_attempt["before"],
                            "after": terminal_attempt["after"],
                            "summary": terminal_attempt["summary"],
                            "summary_stream": terminal_attempt["observation"]["summary_stream"],
                            "output_id": terminal_attempt["output_id"],
                        },
                        "newer_foreground": {
                            "attempt_id": newer_attempt["attempt_id"],
                            "attempt_order": newer_attempt["attempt_order"],
                            "state": newer_attempt["state"],
                            "test_result": newer_attempt["test_result"],
                        },
                        "late_background_did_not_replace_latest_reliable": {
                            "latest_attempt_id": final_latest_attempt["attempt_id"],
                            "last_test_attempt_id": final_last_test["attempt_id"],
                            "expected_newer_attempt_id": newer_attempt["attempt_id"],
                            "background_order": terminal_attempt["attempt_order"],
                            "reliable_order": final_last_test["attempt_order"],
                        },
                    },
                    "handles": {
                        "job_id": job_id,
                        "job_output_id": output_id,
                        "execution_state": status["execution_state"],
                        "sync_state": waited.get("sync_state"),
                        "stop_result_state": stopped["execution_state"],
                    },
                    "output_paging": {
                        "cursor_trace": cursor_trace,
                        "stdout_pages": len(stdout_pages),
                        "marker_counts": {
                            name: captured_stdout.count(name) for name in (
                                "ORIGINAL_PROJECT_STARTED", "ORIGINAL_PROJECT_RESULT",
                                "CURSOR_PREFIX_START", "CURSOR_PREFIX_END", "CURSOR_PAYLOAD_START",
                            )
                        },
                        "complete_unittest_summary_present": (
                            ("Ran 1 test" in captured_stdout and "OK" in captured_stdout) or
                            ("Ran 1 test" in captured_stderr and captured_stderr.rstrip().endswith("OK"))
                        ),
                        "stderr_bytes_returned": len(captured_stderr.encode("utf-8")),
                        "final_page_empty": repeated["stdout_tail"] == repeated["stderr_tail"] == "",
                    },
                    "applicability": {
                        "resume_historical": {
                            "applies_to_current_code": final_last_test["applies_to_current_code"],
                            "current_applicability": final_last_test["current_applicability"],
                            "historical": final_last_test["historical"],
                            "environment_limit": final_last_test["environment_limit"],
                        },
                        "fresh_snapshot": {
                            "attempt_id": fresh_last_test["attempt_id"],
                            "attempt_order": fresh_last_test["attempt_order"],
                            "applies_to_current_code": fresh_last_test["applies_to_current_code"],
                            "environment_verified_current": fresh_last_test["environment_verified_current"],
                            "environment_limit": fresh_last_test["environment_limit"],
                        },
                        "b_snapshot_has_no_a_test_evidence": b_snapshot.get("last_test") is None,
                    },
                    "collector_terminal_record": {
                        "execution_state": terminal_record["execution_state"],
                        "sync_state": terminal_record["sync_state"],
                        "remote_job_dir": terminal_record["remote_job_dir"],
                        "verified_before_any_terminal_mcp_poll": True,
                        "post_release_ssh_targets": sorted({
                            event["target"] for event in collector_events
                        }),
                    },
                    "ssh_events": [
                        {key: event.get(key) for key in ("target", "op", "job_id", "server_pid", "dropped")}
                        for event in _events(event_log)
                    ],
                })

        assert launch_counter.read_text().splitlines() == ["A"]
    finally:
        # Always release the external gate before cleanup so a failure cannot
        # strand the local worker. Stop only this test's exact known job ID.
        release_gate.unlink(missing_ok=True)
        job_id = job_id or _reserved_job_id(registry_path)
        if job_id and not completed:
            async def cleanup_job():
                async with stdio_client(params) as (reader, writer):
                    async with ClientSession(reader, writer) as client:
                        await client.initialize()
                        await _call(client, "stop_job", {"job_id": job_id})
            try:
                await asyncio.wait_for(cleanup_job(), timeout=12)
            except Exception:
                # The gate is already released and the configured job timeout
                # is bounded; don't mask the original assertion failure.
                pass
