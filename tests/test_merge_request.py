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
