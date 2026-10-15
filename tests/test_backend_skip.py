"""skipBackendBugs：分诊说是后端问题就整条让给后端同事，不提交不推送，只在禅道回一条交接说明。"""
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from zentao_auto_fixer.agent_runner import _batch_prompt
from zentao_auto_fixer.config import Settings
from zentao_auto_fixer.worker import Worker, _FOLLOWING_DONE_TEXT


class SkipBackendConfigTests(unittest.TestCase):
    def test_absent_flag_keeps_fixing_backend_bugs(self):
        self.assertFalse(_load({}).skip_backend_bugs)

    def test_flag_is_parsed(self):
        self.assertTrue(_load({"skipBackendBugs": True}).skip_backend_bugs)

    def test_flag_must_be_a_json_boolean(self):
        for value in ("true", 1, None):
            with self.assertRaises(ValueError):
                _load({"skipBackendBugs": value})


class SkipBackendPromptTests(unittest.TestCase):
    def test_flag_tells_the_agent_to_hand_backend_bugs_back(self):
        prompt = _prompt(skip_backend=True)
        self.assertIn("本项目不处理后端问题", prompt)
        # 交接全靠 targets 认出来：json 格式那一段的 rejected 例子没有 targets，必须单独说明。
        self.assertIn('targets 填 ["backend"]', prompt)
        self.assertIn('必须写 targets: ["backend"]', prompt)
        # 后端仓库仍然拉取，但只用于判断问题出没出在后端。
        self.assertIn("只读", prompt)

    def test_default_prompt_keeps_fixing_backend_bugs(self):
        prompt = _prompt(skip_backend=False)
        self.assertIn("确认是后端问题就改 backend", prompt)
        self.assertNotIn("本项目不处理后端问题", prompt)


class BackendHandoffTests(unittest.TestCase):
    def test_backend_verdict_is_handed_back_instead_of_fixed(self):
        worker, state, runner = self._process({"decision": "rejected", "targets": ["backend"]})

        state.update_status.assert_called_once_with(7, "skipped_backend", error=mock.ANY, completed=True)
        state.record_run_event.assert_any_call(7, "skipped_backend", mock.ANY)
        worker._commit_push_and_resolve.assert_not_called()
        self.assertTrue(runner.call_args.kwargs["skip_backend"])

    def test_handed_back_batch_does_not_trip_the_no_progress_fuse(self):
        worker, _state, _runner = self._process({"decision": "rejected", "targets": ["backend"]})

        worker._record_no_progress.assert_not_called()

    def test_backend_worktree_changes_hand_the_bug_back_even_if_the_verdict_says_app(self):
        # AI 自己报的 targets 不算数时才靠这条：工作区真动了 backend 就不能推出去。
        worker, state, _runner = self._process(
            {"decision": "fixed", "targets": ["app"], "platform": "ios"},
            backend_has_work=True,
        )

        state.update_status.assert_called_once_with(7, "skipped_backend", error=mock.ANY, completed=True)
        worker._commit_push_and_resolve.assert_not_called()

    def test_app_bug_is_still_fixed_with_the_flag_on(self):
        worker, state, _runner = self._process(
            {"decision": "fixed", "targets": ["app"], "platform": "ios", "solution": "s"}
        )

        worker._commit_push_and_resolve.assert_called_once()
        worker._record_no_progress.assert_not_called()
        self.assertFalse(
            any(call.args[1] == "skipped_backend" for call in state.update_status.call_args_list)
        )

    def _process(self, verdict, backend_has_work=False):
        run = SimpleNamespace(bug_id=7, title="【ios】列表金额算错", project_name="p",
                              target_branch="dev", repo_url="repo")
        state = mock.Mock()
        state.claim_queued_batch.return_value = [run]
        state.get_run.return_value = SimpleNamespace(status="running")
        state.list_run_events.return_value = []
        settings = SimpleNamespace(
            worker_count=1,
            zentao_client_script=Path("/tmp/z.py"),
            logs_dir=Path("/tmp"),
        )
        worker = Worker(settings, state)
        worker._prepare_checkout = mock.Mock(
            side_effect=lambda *args: SimpleNamespace(
                kind=args[1],
                worktree=Path(f"/wt/{args[1]}"),
                repo_cache=Path(f"/repos/{args[1]}"),
                repo_url=args[2],
                target_branch=args[3],
                delivery_mode="push",
                has_work=lambda: args[1] == "backend" and backend_has_work,
            )
        )
        worker._run_agent_batch_with_retries = mock.Mock(return_value={7: verdict})
        worker._commit_push_and_resolve = mock.Mock()
        worker._record_no_progress = mock.Mock()
        project = SimpleNamespace(
            process_ui_bugs=False,
            skip_backend_bugs=True,
            has_backend_repo=backend_has_work,
            backend_repo_url="backend-repo",
            backend_target_branch="pre_release",
            backend_delivery_mode="push",
            delivery_mode="push",
            agent="claude",
            fallback_agent="",
            allow_full_xcodebuild=False,
            skips_platforms=lambda platforms: False,
        )

        with mock.patch("zentao_auto_fixer.worker.add_comment"), \
                mock.patch("zentao_auto_fixer.worker.remove_worktree"), \
                mock.patch("zentao_auto_fixer.worker.bug_occupancy", return_value=""), \
                mock.patch("zentao_auto_fixer.worker.bug_fresh_title", return_value=run.title):
            worker._process_batch(7, project)

        return worker, state, worker._run_agent_batch_with_retries


