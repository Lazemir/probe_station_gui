Status: DONE

Summary of structural change:
- Added `probe_station_gui.stage.position_update` for stage-position update orchestration: position conversion, preferred design-stage selection, manual/planned prediction reconciliation, unhomed fallback, B-axis invalidation application, stop-tail learning, publish/finish ordering.
- Added `probe_station_gui.views.main_window_stage_position_panel` for view binding: `StagePositionPanel` construction/wiring, axis display/raw conversion, motion-axis blink/style updates, and stage-position display cache updates.
- Kept stable `Main` private method names as delegates so existing callers/tests still cross the same surface.
- Added direct stage-module tests for invalid display-only handling, unhomed fallback side-effect ordering, and planned-move wait-state clearing.

Behavior parity notes:
- Invalid positions still call only `_update_stage_position_display(...)` and return.
- Persisted design restore still happens before normal prediction handling.
- Unhomed fallback still updates display, clears manual/planned prediction state, clears coordinate display center, updates design position, then finishes/clears motion if idle.
- B-axis invalidation still happens before `_last_reported_b_position` is updated.
- Deferred manual jog stop samples and ignored fresh idle samples still return before reconcile/publish.
- Publish still happens before idle coordinate finish and motion-axis clearing.
- Existing route/API/hardware behavior was not touched.

Tests/commands run and exact results:
- `.\.venv\Scripts\python.exe -m pytest tests\stage\test_position_update.py tests\app\test_main_planned_move_prediction.py tests\stage\test_position_presenter.py tests\app\test_main_coordinate_feedrate.py`
  - Final focused run: `189 passed in 2.20s`.
- `.\.venv\Scripts\python.exe -m pytest tests`
  - Final full run: `1145 passed, 2 skipped in 17.50s`.
- `.\.venv\Scripts\python.exe -m ruff check --ignore E402,F401 .`
  - `All checks passed!`.
- `git diff --check`
  - no whitespace errors.

Metrics:
- `radon raw main.py`: LOC `8948 -> 8651`, SLOC `8400 -> 8103`.
- `radon raw` new modules:
  - `probe_station_gui/stage/position_update.py`: LOC `440`, SLOC `401`.
  - `probe_station_gui/views/main_window_stage_position_panel.py`: LOC `200`, SLOC `175`.
  - `tests/stage/test_position_update.py`: LOC `208`.
- `radon cc main.py -s`:
  - `Main._on_stage_position_changed`: `D (28) -> A (1)`.
  - `Main._update_stage_position_display`: `B (9) -> A (1)`.
  - `Main._preferred_design_stage_xy`: `C (15) -> A (1)`.
  - `Main._preferred_design_display_stage_xy`: `B (7) -> A (1)`.
- `radon cc` new modules:
  - `position_update.preferred_design_stage_xy`: `C (15)`.
  - `position_update.on_stage_position_changed`: `C (11)`.
  - `main_window_stage_position_panel.update_stage_position_display`: `B (9)`.
- `radon mi`:
  - `main.py`: `C (0.00) -> C (0.00)`.
  - `probe_station_gui/stage/position_update.py`: `C (16.81)`.
  - `probe_station_gui/views/main_window_stage_position_panel.py`: `A (32.91)`.
- `lizard main.py probe_station_gui\stage\position_update.py probe_station_gui\views\main_window_stage_position_panel.py`:
  - `Main._on_stage_position_changed`: `148` NLOC / CCN `28` -> `2` NLOC / CCN `1`.
  - `position_update.on_stage_position_changed`: `64` NLOC / CCN `11`.
  - `position_update.preferred_design_stage_xy`: `44` NLOC / CCN `15`.
  - `main_window_stage_position_panel.update_stage_position_display`: `55` NLOC / CCN `9`.
  - New modules introduced no lizard warning rows.
  - Scope warning count: `5`; remaining warnings are existing unrelated hotspots: `_api_raw_voltage_sweep`, `_has_cancelable_operation`, `_cancel_stage_coordinate_action`, `on_move_finished`, `closeEvent`.
- Wily final from disposable UTF-8 temp clone/cache, comparing temp Task 39 commit `e08ffdb` against `31c27a6`:
  - `main.py`: LOC `8948 -> 8651`, cyclomatic `1733 -> 1653`, MI `0 -> 0`.
  - `probe_station_gui/stage/position_update.py`: LOC `- -> 440`, cyclomatic `- -> 90`, MI `- -> 16.8115`.
  - `probe_station_gui/views/main_window_stage_position_panel.py`: LOC `- -> 200`, cyclomatic `- -> 39`, MI `- -> 32.9093`.
  - `tests/stage/test_position_update.py`: LOC `- -> 208`, cyclomatic `- -> 32`, MI `- -> 36.1195`.
  - Wily function detail: `Main._on_stage_position_changed` cyclomatic `28 -> 1`; `_preferred_design_stage_xy` `15 -> 1`; `_update_stage_position_display` `9 -> 1`.

Contract:
- Required `main.py` LOC after Task 39: `<= 8750`.
- Actual `main.py` LOC: `8651`.
- Contract target: met by `99` LOC.

Reviews:
- Initial spec/behavior review: no findings.
- Initial code-quality review: flagged important seam quality issues in the first single-file adapter version.
- Follow-up spec/behavior review after split: approved, no findings.
- Follow-up architecture/code-quality review after split: approved, no findings.

Verdict:
This refactor is justified.

- Behavior preserved: yes by focused and full tests.
- Tests passed: yes.
- Metrics improved: yes; target hotspot and `main.py` file-level cyclomatic improved.
- Maintainability improvement: stage-position update state and stage-position panel binding now have separate locality instead of one mixed `Main` block; the warning-level `_on_stage_position_changed` hotspot was removed from `Main` without creating a new lizard warning.
- New risk introduced: medium-low. `position_update` still crosses a fairly broad owner Protocol to preserve behavior and existing `Main` side-effect seams; future stage-position changes should shrink this with explicit state/action dataclasses rather than growing the Protocol.

Files changed:
- `main.py`
- `probe_station_gui/stage/position_update.py`
- `probe_station_gui/views/main_window_stage_position_panel.py`
- `tests/stage/test_position_update.py`
- `.superpowers/sdd/task-39-brief.md`
- `.superpowers/sdd/task-39-report.md`
