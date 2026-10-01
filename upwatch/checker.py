"""Perform a single HTTP check against a URL."""

from __future__ import annotations

import socket
import ssl
import time
import urllib.error
import urllib.request
from dataclasses import dataclass

from upwatch import __version__

DEFAULT_TIMEOUT = 10.0
USER_AGENT = f"upwatch/{__version__} (+https://github.com/jayspyroyale/upwatch)"


@dataclass
class CheckResult:
    status: str  # "up" or "down"
    status_code: int | None
    response_ms: int | None
    error: str | None


def _describe(exc: BaseException, timeout: float) -> str:
    reason = getattr(exc, "reason", exc)
    if isinstance(reason, (socket.timeout, TimeoutError)):
        return f"No response within {timeout:g}s"
    if isinstance(reason, ssl.SSLError):
        return f"SSL error: {getattr(reason, 'reason', None) or reason}"
    if isinstance(reason, socket.gaierror):
        return "DNS lookup failed"
    if isinstance(reason, ConnectionRefusedError):
        return "Connection refused"
    if isinstance(reason, ConnectionResetError):
        return "Connection reset"
    text = str(reason).strip() or type(reason).__name__
    return text[:300]


def check_url(url: str, timeout: float = DEFAULT_TIMEOUT) -> CheckResult:
    """GET `url` and report whether it is up.

    Up   = the server answered with a status below 400 (redirects are followed).
    Down = an HTTP error status (4xx/5xx), or no response before `timeout`
           (connection errors, DNS failures and TLS errors count too).

    Response time is measured until the response headers arrive.
    """
    request = urllib.request.Request(
        url, method="GET", headers={"User-Agent": USER_AGENT, "Accept": "*/*"}
    )
    start = time.perf_counter()
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            elapsed = round((time.perf_counter() - start) * 1000)
            code = response.status
    except urllib.error.HTTPError as exc:
        elapsed = round((time.perf_counter() - start) * 1000)
        exc.close()
        return CheckResult("down", exc.code, elapsed, f"HTTP {exc.code} {exc.reason}".strip())
    except (urllib.error.URLError, OSError, ValueError) as exc:
        return CheckResult("down", None, None, _describe(exc, timeout))
    except Exception as exc:  # never let one odd site kill the scheduler
        return CheckResult("down", None, None, _describe(exc, timeout))

    if code >= 400:
        return CheckResult("down", code, elapsed, f"HTTP {code}")
    return CheckResult("up", code, elapsed, None)
