import unittest
import tempfile
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from zentao_auto_fixer.models import BugCandidate, ProjectConfig
from zentao_auto_fixer.poller import Poller, _is_active
from zentao_auto_fixer.zentao import ZenTaoPollError, bug_is_active


class ActiveCountTests(unittest.TestCase):
    def test_dashboard_predicate_matches_the_repair_queue_gate(self):
        """看板的「激活」统计口径必须和修复队列的入口闸一致，两处实现不许跑偏。"""
        samples = [
            {},
            {"status": "active"},
            {"status": "Active"},
            {"status": "active", "closedBy": "qa"},
            {"status": "active", "closedBy": {"account": "qa"}},
            {"status": "active", "closedBy": {"account": ""}},
            {"status": "active", "resolvedBy": {"account": "dev"}},
            {"status": "active", "resolvedBy": ""},
            {"status": "active", "resolved_by": "dev"},
            {"status": "resolved"},
            {"status": "closed"},
            {"status": "active", "closedBy": 0},
            {"status": "active", "deleted": True},
            {"status": "active", "deleted": "1"},
            {"status": "active", "deleted": False},
            {"status": "active", "deleted": "0"},
        ]
        for raw in samples:
            bug = _bug(str(raw.get("status") or ""), **{k: v for k, v in raw.items() if k != "status"})
            self.assertEqual(_is_active(bug), bug_is_active(raw), raw)

    def test_active_count_is_recorded_for_the_dashboard(self):
        poller, state, worker = _ui_poller(process_ui_bugs=False, max_bugs_per_poll=1)

        with mock.patch("zentao_auto_fixer.poller.list_project_bugs", return_value=[_bug("resolved")]), \
                mock.patch("zentao_auto_fixer.poller.count_active_bugs", return_value=11) as counter:
            poller.poll_once()

        counter.assert_called_once()
        self.assertEqual(state.record_poll_run.call_args.kwargs["active_bugs"], 11)

    def test_active_count_failure_only_degrades_the_statistic(self):
        poller, state, worker = _ui_poller(process_ui_bugs=False, max_bugs_per_poll=1)

        with mock.patch("zentao_auto_fixer.poller.list_project_bugs", return_value=[_bug("resolved")]), \
                mock.patch("zentao_auto_fixer.poller.count_active_bugs", side_effect=ZenTaoPollError("禅道读取失败")):
            poller.poll_once()

        self.assertEqual(state.record_poll_run.call_args.kwargs["active_bugs"], -1)


class DrainTests(unittest.TestCase):
    def test_poll_pauses_while_draining(self):
        """排空期间不排新单：重启脚本先放 drain 标志，等在跑批次收尾。"""
        poller, state, worker = _ui_poller(process_ui_bugs=False)
        state.draining.return_value = True

        with mock.patch("zentao_auto_fixer.poller.list_project_bugs") as lister:
            poller.poll_once()

        lister.assert_not_called()
        worker.enqueue.assert_not_called()
        state.record_poll_run.assert_not_called()


class ActiveFilterTests(unittest.TestCase):
    def test_only_active_status_is_picked_up(self):
        self.assertTrue(_is_active(_bug("active")))
        self.assertTrue(_is_active(_bug("Active")))
        for status in ("resolved", "closed", "done", "confirmed", ""):
            self.assertFalse(_is_active(_bug(status)), status)

    def test_active_but_already_resolved_or_closed_is_skipped(self):
        self.assertFalse(_is_active(_bug("active", resolvedBy="dev")))
        self.assertFalse(_is_active(_bug("active", closedBy="qa")))
        self.assertTrue(_is_active(_bug("active", resolvedBy="", closedBy=None)))

    def test_active_but_deleted_is_skipped(self):
        """已删除的单 status 仍是 active（2026-09-24 实测 #8091/#8220），入口闸要挡住它。"""
        self.assertFalse(_is_active(_bug("active", deleted=True)))
        self.assertFalse(_is_active(_bug("active", deleted="1")))
        self.assertTrue(_is_active(_bug("active", deleted=False)))
        self.assertTrue(_is_active(_bug("active", deleted="0")))


