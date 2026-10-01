"""Local dashboard: a small JSON API plus a single-page UI, stdlib only."""

from __future__ import annotations

import json
import logging
import os
import re
import socket
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from upwatch import __version__
from upwatch.scheduler import MAX_INTERVAL, MIN_INTERVAL, Scheduler
from upwatch.store import HISTORY_LIMIT, Monitor, Store, ValidationError

STATIC_DIR = Path(__file__).parent / "static"
RECENT_POINTS = 30
LOOPBACK_HOSTS = {"localhost", "127.0.0.1", "::1"}
# Every state-changing request must carry this header. Browsers will not let
# another website add a custom header to a cross-origin request without a CORS
# preflight, which this server never approves.
CSRF_HEADER = "X-Upwatch"

log = logging.getLogger("upwatch")

MONITOR_PATH = re.compile(r"^/api/monitors/(\d+)(?:/(checks|pause|resume|check))?$")


def monitor_summary(store: Store, monitor: Monitor) -> dict:
    data = monitor.to_dict()
    history = store.history(monitor.id)
    times = [c.response_ms for c in history if c.status == "up" and c.response_ms is not None]
    data["uptime_pct"] = (
        round(100 * sum(c.status == "up" for c in history) / len(history), 1) if history else None
    )
    data["avg_ms"] = round(sum(times) / len(times)) if times else None
    data["checks_stored"] = len(history)
    data["recent"] = [
        {"status": c.status, "response_ms": c.response_ms, "checked_at": c.checked_at,
         "status_code": c.status_code, "error": c.error}
        for c in reversed(history[:RECENT_POINTS])
    ]
    return data


class Handler(BaseHTTPRequestHandler):
    server: "DashboardServer"
    server_version = f"upwatch/{__version__}"

    # -- plumbing ---------------------------------------------------------

    def log_message(self, fmt: str, *args) -> None:  # keep the console quiet
        log.debug("%s - %s", self.address_string(), fmt % args)

    def _send_json(self, payload, status: int = 200) -> None:
        body = json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _error(self, status: int, message: str) -> None:
        self._send_json({"error": message}, status)

    def _read_json(self) -> dict:
        length = int(self.headers.get("Content-Length") or 0)
        if length > 64 * 1024:
            raise ValidationError("Request body too large.")
        raw = self.rfile.read(length) if length else b"{}"
        try:
            data = json.loads(raw or b"{}")
        except json.JSONDecodeError:
            raise ValidationError("Request body must be JSON.")
        if not isinstance(data, dict):
            raise ValidationError("Request body must be a JSON object.")
        return data

    def _host_allowed(self) -> bool:
        if not self.server.loopback_only:
            return True
        host = (self.headers.get("Host") or "").strip()
        if host.startswith("["):  # [::1]:8321
            host = host[1:].split("]", 1)[0]
        else:
            host = host.rsplit(":", 1)[0] if host.count(":") == 1 else host
        return host.lower() in LOOPBACK_HOSTS

    def _guard(self, mutating: bool) -> bool:
        if not self._host_allowed():
            self._error(HTTPStatus.FORBIDDEN, "Host not allowed.")
            return False
        if mutating and self.headers.get(CSRF_HEADER) != "1":
            self._error(HTTPStatus.FORBIDDEN, f"Missing {CSRF_HEADER} header.")
            return False
        return True

    def _info(self) -> dict:
        sched = self.server.scheduler
        return {
            "version": __version__,
            "interval": sched.interval,
            "interval_min": MIN_INTERVAL,
            "interval_max": MAX_INTERVAL,
            "timeout": sched.timeout,
            "history_limit": HISTORY_LIMIT,
            "db_path": str(self.server.store.path),
        }

    # -- routes -----------------------------------------------------------

    def do_GET(self) -> None:
        if not self._guard(mutating=False):
            return
        url = urlparse(self.path)
        store = self.server.store

        if url.path in ("/", "/index.html"):
            body = (STATIC_DIR / "index.html").read_bytes()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Security-Policy",
                             "default-src 'self'; style-src 'unsafe-inline'; "
                             "script-src 'unsafe-inline'; img-src 'self' data:")
            self.end_headers()
            self.wfile.write(body)
            return

        if url.path == "/api/info":
            self._send_json(self._info())
            return

        if url.path == "/api/monitors":
            self._send_json([monitor_summary(store, m) for m in store.list_monitors()])
            return

        match = MONITOR_PATH.match(url.path)
        if match:
            monitor = store.get_monitor(int(match.group(1)))
            if monitor is None:
                return self._error(HTTPStatus.NOT_FOUND, "Monitor not found.")
            if match.group(2) is None:
                return self._send_json(monitor_summary(store, monitor))
            if match.group(2) == "checks":
                query = parse_qs(url.query)
                try:
                    limit = max(1, min(HISTORY_LIMIT, int(query.get("limit", [HISTORY_LIMIT])[0])))
                except ValueError:
                    limit = HISTORY_LIMIT
                return self._send_json([c.to_dict() for c in store.history(monitor.id, limit)])

        self._error(HTTPStatus.NOT_FOUND, "Not found.")

    def do_POST(self) -> None:
        if not self._guard(mutating=True):
            return
        path = urlparse(self.path).path
        store = self.server.store
        try:
            if path == "/api/monitors":
                data = self._read_json()
                monitor = store.add_monitor(str(data.get("url", "")), str(data.get("name") or ""))
                self.server.scheduler.check_now(monitor)
                return self._send_json(monitor_summary(store, monitor), HTTPStatus.CREATED)

            if path == "/api/settings":
                data = self._read_json()
                if "interval" in data:
                    self.server.scheduler.set_interval(data["interval"])
                return self._send_json(self._info())

            match = MONITOR_PATH.match(path)
            if match and match.group(2) in ("pause", "resume", "check"):
                monitor = store.get_monitor(int(match.group(1)))
                if monitor is None:
                    return self._error(HTTPStatus.NOT_FOUND, "Monitor not found.")
                action = match.group(2)
                if action == "check":
                    queued = self.server.scheduler.check_now(monitor)
                    return self._send_json({"queued": queued}, HTTPStatus.ACCEPTED)
                store.set_paused(monitor.id, action == "pause")
                return self._send_json(monitor_summary(store, store.get_monitor(monitor.id)))
        except ValidationError as exc:
            return self._error(HTTPStatus.BAD_REQUEST, str(exc))
        self._error(HTTPStatus.NOT_FOUND, "Not found.")

    def do_DELETE(self) -> None:
        if not self._guard(mutating=True):
            return
        match = MONITOR_PATH.match(urlparse(self.path).path)
        if match and match.group(2) is None:
            if self.server.store.remove_monitor(int(match.group(1))):
                return self._send_json({"removed": True})
            return self._error(HTTPStatus.NOT_FOUND, "Monitor not found.")
        self._error(HTTPStatus.NOT_FOUND, "Not found.")


class DashboardServer(ThreadingHTTPServer):
    daemon_threads = True
    # On Windows SO_REUSEADDR lets a second server bind a port that is already
    # in use, so a forgotten `upwatch serve` would silently keep answering.
    allow_reuse_address = os.name != "nt"

    def __init__(self, address: tuple[str, int], store: Store, scheduler: Scheduler):
        if ":" in address[0]:
            self.address_family = socket.AF_INET6
        super().__init__(address, Handler)
        self.store = store
        self.scheduler = scheduler
        self.loopback_only = address[0] in LOOPBACK_HOSTS
