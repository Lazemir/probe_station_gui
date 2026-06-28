# Task 42 Brief: Contact Seek, Needle Calibration, and Sample Handling Plans

Base commit: `01639e4 refactor: extract microscope scan planning`
Target: `main.py <= 8000 LOC` by `radon raw main.py`

## Current Behavior

- `Main` owns contact seek start guards, thread creation, cancellation, runtime loop status text, confirmation text, result handling, and Telegram failure notification.
- `Main` owns saved needle targets:
  - save current A lowering after idle checks and A0 reset;
  - save raise/lower target from a displayed or raw A coordinate;
  - save contact-seek lowering as the down target.
- `Main` owns saved chip/stone surface position logic:
  - validate target name;
  - read current XYZ;
  - write chip/stone settings;
  - choose transit Z for a move to a saved position.
- `Main` owns sample load/unload planning:
  - focus memory per active objective;
  - registration-clear prompt decisions;
  - load/unload target coordinates and status messages;
  - autofocus prompt text after sample load.

## Structural Improvement

Create a stage-facing module for these calibration/sample workflows:

- move guard/status/payload/message decisions into a deep helper interface;
- reuse the existing `probe_station_gui.route.contact_seek` depth helpers instead of duplicating seek-step math;
- make saved-position and sample load/unload behavior testable without constructing `Main`.
- keep `Main` public slot names stable as thin wrappers;
- keep Qt signal/message-box/Telegram entrypoints on the main-window owner;
- move cohesive contact-calibration workflow glue behind `probe_station_gui.views.main_window_needle_calibration`;
- move sample load/unload stage sequences behind `probe_station_gui.stage.sample_handling`, with `Main` passing the same tunable class attributes it owned before.

Implementation amendment: the first pure-planning extraction did not reach the
`main.py <= 8000 LOC` contract. The final pass therefore moved a narrow amount of
workflow glue while preserving `Main.CONTACT_SEEK_*`, `Main.SAMPLE_*`, and all
existing private slot names as compatibility surface.

## Validation Checks

- New pure tests for:
  - contact seek request decisions and status/detail messages;
  - needle target update decisions and status text;
  - surface save/move plans;
  - sample focus cache, load/unload, and autofocus prompt plans.
- Existing characterization tests:
  - `tests/route/test_contact_seek.py`
  - relevant app tests in `tests/app/test_main_coordinate_feedrate.py`
  - `tests/ui/test_main_window_docks.py`
  - `tests/stage/test_needle_targets.py`
- Full validation before commit:
  - `.\.venv\Scripts\python.exe -m ruff check . --ignore E402,F401`
  - `.\.venv\Scripts\python.exe -m pytest tests`
  - Wily from disposable UTF-8 temp clone/cache comparing `01639e4` to the task result.

## Baseline Metrics

- `main.py` LOC: `8371`
- `main.py` SLOC: `7823`
- `main.py` Wily cyclomatic at base: `1587`
- Hotspots in scope:
  - `Main._save_current_needle_height`: `C(11)`
  - `Main._run_contact_seek`: `B(10)`
  - `Main._request_contact_seek`: `B(8)`
  - `Main._on_sample_handling_finished`: `B(8)`
  - `Main._move_to_surface_position`: `B(6)`
  - `Main._save_surface_position`: `A(5)`

## Acceptance

- Behavior preserved.
- `main.py` reaches `<= 8000 LOC` or a documented exception explains why hitting the line target would move hardware/Qt side effects across the wrong seam.
- At least one maintainability metric improves:
  - lower `main.py` cyclomatic;
  - shorter warning-level methods in scope;
  - fewer magic literals in `Main`;
  - better module-local coverage around calibration/sample workflow rules.
