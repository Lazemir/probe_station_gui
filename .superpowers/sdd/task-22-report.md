# Task 22 Report: Route Runtime Presentation Sink

## Summary

- Added `probe_station_gui.route.runtime_presenter` as the broader Task 22 seam.
- Kept `probe_station_gui.route.dialog_adapter` focused on dialog creation/default/session helpers from Task 21.
- Moved repeated route runtime presentation fanout for `RouteMeasurementDialog` and `DesignNavigatorPanel` out of `Main`.
- Preserved `Main` ownership of status-bar updates, runner state, persistence fallback, and Pause Request / Pause Ack / Interrupt decisions.
- Passed the Task 22 LOC gate and target.

## TDD Evidence

### RED

New presenter tests were written first in `tests/route/test_runtime_presenter.py`.

Command:

```powershell
C:\Users\Public\code\probe_station_gui\.venv\Scripts\python.exe -m pytest tests\route\test_runtime_presenter.py -q
```

Observed failure before implementation:

```text
ModuleNotFoundError: No module named 'probe_station_gui.route.runtime_presenter'
```

### GREEN

Presenter-focused tests:

```powershell
C:\Users\Public\code\probe_station_gui\.venv\Scripts\python.exe -m pytest tests\route\test_runtime_presenter.py -q
```

Result:

```text
4 passed in 0.05s
```

Focused route/app verification:

```powershell
C:\Users\Public\code\probe_station_gui\.venv\Scripts\python.exe -m pytest tests\route\test_dialog_adapter.py tests\route\test_runtime_presenter.py tests\app\test_main_coordinate_feedrate.py -q
```

Result:

```text
141 passed, 3 subtests passed in 1.77s
```

## Exact LOC Verification

