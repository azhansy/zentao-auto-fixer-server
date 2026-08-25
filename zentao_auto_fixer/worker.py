from __future__ import annotations

import contextlib
import logging
import queue
import threading
import time
import traceback
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

from .agent_runner import AgentError, TriageResultError, run_agent_batch_fix
from .config import Settings
from .git_ops import (
    GitError,
    changed_files,
    commit_all,
    create_detached_worktree,
    ensure_repo_cache,
    has_changes,
    head_commit,
    push_head_dry_run,
    push_head_to_branch,
    remove_worktree,
    repo_cache_name,
    reset_hard_clean,
)
from .models import ProjectConfig, RunRecord, platforms_of
from .state import StateStore
from .zentao import (
    ZenTaoPollError,
    ZenTaoResolveError,
    ZenTaoWriteError,
    assign_bug,
    bug_is_still_actionable,
    comment_bug,
    resolve_bug,
)


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
                if _still_running(self.state, bug_id) or _is_queued(self.state, bug_id):
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

        stale = self._stale_reason(bug_id)
        if stale:
            self.state.update_status(bug_id, "skipped_stale", error=stale, completed=True)
            self.state.record_run_event(bug_id, "skipped_stale", stale)
            LOGGER.info("Bug #%s no longer needs fixing: %s", bug_id, stale)
            return

        titled_platforms = platforms_of(run.title)
        if len(titled_platforms) > 1:
            self._reject_multi_platform(run, titled_platforms)
            return

        if not self._claim_agent_budget():
            self.state.record_run_event(
                bug_id,
                "agent_budget_exhausted",
                f"Hit the {self.settings.max_agent_runs_per_day} agent runs/day ceiling; "
                "staying queued until tomorrow or a restart.",
            )
            LOGGER.warning(
                "Bug #%s stays queued: already used today's %s agent runs",
                bug_id,
                self.settings.max_agent_runs_per_day,
            )
            return

        project = self._project_for(run)
        if project is None:
            message = f"Project {run.project_name!r} is missing from the project config; not guessing its repos."
            self.state.update_status(bug_id, "failed", error=message, completed=True)
            self.state.record_run_event(bug_id, "failed", message)
            LOGGER.error("Worker cannot start bug #%s: %s", bug_id, message)
            return
        repo_urls = [run.repo_url]
        if project.has_backend_repo:
            repo_urls.append(project.backend_repo_url)
        self.state.record_run_event(bug_id, "waiting_repo_lock", " ".join(repo_urls))
        with contextlib.ExitStack() as stack:
            # Sorted so two workers touching the same pair of repos cannot deadlock on each other.
            for repo_url in sorted(set(repo_urls)):
                stack.enter_context(self._lock_for_repo(repo_url))
            self._process_batch_with_repo_lock(bug_id, project)

    def _reject_multi_platform(self, run: RunRecord, platforms: tuple) -> None:
        """One bug must describe one platform; a multi-platform title cannot be pinned to code."""
        named = "、".join(platforms)
        self.state.record_run_event(run.bug_id, "multi_platform", named)
        LOGGER.info("Bug #%s names several platforms (%s), handing it back", run.bug_id, named)
        self._reject_to_reporter(
            run,
            {
                "understanding": f"这条 Bug 的标题同时标注了 {named} 多个端。",
                "steps": [],
                "reason": "一条 Bug 只能描述一个端的问题。同时标注多个端时，无法确定要定位和修改哪一端的代码。",
                "missing": f"请按端拆成多条 Bug（{named} 各一条），每条只写该端的复现步骤和现象。",
            },
        )

    def _stale_reason(self, bug_id: int) -> str:
        """A queued bug can sit for hours or survive a restart; re-check ZenTao before spending an agent run."""
        try:
            actionable, reason = bug_is_still_actionable(self.settings.zentao_client_script, bug_id)
        except ZenTaoPollError as exc:
            LOGGER.warning("Could not re-check bug #%s before fixing it: %s", bug_id, exc)
            return f"Could not confirm the bug is still active: {exc}"
        return "" if actionable else reason

    def _claim_agent_budget(self) -> bool:
        """One batch costs one agent run. The ceiling is the backstop against a runaway poll loop."""
        today = datetime.now(timezone.utc).date().isoformat()
        with self._budget_guard:
            if self._agent_runs_day != today:
                self._agent_runs_day = today
                self._agent_runs_today = 0
            if self._agent_runs_today >= self.settings.max_agent_runs_per_day:
                return False
            self._agent_runs_today += 1
            return True

    def agent_runs_today(self) -> int:
        with self._budget_guard:
            return self._agent_runs_today

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

    def _process_batch_with_repo_lock(self, leader_bug_id: int, project: ProjectConfig) -> None:
        batch = self.state.claim_queued_batch(leader_bug_id, limit=project.max_bugs_per_poll)
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
            verdicts = self._run_agent_batch_with_retries(batch, agent, checkouts, result_path, agent_log)
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
                self._reject_to_reporter(run, verdicts[run.bug_id])
            if not fixed:
                LOGGER.info("Worker finished batch %s with nothing to commit", batch_label)
                return
            self._commit_push_and_resolve(fixed, checkouts, verdicts, batch_label)
        except Exception as exc:
            error = f"{exc}\n{traceback.format_exc()}"
            unfinished = [run.bug_id for run in batch if _still_running(self.state, run.bug_id)]
            self.state.update_statuses(unfinished, "failed", error=error, handled_once=True, completed=True)
            self.state.record_run_events(unfinished, "failed", str(exc))
            LOGGER.error("Worker failed batch %s: %s", batch_label, exc)
        finally:
            for checkout in checkouts.values():
                try:
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

    def _reject_to_reporter(self, run: RunRecord, verdict: Dict[str, Any]) -> None:
        """Hand a bug we could not diagnose back to whoever opened it, leaving it active."""
        reason = verdict.get("reason") or "AI 无法从当前 Bug 描述定位到问题。"
        missing = verdict.get("missing") or "请补充复现步骤、测试账号、出现时间和截图或日志。"
        self.state.record_run_event(run.bug_id, "reject_start", reason)
        try:
            comment_bug(
                self.settings.zentao_client_script,
                run.bug_id,
                cause=_cause_text(verdict, "无法定位的原因", reason),
                solution=f"请补充以下信息后重新指派给开发：{missing}",
            )
            self.state.record_run_event(run.bug_id, "reject_comment_done", missing)
        except Exception as exc:
            error = str(exc)
            self.state.record_run_event(run.bug_id, "reject_comment_failed", error)
            LOGGER.error("Could not comment the hand-back of bug #%s: %s", run.bug_id, error)
            self.state.update_status(run.bug_id, "failed", error=error, handled_once=True, completed=True)
            return

        # The note is what the reporter actually reads; a failed re-assign only means it
        # stays on the AI account, so keep the hand-back and flag the assign separately.
        assign_error = ""
        try:
            assign_bug(run.bug_id, run.opened_by)
            self.state.record_run_event(run.bug_id, "reject_assigned", run.opened_by)
        except Exception as exc:
            assign_error = str(exc)
            self.state.record_run_event(run.bug_id, "reject_assign_failed", assign_error)
            LOGGER.error("Commented bug #%s but could not assign it to %s: %s", run.bug_id, run.opened_by, assign_error)
        self.state.update_status(
            run.bug_id,
            "rejected_to_reporter",
            error=f"指派回 {run.opened_by} 失败，Bug 仍挂在 AI 账号上：{assign_error}" if assign_error else "",
            handled_once=True,
            completed=True,
        )

    def _fail_batch_and_notify(
        self,
        batch: List[RunRecord],
        status: str,
        detail: str,
        commit_hash: str,
    ) -> None:
        """Failures must not go quiet: say so on the bug and get it off the AI account."""
        for run in batch:
            note_error = ""
            try:
                comment_bug(
                    self.settings.zentao_client_script,
                    run.bug_id,
                    cause=f"AI 自动修复未能完成（{status}）：{detail}",
                    solution="这条 Bug 需要人工接手，AI 不会再自动处理。",
                )
                self.state.record_run_event(run.bug_id, "failure_comment_done", status)
                if run.opened_by:
                    assign_bug(run.bug_id, run.opened_by)
                    self.state.record_run_event(run.bug_id, "failure_assigned", run.opened_by)
            except Exception as exc:
                note_error = str(exc)
                self.state.record_run_event(run.bug_id, "failure_notify_failed", note_error)
                LOGGER.error("Could not report the failure of bug #%s to ZenTao: %s", run.bug_id, exc)
            self.state.update_status(
                run.bug_id,
                status,
                error=f"{detail}{'; 禅道回写失败: ' + note_error if note_error else ''}",
                commit_hash=commit_hash,
                handled_once=True,
                completed=True,
            )

    def _commit_push_and_resolve(
        self,
        fixed: List[RunRecord],
        checkouts: Dict[str, "_Checkout"],
        verdicts: Dict[int, Dict[str, Any]],
        batch_label: str,
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
            self.state.update_statuses(
                bug_ids,
                "no_changes",
                error="The agent reported fixes but no git changes were produced",
                handled_once=True,
                completed=True,
            )
            self.state.record_run_events(bug_ids, "no_changes", "The agent reported fixes but no git changes were produced")
            LOGGER.info("Worker finished batch %s with no changes", batch_label)
            return

        commit_message = f"fix: zentao batch {batch_label}"
        commits: List[str] = []
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
            commits.append(f"{checkout.kind}:{commit_hash}")
            self.state.record_run_events(bug_ids, f"commit_done_{checkout.kind}", commit_hash)

        # Dry-run every repository first: a rejection here costs nothing, a rejection
        # half-way through a multi-repo push leaves the fix split across branches.
        for checkout in changed:
            try:
                self.state.record_run_events(bug_ids, f"push_check_{checkout.kind}", checkout.target_branch)
                push_head_dry_run(checkout.worktree, checkout.target_branch)
            except GitError as exc:
                status = "sync_conflict" if _looks_like_non_fast_forward(str(exc)) else "failed"
                self._fail_batch_and_notify(fixed, status, f"{checkout.kind} push check: {exc}", "")
                LOGGER.error("Worker push check failed batch %s repo=%s: %s", batch_label, checkout.kind, exc)
                return

        pushed: List[str] = []
        for checkout in changed:
            try:
                self.state.record_run_events(bug_ids, f"push_start_{checkout.kind}", checkout.target_branch)
                push_head_to_branch(checkout.worktree, checkout.target_branch)
            except GitError as exc:
                status = "sync_conflict" if _looks_like_non_fast_forward(str(exc)) else "failed"
                detail = f"{checkout.kind}: {exc}"
                if pushed:
                    detail = (
                        f"仓库 {'、'.join(pushed)} 的修复已经推送（{' '.join(commits)}），"
                        f"但 {checkout.kind} 推送失败，修复只落地了一半，需要人工处理：{exc}"
                    )
                self._fail_batch_and_notify(fixed, status, detail, " ".join(commits))
                LOGGER.error(
                    "Worker push failed batch %s repo=%s status=%s (already pushed: %s): %s",
                    batch_label,
                    checkout.kind,
                    status,
                    pushed or "none",
                    exc,
                )
                return
            pushed.append(checkout.kind)
            self.state.record_run_events(bug_ids, f"pushed_{checkout.kind}", checkout.target_branch)

        commit_summary = " ".join(commits)
        self._comment_and_resolve(fixed, verdicts, commit_summary)
        LOGGER.info("Worker pushed batch %s commits=%s", batch_label, commit_summary)

    def _comment_and_resolve(
        self,
        fixed: List[RunRecord],
        verdicts: Dict[int, Dict[str, Any]],
        commit_summary: str,
    ) -> None:
        for run in fixed:
            verdict = verdicts[run.bug_id]
            error = ""
            self.state.record_run_event(run.bug_id, "comment_start", commit_summary)
            try:
                comment_bug(
                    self.settings.zentao_client_script,
                    run.bug_id,
                    cause=_cause_text(verdict, "原因分析", verdict.get("cause") or "见提交记录。"),
                    solution=_solution_text(verdict, commit_summary),
                )
                self.state.record_run_event(run.bug_id, "comment_done", "")
            except ZenTaoWriteError as exc:
                error = str(exc)
                self.state.record_run_event(run.bug_id, "comment_failed", error)
                LOGGER.error("Comment failed bug #%s after batch push: %s", run.bug_id, exc)
            try:
                resolve_bug(self.settings.zentao_client_script, run.bug_id)
                self.state.record_run_event(run.bug_id, "resolve_done", "resolved/fixed")
            except ZenTaoResolveError as exc:
                error = f"{error}; {exc}".strip("; ")
                self.state.record_run_event(run.bug_id, "resolve_failed", str(exc))
                LOGGER.error("Resolve failed bug #%s after batch push: %s", run.bug_id, exc)
            self.state.update_status(
                run.bug_id,
                "pushed" if not error else "writeback_failed",
                error=error,
                commit_hash=commit_summary,
                handled_once=True,
                completed=True,
            )


class _Checkout:
    def __init__(self, kind: str, repo_cache: Path, worktree: Path, target_branch: str, baseline: str):
        self.kind = kind
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
