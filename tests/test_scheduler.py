import tempfile
import threading
import time
import unittest
from pathlib import Path

from upwatch.checker import CheckResult
from upwatch.scheduler import Scheduler
from upwatch.store import Store


class SchedulerTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.tmp.name) / "test.db")
        self.calls = []
        self.lock = threading.Lock()

    def tearDown(self):
        self.tmp.cleanup()

    def fake_checker(self, url, timeout):
        with self.lock:
            self.calls.append(url)
        return CheckResult("up", 200, 12, None)

    def test_due_rules(self):
        sched = Scheduler(self.store, interval=300, checker=self.fake_checker)
        fresh = self.store.add_monitor("https://fresh.example")
        recent = self.store.add_monitor("https://recent.example")
        stale = self.store.add_monitor("https://stale.example")
        paused = self.store.add_monitor("https://paused.example")
        now = time.time()
        self.store.record_check(recent.id, "up", 200, 1, checked_at=now - 60)
        self.store.record_check(stale.id, "up", 200, 1, checked_at=now - 301)
        self.store.set_paused(paused.id, True)
        due = {m.id for m in sched.due(now)}
        self.assertEqual(due, {fresh.id, stale.id})

    def test_runs_new_monitor_once_per_interval(self):
        monitor = self.store.add_monitor("https://example.com")
        sched = Scheduler(self.store, interval=300, tick=0.05, checker=self.fake_checker)
        sched.start()
        try:
            deadline = time.time() + 5
            while not self.store.history(monitor.id) and time.time() < deadline:
                time.sleep(0.05)
            time.sleep(0.3)  # several more ticks: must not re-check before the interval
        finally:
            sched.stop()
        self.assertEqual(self.calls, ["https://example.com"])
        self.assertEqual(self.store.get_monitor(monitor.id).status, "up")


if __name__ == "__main__":
    unittest.main()
