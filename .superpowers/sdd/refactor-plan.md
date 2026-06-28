# Architecture Refactoring Plan

Updated: 2026-06-28
Branch: `codex/refactor-stage-controller`
Current completed task: Task 40, coordinate move lifecycle/cancel.
Active next task: Task 41, microscope scan workflow.

This plan supersedes the original 5-task architecture sketch. It reflects the current code shape after Tasks 1-38 and the current LOC audit.

## Current Size Baseline

`main.py`:

- `radon raw main.py`: `LOC 8446`, `SLOC 7898`
- baseline before the current Phase 1 contract: `LOC 8948`, `SLOC 8400`
- Binding target contract: `.superpowers/sdd/main-loc-contract.md`

Largest production files by physical line count:

| File | Lines | Current problem |
| --- | ---: | --- |
| `main.py` | 8410 | central Qt/API/stage/route adapter still owns too many workflows |
| `probe_station_gui/route/measurement.py` | 2978 | route runner still mixes movement, autofocus, contact placement, measurement, confirmation, and artifacts |
| `probe_station_gui/views/design_navigator_panel.py` | 2861 | plotting, route editing, tools, state enablement, and layout-window adapter live together |
| `probe_station_gui/views/joystick_window.py` | 2832 | jog UI, keyboard handling, feedrate UI, serial commands, and panel state are coupled |
| `probe_station_gui/dialogs/route_measurement_dialog.py` | 1992 | Qt construction, profile persistence, runtime controls, histogram/raw data presentation mixed |
| `probe_station_gui/instruments/meters/lcr.py` | 1853 | instrument worker, meter capabilities, polling, and route-meter adapter logic mixed |
| `probe_station_measure/instrument_drivers/Keithley/Keithley_2400_2182A.py` | 1538 | external driver wrapper likely needs an adapter/facade pass before editing internals |
| `probe_station_gui/settings/manager.py` | 1498 | settings load/save/migration and section compatibility remain concentrated |
| `probe_station_gui/dialogs/settings_dialog.py` | 1433 | settings UI still mirrors section knowledge |
| `probe_station_gui/views/microscope_view.py` | 1431 | rendering, overlay state, minimap, input handling, and zoom behavior mixed |

Largest test files:

| File | Lines | Current problem |
| --- | ---: | --- |
| `tests/app/test_main_coordinate_feedrate.py` | 5763 | multiple `Main` adapter suites in one file |
| `tests/stage/test_controller.py` | 3524 | stage controller behavior tests not split by module responsibility |
| `tests/route/test_measurement.py` | 3436 | route runner characterization is monolithic |
| `tests/instruments/meters/test_lcr.py` | 1072 | LCR worker/capability/adapter tests mixed |

## Global Rules

- Preserve external behavior unless the user explicitly requests a functional change.
- Use Superpowers and subagent-driven development for implementation tasks.
- For each refactor pass, create a brief with:
  - current behavior;
  - structural improvement;
  - validation check;
  - baseline metrics and target metrics.
- Run focused tests, full `pytest tests`, configured `ruff check --ignore E402,F401 .`, and Wily/radon/lizard metrics before commit.
- Commit each completed, reviewed pass separately.
- Wily must run from a disposable UTF-8 temp clone/cache, never from the main checkout.
- Route Pause/Resume/Interrupt semantics are safety-critical; any route-control touch requires regression coverage.

## Phase 1: Finish Active Stage-Motion / `main.py` Track

Goal: finish the first two HTML directions (`route operation` and `stage motion / FluidNC seam`) enough that `main.py` drops below the contract target and the remaining stage hotspots are no longer the worst architectural drag.

Contract: `.superpowers/sdd/main-loc-contract.md`

