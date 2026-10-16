import json
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from tests.test_platform_skip import _load
from zentao_auto_fixer.worker import Worker
from zentao_auto_fixer.zentao import ZenTaoResolveError, ZenTaoWriteError


class WritebackTests(unittest.TestCase):
    def setUp(self):
        self.project = _load({})
        self.state = Mock()
        self.settings = SimpleNamespace(zentao_client_script=Path('/helper'),
                                        load_projects=Mock(return_value=[self.project]))
        self.worker = Worker(self.settings, self.state)
        self.worker._still_actionable_before_writeback = Mock(return_value=True)
        self.payload = {'cause': '原因', 'solution': '修复', 'commit_summary': 'app:abc'}
        self.run = SimpleNamespace(bug_id=42, project_name=self.project.name, commit_hash='app:abc',
                                   writeback_payload=json.dumps(self.payload))

    def test_writeback_and_retry_respect_current_project_setting(self):
        for enabled in (True, False):
            for retry in (False, True):
                with self.subTest(enabled=enabled, retry=retry):
                    self.state.reset_mock()
                    self.settings.load_projects.return_value = [_load({'resolveBugAfterFix': enabled})]
                    with patch('zentao_auto_fixer.worker.comment_bug') as comment, \
                         patch('zentao_auto_fixer.worker.resolve_bug') as resolve:
                        if retry:
                            self.worker._retry_writeback(self.run)
                        else:
                            self.worker._writeback_one(self.run, self.payload)
                    comment.assert_called_once_with(Path('/helper'), 42, cause='原因', solution='修复')
                    if enabled:
                        resolve.assert_called_once_with(Path('/helper'), 42)
                        self.state.mark_seen_resolved_once.assert_called_once_with(42, 'resolved')
                    else:
                        resolve.assert_not_called()
                        self.state.mark_seen_resolved_once.assert_not_called()
                        self.assertNotIn('resolve_done', [call.args[1] for call in self.state.record_run_event.call_args_list])
                    self.state.update_status.assert_called_once_with(
                        42, 'pushed', error='', commit_hash='app:abc', handled_once=True, completed=True)
                    self.state.record_run_event.assert_any_call(42, 'writeback_done', 'app:abc')

    def test_missing_project_prevents_comment_and_resolution(self):
        self.settings.load_projects.return_value = []
        with patch('zentao_auto_fixer.worker.comment_bug') as comment, \
             patch('zentao_auto_fixer.worker.resolve_bug') as resolve:
            self.worker._writeback_one(self.run, self.payload)
        comment.assert_not_called()
        resolve.assert_not_called()
        self.assertEqual(self.state.update_status.call_args.args[1], 'writeback_failed')

    def test_unreadable_project_config_does_not_default_to_resolving(self):
        self.settings.load_projects.side_effect = ValueError('invalid project config')
        with patch('zentao_auto_fixer.worker.comment_bug') as comment, \
             patch('zentao_auto_fixer.worker.resolve_bug') as resolve, \
             self.assertLogs('zentao_auto_fixer.worker', level='ERROR'):
            self.worker._writeback_one(self.run, self.payload)
        comment.assert_not_called()
        resolve.assert_not_called()
        self.assertEqual(self.state.update_status.call_args.args[1], 'writeback_failed')

    def test_comment_or_resolution_failure_never_records_completion(self):
        for target, error in (('comment_bug', ZenTaoWriteError('comment failed')),
                              ('resolve_bug', ZenTaoResolveError('resolve failed'))):
            with self.subTest(target=target):
                self.state.reset_mock()
                with patch('zentao_auto_fixer.worker.comment_bug'), \
                     patch('zentao_auto_fixer.worker.resolve_bug') as resolve, \
                     patch('zentao_auto_fixer.worker.' + target, side_effect=error), \
                     self.assertLogs('zentao_auto_fixer.worker', level='ERROR'):
                    self.worker._writeback_one(self.run, self.payload)
                if target == 'comment_bug':
                    resolve.assert_not_called()
                self.state.mark_seen_resolved_once.assert_not_called()
                self.assertEqual(self.state.update_status.call_args.args[1], 'writeback_failed')
                self.assertNotIn('writeback_done', [call.args[1] for call in self.state.record_run_event.call_args_list])