class UiPollerTests(unittest.TestCase):
    def test_ui_tag_is_not_enqueued_by_default(self):
        poller, state, worker = _ui_poller(process_ui_bugs=False, max_bugs_per_poll=1)

        with mock.patch("zentao_auto_fixer.poller.list_project_bugs", return_value=[_bug("active", title="【ui】错位")]):
            poller.poll_once()

        state.enqueue_first_run.assert_not_called()
        worker.enqueue.assert_not_called()
        self.assertEqual(state.record_poll_run.call_args.kwargs["skipped_resolved"], 1)

    def test_ui_tag_is_enqueued_when_explicitly_enabled(self):
        poller, state, worker = _ui_poller(process_ui_bugs=True)
        poller._already_handled_in_zentao = mock.Mock(return_value="fresh")
        state.enqueue_first_run.return_value = True

        with mock.patch("zentao_auto_fixer.poller.list_project_bugs", return_value=[_bug("active", title="[UI] 错位")]):
            poller.poll_once()

        state.enqueue_first_run.assert_called_once()
        worker.enqueue.assert_called_once_with(1)

    def test_occupied_bug_is_never_enqueued(self):
        """别的 AI 已经认领 → 连新一轮修复都不排（dashu 口径：直接跳过）。"""
        poller, state, worker = _ui_poller(process_ui_bugs=False)
        poller._already_handled_in_zentao = mock.Mock(return_value="occupied")

        with mock.patch("zentao_auto_fixer.poller.list_project_bugs", return_value=[_bug("active")]):
            poller.poll_once()

        state.enqueue_first_run.assert_not_called()
        worker.enqueue.assert_not_called()
        self.assertEqual(state.record_poll_run.call_args.kwargs["skipped_resolved"], 1)

    def test_previously_skipped_ui_bug_is_requeued_when_enabled(self):
        poller, state, worker = _ui_poller(process_ui_bugs=True)
        state.get_run.return_value = SimpleNamespace(status="skipped_ui")
        state.requeue_skipped_ui.return_value = True

        with mock.patch("zentao_auto_fixer.poller.list_project_bugs", return_value=[_bug("active", title="【UI】错位")]):
            poller.poll_once()

        state.requeue_skipped_ui.assert_called_once()
        worker.enqueue.assert_called_once_with(1)

    def test_pushed_bug_verified_closed_by_qa_is_marked(self):
        poller, state, worker = _ui_poller(process_ui_bugs=False)
        state.get_run.return_value = SimpleNamespace(status="pushed")
        state.mark_verified_closed.return_value = True

        with mock.patch(
            "zentao_auto_fixer.poller.list_project_bugs",
            return_value=[_bug("closed", closedBy="qa")],
        ):
            poller.poll_once()

        state.mark_verified_closed.assert_called_once_with(1, "closed")
        state.record_run_event.assert_called_once_with(1, "verified_closed", "ZenTao status is now 'closed'")
        worker.enqueue.assert_not_called()

    def test_pushed_bug_vanished_from_list_and_closed_is_verified_closed(self):
        poller, state, worker = _ui_poller(process_ui_bugs=False)
        state.get_run.return_value = None
        state.pushed_resolved_unverified.return_value = [1]
        state.mark_verified_closed.return_value = True

        with mock.patch(
            "zentao_auto_fixer.poller.list_project_bugs", return_value=[]
        ), mock.patch("zentao_auto_fixer.poller.bug_closed_by", return_value="hejiajie"):
            poller.poll_once()

        state.mark_verified_closed.assert_called_once_with(1, "closed")
        state.record_run_event.assert_called_once_with(1, "verified_closed", "closedBy=hejiajie")

    def test_vanished_non_success_bug_closed_in_zentao_is_cleared(self):
        """看板「非成功任务」里的单，禅道里早就被关掉了 —— 关闭会从列表消失，得反过来核一次。"""
        poller, state, worker = _ui_poller(process_ui_bugs=False)
        state.non_success_runs.return_value = [7895]
        state.mark_zentao_resolved.return_value = True

        with mock.patch(
            "zentao_auto_fixer.poller.list_project_bugs", return_value=[]
        ), mock.patch(
            "zentao_auto_fixer.poller.bug_vanished_reason",
            return_value=("closed", "禅道里已被 hejiajie 关闭"),
        ):
            poller.poll_once()

        state.mark_zentao_resolved.assert_called_once_with(
            7895, "closed", "禅道里已被 hejiajie 关闭；本地这条记录已作废。"
        )
        state.record_run_event.assert_called_once_with(7895, "zentao_resolved", mock.ANY)

    def test_vanished_non_success_bug_that_is_not_closed_is_left_alone(self):
        poller, state, worker = _ui_poller(process_ui_bugs=False)
        state.non_success_runs.return_value = [7895]

        with mock.patch(
            "zentao_auto_fixer.poller.list_project_bugs", return_value=[]
        ), mock.patch(
            "zentao_auto_fixer.poller.bug_vanished_reason", return_value=("", "")
        ):
            poller.poll_once()

        state.mark_zentao_resolved.assert_not_called()

    def test_queued_bug_deleted_in_zentao_leaves_the_queue(self):
        """看板「排队」里的单，禅道里被删掉了 —— 删除同样让它从列表消失，状态会永远卡在排队。"""
        poller, state, worker = _ui_poller(process_ui_bugs=False)
        state.queued_runs.return_value = [8091]
        state.mark_zentao_resolved.return_value = True

        with mock.patch(
            "zentao_auto_fixer.poller.list_project_bugs", return_value=[]
        ), mock.patch(
            "zentao_auto_fixer.poller.bug_vanished_reason",
            return_value=("deleted", "禅道里已删除"),
        ):
            poller.poll_once()

        state.mark_zentao_resolved.assert_called_once_with(
            8091, "deleted", "禅道里已删除；本地这条记录已作废。"
        )
        state.record_run_event.assert_called_once_with(8091, "zentao_resolved", mock.ANY)

    def test_queued_bug_still_active_outside_the_list_is_left_queued(self):
        """不在列表里但禅道里还活着（比如换了产品线）—— 不碰它，免得把真该修的单洗掉。"""
        poller, state, worker = _ui_poller(process_ui_bugs=False)
        state.queued_runs.return_value = [8091]

        with mock.patch(
            "zentao_auto_fixer.poller.list_project_bugs", return_value=[]
        ), mock.patch(
            "zentao_auto_fixer.poller.bug_vanished_reason", return_value=("", "")
        ):
            poller.poll_once()

        state.mark_zentao_resolved.assert_not_called()

    def test_pushed_bug_still_listed_is_not_rechecked(self):
        poller, state, worker = _ui_poller(process_ui_bugs=False)
        state.get_run.return_value = None
        state.pushed_resolved_unverified.return_value = [1]

        with mock.patch(
            "zentao_auto_fixer.poller.list_project_bugs", return_value=[_bug("resolved")]
        ), mock.patch("zentao_auto_fixer.poller.bug_closed_by") as closed_by:
            poller.poll_once()

        closed_by.assert_not_called()
        state.mark_verified_closed.assert_not_called()

    def test_pushed_bug_coming_back_active_is_marked_reactivated(self):
        poller, state, worker = _ui_poller(process_ui_bugs=False)
        state.get_run.return_value = SimpleNamespace(status="pushed")
        state.mark_reactivated.return_value = True
        poller._already_handled_in_zentao = mock.Mock(return_value="fresh")

        with mock.patch(
            "zentao_auto_fixer.poller.list_project_bugs",
            return_value=[_bug("active")],
        ):
            poller.poll_once()

        state.mark_reactivated.assert_called_once_with(1)
        state.record_run_event.assert_called_once_with(1, "reactivated_after_auto_fix", mock.ANY)

    def test_resolved_failed_bug_no_longer_keeps_health_degraded(self):
        poller, state, worker = _ui_poller(process_ui_bugs=False)
        state.get_run.return_value = SimpleNamespace(status="failed")

        with mock.patch("zentao_auto_fixer.poller.list_project_bugs", return_value=[_bug("resolved")]):
            poller.poll_once()

        # 禅道已经解决 → 本地的失败记录作废，并且不再挂在看板「非成功任务」里。
        state.mark_zentao_resolved.assert_called_once_with(
            1,
            "resolved",
            "禅道里已经是 resolved；本地的非成功状态已作废。",
        )
        state.record_run_event.assert_called_once_with(1, "zentao_resolved", mock.ANY)
        state.update_status.assert_not_called()
        worker.enqueue.assert_not_called()


