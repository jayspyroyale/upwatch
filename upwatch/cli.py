"""Command-line interface: `upwatch <command>`."""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
import time
import webbrowser
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime

from upwatch import __version__
from upwatch.checker import DEFAULT_TIMEOUT
from upwatch.scheduler import (DEFAULT_INTERVAL, Scheduler, run_check, saved_interval,
                               validate_interval)
from upwatch.store import HISTORY_LIMIT, Monitor, Store, ValidationError

DEFAULT_PORT = 8321

# -- output helpers --------------------------------------------------------

_COLORS = {"up": "32", "down": "31", "unknown": "90", "paused": "33", "dim": "90", "bold": "1"}


def _enable_windows_ansi() -> bool:
    try:
        import ctypes
        kernel32 = ctypes.windll.kernel32
        handle = kernel32.GetStdHandle(-11)  # STD_OUTPUT_HANDLE
        mode = ctypes.c_uint32()
        if not kernel32.GetConsoleMode(handle, ctypes.byref(mode)):
            return False
        return bool(kernel32.SetConsoleMode(handle, mode.value | 0x0004))
    except Exception:
        return False


def _use_color() -> bool:
    # sys.stdout is None under pythonw.exe (no console window).
    if os.environ.get("NO_COLOR") or sys.stdout is None or not sys.stdout.isatty():
        return False
    return _enable_windows_ansi() if os.name == "nt" else True


USE_COLOR = False


def paint(text: str, color: str) -> str:
    return f"\033[{_COLORS[color]}m{text}\033[0m" if USE_COLOR else text


def ago(ts: float | None) -> str:
    if ts is None:
        return "never"
    secs = max(0, int(time.time() - ts))
    if secs < 60:
        return f"{secs}s ago"
    if secs < 3600:
        return f"{secs // 60}m ago"
    if secs < 86400:
        return f"{secs // 3600}h {secs % 3600 // 60}m ago"
    return f"{secs // 86400}d ago"


def local_time(ts: float) -> str:
    return datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M:%S")


def status_label(monitor: Monitor) -> tuple[str, str]:
    """(plain text, color key) for the status column."""
    if monitor.paused:
        return f"PAUSED ({monitor.status})", "paused"
    return monitor.status.upper(), monitor.status


def table(headers: list[str], rows: list[list[tuple[str, str | None]]]) -> str:
    """Render rows of (text, color) cells; widths are computed from plain text."""
    widths = [len(h) for h in headers]
    for row in rows:
        for i, (text, _) in enumerate(row):
            widths[i] = max(widths[i], len(text))
    out = ["  ".join(paint(h.ljust(w), "bold") for h, w in zip(headers, widths)).rstrip()]
    for row in rows:
        cells = [paint(t.ljust(w), c) if c else t.ljust(w) for (t, c), w in zip(row, widths)]
        out.append("  ".join(cells).rstrip())
    return "\n".join(out)


def ms(value: int | None) -> str:
    return f"{value} ms" if value is not None else "-"


def fail(message: str) -> int:
    print(paint(f"error: {message}", "down"), file=sys.stderr)
    return 1


def resolve(store: Store, ref: str) -> Monitor | None:
    monitor = store.find_monitor(ref)
    if monitor is None:
        fail(f"no monitor with id or name '{ref}'. Run 'upwatch list' to see them.")
    return monitor


# -- commands --------------------------------------------------------------

def cmd_add(store: Store, args) -> int:
    try:
        monitor = store.add_monitor(args.url, args.name)
    except ValidationError as exc:
        return fail(str(exc))
    print(f"Added #{monitor.id} {paint(monitor.name, 'bold')} -> {monitor.url}")
    print(paint("It will be checked as soon as 'upwatch serve' is running "
                "(or run 'upwatch check' now).", "dim"))
    return 0


def cmd_list(store: Store, args) -> int:
    monitors = store.list_monitors()
    if args.json:
        print(json.dumps([m.to_dict() for m in monitors], indent=2))
        return 0
    if not monitors:
        print("No monitors yet. Add one with:  upwatch add https://example.com --name Example")
        return 0
    rows = []
    for m in monitors:
        text, color = status_label(m)
        last = m.last_check
        rows.append([
            (str(m.id), None),
            (text, color),
            (m.name, None),
            (m.url, "dim"),
            (ago(last.checked_at) if last else "never", None),
            (ms(last.response_ms) if last else "-", None),
            ((last.error or "") if last else "", "down" if last and last.error else None),
        ])
    print(table(["ID", "STATUS", "NAME", "URL", "LAST CHECK", "RESPONSE", "DETAIL"], rows))
    return 0


