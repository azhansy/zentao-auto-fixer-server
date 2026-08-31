from __future__ import annotations

import contextlib
import json
import logging
import queue
import threading
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

from .agent_runner import (
    AgentError,
    AgentQuotaError,
    TriageResultError,
    run_agent_batch_fix,
    stop_active_agents,
)
from .config import Settings
from .git_ops import (
    GitError,
    RebaseConflictError,
    abort_rebase,
    changed_files,
    commit_all,
    continue_rebase,
    create_detached_worktree,
    ensure_repo_cache,
    export_patch,
    has_changes,
    head_commit,
    push_head_dry_run,
    push_head_to_branch,
    rebase_onto_latest_remote,
    remove_worktree,
    repo_cache_name,
    reset_hard_clean,
)
from .models import ProjectConfig, RunRecord, has_ui_tag, platforms_of
from .state import StateStore
from .zentao import (
    ZenTaoPollError,
    ZenTaoResolveError,
    ZenTaoWriteError,
    bug_is_still_actionable,
    bug_view_url,
    comment_bug,
    resolve_bug,
)


LOGGER = logging.getLogger("zentao_auto_fixer.worker")


class Worker:
    def __init__(self, settings: Settings, state: StateStore):
        self.settings = settings
        self.state = state
        self.queue: "queue.Queue[int]" = queue.Queue()
        self._queued_ids = set()
        self._queue_guard = threading.Lock()
        self._threads: List[threading.Thread] = []
        self._stop = threading.Event()
        self._repo_locks: Dict[str, threading.Lock] = {}
        self._repo_locks_guard = threading.Lock()
        self._budget_guard = threading.Lock()
        self._agent_runs_day = ""
        self._agent_runs_today = 0

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
        stop_active_agents()
        for _thread in self._threads:
            self.queue.put(-1)
        for thread in self._threads:
            thread.join(timeout=5)

    def enqueue(self, bug_id: int) -> None:
        with self._queue_guard:
            if bug_id in self._queued_ids:
                return
            self._queued_ids.add(bug_id)
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
                run = self.state.get_run(bug_id)
                if run and (run.status == "running" or run.status == "queued"):
                    self._fail_batch([run], "failed", str(exc), "")
                LOGGER.exception("Worker failed bug #%s", bug_id)
            finally:
                with self._queue_guard:
                    self._queued_ids.discard(bug_id)
                self.queue.task_done()

    def _process_bug(self, bug_id: int) -> None:
        run = self.state.get_run(bug_id)
        if not run:
            return
        if run.status == "writeback_queued":
            self._retry_writeback(run)
            return
        if run.status != "queued":
            return
        config_error = self.settings.validate_for_worker()
        if config_error:
            self._fail_batch([run], "failed", config_error, "")
            LOGGER.error("Worker cannot start bug #%s: %s", bug_id, config_error)
            return

        project = self._project_for(run)
        if project is None:
            message = f"Project {run.project_name!r} is missing from the project config; not guessing its repos."
            self._fail_batch([run], "failed", message, "")
            LOGGER.error("Worker cannot start bug #%s: %s", bug_id, message)
            return

        if has_ui_tag(run.title) and not project.process_ui_bugs:
            message = "标题带有 UI 标签，当前项目 processUiBugs=false，未调用 AI。"
            self.state.update_status(
                bug_id,
                "skipped_ui",
                error=message,
                handled_once=False,
                completed=True,
            )
            self.state.record_run_event(bug_id, "skipped_ui", message)
            LOGGER.info("Bug #%s skipped because its title carries a UI tag", bug_id)
            return

        stale = self._stale_reason(run)
        if stale:
            self.state.update_status(bug_id, "skipped_stale", error=stale, completed=True)
            self.state.record_run_event(bug_id, "skipped_stale", stale)
            LOGGER.info("Bug #%s no longer needs fixing: %s", bug_id, stale)
            return

        titled_platforms = platforms_of(run.title)
        if len(titled_platforms) > 1:
            self._reject_multi_platform(run, titled_platforms)
            return

        latest = self.state.get_run(bug_id)
        if not latest or latest.status != "queued":
            return
        if not self._claim_agent_budget():
            reason = self._agent_budget_block_reason()
            self.state.record_run_event(
                bug_id,
                "agent_budget_exhausted",
                reason,
            )
            LOGGER.warning("Bug #%s stays queued: %s", bug_id, reason)
            return
        self._process_batch(bug_id, project)

    def _reject_multi_platform(self, run: RunRecord, platforms: tuple) -> None:
        """One bug must describe one platform; a multi-platform title cannot be pinned to code."""
        named = "、".join(platforms)
        self.state.record_run_event(run.bug_id, "multi_platform", named)
        LOGGER.info("Bug #%s names several platforms (%s), handing it back", run.bug_id, named)
        self._record_unable_to_fix(
            run,
            {
                "understanding": f"这条 Bug 的标题同时标注了 {named} 多个端。",
                "steps": [],
                "reason": "一条 Bug 只能描述一个端的问题。同时标注多个端时，无法确定要定位和修改哪一端的代码。",
                "missing": f"请按端拆成多条 Bug（{named} 各一条），每条只写该端的复现步骤和现象。",
            },
        )

    def _stale_reason(self, run: RunRecord) -> str:
        """A queued bug can sit for hours or survive a restart; re-check ZenTao before spending an agent run."""
        try:
            actionable, reason = bug_is_still_actionable(
                self.settings.zentao_client_script,
                run.bug_id,
                ignore_ai_comment=run.event_action == "manual_retry",
            )
        except ZenTaoPollError as exc:
            LOGGER.warning("Could not re-check bug #%s before fixing it: %s", run.bug_id, exc)
            return f"Could not confirm the bug is still active: {exc}"
        return "" if actionable else reason

    def _claim_agent_budget(self) -> bool:
        """One batch costs one agent run. The ceiling is the backstop against a runaway poll loop."""
        today = datetime.now().astimezone().date().isoformat()
        if self.state.daily_counter_value(self._no_progress_counter_name(), today) >= 3:
            return False
        return self._claim_agent_run(today)

    def _claim_agent_run(self, today: Optional[str] = None) -> bool:
        """Count a real model start; conflict resolution may finish an existing fix despite the no-progress fuse."""
        today = today or datetime.now().astimezone().date().isoformat()
        claimed = self.state.claim_daily_counter(
            "agent_runs",
            today,
            self.settings.max_agent_runs_per_day,
        )
        current = self.state.daily_counter_value("agent_runs", today)
        with self._budget_guard:
            self._agent_runs_day = today
            self._agent_runs_today = current
        return claimed

    def _agent_budget_block_reason(self) -> str:
        today = datetime.now().astimezone().date().isoformat()
        if self.state.daily_counter_value(self._no_progress_counter_name(), today) >= 3:
            return "Three consecutive AI runs produced no pushed fix; paused until tomorrow."
        return f"Hit the persisted {self.settings.max_agent_runs_per_day} agent runs/day ceiling; paused until tomorrow."

    def _record_no_progress(self) -> None:
        today = datetime.now().astimezone().date().isoformat()
        self.state.increment_daily_counter(self._no_progress_counter_name(), today)

    def _record_progress(self) -> None:
        today = datetime.now().astimezone().date().isoformat()
        self.state.set_daily_counter(self._no_progress_counter_name(), today, 0)

    def _no_progress_counter_name(self) -> str:
        return f"consecutive_no_progress:{threading.current_thread().name}"

    def agent_runs_today(self) -> int:
        today = datetime.now().astimezone().date().isoformat()
        return self.state.daily_counter_value("agent_runs", today)

    def _project_for(self, run: RunRecord) -> Optional[ProjectConfig]:
        try:
            projects = self.settings.load_projects()
        except Exception:
            LOGGER.exception("Could not read project config while processing bug #%s", run.bug_id)
            return None

        for project in projects:
            if project.name == run.project_name:
                return project
        return None

    def _process_batch(self, leader_bug_id: int, project: ProjectConfig) -> None:
        batch = self.state.claim_queued_batch(leader_bug_id, limit=project.max_bugs_per_poll)
        if not batch:
            return
        skipped_ui = [run for run in batch if has_ui_tag(run.title) and not project.process_ui_bugs]
        for run in skipped_ui:
            message = "标题带有 UI 标签，当前项目 processUiBugs=false，未调用 AI。"
            self.state.update_status(
                run.bug_id,
                "skipped_ui",
                error=message,
                handled_once=False,
                completed=True,
            )
            self.state.record_run_event(run.bug_id, "skipped_ui", message)
        batch = [run for run in batch if run not in skipped_ui]
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

        backend_repo = (project.backend_repo_url, project.backend_target_branch) if project.has_backend_repo else None
        checkouts: Dict[str, _Checkout] = {}
        try:
            checkouts["app"] = self._prepare_checkout(bug_ids, "app", first.repo_url, first.target_branch, batch_label)
            if backend_repo:
                checkouts["backend"] = self._prepare_checkout(
                    bug_ids, "backend", backend_repo[0], backend_repo[1], batch_label
                )

            result_path = self.settings.logs_dir / f"batch-{bug_ids[0]}-{bug_ids[-1]}-triage.json"
            agent_log = self.settings.logs_dir / f"batch-{bug_ids[0]}-{bug_ids[-1]}-agent.log"
            agent = project.agent
            self.state.record_run_events(bug_ids, "agent_start", f"{agent} log={agent_log}")
            verdicts = self._run_agent_batch_with_retries(
                batch,
                agent,
                checkouts,
                result_path,
                agent_log,
                fallback_agent=project.fallback_agent,
                allow_full_xcodebuild=project.allow_full_xcodebuild,
            )
            self.state.record_run_events(bug_ids, "agent_done", str(checkouts["app"].worktree))

            for run in batch:
                self.state.set_triage_targets(run.bug_id, ",".join(verdicts[run.bug_id]["targets"]))

            # Second gate: the title may not have said "android", but the agent just read the bug
            # and told us which platform it is. Drop those before anything reaches a branch.
            skipped = [
                run
                for run in batch
                if project.skips_platforms(_verdict_platforms(verdicts[run.bug_id]))
            ]
            for run in skipped:
                platform = verdicts[run.bug_id].get("platform", "")
                message = f"分诊判定这是 {platform} 端的问题，当前配置跳过该平台，未做任何提交。"
                self.state.update_status(run.bug_id, "skipped_platform", error=message, completed=True)
                self.state.record_run_event(run.bug_id, "skipped_platform", message)
                LOGGER.info("Bug #%s skipped: agent says platform=%s", run.bug_id, platform)
            remaining = [run for run in batch if run not in skipped]

            rejected = [run for run in remaining if verdicts[run.bug_id]["decision"] == "rejected"]
            fixed = [run for run in remaining if verdicts[run.bug_id]["decision"] == "fixed"]
            for run in rejected:
                self._record_unable_to_fix(run, verdicts[run.bug_id])
            if not fixed:
                self._record_no_progress()
                LOGGER.info("Worker finished batch %s with nothing to commit", batch_label)
                return
            with contextlib.ExitStack() as stack:
                for repo_url in sorted({checkout.repo_url for checkout in checkouts.values()}):
                    stack.enter_context(self._lock_for_repo(repo_url))
                self._commit_push_and_resolve(
                    fixed,
                    checkouts,
                    verdicts,
                    batch_label,
                    project.agent,
                    project.allow_full_xcodebuild,
                )
        except Exception as exc:
            unfinished = [run for run in batch if _still_running(self.state, run.bug_id)]
            if self._stop.is_set():
                self.state.record_run_events(
                    [run.bug_id for run in unfinished],
                    "interrupted_for_restart",
                    "Service stopped; the next start will requeue this batch.",
                )
                LOGGER.info("Worker interrupted batch %s for service stop", batch_label)
            else:
                self._fail_batch(unfinished, "failed", str(exc), "", count_no_progress=True)
                LOGGER.exception("Worker failed batch %s", batch_label)
        finally:
            for checkout in checkouts.values():
                try:
                    with self._lock_for_repo(checkout.repo_url):
                        remove_worktree(checkout.repo_cache, checkout.worktree)
                except Exception:
                    LOGGER.exception("Could not remove worktree %s", checkout.worktree)
            if checkouts:
                try:
                    self.state.record_run_events(
                        bug_ids,
                        "cleanup_worktree",
                        " ".join(str(checkout.worktree) for checkout in checkouts.values()),
                    )
                except Exception:
                    LOGGER.exception("Could not record worktree cleanup for batch %s", batch_label)

    def _prepare_checkout(
        self,
        bug_ids: List[int],
        kind: str,
        repo_url: str,
        target_branch: str,
        batch_label: str,
    ) -> "_Checkout":
        repo_cache = self.settings.repo_cache_dir / repo_cache_name(repo_url)
        LOGGER.info("Batch %s syncing %s repo %s", batch_label, kind, repo_url)
        self.state.record_run_events(bug_ids, f"sync_repo_{kind}", repo_url)
        with self._lock_for_repo(repo_url):
            sync_result = ensure_repo_cache(
                repo_url,
                repo_cache,
                target_branch,
                timeout=self.settings.git_timeout_seconds,
                shallow=self.settings.git_shallow_clone,
            )
            self.state.record_run_events(bug_ids, f"repo_{kind}_{sync_result.action}", str(sync_result.path))
            worktree = create_detached_worktree(
                repo_cache,
                self.settings.worktree_dir,
                f"{kind}-zentao-batch-{bug_ids[0]}-{bug_ids[-1]}",
                target_branch,
            )
        self.state.record_run_events(bug_ids, f"create_worktree_{kind}", str(worktree))
        return _Checkout(
            kind=kind,
            repo_url=repo_url,
            repo_cache=repo_cache,
            worktree=worktree,
            target_branch=target_branch,
            baseline=head_commit(worktree),
        )

    def _lock_for_repo(self, repo_url: str) -> threading.Lock:
        with self._repo_locks_guard:
            if repo_url not in self._repo_locks:
                self._repo_locks[repo_url] = threading.Lock()
            return self._repo_locks[repo_url]

    def _run_agent_batch_with_retries(
        self,
        batch: List[RunRecord],
        agent: str,
        checkouts: Dict[str, "_Checkout"],
        result_path: Path,
        agent_log: Path,
        fallback_agent: str = "",
        allow_full_xcodebuild: bool = False,
    ) -> Dict[int, Dict[str, Any]]:
        last_error: Optional[Exception] = None
        bug_ids = [run.bug_id for run in batch]
        bugs = [(run.bug_id, run.title) for run in batch]
        backend = checkouts.get("backend")
        for attempt in range(1, self.settings.codex_attempts + 1):
            self.state.record_run_events(
                bug_ids,
                "agent_attempt",
                f"{agent} {attempt}/{self.settings.codex_attempts}",
            )
            if attempt > 1:
                for checkout in checkouts.values():
                    reset_hard_clean(checkout.worktree, checkout.baseline)
                self.state.record_run_events(bug_ids, "worktrees_reset", "discarded the previous attempt")
            try:
                return run_agent_batch_fix(
                    agent,
                    self.settings.agent_bin(agent),
                    self.settings.zentao_client_script,
                    checkouts["app"].worktree,
                    backend.worktree if backend else None,
                    bugs,
                    result_path,
                    agent_log,
                    timeout_seconds=self.settings.codex_timeout_seconds,
                    allow_full_xcodebuild=allow_full_xcodebuild,
                )
            except AgentQuotaError as exc:
                last_error = exc
                self.state.record_run_events(
                    bug_ids,
                    "agent_attempt_failed",
                    f"{agent} {attempt}/{self.settings.codex_attempts}: {exc}",
                )
                if not fallback_agent:
                    break
                if not self._claim_agent_budget():
                    self.state.record_run_events(
                        bug_ids,
                        "agent_fallback_budget_exhausted",
                        f"Claude quota exhausted, but today's {self.settings.max_agent_runs_per_day} "
                        "agent starts are already used.",
                    )
                    raise AgentError("Claude quota exhausted and no daily agent budget remains for fallback") from exc
                for checkout in checkouts.values():
                    reset_hard_clean(checkout.worktree, checkout.baseline)
                self.state.record_run_events(bug_ids, "worktrees_reset", "discarded the exhausted agent attempt")
                self.state.record_run_events(
                    bug_ids,
                    "agent_fallback",
                    f"{agent} quota exhausted; switching to {fallback_agent}",
                )
                return self._run_agent_batch_with_retries(
                    batch,
                    fallback_agent,
                    checkouts,
                    result_path,
                    agent_log,
                    allow_full_xcodebuild=allow_full_xcodebuild,
                )
            except (AgentError, TriageResultError) as exc:
                last_error = exc
                self.state.record_run_events(
                    bug_ids,
                    "agent_attempt_failed",
                    f"{agent} {attempt}/{self.settings.codex_attempts}: {exc}",
                )
                if attempt < self.settings.codex_attempts:
                    time.sleep(self.settings.codex_retry_delay_seconds)
        assert last_error is not None
        raise last_error

    def _record_unable_to_fix(self, run: RunRecord, verdict: Dict[str, Any]) -> None:
        """Remember an unfixable result locally; unsuccessful runs never mutate ZenTao."""
        reason = verdict.get("reason") or "AI 无法从当前 Bug 描述定位到问题。"
        missing = verdict.get("missing") or "请补充复现步骤、测试账号、出现时间和截图或日志。"
        detail = f"{reason} 需要补充：{missing}"
        self.state.mark_unable_to_fix(run.bug_id, detail)
        self.state.record_run_event(run.bug_id, "unable_to_fix", detail)

    def _save_conflict_patch(self, checkout: "_Checkout", batch_label: str) -> str:
        """Push failed; save the agent's actual diff so a human doesn't have to re-diagnose from scratch."""
        if not checkout.has_work():
            return ""
        try:
            patch = export_patch(checkout.worktree, checkout.baseline)
        except GitError:
            LOGGER.exception("Could not export patch for %s repo=%s", batch_label, checkout.kind)
            return ""
        if not patch.strip():
            return ""
        patches_dir = self.settings.data_dir / "patches"
        patches_dir.mkdir(parents=True, exist_ok=True)
        safe_label = batch_label.replace("#", "").replace(" ", "-")
        path = patches_dir / f"{safe_label}-{checkout.kind}-{checkout.baseline[:8]}.patch"
        path.write_text(patch, encoding="utf-8")
        return str(path)

    def _fail_batch(
        self,
        batch: List[RunRecord],
        status: str,
        detail: str,
        commit_hash: str,
        *,
        count_no_progress: bool = False,
    ) -> None:
        """Keep service failures out of ZenTao; health and local events expose them."""
        for run in batch:
            self.state.update_status(
                run.bug_id,
                status,
                error=detail,
                commit_hash=commit_hash,
                handled_once=True,
                completed=True,
            )
            self.state.record_run_event(run.bug_id, status, detail)
        if batch and count_no_progress:
            self._record_no_progress()

    def _commit_push_and_resolve(
        self,
        fixed: List[RunRecord],
        checkouts: Dict[str, "_Checkout"],
        verdicts: Dict[int, Dict[str, Any]],
        batch_label: str,
        agent: str,
        allow_full_xcodebuild: bool,
    ) -> None:
        bug_ids = [run.bug_id for run in fixed]
        changed = [checkout for checkout in checkouts.values() if checkout.has_work()]
        for checkout in changed:
            files = changed_files(checkout.worktree)
            if files:
                self.state.record_run_events(bug_ids, f"changed_files_{checkout.kind}", ", ".join(files))
            if checkout.agent_committed:
                self.state.record_run_events(
                    bug_ids,
                    f"agent_committed_{checkout.kind}",
                    "The agent committed on its own; pushing its commits instead of dropping them.",
                )
        if not changed:
            detail = "The agent reported a fix but produced no code changes; kept local without ZenTao writeback."
            for run in fixed:
                self.state.mark_unable_to_fix(run.bug_id, detail)
            self.state.record_run_events(bug_ids, "unable_to_fix", detail)
            self._record_no_progress()
            LOGGER.info("Worker finished batch %s with no changes", batch_label)
            return

        commit_message = f"fix: zentao batch {batch_label}"
        for checkout in changed:
            if has_changes(checkout.worktree):
                self.state.record_run_events(bug_ids, f"commit_start_{checkout.kind}", commit_message)
                commit_hash = commit_all(
                    checkout.worktree,
                    commit_message,
                    self.settings.git_author_name,
                    self.settings.git_author_email,
                )
            else:
                commit_hash = head_commit(checkout.worktree)
            self.state.record_run_events(bug_ids, f"commit_done_{checkout.kind}", commit_hash)

        while True:
            if not self._rebase_changed_checkouts(
                fixed,
                changed,
                checkouts,
                verdicts,
                batch_label,
                agent,
                allow_full_xcodebuild,
            ):
                return

            retry_refresh = False
            for checkout in changed:
                try:
                    self.state.record_run_events(bug_ids, f"push_check_{checkout.kind}", checkout.target_branch)
                    push_head_dry_run(checkout.worktree, checkout.target_branch)
                except GitError as exc:
                    if _looks_like_non_fast_forward(str(exc)):
                        self.state.record_run_events(
                            bug_ids, f"push_check_retry_{checkout.kind}", str(exc)
                        )
                        retry_refresh = True
                        break
                    self._fail_push(fixed, checkout, batch_label, f"{checkout.kind} push check: {exc}")
                    return
            if not retry_refresh:
                break

        pushed: List[str] = []
        for checkout in changed:
            while True:
                try:
                    self.state.record_run_events(bug_ids, f"push_start_{checkout.kind}", checkout.target_branch)
                    push_head_to_branch(checkout.worktree, checkout.target_branch)
                    break
                except GitError as exc:
                    if _looks_like_non_fast_forward(str(exc)):
                        self.state.record_run_events(bug_ids, f"push_retry_{checkout.kind}", str(exc))
                        if not self._rebase_changed_checkouts(
                            fixed,
                            [checkout],
                            checkouts,
                            verdicts,
                            batch_label,
                            agent,
                            allow_full_xcodebuild,
                        ):
                            return
                        continue
                    detail = f"{checkout.kind}: {exc}"
                    commits = [f"{item.kind}:{head_commit(item.worktree)}" for item in changed]
                    if pushed:
                        detail = (
                            f"仓库 {'、'.join(pushed)} 的修复已经推送（{' '.join(commits)}），"
                            f"但 {checkout.kind} 推送失败，修复只落地了一半，需要人工处理：{exc}"
                        )
                    self._fail_push(fixed, checkout, batch_label, detail, " ".join(commits))
                    LOGGER.error(
                        "Worker push failed batch %s repo=%s (already pushed: %s): %s",
                        batch_label,
                        checkout.kind,
                        pushed or "none",
                        exc,
                    )
                    return
            pushed.append(checkout.kind)
            self.state.record_run_events(bug_ids, f"pushed_{checkout.kind}", checkout.target_branch)

        commits = [f"{checkout.kind}:{head_commit(checkout.worktree)}" for checkout in changed]
        commit_summary = " ".join(commits)
        self._record_progress()
        self._comment_and_resolve(fixed, verdicts, commit_summary)
        urls = ", ".join(bug_view_url(run.bug_id) for run in fixed)
        LOGGER.info("Worker pushed batch %s commits=%s urls=%s", batch_label, commit_summary, urls)

    def _rebase_changed_checkouts(
        self,
        fixed: List[RunRecord],
        changed: List["_Checkout"],
        checkouts: Dict[str, "_Checkout"],
        verdicts: Dict[int, Dict[str, Any]],
        batch_label: str,
        agent: str,
        allow_full_xcodebuild: bool,
    ) -> bool:
        bug_ids = [run.bug_id for run in fixed]
        for checkout in changed:
            while True:
                old_baseline = checkout.baseline
                self.state.record_run_events(bug_ids, f"refresh_remote_{checkout.kind}", checkout.target_branch)
                try:
                    latest = rebase_onto_latest_remote(
                        checkout.worktree,
                        checkout.target_branch,
                        checkout.baseline,
                        timeout=self.settings.git_timeout_seconds,
                        shallow=self.settings.git_shallow_clone,
                    )
                except RebaseConflictError as exc:
                    if not self._claim_agent_run():
                        self._abort_conflict_and_requeue(
                            fixed,
                            changed,
                            checkout,
                            batch_label,
                            f"Hit the persisted {self.settings.max_agent_runs_per_day} agent runs/day ceiling; "
                            "kept the fix queued until the daily budget resets.",
                        )
                        return False
                    self.state.record_run_events(
                        bug_ids,
                        f"conflict_agent_start_{checkout.kind}",
                        f"latest={exc.latest}",
                    )
                    result_path = self.settings.logs_dir / f"batch-{bug_ids[0]}-{bug_ids[-1]}-conflict-triage.json"
                    agent_log = self.settings.logs_dir / f"batch-{bug_ids[0]}-{bug_ids[-1]}-conflict-agent.log"
                    other_heads = {
                        other.kind: head_commit(other.worktree)
                        for other in checkouts.values()
                        if other is not checkout
                    }
                    try:
                        conflict_verdicts = run_agent_batch_fix(
                            agent,
                            self.settings.agent_bin(agent),
                            self.settings.zentao_client_script,
                            checkouts["app"].worktree,
                            checkouts.get("backend").worktree if checkouts.get("backend") else None,
                            [(run.bug_id, run.title) for run in fixed],
                            result_path,
                            agent_log,
                            timeout_seconds=self.settings.codex_timeout_seconds,
                            allow_full_xcodebuild=allow_full_xcodebuild,
                            conflict_context=f"{checkout.kind} 仓库 {checkout.worktree}",
                        )
                        rejected = [
                            bug_id
                            for bug_id, verdict in conflict_verdicts.items()
                            if verdict["decision"] != "fixed"
                        ]
                        if rejected:
                            raise TriageResultError(
                                "Conflict resolver rejected " + ", ".join(f"#{bug_id}" for bug_id in rejected)
                            )
                        touched_others = [
                            other.kind
                            for other in checkouts.values()
                            if other is not checkout
                            and (
                                has_changes(other.worktree)
                                or head_commit(other.worktree) != other_heads[other.kind]
                            )
                        ]
                        if touched_others:
                            for other in checkouts.values():
                                if other.kind in touched_others:
                                    reset_hard_clean(other.worktree, other_heads[other.kind])
                            raise TriageResultError(
                                "Conflict resolver modified unrelated repositories: " + ", ".join(touched_others)
                            )
                        continue_rebase(checkout.worktree, timeout=self.settings.git_timeout_seconds)
                    except Exception as retry_error:
                        self.state.record_run_events(
                            bug_ids, f"conflict_agent_retry_{checkout.kind}", str(retry_error)
                        )
                        try:
                            abort_rebase(checkout.worktree, timeout=self.settings.git_timeout_seconds)
                        except GitError as abort_error:
                            self._abort_conflict_and_requeue(
                                fixed,
                                changed,
                                checkout,
                                batch_label,
                                f"Conflict retry failed and rebase could not be reset: {abort_error}",
                                abort=False,
                            )
                            return False
                        continue
                    verdicts.update(conflict_verdicts)
                    for run in fixed:
                        self.state.set_triage_targets(
                            run.bug_id,
                            ",".join(conflict_verdicts[run.bug_id]["targets"]),
                        )
                    checkout.baseline = exc.latest
                    self.state.record_run_events(
                        bug_ids,
                        f"conflict_agent_done_{checkout.kind}",
                        f"latest={exc.latest} head={head_commit(checkout.worktree)}",
                    )
                    continue

                if latest != old_baseline:
                    checkout.baseline = latest
                    self.state.record_run_events(
                        bug_ids,
                        f"rebased_{checkout.kind}",
                        f"from={old_baseline} onto={latest} head={head_commit(checkout.worktree)}",
                    )
                break
        return True

    def _abort_conflict_and_requeue(
        self,
        fixed: List[RunRecord],
        changed: List["_Checkout"],
        conflicted: "_Checkout",
        batch_label: str,
        reason: str,
        *,
        abort: bool = True,
    ) -> None:
        if abort:
            try:
                abort_rebase(conflicted.worktree, timeout=self.settings.git_timeout_seconds)
            except GitError as abort_error:
                reason += f"; rebase abort also failed: {abort_error}"
        patches = [self._save_conflict_patch(checkout, batch_label) for checkout in changed]
        saved = [path for path in patches if path]
        if saved:
            reason += "\nSaved patches: " + ", ".join(saved)
        for run in fixed:
            self.state.update_status(run.bug_id, "queued", error=reason, handled_once=True)
            self.state.record_run_event(run.bug_id, "conflict_retry_queued", reason)

    def _fail_push(
        self,
        fixed: List[RunRecord],
        checkout: "_Checkout",
        batch_label: str,
        detail: str,
        commits: str = "",
    ) -> None:
        patch_path = self._save_conflict_patch(checkout, batch_label)
        if patch_path:
            detail += f"\nAI 这次的改动已经存成补丁，人工接手时可以直接用：{patch_path}"
        self._fail_batch(fixed, "failed", detail, commits, count_no_progress=True)

    def _comment_and_resolve(
        self,
        fixed: List[RunRecord],
        verdicts: Dict[int, Dict[str, Any]],
        commit_summary: str,
    ) -> None:
        for run in fixed:
            verdict = verdicts[run.bug_id]
            payload = {
                "cause": _cause_text(verdict, "原因分析", verdict.get("cause") or "见提交记录。"),
                "solution": _solution_text(verdict, commit_summary),
                "commit_summary": commit_summary,
            }
            self.state.set_writeback_payload(run.bug_id, json.dumps(payload, ensure_ascii=False))
            self._writeback_one(run, payload)

    def _retry_writeback(self, run: RunRecord) -> None:
        try:
            payload = json.loads(run.writeback_payload)
        except (TypeError, json.JSONDecodeError) as exc:
            detail = f"Invalid stored writeback payload: {exc}"
            self.state.update_status(run.bug_id, "writeback_exhausted", error=detail, completed=True)
            self.state.record_run_event(run.bug_id, "writeback_exhausted", detail)
            return
        self._writeback_one(run, payload)

    def _writeback_one(self, run: RunRecord, payload: Dict[str, str]) -> None:
        commit_summary = payload.get("commit_summary") or run.commit_hash
        self.state.record_run_event(run.bug_id, "comment_start", commit_summary)
        try:
            comment_bug(
                self.settings.zentao_client_script,
                run.bug_id,
                cause=payload["cause"],
                solution=payload["solution"],
            )
            self.state.record_run_event(run.bug_id, "comment_done", "")
        except (KeyError, ZenTaoWriteError) as exc:
            error = str(exc)
            self.state.record_run_event(run.bug_id, "comment_failed", error)
            self.state.update_status(
                run.bug_id,
                "writeback_failed",
                error=error,
                commit_hash=commit_summary,
                handled_once=True,
                completed=True,
            )
            LOGGER.error("Comment failed bug #%s after push: %s", run.bug_id, exc)
            return

        error = ""
        try:
            resolve_bug(self.settings.zentao_client_script, run.bug_id)
            self.state.record_run_event(run.bug_id, "resolve_done", "resolved/fixed")
        except ZenTaoResolveError as exc:
            error = str(exc)
            self.state.record_run_event(run.bug_id, "resolve_failed", error)
            LOGGER.error("Resolve failed bug #%s after push: %s", run.bug_id, exc)
        self.state.update_status(
            run.bug_id,
            "pushed" if not error else "writeback_failed",
            error=error,
            commit_hash=commit_summary,
            handled_once=True,
            completed=True,
        )


