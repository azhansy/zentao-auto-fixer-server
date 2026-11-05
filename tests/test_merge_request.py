import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, call, patch

from tests.test_git_ops import _clone_pair, _git, _git_output
from tests.test_platform_skip import _load
from tests.test_state import _bug, _project
from zentao_auto_fixer.git_ops import GitError, head_commit, push_merge_request
from zentao_auto_fixer.models import TERMINAL_STATUSES
from zentao_auto_fixer.state import StateStore
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
        worker = Worker(SimpleNamespace(worker_count=3, zentao_client_script=Path('/helper'),
                                       git_author_name='Auto Fixer', git_author_email='fixer@example.com'), Mock())
        worker._project_for = Mock(return_value=_load({}))
        worker._rebase_changed_checkouts = Mock(return_value=True)
        worker._record_progress = Mock()
        app = _Checkout('app', 'app', Path('/cache'), Path('/app'), 'dev', 'base')
        backend = _Checkout('backend', 'api', Path('/cache'), Path('/api'), 'pre_release', 'base', 'merge_request')
        run = SimpleNamespace(bug_id=7578, title='测试', commit_hash='')
        url = 'https://gitlab.example/im/cable/-/merge_requests/42'
        with patch('zentao_auto_fixer.worker.has_changes', return_value=False), \
             patch('zentao_auto_fixer.worker.head_commit', return_value='abcdef1234567890'), \
             patch('zentao_auto_fixer.worker.changed_files', return_value=[]), \
             patch('zentao_auto_fixer.worker.commit_all', return_value='abcdef1234567890') as commit, \
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
            self.assertEqual(commit.call_count, 2)
            self.assertTrue(all(call.kwargs['amend'] for call in commit.call_args_list))
            self.assertTrue(all(call.args[1].endswith('通过 <zentao-bug-fixer>自动修复')
                                for call in commit.call_args_list))
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

    def test_circllo_dev_push_completes_and_resolves(self):
        worker = Worker(SimpleNamespace(zentao_client_script=Path('/helper'),
                                       git_author_name='Auto Fixer', git_author_email='fixer@example.com'), Mock())
        worker._project_for = Mock(return_value=_load({}))
        worker._rebase_changed_checkouts = Mock(return_value=True)
        worker._record_progress = Mock()
        backend = _Checkout('backend', 'git@host:circll/circllo.git',
                            Path('/cache'), Path('/backend'), 'dev', 'base')
        run = SimpleNamespace(bug_id=42, title='组局修复', commit_hash='')
        with patch('zentao_auto_fixer.worker.has_changes', return_value=False), \
             patch('zentao_auto_fixer.worker.head_commit', return_value='fix'), \
             patch('zentao_auto_fixer.worker.changed_files', return_value=[]), \
             patch('zentao_auto_fixer.worker.commit_all', return_value='fix'), \
             patch('zentao_auto_fixer.worker.push_head_dry_run'), \
             patch('zentao_auto_fixer.worker.push_head_to_branch') as push, \
             patch('zentao_auto_fixer.worker.push_merge_request') as mr, \
             patch('zentao_auto_fixer.worker.comment_bug') as comment, \
             patch('zentao_auto_fixer.worker.resolve_bug') as resolve:
            worker._commit_push_and_resolve(
                [run], {'backend': backend}, {42: {'solution': '修复组局'}}, '#42', 'claude', False)
            push.assert_called_once_with(Path('/backend'), 'dev')
            mr.assert_not_called()
            comment.assert_called_once()
            resolve.assert_called_once_with(Path('/helper'), 42)
            self.assertEqual(worker.state.update_status.call_args.args[1], 'pushed')

    def test_permission_denial_is_not_a_rebase_retry(self):
        self.assertFalse(_looks_like_non_fast_forward('remote: You are not allowed to push code to protected branches\n[remote rejected] (pre-receive hook declined)'))
        self.assertTrue(_looks_like_non_fast_forward('[rejected] dev -> dev (fetch first)'))
        self.assertTrue(_looks_like_non_fast_forward('non-fast-forward'))

    def test_ci_repair_pushes_footer_with_or_without_agent_commit(self):
        for agent_commits in (False, True):
            with self.subTest(agent_commits=agent_commits), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                remote, _seed, repo = _clone_pair(root)
                baseline = head_commit(repo)
                worker = Worker(SimpleNamespace(
                    max_bug_retries=2, logs_dir=root / 'logs', codex_timeout_seconds=30,
                    zentao_client_script=Path('/helper'), agent_bin=lambda agent: agent,
                    git_author_name='Auto Fixer', git_author_email='fixer@example.com'), Mock())
                worker._claim_agent_budget = Mock(return_value=True)
                worker._still_actionable_before_writeback = Mock(return_value=True)
                worker._project_for = Mock(return_value=_load({}))
                worker._prepare_checkout = Mock(return_value=_Checkout(
                    'app', str(remote), root / 'cache', repo, 'dev', baseline))
                run = SimpleNamespace(bug_id=42, title='修复问题')
                item = {'kind': 'app', 'repo_url': str(remote), 'source': 'dev', 'target': 'dev',
                        'sha': baseline, 'url': 'https://gitlab.example/app/-/merge_requests/1'}
                payload = {'commit_summary': f'app:{baseline}', 'merge_requests': [item]}
                info = 'AI / 模型：DeepSeek（deepseek-flash）；执行器：Claude Code'

                def repair(*args, **kwargs):
                    (repo / 'fix.txt').write_text('verified fix\n')
                    if agent_commits:
                        _git(repo, 'add', '-A')
                        _git(repo, 'commit', '-qm', 'agent repair')
                    return {42: {'decision': 'fixed', 'solution': '修复问题', 'ai_info': info}}

                with patch('zentao_auto_fixer.worker.run_agent_batch_fix', side_effect=repair), \
                     patch('zentao_auto_fixer.worker.GitLab', return_value=SimpleNamespace(
                         inspect=lambda *args: ({'state': 'opened'}, []))), \
                     patch('zentao_auto_fixer.worker.add_comment'), \
                     patch('zentao_auto_fixer.worker.remove_worktree'):
                    worker._repair_merge_request(run, payload, item, 'test failed')

                self.assertEqual(_git_output(remote, 'log', 'dev', '-1', '--format=%B'),
                                 f'fix(42): 修复问题\n\n---------\n{info}\n通过 <zentao-bug-fixer>自动修复')
                self.assertEqual(_git_output(remote, 'rev-parse', 'dev^'), baseline)
                self.assertEqual(_git_output(remote, 'show', 'dev:fix.txt'), 'verified fix')
                self.assertEqual(item['sha'], head_commit(repo))

class MultiRepositoryPushRetryTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.state = StateStore(self.root / 'state.sqlite3')
        self.state.enqueue_first_run(_bug(42), _project())
        self.state.update_status(42, 'running')
        self.worker = Worker(SimpleNamespace(
            worker_count=1, data_dir=self.root, zentao_client_script=Path('/unused'),
            git_author_name='Auto Fixer', git_author_email='fixer@example.com',
        ), self.state)
        self.worker._project_for = Mock(return_value=_project())
        self.worker._still_actionable_before_writeback = Mock(return_value=True)
        self.refresh = self.worker._rebase_changed_checkouts = Mock(return_value=True)
        self.writeback = self.worker._comment_and_resolve = Mock(wraps=self.worker._comment_and_resolve)
        self.app = _Checkout('app', 'app', self.root / 'app-cache', self.root / 'app',
                             'dev', 'app-base')
        self.backend = _Checkout('backend', 'backend', self.root / 'api-cache', self.root / 'api',
                                 'pre_release', 'backend-base')
        self.checkouts = {'app': self.app, 'backend': self.backend}
        self.heads = {self.app.worktree: 'app-fix', self.backend.worktree: 'backend-fix'}
        self.dirty = {path: True for path in self.heads}
        self.patch_text = 'retained backend fix\n'

        def commit(repo, *args, **kwargs):
            self.dirty[repo] = False
            return self.heads[repo]

        replacements = {
            'has_changes': Mock(side_effect=lambda repo: self.dirty[repo]),
            'head_commit': Mock(side_effect=lambda repo: self.heads[repo]),
            'changed_files': Mock(return_value=['fix.py']),
            'commit_all': Mock(side_effect=commit),
            'push_head_dry_run': Mock(),
            'push_head_to_branch': Mock(side_effect=[None, GitError('non-fast-forward'), None]),
            'run_git': Mock(return_value=''),
            'export_patch': Mock(return_value=self.patch_text),
            'comment_bug': Mock(), 'resolve_bug': Mock(), 'run_agent_batch_fix': Mock(),
        }
        for name, mocked in replacements.items():
            replacement = patch('zentao_auto_fixer.worker.' + name, mocked)
            replacement.start()
            self.addCleanup(replacement.stop)
            setattr(self, name, mocked)

    def deliver(self):
        self.worker._commit_push_and_resolve(
            [self.state.get_run(42)], self.checkouts,
            {42: {'solution': '修复多仓库问题', 'targets': ['app', 'backend']}},
            '#42', 'claude', False,
        )

    def assert_partial_delivery_stops(self, reason):
        self.deliver()

        run = self.state.get_run(42)
        self.assertEqual(run.status, 'retry_exhausted')
        self.assertIn('部分仓库 app 已推送', run.error)
        self.assertIn(reason, run.error)
        self.assertEqual(self.push_head_to_branch.call_args_list, [
            call(self.app.worktree, self.app.target_branch),
            call(self.backend.worktree, self.backend.target_branch),
        ])
        self.assertEqual(self.refresh.call_count, 2)
        self.assertEqual(self.refresh.call_args.args[1], [self.backend])
        self.export_patch.assert_called_once_with(self.backend.worktree, self.backend.baseline)
        patches = list((self.root / 'patches').glob('*.patch'))
        self.assertEqual(len(patches), 1)
        self.assertEqual(patches[0].read_text(), self.patch_text)
        self.assertIn(str(patches[0]), run.error)
        self.writeback.assert_not_called()
        self.comment_bug.assert_not_called()
        self.resolve_bug.assert_not_called()
        self.run_agent_batch_fix.assert_not_called()

    def test_partial_delivery_stops_and_saves_patch_when_nff_refresh_returns_false(self):
        self.refresh.side_effect = [True, False]
        self.assert_partial_delivery_stops('Remote refresh could not finish')

    def test_partial_delivery_stops_and_saves_patch_when_nff_refresh_raises(self):
        self.refresh.side_effect = [True, GitError('fetch failed during refresh')]
        self.assert_partial_delivery_stops('fetch failed during refresh')

    def test_refresh_cannot_change_the_head_of_an_already_pushed_repository(self):
        def refresh(*args, **kwargs):
            if self.refresh.call_count == 2:
                self.heads[self.app.worktree] = 'unexpected-app-head'
            return True

        self.refresh.side_effect = refresh
        self.assert_partial_delivery_stops('modified an already pushed repository')

    def test_refresh_cannot_dirty_an_already_pushed_repository(self):
        def refresh(*args, **kwargs):
            if self.refresh.call_count == 2:
                self.dirty[self.app.worktree] = True
            return True

        self.refresh.side_effect = refresh
        self.assert_partial_delivery_stops('modified an already pushed repository')

    def test_successful_nff_refresh_retries_only_the_undelivered_repository(self):
        def refresh(*args, **kwargs):
            if self.refresh.call_count == 2:
                self.heads[self.backend.worktree] = 'backend-refreshed'
            return True

        self.refresh.side_effect = refresh
        self.deliver()

        self.assertEqual(self.push_head_to_branch.call_args_list, [
            call(self.app.worktree, self.app.target_branch),
            call(self.backend.worktree, self.backend.target_branch),
            call(self.backend.worktree, self.backend.target_branch),
        ])
        self.assertEqual(self.refresh.call_count, 2)
        self.assertEqual(self.refresh.call_args.args[1], [self.backend])
        self.writeback.assert_called_once()
        run = self.state.get_run(42)
        self.assertEqual(run.status, 'pushed')
        self.assertEqual(run.commit_hash, 'app:app-fix backend:backend-refreshed')
        self.comment_bug.assert_called_once()
        self.resolve_bug.assert_called_once()
        self.export_patch.assert_not_called()
        self.run_agent_batch_fix.assert_not_called()


