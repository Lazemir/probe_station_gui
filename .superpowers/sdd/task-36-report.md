# Task 36 Report: Menu Dock And Window Construction Split

Status: DONE

## Files Changed

- `main.py`
- `probe_station_gui/views/main_window_menus.py`
- `probe_station_gui/views/main_window_docks.py`
- `probe_station_gui/views/main_window_auxiliary.py`
- `tests/ui/test_main_window_menus.py`
- `tests/ui/test_main_window_docks.py`
- `tests/ui/test_main_window_auxiliary.py`
- `.superpowers/sdd/task-36-brief.md`
- `.superpowers/sdd/task-36-report.md`

## Current Behavior

- `Main` still exposes the same private methods for menu setup, dock setup, settings/connection dialogs, lazy surface map, microscope scan dialog, contact calibration toggles, and design window creation.
- Menu labels, checkable actions, shortcuts, dock toggle actions, and signal targets are preserved.
- `SurfaceMapWindow`, `MicroscopeScanDialog`, and `DesignLayoutWindow` remain lazy imports/constructions.
- `ContactOscillationWindow` is still created during dock setup, as before.
- Route measurement pause, interrupt, confirmation, jump, move, stop, and measure signal targets are preserved.

## Structural Improvement

- Moved menu construction into `probe_station_gui.views.main_window_menus`.
- Moved serial dialog, resistance/joystick/contact/alignment dock construction and wiring into `probe_station_gui.views.main_window_docks`.
- Moved lazy auxiliary window/dialog and design-layout wiring into `probe_station_gui.views.main_window_auxiliary`.
- Split the new modules by responsibility so the largest moved construction helper is `40` NLOC in lizard, instead of a single transferred `290` NLOC dock method.
- Added explicit owner `Protocol` contracts and replaced catch-all test fakes with finite handler lists.

## Validation

- Import smoke: `C:\Users\Public\code\probe_station_gui\.venv\Scripts\python.exe -c "import main; print('imported')"` passed.
- Focused new UI tests: `9 passed`.
- Broader focused suite: `256 passed, 3 subtests passed`.
- Full suite: `1113 passed, 2 skipped`.
- Ruff: `All checks passed!`

## Metrics

Baseline from `34cf202`:

- `main.py` raw LOC: `9613`
- Wily `main.py` cyclomatic: `1795`
- Wily `main.py` MI: `0`
- Lizard hotspots:
  - `_create_dock_widgets`: `290` NLOC / CCN `1`
  - `_create_design_layout_window`: `124` NLOC / CCN `5`
  - `_setup_menus`: `93` NLOC / CCN `5`

Final:

- `main.py` raw LOC: `8999`
- Wily `main.py` cyclomatic: `1765`
- Wily `main.py` MI: `0`
- New module raw LOC:
  - `main_window_menus.py`: `184`
  - `main_window_docks.py`: `464`
  - `main_window_auxiliary.py`: `447`
- New module MI:
  - `main_window_menus.py`: `49.26`
  - `main_window_docks.py`: `32.91`
  - `main_window_auxiliary.py`: `16.98`
- Lizard on new modules: no warnings; largest helper is `40` NLOC / CCN `1`.

Wily final temp clone/cache:

- `main.py`: cyclomatic `1795 -> 1765`, raw LOC `9613 -> 8999`, MI `0 -> 0`.
- `main_window_menus.py`: new cyclomatic `29`, raw LOC `184`, MI `49.2573`.
- `main_window_docks.py`: new cyclomatic `73`, raw LOC `464`, MI `32.9140`.
- `main_window_auxiliary.py`: new cyclomatic `94`, raw LOC `447`, MI `16.9778`.

The new module Wily cyclomatic totals include `Protocol` method stubs. Lizard reports no warnings in the extracted modules and shows the actual executable helpers below warning thresholds.

## Reviews

- Spec review initially caught a transient missing `main_window_docks.py` state while the file was being replaced. Re-review passed after the split file was restored.
- Spec re-review found no behavior/spec parity issues and verified lazy imports stayed lazy.
- Code-quality review initially found implicit `Any` owner contracts, package-level lazy widget imports, and catch-all fakes. Fixed with direct view imports, explicit owner protocols, and finite test fakes.
- Code-quality re-review found two low test-protection gaps. Fixed by exercising `route_measurement_measure_requested` and by making the fake stage controller expose finite request methods.

## Verdict

This refactor is justified.

- Behavior preserved: yes.
- Tests passed: yes.
- Metrics improved: yes.
- Maintainability improvement: `Main` lost `614` raw LOC, Wily `main.py` cyclomatic dropped by `30`, and three oversized UI-construction responsibilities now live in focused modules with direct tests and explicit owner contracts.
- New risk introduced: low. Hardware paths were not exercised; this pass only rewired Qt construction and signal connections, covered by focused and full tests.
