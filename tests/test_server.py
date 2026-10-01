import http.client
import json
import tempfile
import threading
import unittest
from pathlib import Path

from upwatch.checker import CheckResult
from upwatch.scheduler import Scheduler
from upwatch.server import DashboardServer
from upwatch.store import Store


class ServerTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.tmp.name) / "test.db")
        checker = lambda url, timeout: CheckResult("up", 200, 7, None)
        self.scheduler = Scheduler(self.store, interval=300, checker=checker)
        self.server = DashboardServer(("127.0.0.1", 0), self.store, self.scheduler)
        self.port = self.server.server_address[1]
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.scheduler.stop()
        self.tmp.cleanup()

    def request(self, method, path, body=None, headers=None, host=None):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        all_headers = {"X-Upwatch": "1", "Content-Type": "application/json"}
        all_headers.update(headers or {})
        if host:
            all_headers["Host"] = host
        conn.request(method, path, json.dumps(body) if body is not None else None, all_headers)
        res = conn.getresponse()
        raw = res.read()
        conn.close()
        ctype = res.getheader("Content-Type", "")
        return res.status, (json.loads(raw) if "json" in ctype else raw.decode())

    def test_dashboard_page(self):
        status, body = self.request("GET", "/")
        self.assertEqual(status, 200)
        self.assertIn("<title>upwatch</title>", body)

    def test_full_monitor_lifecycle(self):
        status, created = self.request("POST", "/api/monitors", {"name": "Ex", "url": "example.com"})
        self.assertEqual(status, 201)
        self.assertEqual(created["url"], "https://example.com")
        self.assertIn(created["status"], ("unknown", "up"))
        mid = created["id"]

        status, listing = self.request("GET", "/api/monitors")
        self.assertEqual([m["name"] for m in listing], ["Ex"])

        self.store.record_check(mid, "down", 503, 30, "HTTP 503")
        status, one = self.request("GET", f"/api/monitors/{mid}")
        self.assertEqual(one["status"], "down")
        self.assertEqual(one["last_check"]["status_code"], 503)

        status, checks = self.request("GET", f"/api/monitors/{mid}/checks?limit=5")
        self.assertEqual(status, 200)
        self.assertEqual(checks[0]["error"], "HTTP 503")

        status, paused = self.request("POST", f"/api/monitors/{mid}/pause")
        self.assertTrue(paused["paused"])
        status, resumed = self.request("POST", f"/api/monitors/{mid}/resume")
        self.assertFalse(resumed["paused"])

        status, _ = self.request("DELETE", f"/api/monitors/{mid}")
        self.assertEqual(status, 200)
        status, _ = self.request("GET", f"/api/monitors/{mid}")
        self.assertEqual(status, 404)

    def test_invalid_url_rejected(self):
        status, body = self.request("POST", "/api/monitors", {"url": "ftp://example.com"})
        self.assertEqual(status, 400)
        self.assertIn("http", body["error"])

    def test_writes_require_header(self):
        status, _ = self.request("POST", "/api/monitors", {"url": "example.com"},
                                 headers={"X-Upwatch": ""})
        self.assertEqual(status, 403)
        self.assertEqual(self.store.list_monitors(), [])

    def test_foreign_host_rejected(self):
        status, _ = self.request("GET", "/api/monitors", host="evil.example")
        self.assertEqual(status, 403)
        status, _ = self.request("GET", "/api/monitors", host=f"localhost:{self.port}")
        self.assertEqual(status, 200)


if __name__ == "__main__":
    unittest.main()
