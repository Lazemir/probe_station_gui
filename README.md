# Probe Station GUI

`Probe Station GUI` is an operator-facing control interface for a probe station built around `FluidNC` and a microscope camera.

The application can:
- display the live camera image;
- move the stage manually and by click;
- run `homing`;
- raise and lower the needles through axis `A`;
- run `autofocus`;
- work with `GDS/OAS` layouts;
- align a chip using two points;
- move through the design minimap and the full design map;
- send direct controller commands through `Serial Terminal`;
- run repeated motion patterns from the `Oscillation` panel.

## Main Safety Rule

Any `XY` motion must happen only with the needles raised.

In practice this means:
- before any `click-to-move`, minimap move, full design map move, target move, `autofocus`, or any automated motion, make sure the needles are raised;
- if the UI shows `unknown` needle state, raise the needles again with `Raise` or run `homing A`;
- manual commands from `Serial Terminal`, especially anything touching axis `A` or a controller reset, can invalidate the needle state;
- if the needles are down or their state is unknown, the application must block automatic motion. Do not work around that manually.

## Requirements

Hardware:
- a camera compatible with `rotpy` / Spinnaker;
- a `FluidNC` controller connected over serial;
- for contact checks and route measurements: a supported measurement instrument
  (`GW Instek LCR-76200`, `Keithley 2400`, or `Keithley 2400 + 2182A`).

Core Python packages:
- `PySide6`
- `numpy`
- `opencv-python`
- `rotpy`
- `scipy`
- `gdstk`
- `pyqtgraph`
- `fastapi`
- `uvicorn`

Optional packages for LCR support:
- `pyvisa`
- `qcodes`

## Launch

```powershell
.\.venv\Scripts\python.exe main.py
```

If the virtual environment is already activated:

```powershell
python main.py
```

## First Startup

Recommended order:
1. Start the application and confirm that the camera image is live.
2. In the `Connection` panel, select the controller serial port and press `Connect`.
3. Wait for the startup synchronization to finish.
4. In the `Joystick` panel, home the required axes.
5. Raise the needles and confirm that the needle status shows `up`.
6. Test manual jogging.
7. Only then use `click-to-move`, `Design Window`, `Alignment`, `autofocus`, and `Oscillation`.

## UI Layout

Main window:
- the live microscope image is in the center;
- when a design is loaded, a minimap appears in the image corner;
- the bottom area contains the status bar and status history.

Menus:
- `Application`: settings and status log;
- `Tools`: `Connection`, `Joystick`, `Serial Terminal`, `Oscillation`;
- `Calibration`: `Design Window`, `Contact / Stone Calibration`, `Surface Map`, `Click-to-Move Calibration`, `Alignment`.

GUI rule:
- keep menus and primary controls laconic;
- do not put live measurements, matrices, diagnostics, or implementation detail in top-level menu item text;
- put detailed calibration numbers inside the relevant dialog, status panel, tooltip, or log entry;
- if a workflow needs more than one action, expose a compact entry point and keep the detailed controls behind it.

Calibration/automation rule:
- avoid per-objective magic constants in code, especially motion speeds;
- prefer values measured from live data, produced by calibration scripts, or stored in user-editable settings;
- keep unavoidable numeric guards small, named, and tied to algorithm safety or validation;
- when diagnosing behavior after a run, inspect both `status-history.log` and `probe-station-gui.log` before changing code.

Main panels:
- `Connection`
- `Joystick`
- `Serial Terminal`
- `Alignment`
- `Oscillation`

Separate window:
- `Design Window`

## `Connection` Panel

Purpose:
- scan serial ports;
- connect to and disconnect from `FluidNC`;
- connect to and disconnect from the configured measurement instrument;
- run startup synchronization after connect;
- attempt auto-connect.

How to use it:
1. Press `Refresh` if the port list is outdated.
2. Select the controller port.
3. Check the baud rate, usually `115200`.
4. Press `Connect`.

