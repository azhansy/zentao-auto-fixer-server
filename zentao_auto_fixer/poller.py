from __future__ import annotations

import json
import logging
import threading
import time
from typing import Any, Optional

from .config import Settings
from .models import (
    AUTO_FIXED_STATUSES,
    RESURRECTABLE_STATUSES,
    RETRYABLE_STATUSES,
    TERMINAL_STATUSES,
    BugCandidate,
    ProjectConfig,
    has_manual_tag,
    has_ui_tag,
    platforms_of,
)
from .state import StateStore, utc_now
from .worker import Worker
from .zentao import (
    ZenTaoPollError,
    bug_ai_state,
    bug_closed_by,
    bug_vanished_reason,
    count_active_bugs,
    list_project_bugs,
)


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
        if self.state.draining():
            return  # 排空（准备重启）：不排新单，也不重排 awaiting_merge
        projects = [project for project in self.settings.load_projects() if project.enabled]
        # ponytail: dispatch pauses during polling; publish a collected snapshot if polling latency grows.
        with self.worker.dispatch_lock:
            for bug_id in self.state.awaiting_merge_bug_ids():
                self.worker.enqueue(bug_id)
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
        """历史里已有 AI 备注 → 交给人工；被别人贴了占用标记 → 永久让出，都不再排进修复队列。

        返回 "handled" / "occupied" / "fresh" / "unknown"（历史读不到，这一轮先不动）。
        """
        try:
            handled, occupied = bug_ai_state(self.settings.zentao_client_script, bug.bug_id)
        except ZenTaoPollError as exc:
            LOGGER.warning("Could not read bug #%s history, skipping this round: %s", bug.bug_id, exc)
            return "unknown"
        if occupied:
            if self.state.record_skipped_occupied(bug, project, occupied):
                self.state.record_run_event(bug.bug_id, "skipped_occupied", occupied)
                LOGGER.info("Bug #%s is occupied by another AI, leaving it alone: %s", bug.bug_id, occupied)
            return "occupied"
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

    def _check_closed_vanished(self, project: ProjectConfig, listed_ids: set) -> None:
        """禅道里关闭的 Bug 会从列表接口消失 —— 轮询循环根本走不到它们。

        三种都要核：已推送、等 QA 复验的单（核完可能就是「验证关闭」），本地还挂着
        「非成功」、但禅道里早被别人解决/关闭的单（2026-09-16 看板上 36 单里 35 单是这种），
        以及本地还挂在「排队」的单 —— 已删除的单同样不在列表里，只有反过来核才看得见
        （2026-09-24 看板上 5 条卡在「排队」，禅道里 2 条已删除、3 条已关闭）。
        """
        for bug_id in self.state.pushed_resolved_unverified(project.name):
            if bug_id in listed_ids:
                continue
            try:
                closed_by = bug_closed_by(self.settings.zentao_client_script, bug_id)
            except ZenTaoPollError as exc:
                LOGGER.warning("Could not re-read vanished bug #%s: %s", bug_id, exc)
                continue
            if closed_by and self.state.mark_verified_closed(bug_id, "closed"):
                self.state.record_run_event(bug_id, "verified_closed", f"closedBy={closed_by}")
                LOGGER.info("Bug #%s verified closed by %s", bug_id, closed_by)
        for bug_id in self.state.non_success_runs(project.name) + self.state.queued_runs(project.name):
            if bug_id in listed_ids:
                continue
            try:
                zentao_status, reason = bug_vanished_reason(self.settings.zentao_client_script, bug_id)
            except ZenTaoPollError as exc:
                LOGGER.warning("Could not re-read vanished bug #%s: %s", bug_id, exc)
                continue
            if not reason:
                # 不在列表里又不是终态（比如换了产品线），不动它，免得把真失败洗成已解决
                continue
            message = f"{reason}；本地这条记录已作废。"
            if self.state.mark_zentao_resolved(bug_id, zentao_status, message):
                self.state.record_run_event(bug_id, "zentao_resolved", message)
                LOGGER.info("Bug #%s no longer needs work (%s); local record cleared", bug_id, reason)

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
        skipped_manual = 0
        skipped_occupied = 0
        marked_manual = 0
        requeued_failed = 0
        active_bugs = -1
        try:
            bugs = list_project_bugs(self.settings.zentao_client_script, project)
            total = len(bugs)
            existing_runs = {bug.bug_id: self.state.get_run(bug.bug_id) for bug in bugs}
            bugs.sort(key=lambda bug: bug.bug_id)
            self._check_closed_vanished(project, {bug.bug_id for bug in bugs})
            for bug in bugs:
                if bug.bug_id <= 0:
                    skipped_resolved += 1
                    continue
                existing = existing_runs[bug.bug_id]
                active = _is_active(bug)

                if active and has_ui_tag(bug.title) and not project.process_ui_bugs:
                    skipped_ui += 1
                    continue

                if active and has_manual_tag(bug.title):
                    skipped_manual += 1
                    continue

                if active and project.skips_platforms(platforms_of(bug.title)):
                    skipped_platform += 1
                    continue

                if not active:
                    skipped_resolved += 1
                    if existing and existing.status in AUTO_FIXED_STATUSES:
                        if _has_value(bug.raw.get("closedBy") or bug.raw.get("closed_by")):
                            if self.state.mark_verified_closed(bug.bug_id, bug.status):
                                self.state.record_run_event(
                                    bug.bug_id, "verified_closed", f"ZenTao status is now {bug.status!r}"
                                )
                        else:
                            self.state.mark_seen_resolved_once(bug.bug_id, bug.status)
                    elif existing and existing.status in RESURRECTABLE_STATUSES:
                        # 禅道里已经解决/关闭，本地还挂着「非成功」是过期状态 —— 更新它，
                        # 别让人在看板上以为是待处理（2026-09-16 dashu 反馈）。
                        who = _actor_name(bug.raw.get("resolvedBy") or bug.raw.get("resolved_by"))
                        message = f"禅道里已经是 {bug.status}（解决人：{who}）；本地的非成功状态已作废。"
                        if not who:
                            message = f"禅道里已经是 {bug.status}；本地的非成功状态已作废。"
                        if self.state.mark_zentao_resolved(bug.bug_id, bug.status, message):
                            self.state.record_run_event(bug.bug_id, "zentao_resolved", message)
                            LOGGER.info(
                                "Bug #%s is %r in ZenTao; cleared stale local status %r",
                                bug.bug_id,
                                bug.status,
                                existing.status,
                            )
                    continue

                unresolved_count += 1
                candidate_count += 1

                if existing:
                    if existing.status in AUTO_FIXED_STATUSES:
                        if self.state.mark_reactivated(bug.bug_id):
                            self.state.record_run_event(
                                bug.bug_id, "reactivated_after_auto_fix", "Bug came back active after the AI fix"
                            )
                    if existing.status == "queued":
                        self.worker.enqueue(bug.bug_id)
                        skipped_existing += 1
                        continue
                    if existing.status in {"running", "writeback_queued"}:
                        skipped_existing += 1
                        continue
                    if existing.status == "skipped_ui" and project.process_ui_bugs:
                        if self.state.requeue_skipped_ui(bug, project):
                            self.worker.enqueue(bug.bug_id)
                            queued += 1
                        else:
                            skipped_existing += 1
                        continue
                    if existing.status == "writeback_failed":
                        try:
                            mr_writeback = bool(json.loads(existing.writeback_payload or "{}").get("merge_requests"))
                        except (ValueError, TypeError):
                            mr_writeback = False
                        zentao_state = "fresh" if mr_writeback else self._already_handled_in_zentao(bug, project)
                        if zentao_state == "fresh" and self.state.queue_writeback_retry(
                            bug.bug_id, max_retries=self.settings.max_bug_retries
                        ):
                            self.worker.enqueue(bug.bug_id)
                            queued += 1
                        else:
                            skipped_existing += 1
                        continue
                    if existing.status in RETRYABLE_STATUSES:
                        if self._already_handled_in_zentao(bug, project) != "fresh":
                            skipped_existing += 1
                            continue
                        if self.state.requeue_retryable(bug, project, max_retries=self.settings.max_bug_retries):
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

                zentao_state = self._already_handled_in_zentao(bug, project)
                if zentao_state == "handled":
                    marked_manual += 1
                    continue
                if zentao_state == "occupied":
                    skipped_occupied += 1
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
            active_bugs = _active_bug_count(project)
            self.state.record_poll_run(
                project_name=project.name,
                product_id=project.zentao_product_id,
                started_at=started_at,
                status="ok",
                active_bugs=active_bugs,
                total_bugs=total,
                unresolved_bugs=unresolved_count,
                candidate_bugs=candidate_count,
                queued_bugs=queued,
                skipped_existing=skipped_existing,
                skipped_resolved=skipped_resolved + skipped_platform + skipped_ui + skipped_manual + skipped_occupied,
                marked_manual=marked_manual,
                requeued_failed=requeued_failed,
            )
            LOGGER.info(
                "Polled %s: total=%s unresolved=%s queued=%s existing=%s resolved=%s manual=%s "
                "platform_skipped=%s ui_skipped=%s manual_tag_skipped=%s occupied_skipped=%s",
                project.name,
                total,
                unresolved_count,
                queued,
                skipped_existing,
                skipped_resolved,
                marked_manual,
                skipped_platform,
                skipped_ui,
                skipped_manual,
                skipped_occupied,
            )
        except Exception as exc:
            self.state.record_poll_run(
                project_name=project.name,
                product_id=project.zentao_product_id,
                started_at=started_at,
                status="failed",
                active_bugs=active_bugs,
                total_bugs=total,
                unresolved_bugs=unresolved_count,
                candidate_bugs=candidate_count,
                queued_bugs=queued,
                skipped_existing=skipped_existing,
                skipped_resolved=skipped_resolved + skipped_platform + skipped_ui + skipped_manual + skipped_occupied,
                marked_manual=marked_manual,
                requeued_failed=requeued_failed,
                error=str(exc),
            )
            raise


