from __future__ import annotations

import contextlib
import json
import logging
import queue
import re
import threading
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

from .agent_runner import (
    AgentCredentialError,
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
    push_merge_request,
    push_head_to_branch,
    rebase_onto_latest_remote,
    remove_worktree,
    repo_cache_name,
    reset_hard_clean,
    run_git,
)
from .cable_release import check_cable_release, is_cable_release
from .gitlab import GitLab, GitLabError, required_jobs_pass, required_jobs_present
from .models import ProjectConfig, RunRecord, has_manual_tag, has_ui_tag, platforms_of
from .state import StateStore
from .zentao import (
    ZenTaoPollError,
    ZenTaoResolveError,
    ZenTaoWriteError,
    add_comment,
    bug_fresh_title,
    bug_is_still_actionable,
    bug_view_url,
    comment_bug,
    resolve_bug,
)


LOGGER = logging.getLogger("zentao_auto_fixer.worker")

_FOLLOWING_START_TEXT = (
    "【AI 自动跟进】本 Bug 已被 AI 自动修复任务接管，正在处理中，请勿人工介入；"
    "若开始 1 小时后仍未完成，可人工介入处理。"
)
_FOLLOWING_STILL_ACTIVE = {"queued", "running", "awaiting_merge", "awaiting_release"}
_FOLLOWING_DONE_TEXT = {
    "pushed": "修复成功，已提交交付",
    "failed": "处理失败",
    "skipped_ui": "跳过（UI 问题）",
    "skipped_manual": "跳过（人工处理）",
    "skipped_stale": "跳过（无需处理）",
    "skipped_platform": "跳过（平台不在配置内）",
    "unable_to_fix": "AI 无法自动修复",
    "merge_request_failed": "合并请求失败，需人工处理",
    "writeback_failed": "修复已推送，但禅州备注回写失败",
    "retry_exhausted": "重试次数已耗尽",
    "writeback_exhausted": "备注重试次数已耗尽",
    "no_changes": "AI 未产出代码改动",
    "manual_required": "需人工处理",
    "sync_conflict": "仓库同步冲突",
    "handled_in_zentao": "已在禅州处理过",
    "rejected_to_reporter": "已退回提单人",
}


