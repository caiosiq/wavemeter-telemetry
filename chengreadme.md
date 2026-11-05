# Sending Test Parameters: Guide for the Cheng Lab

Use this guide to try laser lock parameters from your computer. You send a `test_params.txt` file to Zoe's Computer, which overwrites the current one. The laser repeats that sweep until you send another file, and you download the result as `telemetry_test.npy`.

The full contract is in [README.md](README.md).

> **`network-connection.py` in this repository is a simulator.** It is not connected to any laser, and all of its telemetry is fake. To reach the real laser, you connect to Zoe's Computer on port `5000`.

---

## Before You Start

1. **Get the address of Zoe's Computer from the Yan lab.** It replaces `<zoe-computer>` below. Your computer must be on a network that can reach it on port `5000`.
2. **Ask the Yan lab to set up the app.** Chin Sweep must be started with the Test checkbox ticked, and locking must be running. Nothing answers on port `5000` until Chin Sweep is started, and the checkbox chooses the mode, not your file. If locking is stopped, uploads still return `200`, but no sweeps run and the telemetry does not change.
3. **Install the Python packages:** `pip install numpy requests`

---

## Practice With the Simulator

You can try everything in this guide on your own computer, without the Yan lab or the laser.

> **Simulated results are fake.** Use the simulator to test your code, and never use its numbers to choose real gains. The laser in it is a short made-up model, so gains that look best here may not be best on the real laser.

1. Start the simulator and leave it running:

   ```bash
   python network-connection.py
   ```

2. In the commands and script below, use `127.0.0.1:5050` in place of `<zoe-computer>:5000`.

3. To practice a failure, restart it with `--locking-stopped`. Uploads still return `200`, but `/status` reports `locker_running` as `false` and the telemetry does not change.

To practice Run Mode, use `--mode run`. A timer stands in for the hardware trigger. The other options are in Section 6 of [README.md](README.md).

**Telling the two apart:** the simulator's `/status` reply has `"simulated": true`, and its `message` starts with `SIMULATOR`. Its telemetry has an extra `SIMULATED` column, and the example script below prints a warning. The real system has none of these.

Its files are kept in `simulator_data/`, away from any real data.

---

## Check the System First

Ask the system whether it is ready before you send anything:

```bash
curl http://<zoe-computer>:5000/status
```

A system that is ready replies with something like this, plus a few more counters:

```json
{
  "state": "TEST",
  "running": true,
  "mode": "TEST",
  "locker_running": true,
  "test_attempts": 1204,
  "written": 310,
  "write_errors": 0,
  "message": "TEST mode from test_params.txt"
}
```

| Field | Value you want | If not |
| :--- | :--- | :--- |
| `locker_running` | `true` | Locking is stopped, so no sweeps will run. Ask the Yan lab to start it. |
| `mode` | `TEST` | The Test checkbox is not ticked. |
| `test_attempts` | Rising between checks | Sweeps are not running. |
| `message` | No problem reported | It explains problems such as why `test_params.txt` was ignored. |

Three things to keep in mind:

* Use `locker_running` to tell whether locking is on. `state` can still say `TEST` while locking is stopped.
* `locker_running` shows that the locking loop is running, not that the laser is on frequency. Use the telemetry for that.
* `message` keeps its last value, so it can be an older note. After an upload, check that it does not say your file was ignored.

If the connection is refused, Chin Sweep is not started or your network cannot reach Zoe's Computer. The full list of fields is in [README.md](README.md).

---

## What You Send

Send one plain text file with these lines:

```text
MODE = TEST
TARGET_ID = trial_001
START_FREQ_THZ = 351.730000
TARGET_STOP_FREQ_THZ = 351.740000
SWEEP_RATE_MHZ_PER_MS = 2
KP = -200
KI = -2000
KD = 0
DIGITAL_CHANNEL = 0
RAMP_DELAY_MS = 20
```

