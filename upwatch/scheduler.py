"""Background scheduler that checks every active monitor on an interval."""

from __future__ import annotations

import logging
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from typing import Callable

from upwatch.checker import DEFAULT_TIMEOUT, CheckResult, check_url
from upwatch.store import Check, Monitor, Store

DEFAULT_INTERVAL = 300  # five minutes

log = logging.getLogger("upwatch")


def run_check(store: Store, monitor: Monitor, timeout: float = DEFAULT_TIMEOUT,
              checker: Callable[[str, float], CheckResult] = check_url) -> Check | None:
    result = checker(monitor.url, timeout)
    saved = store.record_check(
        monitor.id, result.status, result.status_code, result.response_ms, result.error
    )
    if saved is not None:
        detail = f"{result.response_ms} ms" if result.response_ms is not None else result.error
        log.info("%-4s %s (%s) %s", result.status.upper(), monitor.name, monitor.url, detail)
    return saved


class Scheduler:
    """Checks each active monitor once every `interval` seconds.

    A monitor is due when it has never been checked or its last check is at
    least `interval` seconds old, so new monitors are checked within seconds
    and restarts pick up where they left off.
    """

    def __init__(self, store: Store, interval: float = DEFAULT_INTERVAL,
                 timeout: float = DEFAULT_TIMEOUT, tick: float = 2.0, workers: int = 8,
                 checker: Callable[[str, float], CheckResult] = check_url):
        self.store = store
        self.interval = interval
        self.timeout = timeout
        self.tick = tick
        self.checker = checker
        self._pool = ThreadPoolExecutor(max_workers=workers, thread_name_prefix="upwatch-check")
        self._in_flight: set[int] = set()
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        self._thread = threading.Thread(target=self._loop, name="upwatch-scheduler", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=5)
        self._pool.shutdown(wait=False, cancel_futures=True)

    def check_now(self, monitor: Monitor) -> bool:
        """Queue an immediate check. Returns False if one is already running."""
        with self._lock:
            if monitor.id in self._in_flight:
                return False
            self._in_flight.add(monitor.id)
        self._pool.submit(self._run, monitor)
        return True

    def due(self, now: float | None = None) -> list[Monitor]:
        now = time.time() if now is None else now
        return [
            m for m in self.store.list_monitors(active_only=True)
            if m.last_check is None or now - m.last_check.checked_at >= self.interval
        ]

    def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                for monitor in self.due():
                    self.check_now(monitor)
            except Exception:
                log.exception("scheduler tick failed")
            self._stop.wait(self.tick)

    def _run(self, monitor: Monitor) -> None:
        try:
            run_check(self.store, monitor, self.timeout, self.checker)
        except Exception:
            log.exception("check failed for %s", monitor.url)
        finally:
            with self._lock:
                self._in_flight.discard(monitor.id)
