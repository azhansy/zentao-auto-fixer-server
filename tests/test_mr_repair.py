import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from tests.test_git_ops import _clone_pair, _git, _git_output
from tests.test_state import _bug, _project
from zentao_auto_fixer.git_ops import head_commit
from zentao_auto_fixer.gitlab import GitLabError
from zentao_auto_fixer.state import StateStore
from zentao_auto_fixer.worker import Worker, _Checkout


class MergeRequestRepairTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.remote, self.seed, self.repo = _clone_pair(self.root)
        self.source = 'feature/zentao-9240'
        _git(self.repo, 'checkout', '-qb', self.source)
        (self.repo / 'base.txt').write_text('source fix\n')
        _git(self.repo, 'add', '-A')
        _git(self.repo, 'commit', '-qm', 'fix')
        _git(self.repo, 'push', '-qu', 'origin', self.source)
        self.source_sha = head_commit(self.repo)
        (self.seed / 'base.txt').write_text('target advance\n')
        (self.seed / '.gitlab-ci.yml').write_text('trusted target gate\n')
        _git(self.seed, 'add', '-A')
        _git(self.seed, 'commit', '-qm', 'target advance')
        _git(self.seed, 'push', '-q', 'origin', 'dev')
        self.target_sha = head_commit(self.seed)
        self.item = {'kind': 'app', 'repo_url': str(self.remote), 'source': self.source,
                     'target': 'dev', 'sha': self.source_sha,
                     'url': 'https://gitlab.example/app/-/merge_requests/41'}
        self.payload = {'merge_requests': [self.item], 'ci_attempts': 0,
                        'commit_summary': 'app:' + self.source_sha, 'delivery_status': 'awaiting_merge'}
        self.state = StateStore(self.root / 'state.sqlite3')
        self.state.enqueue_first_run(_bug(9240), _project())
        self.state.update_status(9240, 'awaiting_merge', commit_hash=self.payload['commit_summary'])
        self.state.set_writeback_payload(9240, json.dumps(self.payload))
        self.run = self.state.get_run(9240)
        self.worker = Worker(SimpleNamespace(
            worker_count=1, data_dir=self.root, logs_dir=self.root / 'logs',
            max_bug_retries=2, codex_timeout_seconds=30, git_timeout_seconds=30,
            zentao_client_script=Path('/unused'), agent_bin=lambda agent: agent,
            git_author_name='Auto Fixer', git_author_email='fixer@example.com'), self.state)
        self.worker._project_for = Mock(return_value=_project())
        self.worker._claim_agent_budget = Mock(return_value=True)
        self.worker._still_actionable_before_writeback = Mock(return_value=True)
        self.worker._review_fix = Mock()
        self.worker._prepare_checkout = Mock(return_value=_Checkout(
            'app', str(self.remote), self.remote, self.repo, self.source, self.source_sha))
        self.client = SimpleNamespace(inspect=Mock(return_value=({'state': 'opened'}, [])))
        for name, value in (
                ('GitLab', Mock(return_value=self.client)), ('add_comment', Mock()),
                ('remove_worktree', Mock()), ('run_agent_batch_fix', Mock(side_effect=self.fix))):
            replacement = patch('zentao_auto_fixer.worker.' + name, value)
            replacement.start()
            self.addCleanup(replacement.stop)
            setattr(self, name, value)

    def fix(self, *args, **kwargs):
        self.assertTrue(kwargs['conflict_context'])
        self.assertEqual(_git_output(self.repo, 'diff', '--name-only', '--diff-filter=U'), 'base.txt')
        (self.repo / 'base.txt').write_text('target advance + source fix\n')
        return {9240: {'decision': 'fixed', 'solution': '修复合并冲突', 'targets': ['app']}}

    def repair(self):
        self.worker._repair_merge_request(self.run, self.payload, self.item, 'merge conflict', merge_target=True)

    def test_conflict_repair_keeps_original_mr_and_both_histories(self):
        self.repair()
        merged = head_commit(self.repo)
        self.assertEqual(_git_output(self.remote, 'rev-parse', self.source), merged)
        self.assertEqual(_git_output(self.remote, 'rev-parse', 'dev'), self.target_sha)
        for ancestor in (self.source_sha, self.target_sha):
            _git(self.remote, 'merge-base', '--is-ancestor', ancestor, merged)
        self.assertEqual((self.repo / '.gitlab-ci.yml').read_text(), 'trusted target gate\n')
        self.worker._review_fix.assert_called_once()
        self.assertEqual(self.item['sha'], merged)
        saved = json.loads(self.state.get_run(9240).writeback_payload)
        self.assertEqual(saved['merge_requests'][0]['url'], self.item['url'])
        self.assertEqual(saved['ci_attempts'], 1)
        self.assertIn(self.item['url'], self.add_comment.call_args.args[2])

    def test_closed_or_merged_mr_is_not_pushed_or_recreated(self):
        self.client.inspect.return_value = ({'state': 'closed'}, [])
        with self.assertRaisesRegex(GitLabError, 'closed or merged'):
            self.repair()
        self.assertEqual(_git_output(self.remote, 'rev-parse', self.source), self.source_sha)
        patches = list((self.root / 'patches').glob('*-merge.patch'))
        self.assertEqual(len(patches), 1)
        replay = self.root / 'replay'
        _git(self.root, 'clone', '-q', '--branch', self.source, str(self.remote), str(replay))
        _git(replay, 'apply', str(patches[0]))
        self.assertEqual((replay / 'base.txt').read_text(), 'target advance + source fix\n')
        self.assertEqual((replay / '.gitlab-ci.yml').read_text(), 'trusted target gate\n')

    def test_budget_pause_spends_no_attempt_and_keeps_reason(self):
        self.worker._claim_agent_budget.return_value = False
        self.worker._agent_budget_block_reason = Mock(return_value='今日修复预算已用完')
        self.repair()
        self.run_agent_batch_fix.assert_not_called()
        saved = json.loads(self.state.get_run(9240).writeback_payload)
        self.assertEqual(saved['ci_attempts'], 0)
        self.assertEqual(saved['ci_pause_reason'], '今日修复预算已用完')
        self.assertEqual(self.state.get_run(9240).error, '今日修复预算已用完')
        self.assertEqual(_git_output(self.remote, 'rev-parse', self.source), self.source_sha)

    def test_human_resolution_after_ai_preserves_patch_without_push(self):
        del self.worker._still_actionable_before_writeback
        with patch('zentao_auto_fixer.worker.bug_is_still_actionable',
                   side_effect=[(True, ''), (False, 'resolved by human')]):
            self.repair()
        self.assertEqual(self.state.get_run(9240).status, 'skipped_stale')
        self.assertEqual(_git_output(self.remote, 'rev-parse', self.source), self.source_sha)
        self.assertTrue(list((self.root / 'patches').glob('*.patch')))
        self.worker._review_fix.assert_not_called()

    def test_new_untracked_pipeline_gate_is_rejected(self):
        def unsafe(*args, **kwargs):
            verdict = self.fix(*args, **kwargs)
            (self.repo / '.gitlab').mkdir()
            (self.repo / '.gitlab/extra.yml').write_text('untrusted gate\n')
            return verdict
        self.run_agent_batch_fix.side_effect = unsafe
        with self.assertRaisesRegex(GitLabError, 'pipeline gates'):
            self.repair()
        self.assertEqual(_git_output(self.remote, 'rev-parse', self.source), self.source_sha)
        self.worker._review_fix.assert_not_called()