The same panel contains the `Instrument` block. Configure the instrument type
and resource in `Application` -> `Settings` -> `Measurement`, then press
`Connect Instrument`.
Choose `Keithley 2400` when only the source meter is connected, or
`Keithley 2400 + 2182A` when the nanovoltmeter is available.

After connection:
- the control panels become active;
- the application synchronizes controller state;
- if needed, it automatically raises the needles through `homing A`.

## `Joystick` Panel

This is the main manual control tool.

What it contains:
- manual jog for `X`, `Y`, `Z`;
- `B` axis control when available in the current UI;
- per-axis and `ALL` homing;
- `Raise` and `Lower` for the needles;
- `Unlock`;
- `Reset`;
- `Autofocus`;
- jog speed control.

Behavior:
- holding a button or key keeps the stage moving;
- releasing it stops motion;
- manual `B` motion invalidates design registration;
- click-to-move calibration lives under `Calibration` -> `Click-to-Move Calibration`.

Needles:
- `Raise` brings the system into a safe state for motion;
- `Lower` moves the needles to the saved lower position;
- double-click `Lower` saves the current A coordinate as the lower position;
- if the status is `Needles: unknown`, raise them again first.

## Motion By Camera Image

Clicking the microscope image moves the stage so that the selected point reaches the crosshair center.

How to use it:
1. Connect the controller.
2. Home `X` and `Y`.
3. Raise the needles.
4. Click the desired point in the image.
5. Wait for motion to complete.

Important:
- on first use, or after `Calibration` -> `Click-to-Move Calibration` -> `Reset`, the application rebuilds click calibration;
- if the predicted move is too large, the motion is cancelled and calibration is reset;
- this requires a live and stable camera image.

### Objective Profiles

Objectives can have independent click-to-move matrices and optical XY offsets.
The lowest-magnification objective is treated as the zero-offset reference.

To calibrate objective XY offset:
1. Select the base objective and center a recognizable chip feature.
2. Open `Calibration` -> `Click-to-Move Calibration`.
3. Press `Set Reference`.
4. Select another objective, center the same feature, then press `Save Offset`.

Design registration uses the base-objective optical center. When another
objective is active, design navigation applies that objective offset before
commanding XY motion. Needle calibration and saved needle heights remain
physical stage/A-axis values and are not per-objective.

## `Serial Terminal` Panel

This panel is used for direct controller commands.

Use it for:
- diagnostics;
- reading direct controller responses;
- one-off service commands;
- `Ctrl+X`.

Important:
- the terminal should show only replies to your manual commands;
- manual commands can invalidate the application's internal state;
- after manual `A` or `B` actions, controller reset, or manual coordinate-system changes, re-check needle state and re-sync if necessary.

## Design Workflow

### What `Design Window` Provides

`Design Window` is used for `GDS/OAS` workflows:
- loading a design file;
- selecting `Top cell`;
- enabling and disabling layers;
- viewing the full design map;
- selecting design points for registration;
- navigating measurement targets from a plan;
- `click-to-move` on the full design map after registration is complete.

### Loading A Design

1. Open `Calibration -> Design Window`.
2. Press `Load GDS...`.
3. Select the file.
4. Change `Top cell` if needed.
5. Configure visible layers.

The last opened design folder is saved automatically and reused next time.

### Minimap In The Main Window

When a design is loaded:
- a minimap appears in the image corner;
- it shows targets, registration points, and the current position;
- a single click on the minimap starts motion to the selected design point;
- a double click on the minimap opens `Design Window`;
- while motion is in progress, the minimap position is animated.

Moving by minimap requires completed design registration and raised needles.

### Full Design Map In `Design Window`

The full design map shows:
- the design itself;
- selected design points;
- current stage position;
- current field of view;
- a short green cross at the current position.

After registration is complete:
- left click on the full design map starts motion to the selected point;
- that motion is animated as well;
- the same needle safety checks apply as for any other automatic motion.

Before registration is complete:
- the full design map is used to select the two design calibration points.

### `Snap To Geometry`

`Design Window` has a `Snap To Geometry` checkbox.

