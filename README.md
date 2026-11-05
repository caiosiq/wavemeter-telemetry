# Inter-Lab Spectroscopy Bridge: Cheng's Computer to Zoe's Computer Integration Contract

This document defines the communication protocol, file formats, and execution modes between Cheng's Computer (Mac / New A2) and Zoe's Computer (Windows 11 Wavemeter & PID Controller).

For the steps to send test parameters from Cheng's Computer, see [chengreadme.md](chengreadme.md).

Changes to this contract should be agreed by both labs and made here, in the same change as any update to `network-connection.py`.

---

## 1. System Overview & Transport Layer

* **Protocol:** HTTP on port `5000` of Zoe's Computer.
* **Availability:** On Zoe's Computer, the laser locking app serves these endpoints only while its Chin Sweep feature is started. `network-connection.py` is a standalone server with the same endpoints. It moves files but runs no sweeps, and it cannot run while the app holds the port.
* **Access:** The server listens on all network interfaces and has no authentication, so anyone who can reach the port can queue a sweep. Keep it on a trusted network.

### Endpoints

| Endpoint | Purpose | Responses |
| :--- | :--- | :--- |
| `POST /upload_params` | Uploads a parameter file to the `inbox`. The body is the file contents and the `X-Filename` header is its name. | `200` stored, `400` bad filename or `Content-Length`, `413` over 64 KiB, `500` could not be stored (safe to retry) |
| `GET /list_outbox` | Returns a sorted JSON list of the telemetry files in the `outbox`. | `200` |
| `GET /download/<filename>` | Downloads one telemetry file as raw bytes. | `200`, or `404` if the file does not exist or the name is not a telemetry file |
| `GET /status` | Returns a JSON summary of the system, including whether locking is running. | `200`, or `500` if the status could not be read |

* **Accepted upload names:** `test_params.txt`, or `params_` followed by letters, digits, `_`, `.` or `-`, ending in `.txt`.
* **Same name:** An upload replaces any file already in the `inbox` with that name. The replacement is atomic, so a half-written file is never read. This is intended for `test_params.txt`. Give every Run Mode file a new name, because a queued `params_*.txt` file may already have been loaded, and replacing it would not change that sweep.
* **Missing `X-Filename`:** The file is stored under a generated `params_*.txt` name.
* **A `200` only means the file was stored.** Its contents are checked later, by the sweep (Section 3).

### Status

`GET /status` returns the values shown in the app's Chin Sweep window. If the connection is refused, Chin Sweep is not started or the computer cannot be reached.

```json
{
  "state": "TEST",
  "running": true,
  "mode": "TEST",
  "locker_running": true,
  "queued": 4,
  "triggers": 0,
  "missed": 0,
  "rejected": 0,
  "test_attempts": 1204,
  "written": 310,
  "write_errors": 0,
  "read_errors": 0,
  "last_telemetry": "telemetry_test.npy",
  "last_samples": 37,
  "message": "TEST mode from test_params.txt"
}
```

| Field | Meaning |
| :--- | :--- |
| `locker_running` | `true` while the locking loop is running. Sweeps run only when this is `true`. |
| `running` | `true` while Chin Sweep is watching the `inbox` and trigger. |
| `mode` | `TEST` or `RUN`, as set by the app's Test checkbox. |
| `state` | The trigger indicator from Section 2. |
| `queued` | Parameter files waiting in the `inbox`. |
| `triggers`, `missed` | Rising edges seen, and how many of those could not start a sweep. |
| `rejected` | Unusable parameter files moved to `archive/`. |
| `test_attempts` | Test sweeps attempted since Chin Sweep started. It should keep rising in Test Mode. |
| `written` | Telemetry files written since Chin Sweep started. |
| `write_errors`, `read_errors` | Failures writing telemetry, and failures in the inbox or trigger polling. |
| `last_telemetry`, `last_samples` | The newest telemetry file and its number of samples. |
| `message` | The latest message, such as why `test_params.txt` was ignored. |

`locker_running` does not show that the laser is on frequency. Use the telemetry for that. The standalone `network-connection.py` runs no sweeps, so it returns only `state`, `running`, `locker_running` and `message`, with `locker_running` always `false`.

---

## 2. Operating Modes

The **Test checkbox in the app on Zoe's Computer chooses the mode**, and it can be changed while running. The `MODE` line in a parameter file must be `RUN` or `TEST`, but it does not choose anything.

### A. Test Mode (checkbox ticked)
* **Purpose:** Tuning PID coefficients, checking laser locking stability, and evaluating free-running oscillations *before* the experimental hardware trigger loop is active.
* **Behavior:** The trigger is not used, but locking must be running: sweeps only run inside the locking loop, so while it is stopped, uploads are stored but nothing is swept and the telemetry does not change. The sweep in `test_params.txt` is attempted on every round-robin visit to the laser and cut off at the end of the dwell time. Queued `params_*.txt` files wait untouched.
* **Changing parameters:** Upload a new `test_params.txt`. It overwrites the current file with no restart. The new file is picked up within about 50 ms, and the sweep runs on the laser's next round-robin visit. The file stays in the `inbox` and is never archived.

  ```bash
  curl -X POST http://<zoe-computer>:5000/upload_params \
       -H "X-Filename: test_params.txt" \
       --data-binary @test_params.txt
  ```

  ```python
  import requests

  with open("test_params.txt", "rb") as file:
      requests.post(
          "http://<zoe-computer>:5000/upload_params",
          headers={"X-Filename": "test_params.txt"},
          data=file.read(),
      ).raise_for_status()
  ```