def cmd_history(store: Store, args) -> int:
    monitor = resolve(store, args.monitor)
    if monitor is None:
        return 1
    checks = store.history(monitor.id, args.limit)
    if args.json:
        print(json.dumps([c.to_dict() for c in checks], indent=2))
        return 0
    print(f"{paint(monitor.name, 'bold')}  {paint(monitor.url, 'dim')}")
    if not checks:
        print("No checks recorded yet.")
        return 0
    up = sum(c.status == "up" for c in checks)
    plural = "s" if len(checks) != 1 else ""
    print(paint(f"Last {len(checks)} check{plural}, {100 * up / len(checks):.1f}% up\n", "dim"))
    rows = [[
        (local_time(c.checked_at), None),
        (c.status.upper(), c.status),
        (str(c.status_code) if c.status_code else "-", None),
        (ms(c.response_ms), None),
        (c.error or "", "down" if c.error else None),
    ] for c in checks]
    print(table(["CHECKED AT", "STATUS", "HTTP", "RESPONSE", "DETAIL"], rows))
    return 0


def cmd_pause(store: Store, args, paused: bool) -> int:
    monitor = resolve(store, args.monitor)
    if monitor is None:
        return 1
    store.set_paused(monitor.id, paused)
    print(f"{'Paused' if paused else 'Resumed'} #{monitor.id} {monitor.name}")
    return 0


def cmd_remove(store: Store, args) -> int:
    monitor = resolve(store, args.monitor)
    if monitor is None:
        return 1
    if not args.yes and sys.stdin.isatty():
        answer = input(f"Remove #{monitor.id} {monitor.name} and its history? [y/N] ")
        if answer.strip().lower() not in ("y", "yes"):
            print("Cancelled.")
            return 1
    store.remove_monitor(monitor.id)
    print(f"Removed #{monitor.id} {monitor.name}")
    return 0


def cmd_check(store: Store, args) -> int:
    if args.monitors:
        monitors = [resolve(store, ref) for ref in args.monitors]
        if any(m is None for m in monitors):
            return 1
    else:
        monitors = store.list_monitors(active_only=True)
    if not monitors:
        print("Nothing to check. Add a monitor with 'upwatch add <url>'.")
        return 0
    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(lambda m: (m, run_check(store, m, args.timeout)), monitors))
    rows = [[
        (str(m.id), None),
        (c.status.upper(), c.status),
        (m.name, None),
        (ms(c.response_ms), None),
        (c.error or "", "down" if c.error else None),
    ] for m, c in results if c]
    print(table(["ID", "STATUS", "NAME", "RESPONSE", "DETAIL"], rows))
    return 2 if any(c and c.status == "down" for _, c in results) else 0


def cmd_serve(store: Store, args) -> int:
    from upwatch.server import DashboardServer

    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO,
                        format="%(asctime)s  %(message)s", datefmt="%H:%M:%S")
    scheduler = Scheduler(store, interval=saved_interval(store), timeout=args.timeout)
    if args.interval is not None:
        scheduler.set_interval(args.interval)
    try:
        server = DashboardServer((args.host, args.port), store, scheduler)
    except OSError as exc:
        return fail(f"could not listen on {args.host}:{args.port} ({exc}). "
                    "Try another port with --port.")
    local_only = args.host in ("127.0.0.1", "::1", "localhost")
    host = "localhost" if local_only or args.host in ("0.0.0.0", "::") else args.host
    url = f"http://{host}:{args.port}"
    print(f"{paint('upwatch', 'bold')} {__version__}  dashboard -> {paint(url, 'up')}")
    print(paint(f"checking every {scheduler.interval}s (change it in the dashboard), "
                f"timeout {args.timeout:g}s, "
                f"database {store.path}", "dim"))
    if not local_only:
        print(paint("warning: the dashboard has no login; anyone who can reach this "
                    "address can manage your monitors.", "paused"))
    print(paint("Press Ctrl+C to stop.\n", "dim"))
    scheduler.start()
    if args.open:
        webbrowser.open(url)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nStopping...")
    finally:
        scheduler.stop()
        server.server_close()
    return 0


