"""SIMULATOR of the Yan lab bridge. It is not connected to any laser.

Everything this script produces is FAKE, including its telemetry. Use it to
practice and to test client code. Never use its numbers to choose real gains.

The real system is the laser locking app on Zoe's computer, on port 5000. This
script copies its behavior so the Cheng lab can run the whole workflow without it:

POST /upload_params      Body is a parameter file, named by the X-Filename header.
GET  /list_outbox        JSON list of telemetry files.
GET  /download/<name>    One telemetry file.
GET  /status             JSON summary, with "simulated": true.

Copied from the real system: the endpoints, the filename rules, the parameter
checks, the telemetry format, re-reading test_params.txt, the 200 ms limit, the
samples of another laser at the start of each attempt, and no sweeps while
locking is stopped. Run Mode's hardware trigger is replaced by a timer.

Not copied: the laser. A short made-up model turns gains into plausible numbers,
so better gains here need not be better on the real laser.

To keep fake data apart from real data, the simulator:
  - uses only the simulator_data folder, never the real inbox or outbox
  - listens on port 5050 and only on this computer, unless told otherwise
  - marks each telemetry file with a SIMULATED column, each /status reply with
    "simulated": true, and each HTTP response with an X-Simulator header

Run with: python network-connection.py
Options:  python network-connection.py --help
Needs:    pip install numpy
"""

import argparse
import datetime
import http.server
import json
import os
import random
import re
import socket
import tempfile
import threading
import time
import urllib.parse

import numpy as np

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.path.join(BASE_DIR, "simulator_data")
INBOX_DIR = os.path.join(DATA_DIR, "inbox")
OUTBOX_DIR = os.path.join(DATA_DIR, "outbox")
ARCHIVE_DIR = os.path.join(DATA_DIR, "archive")

# Not the real system's port 5000, which is also often taken on a Mac.
DEFAULT_PORT = 5050
DEFAULT_HOST = "127.0.0.1"

PARAM_FILE_RE = re.compile(r"^params_[\w.-]*\.txt$")
TEST_PARAMS_NAME = "test_params.txt"
TEST_TELEMETRY_NAME = "telemetry_test.npy"
TELEMETRY_FILE_RE = re.compile(r"^telemetry_[\w.-]*\.npy$")

# Largest parameter file accepted, in bytes. Real ones are a few hundred.
MAX_UPLOAD_BYTES = 64 * 1024

# Longest to keep trying to replace a file that is in use, in seconds.
REPLACE_TIMEOUT = 1.0

# Timing of the real system, in seconds.
WINDOW_TIMEOUT = 0.2  # Longest a sweep may run.
SCAN_INTERVAL = 0.05  # Between looks at the inbox.
ATTEMPT_INTERVAL = 0.1  # Between Test Mode attempts.
TEST_WRITE_INTERVAL = 1.0  # Shortest gap between rewrites of the test telemetry.

# Made-up laser model. Illustrative only: these are not measured values.
SAMPLE_INTERVAL = 0.0015  # Seconds between telemetry samples.
WAVEMETER_REFRESH_SAMPLES = 3  # Samples that repeat one wavemeter reading.
OTHER_LASER_TIME = 0.0075  # Seconds of another laser's light at the start.
OTHER_LASER_THZ = 389.2866
LASER_THZ_PER_V = -0.002
HOLD_VOLTAGE = -3.9
VOLTAGE_RANGE = 0.5  # Largest move from the hold voltage, in volts.
START_OFFSET_THZ = 0.000025  # Where the laser sits relative to the start.
NOISE_THZ = 0.0000005  # Wavemeter noise.
TRIGGER_HIGH_TIME = 0.06  # How long the simulated trigger stays high, in seconds.

TELEMETRY_COLUMNS = [
    ("Timestamp_ms", "f8"),
    ("Trigger_Status", "U4"),
    ("Wavemeter_Freq_THz", "f8"),
    ("PID_Error", "f8"),
    ("DAC_Output_V", "f8"),
]
SIMULATED_COLUMN = "SIMULATED"
REQUIRED_NUMBERS = (
    "START_FREQ_THZ",
    "TARGET_STOP_FREQ_THZ",
    "SWEEP_RATE_MHZ_PER_MS",
    "KP",
    "KI",
    "KD",
    "DIGITAL_CHANNEL",
    "RAMP_DELAY_MS",
)


def replace_file(source, destination):
    """Move source over destination, waiting out a reader that has it open."""
    deadline = time.monotonic() + REPLACE_TIMEOUT
    while True:
        try:
            os.replace(source, destination)
            return
        except PermissionError:
            if time.monotonic() >= deadline:
                raise
            time.sleep(0.01)


