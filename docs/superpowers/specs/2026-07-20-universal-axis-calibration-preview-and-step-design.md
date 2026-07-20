# Universal Axis Calibration, Live Preview, and Exact Step Mode

## Purpose

Replace the axis-specific calibration implementations with one file-backed
linear interpolation model for every stage axis, show the selected calibration
curve and live axis position directly in Settings, and make Step mode a reliable
physical-coordinate positioning tool.

The operator should be able to select one calibration NPZ independently for X,
Y, Z, A, B, or C. When enabled, the same mapping must be used consistently for
displayed coordinates, entered targets, click-to-move, precision approach, and
exact steps. Continuous Jog remains direct velocity-like manual control, while
Step accumulates calibrated physical targets without losing rapid key presses.

## Current Problems

- File-backed linear interpolation is special-cased for A and Z. X, Y, B, and C
  cannot select a calibration file.
- The settings model retains separate A cosine and Z polynomial models even
  though file-backed interpolation is the desired calibration mechanism.
- Calibration file import is coupled to precision-approach direction even
  though backlash is handled by the separate precision-approach mechanism.
- Settings show only the selected file and point count. The operator cannot
  inspect the curve or see the current stage location within its calibrated
  domain.
- Step size changes by 0.1 mm/deg and the panel-level wheel handler changes the
  step size whenever Step mode is active. A 100 micrometre increment is too
  coarse for exact positioning, and an unfocused wheel should continue to
  control speed.
- A Step command received while a previous coordinate move is active is
  rejected as busy. Rapid fine-positioning inputs are therefore lost.

## Scope

This design includes:

- one universal calibration settings model for X, Y, Z, A, B, and C;
- one new NPZ contract containing a single strictly increasing curve;
- removal of the A cosine and Z polynomial calibration models;
- universal controller-to-physical and physical-to-controller interpolation;
- use of the universal mapping by every exact coordinate-target path;
- an embedded PyQtGraph curve preview with a live position marker;
- focus-aware wheel handling and a 0.001 mm/deg Step increment;
- debounced accumulation of exact Step targets;
- one prepared Z calibration NPZ containing the direct measured pass.

This design does not:

- fit a polynomial, spline, or hysteresis model;
- select a calibration curve from a motion direction;
- extrapolate beyond a measured calibration domain;
- turn continuous Jog into a sequence of calibrated absolute micro-moves;
- change homing, limit-switch, B-axis reference, needle safety, route
  Pause/Resume/Interrupt, or manual terminal semantics;
- add calibration acquisition hardware or a calibration-fitting workflow.

## Calibration NPZ Contract

### Required Data

A calibration file is a NumPy NPZ archive with exactly one logical curve. It
provides these required entries:

- `axis`: one scalar Unicode axis name: `X`, `Y`, `Z`, `A`, `B`, or `C`;
- `controller`: a one-dimensional finite floating-point array of controller
  machine coordinates;
- `physical`: a one-dimensional finite floating-point array of measured
  physical machine coordinates corresponding one-to-one with `controller`.

`controller` and `physical` must have the same length and contain at least two
points. Both arrays must be strictly increasing. Duplicate coordinates,
plateaus, decreasing samples, non-finite values, and a mismatched axis make the
file invalid. Import does not sort, merge, smooth, fit, or repair the file.

X, Y, Z, and A coordinates are expressed in millimetres. B and C coordinates
are expressed in degrees. The axis name determines the unit; the archive does
not carry a separate unit override.

The application has no direction-aware or alternate NPZ schema. A file either
satisfies the required `axis/controller/physical` contract or reports an
ordinary structural validation error.

### Import Snapshot

NPZ reading and validation remain outside the GUI thread. A successful import
stores an immutable snapshot in settings:

- the absolute calibration file path, for provenance and replacement;
- the controller points;
- the physical points.

Runtime interpolation uses the saved snapshot rather than reopening the NPZ.
The application therefore starts and moves safely if the selected source file
is later unavailable. Selecting an invalid file leaves the previous snapshot,
enabled state, and preview unchanged.

### Prepared Direct-Pass File

