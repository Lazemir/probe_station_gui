# Task 36 Brief: Menu Dock And Window Construction Split

You are implementing Task 36 from `docs/superpowers/plans/2026-06-26-main-loc-reduction.md`.

Use Superpowers as the main workflow, with `superpowers:subagent-driven-development` for this task.

## Goal

Safely reduce `main.py` by moving menu construction, dock/widget construction, and lazy auxiliary window creation wiring into focused view/window factory modules without changing external behavior.

This is a structural refactor only. Preserve visible UI behavior, signals, startup heaviness, public APIs, route measurement semantics, and hardware interaction behavior.

## Baseline

Baseline commit: `34cf202 Update Task 35 final metrics`

Current `main.py` metrics:

- `radon raw main.py`: LOC `9613`, SLOC `9033`, LLOC `5681`
- Wily baseline from Task 35 final report: `main.py` raw LOC `9613`, cyclomatic complexity `1795`, Maintainability Index `0`
- `lizard main.py` target hotspots:
  - `_setup_menus`: `93` NLOC, CCN `5`, length `109`
  - `_create_dock_widgets`: `290` NLOC, CCN `1`, length `303`
  - `_create_design_layout_window`: `124` NLOC, CCN `5`, length `125`
  - `_show_surface_map_window`: `14` NLOC, CCN `2`
  - `_show_microscope_scan_dialog`: `17` NLOC, CCN `2`

Strict target from the HTML/main LOC plan:

- Reduce `main.py` by at least `600` lines: `main.py <= 9013` LOC.

Fallback target if the strict target becomes risky:

- Reduce `main.py` by at least `400` lines.
- Remove `_create_dock_widgets` from `Main`, or reduce it below `50` NLOC.
- Reduce `_setup_menus` below `15` NLOC.
- Improve Wily `main.py` cyclomatic complexity below `1795`.
- Keep auxiliary window creation lazy and do not make startup heavier.

Stop and rethink if this task increases `main.py` LOC.

## Current behavior to preserve

`Main._setup_menus`:

- Adds menu bar menus: `Application`, `Navigation`, `Tools`, `Calibration`.
- Adds final visible actions without ellipses: `Settings`, `Open Status Log`, `Connection`, `Load Sample`, `Unload Sample`, `Design Window`, `Contact / Stone Calibration`, `Surface Map`, `Microscope Scan`, `Click-to-Move Calibration`, `Ruler`, `Rectangle`, `Capture Alignment Point`, `Cancel Alignment Pick`.
- Makes `Design Window`, `Contact / Stone Calibration`, `Ruler`, and `Rectangle` checkable.
- Gives `Ruler` shortcut `R`, `Rectangle` shortcut `T`, alignment capture shortcut `Main.ALIGNMENT_CAPTURE_SHORTCUT`, and alignment exit shortcut `Escape`, all application-scoped.
- Adds dock toggle actions for resistance, oscillation, joystick, and alignment docks when those docks exist.
- Connects existing `Main` methods as handlers.

`Main._create_dock_widgets`:

- Creates the non-modal serial connection dialog with tabs for connection and terminal plus a Close button.
- Creates and wires `SerialConnectionPanel`, `SerialTerminalWindow`, `ResistanceMonitorPanel`, `JoystickWindow`, `ContactOscillationWindow`, `AlignmentPanel`, and `CollapsibleDockWidget` instances.
- Applies feedrate/jog/needle settings to the joystick panel.
- Preserves all signal connections to existing `Main`, `StageController`, and `LCRMeterController` methods.
- Adds/places/splits docks exactly as before.
- Calls `_refresh_manual_alignment_ui`, `_update_coordinate_display`, and `_refresh_design_panel` after construction.

Auxiliary lazy windows:

- `SurfaceMapWindow` is imported and created only inside the show path; it is not created during startup.
- `MicroscopeScanDialog` is imported and created only inside the show path; existing dialog is reused until finished clears `self.microscope_scan_dialog`.
- `DesignLayoutWindow` import and instantiation remain lazy/preload-compatible; `Main._preload_design_layout_window` must still be able to pass an already imported class.
- `ContactOscillationWindow` remains created during dock setup as before; this pass may move construction/wiring but must not make it lazier unless separately proven and tested.

Route measurement safety:

- Do not change Pause Request / Pause Ack / Interrupt semantics.
- If design navigator route signals are moved into a connector helper, connect the same signals to the same `Main` methods or existing adapter callables.
- Do not rename API route control workflows.

## Structural improvement

Create focused modules under `probe_station_gui/views/` or another existing UI package, for example:

- `probe_station_gui/views/main_window_menus.py`
- `probe_station_gui/views/main_window_docks.py`
- `probe_station_gui/views/main_window_auxiliary.py`

