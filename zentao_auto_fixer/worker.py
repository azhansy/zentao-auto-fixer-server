from __future__ import annotations

import logging
import queue
import threading
import time
import traceback
from typing import Dict, List, Optional

from .codex_runner import CodexError, run_codex_batch_fix
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
from .zentao import ZenTaoResolveError, resolve_bug


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
        if not run or run.status != "queued":
            return
        config_error = self.settings.validate_for_worker()
        if config_error:
            self.state.update_status(bug_id, "failed", error=config_error, completed=True)
            self.state.record_run_event(bug_id, "failed", config_error)
            LOGGER.error("Worker cannot start bug #%s: %s", bug_id, config_error)
            return

        self.state.record_run_event(bug_id, "waiting_repo_lock", run.repo_url)
        with self._lock_for_repo(run.repo_url):
            self._process_batch_with_repo_lock(bug_id)

    def _process_batch_with_repo_lock(self, leader_bug_id: int) -> None:
        batch = self.state.claim_queued_batch(leader_bug_id)
        if not batch:
            return
        first = batch[0]
        bug_ids = [run.bug_id for run in batch]
        batch_label = _batch_label(batch)
        self.state.record_run_events(
            bug_ids,
            "started",
            f"batch={batch_label} project={first.project_name} branch={first.target_branch}",
        )
        LOGGER.info(
            "Worker started batch %s project=%s branch=%s",
            batch_label,
            first.project_name,
            first.target_branch,
        )
        repo_cache = self.settings.repo_cache_dir / repo_cache_name(first.repo_url)
        worktree = None
        try:
            LOGGER.info("Batch %s syncing repo %s", batch_label, first.repo_url)
            self.state.record_run_events(bug_ids, "sync_repo", first.repo_url)
            sync_result = ensure_repo_cache(
                first.repo_url,
                repo_cache,
                first.target_branch,
                timeout=self.settings.git_timeout_seconds,
                shallow=self.settings.git_shallow_clone,
            )
            self.state.record_run_events(
                bug_ids,
                f"repo_{sync_result.action}",
                str(sync_result.path),
            )
            self.state.record_run_events(bug_ids, "create_worktree", str(self.settings.worktree_dir))
            worktree = create_detached_worktree(
                repo_cache,
                self.settings.worktree_dir,
                f"{first.project_name}-zentao-batch-{bug_ids[0]}-{bug_ids[-1]}",
                first.target_branch,
            )

            codex_log = self.settings.logs_dir / f"batch-{bug_ids[0]}-{bug_ids[-1]}-codex.log"
            self.state.record_run_events(bug_ids, "codex_start", str(codex_log))
            self._run_codex_batch_with_retries(batch, worktree, codex_log)
            self.state.record_run_events(bug_ids, "codex_done", str(worktree))
            if not has_changes(worktree):
                self.state.update_statuses(
                    bug_ids,
                    "no_changes",
                    error="Codex finished but no git changes were produced",
                    handled_once=True,
                    completed=True,
                )
                self.state.record_run_events(bug_ids, "no_changes", "Codex finished but no git changes were produced")
                LOGGER.info("Worker finished batch %s with no changes", batch_label)
                return

            commit_message = f"fix: zentao batch {batch_label}"
            self.state.record_run_events(bug_ids, "commit_start", commit_message)
            commit_hash = commit_all(
                worktree,
                commit_message,
                self.settings.git_author_name,
                self.settings.git_author_email,
            )
            self.state.record_run_events(bug_ids, "commit_done", commit_hash)
            try:
                self.state.record_run_events(bug_ids, "push_start", first.target_branch)
                push_head_to_branch(worktree, first.target_branch)
            except GitError as exc:
                status = "sync_conflict" if _looks_like_non_fast_forward(str(exc)) else "failed"
                self.state.update_statuses(
                    bug_ids,
                    status,
                    error=str(exc),
                    commit_hash=commit_hash,
                    handled_once=True,
                    completed=True,
                )
                self.state.record_run_events(bug_ids, status, str(exc))
                LOGGER.error("Worker push failed batch %s status=%s: %s", batch_label, status, exc)
                return

            self.state.record_run_events(bug_ids, "pushed", commit_hash)
            self._resolve_batch_after_push(batch, commit_hash)
            LOGGER.info("Worker pushed batch %s commit=%s", batch_label, commit_hash)
        except Exception as exc:
            error = f"{exc}\n{traceback.format_exc()}"
            self.state.update_statuses(
                bug_ids,
                "failed",
                error=error,
                handled_once=True,
                completed=True,
            )
            self.state.record_run_events(bug_ids, "failed", str(exc))
            LOGGER.error("Worker failed batch %s: %s", batch_label, exc)
        finally:
            if worktree is not None:
                remove_worktree(repo_cache, worktree)
                self.state.record_run_events(bug_ids, "cleanup_worktree", str(worktree))

    def _lock_for_repo(self, repo_url: str) -> threading.Lock:
        with self._repo_locks_guard:
            if repo_url not in self._repo_locks:
                self._repo_locks[repo_url] = threading.Lock()
            return self._repo_locks[repo_url]

    def _run_codex_batch_with_retries(self, batch, worktree, codex_log) -> None:
        last_error: Optional[CodexError] = None
        bug_ids = [run.bug_id for run in batch]
        bugs = [(run.bug_id, run.title) for run in batch]
        for attempt in range(1, self.settings.codex_attempts + 1):
            self.state.record_run_events(
                bug_ids,
                "codex_attempt",
                f"{attempt}/{self.settings.codex_attempts}",
            )
            try:
                run_codex_batch_fix(
                    self.settings.codex_bin,
                    worktree,
                    bugs,
                    codex_log,
                    timeout_seconds=self.settings.codex_timeout_seconds,
                    env_overrides={"ZENTAO_RESOLVE_BUG_AFTER_COMMENT": "0"},
                )
                return
            except CodexError as exc:
                last_error = exc
                self.state.record_run_events(
                    bug_ids,
                    "codex_attempt_failed",
                    f"{attempt}/{self.settings.codex_attempts}: {exc}",
                )
                if attempt < self.settings.codex_attempts:
                    time.sleep(self.settings.codex_retry_delay_seconds)
        assert last_error is not None
        raise last_error

    def _resolve_batch_after_push(self, batch, commit_hash: str) -> None:
        for run in batch:
            error = ""
            self.state.record_run_event(run.bug_id, "resolve_start", "Marking ZenTao bug resolved after batch push")
            try:
                resolve_bug(self.settings.zentao_client_script, run.bug_id)
                self.state.record_run_event(run.bug_id, "resolve_done", "resolved/fixed")
            except ZenTaoResolveError as exc:
                error = str(exc)
                self.state.record_run_event(run.bug_id, "resolve_failed", error)
                LOGGER.error("Resolve failed bug #%s after batch push: %s", run.bug_id, exc)
            self.state.update_status(
                run.bug_id,
                "pushed",
                error=error,
                commit_hash=commit_hash,
                handled_once=True,
                completed=True,
            )


def _looks_like_non_fast_forward(error: str) -> bool:
    lowered = error.lower()
    markers = ("non-fast-forward", "fetch first", "stale info", "rejected")
    return any(marker in lowered for marker in markers)


def _batch_label(batch) -> str:
    return " ".join(f"#{run.bug_id}" for run in batch)
