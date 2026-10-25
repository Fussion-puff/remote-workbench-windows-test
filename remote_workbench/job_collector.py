"""Persistent local job handles and autonomous, read-only SSH log collection."""
from __future__ import annotations

import asyncio
import base64
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import time
import uuid

from remote_workbench import audit
from remote_workbench.remote_shell import python_command

_JOB_ID = re.compile(r"^[a-zA-Z0-9_-]{1,80}$")


def _handle_path(executor, job_id):
    if not _JOB_ID.fullmatch(job_id):
        raise ValueError("Invalid job ID")
    identity = [executor.config.ssh_target, executor.config.default_cwd,
                executor.remote_state_dir, job_id]
    key = hashlib.sha256(json.dumps(identity).encode()).hexdigest()
    return audit.root() / ".job-handles" / (key + ".json")


def local_info(executor, job_id):
    path = _handle_path(executor, job_id)
    try:
        handle = json.loads(path.read_text())
        record = json.loads((audit.directory(handle["job_output_id"]) / "record.json").read_text())
        result = {key: record.get(key) for key in (
            "running", "exit_code", "execution_state", "sync_state", "remote_task", "sync_error", "synced_at")}
        result.update(job_id=job_id, job_output_id=handle["job_output_id"])
        for stream in ("stdout", "stderr"):
            result[stream + "_bytes"] = (audit.directory(handle["job_output_id"]) / stream).stat().st_size
        return result
    except FileNotFoundError:
        return {"error": "local_job_not_found", "job_id": job_id}


async def track(executor, result, command, cwd):
    path = _handle_path(executor, result["job_id"])
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    if not path.exists():
        record_id = audit.create(kind="job", parent_id=audit.operation_id.get(),
            host=executor.config.ssh_target, cwd=cwd, command=command, job_id=result["job_id"],
            remote_job_dir=result["job_dir"], remote_task=result.get("remote_task"),
            running=True, exit_code=None, execution_state="running", sync_state="queued", status="running")
        audit.add_child(audit.operation_id.get(), record_id)
        for stream in ("stdout", "stderr"):
            with audit.open_stream(record_id, stream):
                pass
        handle = dict(job_id=result["job_id"], job_dir=result["job_dir"], job_output_id=record_id)
        temporary = path.with_suffix("." + uuid.uuid4().hex + ".tmp")
        with open(temporary, "x", opener=lambda p, f: os.open(p, f, 0o600)) as output:
            json.dump(handle, output)
        temporary.replace(path)
    return await ensure(executor, result["job_id"])


async def ensure(executor, job_id):
    info = local_info(executor, job_id)
    if info.get("error") or info.get("sync_state") == "complete":
        return info
    tasks = getattr(executor, "_log_collectors", None)
    if tasks is None:
        tasks = executor._log_collectors = {}
    path = _handle_path(executor, job_id)
    key = str(path)
    if key not in tasks or tasks[key].done():
        # ContextVars capture the originating session's audit root and identity.
        tasks[key] = asyncio.create_task(_collect(executor, path), name="rwb-log-" + job_id)
        await asyncio.sleep(0)
    return local_info(executor, job_id)


async def close(executor):
    tasks = list(getattr(executor, "_log_collectors", {}).values())
    for task in tasks:
        task.cancel()
    await asyncio.gather(*tasks, return_exceptions=True)


async def _collect(executor, path):
    lock = None
    process = heartbeat = errors = None
    record_id = None
    try:
        # Read the already-atomically-published handle before opening the
        # inter-process lock, so even a lock-file permission error can mark
        # this local record unknown instead of leaving it indefinitely queued.
        handle = json.loads(path.read_text())
        record_id = handle["job_output_id"]
        record_dir = audit.directory(record_id)
        lock = os.open(path.with_suffix(".lock"), os.O_CREAT | os.O_RDWR
                       | getattr(os, "O_NOFOLLOW", 0), 0o600)
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return  # Another MCP process owns this exact log stream.
        cursors = {stream: (record_dir / stream).stat().st_size for stream in ("stdout", "stderr")}
        settings = dict(job_dir=handle["job_dir"], cursors=cursors,
                        prefix="RWB_FOLLOW_" + uuid.uuid4().hex + ":")
        source = ("login_spec = None\n" + Path(__file__).with_name("remote_task.py").read_text()
                  + "\n" + Path(__file__).with_name("remote_log_follow.py").read_text()
                  + "\nfollow_logs(" + repr(settings) + ")\n")
        audit.update(record_id, sync_state="syncing", sync_error=None)
        process = await asyncio.create_subprocess_exec(*executor._ssh_args(python_command(source)),
            stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
            limit=131072)

        async def pulse():
            while True:
                process.stdin.write(b".\n")
                await process.stdin.drain()
                await asyncio.sleep(3)

        async def drain_errors():
            tail = b""
            while data := await process.stderr.read(4096):
                tail = (tail + data)[-2048:]
            return tail.decode(errors="replace")

        heartbeat = asyncio.create_task(pulse())
        errors = asyncio.create_task(drain_errors())
        async for line in process.stdout:
            prefix = settings["prefix"].encode()
            position = line.find(prefix)
            if position < 0:
                continue
            value = json.loads(line[position + len(prefix):])
            if value["type"] == "data":
                stream = value["stream"]
                if stream not in cursors or value["offset"] != cursors[stream]:
                    raise ValueError("Log cursor mismatch; preserved existing local output")
                data = base64.b64decode(value["data"], validate=True)
                with (record_dir / stream).open("ab") as output:
                    output.write(data)
                cursors[stream] += len(data)
                audit.update(record_id, **{stream + "_bytes": cursors[stream]}, synced_at=time.time())
            elif value["type"] == "status":
                complete = value.pop("complete")
                value.pop("type")
                audit.update(record_id, **value, synced_at=time.time(),
                    sync_state="complete" if complete else "syncing",
                    status="finished" if complete else "running" if value.get("running") else "unknown")
            else:
                raise ValueError(value.get("message", "Log follower error"))
        await process.wait()
        diagnostic = await errors
        current = local_info(executor, handle["job_id"])
        if current.get("sync_state") != "complete":
            audit.update(record_id, sync_state="disconnected", running=None, execution_state="unknown",
                         sync_error=diagnostic or "Log channel closed; task completion is unconfirmed")
    except asyncio.CancelledError:
        if record_id:
            try:
                audit.update(record_id, sync_state="disconnected", running=None,
                             execution_state="unknown",
                             sync_error="Local collection stopped; remote job was not cancelled")
            except (OSError, ValueError):
                pass
        raise
    except Exception as exc:
        if record_id:
            try:
                audit.update(record_id, sync_state="disconnected", running=None,
                             execution_state="unknown", sync_error=str(exc))
            except (OSError, ValueError):
                # A damaged/unwritable local record still remains non-complete;
                # never promote it merely because its follower failed.
                pass
    finally:
        for task in (heartbeat, errors):
            if task is not None:
                task.cancel()
        await asyncio.gather(*(task for task in (heartbeat, errors) if task is not None), return_exceptions=True)
        if process is not None:
            if process.stdin:
                process.stdin.close()  # EOF ends only the read-only follower.
            if process.returncode is None:
                try:
                    await asyncio.wait_for(process.wait(), 2)
                except asyncio.TimeoutError:
                    process.kill()  # Only this client, never the master/job.
                    await process.wait()
        if lock is not None:
            os.close(lock)
