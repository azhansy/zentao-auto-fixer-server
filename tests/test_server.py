import http.client
import json
import threading
import unittest
from http.server import ThreadingHTTPServer
from types import SimpleNamespace
from unittest.mock import patch

from zentao_auto_fixer.server import make_handler


def get(app, path):
    server = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(app))
    thread = threading.Thread(target=server.serve_forever)
    thread.start()
    try:
        connection = http.client.HTTPConnection(*server.server_address, timeout=2)
        connection.request("GET", path)
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


if __name__ == "__main__":
    unittest.main()
