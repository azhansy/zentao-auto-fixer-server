import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from zentao_auto_fixer.agent_runner import (
    AgentError,
    AgentQuotaError,
    TriageResultError,
    _agent_quota_exhausted,
    _batch_prompt,
    _descendant_process_groups,
    build_agent_command,
    read_triage_result,
)
from zentao_auto_fixer.worker import _cause_text
from zentao_auto_fixer.zentao import _candidate_from_bug, bug_has_ai_comment


class TriageResultTests(unittest.TestCase):
    def test_reads_fixed_and_rejected_verdicts(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = _write(
                tmp,
                {
                    "bugs": [
                        {
                            "id": 1,
                            "decision": "fixed",
                            "targets": ["backend", "app"],
                            "platform": "IOS",
                            "understanding": "u",
                            "steps": ["  开 App  ", "", "点按钮"],
                            "cause": "c",
                            "solution": "s",
                            "verification": {"command": "swift test", "passed": True},
                        },
                        {"id": 2, "decision": "rejected", "reason": "r", "missing": "m"},
                    ]
                },
            )
            verdicts = read_triage_result(path, [1, 2])

            self.assertEqual(verdicts[1]["decision"], "fixed")
            self.assertEqual(verdicts[1]["targets"], ["app", "backend"])
            self.assertEqual(verdicts[1]["platform"], "ios")
            self.assertEqual(verdicts[1]["understanding"], "u")
            self.assertEqual(verdicts[1]["steps"], ["开 App", "点按钮"])
            self.assertEqual(verdicts[2]["decision"], "rejected")
            self.assertEqual(verdicts[2]["missing"], "m")

    def test_missing_verdict_is_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = _write(tmp, {"bugs": [{"id": 1, "decision": "fixed"}]})
            with self.assertRaises(TriageResultError):
                read_triage_result(path, [1, 2])

    def test_unknown_decision_is_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = _write(tmp, {"bugs": [{"id": 1, "decision": "maybe"}]})
            with self.assertRaises(TriageResultError):
                read_triage_result(path, [1])

    def test_absent_file_is_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(TriageResultError):
                read_triage_result(Path(tmp) / "nope.json", [1])

    def test_steps_sent_as_one_string_are_split(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = _write(tmp, {"bugs": [{"id": 1, "decision": "fixed", "steps": "1. 开 App\n2. 点按钮", "verification": {"command": "swift test", "passed": True}}]})
            self.assertEqual(read_triage_result(path, [1])[1]["steps"], ["1. 开 App", "2. 点按钮"])

    def test_unknown_target_names_are_dropped(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = _write(tmp, {"bugs": [{"id": 1, "decision": "fixed", "targets": ["app", "android"], "verification": {"command": "swift test", "passed": True}}]})
            self.assertEqual(read_triage_result(path, [1])[1]["targets"], ["app"])

    def test_fixed_without_a_passing_test_is_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = _write(tmp, {"bugs": [{"id": 1, "decision": "fixed"}]})
            with self.assertRaisesRegex(TriageResultError, "without a synchronous passing verification"):
                read_triage_result(path, [1])


class CommentTextTests(unittest.TestCase):
    def test_understanding_and_steps_come_before_the_cause(self):
        text = _cause_text(
            {"understanding": "重复响铃", "steps": ["开会话", "两台登录"]},
            "原因分析",
            "推送没去重",
        )
        self.assertLess(text.index("AI 理解的问题"), text.index("复现步骤"))
        self.assertLess(text.index("复现步骤"), text.index("原因分析"))
        self.assertIn("1. 开会话", text)
        self.assertIn("2. 两台登录", text)

    def test_steps_that_already_carry_numbers_are_not_renumbered(self):
        text = _cause_text({"understanding": "x", "steps": ["1. 开会话"]}, "原因分析", "y")
        self.assertIn("1. 开会话", text)
        self.assertNotIn("1. 1. 开会话", text)

    def test_missing_understanding_and_steps_still_produce_a_comment(self):
        self.assertEqual(_cause_text({}, "原因分析", "y"), "【原因分析】\ny")


class AgentCommandTests(unittest.TestCase):
    def test_prompt_blocks_full_xcodebuild_by_default(self):
        prompt = _batch_prompt(
            [(1, "bug")],
            Path("/tmp/zentao.py"),
            Path("/wt/app"),
            None,
            Path("/tmp/result.json"),
        )
        self.assertIn("禁止运行 xcodebuild build/archive 等完整构建", prompt)
        self.assertIn("只运行与改动直接相关的测试用例", prompt)

    def test_prompt_can_allow_full_xcodebuild(self):
        prompt = _batch_prompt(
            [(1, "bug")],
            Path("/tmp/zentao.py"),
            Path("/wt/app"),
            None,
            Path("/tmp/result.json"),
            allow_full_xcodebuild=True,
        )
        self.assertIn("可以按需运行完整 xcodebuild 构建", prompt)

    def test_conflict_prompt_requires_preserving_both_sides_and_retesting(self):
        prompt = _batch_prompt(
            [(1, "bug")],
            Path("/tmp/zentao.py"),
            Path("/wt/app"),
            None,
            Path("/tmp/result.json"),
            conflict_context="app 仓库 /wt/app",
        )
        self.assertIn("同时保留远端的新改动和本次 Bug 修复意图", prompt)
        self.assertIn("解决后重新运行与改动直接相关的测试", prompt)

    def test_timeout_cleanup_includes_detached_descendant_process_groups(self):
        process_list = mock.Mock(
            returncode=0,
            stdout="100 1 100\n101 100 100\n200 101 200\n201 200 201\n999 1 999\n",
        )
        with mock.patch("zentao_auto_fixer.agent_runner.subprocess.run", return_value=process_list):
            self.assertEqual(_descendant_process_groups(100), [200, 201, 100])

    def test_codex_runs_in_the_app_worktree(self):
        cmd = build_agent_command("codex", "codex", Path("/wt/app"), [Path("/wt/api")], "P")
        self.assertEqual(cmd[:4], ["codex", "exec", "--cd", "/wt/app"])
        self.assertEqual(cmd[-1], "P")

    def test_claude_gets_the_backend_worktree_as_an_extra_dir(self):
        cmd = build_agent_command("claude", "claude", Path("/wt/app"), [Path("/wt/api")], "P")
        self.assertEqual(cmd[:4], ["claude", "-p", "P", "--dangerously-skip-permissions"])
        self.assertEqual(cmd[-2:], ["--add-dir", "/wt/api"])

    def test_prompt_is_a_single_argument_for_both_agents(self):
        for agent in ("codex", "claude"):
            cmd = build_agent_command(agent, agent, Path("/wt/app"), [], "a b\nc")
            self.assertEqual(cmd.count("a b\nc"), 1, agent)

    def test_unknown_agent_is_rejected(self):
        with self.assertRaises(AgentError):
            build_agent_command("gemini", "gemini", Path("/wt/app"), [], "P")

    def test_only_claude_quota_messages_trigger_fallback(self):
        self.assertTrue(_agent_quota_exhausted("claude", "You've hit your limit · resets at 1am"))
        self.assertTrue(_agent_quota_exhausted("claude", "Credit balance is too low"))
        self.assertFalse(_agent_quota_exhausted("claude", "authentication failed"))
        self.assertFalse(_agent_quota_exhausted("claude", "rate limit exceeded"))
        self.assertFalse(_agent_quota_exhausted("codex", "You've hit your limit"))


class CallSiteTests(unittest.TestCase):
    def test_rebase_conflict_keeps_resolving_until_remote_is_current(self):
        from types import SimpleNamespace

        from zentao_auto_fixer.git_ops import RebaseConflictError
        from zentao_auto_fixer.worker import Worker

        settings = SimpleNamespace(
            worker_count=1,
            git_timeout_seconds=30,
            git_shallow_clone=True,
            logs_dir=Path("/logs"),
            codex_timeout_seconds=60,
            zentao_client_script=Path("/tmp/zentao.py"),
            agent_bin=lambda agent: agent,
        )
        state = mock.Mock()
        worker = Worker(settings, state)
        worker._claim_agent_run = mock.Mock(return_value=True)
        checkout = SimpleNamespace(
            kind="app",
            worktree=Path("/wt/app"),
            target_branch="dev",
            baseline="old",
        )
        run = SimpleNamespace(bug_id=1, title="bug")
        verdicts = {1: {"decision": "fixed", "solution": "old"}}
        resolved = {1: {"decision": "fixed", "solution": "merged", "targets": ["app"]}}

        with mock.patch(
            "zentao_auto_fixer.worker.rebase_onto_latest_remote",
            side_effect=[
                RebaseConflictError("conflict", "latest-1"),
                RebaseConflictError("conflict again", "latest-2"),
                "latest-2",
            ],
        ), mock.patch(
            "zentao_auto_fixer.worker.run_agent_batch_fix", return_value=resolved
        ) as agent, mock.patch(
            "zentao_auto_fixer.worker.continue_rebase"
        ) as continue_rebase, mock.patch(
            "zentao_auto_fixer.worker.head_commit", return_value="merged-head"
        ):
            self.assertTrue(
                worker._rebase_changed_checkouts(
                    [run],
                    [checkout],
                    {"app": checkout},
                    verdicts,
                    "#1",
                    "claude",
                    False,
                )
            )

        self.assertEqual(worker._claim_agent_run.call_count, 2)
        self.assertIn("conflict_context", agent.call_args.kwargs)
        self.assertEqual(continue_rebase.call_count, 2)
        self.assertEqual(checkout.baseline, "latest-2")
        self.assertEqual(verdicts[1]["solution"], "merged")

    def test_writeback_retry_never_runs_the_agent(self):
        from types import SimpleNamespace

        from zentao_auto_fixer.worker import Worker

        state = mock.Mock()
        worker = Worker(SimpleNamespace(worker_count=1, zentao_client_script=Path("/tmp/z.py")), state)
        run = SimpleNamespace(
            bug_id=1,
            commit_hash="app:abc",
            writeback_payload=json.dumps(
                {"cause": "原因", "solution": "方案", "commit_summary": "app:abc"}
            ),
        )

        with mock.patch("zentao_auto_fixer.worker.comment_bug") as comment, mock.patch(
            "zentao_auto_fixer.worker.resolve_bug"
        ) as resolve, mock.patch("zentao_auto_fixer.worker.run_agent_batch_fix") as agent:
            worker._retry_writeback(run)

        comment.assert_called_once()
        resolve.assert_called_once()
        agent.assert_not_called()
        state.update_status.assert_called_once_with(
            1,
            "pushed",
            error="",
            commit_hash="app:abc",
            handled_once=True,
            completed=True,
        )

    def test_unable_to_fix_is_local_only(self):
        from types import SimpleNamespace

        from zentao_auto_fixer.worker import Worker

        state = mock.Mock()
        worker = Worker(SimpleNamespace(worker_count=1), state)
        run = SimpleNamespace(bug_id=1)

        with mock.patch("zentao_auto_fixer.worker.comment_bug") as comment:
            worker._record_unable_to_fix(run, {"reason": "定位不到", "missing": "日志"})

        comment.assert_not_called()
        state.mark_unable_to_fix.assert_called_once()

    def test_failure_state_change_never_writes_zentao(self):
        from types import SimpleNamespace

        from zentao_auto_fixer.worker import Worker

        state = mock.Mock()
        worker = Worker(SimpleNamespace(worker_count=1), state)
        run = SimpleNamespace(bug_id=1)

        with mock.patch("zentao_auto_fixer.worker.comment_bug") as comment:
            worker._fail_batch([run], "failed", "boom", "")

        comment.assert_not_called()
        state.update_status.assert_called_once_with(
            1,
            "failed",
            error="boom",
            commit_hash="",
            handled_once=True,
            completed=True,
        )
        state.record_run_event.assert_called_once_with(1, "failed", "boom")

    def test_batch_failure_stays_local_instead_of_writing_zentao(self):
        from types import SimpleNamespace

        from zentao_auto_fixer.worker import Worker

        run = SimpleNamespace(
            bug_id=1,
            title="bug",
            project_name="p",
            target_branch="dev",
            repo_url="repo",
        )
        state = mock.Mock()
        state.claim_queued_batch.return_value = [run]
        state.get_run.return_value = SimpleNamespace(status="running")
        worker = Worker(SimpleNamespace(worker_count=1), state)
        worker._prepare_checkout = mock.Mock(side_effect=RuntimeError("boom"))
        worker._fail_batch = mock.Mock()
        project = SimpleNamespace(
            max_bugs_per_poll=3,
            process_ui_bugs=False,
            has_backend_repo=False,
        )

        worker._process_batch_with_repo_lock(1, project)

        worker._fail_batch.assert_called_once_with([run], "failed", "boom", "", count_no_progress=True)

    def test_worker_calls_the_runner_with_a_valid_argument_list(self):
        """autospec rejects a call that no longer matches the runner's signature, which unit tests
        of the runner alone would never catch - it only blows up at run time."""
        from types import SimpleNamespace
        from unittest import mock

        from zentao_auto_fixer.worker import Worker

        settings = SimpleNamespace(
            worker_count=1,
            max_agent_runs_per_day=10,
            codex_attempts=1,
            codex_timeout_seconds=60,
            zentao_client_script=Path("/tmp/zentao_client.py"),
            agent_bin=lambda agent: agent,
        )
        worker = Worker(settings, mock.Mock())
        checkout = SimpleNamespace(worktree=Path("/wt/app"), baseline="abc", kind="app")
        batch = [SimpleNamespace(bug_id=1, title="t")]

        with mock.patch(
            "zentao_auto_fixer.worker.run_agent_batch_fix", autospec=True, return_value={1: {}}
        ) as runner:
            worker._run_agent_batch_with_retries(
                batch, "codex", {"app": checkout}, Path("/logs/r.json"), Path("/logs/a.log")
            )
        self.assertEqual(runner.call_count, 1)
        passed = runner.call_args.args
        self.assertIn(settings.zentao_client_script, passed)

    def test_claude_quota_falls_back_to_codex_and_counts_the_extra_start(self):
        from types import SimpleNamespace

        from zentao_auto_fixer.worker import Worker

        settings = SimpleNamespace(
            worker_count=1,
            max_agent_runs_per_day=20,
            codex_attempts=1,
            codex_retry_delay_seconds=0,
            codex_timeout_seconds=60,
            zentao_client_script=Path("/tmp/zentao_client.py"),
            agent_bin=lambda agent: agent,
        )
        state = mock.Mock()
        state.claim_daily_counter.return_value = True
        state.daily_counter_value.return_value = 2
        worker = Worker(settings, state)
        checkout = SimpleNamespace(worktree=Path("/wt/app"), baseline="abc", kind="app")
        batch = [SimpleNamespace(bug_id=1, title="t")]
        verdict = {1: {"decision": "fixed"}}

        with mock.patch(
            "zentao_auto_fixer.worker.run_agent_batch_fix",
            autospec=True,
            side_effect=[AgentQuotaError("You've hit your limit"), verdict],
        ) as runner, mock.patch("zentao_auto_fixer.worker.reset_hard_clean") as reset:
            actual = worker._run_agent_batch_with_retries(
                batch,
                "claude",
                {"app": checkout},
                Path("/logs/r.json"),
                Path("/logs/a.log"),
                fallback_agent="codex",
            )

        self.assertEqual(actual, verdict)
        self.assertEqual([call.args[0] for call in runner.call_args_list], ["claude", "codex"])
        state.claim_daily_counter.assert_called_once()
        reset.assert_called_once_with(checkout.worktree, checkout.baseline)
        state.record_run_events.assert_any_call([1], "agent_fallback", "claude quota exhausted; switching to codex")


class ZenTaoBugFieldTests(unittest.TestCase):
    def test_bug_list_helper_requests_the_configured_page_limit(self):
        from types import SimpleNamespace

        from zentao_auto_fixer.zentao import _list_project_bugs_with_helper

        project = SimpleNamespace(
            zentao_product_id=10,
            only_code_bugs=True,
            zentao_assigned_to="",
        )
        completed = mock.Mock(returncode=0, stdout="[]", stderr="")
        with mock.patch.dict("os.environ", {"AUTO_FIXER_ZENTAO_PAGE_LIMIT": "123"}), mock.patch(
            "zentao_auto_fixer.zentao.subprocess.run", return_value=completed
        ) as run:
            self.assertEqual(_list_project_bugs_with_helper(Path("/tmp/zentao.py"), project), [])

        command = run.call_args.args[0]
        self.assertIn("--limit", command)
        self.assertEqual(command[command.index("--limit") + 1], "123")

    def test_opened_by_account_is_extracted(self):
        bug = _candidate_from_bug(8, {"id": 1, "title": "t", "openedBy": {"account": "shuke", "realname": "舒克"}})
        self.assertEqual(bug.opened_by, "shuke")

    def test_opened_by_plain_string_is_kept(self):
        self.assertEqual(_candidate_from_bug(8, {"id": 1, "openedBy": "shuke"}).opened_by, "shuke")

    def test_ai_comment_marker_is_detected_in_history(self):
        without = {"actions": [{"comment": "麻烦看下"}, {"comment": ""}]}
        withmark = {
            "actions": [
                {"comment": "麻烦看下"},
                {"comment": "问题原因：x\n---------\n通过 &lt;zentao-bug-fixer&gt; Skill 自动完成问题分析与修复。"},
            ]
        }
        self.assertFalse(_marker_seen(without))
        self.assertTrue(_marker_seen(withmark))

    def test_owner_approved_manual_retry_can_ignore_an_old_ai_marker(self):
        from zentao_auto_fixer.zentao import bug_is_still_actionable

        detail = {
            "id": 1,
            "status": "active",
            "actions": [{"comment": "通过 <zentao-bug-fixer> Skill 自动完成问题分析与修复。"}],
        }
        with mock.patch("zentao_auto_fixer.zentao._bug_detail", return_value=detail):
            self.assertEqual(
                bug_is_still_actionable(Path("/tmp/zentao.py"), 1),
                (False, "ZenTao already carries an AI comment for this bug"),
            )
            self.assertEqual(
                bug_is_still_actionable(Path("/tmp/zentao.py"), 1, ignore_ai_comment=True),
                (True, ""),
            )

    def test_unreadable_history_blocks_instead_of_waving_the_bug_through(self):
        # Fail-closed: this gate exists to stop a second fix, so an odd response must not read as "fresh".
        from zentao_auto_fixer.zentao import ZenTaoPollError

        with self.assertRaises(ZenTaoPollError):
            _marker_seen({"id": 1})


def _marker_seen(detail: dict) -> bool:
    completed = mock.Mock(returncode=0, stdout=json.dumps(detail), stderr="")
    with mock.patch("zentao_auto_fixer.zentao.subprocess.run", return_value=completed):
        return bug_has_ai_comment(Path("/tmp/zentao_client.py"), 1)


def _write(tmp: str, payload: dict) -> Path:
    path = Path(tmp) / "triage.json"
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


if __name__ == "__main__":
    unittest.main()
