# Task 48b Report: Route Measurement Profile Persistence

## Scope

Current behavior:

- `RouteMeasurementDialog` owned profile defaults, JSON profile load/save, settings persistence, profile application, contact-quality extraction, and meter-profile mapping inline.
- `current_configuration()` continued to save the settings file before returning the run configuration.
- Pause Request/Pause Ack/Interrupt methods and route run state stayed in `RouteMeasurementDialog`.

Structural improvement:

- Moved route measurement defaults/prefix constants to `probe_station_gui.dialogs.route_measurement_defaults`.
- Moved profile load/save/apply helpers to `probe_station_gui.dialogs.route_measurement_profile.RouteMeasurementProfileMixin`.
- `RouteMeasurementDialog` now inherits `RouteMeasurementProfileMixin` and keeps Qt construction, route-control state, runtime control wiring, and meter config extraction.
- Added profile characterization tests for profile round-trip, `current_configuration()` parity, settings-file write, and legacy Keithley default migration.

Public API changed: no.

## Metrics

Wily ran from disposable UTF-8 temp clone/cache:

- Temp root: `C:\Users\Lazemir\AppData\Local\Temp\probe_station_gui_wily_task48b_20260628162357`
- Baseline: `5a13565`

| Metric | Before | After | Better? |
| --- | ---: | ---: | --- |
| `route_measurement_dialog.py` LOC | 1809 | 1390 | yes |
| `route_measurement_dialog.py` SLOC (radon) | 1658 | 1263 | yes |
| `route_measurement_dialog.py` Wily cyclomatic | 249 | 176 | yes |
| `route_measurement_dialog.py` MI | 0 | 0 | flat |
| New `route_measurement_profile.py` LOC | - | 441 | n/a |
| New `route_measurement_profile.py` MI | - | 18.72 | n/a |
| New `route_measurement_defaults.py` LOC | - | 46 | n/a |
| New `route_measurement_defaults.py` MI | - | 100.00 | n/a |
| Functions over lizard warning threshold in target slice | 3 | 3 | flat |
| Tests | 1210 passed, 2 skipped | 1212 passed, 2 skipped | yes |
| Coverage total | 72% | 73% | yes |
| Ruff errors | 0 | 0 | flat |

Remaining lizard warnings in the Task 48 slice:

- `RouteMeasurementDialog._update_operation_state`: 44 NLOC / CCN 22.
- `RouteMeasurementProfileMixin._apply_profile_data`: 68 NLOC / CCN 16.
- `RouteMeasurementHistogram.paintEvent`: 135 NLOC / CCN 24.

## Validation

- `.\.venv\Scripts\python.exe -m pytest tests\route\test_route_measurement_profile.py tests\route\test_route_measurement_result_views.py tests\route\test_measurement_display.py tests\route\test_measurement_settings.py tests\route\test_meter_config.py tests\route\test_dialog_adapter.py tests\route\test_run_ui.py`
  - `48 passed`
- `.\.venv\Scripts\python.exe -m coverage run -m pytest tests`
  - `1212 passed, 2 skipped`
- `.\.venv\Scripts\python.exe -m coverage report -m --ignore-errors`
  - total coverage `73%`
  - report used `--ignore-errors` because PySide/coverage recorded generated `pyscript` and `shibokensupport` pseudo-sources without files.
- `.\.venv\Scripts\python.exe -m ruff check --ignore E402,F401 .`
  - clean
- Radon/lizard spot metrics for the Task 48 slice.

## Verdict

This refactor is justified.

Reason:

- behavior preserved: yes
- tests passed: yes
- metrics improved: yes, dialog LOC/cyclomatic dropped and total coverage increased
- maintainability improvement: profile persistence has a focused module and direct tests; the dialog dropped below the Task 48 size target
- new risk introduced: medium-low; `_apply_profile_data` remains a warning-level method, but it is now localized and covered by round-trip/migration characterization

