from __future__ import annotations

import logging
import threading
import time
from typing import Any, Optional

from .config import Settings
from .models import AUTO_FIXED_STATUSES, BugCandidate, ProjectConfig
from .state import StateStore, utc_now
from .worker import Worker
from .zentao import list_project_bugs


LOGGER = logging.getLogger("zentao_auto_fixer.poller")


class Poller:
    def __init__(self, settings: Settings, state: StateStore, worker: Worker):
        self.settings = settings
        self.state = state
        self.worker = worker
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None

    def start(self) -> None:
        if self._thread:
            return
        self._thread = threading.Thread(target=self._run, name="auto-fixer-poller", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=5)

    def poll_once(self) -> None:
        projects = [project for project in self.settings.load_projects() if project.enabled]
        for project in projects:
            self._poll_project(project)

    def _run(self) -> None:
        while not self._stop.is_set():
            started = time.time()
            try:
                self.poll_once()
            except Exception:
                LOGGER.exception("Poll cycle failed")
            elapsed = time.time() - started
            wait_seconds = max(1, self.settings.poll_interval_seconds - int(elapsed))
            self._stop.wait(wait_seconds)

    def _poll_project(self, project: ProjectConfig) -> None:
        started_at = utc_now()
        total = 0
        unresolved_count = 0
        candidate_count = 0
        queued = 0
        skipped_existing = 0
        skipped_resolved = 0
        marked_manual = 0
        requeued_failed = 0
        try:
            bugs = list_project_bugs(self.settings.zentao_client_script, project)
            total = len(bugs)
            for bug in bugs:
                if bug.bug_id <= 0:
                    skipped_resolved += 1
                    continue
                existing = self.state.get_run(bug.bug_id)
                unresolved = _is_unresolved(bug)

                if not unresolved:
                    skipped_resolved += 1
                    if existing and existing.status in AUTO_FIXED_STATUSES:
                        self.state.mark_seen_resolved_once(bug.bug_id, bug.status)
                    continue

                unresolved_count += 1
                candidate_count += 1

                if existing:
                    if self.state.should_mark_manual_required(bug.bug_id):
                        self.state.mark_manual_required(
                            bug.bug_id,
                            "Bug was already automatically fixed once and later appeared unresolved again.",
                        )
                        marked_manual += 1
                    elif existing.status == "failed" and self.settings.retry_failed:
                        if self.state.requeue_failed(bug, project):
                            self.worker.enqueue(bug.bug_id)
                            queued += 1
                            requeued_failed += 1
                        else:
                            skipped_existing += 1
                    else:
                        skipped_existing += 1
                    continue

                if queued >= project.max_bugs_per_poll:
                    skipped_existing += 1
                    continue
                if self.state.enqueue_first_run(bug, project):
                    self.state.record_run_event(
                        bug.bug_id,
                        "queued",
                        f"Queued from poller project={project.name} branch={project.target_branch}",
                    )
                    self.worker.enqueue(bug.bug_id)
                    queued += 1
                else:
                    skipped_existing += 1
            self.state.record_poll_run(
                project_name=project.name,
                product_id=project.zentao_product_id,
                started_at=started_at,
                status="ok",
                total_bugs=total,
                unresolved_bugs=unresolved_count,
                candidate_bugs=candidate_count,
                queued_bugs=queued,
                skipped_existing=skipped_existing,
                skipped_resolved=skipped_resolved,
                marked_manual=marked_manual,
                requeued_failed=requeued_failed,
            )
            LOGGER.info(
                "Polled %s: total=%s unresolved=%s queued=%s existing=%s resolved=%s manual=%s",
                project.name,
                total,
                unresolved_count,
                queued,
                skipped_existing,
                skipped_resolved,
                marked_manual,
            )
        except Exception as exc:
            self.state.record_poll_run(
                project_name=project.name,
                product_id=project.zentao_product_id,
                started_at=started_at,
                status="failed",
                total_bugs=total,
                unresolved_bugs=unresolved_count,
                candidate_bugs=candidate_count,
                queued_bugs=queued,
                skipped_existing=skipped_existing,
                skipped_resolved=skipped_resolved,
                marked_manual=marked_manual,
                requeued_failed=requeued_failed,
                error=str(exc),
            )
            raise


def _is_unresolved(bug: BugCandidate) -> bool:
    status = bug.status.lower()
    if status in {"closed", "resolved", "done"}:
        return False
    raw = bug.raw
    if _has_value(raw.get("closedBy")) or _has_value(raw.get("closed_by")):
        return False
    if _has_value(raw.get("resolvedBy")) or _has_value(raw.get("resolved_by")):
        return False
    return True


def _has_value(value: Any) -> bool:
    if value in (None, "", 0, "0"):
        return False
    if isinstance(value, dict):
        return any(_has_value(item) for item in value.values())
    return True
