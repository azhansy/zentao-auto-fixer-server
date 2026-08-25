import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from zentao_auto_fixer.agent_runner import (
    AgentError,
    TriageResultError,
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
            path = _write(tmp, {"bugs": [{"id": 1, "decision": "fixed", "steps": "1. 开 App\n2. 点按钮"}]})
            self.assertEqual(read_triage_result(path, [1])[1]["steps"], ["1. 开 App", "2. 点按钮"])

    def test_unknown_target_names_are_dropped(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = _write(tmp, {"bugs": [{"id": 1, "decision": "fixed", "targets": ["app", "android"]}]})
            self.assertEqual(read_triage_result(path, [1])[1]["targets"], ["app"])


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


class CallSiteTests(unittest.TestCase):
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


class ZenTaoBugFieldTests(unittest.TestCase):
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
