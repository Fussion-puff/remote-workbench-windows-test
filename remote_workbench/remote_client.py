"""Transport JSON requests over stdin; source and content never use shell interpolation."""
import json
import shlex
import uuid
from pathlib import Path
from remote_workbench import audit

SOURCE = Path(__file__).with_name("remote_worker.py").read_text()


async def request(executor, payload, cwd=None, timeout=None):
    payload.setdefault("max_file_size", executor.config.max_file_size)
    payload["_response_id"] = uuid.uuid4().hex
    return await executor.run("python3 -c " + shlex.quote(SOURCE), cwd=cwd,
        timeout=timeout or executor.config.file_timeout,
        input_data=json.dumps(payload, ensure_ascii=False).encode())


async def edit(executor, payload, cwd=None, max_bytes=8192, timeout=None):
    if not 256 <= max_bytes <= 65536:
        raise ValueError("max_bytes must be between 256 and 65536")
    result = await request(executor, payload, cwd, timeout=timeout)
    if result.exit_code != 0:
        return {"error": "remote_failed", "message": result.stderr, "output_id": result.output_id,
                "execution_state": result.execution_state}
    # Structured worker output is read from the complete spool, not its preview.
    path = audit.directory(result.output_id) / "stdout"
    maximum = executor.config.max_file_size * 8 + 1048576
    if path.stat().st_size > maximum:
        return {"error": "worker_output_too_large", "output_id": result.output_id}
    try:
        data = path.read_bytes()
        marker = ("\x1eREMOTE_WORKBENCH_RESULT_" + payload["_response_id"] + ":\x1f").encode()
        position = data.index(marker) + len(marker)
        value, _ = json.JSONDecoder().raw_decode(data[position:].decode("utf-8"))
    except ValueError:
        return {"error": "invalid_worker_output", "message": result.stdout, "output_id": result.output_id}
    audit.update(result.output_id, worker_op=payload.get("op"))
    if payload.get("op") == "apply_patch":
        # The human viewer can display the submitted patch directly from disk;
        # don't echo an already supplied patch back through the model channel.
        with audit.open_stream(result.output_id, "diff") as output:
            output.write(payload["patch"].encode("utf-8"))
        audit.update(result.output_id, diff_kind="submitted_patch",
                     check_only=bool(payload.get("check_only")),
                     patch_result="failed" if value.get("error") else "checked" if payload.get("check_only") else "applied")
    full_diff = value.pop("diff", "")
    if full_diff:
        data = full_diff.encode()
        with audit.open_stream(result.output_id, "diff") as output:
            output.write(data)
        audit.update(result.output_id, diff_kind="edit_preview" if payload.get("check_only") else "file_edit")
        value["diff"], value["diff_truncated"] = audit.preview(data, max_bytes)
    value["output_id"] = result.output_id
    return value
