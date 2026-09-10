import http.client
import json
import threading
import unittest
from http.server import ThreadingHTTPServer
from types import SimpleNamespace
from unittest.mock import patch

from zentao_auto_fixer.server import make_handler
from zentao_auto_fixer.zentao import ZenTaoPollError


def get(app, path):
    return request(app, "GET", path)


def post(app, path):
    return request(app, "POST", path)


def request(app, method, path):
    server = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(app))
    thread = threading.Thread(target=server.serve_forever)
    thread.start()
    try:
        connection = http.client.HTTPConnection(*server.server_address, timeout=2)
        connection.request(method, path)
        response = connection.getresponse()
        return response.status, dict(response.getheaders()), response.read()
    finally:
        server.shutdown()
        server.server_close()
        thread.join()


class HealthTests(unittest.TestCase):
    def test_failed_run_degrades_health(self):
        app = SimpleNamespace(
            started_at="2026-08-28T00:00:00+00:00",
            state=SimpleNamespace(
                current_problem_count=lambda: 2,
                run_summary_since=lambda _since: {"queued": 3, "running": 1},
            ),
        )
        status, _headers, body = get(app, "/health")

        self.assertEqual(status, 503)
        self.assertEqual(body, b'{"ok": false, "problems": 2, "queued": 3, "running": 1}')


class DashboardTests(unittest.TestCase):
    def test_root_serves_dashboard(self):
        status, headers, body = get(SimpleNamespace(), "/")

        self.assertEqual(status, 200)
        self.assertEqual(headers["Content-Type"], "text/html; charset=utf-8")
        self.assertIn("禅道自动修复看板".encode(), body)
        self.assertIn(b"/runs?limit=500", body)
        self.assertIn(b"window.location.assign(detail.url)", body)
        self.assertIn(b"const pageSize = 50", body)
        self.assertIn(b"sortPriority(left) - sortPriority(right)", body)

    def test_runs_honors_bounded_limit(self):
        seen = []
        app = SimpleNamespace(
            state=SimpleNamespace(list_runs=lambda limit: seen.append(limit) or [{"bug_id": 7310}])
        )

        with patch.dict("os.environ", {"ZENTAO_BASE_URL": "https://zentao.example.test/zentao"}):
            status, _headers, body = get(app, "/runs?limit=9999")

        self.assertEqual(status, 200)
        self.assertEqual(
            json.loads(body),
            {
                "runs": [
                    {
                        "bug_id": 7310,
                        "url": "https://zentao.example.test/zentao/bug-view-7310.html",
                    }
                ]
            },
        )
        self.assertEqual(seen, [500])


class ResurrectEndpointTests(unittest.TestCase):
    def test_resurrect_requeues_clears_fuses_and_enqueues(self):
        calls = []

        def record_event(bug_id, event, message):
            calls.append(("event", bug_id, event, message))

        app = SimpleNamespace(
            state=SimpleNamespace(
                get_run=lambda _bug_id: SimpleNamespace(status="unable_to_fix"),
                resurrect_for_retry=lambda _bug_id: True,
                clear_no_progress_fuses=lambda: 2,
                record_run_event=record_event,
            ),
            worker=SimpleNamespace(enqueue=lambda bug_id: calls.append(("enqueue", bug_id))),
        )
        status, _headers, body = post(app, "/runs/7687/resurrect")

        self.assertEqual(status, 200)
        self.assertEqual(
            json.loads(body),
            {"ok": True, "bug_id": 7687, "status": "queued", "cleared_fuses": 2},
        )
        self.assertEqual(
            calls,
            [
                ("event", 7687, "resurrected", "Manually reset from the dashboard; requeued for another repair attempt."),
                ("enqueue", 7687),
            ],
        )

    def test_resurrect_rejects_non_resettable_runs(self):
        app = SimpleNamespace(
            state=SimpleNamespace(
                get_run=lambda _bug_id: SimpleNamespace(status="pushed"),
                resurrect_for_retry=lambda _bug_id: False,
            ),
            worker=SimpleNamespace(),
        )
        status, _headers, body = post(app, "/runs/7693/resurrect")

        self.assertEqual(status, 400)
        self.assertIn("only non-successful runs can be reset", json.loads(body)["error"])

    def test_resurrect_rejects_non_integer_bug_id(self):
        status, _headers, body = post(SimpleNamespace(), "/runs/abc/resurrect")

        self.assertEqual(status, 400)
        self.assertEqual(json.loads(body), {"error": "bug_id must be an integer"})


class ResurrectAllEndpointTests(unittest.TestCase):
    def _app(self):
        calls = []

        def update_status(bug_id, status, **kwargs):
            calls.append(("update", bug_id, status, kwargs))

        return (
            SimpleNamespace(
                settings=SimpleNamespace(zentao_client_script="/tmp/zentao_client.py"),
                state=SimpleNamespace(
                    list_runs=lambda _limit: [
                        {"bug_id": 7472, "status": "unable_to_fix"},
                        {"bug_id": 7565, "status": "skipped_stale"},
                        {"bug_id": 7694, "status": "retry_exhausted"},
                        {"bug_id": 7693, "status": "pushed"},
                    ],
                    update_status=update_status,
                    record_run_event=lambda bug_id, event, message: calls.append(("event", bug_id, event)),
                    resurrect_for_retry=lambda bug_id: True,
                    clear_no_progress_fuses=lambda: 3,
                ),
                worker=SimpleNamespace(enqueue=lambda bug_id: calls.append(("enqueue", bug_id))),
            ),
            calls,
        )

    def test_resurrect_all_requeues_fresh_and_removes_handled(self):
        app, calls = self._app()
        with patch(
            "zentao_auto_fixer.server.bug_is_still_actionable",
            side_effect=[(True, ""), (False, "ZenTao status is now 'closed', not active"), (True, "")],
        ):
            status, _headers, body = post(app, "/runs/resurrect-all")

        self.assertEqual(status, 200)
        payload = json.loads(body)
        self.assertEqual(payload["resurrected"], [7472, 7694])
        self.assertEqual(payload["removed"], [7565])
        self.assertEqual(payload["skipped"], [])
        self.assertIn(("update", 7565, "handled_in_zentao", {"error": "ZenTao status is now 'closed', not active", "completed": True}), calls)
        self.assertIn(("enqueue", 7472), calls)
        self.assertIn(("enqueue", 7694), calls)
        # 成功的 run 不在候选里，永远不会被碰。
        self.assertFalse(any("7693" in str(item) for item in calls))

    def test_resurrect_all_skips_unreadable_bugs(self):
        app, calls = self._app()
        with patch(
            "zentao_auto_fixer.server.bug_is_still_actionable",
            side_effect=[
                ZenTaoPollError("ZenTao HTTP 403"),
                ZenTaoPollError("ZenTao HTTP 403"),
                ZenTaoPollError("ZenTao HTTP 403"),
            ],
        ):
            status, _headers, body = post(app, "/runs/resurrect-all")

        self.assertEqual(status, 200)
        payload = json.loads(body)
        self.assertEqual(payload["skipped"], [7472, 7565, 7694])
        self.assertEqual(payload["resurrected"], [])
        self.assertEqual(payload["removed"], [])
        # 禅道读取失败时保留原状态：不入队、不标记已处理、不清熔断。
        self.assertFalse(any(isinstance(item, tuple) and item[0] == "enqueue" for item in calls))
        self.assertFalse(any(isinstance(item, tuple) and item[0] == "update" for item in calls))


if __name__ == "__main__":
    unittest.main()
