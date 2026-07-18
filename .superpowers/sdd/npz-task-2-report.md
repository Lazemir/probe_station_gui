# NPZ Task 2 Report: Runtime Linear Interpolation Mapping

## Scope

Implemented runtime piecewise-linear forward/inverse coordinate mapping for
normalized A- and Z-axis calibration snapshots. Controller calibration
application now accepts only finite, equal-length, strictly increasing G-code
and display point sequences with at least two points. Legacy cosine A and
quintic Z branches remain unchanged.

No GUI, route-control, precision-path planning, serial, or hardware behavior
was changed.

## RED-GREEN evidence

1. Added the Z controller-facing interpolation test first and ran:

   `C:\Users\Public\code\probe_station_gui\.venv\Scripts\python.exe -m pytest tests/stage/test_controller_axis_needles.py::StageControllerLinearInterpolationCalibrationTest::test_axis_z_calibration_interpolates_forward_and_inverse -q`

   RED: `0.5` remained `0.5` instead of mapping to `1.0`, proving the
   interpolation snapshot was rejected by the controller.

2. Added the minimal Z interpolation/runtime acceptance branch and reran the
   same command.

   GREEN: `1 passed`.

3. Added the equivalent A controller-facing test and ran it before the A
   implementation.

   RED: raw `-2.0` remained display `-2.0` instead of mapping to `-3.5`.

4. Added the A forward/inverse branch and controller acceptance, then reran the
   focused test.

   GREEN: `1 passed`.

5. Added endpoint clamping, interior round-trip, invalid-snapshot fallback,
   helper-level piecewise interpolation, and precision pre-send rejection
   regression coverage. Refactored A/Z snapshot acceptance through one shared
   validator while GREEN.

## Verification

Required selected regression suite:

`C:\Users\Public\code\probe_station_gui\.venv\Scripts\python.exe -m pytest tests/stage/test_axis_calibration.py tests/stage/test_controller_axis_needles.py tests/stage/test_precision_motion.py -q`

Result: `63 passed, 2 subtests passed`.

Targeted lint:

`C:\Users\Public\code\probe_station_gui\.venv\Scripts\python.exe -m ruff check probe_station_gui/stage/axis_mapping.py probe_station_gui/stage/axis_coordinates.py tests/stage/test_axis_calibration.py tests/stage/test_controller_axis_needles.py tests/stage/test_precision_motion.py`

Result: `All checks passed!`.

## Runtime nuances preserved

- Forward status mapping clamps to interpolation endpoints, so an out-of-range
  raw status value remains safe to display.
- Inverse mapping uses the normalized display points with axes exchanged.
- Requested targets outside the imported display domain still fail the
  existing precision round-trip check before any movement segment is sent.
- Accepted runtime dictionaries retain raw G-code `min`/`max`, positive
  `steps_per_mm`, and immutable point tuples.
- Invalid or incomplete interpolation snapshots disable that axis calibration
  safely instead of partially applying it.
- Commit message: `feat: map axis coordinates by linear interpolation`.

## Review follow-up

The review identified that `bisect_right` could select an out-of-range upper
index when a malformed status frame supplied `NaN` to the interpolation helper.

The public controller regression was added first and run with:

`C:\Users\Public\code\probe_station_gui\.venv\Scripts\python.exe -m pytest tests/stage/test_controller_axis_needles.py::StageControllerLinearInterpolationCalibrationTest::test_nan_status_value_is_preserved_without_mapping_failure -q`

RED: both A and Z subtests raised `IndexError` from
`interpolate_calibration_curve`.

The minimal fix returns a `NaN` input unchanged before endpoint comparison or
bisection. This matches the legacy pass-through behavior and prevents status UI
flow from failing. Positive and negative infinity retain endpoint-clamping
behavior.

Additional review hardening covers:

- A/Z display-to-raw-to-display interior composition;
- mismatched interpolation lengths;
- non-finite G-code and display points;
- nonpositive and non-finite `steps_per_mm`;
- unordered G-code and non-strict display points.

Follow-up verification result: `64 passed, 20 subtests passed`; targeted Ruff
checks passed.

Follow-up commit message: `fix: handle non-finite interpolation input`.