When `Snap To Geometry` is enabled:
- clicks and hover snap to nearby design geometry;
- snap works only inside a reasonable radius around the cursor;
- that radius follows the current zoom level, similar to CAD behavior;
- if there is no nearby line or vertex, the click uses the exact cursor position.

When `Snap To Geometry` is disabled:
- clicks always use the exact cursor position with no snapping.

Automatic behavior:
- it turns on automatically when a design is loaded;
- it should stay on while building registration;
- after successful design alignment, the application turns it off automatically;
- the operator can still toggle it manually.

## `Alignment` Panel

The `Alignment` panel is used to align the chip using two points.

There are two workflows:
- alignment backed by a loaded design;
- quick alignment without a design.

What is on the panel:
- capture mode selector;
- `Set Point 1`;
- `Set Point 2`;
- `Reset Points`;
- `Cancel Pick`;
- `Open Design Window`.

Capture modes:
- `Crosshair center`: save the current image center;
- `Pick in image`: click a point in the camera image.

### Design-Backed Alignment

1. Load the design in `Design Window`.
2. Pick two design points on the full map:
   - left mouse button for `Point 1`;
   - right mouse button for `Point 2`.
3. Go to the `Alignment` panel.
4. Move the first real chip mark under the crosshair and capture `Set Point 1`.
5. Move the second real mark under the crosshair and capture `Set Point 2`.
6. The application calculates the required `B` rotation.
7. After motion completes, registration becomes valid.

After that you can:
- click on the minimap;
- click on the full design map in `Design Window`;
- create and run probe routes.

### Quick Alignment Without A Design

If the design is not loaded, or you do not want to locate design points:
1. Open `Alignment`.
2. choose two real points on the chip;
3. capture `Point 1` and `Point 2`;
4. the application computes the required `B` rotation.

This aligns the chip, but it does not create full design registration.

### When Registration Is Invalidated

Design registration must be considered invalid after:
- controller disconnect;
- `homing` of `X`, `Y`, `B`, or `ALL`;
- manual `B` motion;
- zeroing or manual changes of `B`;
- controller reset;
- any action after which the real stage position no longer matches the stored registration.

In those cases, repeat alignment.

## Probe Route Measurement

`Probe Route` can run an ordered route and write one numeric resistance value per CSV row.

Typical workflow:
1. Load a design in `Design Window`.
2. Complete design-backed alignment so registration is valid.
3. In `Probe Route`, create points manually or with `Array`.
4. Connect the stage controller and the measurement instrument.
5. Press `Run Route`, choose the CSV output path, select the instrument type, and enter the per-run measurement settings.
6. The runner raises needles, moves to each point, lowers needles, reads the instrument, raises needles again, and continues to the next point.

`Stop` requests a safe stop after the current route action. Completed points are already written to CSV.

## Contact Calibration and Instruments

The `Contact / Stone Calibration` window is used to save chip/stone focus
positions and the lower needle contact position. The external measurement
instrument is connected from the `Connection` panel.

Requirements:
- properly configured instrument address;
- installed `pyvisa` and `qcodes`;
- a supported instrument.

Typical workflow:
1. Open measurement settings.
2. Configure the instrument type and resource.
3. In `Connection`, press `Connect Instrument`.
4. Open `Contact / Stone Calibration`.
5. Lower the needles in small steps.
6. Watch the LCR reading and `Short/Open` state in `Connection`.
7. When you find the correct contact point, save the current position.
8. Raise the needles again.

Near contact, always use very small steps.

## `Oscillation` Panel

The `Oscillation` panel starts repeated motion:
- along `X`;
- along `Y`;
- in a spiral.

Use it only when you understand the trajectory and the available travel range.

Before starting:
- raise the needles;
- make sure there is enough free travel around the current point;
- verify amplitude and speed.

## Coordinates

In settings, you can choose whether the GUI works in:
- active `WCS` work coordinates;
- absolute machine coordinates.

At startup the application synchronizes with the controller and uses what `FluidNC` actually reports.