class RetryPolicyTests(unittest.TestCase):
    def test_active_technical_failure_is_requeued_once_without_config_switch(self):
        poller, state, worker = _ui_poller(process_ui_bugs=False)
        state.get_run.return_value = SimpleNamespace(status="failed")
        state.requeue_retryable.return_value = True
        poller._already_handled_in_zentao = mock.Mock(return_value="fresh")

        with mock.patch("zentao_auto_fixer.poller.list_project_bugs", return_value=[_bug("active")]):
            poller.poll_once()

        state.requeue_retryable.assert_called_once()
        # max_retries 必须来自 Settings.max_bug_retries（可通过 AUTO_FIXER_MAX_BUG_RETRIES 配置），
        # 不能悄悄退回 state.py 里那个只给 1 次重试的旧默认值。
        self.assertEqual(state.requeue_retryable.call_args.kwargs["max_retries"], 4)
        worker.enqueue.assert_called_once_with(1)

    def test_writeback_retry_also_uses_the_configured_ceiling(self):
        poller, state, worker = _ui_poller(process_ui_bugs=False)
        state.get_run.return_value = SimpleNamespace(status="writeback_failed", writeback_payload="")
        state.queue_writeback_retry.return_value = True
        poller._already_handled_in_zentao = mock.Mock(return_value="fresh")

        with mock.patch("zentao_auto_fixer.poller.list_project_bugs", return_value=[_bug("active")]):
            poller.poll_once()

        state.queue_writeback_retry.assert_called_once()
        self.assertEqual(state.queue_writeback_retry.call_args.kwargs["max_retries"], 4)
        worker.enqueue.assert_called_once_with(1)

    def test_local_unable_to_fix_result_is_silently_terminal(self):
        poller, state, worker = _ui_poller(process_ui_bugs=False)
        state.get_run.return_value = SimpleNamespace(status="unable_to_fix")
        poller._already_handled_in_zentao = mock.Mock()

        with mock.patch("zentao_auto_fixer.poller.list_project_bugs", return_value=[_bug("active")]):
            poller.poll_once()

        poller._already_handled_in_zentao.assert_not_called()
        worker.enqueue.assert_not_called()

    def test_lower_id_retry_is_queued_before_fresh_bug_even_with_old_poll_limit(self):
        poller, state, worker = _ui_poller(process_ui_bugs=False, max_bugs_per_poll=1)
        older_retry = BugCandidate(**{**_bug("active").__dict__, "bug_id": 1, "opened_at": "2026-08-01"})
        newer_fresh = BugCandidate(**{**older_retry.__dict__, "bug_id": 2, "opened_at": "2026-08-02"})
        runs = {1: SimpleNamespace(status="failed"), 2: None}
        state.get_run.side_effect = lambda bug_id: runs[bug_id]
        state.enqueue_first_run.return_value = True
        poller._already_handled_in_zentao = mock.Mock(return_value="fresh")

        with mock.patch(
            "zentao_auto_fixer.poller.list_project_bugs",
            return_value=[older_retry, newer_fresh],
        ):
            poller.poll_once()

        state.enqueue_first_run.assert_called_once_with(newer_fresh, mock.ANY)
        state.requeue_retryable.assert_called_once()
        self.assertEqual(worker.enqueue.call_args_list, [mock.call(1), mock.call(2)])