Prefer small cohesive helpers/dataclasses that:

- Receive `Main` or a narrow owner object and perform Qt construction/wiring.
- Return created widgets/actions/docks when that makes ownership explicit.
- Keep `Main` as the owner of runtime state and business handlers.
- Keep heavy optional imports inside lazy show/create functions.
- Avoid copying unchanged large blocks into a shallow module unless the module has a clear UI-construction responsibility and direct tests.

Do not introduce framework migrations, dependency upgrades, public API changes, or architecture moves beyond this task. Those belong in separate migration tasks.

## Tests to add or update

Add focused characterization tests around the moved construction behavior. Prefer tests that do not require real hardware.

Suggested tests:

- Menu construction:
  - Given fake/dummy docks, the menu builder creates the expected final visible action labels.
  - `Design Window` and `Contact / Stone Calibration` actions are checkable and connected through `Main` handlers.
  - `Ruler`/`Rectangle` shortcuts remain application-scoped.
  - No visible menu item uses ellipsis.
- Lazy windows:
  - Surface map window factory creates once, reuses the existing instance, and wires `capture_running_changed` to `_update_stage_coordinate_apply_state`.
  - Microscope scan dialog factory creates once, reuses the existing instance, wires scan/stop/finished handlers, and computes default directory through the existing `Main` helper.
  - Design layout connector wires the same navigator/window signals to the same `Main` methods, including route measurement pause/interrupt/confirmation/move requests.
- Dock construction:
  - At minimum, a characterization test verifies the helper creates/assigns the expected owner attributes and adds the expected dock object names.
  - If full Qt instantiation is too brittle, keep the tests at the wiring/factory boundary with small fake widgets/signals and cover the heavy path through existing UI tests.

Run existing relevant suites:

```powershell
C:\Users\Public\code\probe_station_gui\.venv\Scripts\python.exe -m pytest tests\ui tests\app\test_main_design_navigation.py tests\app\test_main_coordinate_feedrate.py tests\app\test_main_planned_move_prediction.py -q
```

Then full verification:

```powershell
C:\Users\Public\code\probe_station_gui\.venv\Scripts\python.exe -m pytest tests
C:\Users\Public\code\probe_station_gui\.venv\Scripts\python.exe -m ruff check --ignore E402,F401 .
```

## Metrics

After implementation and after any review fixes, collect metrics with Wily from a disposable UTF-8 temp clone/cache, not the main checkout:

```powershell
$env:PYTHONIOENCODING='utf-8'
$env:PYTHONUTF8='1'
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8
C:\Users\Public\code\probe_station_gui\.venv\Scripts\python.exe -X utf8 -m wily -c <temp-config> build main.py probe_station_gui\views\main_window_menus.py probe_station_gui\views\main_window_docks.py probe_station_gui\views\main_window_auxiliary.py
C:\Users\Public\code\probe_station_gui\.venv\Scripts\python.exe -X utf8 -m wily -c <temp-config> diff main.py probe_station_gui\views\main_window_menus.py probe_station_gui\views\main_window_docks.py probe_station_gui\views\main_window_auxiliary.py --detail -r 34cf202 --metrics cyclomatic.complexity,raw.loc,maintainability.mi --no-wrap
```

Use `radon`/`lizard` spot checks for exact method-level numbers:

```powershell
C:\Users\Public\code\probe_station_gui\.venv\Scripts\python.exe -m radon raw main.py probe_station_gui\views\main_window_menus.py probe_station_gui\views\main_window_docks.py probe_station_gui\views\main_window_auxiliary.py
C:\Users\Public\code\probe_station_gui\.venv\Scripts\python.exe -m lizard main.py probe_station_gui\views\main_window_menus.py probe_station_gui\views\main_window_docks.py probe_station_gui\views\main_window_auxiliary.py
```

If module names differ, update metric commands and the report with the actual files.

## Report

Create `.superpowers/sdd/task-36-report.md` with:

- Current behavior
- Structural improvement
- Validation checks
- Files changed
- Before/after metrics table
- Review findings and fixes
- Verdict:
  - behavior preserved: yes/no
  - tests passed: yes/no
  - metrics improved: yes/no
  - maintainability improvement
  - new risk introduced

## Acceptance

- Behavior preserved by focused and full tests.
- Configured ruff gate passes.
- `main.py` LOC decreases materially.
- `_create_dock_widgets`, `_setup_menus`, and `_create_design_layout_window` no longer carry large UI-construction bodies inside `Main`.
- Lazy auxiliary windows remain lazy.
- Route measurement controls retain the same signal targets and semantics.
- Metrics report is honest about both improvements and any local regressions in new modules.
