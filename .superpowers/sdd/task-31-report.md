# Task 31 Report: Manual Jog Prediction Module

- status: `DONE`
- commits created: `1` (`51abc77 Refactor manual jog prediction state`)

## Changed Files

- `main.py`
- `probe_station_gui/stage/manual_jog_prediction.py`
- `tests/stage/test_manual_jog_prediction.py`
- `tests/app/test_main_coordinate_feedrate.py`
- `tests/app/test_main_planned_move_prediction.py`

## Behavior Summary

- Added `ManualJogPredictionState` and related result/config dataclasses in `probe_station_gui.stage.manual_jog_prediction`.
- Moved manual jog command normalization, active/available predicates, stop-tail state, prediction advancement, stop-tail learning, idle sample filtering, smoothing policy, and manual waiting cleanup out of `Main`.
- Kept `Main` responsible for Qt timer start/stop, `QTimer.singleShot`, serial terminal pause/resume, stage-controller reads, coordinate-move tracking clearing, motion-axis UI, status refresh scheduling, logging, and stage position publishing.
- Did not move `_start_coordinate_targets_move` or coordinate target completion/feedrate reissue behavior.

## TDD Evidence

RED was reproduced in a disposable temp clone:

- Baseline: `792279f`
- Copied only `tests/stage/test_manual_jog_prediction.py` into the baseline clone.
- Command:
  - `C:\Users\Public\code\probe_station_gui\.venv\Scripts\python.exe -m pytest tests\stage\test_manual_jog_prediction.py -q`
- Result:
  - collection failed with `ModuleNotFoundError: No module named 'probe_station_gui.stage.manual_jog_prediction'`
  - exit code `2`

GREEN:

- `C:\Users\Public\code\probe_station_gui\.venv\Scripts\python.exe -m pytest tests\stage\test_manual_jog_prediction.py -q`
  - `10 passed`

## Focused Verification

- `C:\Users\Public\code\probe_station_gui\.venv\Scripts\python.exe -m pytest tests\stage\test_manual_jog_prediction.py tests\app\test_main_coordinate_feedrate.py tests\app\test_main_planned_move_prediction.py -q`
  - `183 passed, 3 subtests passed`

## Full Verification

- `C:\Users\Public\code\probe_station_gui\.venv\Scripts\python.exe -m pytest tests`
  - `1039 passed, 2 skipped`
- `C:\Users\Public\code\probe_station_gui\.venv\Scripts\python.exe -m ruff check --ignore E402,F401 .`
  - `All checks passed!`
- `git diff --check`
  - no whitespace errors; only existing CRLF replacement warnings for Windows checkout files

## main.py Physical LOC

- Before (`792279f` baseline from brief): `10774`
- After (`51abc77`): `10471`
- Delta: `-303`
- LOC gate `<=10474`: `passed`

## Lizard / Radon Spot Checks

### `main.py` target methods

- `Main._on_manual_jog_command_changed`
  - Before: lizard `108 NLOC / CCN 26`, radon `D (26)`
  - After: lizard `54 NLOC / CCN 10`, radon `B (10)`
- `Main._advance_manual_jog_prediction`
  - Before: lizard `65 NLOC / CCN 19`, radon `C (19)`
  - After: lizard `26 NLOC / CCN 5`, radon `A (5)`
- `Main._seed_motion_prediction_position`
  - Before: lizard `19 NLOC / CCN 11`, radon `C (11)`
  - After: lizard `6 NLOC / CCN 1`, radon `A (1)`
- `Main._learn_manual_jog_stop_tail`
  - Before: lizard `49 NLOC / CCN 10`, radon `B (10)`
  - After: removed from `Main`
- `Main._on_manual_jog_stopped`
  - Before: lizard `39 NLOC / CCN 9`, radon `B (9)`
  - After: lizard `29 NLOC / CCN 9`, radon `B (9)`
- `Main._should_ignore_manual_jog_status_sample`
  - Before: lizard `30 NLOC / CCN 9`, radon `B (9)`
  - After: removed from `Main`
