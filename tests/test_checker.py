import socket
import unittest

from tests.helpers import TargetSite
from upwatch.checker import check_url


class CheckerTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.site = TargetSite().__enter__()

    @classmethod
    def tearDownClass(cls):
        cls.site.__exit__(None, None, None)

    def test_200_is_up_with_response_time(self):
        result = check_url(self.site.base + "/ok", timeout=5)
        self.assertEqual(result.status, "up")
        self.assertEqual(result.status_code, 200)
        self.assertIsInstance(result.response_ms, int)
        self.assertIsNone(result.error)

    def test_redirect_is_followed(self):
        result = check_url(self.site.base + "/redirect", timeout=5)
        self.assertEqual((result.status, result.status_code), ("up", 200))

    def test_http_errors_are_down(self):
        for path, code in (("/missing", 404), ("/error", 500)):
            result = check_url(self.site.base + path, timeout=5)
            self.assertEqual((result.status, result.status_code), ("down", code))
            self.assertIn(str(code), result.error)
            self.assertIsNotNone(result.response_ms)

    def test_timeout_is_down(self):
        result = check_url(self.site.base + "/slow", timeout=0.3)
        self.assertEqual(result.status, "down")
        self.assertIsNone(result.response_ms)
        self.assertIn("No response within 0.3s", result.error)

    def test_connection_refused_is_down(self):
        with socket.socket() as sock:
            sock.bind(("127.0.0.1", 0))
            port = sock.getsockname()[1]
        result = check_url(f"http://127.0.0.1:{port}/", timeout=2)
        self.assertEqual(result.status, "down")
        self.assertIsNone(result.status_code)
        self.assertTrue(result.error)


if __name__ == "__main__":
    unittest.main()
