import json
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from zentao_auto_fixer.agent_runner import _runtime_ai_info, run_agent_batch_fix
from zentao_auto_fixer.poller import Poller
from zentao_auto_fixer.worker import Worker, _solution_text


class DispatchAndMetadataTests(unittest.TestCase):
    def test_all_projects_are_collected_before_lowest_id_is_dispatched(self):
        state = mock.Mock()
        state.queued_bug_ids.return_value = []
        settings = SimpleNamespace(worker_count=3, load_projects=lambda: [
            SimpleNamespace(enabled=True, ids=[40, 20]),
            SimpleNamespace(enabled=True, ids=[30, 10]),
        ])
        worker = Worker(settings, state)
        poller = Poller(settings, state, worker)
        seen = []
        dispatched = []
        take = worker.queue.get_nowait

        def record_dispatch():
            bug_id = take()
            dispatched.append(bug_id)
            return bug_id

        worker.queue.get_nowait = record_dispatch
        finished = threading.Event()
        guard = threading.Lock()

        def process(bug_id):
            with guard:
                seen.append(bug_id)
                if len(seen) == 4:
                    finished.set()

        def collect(project):
            self.assertTrue(worker.dispatch_lock.locked())
            for bug_id in project.ids:
                worker.enqueue(bug_id)
            self.assertEqual(seen, [])

        worker._process_bug = process
        poller._poll_project = collect
        # Start the threads during collection: no partial project may start early.
        with worker.dispatch_lock:
            worker.start()
        try:
            poller.poll_once()
            self.assertTrue(finished.wait(3))
            self.assertEqual(len(worker._threads), 3)
            self.assertEqual(dispatched, [10, 20, 30, 40])
            self.assertEqual(sorted(seen), [10, 20, 30, 40])
        finally:
            worker.stop()

    def test_queue_picks_lowest_id_and_deduplicates_new_arrivals(self):
        worker = Worker(SimpleNamespace(worker_count=3), mock.Mock())
        for bug_id in [30, 20, 10, 20]:
            worker.enqueue(bug_id)
        self.assertEqual(worker.queue.get_nowait(), 10)
        worker.enqueue(5)
        self.assertEqual([worker.queue.get_nowait() for _ in range(3)], [5, 20, 30])

    def test_restart_order_ignores_first_seen_time_and_does_not_batch_larger_ids(self):
        import tempfile
        from tests.test_state import _bug, _project
        from zentao_auto_fixer.state import StateStore
        with tempfile.TemporaryDirectory() as tmp:
            state = StateStore(Path(tmp) / 'state.sqlite3')
            state.enqueue_first_run(_bug(30), _project())
            state.enqueue_first_run(_bug(10), _project())
            self.assertEqual(state.queued_bug_ids(), [10, 30])
        state = mock.Mock()
        state.claim_queued_batch.return_value = []
        worker = Worker(SimpleNamespace(worker_count=3), state)
        worker._process_batch(10, SimpleNamespace(max_bugs_per_poll=99))
        state.claim_queued_batch.assert_called_once_with(10, limit=1)

    def test_failed_job_does_not_stop_following_job(self):
        state = mock.Mock()
        state.queued_bug_ids.return_value = [20, 10]
        state.get_run.return_value = None
        worker = Worker(SimpleNamespace(worker_count=1), state)
        done = threading.Event()
        seen = []

        def process(bug_id):
            seen.append(bug_id)
            if bug_id == 10:
                raise RuntimeError('controlled failure')
            done.set()

        worker._process_bug = process
        try:
            with self.assertLogs('zentao_auto_fixer.worker', level='ERROR'):
                worker.start()
                self.assertTrue(done.wait(3))
            self.assertEqual(seen, [10, 20])
        finally:
            worker.stop()

    def test_model_comes_from_runtime_result_and_survives_writeback_retry(self):
        output = 'diagnostic line\n' + json.dumps({
            'type': 'result', 'modelUsage': {'deepseek-v4-pro[1m]': {}}
        })
        info = _runtime_ai_info('claude', output)
        self.assertIn('DeepSeek（deepseek-v4-pro[1m]）', info)
        self.assertIn('Claude Code', info)
        payload = {'cause': 'cause', 'solution': _solution_text({'ai_info': info}, 'app:abc'),
                   'commit_summary': 'app:abc'}
        state = mock.Mock()
        worker = Worker(SimpleNamespace(zentao_client_script=Path('/tmp/helper')), state)
        run = SimpleNamespace(bug_id=1, commit_hash='app:abc', writeback_payload=json.dumps(payload))
        with mock.patch('zentao_auto_fixer.worker.comment_bug') as comment, mock.patch(
            'zentao_auto_fixer.worker.resolve_bug'
        ):
            worker._retry_writeback(run)
        self.assertIn(info, comment.call_args.kwargs['solution'])

    def test_missing_metadata_is_explicit_and_model_written_in_verdict_is_not_trusted(self):
        self.assertIn('未确认', _runtime_ai_info('claude', 'plain text'))
        self.assertIn('未确认', _runtime_ai_info('codex', '{}'))
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            with mock.patch('zentao_auto_fixer.agent_runner._run_agent', return_value='{}'), mock.patch(
                'zentao_auto_fixer.agent_runner.read_triage_result', return_value={1: {'ai_info': 'fake model'}}
            ):
                verdicts = run_agent_batch_fix('claude', 'wrapper', Path('/helper'), Path(tmp), None,
                                               [(1, 'title')], Path(tmp) / 'result.json')
        self.assertIn('未确认', verdicts[1]['ai_info'])
        self.assertNotIn('fake model', verdicts[1]['ai_info'])


if __name__ == '__main__':
    unittest.main()