Baseline command:

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
11968
```

### LOC Gate Result

- `main.py` physical lines before: `12159`
- `main.py` physical lines after: `11968`
- Delta: `-191`
- Required minimum: `<= 12009`
- Target: `<= 11979`
- Gate result: `passed`
- Target result: `passed`

## Runtime Presenter Scope

`RouteRuntimePresentationSink` now owns the repeated presentation fanout for:

- API route-control updates
- route runner started
- API/GUI route started
- stop requested
- route status fanout
- confirmation waiting clear
- pause requested
- route shift save status
- waiting-state changes
- unsaved/recorded result status
- finish UI
- dialog current-point/session-active updates with `Main` persistence fallback preserved

The dialog-only sink remains as an internal helper inside `runtime_presenter.py`; `dialog_adapter.py` now stays focused on dialog setup/session helper responsibilities.

## Target Fanout Methods

### Lizard before/after

Baseline values come from the task brief where provided.

| Method | Before NLOC / CCN | After NLOC / CCN |
| --- | --- | --- |
| `_update_api_route_control_ui` | `30 / 5` | `6 / 1` |
| `_start_route_measurement_runner` | `49 / 4` | `40 / 2` |
| `_on_route_measurement_started` | `31 / 4` | `17 / 2` |
| `_request_stop_route_measurement` | not called out | `13 / 3` |
| `_request_pause_route_measurement` | `37 / 9` | `22 / 5` |
| `_on_route_measurement_result` | `44 / 6` | `35 / 3` |
| `_on_route_measurement_recorded` | `20 / 4` | `17 / 2` |
| `_apply_route_measurement_finished_ui` | `14 / 3` | removed |
| `_on_route_measurement_finished` | not called out | `45 / 5` |
| `_apply_route_shift_save_status` | not called out | `4 / 1` |
| `_on_route_measurement_progress` | not called out | `8 / 1` |

### Radon after

| Method | After |
| --- | --- |
| `_update_api_route_control_ui` | `A (1)` |
| `_start_route_measurement_runner` | `A (2)` |
| `_on_route_measurement_started` | `A (2)` |
| `_request_stop_route_measurement` | `A (3)` |
| `_request_pause_route_measurement` | `A (5)` |
| `_on_route_measurement_result` | `A (3)` |
| `_on_route_measurement_recorded` | `A (2)` |
| `_on_route_measurement_finished` | `A (5)` |
| `_apply_route_shift_save_status` | `A (1)` |
| `_on_route_measurement_progress` | `A (1)` |
| `_route_runtime_presenter` | `A (2)` |

## Metrics

### Lizard summary

Command:

```powershell
$env:PYTHONIOENCODING='utf-8'
$env:PYTHONUTF8='1'
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8
C:\Users\Public\code\probe_station_gui\.venv\Scripts\python.exe -m lizard main.py probe_station_gui\route\dialog_adapter.py probe_station_gui\route\runtime_presenter.py -l python -C 10 -L 50 --sort cyclomatic_complexity
```

Relevant file summary:

```text
main.py                                    NLOC 11345  Avg.NLOC 21.2  AvgCCN 4.5
probe_station_gui\route\dialog_adapter.py  NLOC   404  Avg.NLOC 20.6  AvgCCN 2.9
probe_station_gui\route\runtime_presenter.py NLOC 268 Avg.NLOC 6.0 AvgCCN 1.8
```

### Radon MI

Command:

```powershell
$env:PYTHONIOENCODING='utf-8'
$env:PYTHONUTF8='1'
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8
C:\Users\Public\code\probe_station_gui\.venv\Scripts\python.exe -m radon mi -s main.py probe_station_gui\route\dialog_adapter.py probe_station_gui\route\runtime_presenter.py
```

Result:

```text
main.py - C (0.00)
probe_station_gui\route\dialog_adapter.py - A (25.62)
probe_station_gui\route\runtime_presenter.py - A (23.51)
```

### Wily diff from `bc691e3`

`wily build` was attempted with the exact brief command, but it refused to archive while the repository also contained an unrelated modified file at `docs/superpowers/plans/2026-06-26-main-loc-reduction.md`. I did not revert or commit that user-owned change. The `wily diff` command still succeeded and provided the comparative metrics below.

Command:

```powershell
C:\Users\Public\code\probe_station_gui\.venv\Scripts\python.exe -X utf8 -m wily -c $env:TEMP\probe_station_gui_wily_task22 diff main.py probe_station_gui\route\dialog_adapter.py probe_station_gui\route\runtime_presenter.py --detail -r bc691e3 --metrics cyclomatic.complexity,raw.loc,maintainability.mi --no-wrap
```

Relevant output:

```text
main.py: cyclomatic 2371 -> 2340, raw.loc 12159 -> 11968, MI 0 -> 0
probe_station_gui\route\runtime_presenter.py: cyclomatic - -> 82, raw.loc - -> 318, MI - -> 23.514177001044143
main.py:Main._update_api_route_control_ui 5 -> 1
main.py:Main._start_route_measurement_runner 4 -> 2
main.py:Main._on_route_measurement_started 4 -> 2
main.py:Main._request_stop_route_measurement 5 -> 3
main.py:Main._request_pause_route_measurement 9 -> 5
main.py:Main._on_route_measurement_result 6 -> 3
main.py:Main._on_route_measurement_recorded 4 -> 2
main.py:Main._apply_route_shift_save_status 7 -> 1
main.py:Main._on_route_measurement_progress 2 -> 1
```

## Verification

### Focused tests

```powershell
C:\Users\Public\code\probe_station_gui\.venv\Scripts\python.exe -m pytest tests\route\test_runtime_presenter.py -q
C:\Users\Public\code\probe_station_gui\.venv\Scripts\python.exe -m pytest tests\route\test_dialog_adapter.py tests\route\test_runtime_presenter.py tests\app\test_main_coordinate_feedrate.py -q
```

Result:

```text
4 passed in 0.05s
141 passed, 3 subtests passed in 1.77s
```

### Full test suite

```powershell
C:\Users\Public\code\probe_station_gui\.venv\Scripts\python.exe -m pytest tests
```

Result:

```text
852 passed, 2 skipped in 11.11s
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

- Re-read the revised Task 22 brief after the controller rejection and expanded scope update.
- Moved the public seam out of `dialog_adapter.py` into the new `runtime_presenter.py` module.
- Kept the earlier sink work, but narrowed `dialog_adapter.py` back to dialog/session duties.
- Re-ran the exact Python `splitlines()` LOC commands during each reduction pass.
- Reduced `main.py` from the rejected `12131` state to `12063`, then to `11991`, then to the final `11968`.
- Updated the one app characterization test that referenced a deleted forwarding helper, without changing runtime behavior.

## Maintainability Outcome

- Route runtime presentation behavior is now concentrated behind a single route-owned seam instead of being fanned out across many `Main` branches.
- `Main` route methods are materially smaller, especially the API route-control UI update, runner-started/start/status/result/finish presentation paths, and the dialog persistence fallback paths.
- `dialog_adapter.py` remains cohesive after the Task 22 expansion instead of becoming a shallow bucket for unrelated route UI updates.

## New Risk Introduced

- Low. The new presenter adds an extra indirection layer, but it is narrow, fully characterized by focused tests, and does not own the safety-critical pause/interrupt decisions.

## Verdict

- behavior preserved: `yes`
- tests passed: `yes`
- metrics improved: `yes`
- LOC gate passed: `yes`
- maintainability improvement: `yes`
- new risk introduced: `low`