def parse_params(text):
    """Check a parameter file with the real system's rules.

    Returns:
        dict: "raw" holds every key as text, and the numbers are under their keys.

    Raises:
        ValueError: The file is unusable. The message names the offending key.
    """
    raw = {}
    for line in text.splitlines():
        if "=" in line:
            key, value = line.split("=", 1)
            raw[key.strip()] = value.strip()
    if raw.get("MODE", "") not in ("RUN", "TEST"):
        raise ValueError(f"MODE must be RUN or TEST, got {raw.get('MODE', '')!r}")
    params = {"raw": raw}
    for key in REQUIRED_NUMBERS:
        if key not in raw:
            raise ValueError(f"missing {key}")
        try:
            params[key] = float(raw[key])
        except ValueError:
            raise ValueError(f"{key} is not a number: {raw[key]!r}") from None
    if params["SWEEP_RATE_MHZ_PER_MS"] <= 0:
        raise ValueError("SWEEP_RATE_MHZ_PER_MS must be positive")
    if params["RAMP_DELAY_MS"] < 0:
        raise ValueError("RAMP_DELAY_MS must not be negative")
    channel = params["DIGITAL_CHANNEL"]
    if channel < 0 or channel != int(channel):
        raise ValueError(f"DIGITAL_CHANNEL must be a bit number, got {channel}")
    return params


def sweep_target(params, elapsed):
    """Return the setpoint in THz, a given number of seconds into a sweep."""
    delay = params["RAMP_DELAY_MS"] * 1e-3
    start = params["START_FREQ_THZ"]
    span = params["TARGET_STOP_FREQ_THZ"] - start
    ramped = (elapsed - delay) * params["SWEEP_RATE_MHZ_PER_MS"] * 1e-3
    return start + np.sign(span) * min(max(ramped, 0.0), abs(span))


def simulate_sweep(params, duration, trigger_status):
    """Make up the samples of one sweep from the illustrative laser model.

    Args:
        params (dict): The parsed parameter file.
        duration (float): Length of the sweep, in seconds.
        trigger_status (str): Trigger_Status of every sample but a Run Mode last.

    Returns:
        list of tuple: Rows matching the first five telemetry columns.
    """
    kp, ki, kd = params["KP"], params["KI"], params["KD"]
    resting = params["START_FREQ_THZ"] + START_OFFSET_THZ
    frequency = resting
    voltage = HOLD_VOLTAGE
    integral = 0.0
    last_error = 0.0
    reading = OTHER_LASER_THZ
    error = None
    update_interval = SAMPLE_INTERVAL * WAVEMETER_REFRESH_SAMPLES
    rows = []
    for index in range(int(duration / SAMPLE_INTERVAL) + 1):
        elapsed = index * SAMPLE_INTERVAL
        target = sweep_target(params, elapsed)
        if elapsed < OTHER_LASER_TIME:
            # The servo refuses these readings, so the laser does not move.
            reading = OTHER_LASER_THZ + random.gauss(0, NOISE_THZ)
        elif error is None or index % WAVEMETER_REFRESH_SAMPLES == 0:
            reading = frequency + random.gauss(0, NOISE_THZ)
            error = target - reading
            integral += ki * error * update_interval
            derivative = (error - last_error) / update_interval
            last_error = error
            voltage = HOLD_VOLTAGE + float(
                np.clip(kp * error + integral + kd * derivative, -VOLTAGE_RANGE, VOLTAGE_RANGE)
            )
            frequency = resting + LASER_THZ_PER_V * (voltage - HOLD_VOLTAGE)
        rows.append((elapsed * 1e3, trigger_status, reading, target - reading, voltage))
    return rows


def build_telemetry(rows, params):
    """Assemble a telemetry array in the real format, marked as simulated.

    Returns:
        np.ndarray: The real columns, then one field per parameter, then SIMULATED.
    """
    reserved = {name for name, _ in TELEMETRY_COLUMNS} | {SIMULATED_COLUMN}
    extras = []
    raw = {key: value for key, value in params["raw"].items() if key not in reserved}
    for key, value in raw.items():
        try:
            float(value)
            extras.append((key, "f8"))
        except ValueError:
            extras.append((key, f"U{max(len(value), 1)}"))
    base = np.array(rows, dtype=TELEMETRY_COLUMNS)
    data = np.empty(len(base), dtype=TELEMETRY_COLUMNS + extras + [(SIMULATED_COLUMN, "U4")])
    for name, _ in TELEMETRY_COLUMNS:
        data[name] = base[name]
    for key, _ in extras:
        data[key] = raw[key]
    data[SIMULATED_COLUMN] = "TRUE"
    return data


def save_telemetry(path, data):
    """Write a telemetry file so a reader never sees a partial one."""
    handle, temp_path = tempfile.mkstemp(dir=OUTBOX_DIR, prefix=".telemetry_", suffix=".tmp")
    try:
        with os.fdopen(handle, "wb") as file:
            np.save(file, data)
        replace_file(temp_path, path)
    except BaseException:
        try:
            os.unlink(temp_path)
        except OSError:
            pass
        raise


