import json
import tempfile
import unittest
from pathlib import Path

from zentao_auto_fixer.config import Settings
from zentao_auto_fixer.models import ProjectConfig, has_ui_tag, platforms_of
from zentao_auto_fixer.worker import _verdict_platforms


class PlatformDetectionTests(unittest.TestCase):
    def test_reads_platform_tags_from_the_title(self):
        self.assertEqual(platforms_of("【ios】【会话】点击进入"), ("ios",))
        self.assertEqual(platforms_of("【安卓】【首页】白屏"), ("android",))
        self.assertEqual(platforms_of("【macOS】崩溃"), ("mac",))
        self.assertEqual(platforms_of("[Windows] 客户端"), ("windows",))

    def test_title_without_a_platform_tag_reports_nothing(self):
        self.assertEqual(platforms_of("【会话窗口】没有平台标记"), ())
        self.assertEqual(platforms_of(""), ())

    def test_all_tagged_platforms_are_reported(self):
        self.assertEqual(set(platforms_of("【mac】【ios】双端都有")), {"mac", "ios"})


class UiTagDetectionTests(unittest.TestCase):
    def test_exact_ui_tags_are_case_insensitive(self):
        for title in ("【UI】按钮错位", "【ui】按钮错位", "[UI] 按钮错位", "[ui] 按钮错位", "[ Ui ] 按钮错位"):
            self.assertTrue(has_ui_tag(title), title)

    def test_plain_or_longer_ui_text_is_not_a_tag(self):
        for title in ("UI 按钮错位", "【UI设计】按钮错位", "[build] 修复 UI", "", "【会话】ui 问题"):
            self.assertFalse(has_ui_tag(title), title)


class SkipRuleTests(unittest.TestCase):
    def test_single_skipped_platform_is_skipped(self):
        self.assertTrue(_project(("android", "mac")).skips_platforms(("mac",)))

    def test_platform_that_is_not_skipped_runs(self):
        self.assertFalse(_project(("android", "mac")).skips_platforms(("ios",)))

    def test_cross_platform_bug_still_runs_when_only_one_side_is_skipped(self):
        # 【mac】【ios】 affects iOS too, so skipping mac must not drop it.
        self.assertFalse(_project(("android", "mac")).skips_platforms(("mac", "ios")))

    def test_untagged_bug_is_never_skipped_here(self):
        self.assertFalse(_project(("android", "mac")).skips_platforms(()))

    def test_nothing_is_skipped_without_the_setting(self):
        self.assertFalse(_project(()).skips_platforms(("android",)))

    def test_agent_verdict_of_both_or_unknown_never_triggers_a_skip(self):
        for platform in ("both", "unknown", "", "all"):
            self.assertEqual(_verdict_platforms({"platform": platform}), ())
        self.assertEqual(_verdict_platforms({"platform": "Android"}), ("android",))


class SkipConfigTests(unittest.TestCase):
    def test_skip_platforms_is_parsed_and_lowercased(self):
        self.assertEqual(_load({"skipPlatforms": ["Android", "MAC"]}).skip_platforms, ("android", "mac"))

    def test_absent_setting_skips_nothing(self):
        self.assertEqual(_load({}).skip_platforms, ())

    def test_unknown_platform_is_rejected(self):
        with self.assertRaises(ValueError):
            _load({"skipPlatforms": ["symbian"]})

    def test_ui_bugs_are_skipped_by_default_and_can_be_enabled(self):
        self.assertFalse(_load({}).process_ui_bugs)
        self.assertTrue(_load({"processUiBugs": True}).process_ui_bugs)

    def test_process_ui_bugs_must_be_a_json_boolean(self):
        for value in ("true", 1, None):
            with self.assertRaises(ValueError):
                _load({"processUiBugs": value})


