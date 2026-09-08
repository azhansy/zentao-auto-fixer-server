import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from tests.test_git_ops import _clone_pair, _git, _git_output
from tests.test_platform_skip import _load
from zentao_auto_fixer.git_ops import GitError, head_commit, push_merge_request
from zentao_auto_fixer.models import TERMINAL_STATUSES
from zentao_auto_fixer.worker import Worker, _Checkout, _looks_like_non_fast_forward


class MergeRequestTests(unittest.TestCase):
    def test_config_defaults_and_rejects_unknown_modes(self):
        self.assertEqual(_load({}).delivery_mode, 'push')
        project = _load({'backend': {'repoUrl': 'git@host:im/cable.git',
                                    'targetBranch': 'pre_release', 'deliveryMode': 'merge_request'}})
        self.assertEqual(project.backend_delivery_mode, 'merge_request')
        for value in ('pr', '', None, False, []):
            with self.assertRaises(ValueError):
                _load({'deliveryMode': value})

    def test_real_git_push_uses_source_branch_and_requires_actual_mr_url(self):
        with tempfile.TemporaryDirectory() as tmp:
            remote, seed, worker = _clone_pair(Path(tmp))
            baseline = head_commit(seed)
            _git(remote, 'config', 'receive.advertisePushOptions', 'true')
            _git(remote, 'config', 'core.hooksPath', str(remote / 'hooks'))
            hook = remote / 'hooks/post-receive'
            hook.write_text('#!/bin/sh\nenv | sort > push-options.txt\necho "https://gitlab.example/im/cable/-/merge_requests/42" >&2\n')
            hook.chmod(0o755)
            (worker / 'fix.txt').write_text('fix\n')
            _git(worker, 'add', '-A'); _git(worker, 'commit', '-qm', 'fix')
            url = push_merge_request(worker, 'zentao-fix/1', 'dev', 'fix: 修复文件检查\n正文')
            self.assertTrue(url.endswith('/42'))
            self.assertEqual(_git_output(remote, 'rev-parse', 'dev'), baseline)
            self.assertEqual(_git_output(remote, 'rev-parse', 'zentao-fix/1'), head_commit(worker))
            options = (remote / 'push-options.txt').read_text()
            for value in ('merge_request.create', 'merge_request.target=dev', 'merge_request.title=fix: 修复文件检查'):
                self.assertIn(value, options)
            self.assertNotIn('auto_merge', options)
            # An unchanged push returns no server-side creation receipt.
            with self.assertRaises(GitError):
                push_merge_request(worker, 'zentao-fix/1', 'dev', 'fix: 检查')
            hook.write_text('#!/bin/sh\necho "https://gitlab.example/im/cable/-/merge_requests/new?source=x" >&2\n')
            with self.assertRaises(GitError):
                push_merge_request(worker, 'zentao-fix/2', 'dev', 'fix: 检查')

    def test_mixed_delivery_and_writeback_retry_never_resolve_before_merge(self):
        worker = Worker(SimpleNamespace(worker_count=3, zentao_client_script=Path('/helper')), Mock())
        worker._rebase_changed_checkouts = Mock(return_value=True)
        worker._record_progress = Mock()
        app = _Checkout('app', 'app', Path('/cache'), Path('/app'), 'dev', 'base')
        backend = _Checkout('backend', 'api', Path('/cache'), Path('/api'), 'pre_release', 'base', 'merge_request')
        run = SimpleNamespace(bug_id=7578, title='测试', commit_hash='')
        url = 'https://gitlab.example/im/cable/-/merge_requests/42'
        with patch('zentao_auto_fixer.worker.has_changes', return_value=False), \
             patch('zentao_auto_fixer.worker.head_commit', return_value='abcdef1234567890'), \
             patch('zentao_auto_fixer.worker.changed_files', return_value=[]), \
             patch('zentao_auto_fixer.worker.push_head_dry_run') as dry, \
             patch('zentao_auto_fixer.worker.push_head_to_branch') as push, \
             patch('zentao_auto_fixer.worker.push_merge_request', return_value=url) as mr, \
             patch('zentao_auto_fixer.worker.comment_bug'), \
             patch('zentao_auto_fixer.worker.resolve_bug') as resolve:
            worker._commit_push_and_resolve([run], {'app': app, 'backend': backend},
                                           {7578: {'solution': '修复文件检查'}}, '#7578', 'claude', False)
            dry.assert_called_once_with(Path('/app'), 'dev')
            push.assert_called_once_with(Path('/app'), 'dev')
            self.assertEqual(mr.call_args.args[2], 'pre_release')
            self.assertEqual(worker.state.update_status.call_args.args[1], 'awaiting_merge')
            payload = worker.state.set_writeback_payload.call_args.args[1]
            self.assertIn(url, payload)
            run.writeback_payload = payload
            worker._retry_writeback(run)
            resolve.assert_not_called()
            # Default delivery retains the existing resolve behavior.
            worker._writeback_one(run, {'cause': '原因', 'solution': '修复', 'commit_summary': 'abc'})
            resolve.assert_called_once()
        self.assertIn('awaiting_merge', TERMINAL_STATUSES)
        self.assertIn('merge_request_failed', TERMINAL_STATUSES)

    def test_permission_denial_is_not_a_rebase_retry(self):
        self.assertFalse(_looks_like_non_fast_forward('remote: You are not allowed to push code to protected branches\n[remote rejected] (pre-receive hook declined)'))
        self.assertTrue(_looks_like_non_fast_forward('[rejected] dev -> dev (fetch first)'))
        self.assertTrue(_looks_like_non_fast_forward('non-fast-forward'))

