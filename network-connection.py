"""HTTP bridge between Cheng's computer and Zoe's inbox and outbox.

POST /upload_params      Body is a parameter file, named by the X-Filename header.
GET  /list_outbox        JSON list of telemetry files.
GET  /download/<name>    One telemetry file.
GET  /status             JSON summary of the sweeps, and whether locking is running.

README.md is the contract. On Zoe's computer the laser locking app serves these
same endpoints while Chin Sweep is started (locker/chin_bridge.py in laser_lock),
so keep the two in step. This script is the standalone version: it moves files
but runs no sweeps, and it cannot share port 5000 with the app.

Uploads land atomically, so a reader never sees a half-written file. Sending
test_params.txt again replaces the current one. Only names in the contract are
accepted, so a request cannot reach outside the inbox or outbox.
"""

import datetime
import http.server
import json
import os
import re
import tempfile
import time
import urllib.parse

PORT = 5000
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
INBOX_DIR = os.path.join(BASE_DIR, "inbox")
OUTBOX_DIR = os.path.join(BASE_DIR, "outbox")

PARAM_FILE_RE = re.compile(r"^params_[\w.-]*\.txt$")
TEST_PARAMS_NAME = "test_params.txt"
TELEMETRY_FILE_RE = re.compile(r"^telemetry_[\w.-]*\.npy$")

# Largest parameter file accepted, in bytes. Real ones are a few hundred.
MAX_UPLOAD_BYTES = 64 * 1024

# This script cannot see the locking app, and runs no sweeps of its own.
STANDALONE_STATUS = {
    "state": "OFF",
    "running": False,
    "locker_running": False,
    "message": "Standalone bridge: files move, but no sweeps run",
}

# Longest to keep trying to replace a file that is in use, in seconds.
REPLACE_TIMEOUT = 1.0


def replace_file(source, destination):
    """Move source over destination, waiting out a reader that has it open.

    Windows refuses to replace an open file, and test_params.txt is read each
    time it changes, so an overwrite can land on a reader that is still closing.
    """
    deadline = time.monotonic() + REPLACE_TIMEOUT
    while True:
        try:
            os.replace(source, destination)
            return
        except PermissionError:
            if time.monotonic() >= deadline:
                raise
            time.sleep(0.01)


class BridgeServer(http.server.ThreadingHTTPServer):
    """HTTP server that fails loudly when the port is already taken."""

    # HTTPServer turns this on, and on Windows SO_REUSEADDR lets a second server
    # bind the same port silently, leaving requests going to either one.
    allow_reuse_address = False
    daemon_threads = True


class BridgeHandler(http.server.BaseHTTPRequestHandler):
    """Serves the three bridge endpoints."""

    def _reply(self, status, body=b"", content_type=None):
        """Send a complete response."""
        self.send_response(status)
        if content_type:
            self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self):
        """Accept a parameter file into the inbox."""
        if self.path != "/upload_params":
            self._reply(404)
            return
        try:
            length = int(self.headers.get("Content-Length", 0))
        except ValueError:
            self._reply(400, b"Bad Content-Length")
            return
        if length > MAX_UPLOAD_BYTES:
            self._reply(413, b"Parameter file too large")
            return
        default = f"params_{datetime.datetime.now():%Y%m%d_%H%M%S_%f}.txt"
        name = self.headers.get("X-Filename", default)
        if name != TEST_PARAMS_NAME and not PARAM_FILE_RE.match(name):
            self._reply(400, b"Bad filename")
            return
        body = self.rfile.read(length)
        handle, temp_path = tempfile.mkstemp(dir=INBOX_DIR, prefix=".upload_")
        try:
            with os.fdopen(handle, "wb") as file:
                file.write(body)
            replace_file(temp_path, os.path.join(INBOX_DIR, name))
        except OSError as exc:
            try:
                os.unlink(temp_path)
            except OSError:
                pass
            self._reply(500, repr(exc).encode("utf-8"))
            return
        print(f"[Bridge] Received parameters: {name}")
        self._reply(200, b"OK")

    def do_GET(self):
        """List the outbox, send one file from it, or report the status."""
        path = urllib.parse.urlparse(self.path).path
        if path == "/status":
            body = json.dumps(STANDALONE_STATUS).encode("utf-8")
            self._reply(200, body, "application/json")
        elif path == "/list_outbox":
            names = sorted(
                name for name in os.listdir(OUTBOX_DIR) if TELEMETRY_FILE_RE.match(name)
            )
            self._reply(200, json.dumps(names).encode("utf-8"), "application/json")
        elif path.startswith("/download/"):
            name = urllib.parse.unquote(path[len("/download/"):])
            if not TELEMETRY_FILE_RE.match(name):
                self._reply(404)
                return
            try:
                # Read whole and close before sending: the TEST file is replaced
                # in place, which Windows refuses while it is open here.
                with open(os.path.join(OUTBOX_DIR, name), "rb") as file:
                    body = file.read()
            except OSError:
                self._reply(404)
                return
            self._reply(200, body, "application/octet-stream")
        else:
            self._reply(404)

    def log_message(self, format, *args):
        # Suppress noisy default logging, print clean custom status
        print(f"[HTTP] {self.client_address[0]} - {format % args}")


if __name__ == "__main__":
    os.makedirs(INBOX_DIR, exist_ok=True)
    os.makedirs(OUTBOX_DIR, exist_ok=True)
    with BridgeServer(("", PORT), BridgeHandler) as httpd:
        print(f"=== HTTP Bridge Server Active on port {PORT} ===")
        print(f"Inbox: {INBOX_DIR}")
        print(f"Outbox: {OUTBOX_DIR}")
        httpd.serve_forever()