| Key | What to send |
| :--- | :--- |
| `MODE` | `TEST`. |
| `TARGET_ID` | A new label for each trial, containing at least one letter, such as `trial_001`. It is copied into the telemetry, so it tells you which upload a result came from. |
| `KP`, `KI`, `KD` | The gains you want to try, in V/THz. They use the same sign as the laser's own gains, which is negative in the example. They apply only during the sweep. |
| `START_FREQ_THZ` | Where the sweep starts, in THz. The laser's target moves here when the file is picked up and stays here afterwards, so agree any change with the Yan lab. |
| `TARGET_STOP_FREQ_THZ` | Where the ramp ends, in THz. It can be above or below the start. |
| `SWEEP_RATE_MHZ_PER_MS` | Ramp speed. Must be positive. |
| `RAMP_DELAY_MS` | How long to hold the start frequency before ramping. Must not be negative. |
| `DIGITAL_CHANNEL` | Required, but not used in Test Mode. Leave it at `0`. |

### How much of the sweep runs

Each attempt ends at the end of the laser's dwell time, which is 50 ms by default and never more than 200 ms. The example sweep covers 10 GHz and would take over 5 seconds, so only the first part of it runs: 20 ms at the start frequency, then 30 ms of ramp, or about 60 MHz.

To see more of the ramp or the stop frequency, shorten the sweep, raise the rate, or ask the Yan lab whether the dwell time can be longer.

---

## How You Send It

Send an HTTP `POST` to `/upload_params`, with the file as the body and `test_params.txt` in the `X-Filename` header:

```bash
curl -X POST http://<zoe-computer>:5000/upload_params \
     -H "X-Filename: test_params.txt" \
     --data-binary @test_params.txt
```

You should get `200` and `OK`. The name must be exactly `test_params.txt`, or the file will join the Run Mode queue instead of overwriting the test file. There is no need to delete or restart anything.

A `200` only means the file was stored. It does not mean the file was valid, so always check that your `TARGET_ID` comes back in the telemetry.

---

## How You Get the Result

Download the telemetry from `/download/telemetry_test.npy`:

```bash
curl -o telemetry_test.npy http://<zoe-computer>:5000/download/telemetry_test.npy
```

This file is overwritten with the latest attempt, at most once per second, so it holds one attempt and not a history. Expect a new upload to appear in about a second. Save a copy of each one you want to keep.

It is a NumPy structured array with one record per sample:

| Column | Meaning |
| :--- | :--- |
| `Timestamp_ms` | Time since the start of the attempt, in ms. |
| `Trigger_Status` | `TEST`, or `NONE` if the attempt produced no samples. |
| `Wavemeter_Freq_THz` | Wavemeter reading, in THz. NaN if there was no usable reading. |
| `PID_Error` | Setpoint minus reading, in THz. NaN if there was no usable reading. |
| `DAC_Output_V` | Output voltage, in V. |

Every line of your file is also included as its own column, repeated on each row. For example, `data["KP"][0]` is the `KP` that produced the file.

### Exclude the first samples

The wavemeter is shared between lasers, so the first few milliseconds of an attempt can be light from another laser while the galvo moves. Those samples show a reading far from the target, with a `PID_Error` of tens of THz, and they did not drive the laser. Remove them before calculating anything, or they will swamp the result. In one attempt, 7 of 37 samples were affected, which made the RMS error about 16,000,000 MHz instead of 31 MHz.

---

## Example: Compare Several Sets of Gains

This script tries each set of gains, waits for the telemetry from that exact upload, saves it, and prints the RMS error of the samples from this laser. Set `BRIDGE` to the address of Zoe's Computer, or to `http://127.0.0.1:5050` for the simulator, and change `GAINS` to the values you want to try.

