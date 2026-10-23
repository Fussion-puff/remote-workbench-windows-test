"""Private local operation records and byte-bounded output retrieval."""
from __future__ import annotations

import contextvars
import json
import os
import re
import time
import uuid
from pathlib import Path

operation_id = contextvars.ContextVar("operation_id", default=None)
_ID = re.compile(r"^[a-f0-9]{32}$")
STREAMS = {"stdout", "stderr", "stdin", "diff", "startup_stdout", "startup_stderr"}


def root() -> Path:
    path = Path(os.environ.get("REMOTE_WORKBENCH_AUDIT_DIR", Path(__file__).resolve().parent.parent / ".remote-workbench-audit"))
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    return path


def directory(record_id: str) -> Path:
    if not _ID.fullmatch(record_id):
        raise ValueError("Invalid operation/output ID")
    return root() / record_id


def create(**metadata) -> str:
    record_id = uuid.uuid4().hex
    directory(record_id).mkdir(mode=0o700)
    update(record_id, id=record_id, created_at=time.time(), **metadata)
    return record_id


def update(record_id: str, **values) -> None:
    path = directory(record_id) / "record.json"
    current = json.loads(path.read_text()) if path.exists() else {}
    current.update(values)
    temporary = path.with_suffix(".tmp")
    with open(temporary, "w", encoding="utf-8", opener=lambda p, f: os.open(p, f, 0o600)) as output:
        json.dump(current, output, ensure_ascii=False, indent=2, default=str)
    temporary.replace(path)


def add_child(parent_id: str | None, child_id: str) -> None:
    if parent_id:
        path = directory(parent_id) / "record.json"
        metadata = json.loads(path.read_text())
        update(parent_id, child_commands=metadata.get("child_commands", []) + [child_id])


def open_stream(record_id: str, stream: str):
    if stream not in STREAMS:
        raise ValueError("Unknown stream")
    return open(directory(record_id) / stream, "wb", opener=lambda p, f: os.open(p, f, 0o600))


def read_output(record_id: str, stream="stdout", cursor=0, max_bytes=8192) -> dict:
    if stream not in STREAMS:
        raise ValueError("Unknown stream")
    cursor, max_bytes = int(cursor), int(max_bytes)
    if cursor < 0 or not 4 <= max_bytes <= 65536:
        raise ValueError("cursor >= 0 and 4 <= max_bytes <= 65536 required")
    path = directory(record_id) / stream
    with path.open("rb") as source:
        source.seek(cursor)
        data = source.read(max_bytes)
    # Preserve UTF-8 boundaries when reading the next page.
    text = data.decode("utf-8", errors="replace")
    if len(data) == max_bytes:
        try:
            text = data.decode("utf-8")
        except UnicodeDecodeError as exc:
            if exc.reason == "unexpected end of data":
                data = data[:exc.start]
            text = data.decode("utf-8", errors="replace")
    end = cursor + len(data)
    return {"output_id": record_id, "stream": stream, "content": text,
            "next_cursor": end, "total_bytes": path.stat().st_size,
            "truncated": end < path.stat().st_size}


def preview(data: bytes, budget: int, max_lines=200) -> tuple[str, bool]:
    """Head/tail preview, with both a byte and line cap (including long lines)."""
    lines = data.splitlines(keepends=True)
    limited = len(lines) > max_lines
    if limited:
        left = max(1, max_lines // 2)
        data = b"".join(lines[:left]) + b"\n[truncated lines]\n" + b"".join(lines[-left:])
    if len(data) > budget:
        marker = b"\n[truncated bytes]\n"
        half = max(0, (budget - len(marker)) // 2)
        data = data[:half] + marker + data[-half:] if half else marker[:budget]
        limited = True
    # Ignore partial boundary bytes, preserving the byte cap.
    return data.decode("utf-8", errors="ignore"), limited


def recent(limit=50) -> list[dict]:
    paths = sorted(root().glob("*/record.json"), key=lambda p: p.stat().st_mtime, reverse=True)
    result = []
    for path in paths[:min(200, max(1, limit))]:
        try:
            value = json.loads(path.read_text())
            result.append({k: value[k] for k in ("id", "kind", "tool", "parent_id", "host", "cwd", "created_at", "status", "exit_code", "elapsed_ms") if k in value})
        except (OSError, ValueError):
            continue
    return result