class Worker:
    def __init__(self, settings: Settings, state: StateStore):
        self.settings = settings
        self.state = state
        self.queue: "queue.PriorityQueue[int]" = queue.PriorityQueue()
        self._queued_ids = set()
        self._queue_guard = threading.Lock()
        self.dispatch_lock = threading.Lock()
        self._threads: List[threading.Thread] = []
        self._stop = threading.Event()
        self._repo_locks: Dict[str, threading.Lock] = {}
        self._repo_locks_guard = threading.Lock()
        self._budget_guard = threading.Lock()
        self._agent_runs_day = ""
        self._agent_runs_today = 0
        self._credential_pause_until = 0.0

    def start(self) -> None:
        if self._threads:
            return
        for bug_id in self.state.queued_bug_ids():
            self.enqueue(bug_id)
        for index in range(self.settings.worker_count):
            thread = threading.Thread(target=self._run, name=f"auto-fixer-worker-{index + 1}", daemon=True)
            thread.start()
            self._threads.append(thread)

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
            with self.dispatch_lock:
                try:
                    bug_id = self.queue.get_nowait()
                except queue.Empty:
                    bug_id = None
            if bug_id is None:
                self._stop.wait(0.5)
                continue
            if bug_id < 0:
                continue
            try:
                self._process_bug(bug_id)
            except Exception as exc:
                run = self.state.get_run(bug_id)
                if run and (run.status == "running" or run.status == "queued"):
                    self._fail_batch([run], "failed", _friendly_error(exc), "")
                LOGGER.exception("Worker failed bug #%s", bug_id)
            finally:
                with self._queue_guard:
                    self._queued_ids.discard(bug_id)
                self.queue.task_done()
                self._note_following_done(bug_id)

    def _note_following_started(self, batch: List[RunRecord]) -> None:
        """Announce on ZenTao that the AI took this bug over; humans should wait up to an hour.

        The note is an auxiliary signal: any failure here must never block the repair.
        """
        for run in batch:
            if self._has_open_following_note(run.bug_id):
                LOGGER.info("Bug #%s already carries an open following-start note; not posting another", run.bug_id)
                continue
            try:
                add_comment(self.settings.zentao_client_script, run.bug_id, _FOLLOWING_START_TEXT)
                self.state.record_run_event(run.bug_id, "following_started", "")
            except Exception as exc:
                self.state.record_run_event(run.bug_id, "following_start_failed", str(exc))
                LOGGER.warning("Could not post following-start note on bug #%s: %s", run.bug_id, exc)

    def _has_open_following_note(self, bug_id: int) -> bool:
        """True when a following-start note was posted and no following-done note closed it yet.

        A service restart requeues running bugs and re-runs them; without this gate every
        recovery would post another identical start note on the same bug.
        """
        last_start = last_done = -1
        for index, event in enumerate(self.state.list_run_events(bug_id)):
            name = event.get("event") if isinstance(event, dict) else ""
            if name == "following_started":
                last_start = index
            elif name == "following_done":
                last_done = index
        return last_start > last_done

    def _note_following_done(self, bug_id: int) -> None:
        """Close the loop after every consumed bug; a note goes out whatever the outcome was."""
        run = self.state.get_run(bug_id)
        if not run or run.status in _FOLLOWING_STILL_ACTIVE:
            return
        text = f"【AI 跟进完成】AI 已结束对本 Bug 的跟进，本轮结果：{_FOLLOWING_DONE_TEXT.get(run.status, run.status)}。"
        if getattr(run, "error", ""):
            text += f"\n原因：{_summarize_error(str(run.error))}"
        try:
            add_comment(self.settings.zentao_client_script, run.bug_id, text)
            self.state.record_run_event(run.bug_id, "following_done", run.status)
        except Exception as exc:
            self.state.record_run_event(run.bug_id, "following_done_failed", str(exc))
            LOGGER.warning("Could not post following-done note on bug #%s: %s", bug_id, exc)

    def _process_bug(self, bug_id: int) -> None:
        run = self.state.get_run(bug_id)
        if not run:
            return
        if run.status in {"awaiting_merge", "awaiting_release", "merge_request_failed"}:
            self._check_merge_requests(run)
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

        if not project.process_ui_bugs and has_ui_tag(self._fresh_title_or_old(run)):
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
        if has_manual_tag(self._fresh_title_or_old(run)):
            message = "标题带有人工标签，未调用 AI。"
            self.state.update_status(
                bug_id,
                "skipped_manual",
                error=message,
                handled_once=False,
                completed=True,
            )
            self.state.record_run_event(bug_id, "skipped_manual", message)
            LOGGER.info("Bug #%s skipped because its title carries a manual tag", bug_id)
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

    def _fresh_title_or_old(self, run: RunRecord) -> str:
        """Humans edit titles (e.g. add a 【ui】 tag) after the bug was queued; re-read before the UI gate."""
        try:
            return bug_fresh_title(self.settings.zentao_client_script, run.bug_id) or run.title
        except ZenTaoPollError as exc:
            LOGGER.warning("Could not re-read bug #%s title before the UI check: %s", run.bug_id, exc)
            return run.title

    def _stale_reason(self, run: RunRecord) -> str:
        """A queued bug can sit for hours or survive a restart; re-check ZenTao before spending an agent run."""
        try:
            actionable, reason = bug_is_still_actionable(
                self.settings.zentao_client_script,
                run.bug_id,
                ignore_ai_comment=getattr(run, "event_action", "") == "manual_retry",
            )
        except ZenTaoPollError as exc:
            LOGGER.warning("Could not re-check bug #%s before fixing it: %s", run.bug_id, exc)
            return f"Could not confirm the bug is still active: {exc}"
        return "" if actionable else reason

    def _claim_agent_budget(self) -> bool:
        """One batch costs one agent run. The ceiling is the backstop against a runaway poll loop."""
        if time.time() < self._credential_pause_until:
            return False
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
        if time.time() < self._credential_pause_until:
            return "AI 服务凭证失效或余额不足，暂停新任务；恢复后会自动继续。"
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
        batch = self.state.claim_queued_batch(leader_bug_id, limit=1)
        if not batch:
            return
        fresh_titles = {run.bug_id: self._fresh_title_or_old(run) for run in batch}
        skipped_ui = [
            run for run in batch
            if has_ui_tag(fresh_titles[run.bug_id]) and not project.process_ui_bugs
        ]
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
        skipped_manual = [run for run in batch if has_manual_tag(fresh_titles[run.bug_id])]
        for run in skipped_manual:
            message = "标题带有人工标签，未调用 AI。"
            self.state.update_status(
                run.bug_id,
                "skipped_manual",
                error=message,
                handled_once=False,
                completed=True,
            )
            self.state.record_run_event(run.bug_id, "skipped_manual", message)
        batch = [run for run in batch if run not in skipped_manual]
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
        self._note_following_started(batch)

        backend_repo = (project.backend_repo_url, project.backend_target_branch) if project.has_backend_repo else None
        checkouts: Dict[str, _Checkout] = {}
        try:
            checkouts["app"] = self._prepare_checkout(bug_ids, "app", first.repo_url, first.target_branch, batch_label)
            if backend_repo:
                checkouts["backend"] = self._prepare_checkout(
                    bug_ids, "backend", backend_repo[0], backend_repo[1], batch_label
                )

            checkouts["app"].delivery_mode = project.delivery_mode
            if "backend" in checkouts:
                checkouts["backend"].delivery_mode = project.backend_delivery_mode

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
        except AgentCredentialError as exc:
            unfinished = [run for run in batch if _still_running(self.state, run.bug_id)]
            # Auth/balance failures are not the AI's fault: don't count them toward the
            # no-progress fuse, and clear earlier counts so a restored token resumes work.
            # Pause new starts for a while so a dead token does not spam failure notes per retry.
            self._fail_batch(unfinished, "failed", _friendly_error(exc), "", count_no_progress=False)
            self._record_progress()
            self._credential_pause_until = time.time() + 900
            LOGGER.exception("Worker failed batch %s on a credential error", batch_label)
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
                self._fail_batch(unfinished, "failed", _friendly_error(exc), "", count_no_progress=True)
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
            except AgentCredentialError as exc:
                last_error = exc
                self.state.record_run_events(
                    bug_ids,
                    "agent_attempt_failed",
                    f"{agent} {attempt}/{self.settings.codex_attempts}: {exc}",
                )
                break  # retrying cannot fix a dead token
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

        fixed = [run for run in fixed if self._still_actionable_before_writeback(run)]
        if not fixed:
            LOGGER.info("Worker skipped batch %s: every bug was handled in ZenTao while the agent ran", batch_label)
            return

        commit_message = _commit_message(fixed, verdicts)
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
                if checkout.delivery_mode == "merge_request":
                    continue
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
        merge_requests: List[Dict[str, str]] = []
        for checkout in changed:
            while True:
                try:
                    self.state.record_run_events(bug_ids, f"push_start_{checkout.kind}", checkout.target_branch)
                    if checkout.delivery_mode == "merge_request":
                        source = f"feature/zentao-{bug_ids[0]}-{bug_ids[-1]}-{head_commit(checkout.worktree)[:12]}"
                        url = push_merge_request(checkout.worktree, source, checkout.target_branch, commit_message)
                        merge_requests.append({"url": url, "source": source, "target": checkout.target_branch,
                                               "repo_url": checkout.repo_url, "kind": checkout.kind,
                                               "sha": head_commit(checkout.worktree)})
                        self.state.record_run_events(bug_ids, f"merge_request_created_{checkout.kind}", url)
                    else:
                        push_head_to_branch(checkout.worktree, checkout.target_branch)
                    break
                except GitError as exc:
                    if checkout.delivery_mode == "push" and _looks_like_non_fast_forward(str(exc)):
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
                    if checkout.delivery_mode == "merge_request":
                        patch = self._save_conflict_patch(checkout, batch_label)
                        detail += f"\n已保留修复，停止自动重跑 AI；补丁：{patch}"
                        self._fail_batch(fixed, "merge_request_failed", detail, " ".join(commits))
                    else:
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
            if checkout.delivery_mode == "push":
                self.state.record_run_events(bug_ids, f"pushed_{checkout.kind}", checkout.target_branch)

        commits = [f"{checkout.kind}:{head_commit(checkout.worktree)}" for checkout in changed]
        commit_summary = " ".join(commits)
        self._record_progress()
        self._comment_and_resolve(fixed, verdicts, commit_summary, merge_requests)
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
                    except AgentCredentialError:
                        raise
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
        merge_requests: Optional[List[Dict[str, str]]] = None,
    ) -> None:
        for run in fixed:
            verdict = verdicts[run.bug_id]
            payload = {
                "cause": _cause_text(verdict, "原因分析", verdict.get("cause") or "见提交记录。"),
                "solution": _solution_text(verdict, commit_summary),
                "commit_summary": commit_summary,
            }
            if merge_requests:
                payload["delivery_status"] = "awaiting_merge"
                payload["solution"] += "\n修复代码已提 MR，等待 CI 与自动合并，尚未完成交付：\n" + "\n".join(mr["url"] for mr in merge_requests)
                payload["merge_requests"] = merge_requests
                payload["verdict"] = verdict
                payload["ci_attempts"] = 0
                payload["ci_wait_started"] = time.time()
            self.state.set_writeback_payload(run.bug_id, json.dumps(payload, ensure_ascii=False))
            if merge_requests:
                self.state.update_status(run.bug_id, "awaiting_merge", error="", commit_hash=commit_summary,
                                         handled_once=True, completed=True)
                self.state.record_run_event(run.bug_id, "awaiting_merge", payload["solution"])
            else:
                self._writeback_one(run, payload)

    def _check_merge_requests(self, run: RunRecord) -> None:
        """Existing poller checks CI without starting a model while nothing needs repair."""
        try:
            project = self._project_for(run)
            if project is None or not project.enabled:
                return
            payload = json.loads(run.writeback_payload)
            if "ci_wait_started" not in payload:
                payload["ci_wait_started"] = time.time()
                self.state.set_writeback_payload(run.bug_id, json.dumps(payload, ensure_ascii=False))
            requests = payload["merge_requests"]
            if not requests:
                raise GitLabError("Missing MR delivery records")
            all_merged = True
            for item in requests:
                client = GitLab(item["url"])
                if is_cable_release(item):
                    complete, stage = check_cable_release(
                        client, item, payload, self.settings.data_dir, run.bug_id,
                        repair_failed=lambda logs: self._repair_merge_request(run, payload, item, logs))
                    if not complete:
                        all_merged = False
                        self.state.set_writeback_payload(run.bug_id, json.dumps(payload, ensure_ascii=False))
                        self.state.update_status(run.bug_id, "awaiting_merge", error=stage,
                                                 commit_hash=run.commit_hash, handled_once=True, completed=True)
                        if stage != run.error:
                            self.state.record_run_event(run.bug_id, "awaiting_merge", stage)
                    continue
                mr, jobs = client.inspect(item["sha"], item["target"])
                if mr.get("state") == "merged":
                    if not required_jobs_pass(jobs) or (mr.get("head_pipeline") or {}).get("status") != "success":
                        raise GitLabError("MR merged without verified required CI jobs on the recorded head")
                    # GitLab removes the feature branch as part of the merge; verify the result.
                    if client.source_branch_exists(item["source"]):
                        raise GitLabError("MR merged but its feature branch still exists")
                    continue
                all_merged = False
                if mr.get("state") != "opened":
                    raise GitLabError("MR closed without merging")
                pipeline = mr.get("head_pipeline") or {}
                if pipeline.get("sha") != item["sha"] or not jobs:
                    ended_empty = pipeline.get("sha") == item["sha"] and pipeline.get("status") in {"success", "failed", "skipped", "canceled"}
                    timed_out = time.time() - payload["ci_wait_started"] > self.settings.git_timeout_seconds
                    if ended_empty or timed_out:
                        raise GitLabError("No verifiable CI jobs for this MR head; pipeline missing or empty")
                    continue
                if not required_jobs_present(jobs):
                    raise GitLabError("CI must contain mandatory lint, unit-test, build and integration jobs; auto merge was not enabled")
                if pipeline.get("status") == "failed":
                    self._repair_merge_request(run, payload, item, client.failed_logs(jobs))
                    return
                if pipeline.get("status") in {"canceled", "skipped", "manual"}:
                    raise GitLabError("CI was canceled, skipped or needs manual action; automatic delivery stopped")
                if any(job.get("status") in {"manual", "skipped"} for job in jobs):
                    raise GitLabError("CI contains manual or skipped jobs; auto merge was not enabled")
                if not mr.get("merge_when_pipeline_succeeds") and not mr.get("auto_merge_enabled"):
                    client.enable_auto_merge(item["sha"])
                    self.state.record_run_event(run.bug_id, "auto_merge_enabled", item["url"])
            if all_merged:
                payload.pop("delivery_status", None)
                payload["solution"] += ("\n修复测试及必需 CI 已通过，MR 已合入 pre_release，修复交付完成。"
                                        if any(is_cable_release(item) for item in requests)
                                        else "\n必需 CI 全部通过，MR 已合并，feature 源分支已删除。")
                self.state.set_writeback_payload(run.bug_id, json.dumps(payload, ensure_ascii=False))
                self._writeback_one(run, payload)
                self._record_progress()
        except (GitLabError, GitError, AgentError, KeyError, ValueError, OSError) as exc:
            if run.status != "merge_request_failed" or run.error != str(exc):
                self._fail_batch([run], "merge_request_failed", str(exc), run.commit_hash)

    def _repair_merge_request(self, run, payload, item, failure):
        attempts = payload.get("ci_attempts", 0)
        if attempts >= self.settings.max_bug_retries:
            raise GitLabError("CI repair attempt limit reached; MR remains unmerged")
        if not self._claim_agent_budget():
            self.state.record_run_event(run.bug_id, "ci_budget_exhausted", self._agent_budget_block_reason())
            return
        project = self._project_for(run)
        if project is None:
            raise GitLabError("Project config missing for CI repair")
        payload["ci_attempts"] = attempts + 1
        self.state.set_writeback_payload(run.bug_id, json.dumps(payload, ensure_ascii=False))
        checkouts = {}
        label = f"#{run.bug_id} CI {attempts + 1}"
        try:
            # Fetch the existing feature head, so every retry updates the same MR without force push.
            checkouts[item["kind"]] = self._prepare_checkout(
                [run.bug_id], item["kind"], item["repo_url"], item["source"], label)
            checkout = checkouts[item["kind"]]
            if checkout.baseline != item["sha"]:
                raise GitLabError("Feature branch changed outside this task")
            if "app" not in checkouts:
                checkouts["app"] = self._prepare_checkout([run.bug_id], "app", project.repo_url, project.target_branch, label)
            path = self.settings.logs_dir / f"bug-{run.bug_id}-ci-{attempts + 1}"
            self.state.record_run_event(run.bug_id, "ci_repair_start", item["url"])
            verdicts = run_agent_batch_fix(
                project.agent, self.settings.agent_bin(project.agent), self.settings.zentao_client_script,
                checkouts["app"].worktree,
                checkouts["backend"].worktree if "backend" in checkouts else None,
                [(run.bug_id, run.title)], path.with_suffix(".json"), path.with_suffix(".log"),
                timeout_seconds=self.settings.codex_timeout_seconds,
                allow_full_xcodebuild=project.allow_full_xcodebuild, ci_context=failure,
            )
            verdict = verdicts[run.bug_id]
            if verdict["decision"] != "fixed" or not checkout.has_work():
                raise GitLabError("Agent did not produce a verified CI repair")
            if any(other.has_work() for other in checkouts.values() if other is not checkout):
                raise GitLabError("CI repair changed a repository outside this MR")
            # The repair agent cannot turn a red pipeline green by changing the gate itself.
            files = run_git(["diff", "--name-only", checkout.baseline], cwd=checkout.worktree).splitlines()
            if any(f == ".gitlab-ci.yml" or f.startswith((".gitlab/", "scripts/ci")) for f in files):
                raise GitLabError("CI repair modified pipeline gates; independent review required")
            if has_changes(checkout.worktree):
                commit_all(checkout.worktree, _commit_message([run], verdicts),
                           self.settings.git_author_name, self.settings.git_author_email)
            with self._lock_for_repo(checkout.repo_url):
                push_head_to_branch(checkout.worktree, item["source"])
            item["sha"] = head_commit(checkout.worktree)
            payload["verdict"] = verdict
            payload["ci_wait_started"] = time.time()
            commits = dict(value.split(":", 1) for value in payload["commit_summary"].split())
            commits[item["kind"]] = item["sha"]
            payload["commit_summary"] = " ".join(f"{kind}:{sha}" for kind, sha in commits.items())
            payload["solution"] = _solution_text(verdict, payload["commit_summary"]) + "\n等待 MR CI 和自动合并：" + item["url"]
            self.state.set_writeback_payload(run.bug_id, json.dumps(payload, ensure_ascii=False))
            self.state.update_status(run.bug_id, "awaiting_merge", commit_hash=payload["commit_summary"], error="")
            self.state.record_run_event(run.bug_id, "ci_repair_pushed", item["sha"])
        except Exception:
            for checkout in checkouts.values():
                self._save_conflict_patch(checkout, label)
            raise
        finally:
            for checkout in checkouts.values():
                with self._lock_for_repo(checkout.repo_url):
                    remove_worktree(checkout.repo_cache, checkout.worktree)

    def _retry_writeback(self, run: RunRecord) -> None:
        try:
            payload = json.loads(run.writeback_payload)
        except (TypeError, json.JSONDecodeError) as exc:
            detail = f"Invalid stored writeback payload: {exc}"
            self.state.update_status(run.bug_id, "writeback_exhausted", error=detail, completed=True)
            self.state.record_run_event(run.bug_id, "writeback_exhausted", detail)
            return
        self._writeback_one(run, payload)

    def _still_actionable_before_writeback(self, run: RunRecord) -> bool:
        """The agent may have run for an hour; a human could have resolved or deleted the bug meanwhile."""
        try:
            actionable, reason = bug_is_still_actionable(
                self.settings.zentao_client_script,
                run.bug_id,
                ignore_ai_comment=getattr(run, "event_action", "") == "manual_retry",
            )
        except ZenTaoPollError as exc:
            # ponytail: fail-open — a transient read error must not eat a finished fix;
            # the human-race window it leaves is seconds, not the hour this check removes.
            LOGGER.warning("Could not re-check bug #%s before writing back: %s", run.bug_id, exc)
            return True
        if not actionable:
            self.state.update_status(run.bug_id, "skipped_stale", error=reason, completed=True)
            self.state.record_run_event(run.bug_id, "skipped_stale", reason)
            LOGGER.info("Bug #%s was handled in ZenTao while the agent ran (%s); dropping the fix", run.bug_id, reason)
            return False
        return True

    def _writeback_one(self, run: RunRecord, payload: Dict[str, str]) -> None:
        commit_summary = payload.get("commit_summary") or run.commit_hash
        if not self._still_actionable_before_writeback(run):
            return
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

        if payload.get("delivery_status") == "awaiting_merge":
            self.state.update_status(run.bug_id, "awaiting_merge", error="", commit_hash=commit_summary,
                                     handled_once=True, completed=True)
            self.state.record_run_event(run.bug_id, "awaiting_merge", payload["solution"])
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
    def __init__(self, kind: str, repo_url: str, repo_cache: Path, worktree: Path, target_branch: str, baseline: str, delivery_mode: str = "push"):
        self.kind = kind
        self.repo_url = repo_url
        self.repo_cache = repo_cache
        self.worktree = worktree
        self.target_branch = target_branch
        self.baseline = baseline
        self.delivery_mode = delivery_mode

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
    parts.append(verdict.get("ai_info") or "AI / 模型：未确认（无运行时记录）")
    return "\n".join(parts)