```python
import io
import time

import numpy as np
import requests

BRIDGE = "http://<zoe-computer>:5000"

# Samples further than this from the target are another laser, in THz.
MAX_ERROR_THZ = 0.001

# (KP, KI, KD) to try.
GAINS = [
    (-200, -2000, 0),
    (-200, -4000, 0),
    (-400, -2000, 0),
]

TEMPLATE = """MODE = TEST
TARGET_ID = {trial}
START_FREQ_THZ = 351.730000
TARGET_STOP_FREQ_THZ = 351.740000
SWEEP_RATE_MHZ_PER_MS = 2
KP = {kp}
KI = {ki}
KD = {kd}
DIGITAL_CHANNEL = 0
RAMP_DELAY_MS = 20
"""


def check_ready():
    """Stop with a clear reason if sweeps are not running."""
    reply = requests.get(f"{BRIDGE}/status", timeout=10)
    reply.raise_for_status()
    status = reply.json()
    if status.get("simulated"):
        print("SIMULATOR: this is not the real laser, and these results are fake.")
    if not status.get("locker_running"):
        raise RuntimeError("Locking is stopped. Ask the Yan lab to start it.")
    if status.get("mode") != "TEST":
        raise RuntimeError("The Test checkbox is not ticked.")


def send(text):
    """Overwrite test_params.txt, retrying if the file is briefly in use."""
    for _ in range(5):
        reply = requests.post(
            f"{BRIDGE}/upload_params",
            headers={"X-Filename": "test_params.txt"},
            data=text.encode("utf-8"),
            timeout=10,
        )
        if reply.status_code != 500:
            break
        time.sleep(0.2)
    reply.raise_for_status()


def wait_for_telemetry(trial, timeout=20):
    """Return the telemetry produced by this trial's upload."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        reply = requests.get(f"{BRIDGE}/download/telemetry_test.npy", timeout=10)
        if reply.status_code == 200:
            data = np.load(io.BytesIO(reply.content))
            if "TARGET_ID" in data.dtype.names and data["TARGET_ID"][0] == trial:
                return data
        time.sleep(0.5)
    raise TimeoutError(f"No telemetry for {trial}. See Troubleshooting.")


check_ready()
run = time.strftime("%Y%m%d_%H%M%S")
for number, (kp, ki, kd) in enumerate(GAINS, start=1):
    trial = f"trial_{run}_{number:03d}"
    send(TEMPLATE.format(trial=trial, kp=kp, ki=ki, kd=kd))
    data = wait_for_telemetry(trial)
    np.save(f"{trial}.npy", data)
    error = data["PID_Error"][np.abs(data["PID_Error"]) < MAX_ERROR_THZ]
    rms_mhz = np.sqrt(np.mean(error**2)) * 1e6 if error.size else float("nan")
    print(
        f"{trial}: KP={kp} KI={ki} KD={kd} "
        f"RMS error {rms_mhz:.3f} MHz from {error.size}/{len(data)} samples"
    )
```

---

## Troubleshooting

| What you see | What it means | What to do |
| :--- | :--- | :--- |
| Connection refused or timed out | Chin Sweep is not started, or your network cannot reach Zoe's Computer. | Ask the Yan lab to check. |
| `/status` shows `"simulated": true`, or the script prints a `SIMULATOR` warning | You are connected to the simulator, so the results are fake. | Set `BRIDGE` to Zoe's Computer for real results. |
| `404` on `/status` | The app on Zoe's Computer has not been restarted since `/status` was added. | Ask the Yan lab to restart it. |
| `400 Bad filename` | The `X-Filename` header is wrong. | Use exactly `test_params.txt`. |
| `500` | The file was in use for a moment. | Send it again. The example script does this for you. |
| `404` on download | No telemetry file exists yet. | Ask the Yan lab to check that the Test checkbox is ticked and locking is running. |
| Upload returns `200`, but the telemetry never changes | Locking is stopped, or the Test checkbox is not ticked. | Check `/status`. `locker_running` should be `true`, `mode` should be `TEST`, and `test_attempts` should keep rising. |
| Your `TARGET_ID` never appears, but the telemetry keeps updating | Your file is invalid, so the previous sweep is still running. | Check `message` in `/status` for the reason. |
| RMS error is enormous | Samples from another laser are included. | Exclude them as described above. |
| All `NaN` in the telemetry | The wavemeter gave no usable readings during that attempt. | Ask the Yan lab to check the wavemeter. |

---

## Please Avoid

* Do not send `params_*.txt` files during these tests. They are for Run Mode and only queue up until the trigger experiment.
* The bridge has no login, so keep the address within the two labs.