Operator notes:
- if you manually change `G54/G55/...` or coordinate-reporting settings, re-check the displayed coordinates and re-sync if needed;
- make sure the coordinates shown in the UI are the ones you intend to work in.

## HTTP And Python API

The GUI starts a local FastAPI server when the `API` settings enable `Start with app`.
The default address is:

```text
http://127.0.0.1:8765
```

Opening the base URL shows links to Swagger UI at `/docs` and ReDoc at `/redoc`.
You can change the host and port in `Application` -> `Settings` -> `API`, or with
`PROBE_STATION_API_HOST` and `PROBE_STATION_API_PORT`.

### API Keys

Create API keys in `Application` -> `Settings` -> `API` -> `API keys`.
Press `Create Key`; the full key is copied to the clipboard and shown only once.
The GUI stores only a hash plus a short display prefix and suffix.

Each key belongs to a user name and has its own permissions:
- `Stage read`: read stage status.
- `Stage write`: send generic stage moves.
- `Route read`: list contacts from the currently loaded route.
- `Route measure`: move to route contacts, operate needles for a route contact,
  check contact quality, run contact seek, configure the meter, access configured
  meter VISA roles, and run raw sweeps.

Default new-key permissions are `Stage read`, `Route read`, and `Route measure`.
`Stage write` is disabled by default. Generic stage moves currently accept all
stage axes exposed by the API, including `A`; use that permission only for code
that is allowed to move the stage directly.

Pass the key as a bearer token:

```powershell
$headers = @{ Authorization = "Bearer psk_your_key_here" }
```

The API also accepts `X-API-Key`, but `Authorization: Bearer` is preferred.

### HTTP Examples

Read the GUI-visible stage state:

```powershell
Invoke-RestMethod `
  -Uri http://127.0.0.1:8765/api/v1/stage/status `
  -Headers $headers
```

Move the stage by coordinates:

```powershell
Invoke-RestMethod -Method Post `
  -Uri http://127.0.0.1:8765/api/v1/stage/move `
  -Headers $headers `
  -ContentType application/json `
  -Body '{"coordinates": {"X": 12.5, "Y": 8.0}, "mode": "absolute", "feedrate": 120}'
```

Relative moves use `mode: "relative"`:

```json
{
  "coordinates": {
    "Z": -0.1
  },
  "mode": "relative"
}
```

The API uses the same coordinate basis that the GUI currently uses. If the GUI
is configured for machine coordinates, API targets are machine coordinates. If
the GUI is configured for work coordinates, API targets are work coordinates.

List the currently loaded probe-route contacts:

```powershell
Invoke-RestMethod `
  -Uri http://127.0.0.1:8765/api/v1/route/contacts `
  -Headers $headers
```

Move to contact 7 without lowering needles:

```powershell
Invoke-RestMethod -Method Post `
  -Uri http://127.0.0.1:8765/api/v1/route/contacts/7/move `
  -Headers $headers `
  -ContentType application/json `
  -Body '{}'
```

Lower or lift needles at a route contact:

```powershell
Invoke-RestMethod -Method Post `
  -Uri http://127.0.0.1:8765/api/v1/route/contacts/7/needles `
  -Headers $headers `
  -ContentType application/json `
  -Body '{"action": "lower"}'
```

Allowed needle actions are `lower`, `lift`, and `raise`.

Check contact quality at the current needle position:

```powershell
Invoke-RestMethod -Method Post `
  -Uri http://127.0.0.1:8765/api/v1/route/contacts/7/check `
  -Headers $headers `
  -ContentType application/json `
  -Body '{"check_sample_count": 10, "contact_settle_s": 0.2}'
```

Run contact seek from the current needle position:

```powershell
Invoke-RestMethod -Method Post `
  -Uri http://127.0.0.1:8765/api/v1/route/contacts/7/seek `
  -Headers $headers `
  -ContentType application/json `
  -Body '{"check_sample_count": 10, "contact_seek_range_mm": 0.003, "contact_seek_step_mm": 0.001}'
