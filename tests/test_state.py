import tempfile
import unittest
from pathlib import Path

from zentao_auto_fixer.models import BugCandidate, ProjectConfig
from zentao_auto_fixer.state import StateStore


class StateTests(unittest.TestCase):
    def test_technical_failure_gets_one_automatic_retry_then_exhausts(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = StateStore(Path(tmp) / "state.sqlite3")
            bug = _bug(1)
            project = _project()
            store.enqueue_first_run(bug, project)
            store.update_status(1, "failed", completed=True)

            self.assertTrue(store.requeue_retryable(bug, project))
            self.assertEqual(store.get_run(1).retry_count, 1)
            store.update_status(1, "failed", completed=True)
            self.assertFalse(store.requeue_retryable(bug, project))
            self.assertEqual(store.get_run(1).status, "retry_exhausted")

    def test_writeback_retry_keeps_the_saved_success_payload(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = StateStore(Path(tmp) / "state.sqlite3")
            store.enqueue_first_run(_bug(1), _project())
            store.set_writeback_payload(1, '{"cause":"c","solution":"s"}')
            store.update_status(1, "writeback_failed", completed=True)

            self.assertTrue(store.queue_writeback_retry(1))
            run = store.get_run(1)
            self.assertEqual(run.status, "writeback_queued")
            self.assertIn('"cause":"c"', run.writeback_payload)

    def test_manual_requeue_is_one_shot_for_failed_stale_or_new_bugs(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = StateStore(Path(tmp) / "state.sqlite3")
            project = _project()
            failed = _bug(1)
            stale = _bug(2)
            new = _bug(3)
            store.enqueue_first_run(failed, project)
            store.enqueue_first_run(stale, project)
            store.update_status(1, "failed", completed=True)
            store.update_status(2, "skipped_stale", completed=True)

            self.assertTrue(store.manual_requeue(failed, project))
            self.assertTrue(store.manual_requeue(stale, project))
            self.assertTrue(store.manual_requeue(new, project))
            self.assertFalse(store.manual_requeue(failed, project))
            for bug_id in (1, 2, 3):
                run = store.get_run(bug_id)
                self.assertEqual(run.status, "queued")
                self.assertEqual(run.event_action, "manual_retry")

    def test_enqueue_once(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = StateStore(Path(tmp) / "state.sqlite3")
            bug = _bug(1)
            project = _project()
            self.assertTrue(store.enqueue_first_run(bug, project))
            self.assertFalse(store.enqueue_first_run(bug, project))
            run = store.get_run(1)
            self.assertIsNotNone(run)
            self.assertEqual(run.status, "queued")
            self.assertEqual(run.project_name, "project")

    def test_skipped_ui_can_be_requeued_after_the_setting_is_enabled(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = StateStore(Path(tmp) / "state.sqlite3")
            bug = BugCandidate(
                bug_id=1,
                title="【UI】按钮错位",
                product_id=8,
                assigned_to="dev",
                bug_type="codeerror",
                status="active",
                severity=3,
                priority=2,
                raw={},
            )
            project = _project()
            store.enqueue_first_run(bug, project)
            store.update_status(1, "skipped_ui", handled_once=False, completed=True)

            self.assertTrue(store.requeue_skipped_ui(bug, project))
            run = store.get_run(1)
            self.assertEqual(run.status, "queued")
            self.assertFalse(run.handled_once)

    def test_record_poll_run(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = StateStore(Path(tmp) / "state.sqlite3")
            store.record_poll_run(
                project_name="project",
                product_id=8,
                started_at="2026-06-01T00:00:00+00:00",
                status="ok",
                total_bugs=3,
                unresolved_bugs=0,
                candidate_bugs=0,
                queued_bugs=0,
                skipped_resolved=3,
            )
            rows = store.list_poll_runs()
            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0]["project_name"], "project")
            self.assertEqual(rows[0]["queued_bugs"], 0)

    def test_record_run_event(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = StateStore(Path(tmp) / "state.sqlite3")
            store.record_run_event(1, "started", "project=x")
            rows = store.list_run_events(1)
            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0]["event"], "started")
            self.assertEqual(rows[0]["message"], "project=x")

    def test_run_summary_since(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = StateStore(Path(tmp) / "state.sqlite3")
            store.enqueue_first_run(_bug(1), _project())
            store.enqueue_first_run(_bug(2), _project())
            store.enqueue_first_run(_bug(3), _project())
            store.update_status(1, "pushed", handled_once=True, completed=True)
            store.update_status(2, "failed", error="boom", completed=True)
            store.update_status(3, "running")

            summary = store.run_summary_since("2000-01-01T00:00:00+00:00")

            self.assertEqual(summary["completed"], 2)
            self.assertEqual(summary["auto_fixed"], 1)
            self.assertEqual(summary["pushed"], 1)
            self.assertEqual(summary["failed"], 1)
            self.assertEqual(summary["running"], 1)

    def test_claim_queued_batch_claims_same_repo_and_branch(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = StateStore(Path(tmp) / "state.sqlite3")
            project = _project()
            other_branch = ProjectConfig(
                name=project.name,
                enabled=True,
                zentao_product_id=project.zentao_product_id,
                zentao_assigned_to="",
                repo_url=project.repo_url,
                target_branch="release",
                only_code_bugs=True,
                max_bugs_per_poll=2,
            )
            store.enqueue_first_run(_bug(1), project)
            store.enqueue_first_run(_bug(2), project)
            store.enqueue_first_run(_bug(3), other_branch)

            batch = store.claim_queued_batch(1)

            self.assertEqual([run.bug_id for run in batch], [1, 2])
            self.assertEqual(store.get_run(1).status, "running")
            self.assertEqual(store.get_run(2).status, "running")
            self.assertEqual(store.get_run(3).status, "queued")
            self.assertEqual(store.claim_queued_batch(2), [])


def _bug(bug_id: int) -> BugCandidate:
    return BugCandidate(
        bug_id=bug_id,
        title="bug",
        product_id=8,
        assigned_to="dev",
        bug_type="codeerror",
        status="active",
        severity=3,
        priority=2,
        raw={},
    )


def _project() -> ProjectConfig:
    return ProjectConfig(
        name="project",
        enabled=True,
        zentao_product_id=8,
        zentao_assigned_to="",
        repo_url="git@example.com:group/project.git",
        target_branch="main",
        only_code_bugs=True,
        max_bugs_per_poll=2,
    )


if __name__ == "__main__":
    unittest.main()
