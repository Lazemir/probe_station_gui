# Task 40 Brief: Coordinate Move Lifecycle / Cancel

Date: 2026-06-28
Branch: `codex/refactor-stage-controller`

## Current Behavior

`Main.on_move_finished`, `Main._has_cancelable_operation`, and
`Main._cancel_stage_coordinate_action` coordinate several stage-motion outcomes:

- planned move prediction cleanup when a stage move finishes;
- design alignment completion after a prepared alignment rotation;
- quick-alignment collapse after B-axis rotation;
- active coordinate target cancellation, including feedrate reissue cancellation;
- generic active controller motion/task cancellation;
- pending click-to-move, manual alignment, homing, route measurement, surface-map
  capture, and microscope-scan cancellation;
- coordinate edit clearing and cancel-button enablement.

External behavior must remain stable: status messages, target cross behavior,
cancel reasons, refresh scheduling, focus restoration, and coordinate target
tracking semantics are part of the compatibility surface.

## Structural Improvement

Extract the lifecycle/cancel orchestration into a stage module with a small
main-window adapter interface:

- `probe_station_gui.stage.move_lifecycle` owns the stage move finish and cancel
  decision flow.
- `main.py` keeps the Qt slot names and side-effect methods as stable wrappers.
- The new module is split by responsibility rather than one copied large
  function: cancelable detection, pending UI-intent cancellation, background
  capture cancellation, active coordinate-move cancellation, controller
  motion/task cancellation, planned-move finish, alignment finish, and coordinate
  finish cleanup.

This should improve locality for stage move lifecycle behavior and remove
warning-level `Main` methods without changing the public or private call surface
used by existing tests.

## Validation Check

Focused checks:

- `tests/stage/test_move_lifecycle.py`
- `tests/app/test_main_coordinate_feedrate.py`
- `tests/app/test_main_planned_move_prediction.py`

Full checks:

- `.\.venv\Scripts\python.exe -m pytest tests`
- `.\.venv\Scripts\python.exe -m ruff check --ignore E402,F401 .`
- `git diff --check`

Metrics checks:

- `.\.venv\Scripts\python.exe -m radon raw main.py`
- `.\.venv\Scripts\python.exe -m radon cc main.py -s`
- `.\.venv\Scripts\python.exe -m lizard main.py`
- Wily diff from a disposable UTF-8 temp clone/cache, comparing the previous
  Task 39 commit to the Task 40 result.

## Baseline Before Edit

- `main.py` LOC: `8651`
- `main.py` SLOC: `8103`
- Wily baseline commit for this task: `b8e373b`
- `Main._has_cancelable_operation`: `30 NLOC / CCN 17`
- `Main._cancel_stage_coordinate_action`: `77 NLOC / CCN 19`
- `Main.on_move_finished`: `96 NLOC / CCN 22`
- Task target: `main.py <= 8550 LOC`

## Acceptance

- Behavior preserved: existing focused app tests and full suite pass.
- Public API changed: no.
- `main.py` LOC target met or miss explained and compensated by hotspot metrics.
- At least one warning-level `Main` lifecycle/cancel method is removed or reduced
  below warning threshold.
- New module tests characterize the extracted seam directly.
