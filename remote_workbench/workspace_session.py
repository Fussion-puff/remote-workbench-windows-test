"""One local MCP session and its lazily created remote project copy."""
from __future__ import annotations

import asyncio
from dataclasses import replace
import json
import posixpath
import time
import uuid

from remote_workbench import audit
from remote_workbench.session_project import prepare_project
from remote_workbench.ssh import SSHExecutor


class WorkspaceSession:
    def __init__(self, template):
        self.id = uuid.uuid4().hex
        self.source = template
        self.audit_dir = audit.base_root() / "sessions" / self.id
        self.audit_dir.mkdir(mode=0o700, parents=True, exist_ok=False)
        destination = posixpath.join(
            posixpath.dirname(template.cwd), ".remote-workbench-sessions",
            posixpath.basename(template.cwd) or "root", self.id, "project",
        )
        self.project_root = destination
        config = replace(template.executor.config, default_cwd=destination)
        self.workspace = replace(template, cwd=destination, executor=SSHExecutor(
            config, remote_state_dir=destination + "/.remote-workbench-state"))
        self.prepared = False
        self._prepare_lock = asyncio.Lock()
        self._save()

    def _save(self):
        path = self.audit_dir / "session.json"
        value = {"session_id": self.id, "host": self.source.host,
                 "source_cwd": self.source.cwd, "cwd": self.workspace.cwd,
                 "prepared": self.prepared}
        temporary = path.with_suffix(".tmp")
        temporary.write_text(json.dumps(value, indent=2) + "\n")
        temporary.chmod(0o600)
        temporary.replace(path)

    async def ensure_ready(self):
        if self.prepared:
            return
        async with self._prepare_lock:
            if self.prepared:
                return
            with audit.scoped_root(self.audit_dir):
                record = audit.create(kind="tool", tool="prepare_workspace", host=self.source.host,
                                      cwd=self.workspace.cwd, status="running",
                                      arguments={"source_cwd": self.source.cwd, "session_id": self.id})
                token = audit.operation_id.set(record)
                started = time.monotonic()
                try:
                    connected, message = await self.source.executor.check_connection()
                    if not connected:
                        raise ValueError(message)
                    result = await prepare_project(self.source.executor, self.source.cwd,
                                                   self.id, self.project_root)
                    if result is not None:
                        cwd = result["cwd"]
                        if posixpath.commonpath([self.project_root, cwd]) != self.project_root:
                            raise ValueError("Prepared project is outside this session")
                        if cwd != self.workspace.cwd:
                            config = replace(self.workspace.executor.config, default_cwd=cwd)
                            self.workspace = replace(self.workspace, cwd=cwd, executor=SSHExecutor(
                                config, remote_state_dir=self.project_root + "/.remote-workbench-state"))
                    self.prepared = True
                    self._save()
                    audit.update(record, status="finished", cwd=self.workspace.cwd,
                                 output_id=result.get("output_id") if result else None,
                                 elapsed_ms=round((time.monotonic() - started) * 1000))
                except BaseException as exc:
                    audit.update(record, status="failed", error=str(exc))
                    raise
                finally:
                    audit.operation_id.reset(token)
