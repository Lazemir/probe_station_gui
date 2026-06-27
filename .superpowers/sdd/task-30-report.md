# Task 30 Report: Stage Position Field Adapter

- status: `DONE`
- commits created: `1` (`Refactor stage position UI adapter`)

## Changed Files

- `main.py`
- `probe_station_gui/views/__init__.py`
- `probe_station_gui/views/stage_position_panel.py`
- `tests/app/test_main_coordinate_feedrate.py`
- `tests/ui/test_stage_position_panel.py`

## Behavior Summary

- Added a real `StagePositionPanel` Qt view adapter that owns stage-position widget construction, field-local pending/return state, formatting, styling, availability, and Apply/Cancel button state.
- Kept `Main` as the coordinator for raw/display calibration, target resolution, software-limit validation, status messages, focus handoff, joystick mode selection, and coordinate-move startup.
- Preserved focused pending-edit behavior, limit-vs-homed style precedence, pending style precedence, motion blink dimming, mode-change clearing/status text, and coordinate-apply ordering.

## TDD Evidence

### RED

- Added `tests/ui/test_stage_position_panel.py` first.
- Initial run:
  - `C:\Users\Public\code\probe_station_gui\.venv\Scripts\python.exe -m pytest tests\ui\test_stage_position_panel.py -q`
  - Result: failed during collection with `ModuleNotFoundError: No module named 'probe_station_gui.views.stage_position_panel'`.

### GREEN

- Implemented `probe_station_gui/views/stage_position_panel.py`.
- Fixed the new panel tests and then expanded coordinator characterization.

### Focused Verification

- `C:\Users\Public\code\probe_station_gui\.venv\Scripts\python.exe -m pytest tests\ui\test_stage_position_panel.py -q`
  - `14 passed`
- `C:\Users\Public\code\probe_station_gui\.venv\Scripts\python.exe -m pytest tests\ui\test_stage_position_panel.py tests\stage\test_position_presenter.py tests\app\test_main_coordinate_feedrate.py tests\app\test_main_planned_move_prediction.py -q`
  - `194 passed, 3 subtests passed`

## Full Verification

- `C:\Users\Public\code\probe_station_gui\.venv\Scripts\python.exe -m pytest tests`
  - `1023 passed, 2 skipped`
- `C:\Users\Public\code\probe_station_gui\.venv\Scripts\python.exe -m ruff check --ignore E402,F401 .`
  - `All checks passed!`

## main.py Physical LOC

- Before (`51eda42` baseline from brief): `10997`
- After (current worktree): `10774`
- LOC gate `<=10777`: `passed`

## Lizard / Radon Spot Checks

### main.py target methods from brief

- `Main._create_stage_position_widget`
  - Before: lizard `74 NLOC / CCN 2`, radon `A (2)`
  - After: lizard `13 NLOC / CCN 1`, radon `A (1)`
- `Main._set_stage_position_fields_available`
  - Before: lizard `17 NLOC / CCN 4`, radon `A (3)`
  - After: removed from `Main`
- `Main._style_stage_axis_field`
  - Before: lizard `11 NLOC / CCN 1`, radon `A (1)`
  - After: removed from `Main`
- `Main._format_stage_axis_value`
  - Before: lizard `6 NLOC / CCN 3`, radon `A (3)`
  - After: removed from `Main` and replaced by `probe_station_gui.views.stage_position_panel.format_stage_axis_value` (`6 NLOC / CCN 3`, radon `A (3)`)
- `Main._apply_stage_axis_field_style`
  - Before: lizard `16 NLOC / CCN 6`, radon `B (6)`
  - After: removed from `Main`
- `Main._refresh_stage_axis_styles`
  - Before: lizard `5 NLOC / CCN 3`, radon `A (3)`
  - After: lizard `5 NLOC / CCN 2`, radon `A (2)`
- `Main._on_stage_axis_escape_pressed`
  - Before: lizard `12 NLOC / CCN 2`, radon `A (2)`
  - After: lizard `15 NLOC / CCN 3`, radon `A (3)`