```

The HTTP API intentionally exposes these lower-level actions separately. It
does not have a high-level `prepare contact` endpoint. Use the Python client
or QCoDeS driver for that convenience workflow.

Run a raw Keithley voltage sweep on contact 7:

```powershell
Invoke-RestMethod -Method Post `
  -Uri http://127.0.0.1:8765/api/v1/measurements/raw-sweep `
  -Headers $headers `
  -ContentType application/json `
  -Body '{"contact_number": 7, "voltages_v": [-0.1, 0, 0.1], "meter": {"meter_type": "keithley", "nplc": 10, "ranges": {"mode": "code_auto", "expected_resistance_ohm": 100000, "max_current_a": 0.0005}}}'
```

The sweep response includes `timestamp_utc`, contact metadata, the requested
`voltages_v`, raw `iv_pairs`, and the full instrument result.

Start a GUI-owned route session for API-owned measurements:

```powershell
Invoke-RestMethod -Method Post `
  -Uri http://127.0.0.1:8765/api/v1/route/sessions `
  -Headers $headers `
  -ContentType application/json `
  -Body '{"initial_measurement_count": 10, "followup_measurement_count": 240, "photo_enabled": true, "photo_autofocus_enabled": true, "contact_quality": {"max_mad_sigma_ohm": 300, "max_p95_abs_step_ohm": 1000, "max_relative_mad_sigma": 0.02, "max_relative_p95_abs_step": 0.05}}'
```

During this session the GUI owns route movement, autofocus, route photos,
needle lowering, resistance contact check, contact seek, pause, interrupt,
Telegram status, and needle lifting. The API client owns the external
experiment measurement and storage. Resistance results, raw resistance samples,
short/bad-contact status, contact seek details, and photo artifact IDs are
returned through session status; no resistance CSV or route photo directory is
created on the server for this API mode.

Session controls:

```powershell
Invoke-RestMethod -Uri http://127.0.0.1:8765/api/v1/route/sessions/current -Headers $headers

Invoke-RestMethod -Method Post `
  -Uri http://127.0.0.1:8765/api/v1/route/sessions/current/actions `
  -Headers $headers `
  -ContentType application/json `
  -Body '{"action": "pause"}'

Invoke-RestMethod -Method Post `
  -Uri http://127.0.0.1:8765/api/v1/route/sessions/current/result `
  -Headers $headers `
  -ContentType application/json `
  -Body '{"status": "ok", "summary": {"iv_points": 101}, "files": [{"kind": "iv", "path": "C:/data/contact-7-iv.csv"}]}'
```

Allowed actions are `pause`, `resume`, `interrupt`, `stop`, `skip`,
`remeasure`, and `jump:<point>`. Use
`POST /api/v1/route/sessions/current/seek` to repeat contact seek from the
current needle position while the session is waiting for contact attention.
Download photo artifacts from
`GET /api/v1/route/sessions/current/artifacts/{artifact_id}` and save them in
the API client experiment folder. Server-side photo files are only used by the
normal GUI route-photo workflow; API session artifacts are temporary in-memory
bytes for the client and Telegram notifications.

Meter ranges can be configured without device-specific range fields:

```powershell
Invoke-RestMethod -Method Post `
  -Uri http://127.0.0.1:8765/api/v1/meter/configure `
  -Headers $headers `
  -ContentType application/json `
  -Body '{"meter_type": "keithley", "measurement_voltage_v": 0.03, "ranges": {"mode": "code_auto", "expected_resistance_ohm": 100000, "max_current_a": 0.00001}}'
```

`code_auto` is probe-station code autoranging. It computes a fixed voltage
range plus current and compliance ranges from the external measurement
parameters before a series starts. `voltage_range_v` is optional; when it is
omitted, raw sweeps use `max(abs(voltages_v))`, and meter configuration uses
`measurement_voltage_v`. It does not enable the instrument's own autorange
during the +/- voltage readings, so both polarities use the same range
configuration. The range changes only when you call `meter.configure`,
`meter.raw_sweep`, or `prepare_contact` with different meter/range parameters.

For API-owned measurements that should reuse the same instrument driver as
the GUI, prefer station-owned VISA roles instead of adding new API measurement
schemas. The server owns the real VISA resources and exposes only configured
roles:

```powershell
Invoke-RestMethod `
  -Uri http://127.0.0.1:8765/api/v1/visa/resources `
  -Headers $headers