def _active_bug_count(project: ProjectConfig) -> int:
    """看板统计用的「激活」总数。取不到就记 -1（看板显示 —），统计失败绝不拖垮本轮修复。"""
    try:
        return count_active_bugs(project)
    except Exception as exc:  # ponytail: 统计是旁路指标，网络/凭证异常都只降级，不中断轮询
        LOGGER.warning("统计 %s 的激活 Bug 数失败：%s", project.name, exc)
        return -1


def _is_active(bug: BugCandidate) -> bool:
    if bug.status.strip().lower() != "active":
        return False
    raw = bug.raw
    if _has_value(raw.get("deleted")):
        # 已删除的单 status 仍是 active（2026-09-24 实测 #8091/#8220），列表接口现在不返回它们，
        # 但入口闸不能指望上游一直如此：一条已删除的单不该换成 AI 时间去跑。
        return False
    if _has_value(raw.get("closedBy")) or _has_value(raw.get("closed_by")):
        return False
    if _has_value(raw.get("resolvedBy")) or _has_value(raw.get("resolved_by")):
        return False
    return True


def _actor_name(value: Any) -> str:
    """禅道的 resolvedBy/closedBy 可能是 {'account','realname'} 也可能是裸字符串。"""
    if isinstance(value, dict):
        return str(value.get("realname") or value.get("account") or "").strip()
    return str(value or "").strip()


def _has_value(value: Any) -> bool:
    if value in (None, "", 0, "0"):
        return False
    if isinstance(value, dict):
        return any(_has_value(item) for item in value.values())
    return True