class BackendHandoffReplyTests(unittest.TestCase):
    """「回复一下不处理，让后端同事跟进」就是这条跟进完成备注，测试同事在禅道上看得见。"""

    def test_handoff_note_asks_backend_colleagues_to_take_over(self):
        state = mock.Mock()
        state.get_run.return_value = SimpleNamespace(
            bug_id=7, status="skipped_backend", error="分诊判定这条 Bug 的根因在后端代码。"
        )
        worker = Worker(SimpleNamespace(worker_count=1, zentao_client_script=Path("/tmp/z.py")), state)

        with mock.patch("zentao_auto_fixer.worker.add_comment") as add:
            worker._note_following_done(7)

        text = add.call_args.args[2]
        self.assertIn("后端同事跟进", text)
        self.assertIn("原因：", text)

    def test_handoff_reply_is_wired_to_the_skip_status(self):
        self.assertIn("后端同事跟进", _FOLLOWING_DONE_TEXT["skipped_backend"])


class BackendSkipClassificationTests(unittest.TestCase):
    def test_handed_off_bugs_are_terminal_so_the_poller_leaves_them_alone(self):
        from zentao_auto_fixer.models import TERMINAL_STATUSES

        self.assertIn("skipped_backend", TERMINAL_STATUSES)

    def test_daily_report_counts_a_handoff_as_skipped_not_failed(self):
        from zentao_auto_fixer.daily_report import EVENT_RESULTS

        self.assertEqual(EVENT_RESULTS["skipped_backend"], "跳过")


def _prompt(*, skip_backend) -> str:
    return _batch_prompt(
        [(7, "【ios】列表金额算错")],
        Path("/tmp/zentao_client.py"),
        Path("/wt/app"),
        Path("/wt/backend"),
        Path("/tmp/result.json"),
        skip_backend=skip_backend,
    )


def _load(extra: dict) -> object:
    import os

    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "projects.json"
        project = {"name": "A", "zentaoProductId": 8, "repoUrl": "git@e:a.git", "targetBranch": "dev"}
        project.update(extra)
        path.write_text(json.dumps({"projects": [project]}), encoding="utf-8")
        old = os.environ.get("AUTO_FIXER_PROJECTS_FILE")
        os.environ["AUTO_FIXER_PROJECTS_FILE"] = str(path)
        try:
            return Settings.from_env().load_projects()[0]
        finally:
            if old is None:
                os.environ.pop("AUTO_FIXER_PROJECTS_FILE", None)
            else:
                os.environ["AUTO_FIXER_PROJECTS_FILE"] = old


if __name__ == "__main__":
    unittest.main()
