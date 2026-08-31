from __future__ import annotations

import sqlite3
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

from .models import RETRYABLE_STATUSES, BugCandidate, ProjectConfig, RunRecord
from .zentao import bug_view_url


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
                    opened_by TEXT NOT NULL DEFAULT '',
                    opened_at TEXT NOT NULL DEFAULT '',
                    triage_targets TEXT NOT NULL DEFAULT '',
                    retry_count INTEGER NOT NULL DEFAULT 0,
                    writeback_payload TEXT NOT NULL DEFAULT '',
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
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS daily_counters (
                    counter_name TEXT NOT NULL,
                    day TEXT NOT NULL,
                    value INTEGER NOT NULL DEFAULT 0,
                    PRIMARY KEY (counter_name, day)
                )
                """
            )

    def _ensure_columns(self, conn: sqlite3.Connection) -> None:
        rows = conn.execute("PRAGMA table_info(bug_runs)").fetchall()
        existing = {row["name"] for row in rows}
        additions = {
            "project_name": "TEXT NOT NULL DEFAULT ''",
            "bug_type": "TEXT NOT NULL DEFAULT ''",
            "bug_status": "TEXT NOT NULL DEFAULT ''",
            "commit_hash": "TEXT NOT NULL DEFAULT ''",
            "seen_resolved_once": "INTEGER NOT NULL DEFAULT 0",
            "opened_by": "TEXT NOT NULL DEFAULT ''",
            "opened_at": "TEXT NOT NULL DEFAULT ''",
            "triage_targets": "TEXT NOT NULL DEFAULT ''",
            "retry_count": "INTEGER NOT NULL DEFAULT 0",
            "writeback_payload": "TEXT NOT NULL DEFAULT ''",
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
                        opened_by, opened_at, first_seen_at, updated_at
                    )
                    VALUES (?, ?, 'queued', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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
                        bug.opened_by,
                        bug.opened_at,
                        now,
                        now,
                    ),
                )
            except sqlite3.IntegrityError:
                return False
            return True

    def requeue_retryable(self, bug: BugCandidate, project: ProjectConfig, max_retries: int = 1) -> bool:
        now = utc_now()
        with self._lock, self._connect() as conn:
            row = conn.execute(
                "SELECT status, retry_count FROM bug_runs WHERE bug_id = ?", (bug.bug_id,)
            ).fetchone()
            if not row or row["status"] not in RETRYABLE_STATUSES:
                return False
            if int(row["retry_count"]) >= max_retries:
                conn.execute(
                    """
                    UPDATE bug_runs
                    SET status = 'retry_exhausted', error = ?, updated_at = ?, completed_at = ?
                    WHERE bug_id = ?
                    """,
                    ("Automatic retry limit reached; kept local without ZenTao writeback.", now, now, bug.bug_id),
                )
                return False
            conn.execute(
                """
                UPDATE bug_runs
                SET title = ?, status = 'queued', project_name = ?, product_id = ?,
                    assigned_to = ?, bug_type = ?, bug_status = ?, repo_url = ?,
                    target_branch = ?, opened_by = ?, opened_at = ?, error = '',
                    retry_count = retry_count + 1, event_action = 'automatic_retry',
                    updated_at = ?, completed_at = NULL
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
                    bug.opened_by,
                    bug.opened_at,
                    now,
                    bug.bug_id,
                ),
            )
            return True

    def requeue_skipped_ui(self, bug: BugCandidate, project: ProjectConfig) -> bool:
        """Requeue a UI-tagged bug after processUiBugs is explicitly enabled."""
        now = utc_now()
        with self._lock, self._connect() as conn:
            row = conn.execute("SELECT status FROM bug_runs WHERE bug_id = ?", (bug.bug_id,)).fetchone()
            if not row or row["status"] != "skipped_ui":
                return False
            conn.execute(
                """
                UPDATE bug_runs
                SET title = ?, status = 'queued', project_name = ?, product_id = ?,
                    assigned_to = ?, bug_type = ?, bug_status = ?, repo_url = ?,
                    target_branch = ?, opened_by = ?, opened_at = ?, error = '', handled_once = 0,
                    updated_at = ?, completed_at = NULL
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
                    bug.opened_by,
                    bug.opened_at,
                    now,
                    bug.bug_id,
                ),
            )
            return True

    def manual_requeue(self, bug: BugCandidate, project: ProjectConfig) -> bool:
        """One owner-approved retry without creating a permanent retry loop."""
        now = utc_now()
        with self._lock, self._connect() as conn:
            row = conn.execute("SELECT status FROM bug_runs WHERE bug_id = ?", (bug.bug_id,)).fetchone()
            if row and row["status"] not in {"failed", "skipped_stale"}:
                return False
            if not row:
                conn.execute(
                    """
                    INSERT INTO bug_runs (
                        bug_id, title, status, project_name, event_action, product_id,
                        assigned_to, bug_type, bug_status, repo_url, target_branch,
                        opened_by, opened_at, first_seen_at, updated_at
                    )
                    VALUES (?, ?, 'queued', ?, 'manual_retry', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        bug.bug_id,
                        bug.title,
                        project.name,
                        bug.product_id,
                        bug.assigned_to,
                        bug.bug_type,
                        bug.status,
                        project.repo_url,
                        project.target_branch,
                        bug.opened_by,
                        bug.opened_at,
                        now,
                        now,
                    ),
                )
                return True
            conn.execute(
                """
                UPDATE bug_runs
                SET title = ?, status = 'queued', project_name = ?, event_action = 'manual_retry',
                    product_id = ?, assigned_to = ?, bug_type = ?, bug_status = ?, repo_url = ?,
                    target_branch = ?, opened_by = ?, opened_at = ?, triage_targets = '',
                    commit_hash = '', error = '', writeback_payload = '', retry_count = 0,
                    handled_once = 0, reactivated_after_auto_fix = 0, seen_resolved_once = 0,
                    updated_at = ?, completed_at = NULL
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
                    bug.opened_by,
                    bug.opened_at,
                    now,
                    bug.bug_id,
                ),
            )
            return True

    def reset_running_to_queued(self) -> List[int]:
        """A restart kills whatever batch was in flight; those bugs must not stay stuck in 'running'."""
        with self._lock, self._connect() as conn:
            rows = conn.execute("SELECT bug_id FROM bug_runs WHERE status = 'running'").fetchall()
            bug_ids = [int(row["bug_id"]) for row in rows]
            if bug_ids:
                conn.execute(
                    "UPDATE bug_runs SET status = 'queued', error = ?, updated_at = ? WHERE status = 'running'",
                    ("Interrupted by a service restart; queued again.", utc_now()),
                )
        return bug_ids

    def get_run(self, bug_id: int) -> Optional[RunRecord]:
        with self._lock, self._connect() as conn:
            row = conn.execute("SELECT * FROM bug_runs WHERE bug_id = ?", (bug_id,)).fetchone()
        return _row_to_run(row) if row else None

    def claim_queued_batch(self, leader_bug_id: int, limit: int = 0) -> List[RunRecord]:
        now = utc_now()
        with self._lock, self._connect() as conn:
            leader = conn.execute(
                "SELECT * FROM bug_runs WHERE bug_id = ?",
                (leader_bug_id,),
            ).fetchone()
            if not leader or leader["status"] != "queued":
                return []
            rows = conn.execute(
                """
                SELECT * FROM bug_runs
                WHERE status = 'queued'
                  AND project_name = ?
                  AND repo_url = ?
                  AND target_branch = ?
                ORDER BY first_seen_at ASC
                """,
                (leader["project_name"], leader["repo_url"], leader["target_branch"]),
            ).fetchall()
            if limit > 0:
                rows = _rows_with_leader_first(rows, leader_bug_id, limit)
            bug_ids = [int(row["bug_id"]) for row in rows]
            if not bug_ids:
                return []
            placeholders = ",".join("?" for _bug_id in bug_ids)
            conn.execute(
                f"""
                UPDATE bug_runs
                SET status = 'running',
                    handled_once = 1,
                    error = '',
                    updated_at = ?
                WHERE bug_id IN ({placeholders})
                """,
                [now, *bug_ids],
            )
        return [_row_to_run(row) for row in rows]

    def list_runs(self, limit: int = 100) -> List[Dict[str, Any]]:
        with self._lock, self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM bug_runs ORDER BY updated_at DESC LIMIT ?",
                (limit,),
            ).fetchall()
        return [{**dict(row), "url": bug_view_url(row["bug_id"])} for row in rows]

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
                "SELECT COUNT(*) AS count FROM bug_runs WHERE status IN ('queued', 'writeback_queued')"
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
        unable_to_fix = by_status.get("unable_to_fix", 0)
        retry_exhausted = by_status.get("retry_exhausted", 0)
        writeback_failed = by_status.get("writeback_failed", 0)
        completed = sum(by_status.values())
        return {
            "completed": completed,
            "auto_fixed": pushed,
            "pushed": pushed,
            "no_changes": no_changes,
            "failed": failed,
            "sync_conflict": sync_conflict,
            "manual_required": manual_required,
            "unable_to_fix": unable_to_fix,
            "retry_exhausted": retry_exhausted,
            "writeback_failed": writeback_failed,
            "queued": int(queued["count"]) if queued else 0,
            "running": int(running["count"]) if running else 0,
        }

    def current_problem_count(self) -> int:
        with self._lock, self._connect() as conn:
            row = conn.execute(
                """
                SELECT COUNT(*) AS count FROM bug_runs
                WHERE status IN (
                    'failed', 'sync_conflict', 'retry_exhausted',
                    'writeback_failed', 'writeback_exhausted'
                )
                """
            ).fetchone()
        return int(row["count"]) if row else 0

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

    def record_run_events(self, bug_ids: List[int], event: str, message: str = "") -> None:
        now = utc_now()
        with self._lock, self._connect() as conn:
            conn.executemany(
                """
                INSERT INTO run_events (bug_id, event, message, created_at)
                VALUES (?, ?, ?, ?)
                """,
                [(bug_id, event, message, now) for bug_id in bug_ids],
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

    def claim_daily_counter(self, counter_name: str, day: str, limit: int) -> bool:
        """Atomically consume one persisted daily allowance across threads and restarts."""
        with self._lock, self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                "SELECT value FROM daily_counters WHERE counter_name = ? AND day = ?",
                (counter_name, day),
            ).fetchone()
            current = int(row["value"]) if row else 0
            if current >= limit:
                conn.rollback()
                return False
            conn.execute(
                """
                INSERT INTO daily_counters (counter_name, day, value)
                VALUES (?, ?, 1)
                ON CONFLICT(counter_name, day)
                DO UPDATE SET value = daily_counters.value + 1
                """,
                (counter_name, day),
            )
            conn.commit()
            return True

    def daily_counter_value(self, counter_name: str, day: str) -> int:
        with self._lock, self._connect() as conn:
            row = conn.execute(
                "SELECT value FROM daily_counters WHERE counter_name = ? AND day = ?",
                (counter_name, day),
            ).fetchone()
        return int(row["value"]) if row else 0

    def set_daily_counter(self, counter_name: str, day: str, value: int) -> None:
        with self._lock, self._connect() as conn:
            conn.execute(
                """
                INSERT INTO daily_counters (counter_name, day, value)
                VALUES (?, ?, ?)
                ON CONFLICT(counter_name, day) DO UPDATE SET value = excluded.value
                """,
                (counter_name, day, max(0, value)),
            )

    def increment_daily_counter(self, counter_name: str, day: str) -> int:
        with self._lock, self._connect() as conn:
            conn.execute(
                """
                INSERT INTO daily_counters (counter_name, day, value)
                VALUES (?, ?, 1)
                ON CONFLICT(counter_name, day)
                DO UPDATE SET value = daily_counters.value + 1
                """,
                (counter_name, day),
            )
            row = conn.execute(
                "SELECT value FROM daily_counters WHERE counter_name = ? AND day = ?",
                (counter_name, day),
            ).fetchone()
        return int(row["value"])

    def queued_bug_ids(self) -> List[int]:
        with self._lock, self._connect() as conn:
            rows = conn.execute(
                """
                SELECT bug_id FROM bug_runs
                WHERE status IN ('queued', 'writeback_queued')
                ORDER BY first_seen_at ASC
                """
            ).fetchall()
        return [int(row["bug_id"]) for row in rows]

    def queue_writeback_retry(self, bug_id: int, max_retries: int = 1) -> bool:
        now = utc_now()
        with self._lock, self._connect() as conn:
            row = conn.execute(
                "SELECT status, retry_count, writeback_payload FROM bug_runs WHERE bug_id = ?",
                (bug_id,),
            ).fetchone()
            if not row or row["status"] != "writeback_failed" or not row["writeback_payload"]:
                return False
            if int(row["retry_count"]) >= max_retries:
                conn.execute(
                    """
                    UPDATE bug_runs
                    SET status = 'writeback_exhausted', updated_at = ?, completed_at = ?
                    WHERE bug_id = ?
                    """,
                    (now, now, bug_id),
                )
                return False
            conn.execute(
                """
                UPDATE bug_runs
                SET status = 'writeback_queued', retry_count = retry_count + 1,
                    error = '', updated_at = ?, completed_at = NULL
                WHERE bug_id = ?
                """,
                (now, bug_id),
            )
            return True

    def set_writeback_payload(self, bug_id: int, payload: str) -> None:
        with self._lock, self._connect() as conn:
            conn.execute(
                "UPDATE bug_runs SET writeback_payload = ?, updated_at = ? WHERE bug_id = ?",
                (payload, utc_now(), bug_id),
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

    def mark_unable_to_fix(self, bug_id: int, reason: str) -> None:
        with self._lock, self._connect() as conn:
            conn.execute(
                """
                UPDATE bug_runs
                SET status = 'unable_to_fix', error = ?, updated_at = ?, completed_at = ?
                WHERE bug_id = ?
                """,
                (reason, utc_now(), utc_now(), bug_id),
            )

    def set_triage_targets(self, bug_id: int, targets: str) -> None:
        with self._lock, self._connect() as conn:
            conn.execute(
                "UPDATE bug_runs SET triage_targets = ?, updated_at = ? WHERE bug_id = ?",
                (targets, utc_now(), bug_id),
            )

    def record_already_handled_in_zentao(self, bug: BugCandidate, project: ProjectConfig) -> bool:
        """Remember a bug that already carries an AI comment in ZenTao, so later polls skip it cheaply."""
        now = utc_now()
        with self._lock, self._connect() as conn:
            try:
                conn.execute(
                    """
                    INSERT INTO bug_runs (
                        bug_id, title, status, project_name, event_action, product_id,
                        assigned_to, bug_type, bug_status, repo_url, target_branch,
                        opened_by, opened_at, handled_once, error, first_seen_at, updated_at, completed_at
                    )
                    VALUES (?, ?, 'handled_in_zentao', ?, 'poll', ?, ?, ?, ?, ?, ?, ?, ?, 1, ?, ?, ?, ?)
                    """,
                    (
                        bug.bug_id,
                        bug.title,
                        project.name,
                        bug.product_id,
                        bug.assigned_to,
                        bug.bug_type,
                        bug.status,
                        project.repo_url,
                        project.target_branch,
                        bug.opened_by,
                        bug.opened_at,
                        "ZenTao already carries an AI comment for this bug; leaving it to a human.",
                        now,
                        now,
                        now,
                    ),
                )
            except sqlite3.IntegrityError:
                return False
            return True

    def mark_handled_in_zentao(self, bug_id: int) -> None:
        with self._lock, self._connect() as conn:
            conn.execute(
                """
                UPDATE bug_runs
                SET status = 'handled_in_zentao', error = ?, updated_at = ?, completed_at = ?
                WHERE bug_id = ?
                """,
                ("ZenTao already carries a successful or unable-to-fix AI marker.", utc_now(), utc_now(), bug_id),
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

    def update_statuses(
        self,
        bug_ids: List[int],
        status: str,
        *,
        error: str = "",
        commit_hash: str = "",
        handled_once: Optional[bool] = None,
        completed: bool = False,
    ) -> None:
        if not bug_ids:
            return
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
        placeholders = ",".join("?" for _bug_id in bug_ids)
        values.extend(bug_ids)
        with self._lock, self._connect() as conn:
            conn.execute(
                f"UPDATE bug_runs SET {', '.join(fields)} WHERE bug_id IN ({placeholders})",
                values,
            )


def _rows_with_leader_first(rows: List[sqlite3.Row], leader_bug_id: int, limit: int) -> List[sqlite3.Row]:
    """Trim a batch to `limit`, always keeping the leader so its worker never claims an empty batch."""
    if len(rows) <= limit:
        return rows
    trimmed = rows[:limit]
    if any(int(row["bug_id"]) == leader_bug_id for row in trimmed):
        return trimmed
    leader = next(row for row in rows if int(row["bug_id"]) == leader_bug_id)
    return [leader, *trimmed[: limit - 1]]


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
        event_action=row["event_action"],
        opened_by=row["opened_by"],
        triage_targets=row["triage_targets"],
        retry_count=int(row["retry_count"]),
        writeback_payload=row["writeback_payload"],
    )