The raw measurement archive
`axis_z_spm6335_section3_precise_start16p5_top23p4_step0p0025_settle2p0_feed1_transition10_20260505_175825.npz`
remains unchanged in the machine-local `calibrations` directory.

A separate machine-local archive with the suffix `_forward.npz` is prepared
from its direct measured pass. The preparation step:

1. selects the direct pass from the raw measurement data;
2. computes a longest strictly increasing subsequence over its physical values;
3. retains the corresponding controller samples without changing either
   measured value;
4. writes `axis="Z"`, `controller`, and `physical` using the new contract.

The source pass contains 2761 samples. Three conflicting initial samples are
excluded, leaving 2758 unchanged samples in the prepared file. This preparation
is a one-time local data operation, not runtime import or compatibility logic.

## Settings Model

`Settings` owns one `axis_calibrations` mapping keyed by all six stage axes.
Each value is the same `AxisCalibrationSettings` type with:

- `enabled: bool`;
- `calibration_file: str`;
- `controller_points: list[float]`;
- `physical_points: list[float]`.

All axes exist in the mapping by default and begin disabled with no selected
file or points. The axis-specific A and Z calibration settings, formula
parameters, model names, fit diagnostics, and image/source fields are removed
from defaults, parsing, serialization, controller setup, and UI code.

Loading settings normalizes the six mapping entries and fills any missing axis
with a disabled empty calibration. Saving writes only the universal mapping.
Removed formula settings are not sampled or converted into interpolation
points.

The existing precision-approach profiles remain a separate mapping. Their
enabled state, backlash, and final approach side do not select or alter a
calibration curve.

## Universal Coordinate Mapping

### Mapper Boundary

One mapper owns calibration lookup and interpolation for every axis. Its public
operations are conceptually:

- `controller_to_physical(axis, controller_coordinate)`;
- `physical_to_controller(axis, physical_coordinate)`;
- `controller_domain(axis)`;
- `physical_domain(axis)`.

An absent or disabled calibration is the identity mapping. An enabled
calibration uses piecewise linear interpolation only within the closed measured
domain. Exact domain endpoints are valid. A value outside the domain returns a
typed unavailable/out-of-domain result; interpolation never clamps or
extrapolates silently.

The mapper is a pure, hardware-independent component. NPZ reading, Qt state,
serial I/O, motion execution, and precision-approach planning remain outside
it.

### Coordinate Systems

Calibration points describe machine coordinates. In machine-coordinate mode,
the reported raw machine coordinate is mapped directly.

In a work-coordinate mode, mapping respects the controller work origin rather
than feeding a translated work coordinate directly into a nonlinear curve. For
raw machine coordinate `m`, raw work origin `o`, and calibration function `f`,
the displayed work coordinate is:

`f(m) - f(o)`

The inverse for a requested physical work target `w` is:

`f_inverse(w + f(o))`

The existing controller status and work-offset state provide `m` and `o`. If a
required offset is unavailable, the calibrated coordinate is unavailable; the
GUI does not invent an offset. Existing B zero-reference and homing state remain
authoritative and are applied at their current controller boundary.

### Motion Paths

When a calibration is enabled, these paths interpret user-facing coordinates
as physical coordinates and convert targets through the inverse mapper:

- coordinate-field Apply;
- absolute API/controller target methods already expressed in GUI coordinates;
- Design Window click-to-move for calibrated X/Y;
- focus and other exact Z targets;
- needle and other exact A targets;
- B/C exact rotation targets;
- precision-approach pre-target and final-target planning;
- Step mode.

Raw targets are validated against the controller domain before any motion is
accepted. Physical targets are validated against the physical domain. Software
limits, homing readiness, coordinate confidence, needle safety, and the
precision-approach side remain additional independent gates.

Homing and controller-origin operations continue to use their existing raw
controller commands. Status received after those operations is mapped for
display normally.

### Continuous Jog

Continuous Jog preserves its current press-and-hold model. A direction key or
button sends a long relative `$J=G91` target, and release sends the realtime jog
stop byte. It is not decomposed into calibrated physical steps.

When calibration is enabled, the maximum continuous-jog endpoint is also
clipped to the raw controller calibration domain, in addition to existing
software limits. Jog therefore remains direct and responsive but cannot carry
the axis into a position that the enabled mapping cannot represent. Reported
positions during and after Jog are mapped to physical coordinates.

