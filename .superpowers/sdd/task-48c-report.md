# Task 48c Report: Route Measurement Operation-State Policy

## Scope

Current behavior:

- `RouteMeasurementDialog._update_operation_state()` computed mode/runtime/editability flags and directly applied enabled states to route output, photo, meter, previous-CSV, and contact controls.
- Waiting route measurement runs could re-enable runtime/output/meter fields; external measurement waiting still kept Interrupt controls.

Structural improvement:

- Added `probe_station_gui.dialogs.route_measurement_operation_state`.
- Moved operation-mode enablement calculation behind `route_measurement_operation_state(...)`.
- `RouteMeasurementDialog._update_operation_state()` now applies a policy object to widgets.
- Added pure policy tests and kept existing app-level waiting-dialog regressions.

Public API changed: no.

## Metrics

Wily ran from disposable UTF-8 temp clone/cache:

- Temp root: `C:\Users\Lazemir\AppData\Local\Temp\probe_station_gui_wily_task48c_20260628162902`
- Baseline: `911a85a`

| Metric | Before | After | Better? |
| --- | ---: | ---: | --- |
| `route_measurement_dialog.py` LOC | 1390 | 1387 | yes |
| `route_measurement_dialog.py` SLOC (radon) | 1263 | 1260 | yes |
| `route_measurement_dialog.py` Wily cyclomatic | 176 | 156 | yes |
| `_update_operation_state` CCN | 22 | 3 | yes |
| New `route_measurement_operation_state.py` LOC | - | 44 | n/a |
| New `route_measurement_operation_state.py` MI | - | 55.70 | n/a |
| Helper `route_measurement_operation_state` CCN | - | 10 | n/a |
| Tests | 1212 passed, 2 skipped | 1215 passed, 2 skipped | yes |
| Coverage total | 73% | 73% | flat |
| Ruff errors | 0 | 0 | flat |

Remaining lizard warnings in the Task 48 slice:

- `RouteMeasurementProfileMixin._apply_profile_data`: 68 NLOC / CCN 16.
- `RouteMeasurementHistogram.paintEvent`: 135 NLOC / CCN 24.

## Validation

- `.\.venv\Scripts\python.exe -m pytest tests\route\test_route_measurement_operation_state.py tests\route\test_operation_modes.py tests\route\test_route_measurement_profile.py tests\route\test_route_measurement_result_views.py tests\route\test_dialog_adapter.py tests\route\test_run_ui.py`
  - `36 passed`
- `.\.venv\Scripts\python.exe -m pytest tests\app\test_main_coordinate_feedrate.py -k "waiting_dialog_enables_runtime_output_and_meter_fields or external_measurement_waiting_keeps_interrupt_control or api_route_control or waiting_dialog"`
  - `23 passed, 139 deselected`
- `.\.venv\Scripts\python.exe -m coverage run -m pytest tests`
  - `1215 passed, 2 skipped`
- `.\.venv\Scripts\python.exe -m coverage report -m --ignore-errors`
  - total coverage `73%`
- `.\.venv\Scripts\python.exe -m ruff check --ignore E402,F401 .`
  - clean

## Verdict

This refactor is justified.

Reason:

- behavior preserved: yes
- tests passed: yes
- metrics improved: yes, `_update_operation_state` dropped from CCN 22 to 3 and dialog Wily cyclomatic dropped by 20
- maintainability improvement: operation-mode editability is now a pure tested policy module
- new risk introduced: low; the app-level waiting-dialog regression still covers the safety-sensitive waiting/control interaction