class SimulatedSystem:
    """Stands in for the Chin Sweep monitor and the locking loop.

    Args:
        mode (str): "TEST" or "RUN", as the real app's Test checkbox would set it.
        locking (bool): False to behave as if locking were stopped.
        dwell (float): Length of a Test Mode attempt, in seconds.
        trigger_period (float): Seconds between simulated Run Mode triggers.
    """

    def __init__(self, mode, locking, dwell, trigger_period):
        self.mode = mode
        self.locking = locking
        self.dwell = min(dwell, WINDOW_TIMEOUT)
        self.trigger_period = trigger_period
        self.test_params = None
        self.test_mtime = None
        self.next_job = None
        self.last_write = 0.0
        self.triggers = 0
        self.missed = 0
        self.rejected = 0
        self.test_attempts = 0
        self.written = 0
        self.write_errors = 0
        self.read_errors = 0
        self.last_telemetry = None
        self.last_samples = 0
        self.message = ""

    def status(self):
        """Return the same fields as the real /status, plus "simulated"."""
        queued = sum(1 for name in os.listdir(INBOX_DIR) if PARAM_FILE_RE.match(name))
        if self.mode == "TEST":
            state = "TEST"
        elif self.next_job is not None and self.locking:
            state = "ARMED"
        else:
            state = "LOW"
        return {
            "simulated": True,
            "state": state,
            "running": True,
            "mode": self.mode,
            "queued": queued,
            "triggers": self.triggers,
            "missed": self.missed,
            "rejected": self.rejected,
            "test_attempts": self.test_attempts,
            "written": self.written,
            "write_errors": self.write_errors,
            "read_errors": self.read_errors,
            "last_telemetry": self.last_telemetry,
            "last_samples": self.last_samples,
            "message": f"SIMULATOR, not a real laser. {self.message}".strip(),
            "locker_running": self.locking,
        }

    def run(self):
        """Run the system until the program exits."""
        next_attempt = 0.0
        next_trigger = time.monotonic() + self.trigger_period
        while True:
            try:
                now = time.monotonic()
                if self.mode == "TEST":
                    self.check_test_params()
                    if self.locking and self.test_params and now >= next_attempt:
                        next_attempt = now + ATTEMPT_INTERVAL
                        self.attempt_test()
                else:
                    self.scan_inbox()
                    if now >= next_trigger:
                        next_trigger = now + self.trigger_period
                        self.on_trigger()
            except OSError as exc:
                self.read_errors += 1
                self.message = f"Could not read the inbox: {exc!r}"
            time.sleep(SCAN_INTERVAL)

    def check_test_params(self):
        """Start the sweep in test_params.txt, or follow an edit to it."""
        path = os.path.join(INBOX_DIR, TEST_PARAMS_NAME)
        try:
            mtime = os.path.getmtime(path)
        except OSError:
            self.message = f"TEST mode: no {TEST_PARAMS_NAME} in the inbox"
            return
        if mtime == self.test_mtime:
            return
        self.test_mtime = mtime
        with open(path, "r", encoding="utf-8", errors="replace") as file:
            text = file.read()
        if not text.strip():
            self.message = f"TEST mode: {TEST_PARAMS_NAME} is empty"
            return
        try:
            self.test_params = parse_params(text)
        except ValueError as exc:
            self.message = f"{TEST_PARAMS_NAME} ignored: {exc}"
            return
        self.message = f"TEST mode from {TEST_PARAMS_NAME}"

    def attempt_test(self):
        """Run one Test Mode attempt, and rewrite the telemetry at most once a second."""
        rows = simulate_sweep(self.test_params, self.dwell, "TEST")
        self.test_attempts += 1
        now = time.monotonic()
        if now - self.last_write >= TEST_WRITE_INTERVAL:
            self.last_write = now
            self.write_telemetry(TEST_TELEMETRY_NAME, rows, self.test_params)

    def scan_inbox(self):
        """Load the oldest usable parameter file, setting aside unusable ones."""
        names = sorted(name for name in os.listdir(INBOX_DIR) if PARAM_FILE_RE.match(name))
        for name in names:
            if self.next_job is not None and self.next_job[0] == name:
                return
            path = os.path.join(INBOX_DIR, name)
            with open(path, "r", encoding="utf-8", errors="replace") as file:
                text = file.read()
            if not text.strip():
                continue
            try:
                self.next_job = (name, parse_params(text))
                return
            except ValueError as exc:
                self.rejected += 1
                self.message = f"Rejected {name}: {exc}"
                replace_file(path, os.path.join(ARCHIVE_DIR, name + ".rejected"))
        self.next_job = None

    def on_trigger(self):
        """Act on a simulated rising edge, in place of the hardware trigger."""
        self.triggers += 1
        if self.next_job is None:
            self.missed += 1
            self.message = "Trigger arrived with no parameter file queued"
            return
        if not self.locking:
            self.missed += 1
            self.message = "Trigger arrived with locking stopped"
            return
        name, params = self.next_job
        self.next_job = None
        moment = datetime.datetime.now()
        rows = simulate_sweep(params, min(TRIGGER_HIGH_TIME, WINDOW_TIMEOUT), "HIGH")
        rows[-1] = (rows[-1][0], "LOW") + rows[-1][2:]
        stamp = f"{moment:%Y%m%d_%H%M%S}_{moment.microsecond // 1000:03d}"
        if self.write_telemetry(f"telemetry_{stamp}.npy", rows, params):
            replace_file(os.path.join(INBOX_DIR, name), os.path.join(ARCHIVE_DIR, name))

    def write_telemetry(self, name, rows, params):
        """Write one telemetry file. Returns True if it was written."""
        try:
            save_telemetry(os.path.join(OUTBOX_DIR, name), build_telemetry(rows, params))
        except OSError as exc:
            self.write_errors += 1
            self.message = f"Could not write {name}: {exc!r}"
            return False
        self.written += 1
        self.last_telemetry = name
        self.last_samples = len(rows)
        return True