Invoke-RestMethod -Method Post `
  -Uri http://127.0.0.1:8765/api/v1/visa/resources/meter.source/query `
  -Headers $headers `
  -ContentType application/json `
  -Body '{"command": "*IDN?", "timeout_ms": 10000}'
```

Current roles are `meter.source` for the required source meter and
`meter.voltmeter` when a separate voltmeter is configured. Trigger Link setup is
not part of the API; it stays inside the high-level meter driver.
Use `meter_type="keithley_2400"` for source-meter-only measurements, or
`meter_type="keithley"` / `meter_type="keithley_2400_2182a"` for the 2400+2182A
pair.

### Python Client

Install the lightweight client extra:

```powershell
.\.venv\Scripts\python.exe -m pip install -e ".[client]"
```

Use an explicit key:

```python
from probe_station_client import ProbeStationClient

client = ProbeStationClient(
    base_url="http://127.0.0.1:8765",
    api_key="psk_your_key_here",
)

print(client.stage_status())
client.move_to(x=12.5, y=8.0)
client.move_by(z=-0.1)
contacts = client.route_contacts()
```

Or save the key once and let the client load it later:

```python
from probe_station_client import ProbeStationClient

client = ProbeStationClient(profile="lab-prober")
backend = client.save_api_key("psk_your_key_here")
print(f"saved in {backend}")

client = ProbeStationClient(profile="lab-prober")
client.prepare_contact(7, check_sample_count=10, contact_settle_s=0.2)
client.meter.configure(
    meter_type="keithley",
    measurement_voltage_v=0.03,
    ranges={
        "mode": "code_auto",
        "expected_resistance_ohm": 100_000,
        "max_current_a": 10e-6,
    },
)
result = client.meter.raw_sweep([-0.1, 0.0, 0.1], contact_number=7)
```

Use the station-owned measurement instrument from Python:

```python
from probe_station_client import ProbeStationClient

client = ProbeStationClient(profile="lab-prober")

# Low level, pyvisa-like access to the configured source meter.
source = client.meter.visa("meter.source")
print(source.query("*IDN?"))

# High-level ohmmeter interface. If meter.voltmeter exists, the driver uses the
# configured 2400+2182A pair. If not, it falls back to the 2400 source meter.
meter = client.meter.ohmmeter()
meter.configure_measurement(
    measurement_voltage_v=0.03,
    voltage_range_v=None,
    current_range_a=10e-6,
    compliance_current_a=10e-6,
    nplc=1,
    ranges={
        "mode": "code_auto",
        "expected_resistance_ohm": 100_000,
    },
)
iv = meter.measure_voltage_list([-0.1, 0.0, 0.1])
```

`prepare_contact` is a client-side recipe. It moves to the loaded route contact,
optionally focuses and captures a contact photo before lowering needles, lowers
needles, checks contact quality, runs contact seek when needed, and lifts
needles on failure. The backend API still sees only the lower-level commands.

For API route control scans, keep the contact loop in the API client and call
`prepare_contact` for each contact. This avoids hidden route-session state:

```python
from pathlib import Path
from probe_station_client import ProbeStationClient

client = ProbeStationClient(profile="lab-prober")
experiment_dir = Path(r"C:\data\chip-001")

