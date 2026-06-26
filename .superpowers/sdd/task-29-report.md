# Task 29 Report: Stage Position Update Fanout

## Summary

- Split `stage_position_display_plan(...)` into small pure helpers inside [probe_station_gui/stage/position_presenter.py](/C:/Users/Public/code/probe_station_gui/probe_station_gui/stage/position_presenter.py).
- Kept all `QLineEdit` mutation, style application, cache mutation, motion clearing, and availability toggles in [main.py](/C:/Users/Public/code/probe_station_gui/main.py).
- Reduced the warning-level presenter function to `A (4)` and pulled `Main._update_stage_position_display(...)` below the warning threshold.
- Satisfied the immediate Task 29 no-growth stop condition for `main.py`, but did not satisfy the formal LOC gate.

## Physical LOC

Verified with Python `splitlines()` physical line counts, which match the task brief/Wily measurement:

- Baseline commit `5fe6109` `main.py`: `11004`
- Current `main.py`: `10997`
- Delta: `-7`

Result:

- Immediate no-growth stop condition: `passed`
- Formal Task 29 LOC gate `main.py <= 10744`: `not passed`
- Exception basis: `yes`; target complexity/warning counts improved materially without crossing into later extraction tasks

## Target Method Metrics

### `Main._on_stage_position_changed`

- Baseline lizard: `118 NLOC / CCN 39`
- Prior inherited Task 29 state: `115 NLOC / CCN 26`
- Current lizard: `115 NLOC / CCN 26`
- Baseline radon: `E (39)`
- Prior inherited Task 29 state: `D (26)`
- Current radon: `D (26)`

### `Main._update_stage_position_display`

- Baseline lizard: `73 NLOC / CCN 13`
- Prior inherited Task 29 state: `68 NLOC / CCN 12`
- Current lizard: `65 NLOC / CCN 10`
- Baseline radon: `C (13)`
- Prior inherited Task 29 state: `C (11)`
- Current radon: `B (9)`

### `stage_position_display_plan`

- Prior inherited Task 29 state: `69 NLOC / CCN 14`, radon `C (14)`
- Current lizard: `42 NLOC / CCN 4`
- Current radon: `A (4)`

## Fresh Spot Checks

Radon:

- `Main._on_stage_position_changed`: `D (26)`
- `Main._update_stage_position_display`: `B (9)`
- `stage_position_display_plan`: `A (4)`

Lizard:

- `Main._on_stage_position_changed`: `115 NLOC / CCN 26`
- `Main._update_stage_position_display`: `65 NLOC / CCN 10`
- `stage_position_display_plan`: `42 NLOC / CCN 4`

MI:

- `main.py`: `C (0.00)`
- `probe_station_gui/stage/position_presenter.py`: `A (23.04)`

## Wily

Wily was rerun after the cleanup from a disposable UTF-8 temp clone:

- Clone: `C:\Users\Lazemir\AppData\Local\Temp\probe_station_gui_wily_task29_report_fix`
- Config/cache: `C:\Users\Lazemir\AppData\Local\Temp\probe_station_gui_wily_task29_report_fix_config`
- Commands exited `0`:
  - `C:\Users\Public\code\probe_station_gui\.venv\Scripts\python.exe -X utf8 -m wily -c <temp-config> build main.py probe_station_gui\stage\position_presenter.py`
  - `C:\Users\Public\code\probe_station_gui\.venv\Scripts\python.exe -X utf8 -m wily -c <temp-config> diff main.py probe_station_gui\stage\position_presenter.py --detail -r 5fe6109 --metrics cyclomatic.complexity,raw.loc,maintainability.mi --no-wrap`

Observed build warnings:

- Wily printed repeated `No data collected` warnings for several older revisions during history build.
- Despite those warnings, the build completed and the requested diff table was produced.

High-signal diff vs `5fe6109`:

- `main.py`: cyclomatic `2148 -> 2131`, raw LOC `11004 -> 10997`, MI `0 -> 0`
- `probe_station_gui/stage/position_presenter.py`: cyclomatic `- -> 67`, raw LOC `- -> 469`, MI `- -> 23.035538720974344`
- `main.py:Main._on_stage_position_changed`: cyclomatic `39 -> 26`
- `main.py:Main._update_stage_position_display`: cyclomatic `13 -> 9`
- `probe_station_gui/stage/position_presenter.py:stage_position_display_plan`: cyclomatic `- -> 4`

## TDD Evidence

### RED

`C:\Users\Public\code\probe_station_gui\.venv\Scripts\python.exe -m pytest tests\stage\test_position_presenter.py -q`

- failed with `TypeError: stage_position_display_plan() got an unexpected keyword argument 'available_axes'`

### GREEN

`C:\Users\Public\code\probe_station_gui\.venv\Scripts\python.exe -m pytest tests\stage\test_position_presenter.py -q`

- passed with `12 passed`

## Fresh Verification

Focused tests:

- `C:\Users\Public\code\probe_station_gui\.venv\Scripts\python.exe -m pytest tests\stage\test_position_presenter.py tests\app\test_main_planned_move_prediction.py tests\app\test_main_coordinate_feedrate.py -q`
  - `177 passed, 3 subtests passed`

Full verification rerun after the cleanup:

- `C:\Users\Public\code\probe_station_gui\.venv\Scripts\python.exe -m pytest tests`
  - `1006 passed, 2 skipped`
- `C:\Users\Public\code\probe_station_gui\.venv\Scripts\python.exe -m ruff check --ignore E402,F401 .`
  - `All checks passed!`

## Verdict

- behavior preserved: `yes`
- tests passed: `yes`
- metrics improved: `yes`
- LOC gate passed: `no`
- maintainability improvement: `yes`, because the presenter now owns the pure normalization/filtering/style/value decisions without leaving a new warning-level function behind
- new risk introduced: `low`; the main remaining hotspot in this area is still `Main._on_stage_position_changed` at `D (26)`, and the branch still depends on a documented LOC-gate exception rather than meeting the formal target directly
