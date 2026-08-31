import http.client
import threading
import unittest
from http.server import ThreadingHTTPServer
from types import SimpleNamespace

from zentao_auto_fixer.server import make_handler


class HealthTests(unittest.TestCase):
    def test_failed_run_degrades_health(self):
        app = SimpleNamespace(
            started_at="2026-08-28T00:00:00+00:00",
            state=SimpleNamespace(
                current_problem_count=lambda: 2,
                run_summary_since=lambda _since: {"queued": 3, "running": 1},
            ),
        )
        server = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(app))
        thread = threading.Thread(target=server.serve_forever)
        thread.start()
        try:
            connection = http.client.HTTPConnection(*server.server_address, timeout=2)
            connection.request("GET", "/health")
            response = connection.getresponse()

            self.assertEqual(response.status, 503)
            self.assertEqual(
                response.read(),
                b'{"ok": false, "problems": 2, "queued": 3, "running": 1}',
            )
        finally:
            server.shutdown()
            server.server_close()
            thread.join()


if __name__ == "__main__":
    unittest.main()
