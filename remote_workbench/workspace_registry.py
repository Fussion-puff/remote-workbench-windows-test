"""Persistent local identities and target history for session workspaces."""
from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
import os
from pathlib import Path
import sqlite3
import json
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


@dataclass(frozen=True, slots=True)
class JobOperation:
    """Persistent idempotency mapping for a requested background job."""

    workspace_id: str
    idempotency_key: str
    job_id: str
    command_fingerprint: str
    state: str
    target: WorkspaceTarget
    created_at: str
    updated_at: str


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
                    """CREATE TABLE IF NOT EXISTS job_operations (
                           workspace_id TEXT NOT NULL REFERENCES workspaces(workspace_id)
                               ON DELETE CASCADE,
                           idempotency_key TEXT NOT NULL,
                           job_id TEXT NOT NULL,
                           command_fingerprint TEXT NOT NULL,
                           target_id TEXT NOT NULL,
                           host TEXT NOT NULL,
                           source_cwd TEXT NOT NULL,
                           cwd TEXT,
                           project_root TEXT,
                           state TEXT NOT NULL,
                           created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                           updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                           PRIMARY KEY(workspace_id, idempotency_key),
                           UNIQUE(workspace_id, job_id)
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
                db.execute("""CREATE TABLE IF NOT EXISTS workspace_notes (
                    workspace_id TEXT PRIMARY KEY REFERENCES workspaces(workspace_id) ON DELETE CASCADE,
                    note TEXT NOT NULL,
                    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
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

    def clear_binding(self, workspace_id: str, expected_revision: int) -> WorkspaceState:
        """Atomically return a thread to its native-local default.

        Only the active binding is cleared. Immutable targets, target history,
        and job origins remain available for later resume and job control.
        """
        if isinstance(expected_revision, bool) or not isinstance(expected_revision, int):
            raise ValueError("expected_revision must be an integer")
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
            if current.host is None:
                return current
            db.execute(
                """UPDATE workspaces
                   SET host = NULL, source_cwd = NULL, cwd = NULL,
                       project_root = NULL, target_id = NULL,
                       revision = revision + 1, updated_at = CURRENT_TIMESTAMP
                   WHERE workspace_id = ?""",
                (workspace_id,),
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

    def list_jobs(self, workspace_id: str, limit: int | None = None, offset: int = 0) -> list[dict]:
        """List job handles in newest-registration order with optional pagination.

        The no-argument form remains compatible and returns all jobs for the
        workspace in job-id order. Paginated requests use newest registration
        first; rowid preserves insertion order without a schema migration.
        Every query remains workspace-scoped.
        """
        if limit is not None and (isinstance(limit, bool) or not isinstance(limit, int) or limit < 0):
            raise ValueError("limit must be a non-negative integer or None")
        if isinstance(offset, bool) or not isinstance(offset, int) or offset < 0:
            raise ValueError("offset must be a non-negative integer")
        ordering = "rowid DESC" if limit is not None or offset else "job_id"
        query = f"""SELECT job_id, target_id, workspace_id, host, source_cwd, cwd, project_root
                    FROM jobs WHERE workspace_id = ? ORDER BY {ordering}"""
        params: tuple = (workspace_id,)
        if limit is None:
            if offset:
                query += " LIMIT -1 OFFSET ?"
                params += (offset,)
        else:
            query += " LIMIT ? OFFSET ?"
            params += (limit, offset)
        with self._connection() as db:
            rows = db.execute(query, params).fetchall()
        return [{
            "job_id": row["job_id"],
            "target": WorkspaceTarget(
                target_id=row["target_id"], workspace_id=row["workspace_id"],
                host=row["host"], source_cwd=row["source_cwd"],
                cwd=row["cwd"], project_root=row["project_root"],
            ),
        } for row in rows]

    @staticmethod
    def _job_operation(row: sqlite3.Row) -> JobOperation:
        return JobOperation(
            workspace_id=row["workspace_id"],
            idempotency_key=row["idempotency_key"],
            job_id=row["job_id"],
            command_fingerprint=row["command_fingerprint"],
            state=row["state"],
            target=WorkspaceTarget(
                target_id=row["target_id"], workspace_id=row["workspace_id"],
                host=row["host"], source_cwd=row["source_cwd"],
                cwd=row["cwd"], project_root=row["project_root"],
            ),
            created_at=row["created_at"],
            updated_at=row["updated_at"],
        )

    def reserve_job_operation(
        self, workspace_id: str, idempotency_key: str, target: WorkspaceTarget,
        job_id: str, command_fingerprint: str,
    ) -> tuple[JobOperation, bool]:
        """Atomically bind a key to one target/job before any remote submission.

        The returned boolean is true only for the first reservation. Existing
        keys with different inputs fail closed; callers must never submit those
        requests again under a conflicting key.
        """
        self._validate_target(workspace_id, target)
        if not isinstance(idempotency_key, str) or not idempotency_key:
            raise ValueError("idempotency_key must be non-empty text")
        if not isinstance(job_id, str) or not job_id:
            raise ValueError("job_id must be non-empty text")
        if not isinstance(command_fingerprint, str) or not command_fingerprint:
            raise ValueError("command_fingerprint must be non-empty text")
        with self._transaction() as db:
            workspace = db.execute(
                "SELECT 1 FROM workspaces WHERE workspace_id = ?", (workspace_id,)
            ).fetchone()
            if workspace is None:
                raise ValueError(f"Unknown workspace: {workspace_id}")
            existing = db.execute(
                "SELECT * FROM job_operations WHERE workspace_id = ? AND idempotency_key = ?",
                (workspace_id, idempotency_key),
            ).fetchone()
            target_fields = (target.target_id, target.host, target.source_cwd,
                             target.cwd, target.project_root)
            if existing is not None:
                saved_target = tuple(existing[name] for name in (
                    "target_id", "host", "source_cwd", "cwd", "project_root"
                ))
                if (saved_target != target_fields or
                        existing["command_fingerprint"] != command_fingerprint):
                    raise ValueError("idempotency_key is already bound to different job parameters")
                return self._job_operation(existing), False

            collision = db.execute(
                "SELECT 1 FROM jobs WHERE workspace_id = ? AND job_id = ?",
                (workspace_id, job_id),
            ).fetchone()
            if collision is not None:
                raise ValueError("job_id is already registered; use a new idempotency_key")
            db.execute(
                """INSERT INTO jobs
                   (workspace_id, job_id, target_id, host, source_cwd, cwd, project_root)
                   VALUES (?, ?, ?, ?, ?, ?, ?)""",
                (workspace_id, job_id, *target_fields),
            )
            db.execute(
                """INSERT INTO job_operations
                   (workspace_id, idempotency_key, job_id, command_fingerprint,
                    target_id, host, source_cwd, cwd, project_root, state)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'reserved')""",
                (workspace_id, idempotency_key, job_id, command_fingerprint, *target_fields),
            )
            row = db.execute(
                "SELECT * FROM job_operations WHERE workspace_id = ? AND idempotency_key = ?",
                (workspace_id, idempotency_key),
            ).fetchone()
            return self._job_operation(row), True

    def get_job_operation(self, workspace_id: str, idempotency_key: str) -> JobOperation:
        with self._connection() as db:
            row = db.execute(
                "SELECT * FROM job_operations WHERE workspace_id = ? AND idempotency_key = ?",
                (workspace_id, idempotency_key),
            ).fetchone()
        if row is None:
            raise ValueError("Unknown idempotency_key for this workspace")
        return self._job_operation(row)

    def update_job_operation(self, workspace_id: str, idempotency_key: str, state: str) -> JobOperation:
        if state not in {"reserved", "submitted", "rejected", "unknown"}:
            raise ValueError("Invalid job operation state")
        with self._transaction() as db:
            cursor = db.execute(
                """UPDATE job_operations SET state = ?, updated_at = CURRENT_TIMESTAMP
                   WHERE workspace_id = ? AND idempotency_key = ?""",
                (state, workspace_id, idempotency_key),
            )
            if cursor.rowcount != 1:
                raise ValueError("Unknown idempotency_key for this workspace")
            row = db.execute(
                "SELECT * FROM job_operations WHERE workspace_id = ? AND idempotency_key = ?",
                (workspace_id, idempotency_key),
            ).fetchone()
            return self._job_operation(row)

    def reopen_rejected_job_operation(self, workspace_id: str, idempotency_key: str) -> bool:
        """Permit an explicit retry only after a tool confirmed no execution."""
        with self._transaction() as db:
            cursor = db.execute(
                """UPDATE job_operations SET state = 'reserved', updated_at = CURRENT_TIMESTAMP
                   WHERE workspace_id = ? AND idempotency_key = ? AND state = 'rejected'""",
                (workspace_id, idempotency_key),
            )
            return cursor.rowcount == 1

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
        with self._connection() as db:
            row = db.execute("SELECT observation FROM last_tests WHERE workspace_id=? AND target_id=?",
                             (workspace_id, target_id)).fetchone()
        return json.loads(row[0]) if row else None

    def workspace_note(self, workspace_id: str, updates: dict | None = None) -> dict | None:
        """Read or merge a small local recovery note scoped to one workspace."""
        if updates is None:
            with self._connection() as db:
                row = db.execute(
                    "SELECT note, updated_at FROM workspace_notes WHERE workspace_id = ?",
                    (workspace_id,),
                ).fetchone()
            if row is None:
                return None
            return {**json.loads(row["note"]), "updated_at": row["updated_at"]}

        allowed = {"goal", "constraints", "decisions", "next_step"}
        if not isinstance(updates, dict) or set(updates) - allowed:
            raise ValueError("workspace note accepts goal, constraints, decisions and next_step")
        if any(not isinstance(value, str) for value in updates.values()):
            raise ValueError("workspace note values must be text")
        with self._transaction() as db:
            workspace = db.execute(
                "SELECT 1 FROM workspaces WHERE workspace_id = ?", (workspace_id,)
            ).fetchone()
            if workspace is None:
                raise ValueError(f"Unknown workspace: {workspace_id}")
            row = db.execute(
                "SELECT note FROM workspace_notes WHERE workspace_id = ?", (workspace_id,)
            ).fetchone()
            note = json.loads(row["note"]) if row else {}
            note.update(updates)
            db.execute(
                """INSERT INTO workspace_notes(workspace_id, note) VALUES (?, ?)
                   ON CONFLICT(workspace_id) DO UPDATE SET note = excluded.note,
                       updated_at = CURRENT_TIMESTAMP""",
                (workspace_id, json.dumps(note, ensure_ascii=False, separators=(",", ":"))),
            )
            saved = db.execute(
                "SELECT note, updated_at FROM workspace_notes WHERE workspace_id = ?",
                (workspace_id,),
            ).fetchone()
            return {**json.loads(saved["note"]), "updated_at": saved["updated_at"]}
