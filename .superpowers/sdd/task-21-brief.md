# Task 21: Route Dialog Adapter And Session Controls

## Goal

Reduce `main.py` physical LOC, reduce cyclomatic complexity, and improve Maintainability Index/locality by moving Route Measurement dialog opening/defaults/session planning out of `Main` and into a focused route module.

This task is part of the LOC-gated architecture plan in `docs/superpowers/plans/2026-06-26-main-loc-reduction.md`.

## Baseline

Baseline commit: `e2da4bf`.

- `main.py` physical lines: `12313`.
- Target method: `Main._open_route_measurement_dialog` is 107 NLOC / CCN 13 / length 108.
- Neighboring route dialog/session methods in scope:
  - `_restore_route_measurement_state_after_design_load`
  - `_load_route_measurement_settings`
  - `_route_measurement_settings_store`
  - `_route_measurement_current_point_from_settings`
  - `_route_measurement_session_active_from_settings`
  - `_route_measurement_settings_match_route`
  - `_clear_route_measurement_dialog`
  - `_start_route_measurement_session`
  - `_cancel_route_measurement_session`

## Current Behavior

Preserve these behaviors exactly unless a test reveals a documented bug:

- If no route or route points exist, opening the route measurement dialog shows `Create or load a probe route before measuring.` with timeout `5000` and returns.
- Default CSV/photo paths:
  - no route path and no document: `probe_route_measurements.csv`, `probe_route_photos`;
  - route path exists: sibling `<route-stem>-measurements.csv`, `<route-stem>-photos`;
  - document path exists and route path missing: document parent `probe_route_measurements.csv`, `probe_route_photos`.
- First dialog creation passes route name, point count, default paths, current meter type, settings path, and parent.
- Existing dialog update calls `set_route(...)`.
- Signal wiring remains equivalent:
  - `measure_requested -> _start_route_measurement`
  - `start_session_requested -> _start_route_measurement_session`
  - `cancel_session_requested -> _cancel_route_measurement_session`
  - confirmation buttons call `_submit_route_measurement_confirmation(...)`
  - shift/interruption/pause/stop/jump/move/current-point callbacks unchanged
  - `finished -> _clear_route_measurement_dialog`
- Dialog state sync after open:
  - `set_measurement_session_active(self._route_measurement_session_active, save=False)`
  - current point set when `_route_measurement_current_point is not None`
  - active thread sets `running=True` and current waiting state
  - session-active dialog with no active thread sets status `Choose a point, then Measure or Move.`
- `show`, `raise_`, and `activateWindow` are still called.
- When `start_context=True` and no route thread is alive, `current_configuration()` is used to start a waiting route measurement.
- Restoring after design load:
  - no route/points returns;
  - load persisted route settings;
  - session active is false if persisted settings do not match route;
  - current point is restored via `_set_route_measurement_resume_point`;
  - active session schedules `QTimer.singleShot(0, self._open_route_measurement_dialog)`.
- Clearing dialog while runner thread is alive sets `_route_measurement_context_close_requested=True` and calls `runner.stop()`.
- Start session:
  - rejects active thread with `Route measurement is already active.` timeout `4000`;
  - current point comes from dialog configuration when present, else `_route_measurement_current_point or 1`;
  - marks session active/pending, saves metadata, status `Route point set to point N.` timeout `5000`.
- Cancel session:
  - rejects active thread with `Stop route measurement before canceling the session.` timeout `5000`;
  - clears pending point, deactivates session, resume point becomes `1`, pending false, status `Route measurement session cancelled.` timeout `5000`.

## Structural Improvement

Create `probe_station_gui/route/dialog_adapter.py` as the deep module for route dialog defaults and route session UI plans.

Allowed pure helpers/dataclasses:

- `RouteDialogDefaults`
- `route_dialog_defaults(route, document) -> RouteDialogDefaults`
- `RouteDialogRestorePlan`
- `route_dialog_restore_plan(state, route) -> RouteDialogRestorePlan`
- `RouteMeasurementSessionStartPlan`
- `route_measurement_session_start_plan(*, thread_active, dialog_configuration, current_point) -> RouteMeasurementSessionStartPlan`
- `RouteMeasurementSessionCancelPlan`
- `route_measurement_session_cancel_plan(*, thread_active) -> RouteMeasurementSessionCancelPlan`
- `RouteDialogOpenState`
- `route_dialog_open_state(*, session_active, current_point, thread_active, waiting) -> RouteDialogOpenState`

