import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from zentao_auto_fixer.worker import _FOLLOWING_START_TEXT, Worker
from zentao_auto_fixer.zentao import ZenTaoWriteError


class FollowingNotesTests(unittest.TestCase):
    def _worker(self):
        state = mock.Mock()
        settings = SimpleNamespace(worker_count=1, zentao_client_script=Path("/tmp/z.py"))
        return Worker(settings, state), state

    def test_start_note_posted_for_every_bug_in_batch(self):
        worker, state = self._worker()
        runs = [SimpleNamespace(bug_id=1), SimpleNamespace(bug_id=2)]
        with mock.patch("zentao_auto_fixer.worker.add_comment") as add:
            worker._note_following_started(runs)
        self.assertEqual(add.call_count, 2)
        add.assert_any_call(worker.settings.zentao_client_script, 1, _FOLLOWING_START_TEXT)
        add.assert_any_call(worker.settings.zentao_client_script, 2, _FOLLOWING_START_TEXT)
        state.record_run_event.assert_any_call(1, "following_started", "")
        state.record_run_event.assert_any_call(2, "following_started", "")

    def test_start_note_failure_is_swallowed(self):
        worker, state = self._worker()
        with mock.patch(
            "zentao_auto_fixer.worker.add_comment", side_effect=ZenTaoWriteError("boom")
        ):
            worker._note_following_started([SimpleNamespace(bug_id=1)])
        state.record_run_event.assert_called_once_with(1, "following_start_failed", "boom")

    def test_done_note_posted_for_ended_statuses(self):
        for status in ("pushed", "failed", "skipped_ui", "merge_request_failed", "writeback_failed"):
            with self.subTest(status=status):
                worker, state = self._worker()
                state.get_run.return_value = SimpleNamespace(bug_id=7, status=status)
                with mock.patch("zentao_auto_fixer.worker.add_comment") as add:
                    worker._note_following_done(7)
                add.assert_called_once()
                self.assertIn("AI 已结束对本 Bug 的跟进", add.call_args.args[2])
                state.record_run_event.assert_called_once_with(7, "following_done", status)

    def test_done_note_carries_reason_for_unable_to_fix(self):
        worker, state = self._worker()
        state.get_run.return_value = SimpleNamespace(
            bug_id=7, status="unable_to_fix", error="AI 无法从当前 Bug 描述定位到问题。 需要补充：复现步骤。"
        )
        with mock.patch("zentao_auto_fixer.worker.add_comment") as add:
            worker._note_following_done(7)
        text = add.call_args.args[2]
        self.assertIn("AI 无法自动修复", text)
        self.assertIn("原因：AI 无法从当前 Bug 描述定位到问题。 需要补充：复现步骤。", text)

    def test_done_note_without_reason_stays_one_line(self):
        worker, state = self._worker()
        state.get_run.return_value = SimpleNamespace(bug_id=7, status="unable_to_fix", error="")
        with mock.patch("zentao_auto_fixer.worker.add_comment") as add:
            worker._note_following_done(7)
        self.assertNotIn("原因：", add.call_args.args[2])

    def test_done_note_skipped_while_still_active(self):
        for status in ("queued", "running", "awaiting_merge", "awaiting_release"):
            with self.subTest(status=status):
                worker, state = self._worker()
                state.get_run.return_value = SimpleNamespace(bug_id=7, status=status)
                with mock.patch("zentao_auto_fixer.worker.add_comment") as add:
                    worker._note_following_done(7)
                add.assert_not_called()

    def test_done_note_skipped_when_no_run(self):
        worker, state = self._worker()
        state.get_run.return_value = None
        with mock.patch("zentao_auto_fixer.worker.add_comment") as add:
            worker._note_following_done(7)
        add.assert_not_called()

    def test_done_note_failure_is_swallowed(self):
        worker, state = self._worker()
        state.get_run.return_value = SimpleNamespace(bug_id=7, status="failed")
        with mock.patch(
            "zentao_auto_fixer.worker.add_comment", side_effect=ZenTaoWriteError("boom")
        ):
            worker._note_following_done(7)
        state.record_run_event.assert_called_once_with(7, "following_done_failed", "boom")

    def test_run_finally_posts_done_after_unhandled_error(self):
        worker, state = self._worker()
        state.get_run.return_value = SimpleNamespace(bug_id=7, status="failed")

        def blow_up(bug_id):
            worker._stop.set()
            raise RuntimeError("boom")

        worker._process_bug = mock.Mock(side_effect=blow_up)
        worker.queue.put(7)
        worker._queued_ids.add(7)
        with mock.patch("zentao_auto_fixer.worker.add_comment") as add:
            worker._run()
        add.assert_called_once_with(worker.settings.zentao_client_script, 7, mock.ANY)
        self.assertNotIn(7, worker._queued_ids)


if __name__ == "__main__":
    unittest.main()
