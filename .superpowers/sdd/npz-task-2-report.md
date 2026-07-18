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

## Final integrated safety review

The final review found two request paths that could lose an out-of-domain
target through endpoint clamping:

1. `probe_station_gui/stage/precision_motion.py` converted an original raw
   target to display coordinates before planning. Raw Z `4.0` on a curve whose
   G-code domain ends at `3.0` became display endpoint `5.0`, then inverse
   conversion sent raw endpoint `3.0`.
2. `probe_station_gui/views/main_window_stage_position_panel.py` used the
   clamping inverse converter for GUI/API display targets before the existing
   display software-limit check. Display Z `6.0` therefore became raw endpoint
   `3.0` before the motion request retained the original domain information.

### RED-GREEN evidence

Direct raw request test:

`C:\Users\Public\code\probe_station_gui\.venv\Scripts\python.exe -m pytest tests/stage/test_precision_motion.py::test_interpolated_z_rejects_direct_raw_target_outside_curve_before_send -q`

RED: the request completed successfully and sent the clamped endpoint. GREEN:
the public blocking request raises that the target cannot be represented,
reports failure, and leaves the send list empty.

Calibrated-display API test:

`C:\Users\Public\code\probe_station_gui\.venv\Scripts\python.exe -m pytest tests/app/test_main_stage_coordinate_controls.py::MainStageCoordinateControlsTest::test_api_calibrated_display_target_outside_curve_is_rejected_before_send -q`

RED: the API response was accepted and the fake controller recorded a motion
request. GREEN: the response is rejected with status 409 and no request is
recorded.

### Implementation paths

- `probe_station_gui/stage/axis_coordinates.py`: explicit checked raw/display
  target-domain contract for linear interpolation curves.
- `probe_station_gui/stage/precision_motion.py`: validate each original raw
  request before planner conversion, safety checks, or any send.
- `probe_station_gui/views/main_window_stage_position_panel.py`: the actual
  Main GUI/API coordinate resolver now prefers checked display-target
  conversion; legacy controllers keep the original fallback.
- `tests/stage/test_precision_motion.py`: direct raw request safety regression.
- `tests/app/test_main_stage_coordinate_controls.py`: real Main/API
  calibrated-display request regression.

Status display still uses the clamping `calibrated_axis_display_value` path.
Legacy parametric calibration branches, existing display software-limit
checks, safety, cancellation, and route behavior remain unchanged.

Focused verification: `104 passed, 20 subtests passed`.

Full verification: `1743 passed, 30 subtests passed`.

Targeted Ruff checks passed. Commit message:
`fix: reject calibration targets outside curve`.

## Needle target safety follow-up

A second motion-path audit found that physical needle lowering still used the
legacy clamping inverse directly. With a linear A curve spanning display/G-code
`[-2.0, 0.0]`, a saved lowering of `3.0 mm` was silently clamped to raw
`A=-2.0` and accepted as a shorter physical move. The display-coordinate
needle-calibration save path had the same unchecked inverse behavior.

### RED-GREEN evidence

Physical needle lowering test:

`C:\Users\Public\code\probe_station_gui\.venv\Scripts\python.exe -m pytest tests/stage/test_controller_axis_needles.py::StageControllerLinearInterpolationCalibrationTest::test_needles_reject_physical_lowering_outside_curve_before_send -q`

RED: the normal lowering path sent `A=-2.0` for the unrepresentable `3.0 mm`
physical target. GREEN: it emits a failed needle action and performs neither a
serial send nor a needle-state update.

Display needle-calibration save test:

`C:\Users\Public\code\probe_station_gui\.venv\Scripts\python.exe -m pytest tests/app/test_main_stage_coordinate_controls.py::MainStageCoordinateControlsTest::test_display_needle_target_outside_curve_is_rejected_without_save -q`

RED: the out-of-domain display target was persisted (`saved_count == 1`).
GREEN: it produces an operator status message and leaves settings unchanged.

Floating-point endpoint test:

`C:\Users\Public\code\probe_station_gui\.venv\Scripts\python.exe -m pytest tests/stage/test_controller_axis_needles.py::StageControllerLinearInterpolationCalibrationTest::test_checked_raw_and_display_targets_allow_floating_point_endpoint_noise -q`

RED: `0.1 + 0.2` was rejected against an exact `0.3` endpoint. GREEN: a named
`1e-9` domain tolerance admits only insignificant representation noise, after
which interpolation clamps to the exact endpoint.

### Audited paths and regressions

- Normal needle raise/lower/lift: direct target validation, motion-profile
  construction, and every profile segment now use the checked physical-target
  converter before any absolute move.
- Incremental needle adjust: the shared lowering-step converter validates the
  resulting physical target; a regression confirms an out-of-range adjustment
  performs no send or state update.
- Oscillation needle raise/lower/lift: initial and per-segment targets use the
  checked converter before any relative write; a focused orchestration test
  confirms rejection before `_write_relative_g1_unchecked`.
- Oscillation adjust uses the same checked lowering-step converter as normal
  adjust.
- Display-coordinate needle calibration rejects an out-of-domain value before
  cloning or saving settings, while an exact curve endpoint still saves.
- A valid physical endpoint affected by floating-point noise still completes
  and sends the exact raw endpoint.
- Legacy non-linear A calibration keeps its existing conversion behavior
  because domain rejection is enabled only for linear-interpolation snapshots.

Focused verification:

`C:\Users\Public\code\probe_station_gui\.venv\Scripts\python.exe -m pytest tests/stage/test_controller_axis_needles.py tests/app/test_main_stage_coordinate_controls.py tests/stage/test_precision_motion.py -q`

Result: `104 passed, 20 subtests passed`.

Full verification:

`C:\Users\Public\code\probe_station_gui\.venv\Scripts\python.exe -m pytest tests -q`

Result: `1750 passed, 30 subtests passed`.

Targeted Ruff checks passed. Commit message:
`fix: reject out-of-range needle calibration targets`.