def _still_running(state: StateStore, bug_id: int) -> bool:
    """Bugs already rejected or pushed keep their outcome when a later step blows up."""
    current = state.get_run(bug_id)
    return bool(current and current.status == "running")


def _summarize_error(detail: str, limit: int = 120) -> str:
    """Compress a long error into its gist: the lead sentence plus what is still missing."""
    text = " ".join((detail or "").split())
    if len(text) <= limit:
        return text
    reason, _, missing = text.partition("需要补充：")
    lead = re.split(r"[。；]", reason, maxsplit=1)[0].strip()
    if not lead:
        lead = reason[: limit - 10].rstrip()
    summary = lead
    if missing:
        need = re.split(r"[。；，,]", missing, maxsplit=1)[0].strip()
        summary += f"；需补充：{need}"
    if len(summary) > limit:
        summary = summary[:limit].rstrip()
    return summary


def _friendly_error(exc: Exception) -> str:
    """Human-readable Chinese summary for ZenTao notes; raw agent output stays in the event log."""
    if isinstance(exc, AgentCredentialError):
        return "AI 服务凭证失效或余额不足（token 过期或账户欠费），本轮未实际调用 AI；恢复后会自动重试。"
    if isinstance(exc, AgentQuotaError):
        return "AI 调用配额已用尽，今日无法继续自动修复。"
    if isinstance(exc, AgentError):
        if "timed out" in str(exc):
            return "AI 处理超时，本轮未完成修复。"
        return "AI 引擎执行失败，详情见任务流水。"
    return str(exc)