| Task | Scope | Current behavior | Structural improvement | Validation check | Target |
| --- | --- | --- | --- | --- | --- |
| 38 | `stage.api_moves` | API coordinate move planning is pure but warning-level: 119 NLOC / CCN 20. | Split validation/planning helpers without touching GUI/stage side effects. | `tests/stage/test_api_moves.py`, `tests/app/test_main_coordinate_feedrate.py`, full suite. | No `main.py` increase; `api_coordinate_move_plan` below warning threshold. |
| 39 | Stage position update adapter | `Main._on_stage_position_changed` owns status, prediction reconciliation, design position, B-axis invalidation, contact calibration, idle cleanup. | Extract stage-position update adapter/presenter; `Main` applies a plan and keeps Qt/controller side effects. | `tests/app/test_main_planned_move_prediction.py`, `tests/stage/test_position_presenter.py`, focused app coordinate tests, full suite. | `main.py <= 8750 LOC`; hotspot CCN below 15. |
| 40 | Coordinate move lifecycle/cancel | `Main.on_move_finished`, `_has_cancelable_operation`, `_cancel_stage_coordinate_action` mix task lifecycle, UI state, and stage coordinate cleanup. | Extract coordinate move lifecycle/cancel decision helpers into a stage UI adapter module; keep controller calls in `Main`. | Coordinate feedrate/app tests, stage coordinate target tests, full suite. | `main.py <= 8550 LOC`; remove at least one lizard warning. |
| 41 | Microscope scan workflow | `Main` owns scan start, run loop, settle, tile capture, mosaic save, manifest write, finish UI. | Move scan planning/filesystem payload helpers into a microscope scan module; keep thread/Qt updates in `Main`. | Existing microscope scan/app tests plus new pure tests for manifest/tile payloads. | `main.py <= 8250 LOC`. |
| 42 | Contact seek and saved positions | `Main` owns contact seek request/run/finish plus saved needle/surface/sample position helpers. | Extract contact seek presentation/result planning and saved-position payload helpers. | Contact seek/app tests, stage needle settings tests, full suite. | `main.py <= 8000 LOC`. |
| 43 | Serial/controller connection UI flow | `Main` owns serial connected/disconnected, auto-connect, startup sync, reboot recovery, feedrate preferences, controller-state persistence glue. | Move connection-state UI orchestration into a main-window adapter module; keep actual serial/stage behavior unchanged. | Serial connection/terminal/stage controller tests, app smoke, full suite. | `main.py <= 7750 LOC`. |
| 44 | Route/UI residue cleanup | Many thin `Main` wrappers remain after route/design/API/Telegram/objective extraction. | Delete/consolidate wrappers that no longer add locality; move cohesive adapter groups to existing modules. | Full route/app/API/Telegram/design focused suites, full suite. | `main.py <= 7550 LOC`. |

Phase 1 is complete only when:

- `main.py <= 7550 LOC` by `radon raw`;
- Wily `main.py` cyclomatic is below the `19ccb0b` baseline;
- full tests and configured ruff pass;
- no unresolved important review findings remain.

## Phase 2: Production Monolith Track

Goal: after `main.py` is no longer the dominant blocker, reduce navigation cost in the next production monoliths. These tasks should not start until Phase 1 is complete unless a Phase 1 pass directly requires touching one of these files.