- `Main._manual_jog_prediction_active`
  - Before: lizard `17 NLOC / CCN 6`, radon `B (6)`
  - After: removed from `Main`
- `Main._smooth_manual_jog_actual_position`
  - Before: lizard `28 NLOC / CCN 4`, radon `A (4)`
  - After: removed from `Main`

### New module

- `probe_station_gui/stage/manual_jog_prediction.py`
  - lizard file summary: `534 NLOC`, average CCN `6.0`
  - radon MI: `C (3.26)`
- `ManualJogPredictionState.handle_command`
  - lizard `109 NLOC / CCN 22`, radon `D (22)`
- `ManualJogPredictionState.advance`
  - lizard `88 NLOC / CCN 18`, radon `C (18)`
- `ManualJogPredictionState.learn_stop_tail`
  - lizard `48 NLOC / CCN 11`, radon `C (11)`
- `ManualJogPredictionState.resolve_waiting_stage_xy`
  - lizard `25 NLOC / CCN 9`, radon `B (9)`
- `ManualJogPredictionState.ignore_idle_status_sample`
  - lizard `27 NLOC / CCN 9`, radon `B (9)`

## Wily

Wily was run from a disposable UTF-8 temp clone:

- Clone/cache root: `C:\Users\Lazemir\AppData\Local\Temp\probe_station_gui_wily_task31_b414c2a002824f40abe25c5f250c289d`
- Commands exited `0`:
  - `C:\Users\Public\code\probe_station_gui\.venv\Scripts\python.exe -X utf8 -m wily -c <temp-cache> build main.py probe_station_gui\stage\manual_jog_prediction.py probe_station_gui\stage\position_presenter.py`
  - `C:\Users\Public\code\probe_station_gui\.venv\Scripts\python.exe -X utf8 -m wily -c <temp-cache> diff main.py probe_station_gui\stage\manual_jog_prediction.py probe_station_gui\stage\position_presenter.py --detail -r 792279f --metrics cyclomatic.complexity,raw.loc,maintainability.mi --no-wrap`

Observed build warnings:

- Wily printed repeated `No data collected` warnings for several older revisions, including `51eda42`, but the history build and final diff both completed successfully.

High-signal Wily diff vs `792279f`:

- `main.py`: cyclomatic `2099 -> 2008`, raw LOC `10774 -> 10471`, MI `0 -> 0`
- `probe_station_gui/stage/manual_jog_prediction.py`: cyclomatic `- -> 134`, raw LOC `- -> 584`, MI `- -> 3.2567847325750465`
- `main.py:Main._on_manual_jog_command_changed`: cyclomatic `26 -> 10`
- `main.py:Main._advance_manual_jog_prediction`: cyclomatic `19 -> 5`
- `main.py:Main._seed_motion_prediction_position`: cyclomatic `11 -> 1`
- `main.py:Main._preferred_design_stage_xy`: cyclomatic `24 -> 15`
- `main.py:Main._on_stage_position_changed`: cyclomatic `26 -> 28`
- `main.py:Main._preferred_design_display_stage_xy`: cyclomatic `6 -> 7`
- `ManualJogPredictionState.handle_command`: cyclomatic `- -> 22`
- `ManualJogPredictionState.advance`: cyclomatic `- -> 18`
- `ManualJogPredictionState.learn_stop_tail`: cyclomatic `- -> 11`

## Verdict

- behavior preserved: `yes`
- tests passed: `yes`
- metrics improved: `yes`
- LOC gate passed: `yes`
- maintainability improvement: `main.py` loses `303` physical lines and the main manual-jog hotspots drop sharply or leave `Main` entirely; manual jog prediction state now has a single place to change and focused pure tests.
- new risk introduced: `medium`; the new module is cohesive but still has warning-level methods (`handle_command`, `advance`, `learn_stop_tail`) and low MI. Also, Wily reports small `Main` complexity regressions in `_on_stage_position_changed` and `_preferred_design_display_stage_xy`. These should be review targets and likely follow-up cleanup, but the pass is justified because `main.py` LOC and cyclomatic complexity improved materially while behavior checks passed.
