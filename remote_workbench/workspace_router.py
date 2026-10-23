"""Persistent thread workspaces and immutable, request-scoped SSH targets."""
from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from dataclasses import dataclass, replace
import fcntl
import json
import os
from pathlib import Path
import posixpath
import uuid

from remote_workbench.config import ServerConfig
from remote_workbench.session_project import prepare_project
from remote_workbench.ssh import SSHExecutor
from remote_workbench.workspace import Workspace
from remote_workbench.workspace_registry import Registry, RevisionConflict, WorkspaceState, WorkspaceTarget


class WorkspaceUnbound(ValueError):
    pass


def default_state_dir() -> Path:
    """Stable local storage independent of the source or installed wheel."""
    configured = os.environ.get("REMOTE_WORKBENCH_DATA_DIR")
    if configured:
        return Path(configured).expanduser()
    return Path(os.environ.get("XDG_DATA_HOME", Path.home() / ".local/share")) / "remote-workbench"


@dataclass(frozen=True)
class ExecutionSnapshot:
    thread_id: str
    workspace_id: str
    host: str
    cwd: str
    source_cwd: str
    project_root: str
    target_id: str
    revision: int
    workspace: Workspace

    def metadata(self):
        return dict(thread_id=self.thread_id, workspace_id=self.workspace_id,
                    revision=self.revision, target_id=self.target_id)