def _bug(status: str, title="bug", **raw) -> BugCandidate:
    return BugCandidate(
        bug_id=1,
        title=title,
        product_id=8,
        assigned_to="dev",
        bug_type="codeerror",
        status=status,
        severity=3,
        priority=2,
        raw=raw,
    )


def _ui_poller(*, process_ui_bugs: bool, max_bugs_per_poll: int = 3):
    project = ProjectConfig(
        name="project",
        enabled=True,
        zentao_product_id=8,
        zentao_assigned_to="",
        repo_url="git@example.com:project.git",
        target_branch="main",
        only_code_bugs=True,
        max_bugs_per_poll=max_bugs_per_poll,
        process_ui_bugs=process_ui_bugs,
    )
    settings = SimpleNamespace(
        load_projects=lambda: [project],
        zentao_client_script=Path("/tmp/zentao_client.py"),
        max_bug_retries=4,
    )
    state = mock.Mock()
    state.awaiting_merge_bug_ids.return_value = []
    state.get_run.return_value = None
    state.pushed_resolved_unverified.return_value = []
    state.non_success_runs.return_value = []
    state.queued_runs.return_value = []
    state.draining.return_value = False
    worker = mock.Mock()
    worker.dispatch_lock = threading.Lock()
    return Poller(settings, state, worker), state, worker