# -- argument parsing ------------------------------------------------------

def positive(value: str) -> float:
    number = float(value)
    if number <= 0:
        raise argparse.ArgumentTypeError("must be greater than 0")
    return number


def interval_arg(value: str) -> int:
    try:
        return validate_interval(value)
    except ValidationError as exc:
        raise argparse.ArgumentTypeError(str(exc))


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="upwatch",
        description="A tiny uptime monitor for a handful of URLs.",
        epilog="Start here:  upwatch add https://example.com --name Example  then  upwatch serve",
    )
    parser.add_argument("--version", action="version", version=f"upwatch {__version__}")
    parser.add_argument("--db", metavar="PATH",
                        help="database file (default: $UPWATCH_DB or ~/.upwatch/upwatch.db)")
    sub = parser.add_subparsers(dest="command", metavar="<command>")

    p = sub.add_parser("add", help="add a URL to monitor")
    p.add_argument("url", help="address to check, e.g. https://example.com")
    p.add_argument("-n", "--name", help="friendly name (default: the hostname)")

    p = sub.add_parser("list", aliases=["ls"], help="show all monitors and their status")
    p.add_argument("--json", action="store_true", help="output JSON")

    p = sub.add_parser("history", help="show recent checks for a monitor")
    p.add_argument("monitor", help="monitor id or name")
    p.add_argument("-l", "--limit", type=int, default=20,
                   help=f"how many checks to show (max {HISTORY_LIMIT}, default 20)")
    p.add_argument("--json", action="store_true", help="output JSON")

    for name, text in (("pause", "stop checking a monitor"), ("resume", "start checking again")):
        p = sub.add_parser(name, help=text)
        p.add_argument("monitor", help="monitor id or name")

    p = sub.add_parser("remove", aliases=["rm"], help="delete a monitor and its history")
    p.add_argument("monitor", help="monitor id or name")
    p.add_argument("-y", "--yes", action="store_true", help="don't ask for confirmation")

    p = sub.add_parser("check", help="check now and save the results (exit code 2 if any are down)")
    p.add_argument("monitors", nargs="*", metavar="monitor",
                   help="ids or names (default: every active monitor)")
    p.add_argument("--timeout", type=positive, default=DEFAULT_TIMEOUT,
                   help=f"seconds to wait for a response (default {DEFAULT_TIMEOUT:g})")

    p = sub.add_parser("serve", help="run the scheduler and the web dashboard")
    p.add_argument("--host", default="127.0.0.1",
                   help="address to listen on (default 127.0.0.1, this computer only)")
    p.add_argument("-p", "--port", type=int, default=DEFAULT_PORT,
                   help=f"port (default {DEFAULT_PORT})")
    p.add_argument("--interval", type=interval_arg, default=None,
                   help="seconds between checks of each URL; saved for next time "
                        f"(default: last value used, else {DEFAULT_INTERVAL})")
    p.add_argument("--timeout", type=positive, default=DEFAULT_TIMEOUT,
                   help=f"seconds to wait for a response (default {DEFAULT_TIMEOUT:g})")
    p.add_argument("--open", action="store_true", help="open the dashboard in your browser")
    p.add_argument("-v", "--verbose", action="store_true", help="log every HTTP request")
    return parser


def main(argv: list[str] | None = None) -> int:
    global USE_COLOR
    USE_COLOR = _use_color()
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(errors="replace")
        except Exception:
            pass

    parser = build_parser()
    args = parser.parse_args(argv)
    if not args.command:
        parser.print_help()
        return 0

    store = Store(args.db) if args.db else Store()
    command = args.command
    if command in ("pause", "resume"):
        return cmd_pause(store, args, paused=command == "pause")
    handlers = {"add": cmd_add, "list": cmd_list, "ls": cmd_list, "history": cmd_history,
                "remove": cmd_remove, "rm": cmd_remove, "check": cmd_check, "serve": cmd_serve}
    return handlers[command](store, args)