def _project(skip_platforms, *, process_ui_bugs=False) -> ProjectConfig:
    return ProjectConfig(
        name="p",
        enabled=True,
        zentao_product_id=8,
        zentao_assigned_to="",
        repo_url="git@example.com:a.git",
        target_branch="dev",
        only_code_bugs=True,
        max_bugs_per_poll=1,
        skip_platforms=skip_platforms,
        process_ui_bugs=process_ui_bugs,
    )


def _load(extra: dict) -> ProjectConfig:
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


class MultiPlatformHandBackTests(unittest.TestCase):
    """A bug naming several platforms is a description problem, so it goes back to the reporter."""

    def test_multi_platform_bug_is_handed_back_without_calling_the_agent(self):
        from types import SimpleNamespace
        from unittest import mock

        from zentao_auto_fixer.worker import Worker

        project = _project(())
        settings = SimpleNamespace(
            worker_count=1,
            max_agent_runs_per_day=10,
            validate_for_worker=lambda: None,
            zentao_client_script=Path("/tmp/zentao_client.py"),
            load_projects=lambda: [project],
        )
        state = mock.Mock()
        state.daily_counter_value.return_value = 0
        state.get_run.return_value = SimpleNamespace(
            bug_id=1, status="queued", title="【mac】【ios】双端都有", opened_by="shuke",
            repo_url="r", project_name="p",
        )
        worker = Worker(settings, state)
        worker._stale_reason = lambda bug_id: ""
        worker._record_unable_to_fix = mock.Mock()
        worker._process_batch = mock.Mock()

        worker._process_bug(1)

        worker._record_unable_to_fix.assert_called_once()
        verdict = worker._record_unable_to_fix.call_args.args[1]
        self.assertIn("mac", verdict["understanding"])
        self.assertIn("ios", verdict["understanding"])
        self.assertIn("拆成多条", verdict["missing"])
        # The whole point is that this costs no agent run.
        self.assertEqual(worker.agent_runs_today(), 0)

    def test_single_platform_bug_is_not_handed_back(self):
        from types import SimpleNamespace
        from unittest import mock

        from zentao_auto_fixer.worker import Worker

        project = _project(())
        settings = SimpleNamespace(
            worker_count=1,
            max_agent_runs_per_day=10,
            validate_for_worker=lambda: None,
            load_projects=lambda: [project],
            zentao_client_script=Path("/tmp/zentao_client.py"),
        )
        state = mock.Mock()
        state.daily_counter_value.return_value = 0
        state.claim_daily_counter.return_value = True
        state.get_run.return_value = SimpleNamespace(
            bug_id=1, status="queued", title="【ios】【会话】单端", opened_by="shuke",
            repo_url="r", project_name="p",
        )
        worker = Worker(settings, state)
        worker._stale_reason = lambda bug_id: ""
        worker._record_unable_to_fix = mock.Mock()
        worker._process_batch = mock.Mock()

        worker._process_bug(1)

        worker._record_unable_to_fix.assert_not_called()


class UiWorkerGateTests(unittest.TestCase):
    def test_queued_ui_bug_is_skipped_before_any_agent_budget_is_used(self):
        from types import SimpleNamespace
        from unittest import mock

        from zentao_auto_fixer.worker import Worker

        project = _project(())
        settings = SimpleNamespace(
            worker_count=1,
            max_agent_runs_per_day=10,
            validate_for_worker=lambda: None,
            load_projects=lambda: [project],
        )
        state = mock.Mock()
        state.get_run.return_value = SimpleNamespace(
            bug_id=1,
            status="queued",
            title="【UI】按钮错位",
            repo_url=project.repo_url,
            project_name=project.name,
        )
        worker = Worker(settings, state)

        worker._process_bug(1)

        state.update_status.assert_called_once_with(
            1,
            "skipped_ui",
            error="标题带有 UI 标签，当前项目 processUiBugs=false，未调用 AI。",
            handled_once=False,
            completed=True,
        )
        state.claim_daily_counter.assert_not_called()
