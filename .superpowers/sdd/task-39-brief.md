# Task 39 Brief: Stage Position Update Adapter

Baseline commit: `31c27a6 docs: update refactoring roadmap`

## Current Behavior

- `Main._on_stage_position_changed(...)` receives stage status positions from `StageController.stage_position_changed`.
- Invalid/short/non-tuple positions update the stage position display only and do not publish, finish, or clear motion state.
- Valid positions restore pending design state, read latest FluidNC state/homing, build `stage_position_signal_plan(...)`, update contact calibration, handle unhomed XY fallback, invalidate design registration after B-axis motion, reconcile manual/planned motion predictions, learn manual jog stop-tail timing, publish the display estimate, and finish coordinate moves once idle.
- Publish must happen before idle finish and stage motion-axis clearing.
- Fresh idle samples immediately after manual jog must be ignored until stale enough to reconcile.

## Structural Improvement

- Add focused stage-position modules behind stable `Main` private delegates.
- Move stage-position panel construction/display planning/motion-axis styling into a view binding module.
- Move position conversion, preferred design-stage selection, and stage-position update orchestration into a stage update module.
- Keep broad application side effects behind owner calls: design invalidation, coordinate display, design position update, publish, finish, and motion-axis clearing still cross the existing `Main` private method seam so tests and callers keep the same surface.
- Move orchestration with `ManualJogPredictionState`, `CoordinateTargetMoveState`, `stage_position_signal_plan(...)`, B-axis state, planned-move waiting state, and stop-tail learning into the adapter so this locality is no longer spread through `Main`.
- Do not move serial/hardware I/O, Qt widget mutation, or public API behavior into the adapter.

## Baseline Metrics

- `radon raw main.py`: LOC `8948`, SLOC `8400`.
- `radon cc main.py -s`: `Main._on_stage_position_changed` `D (28)`.
- `lizard main.py`: `Main._on_stage_position_changed` `148` NLOC / CCN `28`; warning count includes this function.
- Related functions:
  - `Main._update_stage_position_display`: `44` NLOC / CCN `9`.
  - `Main._preferred_design_display_stage_xy`: `11` NLOC / CCN `7`.

## Target

- `main.py <= 8750 LOC` by `radon raw`.
- `Main._on_stage_position_changed` below lizard warning thresholds, ideally `<= 50` NLOC and CCN `<= 10`.
- No new adapter helper exceeds lizard warning thresholds.
- Public methods, signals, route-control behavior, coordinate move behavior, and UI copy remain stable.

## Validation

- Focused:
  - `.\.venv\Scripts\python.exe -m pytest tests\app\test_main_planned_move_prediction.py tests\stage\test_position_presenter.py`
  - New adapter tests if a new module is added.
- Full:
  - `.\.venv\Scripts\python.exe -m pytest tests`
  - `.\.venv\Scripts\python.exe -m ruff check --ignore E402,F401 .`
- Metrics:
  - `.\.venv\Scripts\python.exe -m radon raw main.py`
  - `.\.venv\Scripts\python.exe -m lizard main.py probe_station_gui\stage`
  - Wily from a disposable UTF-8 temp clone/cache comparing against `31c27a6`.

## Risks

- Limit Qt widget mutation to the existing `StagePositionPanel` display/construction adapter. Do not emit Qt signals or move broader app UI side effects into the stage update module.
- Preserve the exact early-return order:
  - invalid position display-only return;
  - unhomed fallback return after display/clear/design update/idle finish;
  - deferred manual stop sample return;
  - ignored fresh idle sample return.
- Preserve B-axis invalidation ordering before `_last_reported_b_position` update.
- Preserve publish-before-finish ordering for idle coordinate moves.
- Preserve manual jog stop-tail learn and clearing only on idle fresh status.
