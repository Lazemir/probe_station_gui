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
- for needle calibration mode: a supported `GW Instek LCR`.

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
- `Calibration`: `Design Window`, `Contact / Stone Calibration`, `Surface Map`, `Alignment`.

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
- connect to and disconnect from the configured `LCR`;
- run startup synchronization after connect;
- attempt auto-connect.

How to use it:
1. Press `Refresh` if the port list is outdated.
2. Select the controller port.
3. Check the baud rate, usually `115200`.
4. Press `Connect`.

The same panel contains the `LCR Meter` block. Configure its resource in
`Application` -> `Settings` -> `Needles`, then press `Connect LCR`.

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
- `Reset Cal`;
- `Autofocus`;
- jog speed control.

Behavior:
- holding a button or key keeps the stage moving;
- releasing it stops motion;
- manual `B` motion invalidates design registration;
- `Reset Cal` resets image click-to-move calibration.

Needles:
- `Raise` brings the system into a safe state for motion;
- `Lower` moves the needles to the saved lower position;
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
- on first use, or after `Reset Cal`, the application rebuilds click calibration;
- if the predicted move is too large, the motion is cancelled and calibration is reset;
- this requires a live and stable camera image.

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
- move to measurement-plan targets.

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

## Measurement Plan And Targets

When a measurement plan is loaded:
- `Design Window` shows a target list;
- you can select targets manually;
- you can move with `Previous` / `Next`;
- `Move To` moves the stage to the selected target.

For `Move To` to work correctly you need:
- a loaded design;
- valid registration;
- raised needles.

### Probe Route Measurement

`Probe Route` can run an ordered route and write one numeric resistance value per CSV row.

Typical workflow:
1. Load a design in `Design Window`.
2. Complete design-backed alignment so registration is valid.
3. In `Probe Route`, create points manually or with `Array`.
4. Connect the stage controller and the LCR meter.
5. Press `Run Route` and choose the CSV output path.
6. The runner raises needles, moves to each point, lowers needles, reads the LCR, raises needles again, and continues to the next point.

`Stop` requests a safe stop after the current route action. Completed points are already written to CSV.

## Contact Calibration and LCR

The `Contact / Stone Calibration` window is used to save chip/stone focus
positions and the lower needle contact position. The external `LCR` is connected
from the `Connection` panel.

Requirements:
- properly configured instrument address;
- installed `pyvisa` and `qcodes`;
- a supported `GW Instek LCR`.

Typical workflow:
1. Open needle settings.
2. Configure the `LCR` parameters.
3. In `Connection`, press `Connect LCR`.
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

## HTTP Control API

When the GUI starts, it also starts a local FastAPI server at:

```text
http://127.0.0.1:8765
```

The API uses the same coordinate basis and feedrate that the GUI currently shows. If settings are configured for machine coordinates, API targets are machine coordinates; if settings are configured for work coordinates, API targets are work coordinates. Coordinate move requests go through the same queue and status display as editing the coordinate fields in the status bar.

Move to a coordinate:

```powershell
Invoke-RestMethod -Method Post `
  -Uri http://127.0.0.1:8765/api/v1/stage/move `
  -ContentType application/json `
  -Body '{"x": 12.5, "y": 8.0}'
```

Equivalent payload:

```json
{
  "coordinates": {
    "X": 12.5,
    "Y": 8.0
  }
}
```

Read the GUI-visible stage state:

```powershell
Invoke-RestMethod http://127.0.0.1:8765/api/v1/stage/status
```

You can override the bind address with `PROBE_STATION_API_HOST` and `PROBE_STATION_API_PORT`.

## Settings

Open settings through `Application -> Settings...`.

Typical items:
- `Controls`: key bindings;
- `Jog`: manual jog parameters;
- `Needles`: `LCR` parameters, lowering direction, saved lower position;
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
