# Main LOC Reduction Refactor Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development to implement this plan task-by-task. Each task must pass review before the next task starts.

**Goal:** Increase Maintainability Index, reduce LOC, reduce cyclomatic complexity, and turn `main.py` from a 12k-line catch-all into a human-maintainable Qt shell with deeper route, stage, instrument, and design modules behind small interfaces.

**Architecture:** Move cohesive behavior out of `Main` into deeper modules and adapters. `Main` should own top-level Qt wiring, app lifetime, and cross-module composition, while route/stage/instrument/design modules own their local policy and presentation state. A task is accepted only when locality improves and `main.py` loses meaningful lines, not merely when a hotspot method is split into more `Main` helpers.

**Tech Stack:** Python 3.11, PySide6, pytest, ruff, radon, lizard, wily with forced UTF-8 on Windows.

## Global Constraints

- Preserve external behavior unless explicitly requested otherwise.
- Keep route Pause Request / Pause Ack / Interrupt semantics stable.
- Do not route in-process GUI behavior through the localhost API.
- Do not run hardware-dependent automation.
- Use `C:\Users\Public\code\probe_station_gui\.venv\Scripts\python.exe` for Python commands.
- Use Wily with forced UTF-8 and cache under `%TEMP%`.
- After each completed task: focused tests, full `pytest tests`, ruff, metrics, review, commit.
- Each task must report `main.py` physical line count before/after, target method lizard/radon before/after, and Wily file-level cyclomatic/MI before/after.
- LOC gate: each task should reduce `main.py` by at least 150 physical lines. If a task cannot hit that because the seam is smaller, it must explicitly justify the exception and improve a major hotspot without increasing full-scope warning count.
- CC gate: each task should reduce at least one target method's lizard CCN/radon complexity or remove it from `main.py` entirely. If file-level Wily cyclomatic rises, explain why and add a cleanup follow-up before moving on unless target complexity and LOC both materially improve.
- MI gate: each task must record Wily/radon Maintainability Index for touched files. If MI worsens, explain whether the change still improves locality and schedule a cleanup when the regression is real rather than metric noise.
- Do not accept a task where `main.py` LOC grows or stays flat unless the user explicitly approves that pass.

## Current Baseline

- Branch: `codex/refactor-stage-controller`
- Baseline commit: `e2da4bf`
- `main.py` physical lines: `12313`
- Known remaining `main.py` hotspots:
  - `_on_stage_position_changed`: 118 NLOC / CCN 39
  - `_api_raw_voltage_sweep`: 148 NLOC / CCN 31
  - `_api_move_to_coordinates`: 140 NLOC / CCN 23
  - `_api_measure_current_contact`: 185 NLOC / CCN 18
  - `_api_move_to_contact`: 95 NLOC / CCN 14
  - `_open_route_measurement_dialog`: 107 NLOC / CCN 13
  - `_api_start_route_session`: 117 NLOC / CCN 10

## Milestones

- After Tasks 21-24: `main.py < 11300`
- After Tasks 21-28: `main.py < 10000`
- After Tasks 21-36: `main.py < 8000`
- If `main.py < 8000` is reached with remaining high-complexity clusters, create a second phase for startup/window construction and lower-level stage refactors.

## Planned Tasks

### Task 21: Route Dialog Adapter And Session Controls

**Files:**
- Create: `probe_station_gui/route/dialog_adapter.py`
- Modify: `main.py`
- Test: `tests/route/test_dialog_adapter.py`
- Test: `tests/app/test_main_coordinate_feedrate.py`
- Report: `.superpowers/sdd/task-21-report.md`

**Interface target:**
- `route_dialog_defaults(route, document) -> RouteDialogDefaults`
- `open_or_update_route_measurement_dialog(...) -> object | None`
- `restore_route_measurement_state_after_design_load(...) -> RouteDialogRestorePlan`
- `route_measurement_session_start_plan(...) -> RouteMeasurementSessionStartPlan`
- `route_measurement_session_cancel_plan(...) -> RouteMeasurementSessionCancelPlan`

**Main LOC target:** reduce `main.py` by at least 180 physical lines.

**Current behavior:** Opening route controls creates or updates `RouteMeasurementDialog`, wires all signals, restores session state after design load, starts waiting measurements when requested, and handles session start/cancel state.

**Structural improvement:** Move route dialog construction/default path/session planning into a route dialog adapter module. `Main` remains the Qt owner of existing callbacks and status emission, but no longer contains dialog construction and route session planning bodies.

**Validation:** Add pure route tests for defaults/session plans and app characterization for open/update/session start/cancel behavior. Run `tests\route\test_dialog_adapter.py`, focused app tests, full suite, ruff, lizard/radon/Wily.

### Task 22: Route Dialog Runtime State Sink

**Files:** `probe_station_gui/route/dialog_adapter.py`, `main.py`, route/app tests.

**Main LOC target:** reduce by at least 180 lines.

**Scope:** Move `Main` route dialog UI update fanout (`set_status`, `set_progress`, `set_result`, running/waiting/session active/current point) into a small adapter object that owns only dialog-facing calls.

### Task 23: Route Telegram Photo Adapter

