# Task 40 Report: Coordinate Move Lifecycle / Cancel

Date: 2026-06-28
Base commit: `b8e373b refactor: split stage position update adapter`

## Changed

- Added `probe_station_gui.stage.move_lifecycle` as the stage move lifecycle
  module for cancelability, cancel action ordering, coordinate tracking cleanup,
  idle finish handling, and movement-finished handling.
- Kept existing `Main` private/slot names stable as main-window adapter wrappers.
- Added direct characterization tests in `tests/stage/test_move_lifecycle.py`
  for:
  - active coordinate move cancel priority over generic busy-task cancel;
  - route runner and coordinate cancel ordering;
  - surface-map/microscope-scan cancel side effects;
  - pending coordinate edit clearing;
  - planned move finish cleanup;
  - alignment preparation finish early-return behavior;
  - feedrate reissue cancellation preserving tracking and suppressing status;
  - normal failure and skipped-message coordinate tracking cleanup;
  - idle finish scheduling pending homing.

## Behavior

Public API changed: no.

Existing `Main` entrypoints remain stable:

- `_has_cancelable_operation`
- `_cancel_stage_coordinate_action`
- `on_move_finished`
- `_clear_coordinate_move_tracking`
- `_finish_coordinate_move_if_idle`

The refactor preserves the important ordering:

- active coordinate move cancel returns before generic busy-task cancel;
- route/capture pending cancels happen before active coordinate cancel;
- feedrate reissue `"Operation cancelled."` clears the reissue flag, keeps
  coordinate tracking active, schedules refreshes, and does not show the normal
  failure status;
- successful non-skipped move finish still waits for idle position handling to
  clear coordinate tracking.

## Verification

Focused:

```text
.\.venv\Scripts\python.exe -m pytest tests\stage\test_move_lifecycle.py tests\app\test_main_coordinate_feedrate.py tests\app\test_main_planned_move_prediction.py tests\stage\test_coordinate_targets.py tests\stage\test_position_update.py
199 passed
```

Full:

```text
.\.venv\Scripts\python.exe -m pytest tests
1158 passed, 2 skipped
```

Static checks:

```text
.\.venv\Scripts\python.exe -m ruff check --ignore E402,F401 .
All checks passed

git diff --check
No whitespace errors; Git reports only the existing LF/CRLF warning for main.py.
```

## Metrics

Before: `b8e373b`.

```text
Metric                        Before     After      Better?
Largest file LOC (main.py)    8651       8446       yes
Largest function LOC          96         96         unchanged in main.py warning list
Max cyclomatic complexity     22*        22**       target hotspots improved
Average cyclomatic complexity 1653 total 1591 total yes
Maintainability Index         0          0          unchanged
Functions >50 LOC             lower      lower      yes for target methods
Functions with CC >10         lower      lower      yes
Duplicated blocks             not rerun   not rerun  n/a
Dead code candidates          not rerun   not rerun  n/a
Magic literals                unchanged  unchanged  n/a
Lint errors                   0          0          yes
Type errors                   not run    not run     n/a
Tests                         pass       pass        yes
Coverage                      not run    not run     n/a
Public API changed            no         no          yes
```

`*` Target hotspot maximum before Task 40 was `Main.on_move_finished` CC 22.
`**` Remaining `main.py` maximum is now `_api_raw_voltage_sweep` CC 22; the
Task 40 hotspots are all CC 1.

Wily from disposable UTF-8 temp clone/cache:

```text
main.py LOC: 8651 -> 8446
main.py SLOC: 8103 -> 7898
main.py cyclomatic: 1653 -> 1591
main.py MI: 0 -> 0

Main._has_cancelable_operation: 17 -> 1
Main._cancel_stage_coordinate_action: 19 -> 1
Main.on_move_finished: 22 -> 1
Main._clear_coordinate_move_tracking: 5 -> 1
Main._finish_coordinate_move_if_idle: 4 -> 1
```

Radon/lizard spot checks after the final quality fix:

```text
main.py LOC: 8446
main.py SLOC: 7898
move_lifecycle.py LOC: 436
move_lifecycle.py SLOC: 380
test_move_lifecycle.py LOC: 478
test_move_lifecycle.py SLOC: 360

move_lifecycle.py max radon CC: B(8)
move_lifecycle.py lizard warnings: 0
Task 40 Main adapters:
  _has_cancelable_operation: 2 NLOC / CCN 1
  _cancel_stage_coordinate_action: 5 NLOC / CCN 1
  on_move_finished: 2 NLOC / CCN 1
  _clear_coordinate_move_tracking: 8 NLOC / CCN 1
  _finish_coordinate_move_if_idle: 7 NLOC / CCN 1
```

## Reviews

- Explorer/spec review: no findings. It confirmed stable call surface and cancel
  ordering; it asked for more direct characterization around route/capture,
  planned move, and alignment branches.
- Added characterization tests for those branches.
- Code-quality review: one P2 finding. The first version made the module call
  back through `owner._clear_coordinate_move_tracking`, which weakened the seam.
  Fixed by calling local `clear_coordinate_move_tracking(...)` internally and
  removing `_clear_coordinate_move_tracking` plus unused `_stage_axis_display_values`
  from the owner Protocol.
- Code-quality re-review: no findings.

## Verdict

This refactor is justified.

Reason:

- behavior preserved: yes;
- tests passed: yes;
- metrics improved: yes;
- maintainability improvement: stage move lifecycle/cancel policy moved behind a
  cohesive module interface, target `Main` hotspots are thin adapters, and direct
  tests now cover risky ordering;
- new risk introduced: low. The owner Protocol is still broad because this seam
  coordinates GUI/stage side effects, but it no longer requires adapter methods
  for module-owned behavior.
