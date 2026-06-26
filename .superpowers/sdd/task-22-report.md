# Task 22 Report: Route Dialog Runtime State Sink

## Summary

- Added `RouteDialogRuntimeSink` to `probe_station_gui.route.dialog_adapter`.
- Moved repeated route-dialog runtime fanout from `main.py` into the sink.
- Added sink-focused TDD coverage in `tests/route/test_dialog_adapter.py`.
- Verified behavior with focused and full test suites.
- Corrected the LOC accounting: Task 22 does **not** pass the LOC gate.

## TDD Evidence

### RED

Command:

```powershell
C:\Users\Public\code\probe_station_gui\.venv\Scripts\python.exe -m pytest tests\route\test_dialog_adapter.py -q
```

Observed failure before sink implementation:

```text
ImportError: cannot import name 'RouteDialogRuntimeSink' from 'probe_station_gui.route.dialog_adapter'
```

### GREEN

Focused sink tests:

```powershell
C:\Users\Public\code\probe_station_gui\.venv\Scripts\python.exe -m pytest tests\route\test_dialog_adapter.py -q
```

Result:

```text
17 passed in 0.08s
```

Focused route/app characterization:

```powershell
C:\Users\Public\code\probe_station_gui\.venv\Scripts\python.exe -m pytest tests\route\test_dialog_adapter.py tests\app\test_main_coordinate_feedrate.py -q
```

Result from the final verification pass:

```text
141 passed, 3 subtests passed in 1.84s
```

## Exact LOC Verification

Baseline command from the task brief review:

```powershell
git show bc691e3:main.py | C:\Users\Public\code\probe_station_gui\.venv\Scripts\python.exe -c "import sys; print(len(sys.stdin.read().splitlines()))"
```

Result:

```text
12159
```

Current file command:

```powershell
C:\Users\Public\code\probe_station_gui\.venv\Scripts\python.exe -c "from pathlib import Path; print(len(Path('main.py').read_text(encoding='utf-8').splitlines()))"
```

Result:

```text
12131
```

Diff command:

```powershell
git diff --numstat bc691e3..HEAD -- main.py
```

Result:

```text
66  94  main.py
```

### LOC Gate Result

- `main.py` physical lines before: `12159`
- `main.py` physical lines after: `12131`
- Delta: `-28`
- Required minimum: `<= 12009`
- Target: `<= 11979`
- Gate result: `failed`

## Target Fanout Methods

### Lizard before/after

| Method | Before NLOC / CCN | After NLOC / CCN |
| --- | --- | --- |
| `_update_api_route_control_ui` | `30 / 5` | `20 / 3` |
| `_start_route_measurement_runner` | `49 / 4` | `49 / 3` |
| `_on_route_measurement_started` | `31 / 4` | `29 / 3` |
| `_show_route_measurement_status` | `13 / 4` | `12 / 3` |
| `_request_pause_route_measurement` | `37 / 9` | `31 / 7` |
| `_on_route_measurement_result` | `44 / 6` | `42 / 4` |
| `_on_route_measurement_recorded` | `20 / 4` | `19 / 3` |
| `_apply_route_measurement_finished_ui` | `14 / 3` | `11 / 2` |

### Radon before/after

| Method | Before | After |
| --- | --- | --- |
| `_update_api_route_control_ui` | `B (5)` | `A (3)` |
| `_start_route_measurement_runner` | `A (4)` | `A (3)` |
| `_on_route_measurement_started` | `A (4)` | `A (3)` |
| `_show_route_measurement_status` | `A (4)` | `A (3)` |
| `_request_pause_route_measurement` | `B (9)` | `B (7)` |
| `_on_route_measurement_result` | `B (6)` | `A (4)` |
| `_on_route_measurement_recorded` | `A (4)` | `A (3)` |
| `_apply_route_measurement_finished_ui` | `A (3)` | `A (2)` |

## Metrics

### Wily diff from `bc691e3`

Command:

```powershell
C:\Users\Public\code\probe_station_gui\.venv\Scripts\python.exe -X utf8 -m wily -c $env:TEMP\probe_station_gui_wily_task22 diff main.py probe_station_gui\route\dialog_adapter.py --detail -r bc691e3 --metrics cyclomatic.complexity,raw.loc,maintainability.mi --no-wrap
```

Relevant output:

```text
main.py: cyclomatic 2371 -> 2350, raw.loc 12159 -> 12131, MI 0 -> 0
probe_station_gui\route\dialog_adapter.py: cyclomatic 51 -> 86, raw.loc 454 -> 612, MI 25.6219 -> 17.3023
main.py:Main._request_pause_route_measurement 9 -> 7
main.py:Main._on_route_measurement_result 6 -> 4
main.py:Main._update_api_route_control_ui 5 -> 3
main.py:Main._start_route_measurement_runner 4 -> 3
main.py:Main._on_route_measurement_started 4 -> 3
main.py:Main._show_route_measurement_status 4 -> 3
main.py:Main._on_route_measurement_recorded 4 -> 3
main.py:Main._apply_route_measurement_finished_ui 3 -> 2
```

## Verification

### Focused tests

```powershell
C:\Users\Public\code\probe_station_gui\.venv\Scripts\python.exe -m pytest tests\route\test_dialog_adapter.py tests\app\test_main_coordinate_feedrate.py -q
```

Result:

```text
141 passed, 3 subtests passed in 1.84s
```

### Full test suite

```powershell
C:\Users\Public\code\probe_station_gui\.venv\Scripts\python.exe -m pytest tests
```

Result:

```text
852 passed, 2 skipped in 11.27s
```

### Ruff

```powershell
C:\Users\Public\code\probe_station_gui\.venv\Scripts\python.exe -m ruff check --ignore E402,F401 .
```

Result:

```text
All checks passed!
```

## Fix-Loop Evidence

- Re-ran the exact Python `splitlines()` LOC commands requested by the controller review.
- Confirmed the original report’s `11514` figure was wrong for task gating.
- Tried one additional in-scope extraction pass centered on route status/pending/resume helpers in `dialog_adapter.py`.
- Measured the result immediately with the same exact `splitlines()` command.
- That pass made `main.py` longer instead of shorter, so it was reverted before final verification.

## Scope Judgment

Task 22’s accepted implementation seam is real for complexity reduction, but it is not wide enough to remove another `122` physical lines from `main.py` honestly without crossing the task boundary.

Why:

- The dialog-only fanout is already concentrated in `RouteDialogRuntimeSink`.
- The remaining route-runtime methods in `main.py` are dominated by:
  - `DesignNavigatorPanel` updates,
  - global status bar updates,
  - runner/thread lifecycle,
  - Telegram side effects,
  - persistence/state ownership in `Main`.
- Moving enough additional lines to hit the gate would require either:
  - moving `DesignNavigatorPanel` presentation into adapter helpers, which the brief explicitly marked out of scope, or
  - turning `dialog_adapter.py` into a shallow callback bucket that merely hosts `Main` logic elsewhere.

Given those constraints, I do not have an honest path to `main.py <= 12009` within Task 22.

## Maintainability Outcome

- Route dialog runtime fanout is better isolated than baseline.
- The targeted fanout methods are smaller and simpler.
- External behavior stayed stable under focused and full verification.
- The LOC gate requirement for Task 22 remains unmet.

## New Risk Introduced

- None identified beyond the already-tested sink indirection.

## Verdict

- behavior preserved: `yes`
- tests passed: `yes`
- metrics improved: `partially`
- LOC gate passed: `no`
- maintainability improvement: `yes`
- new risk introduced: `low`
