# Task 43 Brief: Serial and Controller Connection Flow

Base commit: `c5f5f96 refactor: extract needle calibration workflows`
Target: `main.py <= 7750 LOC` by `radon raw main.py`

## Current Behavior

- `Main.on_serial_connected` closes any previous serial object, records port/baud, suspends controller-state persistence while attaching the serial connection to `StageController`, restores cached controller/design state, wires joystick and serial terminal panels, schedules startup sync, and refreshes design position.
- `Main.on_serial_disconnected` stops jog state, closes serial, persists disconnect state, clears pending motion/homing/design state, detaches controller/widgets, updates calibration/oscillation panels, invalidates design registration, and clears design position.
- `Main._auto_connect_if_possible` triggers serial and meter reconnect based on persisted settings.
- `Main` owns startup sync, reboot-detected/reboot-ready recovery scheduling, axis feedrate propagation to joystick UI, controller-state persistence, and persisted serial/LCR connection state.

## Structural Improvement

Create `probe_station_gui.views.main_window_connection_flow` as a main-window connection orchestration module:

- keep existing `Main` public/private method names as wrappers because dock wiring and tests connect to them;
- keep `Main` as owner of serial objects, widgets, timers, `StageController`, settings, and LCR controller;
- move the sequencing of connection/disconnection/reboot/feedrate/persistence side effects into one module for locality;
- preserve old internal method-call seams by calling `owner._...` where the original body called another `Main` method.

## Validation Checks

- New focused tests for:
  - serial connect attach order and startup sync scheduling;
  - serial disconnect cleanup/detach ordering;
  - startup-sync guard and reboot-ready coalescing;
  - feedrate-limit propagation and auto-connect decisions;
  - LCR disconnect persistence ordering.
- Existing characterization tests:
  - `tests/app/test_main_coordinate_feedrate.py::MainCoordinateFeedrateTest::test_meter_auto_connect_uses_shared_lcr_controller`
  - `tests/app/test_main_design_navigation.py`
  - `tests/app/test_main_planned_move_prediction.py`
  - `tests/ui/test_main_window_docks.py`
  - serial-terminal and stage-controller tests in the full suite.
- Full validation before commit:
  - `.\.venv\Scripts\python.exe -m ruff check . --ignore E402,F401`
  - `.\.venv\Scripts\python.exe -m pytest tests`
  - coverage report
  - Wily from disposable UTF-8 temp clone/cache comparing `c5f5f96` to the task result.

## Baseline Metrics

- `main.py` LOC: `7961`
- `main.py` SLOC: `7413`
- Wily `main.py` cyclomatic at base: `1502`
- Hotspots in scope:
  - `Main.on_serial_connected`: `B(8)`
  - `Main.on_serial_disconnected`: `B(8)`
  - `Main._auto_connect_if_possible`: `B(7)`
  - `Main._restore_persisted_controller_state`: `B(6)`

## Acceptance

- Behavior preserved.
- `main.py <= 7750 LOC`.
- Wily `main.py` cyclomatic decreases.
- No route Pause/Resume/Interrupt behavior is touched.
- Serial terminal and stage-controller coordination ordering is preserved.
