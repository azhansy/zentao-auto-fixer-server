import http.client
import json
import threading
import unittest
from http.server import ThreadingHTTPServer
from types import SimpleNamespace
from unittest.mock import patch

from zentao_auto_fixer.server import make_handler


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


if __name__ == "__main__":
    unittest.main()
