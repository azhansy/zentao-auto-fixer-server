import json
from datetime import date
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

from zentao_auto_fixer.daily_report import collect, deliver, render, send


class DailyReportTests(unittest.TestCase):
    def test_calendar_dedupe_final_result_and_delivery(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            db = root / 'state.sqlite3'
            with sqlite3.connect(db) as conn:
                conn.executescript('CREATE TABLE bug_runs(bug_id, project_name); CREATE TABLE run_events(id INTEGER PRIMARY KEY, bug_id, event, created_at);')
                conn.executemany('INSERT INTO bug_runs VALUES (?,?)', [(1,'Loopin'), (2,'Cable'), (3,'Loopin')])
                conn.executemany('INSERT INTO run_events(bug_id,event,created_at) VALUES (?,?,?)', [
                    (1,'started','2026-09-07T16:00:00+00:00'),
                    (1,'failed','2026-09-08T01:00:00+00:00'),
                    (1,'resolve_done','2026-09-08T15:59:59+00:00'),
                    (1,'failed','2026-09-08T16:00:00+00:00'),
                    (2,'awaiting_release','2026-09-08T02:00:00+00:00'),
                    (2,'cleanup_worktree','2026-09-08T03:00:00+00:00'),
                    (3,'failed','2026-09-08T04:00:00+00:00'),
                ])
            day = date(2026,9,8)
            grouped = collect(db,day)
            self.assertEqual(grouped, {'Loopin': {'成功':1,'失败':1}, 'Cable': {'待上线':1}})
            self.assertIn('合计：处理 3 项；成功 1，失败 1，待上线 1', render(day,grouped))
            with patch('zentao_auto_fixer.daily_report.send', return_value={'code':0}) as post:
                deliver(db,root/'receipts',day,'unused')
                deliver(db,root/'receipts',day,'unused')
                deliver(db,root/'receipts',date(2026,9,6),'unused')
                self.assertEqual(post.call_count,1)
            with patch('zentao_auto_fixer.daily_report.send', side_effect=RuntimeError('rejected')):
                with self.assertRaises(RuntimeError):
                    deliver(db,root/'receipts',date(2026,9,9),'unused')
            self.assertFalse((root/'receipts/2026-09-09.json').exists())

    def test_http_200_business_failure_is_not_success(self):
        import io
        with patch('urllib.request.urlopen', return_value=io.StringIO(json.dumps({'code':19001}))):
            with self.assertRaises(RuntimeError):
                send('https://open.feishu.cn/example','report')


if __name__ == '__main__':
    unittest.main()
