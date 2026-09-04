import unittest
import tempfile
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from zentao_auto_fixer.models import BugCandidate, ProjectConfig
from zentao_auto_fixer.poller import Poller, _is_active


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

    def test_previously_skipped_ui_bug_is_requeued_when_enabled(self):
        poller, state, worker = _ui_poller(process_ui_bugs=True)
        state.get_run.return_value = SimpleNamespace(status="skipped_ui")
        state.requeue_skipped_ui.return_value = True

        with mock.patch("zentao_auto_fixer.poller.list_project_bugs", return_value=[_bug("active", title="【UI】错位")]):
            poller.poll_once()

        state.requeue_skipped_ui.assert_called_once()
        worker.enqueue.assert_called_once_with(1)

    def test_resolved_failed_bug_no_longer_keeps_health_degraded(self):
        poller, state, worker = _ui_poller(process_ui_bugs=False)
        state.get_run.return_value = SimpleNamespace(status="failed")

        with mock.patch("zentao_auto_fixer.poller.list_project_bugs", return_value=[_bug("resolved")]):
            poller.poll_once()

        state.update_status.assert_called_once_with(
            1,
            "skipped_stale",
            error="ZenTao status is now 'resolved'; the failed task is no longer active.",
            completed=True,
        )
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
        state.get_run.return_value = SimpleNamespace(status="writeback_failed")
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

    def test_fresh_bug_is_queued_before_an_older_retry(self):
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
        state.requeue_retryable.assert_not_called()
        worker.enqueue.assert_called_once_with(2)


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
    state.get_run.return_value = None
    worker = mock.Mock()
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