for contact in range(32, 421):
    run_dir = experiment_dir / f"contact-{contact:03d}"
    run_dir.mkdir(parents=True, exist_ok=True)
    prep = client.prepare_contact(
        contact,
        raise_before_move=True,
        move_to_contact=True,
        focus_before_lower=True,
        photo_path=run_dir / "contact_photo.jpg",
        lower_needles=True,
        check_sample_count=10,
        measurement_count=250,
        seek_attempts=3,
        telegram_on_seek_failure=True,
        contact_quality={
            "max_mad_sigma_ohm": 300.0,
            "max_p95_abs_step_ohm": 1_000.0,
            "max_relative_mad_sigma": 0.02,
            "max_relative_p95_abs_step": 0.05,
        },
    )
    if prep.get("measurement", {}).get("status") == "short":
        continue
    if not prep.get("prepared"):
        continue

    result = client.meter.raw_sweep(
        [-0.1, 0.0, 0.1],
        contact_number=contact,
        move_to_contact=False,
        lower_needles=False,
    )
```

`GET /api/v1/route/contacts/{contact}/photo` captures the current camera frame
as bytes. `prepare_contact(photo_path=...)` calls this endpoint after focus and
before needle lowering, then writes the image into the API client experiment
folder.

Contact quality criteria are API settings, not hidden code constants. Pass
`contact_quality` to `check_contact`, `contact_seek`, `prepare_contact`, or
`route.start_external`. The response includes `contact_quality_limits`; failed
checks include `measurement.contact_quality.failure_criteria` with the exact
threshold that rejected the contact.

API route control exposes a GUI pause/resume control without creating a route
session. Call `client.api_route_control.start(...)` before the loop, poll
`client.api_route_control.status()` between contacts or voltage blocks, and
call `client.api_route_control.finish(...)` when done. `pause` is only a pause
request. The API client must call `client.api_route_control.pause_ack()` when it
has reached a safe waiting point; only then does the GUI show Resume. While the
pause request is pending, the same GUI control remains an Interrupt path and
aborts the current stage/contact-seek/meter operation best-effort.

The older external route session API is still available when the GUI, rather
than the API client, should own route iteration:

```python
from pathlib import Path
from probe_station_client import ProbeStationClient

client = ProbeStationClient(profile="lab-prober")
session = client.route.start_external(
    initial_measurement_count=10,
    followup_measurement_count=240,
    photo_enabled=True,
    photo_autofocus_enabled=True,
    meter={
        "meter_type": "keithley",
        "measurement_voltage_v": 0.03,
        "ranges": {
            "mode": "code_auto",
            "expected_resistance_ohm": 100_000,
            "max_current_a": 10e-6,
        },
    },
)

# start_external opens the GUI route controls and starts the session paused at
# the first selected contact. Apply Save Shift or choose another current point
# in the GUI if needed, then resume from the GUI or from Python.
session.resume()

experiment_dir = Path(r"C:\data\chip-001")
experiment_dir.mkdir(parents=True, exist_ok=True)

for contact in session.iter_ready():
    prep = contact.preparation
    photo_id = prep.get("photo_artifact_id")
    if photo_id:
        (experiment_dir / f"contact-{contact.contact_number}.jpg").write_bytes(
            contact.download_artifact(photo_id)
        )

    # Run the API-owned IV measurement through the station-owned meter
    # and write its files locally.
    meter = client.meter.ohmmeter()
    iv = meter.measure_voltage_list([-0.1, 0.0, 0.1])
    iv_path = experiment_dir / f"contact-{contact.contact_number}-iv.csv"

    contact.submit_result(
        status="ok",
        summary={"iv_points": len(iv)},
        files=[{"kind": "iv", "path": str(iv_path)}],
    )
```

If the resistance precheck reports `short`, the GUI records that status in the
session result and skips the external wait for that contact. If contact quality
is bad, the session waits; call `session.seek_current()`, `session.skip()`, or
use the GUI/Telegram route actions. `session.iter_ready()` yields only contacts
that are waiting for the API-owned external measurement. During the initial
paused state, pause, interrupt correction, or contact attention it keeps polling
while the GUI route controls remain active; if the session stops or fails, it raises
`ProbeStationClientError` with the server message instead of ending the loop
silently.

Credential lookup order is:
1. `PROBE_STATION_API_KEY`;
2. the OS credential backend through `keyring`;
3. a per-user credentials file with restrictive permissions where the platform
   supports them.

The base URL can be passed to `ProbeStationClient` or set with
`PROBE_STATION_API_URL`.

### QCoDeS Driver

Install the QCoDeS client extra:

```powershell
.\.venv\Scripts\python.exe -m pip install -e ".[qcodes-client]"
```

Use the GUI as a QCoDeS instrument:

```python
from probe_station_client import ProbeStationInstrument