class AutoMergeLifecycleTests(unittest.TestCase):
    def setUp(self):
        from zentao_auto_fixer.gitlab import required_jobs_pass
        self.jobs = [{'id': i, 'name': name, 'status': 'success', 'allow_failure': False}
                     for i, name in enumerate(('lint', 'unit-test', 'build', 'integration'), 1)]
        self.assertTrue(required_jobs_pass(self.jobs))
        self.item = {'sha': 'abc', 'target': 'pre_release', 'source': 'feature/zentao-1',
                     'repo_url': 'git@host:im/cable.git', 'url': 'https://gitlab.example/im/service/-/merge_requests/1'}
        self.payload = {'merge_requests': [self.item], 'delivery_status': 'awaiting_merge',
                        'cause': 'cause', 'solution': 'solution', 'commit_summary': 'backend:abc'}
        self.run = SimpleNamespace(bug_id=1, status='awaiting_merge', error='', commit_hash='backend:abc', writeback_payload=json.dumps(self.payload))
        self.worker = Worker(SimpleNamespace(worker_count=3, max_bug_retries=2, git_timeout_seconds=30), Mock())
        self.worker._project_for = Mock(return_value=SimpleNamespace(enabled=True))
        self.worker._still_actionable_before_writeback = Mock(return_value=True)
        self.worker._note_merge_requests = Mock()
        self.worker._writeback_one = Mock()
        self.worker._record_progress = Mock()

    def test_only_complete_required_jobs_on_merged_head_can_resolve(self):
        with patch('zentao_auto_fixer.worker.GitLab') as cls:
            cls.return_value.source_branch_exists.return_value = False
            cls.return_value.inspect.return_value = ({'state': 'merged', 'merge_commit_sha': 'merge',
                                                     'head_pipeline': {'status': 'success'}}, self.jobs)
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

    def test_empty_finished_ci_or_timed_out_missing_ci_fails(self):
        import time
        with patch('zentao_auto_fixer.worker.GitLab') as cls:
            for pipeline in ({'sha': 'abc', 'status': 'success'}, {}):
                self.payload['ci_wait_started'] = time.time() - 60
                self.run.writeback_payload = json.dumps(self.payload)
                cls.return_value.inspect.return_value = ({'state': 'opened', 'head_pipeline': pipeline}, [])
                self.worker._check_merge_requests(self.run)
                self.assertEqual(self.worker.state.update_status.call_args.args[1], 'merge_request_failed')
                cls.return_value.enable_auto_merge.assert_not_called()

    def test_disabled_project_never_reads_gitlab_or_starts_ai(self):
        self.worker._project_for.return_value = SimpleNamespace(enabled=False)
        with patch('zentao_auto_fixer.worker.GitLab') as client, \
             patch('zentao_auto_fixer.worker.run_agent_batch_fix') as agent:
            self.worker._check_merge_requests(self.run)
            client.assert_not_called()
            agent.assert_not_called()
            self.worker.state.update_status.assert_not_called()

    def test_branch_cleanup_distinguishes_missing_from_api_failure(self):
        from urllib.error import HTTPError
        from zentao_auto_fixer.gitlab import GitLab, GitLabError
        client = object.__new__(GitLab)
        client.project = '/projects/im%2Fcable'
        client.request = Mock(return_value={'name': 'feature/one'})
        self.assertTrue(client.source_branch_exists('feature/one'))
        for status in (404, 403):
            error = GitLabError('API failure')
            error.__cause__ = HTTPError('https://gitlab.example', status, 'error', None, None)
            client.request.side_effect = error
            if status == 404:
                self.assertFalse(client.source_branch_exists('feature/one'))
            else:
                with self.assertRaises(GitLabError):
                    client.source_branch_exists('feature/one')