- `Main._selected_stage_coordinate_input_mode`
  - Before: lizard `6 NLOC / CCN 3`, radon `A (3)`
  - After: removed from `Main`
- `Main._stage_axis_fields_have_modified_text`
  - Before: lizard `5 NLOC / CCN 3`, radon `A (3)`
  - After: removed from `Main`
- `Main._update_stage_coordinate_apply_state`
  - Before: lizard `15 NLOC / CCN 8`, radon `B (8)`
  - After: lizard `8 NLOC / CCN 6`, radon `B (6)`
- `Main._clear_pending_stage_coordinate_targets`
  - Before: lizard `12 NLOC / CCN 3`, radon `A (3)`
  - After: lizard `7 NLOC / CCN 2`, radon `A (2)`
- `Main._update_stage_position_display`
  - Before: lizard `65 NLOC / CCN 10`, radon `B (9)`
  - After: lizard `44 NLOC / CCN 9`, radon `B (9)`

### New panel module

- `probe_station_gui/views/stage_position_panel.py`
  - lizard file summary: `333 NLOC`, `33` functions
  - radon MI: `A (19.35)`
- `StagePositionPanel.apply_display_plan`
  - lizard `37 NLOC / CCN 6`
  - radon `B (6)`
- `StagePositionPanel.refresh_axis_styles`
  - lizard `15 NLOC / CCN 5`
  - radon `A (5)`

### position_presenter reference file

- `probe_station_gui/stage/position_presenter.py`
  - lizard file summary: `416 NLOC`, `17` functions
  - radon MI: `A (23.04)`

## Wily

- Disposable UTF-8 clone: `C:\Users\Lazemir\AppData\Local\Temp\probe_station_gui_task30_metrics\clone`
- Disposable cache/config: `C:\Users\Lazemir\AppData\Local\Temp\probe_station_gui_task30_metrics\wily`
- Commands:
  - `C:\Users\Public\code\probe_station_gui\.venv\Scripts\python.exe -X utf8 -m wily -c <temp-config> build main.py probe_station_gui\views\stage_position_panel.py probe_station_gui\stage\position_presenter.py`
  - `C:\Users\Public\code\probe_station_gui\.venv\Scripts\python.exe -X utf8 -m wily -c <temp-config> diff main.py probe_station_gui\views\stage_position_panel.py probe_station_gui\stage\position_presenter.py --detail -r 51eda42 --metrics cyclomatic.complexity,raw.loc,maintainability.mi --no-wrap`
- Build result: exit `0`
- Build warnings: repeated `No data collected` warnings for many older revisions, including `51eda42`, but the history build completed.
- Diff result: exit `0`

High-signal Wily diff vs `51eda42`:

- `main.py`: cyclomatic `2131 -> 2099`, raw LOC `10997 -> 10774`, MI `0 -> 0`
- `probe_station_gui/views/stage_position_panel.py`: cyclomatic `- -> 67`, raw LOC `- -> 390`, MI `- -> 19.354341283536876`
- `main.py:Main._update_stage_coordinate_apply_state`: cyclomatic `8 -> 6`
- `main.py:Main._clear_pending_stage_coordinate_targets`: cyclomatic `3 -> 2`
- `main.py:Main._create_stage_position_widget`: cyclomatic `2 -> 1`
- `main.py:Main._refresh_stage_axis_styles`: cyclomatic `3 -> 2`
- `main.py:Main._on_stage_axis_editing_finished`: cyclomatic `10 -> 11`

## Verdict

- behavior preserved: `yes`
- tests passed: `yes`
- metrics improved: `yes`
- LOC gate passed: `yes`
- maintainability improvement:
  - `main.py` dropped below the task gate (`10997 -> 10774`) and Wily file-level cyclomatic also improved (`2131 -> 2099`).
  - Most field-construction and field-local state code moved into a focused Qt adapter instead of staying in `Main`.
- new risk introduced:
  - `Main._on_stage_axis_editing_finished` picked up a small cyclomatic increase in Wily (`10 -> 11`) because the coordinator still owns parse/resolve/limit failure branches. The new UI and app characterization tests cover those paths.
