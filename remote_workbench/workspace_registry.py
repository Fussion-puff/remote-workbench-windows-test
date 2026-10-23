"""Persistent local identities and target history for session workspaces."""
from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
import os
from pathlib import Path
import sqlite3
from typing import Iterator
import uuid


@dataclass(frozen=True, slots=True)
class WorkspaceState:
    workspace_id: str
    thread_id: str
    host: str | None = None
    source_cwd: str | None = None
    cwd: str | None = None
    project_root: str | None = None
    target_id: str | None = None
    revision: int = 0


@dataclass(frozen=True, slots=True)
class WorkspaceTarget:
    target_id: str
    workspace_id: str
    host: str
    source_cwd: str
    cwd: str | None
    project_root: str | None


class RevisionConflict(ValueError):
    """Raised when a workspace was changed since the caller read it."""


class Registry:
    """SQLite registry with per-operation connections and transactional CAS."""

    _BUSY_TIMEOUT_MS = 5000
    _MAX_HISTORY_LIMIT = 100

    def __init__(self, path: Path):
        self.path = Path(path).expanduser()
        self.path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        if self.path.is_symlink():
            raise ValueError("Registry database path must not be a symlink")

        # Create the database with private permissions before SQLite opens it.
        try:
            fd = os.open(self.path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        except FileExistsError:
            os.chmod(self.path, 0o600)
        else:
            os.close(fd)

        with self._connection() as db:
            db.execute("PRAGMA journal_mode = WAL")
            db.execute("BEGIN IMMEDIATE")
            try:
                db.execute(
                    """CREATE TABLE IF NOT EXISTS workspaces (
                           workspace_id TEXT PRIMARY KEY,
                           thread_id TEXT NOT NULL UNIQUE,
                           host TEXT,
                           source_cwd TEXT,
                           cwd TEXT,
                           project_root TEXT,
                           target_id TEXT,
                           revision INTEGER NOT NULL DEFAULT 0,
                           created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                           updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                       )"""
                )
                db.execute(
                    """CREATE TABLE IF NOT EXISTS targets (
                           target_id TEXT PRIMARY KEY,
                           workspace_id TEXT NOT NULL REFERENCES workspaces(workspace_id)
                               ON DELETE CASCADE,
                           host TEXT NOT NULL,
                           source_cwd TEXT NOT NULL,
                           cwd TEXT,
                           project_root TEXT,
                           UNIQUE(workspace_id, host, source_cwd)
                       )"""
                )
                db.execute(
                    """CREATE TABLE IF NOT EXISTS target_history (
                           history_id INTEGER PRIMARY KEY AUTOINCREMENT,
                           workspace_id TEXT NOT NULL REFERENCES workspaces(workspace_id)
                               ON DELETE CASCADE,
                           target_id TEXT NOT NULL,
                           host TEXT NOT NULL,
                           source_cwd TEXT NOT NULL,
                           cwd TEXT,
                           project_root TEXT,
                           revision INTEGER NOT NULL,
                           changed_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                           UNIQUE(workspace_id, revision)
                       )"""
                )
                db.execute(
                    """CREATE TABLE IF NOT EXISTS jobs (
                           workspace_id TEXT NOT NULL REFERENCES workspaces(workspace_id)
                               ON DELETE CASCADE,
                           job_id TEXT NOT NULL,
                           target_id TEXT NOT NULL,
                           host TEXT NOT NULL,
                           source_cwd TEXT NOT NULL,
                           cwd TEXT,
                           project_root TEXT,
                           PRIMARY KEY(workspace_id, job_id)
                       )"""
                )
                db.execute(
                    "CREATE INDEX IF NOT EXISTS target_history_recent "
                    "ON target_history(workspace_id, revision DESC)"
                )
                db.execute("""CREATE TABLE IF NOT EXISTS call_metrics (
                    id INTEGER PRIMARY KEY, workspace_id TEXT NOT NULL REFERENCES workspaces(workspace_id),
                    tool TEXT NOT NULL, argument_bytes INTEGER NOT NULL, result_bytes INTEGER NOT NULL,
                    duration_ms INTEGER NOT NULL, failed INTEGER NOT NULL,
                    read_key TEXT, repeated_read INTEGER NOT NULL,
                    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                )""")
                db.execute("CREATE INDEX IF NOT EXISTS call_metrics_workspace ON call_metrics(workspace_id)")
                db.execute("CREATE INDEX IF NOT EXISTS call_metrics_reads ON call_metrics(workspace_id, read_key)")
                db.execute("""CREATE TABLE IF NOT EXISTS last_tests (
                    workspace_id TEXT NOT NULL REFERENCES workspaces(workspace_id), target_id TEXT NOT NULL,
                    observation TEXT NOT NULL, PRIMARY KEY(workspace_id, target_id)
                )""")
                db.commit()
            except BaseException:
                db.rollback()
                raise

        # SQLite creates sidecar files from the database's mode. Tighten any
        # that already exist as well, including files left by another process.
        for private_file in (self.path, Path(str(self.path) + "-wal"),
                             Path(str(self.path) + "-shm")):
            if private_file.exists():
                os.chmod(private_file, 0o600)

    @contextmanager
    def _connection(self) -> Iterator[sqlite3.Connection]:
        db = sqlite3.connect(
            self.path,
            timeout=self._BUSY_TIMEOUT_MS / 1000,
            isolation_level=None,
        )
        db.row_factory = sqlite3.Row
        db.execute(f"PRAGMA busy_timeout = {self._BUSY_TIMEOUT_MS}")
        db.execute("PRAGMA foreign_keys = ON")
        try:
            yield db
        finally:
            db.close()

    @contextmanager
    def _transaction(self) -> Iterator[sqlite3.Connection]:
        with self._connection() as db:
            db.execute("BEGIN IMMEDIATE")
            try:
                yield db
                db.commit()
            except BaseException:
                db.rollback()
                raise

    @staticmethod
    def _state(row: sqlite3.Row) -> WorkspaceState:
        return WorkspaceState(
            workspace_id=row["workspace_id"],
            thread_id=row["thread_id"],
            host=row["host"],
            source_cwd=row["source_cwd"],
            cwd=row["cwd"],
            project_root=row["project_root"],
            target_id=row["target_id"],
            revision=row["revision"],
        )

    @staticmethod
    def _target(row: sqlite3.Row) -> WorkspaceTarget:
        return WorkspaceTarget(
            target_id=row["target_id"],
            workspace_id=row["workspace_id"],
            host=row["host"],
            source_cwd=row["source_cwd"],
            cwd=row["cwd"],
            project_root=row["project_root"],
        )

    @staticmethod
    def _validate_target(workspace_id: str, target: WorkspaceTarget) -> None:
        if target.workspace_id != workspace_id:
            raise ValueError("Target belongs to a different workspace")
        for name in ("target_id", "host", "source_cwd"):
            value = getattr(target, name)
            if not isinstance(value, str) or not value:
                raise ValueError(f"Target {name} must be a non-empty string")
        for name in ("cwd", "project_root"):
            value = getattr(target, name)
            if value is not None and not isinstance(value, str):
                raise ValueError(f"Target {name} must be a string or None")

    def get_or_create(self, thread_id: str) -> WorkspaceState:
        if not isinstance(thread_id, str) or not thread_id:
            raise ValueError("thread_id must be a non-empty string")
        with self._transaction() as db:
            db.execute(
                "INSERT INTO workspaces(workspace_id, thread_id) VALUES (?, ?) "
                "ON CONFLICT(thread_id) DO NOTHING",
                (uuid.uuid4().hex, thread_id),
            )
            row = db.execute(
                "SELECT * FROM workspaces WHERE thread_id = ?", (thread_id,)
            ).fetchone()
            return self._state(row)

    def get(self, workspace_id: str) -> WorkspaceState:
        with self._connection() as db:
            row = db.execute(
                "SELECT * FROM workspaces WHERE workspace_id = ?", (workspace_id,)
            ).fetchone()
        if row is None:
            raise ValueError(f"Unknown workspace: {workspace_id}")
        return self._state(row)

    def find_target(
        self, workspace_id: str, host: str, source_cwd: str
    ) -> WorkspaceTarget | None:
        with self._connection() as db:
            row = db.execute(
                """SELECT * FROM targets
                   WHERE workspace_id = ? AND host = ? AND source_cwd = ?""",
                (workspace_id, host, source_cwd),
            ).fetchone()
        return self._target(row) if row is not None else None

    def commit_switch(
        self,
        workspace_id: str,
        expected_revision: int,
        target: WorkspaceTarget,
    ) -> WorkspaceState:
        self._validate_target(workspace_id, target)
        with self._transaction() as db:
            row = db.execute(
                "SELECT * FROM workspaces WHERE workspace_id = ?", (workspace_id,)
            ).fetchone()
            if row is None:
                raise ValueError(f"Unknown workspace: {workspace_id}")
            current = self._state(row)
            if current.revision != expected_revision:
                raise RevisionConflict(
                    f"Workspace {workspace_id} revision is {current.revision}, "
                    f"expected {expected_revision}"
                )

            existing = db.execute(
                """SELECT * FROM targets
                   WHERE workspace_id = ? AND host = ? AND source_cwd = ?""",
                (workspace_id, target.host, target.source_cwd),
            ).fetchone()
            canonical_target_id = existing["target_id"] if existing else target.target_id
            if existing is not None and self._target(existing) != target:
                raise ValueError("An existing target is immutable; resume its saved project directory")

            # Re-selecting the exact current target is an idempotent no-op:
            # it neither creates a history row nor increments the revision.
            if (
                current.target_id == canonical_target_id
                and current.host == target.host
                and current.source_cwd == target.source_cwd
                and current.cwd == target.cwd
                and current.project_root == target.project_root
            ):
                return current

            if existing is None:
                try:
                    db.execute(
                        """INSERT INTO targets
                           (target_id, workspace_id, host, source_cwd, cwd, project_root)
                           VALUES (?, ?, ?, ?, ?, ?)""",
                        (target.target_id, workspace_id, target.host, target.source_cwd,
                         target.cwd, target.project_root),
                    )
                except sqlite3.IntegrityError as exc:
                    raise ValueError("Target ID is already in use") from exc

            revision = current.revision + 1
            db.execute(
                """INSERT INTO target_history
                   (workspace_id, target_id, host, source_cwd, cwd, project_root, revision)
                   VALUES (?, ?, ?, ?, ?, ?, ?)""",
                (workspace_id, canonical_target_id, target.host, target.source_cwd,
                 target.cwd, target.project_root, revision),
            )
            db.execute(
                """UPDATE workspaces
                   SET host = ?, source_cwd = ?, cwd = ?, project_root = ?,
                       target_id = ?, revision = ?, updated_at = CURRENT_TIMESTAMP
                   WHERE workspace_id = ?""",
                (target.host, target.source_cwd, target.cwd, target.project_root,
                 canonical_target_id, revision, workspace_id),
            )
            updated = db.execute(
                "SELECT * FROM workspaces WHERE workspace_id = ?", (workspace_id,)
            ).fetchone()
            return self._state(updated)

    def history(self, workspace_id: str, limit: int = 20) -> list[dict]:
        if not isinstance(limit, int) or isinstance(limit, bool) or limit < 0:
            raise ValueError("limit must be a non-negative integer")
        bounded_limit = min(limit, self._MAX_HISTORY_LIMIT)
        with self._connection() as db:
            rows = db.execute(
                """SELECT target_id, workspace_id, host, source_cwd, cwd,
                          project_root, revision, changed_at
                   FROM target_history WHERE workspace_id = ?
                   ORDER BY revision DESC LIMIT ?""",
                (workspace_id, bounded_limit),
            ).fetchall()
        return [dict(row) for row in rows]

    def register_job(
        self, workspace_id: str, job_id: str, target: WorkspaceTarget
    ) -> None:
        self._validate_target(workspace_id, target)
        if not isinstance(job_id, str) or not job_id:
            raise ValueError("job_id must be a non-empty string")
        with self._transaction() as db:
            workspace = db.execute(
                "SELECT 1 FROM workspaces WHERE workspace_id = ?", (workspace_id,)
            ).fetchone()
            if workspace is None:
                raise ValueError(f"Unknown workspace: {workspace_id}")
            existing = db.execute(
                "SELECT * FROM jobs WHERE workspace_id = ? AND job_id = ?",
                (workspace_id, job_id),
            ).fetchone()
            snapshot = (
                target.target_id, target.host, target.source_cwd,
                target.cwd, target.project_root,
            )
            if existing is not None:
                saved = tuple(existing[key] for key in (
                    "target_id", "host", "source_cwd", "cwd", "project_root"
                ))
                if saved != snapshot:
                    raise ValueError("Job is already registered with a different target")
                return
            db.execute(
                """INSERT INTO jobs
                   (workspace_id, job_id, target_id, host, source_cwd, cwd, project_root)
                   VALUES (?, ?, ?, ?, ?, ?, ?)""",
                (workspace_id, job_id, *snapshot),
            )

    def get_job(self, workspace_id: str, job_id: str) -> WorkspaceTarget:
        with self._connection() as db:
            row = db.execute(
                "SELECT * FROM jobs WHERE workspace_id = ? AND job_id = ?",
                (workspace_id, job_id),
            ).fetchone()
        if row is None:
            raise ValueError(f"Unknown job for workspace: {job_id}")
        return WorkspaceTarget(
            target_id=row["target_id"],
            workspace_id=row["workspace_id"],
            host=row["host"],
            source_cwd=row["source_cwd"],
            cwd=row["cwd"],
            project_root=row["project_root"],
        )

    def record_call(self, workspace_id, tool, argument_bytes, result_bytes, duration_ms, failed, read_key=None):
        with self._transaction() as db:
            repeated = bool(read_key) and db.execute(
                "SELECT 1 FROM call_metrics WHERE workspace_id=? AND read_key=? LIMIT 1",
                (workspace_id, read_key)).fetchone() is not None
            db.execute("""INSERT INTO call_metrics
                (workspace_id,tool,argument_bytes,result_bytes,duration_ms,failed,read_key,repeated_read)
                VALUES (?,?,?,?,?,?,?,?)""", (workspace_id, tool, argument_bytes, result_bytes,
                                               duration_ms, int(failed), read_key, int(repeated)))

    def metrics(self, workspace_id):
        with self._connection() as db:
            row = db.execute("""SELECT COUNT(*) AS recorded_responses,
                COALESCE(SUM(argument_bytes),0) AS validated_argument_bytes,
                COALESCE(SUM(result_bytes),0) AS result_text_bytes,
                COALESCE(SUM(failed),0) AS failed_results,
                COALESCE(SUM(repeated_read),0) AS repeated_output_reads,
                COALESCE(SUM(duration_ms),0) AS total_duration_ms
                FROM call_metrics WHERE workspace_id=?""", (workspace_id,)).fetchone()
        return dict(row)

    def save_test(self, workspace_id, target_id, observation):
        import json
        with self._transaction() as db:
            db.execute("""INSERT INTO last_tests(workspace_id,target_id,observation) VALUES (?,?,?)
                ON CONFLICT(workspace_id,target_id) DO UPDATE SET observation=excluded.observation""",
                (workspace_id, target_id, json.dumps(observation, separators=(",", ":"))))

    def last_test(self, workspace_id, target_id):
        import json
        with self._connection() as db:
            row = db.execute("SELECT observation FROM last_tests WHERE workspace_id=? AND target_id=?",
                             (workspace_id, target_id)).fetchone()
        return json.loads(row[0]) if row else None
