from __future__ import annotations

import sqlite3
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

from .models import AUTO_FIXED_STATUSES, BugCandidate, ProjectConfig, RunRecord


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class StateStore:
    def __init__(self, path: Path):
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._init_db()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(str(self.path))
        conn.row_factory = sqlite3.Row
        return conn

    def _init_db(self) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS bug_runs (
                    bug_id INTEGER PRIMARY KEY,
                    title TEXT NOT NULL,
                    status TEXT NOT NULL,
                    project_name TEXT NOT NULL DEFAULT '',
                    event_action TEXT NOT NULL DEFAULT '',
                    product_id INTEGER,
                    assigned_to TEXT NOT NULL DEFAULT '',
                    bug_type TEXT NOT NULL DEFAULT '',
                    bug_status TEXT NOT NULL DEFAULT '',
                    repo_url TEXT NOT NULL DEFAULT '',
                    target_branch TEXT NOT NULL DEFAULT '',
                    commit_hash TEXT NOT NULL DEFAULT '',
                    error TEXT NOT NULL DEFAULT '',
                    handled_once INTEGER NOT NULL DEFAULT 0,
                    reactivated_after_auto_fix INTEGER NOT NULL DEFAULT 0,
                    seen_resolved_once INTEGER NOT NULL DEFAULT 0,
                    first_seen_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    completed_at TEXT
                )
                """
            )
            self._ensure_columns(conn)
            conn.execute("CREATE INDEX IF NOT EXISTS idx_bug_runs_status ON bug_runs(status)")
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS poll_runs (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    project_name TEXT NOT NULL,
                    product_id INTEGER NOT NULL,
                    started_at TEXT NOT NULL,
                    finished_at TEXT NOT NULL,
                    status TEXT NOT NULL,
                    total_bugs INTEGER NOT NULL DEFAULT 0,
                    unresolved_bugs INTEGER NOT NULL DEFAULT 0,
                    candidate_bugs INTEGER NOT NULL DEFAULT 0,
                    queued_bugs INTEGER NOT NULL DEFAULT 0,
                    skipped_existing INTEGER NOT NULL DEFAULT 0,
                    skipped_resolved INTEGER NOT NULL DEFAULT 0,
                    marked_manual INTEGER NOT NULL DEFAULT 0,
                    requeued_failed INTEGER NOT NULL DEFAULT 0,
                    error TEXT NOT NULL DEFAULT ''
                )
                """
            )
            conn.execute("CREATE INDEX IF NOT EXISTS idx_poll_runs_started ON poll_runs(started_at)")
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS run_events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    bug_id INTEGER NOT NULL,
                    event TEXT NOT NULL,
                    message TEXT NOT NULL DEFAULT '',
                    created_at TEXT NOT NULL
                )
                """
            )
            conn.execute("CREATE INDEX IF NOT EXISTS idx_run_events_bug ON run_events(bug_id, id)")

    def _ensure_columns(self, conn: sqlite3.Connection) -> None:
        rows = conn.execute("PRAGMA table_info(bug_runs)").fetchall()
        existing = {row["name"] for row in rows}
        additions = {
            "project_name": "TEXT NOT NULL DEFAULT ''",
            "bug_type": "TEXT NOT NULL DEFAULT ''",
            "bug_status": "TEXT NOT NULL DEFAULT ''",
            "commit_hash": "TEXT NOT NULL DEFAULT ''",
            "seen_resolved_once": "INTEGER NOT NULL DEFAULT 0",
        }
        for name, definition in additions.items():
            if name not in existing:
                conn.execute(f"ALTER TABLE bug_runs ADD COLUMN {name} {definition}")

    def enqueue_first_run(self, bug: BugCandidate, project: ProjectConfig) -> bool:
        now = utc_now()
        with self._lock, self._connect() as conn:
            try:
                conn.execute(
                    """
                    INSERT INTO bug_runs (
                        bug_id, title, status, project_name, event_action, product_id,
                        assigned_to, bug_type, bug_status, repo_url, target_branch,
                        first_seen_at, updated_at
                    )
                    VALUES (?, ?, 'queued', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        bug.bug_id,
                        bug.title,
                        project.name,
                        "poll",
                        bug.product_id,
                        bug.assigned_to,
                        bug.bug_type,
                        bug.status,
                        project.repo_url,
                        project.target_branch,
                        now,
                        now,
                    ),
                )
            except sqlite3.IntegrityError:
                return False
            return True

    def requeue_failed(self, bug: BugCandidate, project: ProjectConfig) -> bool:
        now = utc_now()
        with self._lock, self._connect() as conn:
            row = conn.execute("SELECT status FROM bug_runs WHERE bug_id = ?", (bug.bug_id,)).fetchone()
            if not row or row["status"] != "failed":
                return False
            conn.execute(
                """
                UPDATE bug_runs
                SET title = ?, status = 'queued', project_name = ?, product_id = ?,
                    assigned_to = ?, bug_type = ?, bug_status = ?, repo_url = ?,
                    target_branch = ?, error = '', updated_at = ?, completed_at = NULL
                WHERE bug_id = ?
                """,
                (
                    bug.title,
                    project.name,
                    bug.product_id,
                    bug.assigned_to,
                    bug.bug_type,
                    bug.status,
                    project.repo_url,
                    project.target_branch,
                    now,
                    bug.bug_id,
                ),
            )
            return True

    def get_run(self, bug_id: int) -> Optional[RunRecord]:
        with self._lock, self._connect() as conn:
            row = conn.execute("SELECT * FROM bug_runs WHERE bug_id = ?", (bug_id,)).fetchone()
        return _row_to_run(row) if row else None

    def list_runs(self, limit: int = 100) -> List[Dict[str, Any]]:
        with self._lock, self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM bug_runs ORDER BY updated_at DESC LIMIT ?",
                (limit,),
            ).fetchall()
        return [dict(row) for row in rows]

    def run_summary_since(self, since: str) -> Dict[str, int]:
        with self._lock, self._connect() as conn:
            status_rows = conn.execute(
                """
                SELECT status, COUNT(*) AS count
                FROM bug_runs
                WHERE completed_at IS NOT NULL AND completed_at >= ?
                GROUP BY status
                """,
                (since,),
            ).fetchall()
            queued = conn.execute(
                "SELECT COUNT(*) AS count FROM bug_runs WHERE status = 'queued'"
            ).fetchone()
            running = conn.execute(
                "SELECT COUNT(*) AS count FROM bug_runs WHERE status = 'running'"
            ).fetchone()

        by_status = {str(row["status"]): int(row["count"]) for row in status_rows}
        pushed = by_status.get("pushed", 0)
        no_changes = by_status.get("no_changes", 0)
        failed = by_status.get("failed", 0)
        sync_conflict = by_status.get("sync_conflict", 0)
        manual_required = by_status.get("manual_required", 0)
        completed = sum(by_status.values())
        return {
            "completed": completed,
            "auto_fixed": pushed + no_changes,
            "pushed": pushed,
            "no_changes": no_changes,
            "failed": failed,
            "sync_conflict": sync_conflict,
            "manual_required": manual_required,
            "queued": int(queued["count"]) if queued else 0,
            "running": int(running["count"]) if running else 0,
        }

    def record_poll_run(
        self,
        *,
        project_name: str,
        product_id: int,
        started_at: str,
        status: str,
        total_bugs: int = 0,
        unresolved_bugs: int = 0,
        candidate_bugs: int = 0,
        queued_bugs: int = 0,
        skipped_existing: int = 0,
        skipped_resolved: int = 0,
        marked_manual: int = 0,
        requeued_failed: int = 0,
        error: str = "",
    ) -> None:
        with self._lock, self._connect() as conn:
            conn.execute(
                """
                INSERT INTO poll_runs (
                    project_name, product_id, started_at, finished_at, status,
                    total_bugs, unresolved_bugs, candidate_bugs, queued_bugs,
                    skipped_existing, skipped_resolved, marked_manual,
                    requeued_failed, error
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    project_name,
                    product_id,
                    started_at,
                    utc_now(),
                    status,
                    total_bugs,
                    unresolved_bugs,
                    candidate_bugs,
                    queued_bugs,
                    skipped_existing,
                    skipped_resolved,
                    marked_manual,
                    requeued_failed,
                    error,
                ),
            )

    def list_poll_runs(self, limit: int = 100, project_name: Optional[str] = None) -> List[Dict[str, Any]]:
        with self._lock, self._connect() as conn:
            if project_name:
                rows = conn.execute(
                    """
                    SELECT * FROM poll_runs
                    WHERE project_name = ?
                    ORDER BY started_at DESC
                    LIMIT ?
                    """,
                    (project_name, limit),
                ).fetchall()
            else:
                rows = conn.execute(
                    "SELECT * FROM poll_runs ORDER BY started_at DESC LIMIT ?",
                    (limit,),
                ).fetchall()
        return [dict(row) for row in rows]

    def record_run_event(self, bug_id: int, event: str, message: str = "") -> None:
        with self._lock, self._connect() as conn:
            conn.execute(
                """
                INSERT INTO run_events (bug_id, event, message, created_at)
                VALUES (?, ?, ?, ?)
                """,
                (bug_id, event, message, utc_now()),
            )

    def list_run_events(self, bug_id: int, limit: int = 200) -> List[Dict[str, Any]]:
        with self._lock, self._connect() as conn:
            rows = conn.execute(
                """
                SELECT * FROM run_events
                WHERE bug_id = ?
                ORDER BY id ASC
                LIMIT ?
                """,
                (bug_id, limit),
            ).fetchall()
        return [dict(row) for row in rows]

    def queued_bug_ids(self) -> List[int]:
        with self._lock, self._connect() as conn:
            rows = conn.execute(
                "SELECT bug_id FROM bug_runs WHERE status = 'queued' ORDER BY first_seen_at ASC"
            ).fetchall()
        return [int(row["bug_id"]) for row in rows]

    def should_mark_manual_required(self, bug_id: int) -> bool:
        with self._lock, self._connect() as conn:
            row = conn.execute(
                "SELECT status, handled_once, seen_resolved_once FROM bug_runs WHERE bug_id = ?",
                (bug_id,),
            ).fetchone()
        if not row:
            return False
        return (
            bool(row["handled_once"])
            and row["status"] in AUTO_FIXED_STATUSES
            and bool(row["seen_resolved_once"])
        )

    def mark_seen_resolved_once(self, bug_id: int, bug_status: str) -> None:
        with self._lock, self._connect() as conn:
            conn.execute(
                """
                UPDATE bug_runs
                SET seen_resolved_once = 1, bug_status = ?, updated_at = ?
                WHERE bug_id = ?
                """,
                (bug_status, utc_now(), bug_id),
            )

    def mark_manual_required(self, bug_id: int, reason: str) -> None:
        with self._lock, self._connect() as conn:
            conn.execute(
                """
                UPDATE bug_runs
                SET status = 'manual_required',
                    reactivated_after_auto_fix = 1,
                    error = ?,
                    updated_at = ?,
                    completed_at = COALESCE(completed_at, ?)
                WHERE bug_id = ?
                """,
                (reason, utc_now(), utc_now(), bug_id),
            )

    def update_commit(self, bug_id: int, commit_hash: str) -> None:
        with self._lock, self._connect() as conn:
            conn.execute(
                "UPDATE bug_runs SET commit_hash = ?, updated_at = ? WHERE bug_id = ?",
                (commit_hash, utc_now(), bug_id),
            )

    def update_status(
        self,
        bug_id: int,
        status: str,
        *,
        error: str = "",
        commit_hash: str = "",
        handled_once: Optional[bool] = None,
        completed: bool = False,
    ) -> None:
        fields = ["status = ?", "error = ?", "updated_at = ?"]
        values: List[Any] = [status, error, utc_now()]
        if commit_hash:
            fields.append("commit_hash = ?")
            values.append(commit_hash)
        if handled_once is not None:
            fields.append("handled_once = ?")
            values.append(1 if handled_once else 0)
        if completed:
            fields.append("completed_at = ?")
            values.append(utc_now())
        values.append(bug_id)
        with self._lock, self._connect() as conn:
            conn.execute(f"UPDATE bug_runs SET {', '.join(fields)} WHERE bug_id = ?", values)


def _row_to_run(row: sqlite3.Row) -> RunRecord:
    return RunRecord(
        bug_id=int(row["bug_id"]),
        title=row["title"],
        status=row["status"],
        project_name=row["project_name"],
        target_branch=row["target_branch"],
        repo_url=row["repo_url"],
        product_id=row["product_id"],
        commit_hash=row["commit_hash"],
        error=row["error"],
        handled_once=bool(row["handled_once"]),
    )