station = ProbeStationInstrument("probe_station", profile="lab-prober")

print(station.stage.state())
print(station.stage.position())
station.stage.move_to(x=12.5, y=8.0)
station.stage.move_by(z=-0.1)

station.meter.configure(
    meter_type="keithley",
    measurement_voltage_v=0.03,
    ranges={"mode": "code_auto", "expected_resistance_ohm": 100_000},
)
data = station.meter.raw_sweep([-0.1, 0.0, 0.1], contact_number=7)
meter = station.meter.ohmmeter()
iv = meter.measure_voltage_list([-0.1, 0.0, 0.1])

session = station.route.start_external(initial_measurement_count=10)
print(station.route.status())
station.route.resume()

station.close()
```

Stage motion lives under the `stage` submodule: `station.stage.x`,
`station.stage.move_to`, and `station.stage.move_by`. Measurement-instrument
configuration, raw sweeps, remote VISA handles, and the high-level ohmmeter
factory live under `station.meter`. GUI-owned route workflow controls live
under `station.route`.

## Settings

Open settings through `Application -> Settings...`.

Typical items:
- `Controls`: key bindings;
- `Jog`: manual jog parameters;
- `Measurement`: instrument connection and live measurement defaults;
- `Needles`: needle safety zone and chip contact machine position;
- `Coordinates`: coordinate mode;
- `Logging`: log path and log level.

## Typical Operator Scenarios

### Move Around The Chip Manually

1. Connect the controller.
2. Run `homing`.
3. Raise the needles.
4. Move through `Joystick`.
5. Use `Autofocus` if needed.

### Move To A Point In The Camera Image

1. Raise the needles.
2. Click the required point in the central image.
3. Wait for the move to complete.

### Register The Chip To The Design

1. Load the design.
2. Open `Design Window`.
3. Select two design points.
4. In `Alignment`, capture the two corresponding real chip points.
5. Wait for rotation and registration to complete.
6. After that, use the minimap, the full design map, and plan targets.

### Align The Chip Quickly Without A Design

1. Open `Alignment`.
2. Capture two real chip points.
3. Wait for the `B` rotation to complete.

### Move By Minimap

1. Make sure the design is loaded and registration is valid.
2. Raise the needles.
3. Click the minimap.

### Move By Full Design Map

1. Make sure the design is loaded and registration is valid.
2. Raise the needles.
3. Open `Design Window`.
4. Click the required point on the full design map.

## Troubleshooting

### Automatic Motion Does Not Start

Check:
- whether the needles are raised;
- whether the needle state is `unknown`;
- whether `homing` has been completed;
- whether the controller is busy with another operation;
- whether registration is valid for minimap, full-map, or target-driven motion.

### Design-Based Motion Does Not Work

Check:
- whether a design is loaded;
- whether alignment has been completed;
- whether registration was invalidated by `B` motion, `homing`, disconnect, or reset.

### The Click Snaps To The Wrong Place

Check:
- whether `Snap To Geometry` is enabled;
- whether you are zoomed in enough around the intended geometry;
- whether another nearby line or vertex is closer;
- if you need an exact raw click, turn off `Snap To Geometry` temporarily.

### Terminal Commands Disturb Machine State

After manual terminal commands:
- re-check displayed coordinates;
- re-check the needle state;
- if needed, run `Raise` again or `homing A`;
- if needed, repeat alignment.

## Log

Open the status log through `Application -> Open Status Log`.

Useful things to check there:
- connection errors;
- safety-block messages;
- design-registration invalidation messages;
- failed move or failed autofocus messages.