class AutoMergeLifecycleTests(unittest.TestCase):
    def setUp(self):
        from zentao_auto_fixer.gitlab import required_jobs_pass
        self.jobs = [{'id': i, 'name': name, 'status': 'success', 'allow_failure': False}
                     for i, name in enumerate(('lint', 'unit-test', 'build', 'integration'), 1)]
        self.assertTrue(required_jobs_pass(self.jobs))
        self.item = {'sha': 'abc', 'target': 'pre_release', 'source': 'feature/zentao-1',
                     'repo_url': 'git@host:im/cable.git', 'url': 'https://gitlab.example/im/cable/-/merge_requests/1'}
        self.payload = {'merge_requests': [self.item], 'delivery_status': 'awaiting_merge',
                        'cause': 'cause', 'solution': 'solution', 'commit_summary': 'backend:abc'}
        self.run = SimpleNamespace(bug_id=1, commit_hash='backend:abc', writeback_payload=json.dumps(self.payload))
        self.worker = Worker(SimpleNamespace(worker_count=3, max_bug_retries=2), Mock())
        self.worker._writeback_one = Mock()
        self.worker._record_progress = Mock()

    def test_only_complete_required_jobs_on_merged_head_can_resolve(self):
        with patch('zentao_auto_fixer.worker.GitLab') as cls, \
             patch('zentao_auto_fixer.worker.remote_branch_exists_for_url', return_value=False):
            cls.return_value.inspect.return_value = ({'state': 'merged', 'head_pipeline': {'status': 'success'}}, self.jobs)
            self.worker._check_merge_requests(self.run)
            payload = self.worker._writeback_one.call_args.args[1]
            self.assertNotIn('delivery_status', payload)
            self.worker._writeback_one.reset_mock()
            for jobs in ([], self.jobs[:-1], [dict(j, allow_failure=True) for j in self.jobs],
                         [dict(j, status='skipped') for j in self.jobs]):
                cls.return_value.inspect.return_value = ({'state': 'merged', 'head_pipeline': {'status': 'success'}}, jobs)
                self.worker._check_merge_requests(self.run)
                self.worker._writeback_one.assert_not_called()
                self.assertEqual(self.worker.state.update_status.call_args.args[1], 'merge_request_failed')

    def test_pending_ci_enables_auto_merge_but_missing_jobs_does_not(self):
        with patch('zentao_auto_fixer.worker.GitLab') as cls:
            mr = {'state': 'opened', 'head_pipeline': {'sha': 'abc', 'status': 'running'}}
            cls.return_value.inspect.return_value = (mr, [dict(j, status='pending') for j in self.jobs])
            self.worker._check_merge_requests(self.run)
            cls.return_value.enable_auto_merge.assert_called_once_with('abc')
            self.worker._writeback_one.assert_not_called()
            cls.return_value.enable_auto_merge.reset_mock()
            cls.return_value.inspect.return_value = (mr, self.jobs[:-1])
            self.worker._check_merge_requests(self.run)
            cls.return_value.enable_auto_merge.assert_not_called()

    def test_failed_ci_repairs_same_mr_and_stops_at_budget(self):
        with patch('zentao_auto_fixer.worker.GitLab') as cls, \
             patch.object(self.worker, '_repair_merge_request') as repair:
            cls.return_value.inspect.return_value = (
                {'state': 'opened', 'head_pipeline': {'sha': 'abc', 'status': 'failed'}},
                [dict(j, status='failed' if j['name'] == 'lint' else 'skipped') for j in self.jobs])
            cls.return_value.failed_logs.return_value = 'lint failed'
            self.worker._check_merge_requests(self.run)
            self.assertEqual(repair.call_args.args[2]['source'], 'feature/zentao-1')
            cls.return_value.enable_auto_merge.assert_not_called()
        from zentao_auto_fixer.gitlab import GitLabError
        with self.assertRaises(GitLabError):
            self.worker._repair_merge_request(self.run, dict(self.payload, ci_attempts=2), self.item, '')

    def test_direct_cable_pre_release_push_is_rejected_before_network_push(self):
        from zentao_auto_fixer.git_ops import push_head_to_branch, push_head_dry_run
        with patch('zentao_auto_fixer.git_ops.run_git', return_value='git@ssh.into.wang:im/cable.git') as git:
            for push in (push_head_to_branch, push_head_dry_run):
                with self.assertRaises(GitError):
                    push(Path('/repo'), 'pre_release')
            self.assertTrue(all(call.args[0] == ['remote', 'get-url', 'origin'] for call in git.call_args_list))

    def test_mr_failure_is_unhealthy_and_counted(self):
        from zentao_auto_fixer.state import StateStore
        from tests.test_state import _bug, _project
        with tempfile.TemporaryDirectory() as tmp:
            state = StateStore(Path(tmp) / 'state.sqlite3')
            state.enqueue_first_run(_bug(1), _project())
            state.update_status(1, 'merge_request_failed', error='CI missing', completed=True)
            self.assertEqual(state.current_problem_count(), 1)
            self.assertEqual(state.run_summary_since('2000-01-01')['failed'], 1)

    def test_api_pins_head_target_host_and_auto_merge_options(self):
        import os
        from zentao_auto_fixer.gitlab import GitLab, GitLabError
        with patch.dict(os.environ, {'AUTO_FIXER_GITLAB_URL': 'https://gitlab.example',
                                    'AUTO_FIXER_GITLAB_TOKEN': 'private-test-token',
                                    'AUTO_FIXER_GITLAB_TOKEN_FILE': ''}):
            with self.assertRaises(GitLabError):
                GitLab('https://other.example/im/cable/-/merge_requests/1')
            client = GitLab(self.item['url'])
            client.request = Mock(return_value={'sha': 'someone-else', 'target_branch': 'pre_release'})
            with self.assertRaises(GitLabError):
                client.inspect('abc', 'pre_release')
            client.request.return_value = {'only_allow_merge_if_pipeline_succeeds': True}
            client.enable_auto_merge('abc')
            self.assertEqual(client.request.call_args.args[2],
                             {'sha': 'abc', 'auto_merge': True, 'should_remove_source_branch': True})
            client.request.return_value = {'only_allow_merge_if_pipeline_succeeds': False}
            with self.assertRaises(GitLabError):
                client.enable_auto_merge('abc')