## Axes Settings UI

### Per-Axis Controls

The existing X/Y/Z/A/B/C selector remains. Every axis page presents the same
two groups:

1. Precision approach: Enabled, Backlash, and Final direction.
2. Coordinate calibration: Enabled, selected File, Browse, Reset, status, and
   conditional curve preview.

Browse filters for NPZ files and imports for the selected axis. Reset disables
calibration, clears its file and snapshot, and removes the preview immediately.

The curve preview is present only when that axis has a successfully imported
calibration file. There is no empty graph or reserved blank height before a
file is selected.

### PyQtGraph Preview

The preview uses the already-installed PyQtGraph rather than Plotly or
`QWebEngineView`. It contains:

- a blue piecewise-linear curve;
- small source sample points;
- controller-coordinate X and physical-coordinate Y labels with the axis unit;
- native mouse pan, wheel zoom, rectangular zoom, auto-range, and point hover;
- a large contrasting diamond at the current mapped position;
- a thin dashed vertical line through the current controller coordinate;
- a hover tooltip showing controller and physical coordinates.

The graph is constructed only after a valid file is imported or when settings
open with a saved valid snapshot. It is a normal persistent part of that axis
page once constructed; switching axes does not discard it.

The existing `stage_position_changed` path is the notification that refreshes
the current marker. Settings receive an explicit narrow position source that
returns the latest cached raw machine coordinate and required origin state;
they do not reuse an already mapped display value, reach through the parent
widget, or query hardware. Marker updates change only the diamond and vertical
line data and do not rebuild the curve or reset the user's view.

If Coordinate calibration is unchecked, the curve remains visible but the
diamond and vertical line are hidden. If position is unavailable they are also
hidden. If an enabled current raw position lies outside the curve domain, the
plot range is not expanded; a short `Current position is outside the
calibration range` message is shown. Loading another valid curve auto-ranges
once. Ordinary position changes never auto-range.

## Exact Step Mode

### Wheel and Step Size

The Step size spin box keeps three decimals and uses a single step of 0.001.
For X/Y/Z/A this is 0.001 mm, or one micrometre. For B/C it is 0.001 degrees.

The joystick-panel mouse wheel normally adjusts the active feedrate in both Jog
and Step modes. Step size changes by wheel only after the operator clicks into
the Step size value so that the editor has focus. Its wheel changes are fixed
at 0.001 per notch and do not use the panel's speed-dependent wheel
acceleration. When the editor loses focus, the wheel again controls feedrate.

Typing a step size and using the spin-box buttons remain supported. Switching
between Jog and Step does not itself alter the chosen step size.

### Physical Target Accumulator

Step is an exact physical-coordinate mode. A press adds a signed step to a
desired physical target, not to the most recently received raw status sample.
The baseline is selected in this order:

1. the current accumulated target for that axis;
2. the active exact-move target for that axis;
3. the latest known physical displayed coordinate.

The first press starts a fixed approximately 80 ms accumulation window. Inputs
received in that window are combined into one multi-axis target. The window is
not repeatedly extended by every press. Five `+0.001 mm` inputs therefore issue
one `+0.005 mm` physical target after the short window.

If an exact move is already executing, new Step presses update one pending
physical target rather than being rejected as busy. The active motion is not
interrupted. When it finishes successfully, one move to the latest accumulated
target begins. Opposite inputs subtract from the target; a net target equal to
the reached coordinate produces no additional move.

Each candidate addition is checked against the physical calibration domain and
existing display-space software limits. An invalid addition is rejected without
discarding the previously valid accumulated target. The accepted accumulated
coordinate is shown using the existing pending-coordinate field styling.

Precision approach applies to each actual issued target, including the final
coalesced target, rather than to each input event. With calibration disabled,
the identity mapping gives the same accumulation behavior in raw coordinates.

### Accumulator Invalidation

Pending Step targets and timers are cleared without executing deferred motion
when any of these occurs:

- leaving Step mode;
- disconnect or reconnect;
- soft reset;
- homing or controller-origin change;
- a manual serial command;
- coordinate-system or work-origin change;
- active motion failure or cancellation;
- application shutdown.

