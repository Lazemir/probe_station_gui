# Task 18 Report: Extract Route Shift-Save Execution Decisions

## Files Changed

- `main.py`
- `probe_station_gui/route/adjustment_flow.py`
- `tests/route/test_adjustment_flow.py`
- `tests/app/test_main_coordinate_feedrate.py`

## Behavior Parity Notes

- `Main._save_route_measurement_shift` now delegates pure route shift-save decisions to `probe_station_gui.route.adjustment_flow` and keeps side effects in `Main`.
- Dialog current-point configuration is still read only after save guards accept.
- Runner-active saves still select the adjustment point before reading stage position, save through `runner.save_current_position_adjustment`, update `_api_route_offset_xy` only when `runner.route_offset_xy()` is usable, and mark route interrupt pending in the design navigator/dialog.
- API route-control saves still use `_api_contact_context`, `route_shift_from_stage_xy`, and direct `_api_route_offset_xy` updates without routing in-process behavior through localhost.
- Stage position errors preserve the old fallback rule: busy controllers or missing latest position block with the error; a valid latest position is used as fallback.
- Pause request, pause ack, and interrupt semantics were not changed.

## Tests And Checks

- Red check: `C:\Users\Public\code\probe_station_gui\.venv\Scripts\python.exe -m pytest tests\route\test_adjustment_flow.py tests\app\test_main_coordinate_feedrate.py`
  - Expected failure before implementation: `ImportError: cannot import name 'route_shift_runner_offset_update'`.
- Targeted green check: `C:\Users\Public\code\probe_station_gui\.venv\Scripts\python.exe -m pytest tests\route\test_adjustment_flow.py tests\app\test_main_coordinate_feedrate.py`
  - `116 passed in 1.68s`.
- Required targeted suite: `C:\Users\Public\code\probe_station_gui\.venv\Scripts\python.exe -m pytest tests\route tests\app\test_main_coordinate_feedrate.py`
  - `329 passed in 3.52s`.
- Full suite: `C:\Users\Public\code\probe_station_gui\.venv\Scripts\python.exe -m pytest tests`
  - `814 passed, 2 skipped in 10.70s`.
- Ruff: `C:\Users\Public\code\probe_station_gui\.venv\Scripts\python.exe -m ruff check --ignore E402,F401 .`
  - `All checks passed!`.

## Metrics

Before:

- Lizard `Main._save_route_measurement_shift`: 109 NLOC / CCN 28 / 576 tokens / length 109.
- Radon `Main._save_route_measurement_shift`: D (28).

After:

- Lizard `Main._save_route_measurement_shift`: 31 NLOC / CCN 5 / 141 tokens / length 31.
- Radon `Main._save_route_measurement_shift`: A (5).
- New adapter/helper lizard metrics:
  - `Main._route_shift_save_plan`: 31 NLOC / CCN 5.
  - `Main._route_shift_adjustment_point`: 25 NLOC / CCN 7.
  - `Main._route_shift_current_stage_xy`: 23 NLOC / CCN 4.
  - `Main._update_api_route_offset_from_runner`: 10 NLOC / CCN 4.
  - `Main._apply_route_shift_save_status`: 22 NLOC / CCN 7.
  - `route_shift_stage_position_error_plan`: 12 NLOC / CCN 3.
  - `route_shift_stage_xy_plan`: 10 NLOC / CCN 2.
  - `route_shift_runner_offset_update`: 5 NLOC / CCN 2.
  - `route_shift_save_status_plan`: 10 NLOC / CCN 1.
- Full lizard warning count for `main.py probe_station_gui\route`: 83 before, 82 after. The old `_save_route_measurement_shift` hotspot no longer appears in the warning list.
- Wily: `python.exe -X utf8 -m wily report main.py probe_station_gui\route\adjustment_flow.py` crashed despite UTF-8 mode; the generated log at `C:\Users\Lazemir\AppData\Local\Temp\tmp1ec6oxq8wily_log` repeated only Wily's crash banner.

## Verdict

Accepted. The hotspot was reduced materially while preserving the route shift-save behavior boundary: pure decisions moved into route helpers, and hardware/UI/runner side effects stayed in `Main`. The new behavior pins cover stage-position fallback/blocking, missing X/Y, usable runner offsets, and runner-active interrupt-pending updates.

Residual risk: the route-active app tests still use fakes rather than a real Qt dialog/runner thread, by design, to avoid hardware and GUI runtime work.
