from __future__ import annotations

import logging
import threading
import time
from typing import Any, Optional

from .config import Settings
from .models import (
    AUTO_FIXED_STATUSES,
    RETRYABLE_STATUSES,
    TERMINAL_STATUSES,
    BugCandidate,
    ProjectConfig,
    has_ui_tag,
    platforms_of,
)
from .state import StateStore, utc_now
from .worker import Worker
from .zentao import ZenTaoPollError, bug_has_ai_comment, list_project_bugs


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

    def _already_handled_in_zentao(self, bug: BugCandidate, project: ProjectConfig) -> str:
        """A ZenTao comment from the AI means this bug was already handled once; leave it to a human."""
        try:
            handled = bug_has_ai_comment(self.settings.zentao_client_script, bug.bug_id)
        except ZenTaoPollError as exc:
            LOGGER.warning("Could not read bug #%s history, skipping this round: %s", bug.bug_id, exc)
            return "unknown"
        if not handled:
            return "fresh"
        if self.state.record_already_handled_in_zentao(bug, project):
            self.state.record_run_event(
                bug.bug_id,
                "already_handled_in_zentao",
                "ZenTao comments already carry the AI marker; not fixing again.",
            )
            LOGGER.info("Bug #%s already has an AI comment in ZenTao, skipping", bug.bug_id)
        else:
            self.state.mark_handled_in_zentao(bug.bug_id)
        return "handled"

    def _poll_project(self, project: ProjectConfig) -> None:
        started_at = utc_now()
        total = 0
        unresolved_count = 0
        candidate_count = 0
        queued = 0
        skipped_existing = 0
        skipped_resolved = 0
        skipped_platform = 0
        skipped_ui = 0
        marked_manual = 0
        requeued_failed = 0
        try:
            bugs = list_project_bugs(self.settings.zentao_client_script, project)
            total = len(bugs)
            existing_runs = {bug.bug_id: self.state.get_run(bug.bug_id) for bug in bugs}
            bugs.sort(
                key=lambda bug: (
                    existing_runs[bug.bug_id] is not None,
                    bug.opened_at or "9999",
                    bug.bug_id,
                )
            )
            for bug in bugs:
                if bug.bug_id <= 0:
                    skipped_resolved += 1
                    continue
                existing = existing_runs[bug.bug_id]
                active = _is_active(bug)

                if active and has_ui_tag(bug.title) and not project.process_ui_bugs:
                    skipped_ui += 1
                    continue

                if active and project.skips_platforms(platforms_of(bug.title)):
                    skipped_platform += 1
                    continue

                if not active:
                    skipped_resolved += 1
                    if existing and existing.status in AUTO_FIXED_STATUSES:
                        self.state.mark_seen_resolved_once(bug.bug_id, bug.status)
                    elif existing and existing.status == "failed":
                        message = f"ZenTao status is now {bug.status!r}; the failed task is no longer active."
                        self.state.update_status(bug.bug_id, "skipped_stale", error=message, completed=True)
                        self.state.record_run_event(bug.bug_id, "skipped_stale", message)
                    continue

                unresolved_count += 1
                candidate_count += 1

                if existing:
                    if existing.status == "queued":
                        self.worker.enqueue(bug.bug_id)
                        skipped_existing += 1
                        continue
                    if existing.status in {"running", "writeback_queued"}:
                        skipped_existing += 1
                        continue
                    if existing.status == "skipped_ui" and project.process_ui_bugs:
                        if queued >= project.max_bugs_per_poll:
                            skipped_existing += 1
                        elif self.state.requeue_skipped_ui(bug, project):
                            self.worker.enqueue(bug.bug_id)
                            queued += 1
                        else:
                            skipped_existing += 1
                        continue
                    if existing.status == "writeback_failed":
                        zentao_state = self._already_handled_in_zentao(bug, project)
                        if zentao_state == "fresh" and self.state.queue_writeback_retry(bug.bug_id):
                            self.worker.enqueue(bug.bug_id)
                            queued += 1
                        else:
                            skipped_existing += 1
                        continue
                    if existing.status in RETRYABLE_STATUSES:
                        if queued >= project.max_bugs_per_poll:
                            skipped_existing += 1
                            continue
                        if self._already_handled_in_zentao(bug, project) != "fresh":
                            skipped_existing += 1
                            continue
                        if self.state.requeue_retryable(bug, project):
                            self.worker.enqueue(bug.bug_id)
                            queued += 1
                            requeued_failed += 1
                        else:
                            skipped_existing += 1
                        continue
                    if existing.status in TERMINAL_STATUSES:
                        skipped_existing += 1
                        continue
                    skipped_existing += 1
                    continue

                if queued >= project.max_bugs_per_poll:
                    skipped_existing += 1
                    continue
                zentao_state = self._already_handled_in_zentao(bug, project)
                if zentao_state == "handled":
                    marked_manual += 1
                    continue
                if zentao_state == "unknown":
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
                skipped_resolved=skipped_resolved + skipped_platform + skipped_ui,
                marked_manual=marked_manual,
                requeued_failed=requeued_failed,
            )
            LOGGER.info(
                "Polled %s: total=%s unresolved=%s queued=%s existing=%s resolved=%s manual=%s "
                "platform_skipped=%s ui_skipped=%s",
                project.name,
                total,
                unresolved_count,
                queued,
                skipped_existing,
                skipped_resolved,
                marked_manual,
                skipped_platform,
                skipped_ui,
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
                skipped_resolved=skipped_resolved + skipped_platform + skipped_ui,
                marked_manual=marked_manual,
                requeued_failed=requeued_failed,
                error=str(exc),
            )
            raise


def _is_active(bug: BugCandidate) -> bool:
    if bug.status.strip().lower() != "active":
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