Clearing a stale target also clears its pending-coordinate presentation. No
deferred Step command may survive a loss of coordinate authority.

## Threading and Responsiveness

- NPZ file access and validation run in the existing worker-pool pattern.
- Serial status and motion remain in controller workers.
- The GUI thread creates and updates Qt/PyQtGraph objects only.
- Curve creation is linear in the imported point count and happens only after a
  successful import or when showing a saved preview.
- Live position updates mutate constant-size marker items and do not recreate
  the sample curve, settings page, or plot widget.
- No in-process feature calls the application's localhost API.

## Error Handling

- Missing, unreadable, malformed, mismatched-axis, non-finite, unequal-length,
  undersized, or non-increasing NPZ data produce a concise validation status.
- Failed import preserves the previously selected file, enabled state, saved
  snapshot, and preview.
- An enabled calibration with an invalid persisted snapshot is disabled during
  settings normalization and is not applied to motion.
- Out-of-domain exact targets are rejected before a controller command is
  queued.
- Loss of a required work offset makes the corresponding calibrated coordinate
  and target unavailable.
- A failed or cancelled Step move clears accumulated follow-up work.
- Preview absence or hidden live markers never changes calibration or motion
  state.

## Testing

### Pure Model and Import Tests

- accept each of the six valid axis names and both unit families;
- reject mismatched axis, missing entries, malformed shapes, unequal lengths,
  non-finite values, duplicates, plateaus, and decreasing values;
- verify direct and inverse piecewise-linear interpolation at endpoints and
  interior points;
- verify identity behavior when calibration is absent or disabled;
- reject controller and physical values outside their domains;
- verify machine and work-coordinate transforms around a nonlinear curve;
- verify settings defaults, cloning, serialization, and invalid-snapshot
  disabling for all six axes.

### Motion Tests

- verify coordinate fields, multi-axis Apply, click-to-move, precision
  approach, and Step submit the expected inverse-mapped raw targets;
- verify status display uses forward mapping for all axes;
- verify calibrated domain checks compose with software limits and safety;
- verify continuous Jog remains relative and is clipped to calibration domain;
- verify homing and raw origin operations are not inverse-mapped;
- retain existing route-control and needle-safety regression coverage.

### UI and Step Tests

- show a preview only after a valid file exists for that axis;
- keep the curve visible but hide live marker items when calibration is
  disabled;
- update a visible marker without replacing curve items or resetting range;
- preserve the previous preview after a failed import and remove it on Reset;
- verify graph units for linear and rotary axes;
- verify the unfocused panel wheel changes feedrate in both modes;
- verify the focused Step editor changes by exactly 0.001 per notch;
- combine rapid same-direction and opposite-direction inputs;
- accumulate against active and pending physical targets rather than stale
  status;
- defer one latest target while motion is active and issue it after success;
- reject out-of-domain additions without losing an earlier valid target;
- clear timers, pending presentation, and targets on every invalidation event.

### Prepared Data Verification

- verify the raw measurement archive remains byte-for-byte unchanged;
- verify the prepared `_forward.npz` exposes only the required logical data;
- verify its axis is Z, its arrays contain 2758 points, and both arrays are
  strictly increasing;
- verify the prepared samples are unchanged samples from the selected raw pass.

## Acceptance Criteria

The work is complete when:

1. every axis can independently select, enable, reset, persist, and apply one
   NPZ calibration;
2. only the universal file-backed settings and mapper remain;
3. exact user targets round-trip between controller and physical coordinates
   within interpolation tolerance and cannot leave the measured domain;
4. a selected file displays a responsive embedded PyQtGraph preview;
5. the live marker follows cached stage status only while calibration is
   enabled;
6. the joystick wheel controls feedrate except when the Step size editor is
   explicitly focused, where it changes by 0.001;
7. rapid Step presses accumulate into safe physical targets without being lost;
8. stale accumulated targets cannot execute after coordinate authority changes;
9. the prepared direct-pass Z archive satisfies the new contract while the raw
   source remains unchanged;
10. the full automated test suite passes without hardware access.