class _Checkout:
    def __init__(self, kind: str, repo_url: str, repo_cache: Path, worktree: Path, target_branch: str, baseline: str):
        self.kind = kind
        self.repo_url = repo_url
        self.repo_cache = repo_cache
        self.worktree = worktree
        self.target_branch = target_branch
        self.baseline = baseline

    @property
    def agent_committed(self) -> bool:
        """True when the agent ran git commit itself despite being told not to."""
        return head_commit(self.worktree) != self.baseline

    def has_work(self) -> bool:
        return has_changes(self.worktree) or self.agent_committed


def _verdict_platforms(verdict: Dict[str, Any]) -> tuple:
    """The agent answers android / ios / both / unknown; 'both' means it is not a single-platform bug."""
    platform = str(verdict.get("platform") or "").strip().lower()
    if not platform or platform in {"unknown", "both", "all"}:
        return ()
    return (platform,)


def _cause_text(verdict: Dict[str, Any], tail_title: str, tail_body: str) -> str:
    """Lead with what the AI understood and how it reproduced, so QA can tell it read the right bug."""
    parts = []
    understanding = verdict.get("understanding")
    if understanding:
        parts.append(f"【AI 理解的问题】\n{understanding}")
    steps = verdict.get("steps") or []
    if steps:
        numbered = "\n".join(step if step[:1].isdigit() else f"{index}. {step}" for index, step in enumerate(steps, 1))
        parts.append(f"【复现步骤】\n{numbered}")
    parts.append(f"【{tail_title}】\n{tail_body}")
    return "\n\n".join(parts)


def _solution_text(verdict: Dict[str, Any], commit_summary: str) -> str:
    parts = [verdict.get("solution") or "已提交修复。"]
    targets = verdict.get("targets") or []
    if targets:
        parts.append(f"改动仓库：{'、'.join(targets)}")
    platform = verdict.get("platform")
    if platform and platform != "unknown":
        parts.append(f"复现端：{platform}")
    if verdict.get("verification_passed") and verdict.get("verification_command"):
        parts.append(f"测试：{verdict['verification_command']}")
    parts.append(f"提交：{commit_summary}")
    return "\n".join(parts)


def _still_running(state: StateStore, bug_id: int) -> bool:
    """Bugs already rejected or pushed keep their outcome when a later step blows up."""
    current = state.get_run(bug_id)
    return bool(current and current.status == "running")


def _looks_like_non_fast_forward(error: str) -> bool:
    lowered = error.lower()
    markers = ("non-fast-forward", "fetch first", "stale info", "rejected")
    return any(marker in lowered for marker in markers)


def _batch_label(batch: List[RunRecord]) -> str:
    return " ".join(f"#{run.bug_id}" for run in batch)