**Files:** create `probe_station_gui/route/telegram_adapter.py`, modify `main.py`, route/app tests.

**Main LOC target:** reduce by at least 220 lines.

**Scope:** Move route photo/contact photo captioning, comparison photo combination, and route-start/finish Telegram message formatting out of `Main`. Keep Telegram bot service itself outside this task.

### Task 24: API Route Artifact And Session Result Adapter

**Files:** create `probe_station_gui/route/api_artifacts.py`, modify `main.py`, route/app tests.

**Main LOC target:** reduce by at least 200 lines.

**Scope:** Move API route artifact payload/result/status formatting and artifact bookkeeping policy out of `Main`; keep thread/runner side effects in `Main`.

### Task 25: API Current Contact Measurement Module

**Files:** create `probe_station_gui/route/api_measurement.py`, modify `main.py`, route/app tests.

**Main LOC target:** reduce by at least 250 lines.

**Scope:** Move `_api_measure_current_contact` planning, payload parsing, contact context, meter settings, and response formatting behind route module helpers. `Main` keeps actual stage/LCR calls.

### Task 26: Raw Voltage Sweep Instrument Adapter

**Files:** create `probe_station_gui/instruments/api_sweep.py`, modify `main.py`, instrument/app tests.

**Main LOC target:** reduce by at least 220 lines.

**Scope:** Move `_api_raw_voltage_sweep` request parsing, sweep result formatting, and error payloads out of `Main`. Keep actual instrument calls in adapters.

### Task 27: API Stage Move Planning

**Files:** create `probe_station_gui/stage/api_moves.py`, modify `main.py`, stage/app tests.

**Main LOC target:** reduce by at least 240 lines.

**Scope:** Move `_api_move_to_coordinates`, `_api_move_feedrate`, `_api_axis_value_map`, and coordinate input normalization into stage API planning helpers. `Main` keeps stage controller invocation.

### Task 28: API Move-To-Contact Planning

**Files:** create `probe_station_gui/design/contact_navigation.py`, modify `main.py`, design/app tests.

**Main LOC target:** reduce by at least 170 lines.

**Scope:** Move `_api_move_to_contact` contact lookup, payload validation, and response planning out of `Main`. Preserve Stage movement behavior.

### Task 29: Stage Position Update Fanout

**Files:** create `probe_station_gui/stage/position_presenter.py`, modify `main.py`, stage/app/ui tests.

**Main LOC target:** reduce by at least 260 lines.

**Scope:** Move `_on_stage_position_changed` and display reconciliation planning into a presenter module. `Main` keeps Qt widget references and delegates decisions.

### Task 30: Stage Position Field Adapter

**Files:** create `probe_station_gui/views/stage_position_panel.py` or deeper existing views module, modify `main.py`, UI/app tests.

**Main LOC target:** reduce by at least 220 lines.

**Scope:** Move stage coordinate field creation, styling, edit/apply state, and display formatting out of `Main`.

### Task 31: Manual Jog Prediction Module

**Files:** create `probe_station_gui/stage/manual_jog_prediction.py`, modify `main.py`, stage/app tests.

**Main LOC target:** reduce by at least 300 lines.

**Scope:** Move manual jog prediction state transitions, stop-tail logic, and actual/estimated position reconciliation out of `Main`.

### Task 32: Coordinate Target Move Module

**Files:** create `probe_station_gui/stage/coordinate_targets.py`, modify `main.py`, stage/app tests.

**Main LOC target:** reduce by at least 300 lines.

**Scope:** Move coordinate target queueing, feedrate selection, pending target resolution, and finish-if-idle logic out of `Main`.

### Task 33: Design Document Load And Navigation Module

**Files:** create `probe_station_gui/design/navigation_adapter.py`, modify `main.py`, design/app tests.

**Main LOC target:** reduce by at least 400 lines.

**Scope:** Move design document load selection, visible layer parsing, point selection, and route-point navigation decisions out of `Main`.

### Task 34: Objective And Alignment Adapter

**Files:** create `probe_station_gui/design/objective_alignment.py`, modify `main.py`, design/app tests.

**Main LOC target:** reduce by at least 320 lines.

**Scope:** Move objective profile add/delete/offset/reference and manual alignment capture planning out of `Main`.

### Task 35: Telegram Bot Command Module

**Files:** create `probe_station_gui/notifications/telegram_commands.py`, modify `main.py`, notification/app tests.

**Main LOC target:** reduce by at least 280 lines.

**Scope:** Move Telegram command parsing/status text/route action response construction out of `Main`; keep bot service wiring in `Main`.

### Task 36: Menu Dock And Window Construction Split

**Files:** create focused view/window factory modules, modify `main.py`, UI/app tests.

**Main LOC target:** reduce by at least 600 lines.

**Scope:** Move `_setup_menus`, `_create_dock_widgets`, and lazy window creation wiring into focused modules without changing startup heaviness.

## Stop Conditions

- Stop and rethink if a task increases `main.py` LOC.
- Stop and rethink if a new module is shallow by deletion test: deleting it would simply move unchanged complexity back to `Main`.
- Stop and split if a task touches route Pause Request / Pause Ack / Interrupt semantics beyond direct UI adapter calls.
- Stop and split if a task requires hardware automation.