class SimulatorServer(http.server.ThreadingHTTPServer):
    """HTTP server that fails loudly when the port is already taken."""

    # HTTPServer turns this on, and on Windows SO_REUSEADDR lets a second server
    # bind the same port silently, leaving requests going to either one.
    allow_reuse_address = False
    daemon_threads = True


class BridgeHandler(http.server.BaseHTTPRequestHandler):
    """Serves the same endpoints as the real bridge."""

    system = None  # The SimulatedSystem, set at startup.

    def _reply(self, status, body=b"", content_type=None):
        """Send a complete response."""
        self.send_response(status)
        self.send_header("X-Simulator", "true")
        if content_type:
            self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self):
        """Accept a parameter file into the simulator's inbox."""
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
        print(f"[Simulator] Received parameters: {name}")
        self._reply(200, b"OK")

    def do_GET(self):
        """List the outbox, send one file from it, or report the status."""
        path = urllib.parse.urlparse(self.path).path
        if path == "/status":
            body = json.dumps(self.system.status()).encode("utf-8")
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
        print(f"[Simulator HTTP] {self.client_address[0]} - {format % args}")


def port_in_use(port):
    """Check whether something on this computer already answers on a port.

    Binding alone is not enough. Windows lets 127.0.0.1 bind a port that another
    server holds on all interfaces, which could hide the real bridge from local
    clients.
    """
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=0.5):
            return True
    except OSError:
        return False


def main():
    """Parse the options, then start the simulated system and its server."""
    parser = argparse.ArgumentParser(
        description="SIMULATOR of the Yan lab bridge. Not connected to any laser."
    )
    parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    parser.add_argument(
        "--host",
        default=DEFAULT_HOST,
        help="address to listen on; the default is reachable only from this computer",
    )
    parser.add_argument(
        "--mode",
        choices=["test", "run"],
        default="test",
        help="stands in for the Test checkbox in the real app",
    )
    parser.add_argument(
        "--locking-stopped",
        action="store_true",
        help="behave as if locking were stopped, to practice that failure",
    )
    parser.add_argument(
        "--dwell-ms",
        type=float,
        default=50,
        help="length of a Test Mode attempt, limited to 200 ms",
    )
    parser.add_argument(
        "--trigger-period",
        type=float,
        default=1.0,
        help="seconds between simulated Run Mode triggers",
    )
    args = parser.parse_args()

    if port_in_use(args.port):
        raise SystemExit(
            f"Port {args.port} is already in use, so the simulator did not start. "
            "It may be the real system. Choose another port with --port."
        )
    for folder in (INBOX_DIR, OUTBOX_DIR, ARCHIVE_DIR):
        os.makedirs(folder, exist_ok=True)
    system = SimulatedSystem(
        mode=args.mode.upper(),
        locking=not args.locking_stopped,
        dwell=args.dwell_ms * 1e-3,
        trigger_period=args.trigger_period,
    )
    BridgeHandler.system = system
    with SimulatorServer((args.host, args.port), BridgeHandler) as httpd:
        threading.Thread(target=system.run, daemon=True).start()
        print("=" * 66)
        print("  SIMULATOR: not connected to any laser. All telemetry is FAKE.")
        print("=" * 66)
        print(f"Address: http://{args.host}:{args.port}")
        print(f"Mode: {system.mode}   Locking: {'running' if system.locking else 'stopped'}")
        print(f"Data folder: {DATA_DIR}")
        httpd.serve_forever()


if __name__ == "__main__":
    main()
