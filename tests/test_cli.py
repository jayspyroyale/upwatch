import contextlib
import io
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from tests.helpers import TargetSite
from upwatch import cli


class CliTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = str(Path(self.tmp.name) / "cli.db")

    def tearDown(self):
        self.tmp.cleanup()

    def run_cli(self, *args):
        out = io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(io.StringIO()):
            code = cli.main(["--db", self.db, *args])
        return code, out.getvalue()

    def test_add_list_pause_remove(self):
        self.assertEqual(self.run_cli("add", "example.com", "-n", "Example")[0], 0)
        code, out = self.run_cli("list", "--json")
        monitors = json.loads(out)
        self.assertEqual(monitors[0]["status"], "unknown")
        self.assertEqual(self.run_cli("pause", "example")[0], 0)
        self.assertTrue(json.loads(self.run_cli("list", "--json")[1])[0]["paused"])
        self.assertEqual(self.run_cli("rm", "1", "-y")[0], 0)
        self.assertEqual(json.loads(self.run_cli("list", "--json")[1]), [])

    def test_unknown_monitor_and_bad_url_fail(self):
        self.assertEqual(self.run_cli("pause", "nope")[0], 1)
        self.assertEqual(self.run_cli("add", "ftp://example.com")[0], 1)

    def test_check_exit_code_reflects_down_sites(self):
        with TargetSite() as site:
            self.run_cli("add", site.base + "/ok", "-n", "ok")
            code, out = self.run_cli("check")
            self.assertEqual(code, 0)
            self.assertIn("UP", out)
            self.run_cli("add", site.base + "/error", "-n", "broken")
            code, out = self.run_cli("check")
            self.assertEqual(code, 2)
            code, out = self.run_cli("history", "broken", "--json")
            self.assertEqual(json.loads(out)[0]["status_code"], 500)

    def test_serve_rejects_out_of_range_interval(self):
        with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit) as ctx:
            cli.build_parser().parse_args(["serve", "--interval", "5"])
        self.assertEqual(ctx.exception.code, 2)
        args = cli.build_parser().parse_args(["serve"])
        self.assertIsNone(args.interval)  # None means "use the saved interval"

    def test_no_console_does_not_crash(self):
        with mock.patch.object(sys, "stdout", None):
            self.assertFalse(cli._use_color())


if __name__ == "__main__":
    unittest.main()