def _looks_like_non_fast_forward(error: str) -> bool:
    lowered = error.lower()
    markers = ("non-fast-forward", "fetch first", "stale info")
    return any(marker in lowered for marker in markers)


def _batch_label(batch: List[RunRecord]) -> str:
    return " ".join(f"#{run.bug_id}" for run in batch)


def _commit_message(fixed: List[RunRecord], verdicts: Dict[int, Dict[str, Any]]) -> str:
    """Conventional-commit style with the ZenTao bug id as scope: fix(7499): 具体修复的内容."""
    if len(fixed) == 1:
        run = fixed[0]
        return f"fix({run.bug_id}): {_fix_summary(verdicts.get(run.bug_id, {}), run.title)}"
    ids = ",".join(str(run.bug_id) for run in fixed)
    summary = "；".join(_fix_summary(verdicts.get(run.bug_id, {}), run.title) for run in fixed)
    lines = [f"fix({ids}): {summary}"]
    for run in fixed:
        lines.append(f"- #{run.bug_id} {_fix_summary(verdicts.get(run.bug_id, {}), run.title)}")
    return "\n".join(lines)


def _fix_summary(verdict: Dict[str, Any], title: str) -> str:
    """One line describing the actual fix, for the commit subject — not the raw ZenTao title."""
    text = (verdict.get("solution") or verdict.get("understanding") or title or "").strip()
    text = " ".join(text.split())
    limit = 72
    if len(text) > limit:
        text = text[: limit - 1].rstrip() + "…"
    return text or "修复问题"