* **Invalid uploads:** The overwrite still happens, but the file is ignored and the previous sweep keeps running. The reason appears only in the app on Zoe's Computer. If `test_params.txt` is deleted, the last sweep also keeps running until new parameters arrive.
* **Telemetry:** The most recent attempt overwrites `telemetry_test.npy`, at most once per second.

### B. Run Mode (checkbox unticked)
* **Purpose:** The actual automated binary search experiment coupled with the main experimental loop.
* **Behavior:** Files wait in the `inbox`, oldest first by name. Each rising edge of the physical TTL trigger consumes the next one, and locking must be running.
* **Trigger sequence:**
  1. Hold `START_FREQ_THZ` for `RAMP_DELAY_MS`, counted from the rising edge.
  2. Ramp the PID target at `SWEEP_RATE_MHZ_PER_MS`, in either direction.
  3. Hold `TARGET_STOP_FREQ_THZ` until the trigger goes low, or for at most 200 ms from the rising edge.
  4. Write the telemetry file to the `outbox` and move the parameter file to `archive/`.
* **Rearming:** The timeout never re-fires. The trigger must be measured low before another rising edge is accepted, so a line that is already high when monitoring starts is not a trigger.

The laser's target moves to `START_FREQ_THZ` when a file is picked up, in either mode, and stays there after the sweep.

The app on Zoe's Computer shows the trigger status:

| Indicator | Meaning |
| :--- | :--- |
| `ARMED` | Trigger low, a file queued, and locking running |
| `HIGH` | Sweeping |
| `HIGH, NOT ARMED` | Trigger high, waiting for it to go low |
| `LOW` | Not ready for a trigger |
| `TEST` | Test mode |
| `OFF` | Chin Sweep not started |

---

## 3. Parameter File Format (Cheng's Computer -> Zoe's Computer Inbox)

* **File Naming Convention:** `params_[YYYYMMDD_HHMMSS_ms].txt`, or `test_params.txt` for Test Mode. Timestamped names keep the queue in order.
* **Lifecycle:** Created by Cheng's Computer -> Uploaded to the `inbox` -> In Run Mode, moved to `archive/` after execution. `test_params.txt` stays in the `inbox`.
* **Rejected files:** In Run Mode, an unusable file moves to `archive/` as `<name>.rejected` so it cannot block the queue.
* **Payload Structure:** One `KEY = value` per line.

```text
MODE = RUN
TARGET_ID = shot_042
START_FREQ_THZ = 384.000000
TARGET_STOP_FREQ_THZ = 384.120000
SWEEP_RATE_MHZ_PER_MS = 2.0
KP = 1.2
KI = 0.15
KD = 0.01
DIGITAL_CHANNEL = 0
RAMP_DELAY_MS = 20
```

| Key | Requirement |
| :--- | :--- |
| `MODE` | Required. Must be `RUN` or `TEST`. |
| `START_FREQ_THZ`, `TARGET_STOP_FREQ_THZ` | Required numbers, in THz. |
| `SWEEP_RATE_MHZ_PER_MS` | Required. Must be positive. |
| `KP`, `KI`, `KD` | Required numbers, used as given, in the same units (V/THz) and sign as the laser's own gains. |
| `DIGITAL_CHANNEL` | Required. Bit number of the digital port carrying the trigger. |
| `RAMP_DELAY_MS` | Required. Must not be negative. |
| Any other key, such as `TARGET_ID` | Optional. Copied into the telemetry. |

---

## 4. Telemetry File Format (Zoe's Computer Outbox -> Cheng's Computer)

* **File Naming Convention:** `telemetry_[YYYYMMDD_HHMMSS_ms].npy`, stamped with the time of the rising edge, or `telemetry_test.npy` in Test Mode.
* **Lifecycle:** Generated by Zoe's Computer after a trigger window ends -> Placed in the `outbox` -> Downloaded and processed by Cheng's Computer. Telemetry files are written atomically, and the bridge does not delete them.
* **Format:** A NumPy structured array, one record per sample, which loads without pickle:

```python
import numpy as np

data = np.load("telemetry_test.npy")
print(data.dtype.names)
print(data["Wavemeter_Freq_THz"])
```

| Column | Type | Meaning |
| :--- | :--- | :--- |
| `Timestamp_ms` | float | Time since the rising edge in Run Mode, or since the start of the attempt in Test Mode, in ms. |
| `Trigger_Status` | string | `HIGH` or `LOW` in Run Mode, `TEST` in Test Mode, `NONE` if there were no samples. |
| `Wavemeter_Freq_THz` | float | Wavemeter reading, in THz. |
| `PID_Error` | float | Setpoint minus reading, in THz. |
| `DAC_Output_V` | float | Output voltage, in V. |

The wavemeter is shared between lasers, so the first samples of a window can be another laser's light while the galvo moves. They appear with a `PID_Error` of tens of THz, do not drive the laser, and should be excluded from analysis.

These columns are followed by one field per key in the parameter file, repeated on every row, so each file records the parameters that produced it. A key with a numeric value is a float and any other is a string. A window with no samples yields a single row of NaN values, with `Trigger_Status` set to `NONE`, so the parameters still arrive.

---

## 5. Summary of Responsibilities

| Component | Responsibility |
| :--- | :--- |
| **Cheng's Computer (Mac)** | • Analyzes incoming telemetry.<br>• Computes binary search adjustments & PID updates.<br>• Uploads timestamped `params_*.txt` files in Run Mode, and overwrites `test_params.txt` in Test Mode. |
| **Zoe's Computer (Win 11)** | • Hosts the HTTP bridge on port `5000`.<br>• Monitors the `inbox` and archives processed parameter files.<br>• Listens for hardware TTL triggers in Run Mode, and repeats the `test_params.txt` sweep in Test Mode.<br>• Writes time-series telemetry `.npy` files into the `outbox`. |
