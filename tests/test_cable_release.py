import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from zentao_auto_fixer.cable_release import check_cable_release, is_cable_release
from zentao_auto_fixer.gitlab import GitLab, GitLabError
from zentao_auto_fixer.worker import Worker


class CableReleaseTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.item = {'sha': 'fix', 'target': 'pre_release',
                     'url': 'https://gitlab.example/im/cable/-/merge_requests/1'}
        self.payload = {'verdict': {'verification_passed': True, 'verification_method': 'test',
                                   'verification_command': 'go test ./internal/utils'},
                        'cause': '原因', 'solution': '修复', 'delivery_status': 'awaiting_merge',
                        'merge_requests': [self.item], 'commit_summary': 'backend:fix'}
        self.client = Mock()
        self.feature = {'state': 'merged', 'merge_commit_sha': 'merged-fix',
                        'head_pipeline': {'id': 1, 'sha': 'fix', 'status': 'success'}}
        self.client.inspect.return_value = (self.feature, [])
        self.client.verified_pipeline.return_value = True

    def check(self, repair=None):
        return check_cable_release(self.client, self.item, self.payload, self.root, 1, repair)

    def test_merged_fix_completes_without_production_evidence(self):
        self.assertTrue(self.check()[0])
        self.client.inspect.assert_called_once_with('fix', 'pre_release')
        self.client.verified_pipeline.assert_called_once_with(
            self.feature['head_pipeline'], 'fix', {'sql-static-gate', 'hk-seed-environment-gate'})
        self.client.request.assert_not_called()
        self.client.enable_auto_merge.assert_not_called()

    def test_unmerged_fix_enables_auto_merge_but_does_not_complete(self):
        self.feature['state'] = 'opened'
        self.assertFalse(self.check()[0])
        self.client.enable_auto_merge.assert_called_once_with('fix')

    def test_ci_tests_changed_head_closed_mr_or_missing_merge_cannot_complete(self):
        self.client.verified_pipeline.return_value = False
        self.assertFalse(self.check()[0])
        self.client.verified_pipeline.return_value = True
        for key, value in [('verification_passed', False), ('verification_method', 'review'),
                           ('verification_command', '')]:
            old = self.payload['verdict'][key]
            self.payload['verdict'][key] = value
            self.assertFalse(self.check()[0])
            self.payload['verdict'][key] = old
        self.feature['merge_commit_sha'] = None
        with self.assertRaises(GitLabError):
            self.check()
        self.feature['state'] = 'closed'
        with self.assertRaises(GitLabError):
            self.check()
        self.client.inspect.side_effect = GitLabError('head changed')
        with self.assertRaises(GitLabError):
            self.check()

    def test_failed_feature_ci_still_repairs_original_mr(self):
        self.feature['state'] = 'opened'
        self.feature['head_pipeline']['status'] = 'failed'
        self.client.inspect.return_value = (self.feature, [
            {'name': 'sql-static-gate'}, {'name': 'hk-seed-environment-gate'}])
        repair = Mock()
        self.assertFalse(self.check(repair)[0])
        repair.assert_called_once_with(self.client.failed_logs.return_value)

    def test_old_release_wait_runs_normal_comment_and_resolve(self):
        state = Mock()
        worker = Worker(SimpleNamespace(data_dir=self.root, zentao_client_script=Path('/helper')), state)
        worker._project_for = Mock(return_value=SimpleNamespace(enabled=True))
        worker._record_progress = Mock()
        run = SimpleNamespace(bug_id=1, status='awaiting_release', error='等待 PRE 验收',
                              writeback_payload=json.dumps(self.payload), commit_hash='backend:fix')
        state.get_run.return_value = run
        with patch('zentao_auto_fixer.worker.GitLab', return_value=self.client), \
             patch('zentao_auto_fixer.worker.comment_bug') as comment, \
             patch('zentao_auto_fixer.worker.resolve_bug') as resolve:
            worker._process_bug(1)
            comment.assert_called_once()
            resolve.assert_called_once_with(Path('/helper'), 1)
            self.assertEqual(state.update_status.call_args.args[1], 'pushed')
            solution = comment.call_args.kwargs['solution']
            self.assertIn('已合入 pre_release', solution)
            self.assertNotIn('已完成 PRE 验收', solution)
            self.feature['state'] = 'opened'
            comment.reset_mock()
            resolve.reset_mock()
            worker._process_bug(1)
            comment.assert_not_called()
            resolve.assert_not_called()
            self.assertEqual(state.update_status.call_args.args[1], 'awaiting_merge')

    def test_circllo_dev_does_not_use_cable_gate(self):
        self.assertTrue(is_cable_release(self.item))
        self.assertFalse(is_cable_release(dict(self.item, target='dev')))
        self.assertFalse(is_cable_release(dict(
            self.item, url='https://gitlab.example/circll/circllo/-/merge_requests/1', target='dev')))

    def test_pipeline_checks_include_paginated_bridges_and_fail_closed(self):
        client = object.__new__(GitLab)
        client.project = '/projects/1'
        client.request = Mock(side_effect=[
            [{'id': i, 'name': f'job{i}', 'status': 'success'} for i in range(100)], [],
            [{'id': 101, 'name': 'prod-sql-gate', 'status': 'success',
              'downstream_pipeline': {'id': 9, 'status': 'failed'}}],
        ])
        pipeline = {'id': 8, 'sha': 'pre', 'status': 'success'}
        self.assertFalse(client.verified_pipeline(pipeline, 'pre', {'prod-sql-gate'}))
        self.assertIn('/bridges?', client.request.call_args.args[0])
        client.pipeline_checks = Mock(return_value=[{'id': 1, 'name': 'gate', 'status': 'success', 'allow_failure': False}])
        self.assertTrue(client.verified_pipeline(pipeline, 'pre', {'gate'}))
        for checks in ([], [{'id': 1, 'name': 'gate', 'status': 'success', 'allow_failure': True}],
                       [{'id': 1, 'name': 'gate', 'status': 'skipped'}]):
            client.pipeline_checks.return_value = checks
            self.assertFalse(client.verified_pipeline(pipeline, 'pre', {'gate'}))
        self.assertFalse(client.verified_pipeline(pipeline, 'other-sha', {'gate'}))


if __name__ == '__main__':
    unittest.main()
