# Task 32 Report

- status: `DONE_WITH_CONCERNS`
- commits created:
  - `refactor: extract coordinate target move state module`
- changed files:
  - `main.py`
  - `probe_station_gui/stage/coordinate_targets.py`
  - `tests/stage/test_coordinate_targets.py`
  - `tests/app/test_main_coordinate_feedrate.py`
  - `tests/app/test_main_planned_move_prediction.py`
  - `.superpowers/sdd/task-32-report.md`

## Behavior Summary

Extracted coordinate target move tracking and planning into `probe_station_gui.stage.coordinate_targets` with a real `CoordinateTargetMoveState` plus pure planning helpers for:

- coordinate target start planning
- feedrate reissue planning
- idle-finish decisions
- common feedrate target calculation
- axis target resolution
- software limit text generation

`Main` now acts as the side-effect adapter for controller calls, Qt focus/timers, joystick feedrate UI, status messages, design-registration invalidation, and position publishing. API/status payload shapes and existing coordinate-move behavior were preserved by characterization tests.

## TDD Red/Green Evidence

- Red:
  - `C:\Users\Public\code\probe_station_gui\.venv\Scripts\python.exe -m pytest tests\stage\test_coordinate_targets.py -q`
  - failed during collection with `ModuleNotFoundError: No module named 'probe_station_gui.stage.coordinate_targets'`
- Green:
  - `C:\Users\Public\code\probe_station_gui\.venv\Scripts\python.exe -m pytest tests\stage\test_coordinate_targets.py -q`
  - `9 passed in 0.07s`
- Focused regression sweep:
  - `C:\Users\Public\code\probe_station_gui\.venv\Scripts\python.exe -m pytest tests\stage\test_coordinate_targets.py tests\stage\test_api_moves.py tests\app\test_main_coordinate_feedrate.py tests\app\test_main_planned_move_prediction.py -q`
  - `193 passed, 3 subtests passed in 2.17s`

## Verification Output Summaries

- Focused verification:
  - new module tests passed
  - coordinate move/API/planned-move characterization tests passed
- Full verification:
  - `C:\Users\Public\code\probe_station_gui\.venv\Scripts\python.exe -m pytest tests`
  - `1048 passed, 2 skipped in 11.34s`
- Ruff:
  - `C:\Users\Public\code\probe_station_gui\.venv\Scripts\python.exe -m ruff check --ignore E402,F401 .`
  - `All checks passed!`

## Metrics

### main.py Physical LOC

- before: `10471`
- after: `10285`
- delta: `-186`
- gate `<=10171`: `FAILED`

### Wily (temp clone/cache, diff vs `7ce5d5f`)

- `main.py`
  - cyclomatic complexity: `2008 -> 1939`
  - raw LOC: `10471 -> 10285`
  - maintainability index: `0 -> 0`
- `probe_station_gui/stage/coordinate_targets.py`
  - cyclomatic complexity: `- -> 111`
  - raw LOC: `- -> 579`
  - maintainability index: `- -> 9.303046718341122`
- `probe_station_gui/stage/manual_jog_prediction.py`
  - no diff reported

### Lizard / Radon Spot Checks

Before metrics are from the task brief baseline. After metrics are from the current working tree.

- `Main._apply_coordinate_move_feedrate`
  - before: lizard `84 NLOC / CCN 21`, radon `D (21)`
  - after: lizard `53 NLOC / CCN 9`, radon `B (9)`
- `Main._start_coordinate_targets_move`
  - before: lizard `89 NLOC / CCN 17`, radon `C (17)`
  - after: lizard `53 NLOC / CCN 8`, radon `B (8)`
- `Main._apply_pending_stage_coordinate_targets`
  - before: lizard `27 NLOC / CCN 10`, radon `B (10)`
  - after: lizard `27 NLOC / CCN 10`, radon `B (10)`
- `Main._finish_coordinate_move_if_idle`
  - before: lizard `19 NLOC / CCN 10`, radon `B (10)`
  - after: lizard `13 NLOC / CCN 4`, radon `A (4)`
- `Main._position_with_axis_values`
  - before: lizard `25 NLOC / CCN 8`, radon `B (8)`
  - after: removed from `main.py`; logic moved into `probe_station_gui/stage/coordinate_targets.py:position_with_axis_values` (`22 NLOC / CCN 7`, radon `B (7)`)
- `Main._coordinate_move_duration_s`
  - before: lizard `22 NLOC / CCN 7`, radon `B (7)`
  - after: removed from `main.py`; logic moved into `probe_station_gui/stage/coordinate_targets.py:coordinate_move_duration_s` (`24 NLOC / CCN 5`, radon `A (5)`)
- `Main._advance_coordinate_move_prediction`
  - before: lizard `18 NLOC / CCN 5`, radon `A (5)`
  - after: lizard `9 NLOC / CCN 3`, radon `A (3)`
- `Main._clear_coordinate_move_tracking`
  - before: lizard `23 NLOC / CCN 5`, radon `A (5)`
  - after: lizard `14 NLOC / CCN 5`, radon `A (5)`
- `Main._resolve_stage_axis_target`
  - before: lizard `19 NLOC / CCN 5`, radon `A (5)`
  - after: lizard `14 NLOC / CCN 1`, radon `A (1)`
- `Main._stage_axis_target_limit_error`
  - before: lizard `19 NLOC / CCN 4`, radon `A (4)`
  - after: lizard `11 NLOC / CCN 1`, radon `A (1)`

New module hotspots:

- `plan_coordinate_target_start`: lizard `101 NLOC / CCN 12`, radon `C (12)`
- `CoordinateTargetMoveState.plan_feedrate_reissue`: lizard `60 NLOC / CCN 12`, radon `C (12)`
- `coordinate_target_common_feedrate_plan`: lizard `29 NLOC / CCN 10`, radon `B (10)`

## Explicit Verdict

- behavior preserved: `yes`
- tests passed: `yes`
- metrics improved: `yes`
- LOC gate passed: `no`
- maintainability improvement:
  - coordinate target movement state and policy are no longer spread across `Main` scalar fields and long methods
  - the heaviest coordinate-target methods in `Main` dropped substantially in complexity
- new risk introduced:
  - low, but `main.py` still retains coordinate-field UI/apply plumbing and the task missed the `<=10171` LOC gate by `114` lines
  - pushing further would require moving more GUI/input-commit plumbing and adjacent stage workflows beyond the scoped state/policy extraction completed here
