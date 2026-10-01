import tempfile
import unittest
from pathlib import Path

from upwatch.store import HISTORY_LIMIT, Store, ValidationError, normalize_url


class StoreTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.tmp.name) / "test.db")

    def tearDown(self):
        self.tmp.cleanup()

    def test_normalize_url(self):
        self.assertEqual(normalize_url("example.com"), "https://example.com")
        self.assertEqual(normalize_url(" http://a.b/x "), "http://a.b/x")
        for bad in ("", "ftp://example.com", "https://"):
            with self.assertRaises(ValidationError):
                normalize_url(bad)

    def test_add_defaults_name_to_hostname_and_status_unknown(self):
        monitor = self.store.add_monitor("https://example.com/health")
        self.assertEqual(monitor.name, "example.com")
        self.assertEqual(monitor.status, "unknown")
        self.assertFalse(monitor.paused)
        self.assertIsNone(monitor.last_check)

    def test_find_by_id_or_name(self):
        monitor = self.store.add_monitor("https://example.com", "My Site")
        self.assertEqual(self.store.find_monitor(str(monitor.id)).id, monitor.id)
        self.assertEqual(self.store.find_monitor("my site").id, monitor.id)
        self.assertIsNone(self.store.find_monitor("nope"))

    def test_status_follows_latest_check(self):
        monitor = self.store.add_monitor("https://example.com")
        self.store.record_check(monitor.id, "up", 200, 42)
        self.assertEqual(self.store.get_monitor(monitor.id).status, "up")
        self.store.record_check(monitor.id, "down", None, None, "No response within 10s")
        latest = self.store.get_monitor(monitor.id)
        self.assertEqual(latest.status, "down")
        self.assertEqual(latest.last_check.error, "No response within 10s")

    def test_history_keeps_latest_100(self):
        monitor = self.store.add_monitor("https://example.com")
        for i in range(HISTORY_LIMIT + 25):
            self.store.record_check(monitor.id, "up", 200, i, checked_at=1000.0 + i)
        history = self.store.history(monitor.id, limit=1000)
        self.assertEqual(len(history), HISTORY_LIMIT)
        self.assertEqual(history[0].response_ms, HISTORY_LIMIT + 24)  # newest first
        self.assertEqual(history[-1].response_ms, 25)

    def test_trimming_is_per_monitor(self):
        a = self.store.add_monitor("https://a.example")
        b = self.store.add_monitor("https://b.example")
        for _ in range(HISTORY_LIMIT + 5):
            self.store.record_check(a.id, "up", 200, 1)
        self.store.record_check(b.id, "up", 200, 1)
        self.assertEqual(len(self.store.history(b.id)), 1)

    def test_pause_and_active_only(self):
        a = self.store.add_monitor("https://a.example")
        b = self.store.add_monitor("https://b.example")
        self.assertTrue(self.store.set_paused(a.id, True))
        self.assertEqual([m.id for m in self.store.list_monitors(active_only=True)], [b.id])
        self.store.set_paused(a.id, False)
        self.assertEqual(len(self.store.list_monitors(active_only=True)), 2)

    def test_settings_round_trip(self):
        self.assertIsNone(self.store.get_setting("interval"))
        self.store.set_setting("interval", 60)
        self.store.set_setting("interval", 120)
        self.assertEqual(self.store.get_setting("interval"), "120")
        reopened = Store(self.store.path)
        self.assertEqual(reopened.get_setting("interval"), "120")

    def test_remove_deletes_history(self):
        monitor = self.store.add_monitor("https://example.com")
        self.store.record_check(monitor.id, "up", 200, 5)
        self.assertTrue(self.store.remove_monitor(monitor.id))
        self.assertIsNone(self.store.get_monitor(monitor.id))
        self.assertEqual(self.store.history(monitor.id), [])
        self.assertFalse(self.store.remove_monitor(monitor.id))
        self.assertIsNone(self.store.record_check(monitor.id, "up", 200, 5))


if __name__ == "__main__":
    unittest.main()