class SessionRouter:
    def __init__(self, config: ServerConfig, state_dir: Path, *, executor_factory=SSHExecutor):
        self.config = config
        self.state_dir = Path(state_dir).expanduser().resolve()
        self.state_dir.mkdir(parents=True, mode=0o700, exist_ok=True)
        self.registry = Registry(self.state_dir / "workspaces.sqlite3")
        self.executor_factory = executor_factory
        self._executors = {}
        (self.state_dir / "locks").mkdir(mode=0o700, exist_ok=True)

    def audit_dir(self, workspace_id: str) -> Path:
        return self.state_dir / "audit" / workspace_id

    @asynccontextmanager
    async def lease(self, workspace_id: str):
        """Serialize foreground calls and switches across local MCP processes.

        Nonblocking flock polling is cancellable and never blocks the event
        loop. Different conversations use different locks and run in parallel.
        """
        # Only registry-issued UUIDs may become local path components.
        if len(workspace_id) != 32 or any(c not in "0123456789abcdef" for c in workspace_id):
            raise ValueError("Invalid workspace ID")
        path = self.state_dir / "locks" / (workspace_id + ".lock")
        fd = os.open(path, os.O_CREAT | os.O_RDWR | getattr(os, "O_NOFOLLOW", 0), 0o600)
        try:
            deadline = asyncio.get_running_loop().time() + 30
            while True:
                try:
                    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except BlockingIOError:
                    if asyncio.get_running_loop().time() >= deadline:
                        raise ValueError("This conversation has an active foreground operation; retry when it finishes")
                    await asyncio.sleep(0.05)
            yield
        finally:
            os.close(fd)

    def executor(self, host: str, cwd: str, project_root: str | None = None):
        # No default-host fallback and no inherited activation/preamble.
        if host not in self.config.hosts:
            raise ValueError("Host is not in the configured allowlist")
        key = (host, cwd, project_root)
        if key not in self._executors:
            config = replace(self.config.hosts[host], default_cwd=cwd, preamble="")
            self._executors[key] = self.executor_factory(
                config, remote_state_dir=(project_root + "/.remote-workbench-state") if project_root else None)
        return self._executors[key]

    def snapshot(self, state: WorkspaceState, target: WorkspaceTarget | None = None) -> ExecutionSnapshot:
        if target is None:
            if state.host is None:
                raise WorkspaceUnbound("This conversation has no remote workspace. Use workspace_switch(host, cwd) first")
            target = WorkspaceTarget(state.target_id, state.workspace_id, state.host,
                                     state.source_cwd, state.cwd, state.project_root)
        if target.workspace_id != state.workspace_id or not all((target.cwd, target.project_root, target.target_id)):
            raise ValueError("Invalid workspace target")
        executor = self.executor(target.host, target.cwd, target.project_root)
        workspace = Workspace(state.workspace_id, target.host, target.cwd, executor)
        return ExecutionSnapshot(state.thread_id, state.workspace_id, target.host, target.cwd,
                                 target.source_cwd, target.project_root, target.target_id,
                                 state.revision, workspace)

    async def inspect_directory(self, host: str, cwd: str) -> dict:
        if not isinstance(cwd, str) or not cwd.startswith("/") or "\x00" in cwd:
            raise ValueError("cwd must be an absolute remote directory")
        executor = self.executor(host, "/")
        connected, message = await executor.check_connection()
        if not connected:
            raise ValueError(message)
        code = ("import json,os,sys,subprocess\n"
                "p=os.path.realpath(sys.argv[1]); os.chdir(p)\n"
                "repository_root=None\n"
                "try:\n"
                " r=subprocess.run(['git','rev-parse','--show-toplevel'],stdout=subprocess.PIPE,stderr=subprocess.PIPE)\n"
                " if r.returncode==0: repository_root=os.path.realpath(os.fsdecode(r.stdout).strip())\n"
                "except FileNotFoundError: pass\n"
                "print(json.dumps({'cwd':os.getcwd(),'repository_root':repository_root}))")
        result = await executor.run_program(["python3", "-c", code, cwd], cwd="/", output_budget=2048)
        if result.exit_code != 0:
            raise ValueError("Remote directory validation failed: " + result.stderr)
        try:
            detail = json.loads(result.stdout)
            canonical = detail["cwd"]
        except (ValueError, KeyError, TypeError) as exc:
            raise ValueError("Invalid remote directory validation result") from exc
        if not isinstance(canonical, str) or not canonical.startswith("/"):
            raise ValueError("Remote directory was not absolute")
        repository_root = detail.get("repository_root")
        if repository_root is not None and (not isinstance(repository_root, str) or not repository_root.startswith("/")
                                           or posixpath.commonpath([canonical, repository_root]) != repository_root):
            raise ValueError("Invalid remote repository root")
        return detail

    async def validate_directory(self, host: str, cwd: str) -> str:
        return (await self.inspect_directory(host, cwd))["cwd"]

    async def switch(self, state: WorkspaceState, host: str, cwd: str, expected_revision: int | None = None):
        """Caller holds the workspace lease; publish only after remote validation."""
        expected = state.revision if expected_revision is None else expected_revision
        if isinstance(expected, bool) or not isinstance(expected, int) or expected != state.revision:
            raise RevisionConflict("Workspace revision changed; inspect workspace_info before retrying")
        detail = await self.inspect_directory(host, cwd)
        canonical = detail["cwd"]
        target = self.registry.find_target(state.workspace_id, host, canonical)
        if target is None:
            # Each thread/target owns its copy. Changing host/cwd never binds
            # an arbitrary shared source directory as a mutable workspace.
            target_id = uuid.uuid4().hex
            project_source = detail["repository_root"] or canonical
            destination = posixpath.join(posixpath.dirname(project_source), ".remote-workbench-sessions",
                                        posixpath.basename(project_source) or "root", target_id, "project")
            source_executor = self.executor(host, canonical)
            result = await prepare_project(source_executor, canonical, target_id, destination)
            actual = result["cwd"]
            if posixpath.commonpath([destination, actual]) != destination:
                raise ValueError("Prepared project escaped its private directory")
            target = WorkspaceTarget(target_id, state.workspace_id, host, canonical, actual, destination)
        else:
            # Never silently recreate a disappeared copy from the template;
            # that could lose edits the user expects to resume.
            actual = await self.validate_directory(host, target.cwd)
            if actual != target.cwd:
                raise ValueError("Saved workspace directory changed; refusing implicit relocation")
        return self.registry.commit_switch(state.workspace_id, expected, target)
