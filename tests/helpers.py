import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


class _Target(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def do_GET(self):
        if self.path == "/ok":
            self._reply(200)
        elif self.path == "/missing":
            self._reply(404)
        elif self.path == "/error":
            self._reply(500)
        elif self.path == "/redirect":
            self.send_response(302)
            self.send_header("Location", "/ok")
            self.send_header("Content-Length", "0")
            self.end_headers()
        elif self.path == "/slow":
            time.sleep(1.5)
            self._reply(200)
        else:
            self._reply(404)

    def _reply(self, code):
        body = b"hello"
        self.send_response(code)
        self.send_header("Content-Length", str(len(body)))
        try:
            self.end_headers()
            self.wfile.write(body)
        except ConnectionError:  # the client gave up (timeout test)
            pass


class TargetSite:
    """A local website with predictable endpoints for checks to hit."""

    def __enter__(self):
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), _Target)
        self.server.daemon_threads = True
        self.base = f"http://127.0.0.1:{self.server.server_address[1]}"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        return self

    def __exit__(self, *exc):
        self.server.shutdown()
        self.server.server_close()
