# Task 14 Report

## Status

DONE

## Changed Files

- `main.py`
- `probe_station_gui/route/adjustment_flow.py`
- `tests/route/test_adjustment_flow.py`
- `.superpowers/sdd/task-14-report.md`

## Commit Hashes

- `e7ab130` - Extract route adjustment flow planning.

## Tests Run

- `C:\Users\Public\code\probe_station_gui\.venv\Scripts\python.exe -m pytest tests\route\test_adjustment_flow.py`
  - Result: 7 passed in 0.07s.
- `C:\Users\Public\code\probe_station_gui\.venv\Scripts\python.exe -m pytest tests\route\test_adjustment_flow.py tests\route\test_operation_guards.py tests\route\test_session_actions.py`
  - Result: 17 passed in 0.09s.
- `C:\Users\Public\code\probe_station_gui\.venv\Scripts\python.exe -m pytest tests\app\test_main_coordinate_feedrate.py`
  - First result: 6 failed, 91 passed in 3.98s. Root cause: the new `Main` adapter wiring read `_route_measurement_waiting` and `_route_measurement_current_point` earlier than the old branches did in partial `Main.__new__` tests.
  - Final result: 97 passed in 1.58s.
- `C:\Users\Public\code\probe_station_gui\.venv\Scripts\python.exe -m pytest tests\route tests\app\test_main_coordinate_feedrate.py tests\ui\test_design_navigator_panel.py`
  - Result: 304 passed in 4.16s.
- `C:\Users\Public\code\probe_station_gui\.venv\Scripts\python.exe -m pytest tests`
  - Result: 788 passed, 2 skipped in 10.76s.
- `C:\Users\Public\code\probe_station_gui\.venv\Scripts\python.exe -m ruff check --ignore E402,F401 .`
  - Result: All checks passed.

## Metrics

Wily was run first with UTF-8 output and cache outside the repo:

- Build command: `$env:PYTHONIOENCODING='utf-8'; $cache=Join-Path $env:TEMP 'probe_station_gui_wily_task14'; C:\Users\Public\code\probe_station_gui\.venv\Scripts\python.exe -m wily -c $cache build main.py probe_station_gui\route`
- Diff command: `$env:PYTHONIOENCODING='utf-8'; $cache=Join-Path $env:TEMP 'probe_station_gui_wily_task14'; C:\Users\Public\code\probe_station_gui\.venv\Scripts\python.exe -m wily -c $cache diff main.py probe_station_gui\route\adjustment_flow.py --detail -r HEAD --metrics cyclomatic.complexity,raw.loc,maintainability.mi --no-wrap`

Wily diff against `114d781`:

- `main.py`: cyclomatic complexity 2420 -> 2412; LOC 12308 -> 12296; MI 0 -> 0.
- `probe_station_gui\route\adjustment_flow.py`: new file, cyclomatic complexity 32; LOC 186; MI 36.73564491618881.
- `main.py:Main._save_route_measurement_shift`: cyclomatic complexity 32 -> 27.
- `main.py:Main._request_route_contact_move`: cyclomatic complexity 13 -> 10.
- `probe_station_gui\route\adjustment_flow.py:route_shift_save_plan`: new, cyclomatic complexity 8.
- `probe_station_gui\route\adjustment_flow.py:route_contact_move_plan`: new, cyclomatic complexity 6.
- `probe_station_gui\route\adjustment_flow.py:route_confirmation_submission_plan`: new, cyclomatic complexity 4.

Radon before:

- `main.py:Main._submit_route_measurement_confirmation`: C (17).
- `main.py:Main._request_route_contact_move`: C (13).
- `main.py:Main._save_route_measurement_shift`: E (32).
- `main.py:Main._on_route_measurement_finished`: D (26).

Radon after:

- `main.py:Main._submit_route_measurement_confirmation`: C (17).
- `main.py:Main._request_route_contact_move`: B (10).
- `main.py:Main._save_route_measurement_shift`: D (27).
- `main.py:Main._on_route_measurement_finished`: D (26), unchanged.
- `probe_station_gui.route.adjustment_flow.route_confirmation_submission_plan`: A (4).
- `probe_station_gui.route.adjustment_flow.route_contact_move_plan`: B (6).
- `probe_station_gui.route.adjustment_flow.route_shift_save_plan`: B (8).

Lizard before:

- `_submit_route_measurement_confirmation`: NLOC 76, CCN 17, length 76.
- `_request_route_contact_move`: NLOC 42, CCN 13, length 42.
- `_save_route_measurement_shift`: NLOC 101, CCN 32, length 101.
- `_on_route_measurement_finished`: NLOC 107, CCN 26, length 107.

Lizard after:

- `_submit_route_measurement_confirmation`: NLOC 78, CCN 17, length 78.
- `_request_route_contact_move`: NLOC 37, CCN 10, length 37.
- `_save_route_measurement_shift`: NLOC 94, CCN 27, length 94.
- `_on_route_measurement_finished`: NLOC 107, CCN 26, length 107.
- `route_confirmation_submission_plan`: NLOC 29, CCN 4, length 29.
- `route_contact_move_plan`: NLOC 32, CCN 6, length 32.
- `route_shift_save_plan`: NLOC 45, CCN 8, length 46.

## Verdict

The refactor is justified. `Main` remains the Qt, runner, and hardware side-effect adapter, while confirmation preflight, contact-move guard planning, and shift-save guard/path planning now live in a focused route module with direct tests. Two targeted `main.py` hotspots improved materially: `_request_route_contact_move` dropped from CC 13 to 10 and from 42 to 37 lizard NLOC; `_save_route_measurement_shift` dropped from CC 32 to 27 and from 101 to 94 lizard NLOC.

## Concerns / Residual Gaps

- `_submit_route_measurement_confirmation` still has CC 17 because restart/runtime-settings/meter side effects remain in `Main`. This is intentional for this pass, but it remains a future extraction candidate.
- `_on_route_measurement_finished` was not touched in this slice; finish cleanup remains complex and should be handled by a separate focused pass if needed.

## Fix after review

### Changed Files

- `main.py`
- `probe_station_gui/route/adjustment_flow.py`
- `tests/app/test_main_coordinate_feedrate.py`
- `.superpowers/sdd/task-14-report.md`

### Commit Hashes

- Pending fix commit.

### Tests Run

- `C:\Users\Public\code\probe_station_gui\.venv\Scripts\python.exe -m pytest tests\app\test_main_coordinate_feedrate.py::MainCoordinateFeedrateTest::test_blocked_route_shift_save_does_not_read_dialog_configuration`
  - First result: 1 failed in 0.90s. The fake dialog recorded a `current_configuration()` call before the blocked API Route Control guard returned.
  - Final result: 1 passed in 0.76s.
- `C:\Users\Public\code\probe_station_gui\.venv\Scripts\python.exe -m pytest tests\route tests\app\test_main_coordinate_feedrate.py tests\ui\test_design_navigator_panel.py`
  - Result: 305 passed in 4.14s.
- `C:\Users\Public\code\probe_station_gui\.venv\Scripts\python.exe -m ruff check --ignore E402,F401 .`
  - Result: All checks passed.

### Residual Concerns

- No additional concerns from the review fix. The accepted shift-save point precedence remains requested point, dialog current point, then stored current point; blocked shift-save now returns before dialog configuration is read.
