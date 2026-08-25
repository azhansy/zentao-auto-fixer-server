import unittest

from zentao_auto_fixer.models import BugCandidate
from zentao_auto_fixer.poller import _is_active


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


def _bug(status: str, **raw) -> BugCandidate:
    return BugCandidate(
        bug_id=1,
        title="bug",
        product_id=8,
        assigned_to="dev",
        bug_type="codeerror",
        status=status,
        severity=3,
        priority=2,
        raw=raw,
    )


if __name__ == "__main__":
    unittest.main()


class AgentBudgetTests(unittest.TestCase):
    def test_ceiling_stops_further_agent_runs_the_same_day(self):
        worker = _worker(max_runs=2)
        self.assertTrue(worker._claim_agent_budget())
        self.assertTrue(worker._claim_agent_budget())
        self.assertFalse(worker._claim_agent_budget())
        self.assertEqual(worker.agent_runs_today(), 2)

    def test_counter_resets_on_a_new_day(self):
        worker = _worker(max_runs=1)
        self.assertTrue(worker._claim_agent_budget())
        self.assertFalse(worker._claim_agent_budget())
        worker._agent_runs_day = "1999-01-01"
        self.assertTrue(worker._claim_agent_budget())


def _worker(max_runs: int):
    from unittest import mock

    from zentao_auto_fixer.worker import Worker

    settings = mock.Mock(worker_count=1, max_agent_runs_per_day=max_runs)
    return Worker(settings, mock.Mock())