It is acceptable to add a small `Main` helper that creates/wires the dialog if that removes real duplication, but do not merely move 100 lines from `_open_route_measurement_dialog` into another `Main` method. The task must move meaningful code out of `main.py`.

Do not move Qt signal emission ownership, runner/thread stop side effects, or callback method implementations out of `Main` in this task.

## LOC Gate

- Required: `main.py` physical lines after this task must be at least `150` lines lower than baseline `12313`.
- Target: `main.py <= 12130`.
- If the worker cannot hit `main.py <= 12163`, return `DONE_WITH_CONCERNS` with exact reason instead of committing a weak pass.
- CC gate: `_open_route_measurement_dialog` must either leave `main.py` or drop below lizard warning thresholds; any replacement `Main` helpers must stay below CCN 10 and length 50 unless explicitly justified.
- MI gate: report Wily/radon Maintainability Index for `main.py` and `probe_station_gui\route\dialog_adapter.py`. If MI worsens, explain whether locality/LOC/CC improvements justify the tradeoff.

## Tests

Use TDD for new helpers:

- Add `tests/route/test_dialog_adapter.py` first with failing tests for:
  - default paths from route path, document path, and fallback;
  - restore plan invalidates stale route settings;
  - start-session plan active-thread rejection and accepted dialog/current-point fallback;
  - cancel-session plan active-thread rejection and accepted reset state.
- Add or update app characterization in `tests/app/test_main_coordinate_feedrate.py` for:
  - opening without route shows existing status and does not create dialog;
  - existing dialog update calls `set_route(...)` and then state sync;
  - start-context opens dialog and starts waiting route measurement when no active thread;
  - clear dialog with active runner requests context close and stops runner;
  - cancel session behavior remains persisted, extending existing `test_cancel_route_measurement_session_clears_persisted_state` if useful.

Focused commands:

```powershell
C:\Users\Public\code\probe_station_gui\.venv\Scripts\python.exe -m pytest tests\route\test_dialog_adapter.py -q
C:\Users\Public\code\probe_station_gui\.venv\Scripts\python.exe -m pytest tests\route\test_dialog_adapter.py tests\app\test_main_coordinate_feedrate.py -q
```

Final commands:

```powershell
C:\Users\Public\code\probe_station_gui\.venv\Scripts\python.exe -m pytest tests
C:\Users\Public\code\probe_station_gui\.venv\Scripts\python.exe -m ruff check --ignore E402,F401 .
```

Metrics commands:

```powershell
$env:PYTHONIOENCODING='utf-8'
$env:PYTHONUTF8='1'
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8
C:\Users\Public\code\probe_station_gui\.venv\Scripts\python.exe -m lizard main.py probe_station_gui\route -l python -C 10 -L 50 --sort cyclomatic_complexity
C:\Users\Public\code\probe_station_gui\.venv\Scripts\python.exe -m radon cc -s main.py probe_station_gui\route\dialog_adapter.py
C:\Users\Public\code\probe_station_gui\.venv\Scripts\python.exe -X utf8 -m wily -c $env:TEMP\probe_station_gui_wily_task21 build main.py probe_station_gui\route\dialog_adapter.py
C:\Users\Public\code\probe_station_gui\.venv\Scripts\python.exe -X utf8 -m wily -c $env:TEMP\probe_station_gui_wily_task21 diff main.py probe_station_gui\route\dialog_adapter.py --detail -r e2da4bf --metrics cyclomatic.complexity,raw.loc,maintainability.mi --no-wrap
```

## Report

Write `.superpowers/sdd/task-21-report.md` with:

- exact `main.py` physical line count before/after and delta;
- target method lizard/radon before/after;
- Wily diff;
- tests and ruff results;
- TDD RED/GREEN evidence;
- verdict:
  - behavior preserved: yes/no;
  - tests passed: yes/no;
  - metrics improved: yes/no;
  - LOC gate passed: yes/no;
  - maintainability improvement;
  - new risk introduced.
