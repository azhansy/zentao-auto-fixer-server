import json
import os
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from tests.test_state import _bug, _project
from zentao_auto_fixer.cable_release import check_cable_release
from zentao_auto_fixer.gitlab import GitLabError
from zentao_auto_fixer.models import AI_COMMENT_MARKER
from zentao_auto_fixer.state import StateStore
from zentao_auto_fixer.worker import Worker
from zentao_auto_fixer.zentao import ZenTaoPollError, ZenTaoWriteError


class MergeRequestLifecycleTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.database = self.root / 'state.sqlite3'
        self.state = StateStore(self.database)
        self.project = replace(_project(), resolve_bug_after_fix=False)
        self.state.enqueue_first_run(_bug(9240), self.project)
        self.settings = SimpleNamespace(
            worker_count=1, data_dir=self.root, logs_dir=self.root / 'logs',
            zentao_client_script=Path('/unused-zentao-helper'),
            git_timeout_seconds=900, max_bug_retries=2,
            max_agent_runs_per_day=20,
        )
        self.worker = self.new_worker()
        self.requests = [
            {'kind': 'app', 'repo_url': self.project.repo_url,
             'url': 'https://gitlab.example/group/app/-/merge_requests/41',
             'source': 'feature/zentao-9240-app', 'target': 'main', 'sha': 'a' * 40},
            {'kind': 'backend', 'repo_url': 'git@example.com:group/api.git',
             'url': 'https://gitlab.example/group/api/-/merge_requests/42',
             'source': 'feature/zentao-9240-api', 'target': 'staging', 'sha': 'b' * 40},
        ]
        self.verdict = {
            'cause': '选中分组跨登录周期保留。',
            'solution': '登录周期结束时清理当前账号的选中状态。',
            'targets': ['app'], 'platform': 'ios',
        }
        self.add_note = self.patched('add_comment')
        self.final_note = self.patched('comment_bug')
        self.resolve = self.patched('resolve_bug')
        self.agent = self.patched('run_agent_batch_fix')
        self.reviewer = self.patched('run_agent_review')
        self.create_mr = self.patched('push_merge_request')
        self.gitlab = self.patched('GitLab')
        self.client = self.gitlab.return_value
        self.client.source_branch_exists.return_value = False
        environment = patch.dict(os.environ, {
            'AUTO_FIXER_GITLAB_REQUIRED_JOBS': 'lint,unit-test,build,integration',
        })
        environment.start()
        self.addCleanup(environment.stop)

    def patched(self, name):
        replacement = patch('zentao_auto_fixer.worker.' + name)
        mocked = replacement.start()
        self.addCleanup(replacement.stop)
        return mocked

    def new_worker(self):
        worker = Worker(self.settings, StateStore(self.database))
        worker._project_for = Mock(return_value=self.project)
        worker._still_actionable_before_writeback = Mock(return_value=True)
        return worker

    def seed_mr_delivery(self, requests=None):
        requests = self.requests if requests is None else requests
        commits = ' '.join(f'{item["kind"]}:{item["sha"]}' for item in requests)
        self.worker._comment_and_resolve(
            [self.state.get_run(9240)], {9240: self.verdict}, commits, requests,
        )

    def payload(self):
        return json.loads(self.state.get_run(9240).writeback_payload)

    def jobs(self, status='success'):
        return [
            {'id': index, 'name': name, 'status': status, 'allow_failure': False}
            for index, name in enumerate(('lint', 'unit-test', 'build', 'integration'), 1)
        ]

    def opened_mr(self, item=None, status='pending', **fields):
        item = self.requests[0] if item is None else item
        return {
            'state': 'opened', 'auto_merge_enabled': True,
            'head_pipeline': {'id': 17, 'sha': item['sha'], 'status': status},
            **fields,
        }

    def check_twice(self, worker=None):
        worker = self.worker if worker is None else worker
        for _ in range(2):
            worker._check_merge_requests(self.state.get_run(9240))
            worker._note_following_done(9240)

    def test_created_mrs_are_announced_without_a_success_marker_or_resolution(self):
        self.seed_mr_delivery()

        self.add_note.assert_called_once()
        text = self.add_note.call_args.args[2]
        for item in self.requests:
            for value in (item['url'], item['target'], item['sha'][:12]):
                self.assertIn(value, text)
        self.assertIn('尚未完成交付', text)
        self.assertNotIn(AI_COMMENT_MARKER, text)
        self.final_note.assert_not_called()
        self.resolve.assert_not_called()
        self.assertEqual(self.state.get_run(9240).status, 'awaiting_merge')
        self.assertEqual(self.payload()['merge_requests'], self.requests)

    def test_pending_notice_survives_worker_and_sqlite_store_recreation(self):
        self.seed_mr_delivery()
        self.client.inspect.return_value = (self.opened_mr(), self.jobs('pending'))

        self.check_twice(self.new_worker())

        self.add_note.assert_called_once()
        self.create_mr.assert_not_called()
        self.agent.assert_not_called()
        self.assertEqual(self.state.get_run(9240).status, 'awaiting_merge')

    def test_failed_notice_is_retried_from_the_stored_mr_without_recreating_it(self):
        self.add_note.side_effect = [ZenTaoWriteError('暂时无法回写'), None]
        self.seed_mr_delivery()
        self.assertNotIn('mr_note', self.payload())
        self.assertIn('mr_comment_failed', [e['event'] for e in self.state.list_run_events(9240)])
        self.client.inspect.return_value = (self.opened_mr(), self.jobs('pending'))

        self.check_twice(self.new_worker())

        self.assertEqual(self.add_note.call_count, 2)
        self.assertEqual(self.add_note.call_args_list[0].args, self.add_note.call_args_list[1].args)
        self.assertTrue(self.payload()['mr_note'])
        self.create_mr.assert_not_called()
        self.agent.assert_not_called()
        self.resolve.assert_not_called()

    def test_same_blocker_does_not_alternate_pending_and_failed_notices(self):
        self.seed_mr_delivery()
        self.client.inspect.side_effect = GitLabError('必需 CI 任务缺失')

        self.check_twice()

        self.assertEqual(self.add_note.call_count, 2)
        self.assertIn('尚未完成交付', self.add_note.call_args_list[0].args[2])
        self.assertIn('必需 CI 任务缺失', self.add_note.call_args_list[1].args[2])
        self.assertEqual(self.state.get_run(9240).status, 'merge_request_failed')
        self.agent.assert_not_called()
        self.resolve.assert_not_called()

    def test_regular_work_precedes_older_idle_mr_work(self):
        self.worker.enqueue(1, idle=True)
        self.worker.enqueue(9000)
        self.worker.enqueue(8000)
        self.worker.enqueue(1, idle=True)

        self.assertEqual([self.worker.queue.get_nowait()[1] for _ in range(3)], [8000, 9000, 1])
        self.assertTrue(self.worker.queue.empty())

    def test_green_ci_with_a_conflict_or_required_rebase_repairs_the_same_mr(self):
        self.seed_mr_delivery()
        self.worker._repair_merge_request = Mock()
        for fields in ({'has_conflicts': True}, {'detailed_merge_status': 'need_rebase'}):
            with self.subTest(fields=fields):
                self.worker._repair_merge_request.reset_mock()
                self.client.inspect.return_value = (
                    self.opened_mr(status='success', **fields), self.jobs(),
                )

                self.worker._check_merge_requests(self.state.get_run(9240))

                repair = self.worker._repair_merge_request
                repair.assert_called_once()
                self.assertTrue(repair.call_args.kwargs['merge_target'])
                self.assertEqual(repair.call_args.args[2], self.requests[0])
                self.assertEqual(repair.call_args.args[1]['merge_requests'], self.requests)
                self.create_mr.assert_not_called()

    def test_failed_ci_passes_real_failure_context_to_the_original_mr_repair(self):
        self.seed_mr_delivery()
        self.worker._repair_merge_request = Mock()
        self.client.inspect.return_value = (self.opened_mr(status='failed'), self.jobs('failed'))
        self.client.failed_logs.return_value = 'unit-test: assertion failed in login cycle'

        self.worker._check_merge_requests(self.state.get_run(9240))

        repair = self.worker._repair_merge_request
        repair.assert_called_once()
        self.assertEqual(repair.call_args.args[2]['url'], self.requests[0]['url'])
        self.assertEqual(repair.call_args.args[3], self.client.failed_logs.return_value)
        self.assertFalse(repair.call_args.kwargs.get('merge_target', False))
        self.create_mr.assert_not_called()
        self.final_note.assert_not_called()

    def test_pending_ci_and_approval_waiting_never_start_a_model(self):
        self.seed_mr_delivery()
        self.worker._repair_merge_request = Mock()
        for fields in ({}, {'detailed_merge_status': 'not_approved'}):
            with self.subTest(fields=fields):
                self.client.inspect.return_value = (
                    self.opened_mr(**fields), self.jobs('pending'),
                )
                self.check_twice()

        self.worker._repair_merge_request.assert_not_called()
        self.agent.assert_not_called()
        self.reviewer.assert_not_called()
        self.assertEqual(self.payload()['ci_attempts'], 0)
        self.resolve.assert_not_called()

    def test_api_errors_and_configuration_blockers_do_not_start_a_model(self):
        self.seed_mr_delivery()
        self.worker._repair_merge_request = Mock()
        for message in ('GitLab GET: HTTP Error 503', 'GitLab GET: HTTP Error 403',
                        'MR head or target changed outside this task'):
            with self.subTest(message=message):
                self.client.inspect.side_effect = GitLabError(message)
                self.check_twice()
        self.client.inspect.side_effect = None
        self.client.inspect.return_value = (self.opened_mr(status='success'), self.jobs()[:-1])
        self.check_twice()
        self.client.inspect.return_value = (
            self.opened_mr(status='success', auto_merge_enabled=False), self.jobs(),
        )
        self.client.enable_auto_merge.side_effect = GitLabError(
            'GitLab must require a successful pipeline before auto merge',
        )
        self.check_twice()

        self.worker._repair_merge_request.assert_not_called()
        self.agent.assert_not_called()
        self.reviewer.assert_not_called()
        self.assertEqual(self.payload()['ci_attempts'], 0)
        self.resolve.assert_not_called()

    def test_human_resolution_stops_mr_inspection_and_repair(self):
        self.seed_mr_delivery()
        self.worker._still_actionable_before_writeback.return_value = False
        self.worker._repair_merge_request = Mock()

        self.worker._check_merge_requests(self.state.get_run(9240))

        self.gitlab.assert_not_called()
        self.worker._repair_merge_request.assert_not_called()
        self.agent.assert_not_called()
        self.final_note.assert_not_called()
        self.resolve.assert_not_called()

    def test_zentao_read_failure_preserves_mr_without_inspection_or_spending_budget(self):
        self.seed_mr_delivery()
        before = self.state.get_run(9240)
        del self.worker._still_actionable_before_writeback
        self.worker._claim_agent_budget = Mock()
        self.worker._claim_agent_run = Mock()
        self.add_note.reset_mock()

        with patch('zentao_auto_fixer.worker.bug_is_still_actionable',
                   side_effect=ZenTaoPollError('禅道暂时无法读取')):
            self.worker._check_merge_requests(before)

        after = self.state.get_run(9240)
        self.assertEqual(after.status, before.status)
        self.assertEqual(after.writeback_payload, before.writeback_payload)
        self.assertEqual(after.commit_hash, before.commit_hash)
        self.assertIn('mr_zentao_unavailable',
                      [event['event'] for event in self.state.list_run_events(9240)])
        self.gitlab.assert_not_called()
        self.worker._claim_agent_budget.assert_not_called()
        self.worker._claim_agent_run.assert_not_called()
        self.agent.assert_not_called()
        self.reviewer.assert_not_called()
        self.add_note.assert_not_called()

    def test_all_merged_mrs_are_listed_in_the_final_success_note(self):
        self.seed_mr_delivery()
        self.client.inspect.side_effect = [
            ({'state': 'merged', 'merge_commit_sha': 'merge-app',
              'head_pipeline': {'sha': self.requests[0]['sha'], 'status': 'success'}}, self.jobs()),
            ({'state': 'merged', 'squash_commit_sha': 'merge-api',
              'head_pipeline': {'sha': self.requests[1]['sha'], 'status': 'success'}}, self.jobs()),
        ]

        self.worker._check_merge_requests(self.state.get_run(9240))

        self.final_note.assert_called_once()
        solution = self.final_note.call_args.kwargs['solution']
        for item in self.requests:
            self.assertIn(item['url'], solution)
        self.assertEqual(self.state.get_run(9240).status, 'pushed')
        self.assertNotIn('delivery_status', self.payload())
        self.resolve.assert_not_called()

    def test_a_merged_mr_without_an_actual_merge_commit_cannot_complete(self):
        self.seed_mr_delivery()
        self.client.inspect.return_value = (
            {'state': 'merged', 'head_pipeline': {'status': 'success'}}, self.jobs(),
        )

        self.worker._check_merge_requests(self.state.get_run(9240))

        self.assertEqual(self.state.get_run(9240).status, 'merge_request_failed')
        self.assertIn('actual merge commit', self.state.get_run(9240).error)
        self.final_note.assert_not_called()
        self.resolve.assert_not_called()

    def test_one_merged_mr_does_not_complete_another_pending_mr(self):
        self.seed_mr_delivery()
        self.client.inspect.side_effect = [
            ({'state': 'merged', 'merge_commit_sha': 'merged',
              'head_pipeline': {'status': 'success'}}, self.jobs()),
            (self.opened_mr(self.requests[1]), self.jobs('pending')),
        ]

        self.worker._check_merge_requests(self.state.get_run(9240))

        self.assertEqual(self.state.get_run(9240).status, 'awaiting_merge')
        self.final_note.assert_not_called()
        self.resolve.assert_not_called()

    def test_cable_conflict_uses_the_same_bounded_mr_repair_callback(self):
        cable = dict(self.requests[1], target='pre_release',
                     url='https://gitlab.example/im/cable/-/merge_requests/42')
        self.seed_mr_delivery([cable])
        self.worker._repair_merge_request = Mock()
        self.client.inspect.return_value = (
            self.opened_mr(cable, status='success', has_conflicts=True), self.jobs(),
        )

        self.worker._check_merge_requests(self.state.get_run(9240))

        repair = self.worker._repair_merge_request
        repair.assert_called_once()
        self.assertTrue(repair.call_args.kwargs['merge_target'])
        self.assertEqual(repair.call_args.args[2], cable)
        self.client.verified_pipeline.assert_not_called()
        self.create_mr.assert_not_called()
        self.resolve.assert_not_called()

    def test_cable_budget_pause_preserves_its_reason_and_latest_commit_summary(self):
        cable = dict(self.requests[1], target='pre_release',
                     url='https://gitlab.example/im/cable/-/merge_requests/42')
        self.seed_mr_delivery([cable])
        self.add_note.reset_mock()
        before = self.state.get_run(9240)
        pause_reason = '当天 AI 调用额度已用完，暂停续修。'
        latest_sha = 'c' * 40
        latest_summary = 'backend:' + latest_sha

        def pause_repair(run, payload, item, failure):
            item['sha'] = latest_sha
            payload['commit_summary'] = latest_summary
            payload['ci_pause_reason'] = pause_reason
            self.state.set_writeback_payload(run.bug_id, json.dumps(payload, ensure_ascii=False))
            self.state.update_status(run.bug_id, 'awaiting_merge', error=pause_reason,
                                     commit_hash=latest_summary)

        self.worker._repair_merge_request = Mock(side_effect=pause_repair)
        self.client.inspect.return_value = (
            self.opened_mr(cable, status='failed'),
            [{'name': name, 'status': 'failed', 'allow_failure': False}
             for name in ('sql-static-gate', 'hk-seed-environment-gate')],
        )

        self.worker._check_merge_requests(before)

        self.worker._repair_merge_request.assert_called_once()
        after = self.state.get_run(9240)
        self.assertEqual(after.status, 'awaiting_merge')
        self.assertEqual(after.error, pause_reason)
        self.assertNotEqual(before.commit_hash, latest_summary)
        self.assertEqual(after.commit_hash, latest_summary)
        self.assertEqual(self.payload()['commit_summary'], latest_summary)
        self.assertEqual(self.payload()['ci_pause_reason'], pause_reason)
        self.assertEqual(self.payload()['ci_attempts'], 0)
        self.add_note.assert_called_once()
        text = self.add_note.call_args.args[2]
        self.assertIn(pause_reason, text)
        self.assertIn(latest_sha[:12], text)
        self.assertNotIn('已进入原 MR 续修流程', text)
        self.agent.assert_not_called()
        self.reviewer.assert_not_called()
        self.final_note.assert_not_called()
        self.resolve.assert_not_called()

    def test_cable_required_rebase_invokes_conflict_callback_before_ci_wait(self):
        item = dict(self.requests[1], target='pre_release',
                    url='https://gitlab.example/im/cable/-/merge_requests/42')
        self.client.inspect.return_value = (
            self.opened_mr(item, status='success', detailed_merge_status='need_rebase'), self.jobs(),
        )
        repair_conflict = Mock()
        repair_failed = Mock()

        complete, _stage = check_cable_release(
            self.client, item, {}, self.root, 9240,
            repair_failed=repair_failed, repair_conflict=repair_conflict,
        )

        self.assertFalse(complete)
        repair_conflict.assert_called_once_with()
        repair_failed.assert_not_called()
        self.client.verified_pipeline.assert_not_called()
        self.agent.assert_not_called()


if __name__ == '__main__':
    unittest.main()