| Task | Scope | Current behavior | Structural improvement | Validation check | Target |
| --- | --- | --- | --- | --- | --- |
| 45 | `route/measurement.py` | Runner still coordinates movement, autofocus, photo capture, contact placement, measurement reading, confirmation, and result recording. | Split remaining runner internals by route-contact lifecycle and artifact/result recording while preserving `RouteMeasurementRunner` interface. | `tests/route/test_measurement.py`, route interrupt/autofocus/contact tests, full suite. | File below 2500 lines; remove warning-level runner helpers. |
| 46 | `views/design_navigator_panel.py` | Plot pane, route editing, tool state, enablement policy, and layout-window adapter are mixed. | Split plot rendering/tool policy from panel adapter; preserve signals and UI copy. | `tests/ui/test_design_navigator_panel.py`, design workflow/navigation tests, full suite. | File below 2300 lines; `_update_enabled_state` below warning threshold. |
| 47 | `views/joystick_window.py` | Jog UI, keyboard controls, feedrate controls, serial command intent, and panel state are mixed. | Extract keyboard/jog presentation and feedrate sections into focused view helpers. | `tests/ui/test_joystick_feedrate.py`, key binding tests, stage jog command tests, full suite. | File below 2300 lines. |
| 48 | `dialogs/route_measurement_dialog.py` | Qt construction, profile persistence, run controls, histogram/raw data display live together. | Split profile persistence and presentation modules; keep dialog as Qt adapter. | Route dialog/run UI tests and route measurement tests. | File below 1600 lines; histogram paint/control state helpers below warning threshold. |
| 49 | `instruments/meters/lcr.py` | Worker scheduling, live polling, capabilities, and route-meter behavior are mixed. | Separate instrument worker lifecycle from meter capabilities and adapter code. | LCR worker/GW Instek/instrument tests, full suite. | File below 1400 lines. |
| 50 | Settings manager/dialog | Settings parsing, migration, defaults, and UI section knowledge are still duplicated. | Move remaining settings sections behind section modules and metadata consumed by UI. | Settings tests, key binding/feedrate/needle calibration tests, full suite. | `settings/manager.py < 1100`, `settings_dialog.py < 1100`. |

## Phase 3: Test Monolith Track

Goal: test files should be navigable by responsibility. This is not cosmetic: review and future refactor safety are slower while major suites are 3k-5k line files.

| Task | Scope | Current behavior | Structural improvement | Validation check | Target |
| --- | --- | --- | --- | --- | --- |
| 51 | `tests/app/test_main_coordinate_feedrate.py` | Many unrelated `Main` adapter behaviors live in one 5763-line file. | Split by feature area: API coordinate moves, route API, manual jog prediction, stage panel, route control, Telegram/app adapter. | Run split files plus full suite; ensure no shared fixture behavior changes. | No app test file above 1800 lines. |
| 52 | `tests/stage/test_controller.py` | Stage controller tests are not split by current module responsibilities. | Split by controller seam: status/session, motion commands, needle actions, connection state, cache/import. | Stage test subset and full suite. | No stage controller test file above 1600 lines. |
| 53 | `tests/route/test_measurement.py` | Route runner characterization is monolithic. | Split by lifecycle: start/finish, contact seek/placement, interrupt/pause, recording/artifacts, config. | Route test subset and full suite. | No route measurement test file above 1600 lines. |
| 54 | Instrument tests | LCR and Keithley tests mix capabilities, worker behavior, and driver behavior. | Split LCR worker/capability/adapter tests and Keithley driver tests by behavior. | Instrument test subset and full suite. | No instrument test file above 900 lines. |

## Phase 4: Final Branch Hardening

| Task | Scope | Current behavior | Structural improvement | Validation check | Target |
| --- | --- | --- | --- | --- | --- |
| 55 | Whole-branch metrics and review | Branch contains many behavior-preserving refactor passes. | Compare branch against `main`, run whole-branch code review, and identify cleanup/follow-up tasks. | Full tests, ruff, Wily/radon/lizard vs `main`, import smoke, final reviewer. | No unresolved blocking/important findings. |
| 56 | Documentation cleanup | Refactor reports/contracts are scattered across SDD files and context. | Summarize final architecture seams, metrics, and remaining known risks in `CONTEXT.md`/docs. | Docs review and final status check. | Handoff is enough for future agents/humans. |

## Separate Migration Tasks

These are intentionally not part of the behavior-preserving refactor passes:

- Any dependency upgrade or framework migration.
- Any public API/client method rename.
- Any GUI label/workflow change.
- Any route-control semantic change.
- Any hardware protocol behavior change.
- Any import-path breaking migration without compatibility period.

## Next Action

Start Task 41 from the `main.py` LOC contract:

1. Create a Task 41 brief with current behavior, structural improvement, validation checks, baseline metrics, and LOC target.
2. Use subagents for implementation review/spec review where useful.
3. Run Wily metrics from a disposable UTF-8 temp clone/cache.
4. Run full `pytest tests` and configured ruff.
5. Commit Task 41 separately.
