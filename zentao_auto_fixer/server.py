from __future__ import annotations

import json
import logging
import signal
import threading
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Dict, Tuple
from urllib.parse import parse_qs, urlparse

from .config import Settings
from .poller import Poller
from .state import StateStore, utc_now
from .worker import Worker


LOGGER = logging.getLogger("zentao_auto_fixer")


class App:
    def __init__(self, settings: Settings):
        self.settings = settings
        self.started_at = utc_now()
        self.state = StateStore(settings.database_path)
        self.worker = Worker(settings, self.state)
        self.poller = Poller(settings, self.state, self.worker)
        self._stopped = False
        self._stop_lock = threading.Lock()

    def start(self) -> None:
        self.settings.data_dir.mkdir(parents=True, exist_ok=True)
        self.worker.start()
        self.poller.start()

    def stop(self) -> None:
        with self._stop_lock:
            if self._stopped:
                return
            self._stopped = True
        self.poller.stop()
        self.worker.stop()


def make_handler(app: App):
    class Handler(BaseHTTPRequestHandler):
        server_version = "ZentaoAutoFixer/0.1"

        def do_GET(self) -> None:
            path, query = _path_and_query(self.path)
            if path == "/health":
                self._json(HTTPStatus.OK, {"ok": True})
                return
            if path == "/runs":
                self._json(HTTPStatus.OK, {"runs": app.state.list_runs()})
                return
            if path == "/polls":
                self._json(
                    HTTPStatus.OK,
                    {"polls": app.state.list_poll_runs(_limit_from_query(query))},
                )
                return
            if path.startswith("/polls/"):
                project_name = path.removeprefix("/polls/")
                self._json(
                    HTTPStatus.OK,
                    {"polls": app.state.list_poll_runs(_limit_from_query(query), project_name)},
                )
                return
            if path.startswith("/runs/"):
                rest = path.removeprefix("/runs/")
                events_requested = rest.endswith("/events")
                bug_id_text = rest.removesuffix("/events").rstrip("/")
                try:
                    bug_id = int(bug_id_text)
                except ValueError:
                    self._json(HTTPStatus.BAD_REQUEST, {"error": "bug_id must be an integer"})
                    return
                if events_requested:
                    self._json(
                        HTTPStatus.OK,
                        {"events": app.state.list_run_events(bug_id, _limit_from_query(query))},
                    )
                    return
                run = app.state.get_run(bug_id)
                if not run:
                    self._json(HTTPStatus.NOT_FOUND, {"error": "run not found"})
                    return
                self._json(HTTPStatus.OK, {"run": run.__dict__})
                return
            self._json(HTTPStatus.NOT_FOUND, {"error": "not found"})

        def log_message(self, fmt: str, *args: Any) -> None:
            LOGGER.info("%s - %s", self.address_string(), fmt % args)

        def _json(self, status: HTTPStatus, payload: Dict[str, Any]) -> None:
            data = json.dumps(payload, ensure_ascii=False, sort_keys=True).encode("utf-8")
            self.send_response(status.value)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

    return Handler


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    settings = Settings.from_env()
    app = App(settings)
    server = ThreadingHTTPServer((settings.host, settings.port), make_handler(app))

    def shutdown(_signum: int, _frame: Any) -> None:
        LOGGER.info("收到退出信号，正在停止自动解决 Bug 服务...")
        app.stop()
        threading.Thread(target=server.shutdown, name="auto-fixer-shutdown", daemon=True).start()

    signal.signal(signal.SIGTERM, shutdown)
    signal.signal(signal.SIGINT, shutdown)
    _log_startup(settings)
    app.start()
    try:
        server.serve_forever()
    finally:
        app.stop()
        server.server_close()
        _log_shutdown_summary(app)


def _path_and_query(raw_path: str) -> Tuple[str, Dict[str, str]]:
    parsed = urlparse(raw_path)
    query = {key: values[-1] for key, values in parse_qs(parsed.query).items() if values}
    return parsed.path, query


def _limit_from_query(query: Dict[str, str]) -> int:
    try:
        return max(1, min(500, int(query.get("limit", "100"))))
    except ValueError:
        return 100


def _log_startup(settings: Settings) -> None:
    LOGGER.info(
        "自动解决 Bug 服务已开启：地址=http://%s:%s，将每 %s 秒轮询一次。按 Ctrl+C 退出服务。",
        settings.host,
        settings.port,
        settings.poll_interval_seconds,
    )
    LOGGER.info(
        "配置：workers=%s enabled_projects=%s projects_file=%s data_dir=%s",
        settings.worker_count,
        _enabled_project_count(settings),
        settings.projects_file,
        settings.data_dir,
    )


def _enabled_project_count(settings: Settings) -> int:
    try:
        return sum(1 for project in settings.load_projects() if project.enabled)
    except Exception:
        LOGGER.exception("读取项目配置失败")
        return 0


def _log_shutdown_summary(app: App) -> None:
    summary = app.state.run_summary_since(app.started_at)
    LOGGER.info(
        "自动解决 Bug 服务已退出。本次完成处理 %s 个；自动修复 %s 个，其中已提交推送 %s 个、无代码变更 %s 个；失败 %s 个、同步冲突 %s 个、转人工 %s 个；退出时排队 %s 个、运行中 %s 个。",
        summary["completed"],
        summary["auto_fixed"],
        summary["pushed"],
        summary["no_changes"],
        summary["failed"],
        summary["sync_conflict"],
        summary["manual_required"],
        summary["queued"],
        summary["running"],
    )


if __name__ == "__main__":
    main()