if __name__ == "__main__":
    unittest.main()


class AgentBudgetTests(unittest.TestCase):
    def setUp(self):
        self._temp_dirs = []

    def tearDown(self):
        for temp_dir in self._temp_dirs:
            temp_dir.cleanup()

    def test_ceiling_stops_further_agent_runs_the_same_day(self):
        worker = _worker(max_runs=2, temp_dirs=self._temp_dirs)
        self.assertTrue(worker._claim_agent_budget())
        self.assertTrue(worker._claim_agent_budget())
        self.assertFalse(worker._claim_agent_budget())
        self.assertEqual(worker.agent_runs_today(), 2)

    def test_counter_is_independent_on_a_new_day(self):
        worker = _worker(max_runs=1, temp_dirs=self._temp_dirs)
        state = worker.state
        self.assertTrue(state.claim_daily_counter("test", "2026-08-27", 1))
        self.assertFalse(state.claim_daily_counter("test", "2026-08-27", 1))
        self.assertTrue(state.claim_daily_counter("test", "2026-08-28", 1))

    def test_counter_survives_worker_restart(self):
        worker = _worker(max_runs=2, temp_dirs=self._temp_dirs)
        self.assertTrue(worker._claim_agent_budget())
        restarted = _worker(max_runs=2, state=worker.state)
        self.assertTrue(restarted._claim_agent_budget())
        self.assertFalse(restarted._claim_agent_budget())
        self.assertEqual(restarted.agent_runs_today(), 2)

    def test_counter_is_atomic_across_store_instances(self):
        from zentao_auto_fixer.state import StateStore

        temp_dir = tempfile.TemporaryDirectory()
        self._temp_dirs.append(temp_dir)
        path = Path(temp_dir.name) / "state.sqlite3"
        stores = [StateStore(path), StateStore(path)]

        with ThreadPoolExecutor(max_workers=8) as pool:
            claimed = list(
                pool.map(
                    lambda index: stores[index % 2].claim_daily_counter(
                        "agent_runs", "2026-08-27", 20
                    ),
                    range(80),
                )
            )

        self.assertEqual(sum(claimed), 20)
        self.assertEqual(stores[0].daily_counter_value("agent_runs", "2026-08-27"), 20)

    def test_three_consecutive_no_progress_runs_trip_the_daily_fuse(self):
        worker = _worker(max_runs=50, temp_dirs=self._temp_dirs)
        day = datetime.now().astimezone().date().isoformat()
        worker.state.set_daily_counter(worker._no_progress_counter_name(), day, 3)

        self.assertFalse(worker._claim_agent_budget())
        self.assertEqual(worker.agent_runs_today(), 0)


def _worker(max_runs: int, state=None, temp_dirs=None):
    from unittest import mock

    from zentao_auto_fixer.state import StateStore
    from zentao_auto_fixer.worker import Worker

    temp_dir = None
    if state is None:
        temp_dir = tempfile.TemporaryDirectory()
        if temp_dirs is not None:
            temp_dirs.append(temp_dir)
        state = StateStore(Path(temp_dir.name) / "state.sqlite3")
    settings = mock.Mock(worker_count=1, max_agent_runs_per_day=max_runs)
    worker = Worker(settings, state)
    worker._test_temp_dir = temp_dir
    return worker
