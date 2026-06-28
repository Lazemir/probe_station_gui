# Task 48e Report: Route Measurement Profile Apply Cleanup

## Scope

Current behavior:

- `RouteMeasurementProfileMixin._apply_profile_data()` applied saved route measurement profile fields to the dialog, including path fallback, operation mode, photo settings, measurement counts, session state, contact settings, meter settings, and legacy Keithley defaults migration.
- If `previous_csv_path` was absent, a non-empty `csv_path` also populated the previous CSV path.
- Legacy Keithley profiles still migrated route-count and Keithley defaults.

Structural improvement:

- Kept the public profile interface stable: `_apply_profile_data(data) -> bool`.
- Moved profile text/path normalization into `_profile_text()` and `_apply_profile_paths()`.
- Moved nested meter profile application into `_apply_meter_profile()`.
- Reused the existing ratio-percent spinbox helper for `max_relative_rms` instead of keeping custom conversion logic inside `_apply_profile_data()`.
- Added a characterization test for `csv_path` to `previous_csv_path` fallback.

Public API changed: no.

## Metrics

Wily ran from disposable UTF-8 temp clone/cache:

- Temp root: `C:\Users\Lazemir\AppData\Local\Temp\probe_station_gui_wily_task48e_20260628164143`
- Baseline: `5d1a8b2`

| Metric | Before | After | Better? |
| --- | ---: | ---: | --- |
| `route_measurement_profile.py` Wily LOC | 441 | 451 | no |
| `route_measurement_profile.py` Wily cyclomatic | 77 | 76 | yes |
| `route_measurement_profile.py` MI | 18.717 | 18.5903 | no |
| `_apply_profile_data` CCN | 16 | 5 | yes |
| Radon average complexity | 3.5 | 3.04 | yes |
| Lizard warnings in profile module | 1 | 0 | yes |
| Lizard warnings in Task 48 slice | 1 | 0 | yes |
| Focused profile/dialog tests | 28 passed | 29 passed | yes |
| Tests | 1216 passed, 2 skipped | 1217 passed, 2 skipped | yes |
| Coverage total | 73% | 73% | flat |
| Ruff errors | 0 | 0 | flat |

Remaining lizard warnings in the Task 48 slice:

- None.

## Validation

- `.\.venv\Scripts\python.exe -m pytest tests\route\test_route_measurement_profile.py tests\route\test_dialog_adapter.py tests\route\test_run_ui.py`
  - `29 passed`
- `.\.venv\Scripts\python.exe -m ruff check --ignore E402,F401 probe_station_gui\dialogs\route_measurement_profile.py tests\route\test_route_measurement_profile.py`
  - clean
- `.\.venv\Scripts\python.exe -m radon cc -s -a probe_station_gui\dialogs\route_measurement_profile.py`
  - average complexity `A (3.04)`, `_apply_profile_data` `A (5)`
- `.\.venv\Scripts\python.exe -m radon mi -s probe_station_gui\dialogs\route_measurement_profile.py`
  - `B (18.59)`
- `.\.venv\Scripts\python.exe -m lizard -w probe_station_gui\dialogs\route_measurement_dialog.py probe_station_gui\dialogs\route_measurement_profile.py probe_station_gui\dialogs\route_measurement_result_views.py probe_station_gui\dialogs\route_measurement_widgets.py probe_station_gui\dialogs\route_measurement_operation_state.py`
  - no warnings
- `.\.venv\Scripts\python.exe -m coverage run -m pytest tests`
  - `1217 passed, 2 skipped`
- `.\.venv\Scripts\python.exe -m coverage report -m --ignore-errors`
  - total coverage `73%`
- `.\.venv\Scripts\python.exe -m ruff check --ignore E402,F401 .`
  - clean

## Verdict

This refactor is justified.

Reason:

- behavior preserved: yes
- tests passed: yes
- metrics improved: yes, max-function complexity and aggregate Wily cyclomatic both decreased
- maintainability improvement: profile path normalization and nested meter application now have local helpers, and Task 48 has no remaining lizard warning-level functions
- new risk introduced: low; LOC increased by 10 and MI dipped slightly, but the changed fallback behavior is explicitly covered by a new test
