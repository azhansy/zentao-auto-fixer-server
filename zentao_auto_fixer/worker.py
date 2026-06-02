from __future__ import annotations

import logging
import queue
import threading
import time
import traceback
from typing import Dict, List, Optional

from .codex_runner import CodexError, run_codex_fix
from .config import Settings
from .git_ops import (
    GitError,
    commit_all,
    create_detached_worktree,
    ensure_repo_cache,
    has_changes,
    push_head_to_branch,
    remove_worktree,
    repo_cache_name,
)
from .state import StateStore


LOGGER = logging.getLogger("zentao_auto_fixer.worker")


class Worker:
    def __init__(self, settings: Settings, state: StateStore):
        self.settings = settings
        self.state = state
        self.queue: "queue.Queue[int]" = queue.Queue()
        self._threads: List[threading.Thread] = []
        self._stop = threading.Event()
        self._repo_locks: Dict[str, threading.Lock] = {}
        self._repo_locks_guard = threading.Lock()

    def start(self) -> None:
        if self._threads:
            return
        for index in range(self.settings.worker_count):
            thread = threading.Thread(target=self._run, name=f"auto-fixer-worker-{index + 1}", daemon=True)
            thread.start()
            self._threads.append(thread)
        for bug_id in self.state.queued_bug_ids():
            self.enqueue(bug_id)

    def stop(self) -> None:
        self._stop.set()
        for _thread in self._threads:
            self.queue.put(-1)
        for thread in self._threads:
            thread.join(timeout=5)

    def enqueue(self, bug_id: int) -> None:
        self.queue.put(bug_id)
        LOGGER.info("Queued bug #%s for repair", bug_id)

    def _run(self) -> None:
        while not self._stop.is_set():
            bug_id = self.queue.get()
            if bug_id < 0:
                continue
            try:
                self._process_bug(bug_id)
            except Exception as exc:
                error = f"{exc}\n{traceback.format_exc()}"
                self.state.update_status(bug_id, "failed", error=error, completed=True)
                self.state.record_run_event(bug_id, "failed", str(exc))
                LOGGER.error("Worker failed bug #%s: %s", bug_id, exc)
            finally:
                self.queue.task_done()

    def _process_bug(self, bug_id: int) -> None:
        run = self.state.get_run(bug_id)
        if not run:
            return
        config_error = self.settings.validate_for_worker()
        if config_error:
            self.state.update_status(bug_id, "failed", error=config_error, completed=True)
            self.state.record_run_event(bug_id, "failed", config_error)
            LOGGER.error("Worker cannot start bug #%s: %s", bug_id, config_error)
            return

        self.state.record_run_event(bug_id, "waiting_repo_lock", run.repo_url)
        with self._lock_for_repo(run.repo_url):
            self._process_bug_with_repo_lock(bug_id)

    def _process_bug_with_repo_lock(self, bug_id: int) -> None:
        run = self.state.get_run(bug_id)
        if not run:
            return
        self.state.update_status(bug_id, "running", handled_once=True)
        self.state.record_run_event(
            bug_id,
            "started",
            f"project={run.project_name} branch={run.target_branch}",
        )
        LOGGER.info(
            "Worker started bug #%s project=%s branch=%s",
            bug_id,
            run.project_name,
            run.target_branch,
        )
        repo_cache = self.settings.repo_cache_dir / repo_cache_name(run.repo_url)
        worktree = None
        try:
            LOGGER.info("Bug #%s syncing repo %s", bug_id, run.repo_url)
            self.state.record_run_event(bug_id, "sync_repo", run.repo_url)
            sync_result = ensure_repo_cache(
                run.repo_url,
                repo_cache,
                run.target_branch,
                timeout=self.settings.git_timeout_seconds,
                shallow=self.settings.git_shallow_clone,
            )
            self.state.record_run_event(
                bug_id,
                f"repo_{sync_result.action}",
                str(sync_result.path),
            )
            self.state.record_run_event(bug_id, "create_worktree", str(self.settings.worktree_dir))
            worktree = create_detached_worktree(
                repo_cache,
                self.settings.worktree_dir,
                f"{run.project_name}-zentao-{bug_id}",
                run.target_branch,
            )

            codex_log = self.settings.logs_dir / f"bug-{bug_id}-codex.log"
            self.state.record_run_event(bug_id, "codex_start", str(codex_log))
            self._run_codex_with_retries(bug_id, worktree, run.title, codex_log)
            self.state.record_run_event(bug_id, "codex_done", str(worktree))
            if not has_changes(worktree):
                self.state.update_status(
                    bug_id,
                    "no_changes",
                    error="Codex finished but no git changes were produced",
                    handled_once=True,
                    completed=True,
                )
                self.state.record_run_event(bug_id, "no_changes", "Codex finished but no git changes were produced")
                LOGGER.info("Worker finished bug #%s with no changes", bug_id)
                return

            commit_message = f"fix: zentao #{bug_id} {run.title}"
            self.state.record_run_event(bug_id, "commit_start", commit_message)
            commit_hash = commit_all(
                worktree,
                commit_message,
                self.settings.git_author_name,
                self.settings.git_author_email,
            )
            self.state.record_run_event(bug_id, "commit_done", commit_hash)
            try:
                self.state.record_run_event(bug_id, "push_start", run.target_branch)
                push_head_to_branch(worktree, run.target_branch)
            except GitError as exc:
                status = "sync_conflict" if _looks_like_non_fast_forward(str(exc)) else "failed"
                self.state.update_status(
                    bug_id,
                    status,
                    error=str(exc),
                    commit_hash=commit_hash,
                    handled_once=True,
                    completed=True,
                )
                self.state.record_run_event(bug_id, status, str(exc))
                LOGGER.error("Worker push failed bug #%s status=%s: %s", bug_id, status, exc)
                return

            self.state.update_status(
                bug_id,
                "pushed",
                commit_hash=commit_hash,
                handled_once=True,
                completed=True,
            )
            self.state.record_run_event(bug_id, "pushed", commit_hash)
            LOGGER.info("Worker pushed bug #%s commit=%s", bug_id, commit_hash)
        finally:
            if worktree is not None:
                remove_worktree(repo_cache, worktree)
                self.state.record_run_event(bug_id, "cleanup_worktree", str(worktree))

    def _lock_for_repo(self, repo_url: str) -> threading.Lock:
        with self._repo_locks_guard:
            if repo_url not in self._repo_locks:
                self._repo_locks[repo_url] = threading.Lock()
            return self._repo_locks[repo_url]

    def _run_codex_with_retries(self, bug_id: int, worktree, title: str, codex_log) -> None:
        last_error: Optional[CodexError] = None
        for attempt in range(1, self.settings.codex_attempts + 1):
            self.state.record_run_event(
                bug_id,
                "codex_attempt",
                f"{attempt}/{self.settings.codex_attempts}",
            )
            try:
                run_codex_fix(self.settings.codex_bin, worktree, bug_id, title, codex_log)
                return
            except CodexError as exc:
                last_error = exc
                self.state.record_run_event(
                    bug_id,
                    "codex_attempt_failed",
                    f"{attempt}/{self.settings.codex_attempts}: {exc}",
                )
                if attempt < self.settings.codex_attempts:
                    time.sleep(self.settings.codex_retry_delay_seconds)
        assert last_error is not None
        raise last_error


def _looks_like_non_fast_forward(error: str) -> bool:
    lowered = error.lower()
    markers = ("non-fast-forward", "fetch first", "stale info", "rejected")
    return any(marker in lowered for marker in markers)
