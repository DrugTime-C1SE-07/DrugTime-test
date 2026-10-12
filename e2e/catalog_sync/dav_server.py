"""Local deterministic DAV fixture with bounded, controllable HTTP holds."""

import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

SNAPSHOT = Path(__file__).with_name("fixtures") / "dav_snapshot.json"


class DavServer:
    def __init__(self):
        self.document = json.loads(SNAPSHOT.read_text(encoding="utf-8"))
        self.statuses = []
        self.requests = []
        self.entered = threading.Event()
        self.release = threading.Event()
        self.release.set()
        self.hold = False
        self.lock = threading.Lock()
        fixture = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_POST(self):
                self.connection.settimeout(5)
                length = int(self.headers.get("Content-Length", 0))
                if length > 65536:
                    self.send_error(413)
                    return
                request = json.loads(self.rfile.read(length))
                with fixture.lock:
                    index = len(fixture.requests)
                    status = fixture.statuses[min(index, len(fixture.statuses) - 1)] \
                        if fixture.statuses else 200
                    record = {"index": index, "skipCount": request.get("skipCount"),
                              "maxResultCount": request.get("maxResultCount"), "status": status,
                              "received_at": time.monotonic()}
                    fixture.requests.append(record)
                    document = fixture.document
                    hold = fixture.hold and index == 0
                if hold:
                    fixture.entered.set()
                    if not fixture.release.wait(20):
                        record["hold_timeout"] = True
                        status = 504
                    record["released_at"] = time.monotonic()
                body = json.dumps(document, ensure_ascii=False).encode()
                try:
                    self.send_response(status)
                    self.send_header("Content-Type", "application/json")
                    self.send_header("Content-Length", str(len(body)))
                    self.end_headers()
                    self.wfile.write(body)
                except (BrokenPipeError, ConnectionResetError):
                    pass

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.server.daemon_threads = True
        self.server.timeout = 0.2
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.url = f"http://127.0.0.1:{self.server.server_port}/dav"

    def configure(self, *, statuses=(), document=None, hold=False):
        with self.lock:
            self.statuses = list(statuses)
            if document is not None:
                self.document = document
            self.hold = hold
            self.entered.clear()
            self.release.clear() if hold else self.release.set()

    def close(self):
        self.release.set()
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(2)
