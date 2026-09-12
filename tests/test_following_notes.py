import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from zentao_auto_fixer.worker import _FOLLOWING_START_TEXT, Worker
from zentao_auto_fixer.zentao import ZenTaoPollError, ZenTaoWriteError


class FollowingNotesTests(unittest.TestCase):
    def _worker(self):
        state = mock.Mock()
        settings = SimpleNamespace(worker_count=1, zentao_client_script=Path("/tmp/z.py"))
        return Worker(settings, state), state

    def test_start_note_posted_for_every_bug_in_batch(self):
        worker, state = self._worker()
        state.list_run_events.return_value = []
        runs = [SimpleNamespace(bug_id=1), SimpleNamespace(bug_id=2)]
        with mock.patch("zentao_auto_fixer.worker.add_comment") as add:
            worker._note_following_started(runs)
        self.assertEqual(add.call_count, 2)
        add.assert_any_call(worker.settings.zentao_client_script, 1, _FOLLOWING_START_TEXT)
        add.assert_any_call(worker.settings.zentao_client_script, 2, _FOLLOWING_START_TEXT)
        state.record_run_event.assert_any_call(1, "following_started", "")
        state.record_run_event.assert_any_call(2, "following_started", "")

    def test_start_note_not_reposted_while_previous_one_is_open(self):
        worker, state = self._worker()
        state.list_run_events.return_value = [{"event": "started"}, {"event": "following_started"}]
        with mock.patch("zentao_auto_fixer.worker.add_comment") as add:
            worker._note_following_started([SimpleNamespace(bug_id=1)])
        add.assert_not_called()

    def test_start_note_reposted_after_previous_round_closed(self):
        worker, state = self._worker()
        state.list_run_events.return_value = [
            {"event": "following_started"},
            {"event": "following_done"},
        ]
        with mock.patch("zentao_auto_fixer.worker.add_comment") as add:
            worker._note_following_started([SimpleNamespace(bug_id=1)])
        add.assert_called_once()

    def test_start_note_posted_when_no_events_yet(self):
        worker, state = self._worker()
        state.list_run_events.return_value = []
        with mock.patch("zentao_auto_fixer.worker.add_comment") as add:
            worker._note_following_started([SimpleNamespace(bug_id=1)])
        add.assert_called_once()

    def test_start_note_failure_is_swallowed(self):
        worker, state = self._worker()
        state.list_run_events.return_value = []
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

    def test_done_note_carries_reason_for_failed(self):
        worker, state = self._worker()
        state.get_run.return_value = SimpleNamespace(
            bug_id=7, status="failed", error="claude failed with exit 1: boom"
        )
        with mock.patch("zentao_auto_fixer.worker.add_comment") as add:
            worker._note_following_done(7)
        text = add.call_args.args[2]
        self.assertIn("处理失败", text)
        self.assertIn("原因：claude failed with exit 1: boom", text)

    def test_done_note_reason_is_summarized_not_bluntly_truncated(self):
        from zentao_auto_fixer.worker import _summarize_error

        long_detail = (
            "描述不足且无法定位到代码缺陷：报单只有通用步骤，没有页面/按钮名称、账号与 App 版本。"
            "对 iOS 付费全链路逐项核对后：新订单必然返回 present_payment_sheet，客户端只有在 "
            "Stripe 支付面板交互完成后才可能推进资金状态。 需要补充：复现步骤、测试账号、订单号与截图。"
        )
        summary = _summarize_error(long_detail)
        self.assertLessEqual(len(summary), 120)
        self.assertTrue(summary.startswith("描述不足且无法定位到代码缺陷"))
        self.assertIn("需补充：", summary)
        self.assertNotIn("逐项核对", summary)  # 过程性内容被摘掉

        self.assertEqual(_summarize_error("短原因。"), "短原因。")
        self.assertEqual(
            _summarize_error("第一句。后面的过程性描述很长很长，不需要出现在备注里。" * 5),
            "第一句",
        )

    def test_done_note_reason_uses_summary(self):
        worker, state = self._worker()
        state.get_run.return_value = SimpleNamespace(
            bug_id=7, status="unable_to_fix", error="第一句结论。后续很长的过程性描述。" * 3
        )
        with mock.patch("zentao_auto_fixer.worker.add_comment") as add:
            worker._note_following_done(7)
        text = add.call_args.args[2]
        self.assertIn("原因：第一句结论", text)

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

    def test_writeback_guard_drops_bug_resolved_while_agent_ran(self):
        worker, state = self._worker()
        run = SimpleNamespace(bug_id=7, event_action="")
        with mock.patch(
            "zentao_auto_fixer.worker.bug_is_still_actionable",
            return_value=(False, "ZenTao status is now 'resolved', not active"),
        ):
            self.assertFalse(worker._still_actionable_before_writeback(run))
        state.update_status.assert_called_once_with(
            7, "skipped_stale", error="ZenTao status is now 'resolved', not active", completed=True
        )
        state.record_run_event.assert_called_once_with(
            7, "skipped_stale", "ZenTao status is now 'resolved', not active"
        )

    def test_writeback_guard_passes_fresh_bug(self):
        worker, state = self._worker()
        with mock.patch("zentao_auto_fixer.worker.bug_is_still_actionable", return_value=(True, "")):
            self.assertTrue(worker._still_actionable_before_writeback(SimpleNamespace(bug_id=7, event_action="")))
        state.update_status.assert_not_called()

    def test_writeback_guard_fails_open_on_read_error(self):
        worker, state = self._worker()
        with mock.patch(
            "zentao_auto_fixer.worker.bug_is_still_actionable",
            side_effect=ZenTaoPollError("boom"),
        ):
            self.assertTrue(worker._still_actionable_before_writeback(SimpleNamespace(bug_id=7, event_action="")))
        state.update_status.assert_not_called()

    def test_writeback_one_skips_comment_when_bug_already_handled(self):
        worker, state = self._worker()
        run = SimpleNamespace(bug_id=7, commit_hash="app:abc", event_action="")
        payload = {"cause": "原因", "solution": "方案", "commit_summary": "app:abc"}
        with mock.patch(
            "zentao_auto_fixer.worker.bug_is_still_actionable",
            return_value=(False, "ZenTao bug has been deleted"),
        ), mock.patch("zentao_auto_fixer.worker.comment_bug") as comment, mock.patch(
            "zentao_auto_fixer.worker.resolve_bug"
        ) as resolve:
            worker._writeback_one(run, payload)
        comment.assert_not_called()
        resolve.assert_not_called()

    def test_done_note_carries_reason_for_skipped_stale(self):
        worker, state = self._worker()
        state.get_run.return_value = SimpleNamespace(
            bug_id=7, status="skipped_stale", error="ZenTao status is now 'resolved', not active"
        )
        with mock.patch("zentao_auto_fixer.worker.add_comment") as add:
            worker._note_following_done(7)
        text = add.call_args.args[2]
        self.assertIn("跳过（无需处理）", text)
        self.assertIn("原因：ZenTao status is now 'resolved', not active", text)

    def test_fresh_title_preferred_over_queued_title(self):
        worker, state = self._worker()
        run = SimpleNamespace(bug_id=7, title="【ios】旧标题")
        with mock.patch("zentao_auto_fixer.worker.bug_fresh_title", return_value="【ui】【ios】新标题"):
            self.assertEqual(worker._fresh_title_or_old(run), "【ui】【ios】新标题")

    def test_fresh_title_falls_back_to_old_on_read_error(self):
        worker, state = self._worker()
        run = SimpleNamespace(bug_id=7, title="【ios】旧标题")
        with mock.patch("zentao_auto_fixer.worker.bug_fresh_title", side_effect=ZenTaoPollError("boom")):
            self.assertEqual(worker._fresh_title_or_old(run), "【ios】旧标题")

    def test_ui_tag_added_after_queuing_is_skipped_before_any_note(self):
        worker, state = self._worker()
        settings = SimpleNamespace(
            worker_count=1,
            zentao_client_script=Path("/tmp/z.py"),
            validate_for_worker=lambda: None,
            load_projects=lambda: [
                SimpleNamespace(name="p", process_ui_bugs=False)
            ],
        )
        worker.settings = settings
        state.get_run.return_value = SimpleNamespace(
            bug_id=7, title="【ios】旧标题（无 ui）", status="queued", project_name="p"
        )
        with mock.patch("zentao_auto_fixer.worker.bug_fresh_title", return_value="【ui】【ios】新标题"), mock.patch(
            "zentao_auto_fixer.worker.add_comment"
        ) as add:
            worker._process_bug(7)
        state.update_status.assert_called_once_with(
            7,
            "skipped_ui",
            error="标题带有 UI 标签，当前项目 processUiBugs=false，未调用 AI。",
            handled_once=False,
            completed=True,
        )
        add.assert_not_called()

    def test_manual_tag_added_after_queuing_is_skipped_before_any_note(self):
        worker, state = self._worker()
        settings = SimpleNamespace(
            worker_count=1,
            zentao_client_script=Path("/tmp/z.py"),
            validate_for_worker=lambda: None,
            load_projects=lambda: [
                SimpleNamespace(name="p", process_ui_bugs=False)
            ],
        )
        worker.settings = settings
        state.get_run.return_value = SimpleNamespace(
            bug_id=7, title="【ios】旧标题（无标签）", status="queued", project_name="p"
        )
        with mock.patch("zentao_auto_fixer.worker.bug_fresh_title", return_value="【人工】【ios】新标题"), mock.patch(
            "zentao_auto_fixer.worker.add_comment"
        ) as add:
            worker._process_bug(7)
        state.update_status.assert_called_once_with(
            7,
            "skipped_manual",
            error="标题带有人工标签，未调用 AI。",
            handled_once=False,
            completed=True,
        )
        add.assert_not_called()

    def test_manual_tag_detection(self):
        from zentao_auto_fixer.models import has_manual_tag

        self.assertTrue(has_manual_tag("【人工】【ios】标题"))
        self.assertTrue(has_manual_tag("[人工] 标题"))
        self.assertTrue(has_manual_tag("【 人工 】标题"))
        self.assertFalse(has_manual_tag("【ios】标题"))
        self.assertFalse(has_manual_tag("人工处理一下这个 bug"))

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
