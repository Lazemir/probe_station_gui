# Architecture Refactoring Plan

Updated: 2026-06-29
Branch: `codex/refactor-stage-controller`
Current completed task: Task 54a, LCR meter test split.
Active next task: Task 55, whole-branch metrics and final review.

This plan supersedes the original 5-task architecture sketch. It reflects the current code shape after Tasks 1-38 and the current LOC audit.

## Current Size Baseline

`main.py`:

- `radon raw main.py`: `LOC 7545`, `SLOC 6997`
- baseline before the current Phase 1 contract: `LOC 8948`, `SLOC 8400`
- Binding target contract: `.superpowers/sdd/main-loc-contract.md`

Largest production files by physical line count:

| File | Lines | Current problem |
| --- | ---: | --- |
| `main.py` | 7545 | still central, but Phase 1 target is met; remaining work should avoid using it as the only sink |
| `probe_station_gui/route/measurement.py` | 2479 | route runner still coordinates route point orchestration and confirmation, but contact lifecycle/readout/recording policy are now separate modules |
| `probe_station_gui/views/joystick_window.py` | 2254 | jog execution, keyboard handling, serial commands, homing/needle UI, and panel state remain; feedrate panel and helper widgets are isolated |
| `probe_station_gui/views/design_navigator_panel.py` | 1849 | route editing/tool state and layout-window adapter remain; plot rendering and enablement policy are now isolated |
| `probe_station_measure/instrument_drivers/Keithley/Keithley_2400_2182A.py` | 1698 | external driver wrapper likely needs an adapter/facade pass before editing internals |
| `probe_station_gui/views/microscope_view.py` | 1567 | rendering, overlay state, minimap, input handling, and zoom behavior mixed |
| `probe_station_gui/instruments/meters/lcr.py` | 1398 | instrument facade is below target; remaining worker/capability internals should only move with focused tests |
| `probe_station_gui/dialogs/route_measurement_dialog.py` | 1387 | profile persistence/result views are split; remaining run controls and Qt construction still share the dialog adapter |
| `probe_station_gui/settings/manager.py` | 1006 | settings load/save/migration and remaining section orchestration are still concentrated, but section parsers now live in focused settings modules |
| `probe_station_gui/dialogs/settings_dialog.py` | 1091 | settings dialog is below target; logging/API/Telegram/Needles/Axis widgets remain inline |

Largest test files:

| File | Lines | Current problem |
| --- | ---: | --- |
| `tests/stage/test_controller.py` | 1501 | remaining startup/absolute/status/autofocus/objective tests are still together, but stage controller test files are now below the size target |
| `tests/app/test_main_route_measurement_session.py` | 1445 | largest remaining app split file; route session/start tests are isolated and below target |
| `tests/app/test_main_meter_contact_actions.py` | 1306 | meter/contact API action tests are isolated and below target |
| `tests/route/test_measurement.py` | 1178 | remaining route lifecycle characterization; below target after route test splits |
| `tests/route/test_measurement_readout_quality.py` | 1055 | split readout/recording/contact-quality characterization; below target |
| `tests/route/test_measurement_contact_seek.py` | 1031 | split contact seek characterization; below target |
| `tests/instruments/meters/test_lcr.py` | 861 | remaining LCR controller/session characterization; below target after RouteMeter split |
| `tests/instruments/test_probe_station_measure_keithley.py` | 628 | Keithley driver tests are below target; no split needed for current metric gate |
| `tests/instruments/meters/test_lcr_route_meter.py` | 496 | split RouteMeter/VISA/connect-now characterization; below target |
| `tests/route/test_measurement_api_route_control.py` | 546 | split API route control characterization; below target |

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
| 41 | Microscope scan workflow | `Main` owns scan start, run loop, settle, tile capture, mosaic save, manifest write, finish UI. | Move scan planning/filesystem payload helpers into a microscope scan module; keep thread/Qt updates in `Main`. | Existing microscope scan/app tests plus new pure tests for manifest/tile payloads. | Completed as LOC-gate exception: `8371 LOC`; Wily cyclomatic `1591 -> 1587`. |
| 42 | Contact seek and saved positions | `Main` owns contact seek request/run/finish plus saved needle/surface/sample position helpers. | Extract contact seek presentation/result planning, saved-position payload helpers, and sample handling stage sequences while preserving owner-level tunables. | Contact seek/app tests, stage needle settings tests, full suite. | Completed: `main.py 8371 -> 7961 LOC`; Wily cyclomatic `1587 -> 1502`; target passed. |
| 43 | Serial/controller connection UI flow | `Main` owns serial connected/disconnected, auto-connect, startup sync, reboot recovery, feedrate preferences, controller-state persistence glue. | Move connection-state UI orchestration into a main-window adapter module; keep actual serial/stage behavior unchanged. | Serial connection/terminal/stage controller tests, app smoke, full suite. | Completed: `main.py 7961 -> 7720 LOC`; Wily cyclomatic `1502 -> 1451`; target passed. |
| 44 | Route/UI residue cleanup | Many thin `Main` wrappers remain after route/design/API/Telegram/objective extraction. | Delete/consolidate wrappers that no longer add locality; move cohesive adapter groups to existing modules. | Full route/app/API/Telegram/design focused suites, full suite. | Completed: `main.py 7720 -> 7545 LOC`; Wily cyclomatic `1451 -> 1385`; target passed. |

Phase 1 is complete:

- `main.py <= 7550 LOC` by `radon raw`;
- Wily `main.py` cyclomatic is below the `19ccb0b` baseline;
- full tests and configured ruff pass;
- no unresolved important review findings remain.

## Phase 2: Production Monolith Track

Goal: after `main.py` is no longer the dominant blocker, reduce navigation cost in the next production monoliths. These tasks should not start until Phase 1 is complete unless a Phase 1 pass directly requires touching one of these files.

| Task | Scope | Current behavior | Structural improvement | Validation check | Target |
| --- | --- | --- | --- | --- | --- |
| 45 | `route/measurement.py` | Runner coordinated movement, autofocus, photo capture, contact placement, measurement reading, confirmation, and result recording. | Split route-contact lifecycle, contact readout/seek, and record/result policy while preserving `RouteMeasurementRunner` interface and route control semantics. | `tests/route/test_measurement.py`, route interrupt/autofocus/contact tests, full suite. | Completed: 45a extracted contact lifecycle (`measurement.py 3176 -> 2988 LOC`, Wily cyclomatic `520 -> 493`, runner `place_contact`/`prepare_external_contact` CC `11/10 -> 1/1`); 45b extracted contact measurement/readout/seek (`measurement.py 2988 -> 2650 LOC`, Wily SLOC `2775 -> 2445`, Wily cyclomatic `493 -> 417`, runner contact-readout helper CCs down to `1`, new `route/contact_measurement.py` 751 LOC / MI 17.18 / max CC 8); 45c extracted record/result policy (`measurement.py 2650 -> 2479 LOC`, Wily SLOC `2445 -> 2289`, Wily cyclomatic `417 -> 379`, new `route/measurement_recording.py` 211 LOC / MI 37.05 / max CC 8 / 100% coverage). Target passed: file is below 2500 lines, no lizard warnings in touched route slice. |
| 46 | `views/design_navigator_panel.py` | Plot pane, route editing, tool state, enablement policy, and layout-window adapter were mixed. | Split plot rendering and enablement policy from panel adapter; preserve signals and UI copy. | `tests/ui/test_design_navigator_panel.py`, design workflow/navigation tests, full suite. | Completed: 46a extracted `_DesignPlotPane` to `views/design_plot_pane.py` (`design_navigator_panel.py 3083 -> 1818 LOC`, Wily SLOC `2848 -> 1670`, Wily cyclomatic `535 -> 251`; new plot module `1286 LOC`, Wily cyclomatic `284`). 46b extracted `DesignNavigatorEnablement` (`design_navigator_panel.py 1818 -> 1849 LOC`, Wily file cyclomatic `251 -> 238`, `_update_enabled_state` CC `29 -> 1`, new enablement module `82 LOC` / MI 38.89 / 100% coverage). Target passed: file is below 2300 lines and `_update_enabled_state` is below warning threshold. |
| 47 | `views/joystick_window.py` | Jog UI, keyboard controls, feedrate controls, serial command intent, and panel state were mixed. | Extract feedrate and helper widget sections into focused view helpers while preserving jog/serial behavior. | `tests/ui/test_joystick_feedrate.py`, key binding tests, stage jog command tests, full suite. | Completed: 47a extracted feedrate controls to `views/joystick/feedrate_panel.py` (`joystick_window.py 3059 -> 2372 LOC`, Wily SLOC `2822 -> 2189`, Wily cyclomatic `644 -> 509`; new feedrate module `709 LOC`, max CC 10). 47b extracted helper widgets to `views/joystick/widgets.py` (`joystick_window.py 2372 -> 2254 LOC`, Wily SLOC `2189 -> 2094`, Wily cyclomatic `509 -> 478`; new widgets module `134 LOC` / MI 49.68). Target passed: `joystick_window.py < 2300 LOC`. Residual known hotspot: `_apply_axes` remains 55 NLOC / CCN 17 and should be handled only with explicit characterization of `$J`, stop/resend, and controller coordination. |
| 48 | `dialogs/route_measurement_dialog.py` | Qt construction, profile persistence, run controls, histogram/raw data display live together. | Split profile persistence and presentation modules; keep dialog as Qt adapter. | Route dialog/run UI tests and route measurement tests. | Completed: 48a extracted SI-prefix and histogram/raw-data presentation (`route_measurement_dialog.py 2179 -> 1809 LOC`, Wily cyclomatic `324 -> 249`). 48b extracted defaults/profile persistence (`route_measurement_dialog.py 1809 -> 1390 LOC`, Wily cyclomatic `249 -> 176`, coverage `72% -> 73%`). 48c extracted operation-state policy (`_update_operation_state` CCN `22 -> 3`, dialog Wily cyclomatic `176 -> 156`). 48d split histogram painting (`paintEvent` CCN `24 -> 4`, lizard result-view warnings `1 -> 0`; aggregate Wily file metrics worsened, so treat this only as a hotspot cleanup). 48e split profile path/meter helpers (`_apply_profile_data` CCN `16 -> 5`, profile Wily cyclomatic `77 -> 76`, Task 48 lizard warnings `1 -> 0`). Target passed: `route_measurement_dialog.py < 1500 LOC`, Task 48 slice has no lizard warning-level functions. |
| 49 | `instruments/meters/lcr.py` | Worker scheduling, live polling, capabilities, and route-meter behavior are mixed. | Separate instrument worker lifecycle from meter capabilities and adapter code. | LCR worker/GW Instek/instrument tests, full suite. | Completed: 49a extracted low-level VISA operation dispatch into `instruments/meters/lcr_visa.py` while preserving `lcr.py` facade (`lcr.py 2010 -> 1938 LOC`, Wily cyclomatic `395 -> 371`, `_session_visa_operation` CCN `22 -> 2`, coverage `73% -> 74%`). 49b extracted shared route-session configuration/read/batch helpers into `instruments/meters/lcr_route_session.py` (`lcr.py 1938 -> 1800 LOC`, Wily cyclomatic `371 -> 355`, route adapter helper CCs below warning threshold, coverage stayed `74%`). 49c extracted GW Instek session implementation into `instruments/meters/gwinstek_session.py` (`lcr.py 1800 -> 1495 LOC`, Wily cyclomatic `355 -> 300`, moved `configure_measurement` CCN `12 -> 5`, coverage stayed `74%`). 49d extracted worker scheduling into `MeterWorkerRuntime` (`lcr.py 1495 -> 1398 LOC`, Wily cyclomatic `300 -> 273`, full suite `1232 passed, 2 skipped`). Target passed: `lcr.py < 1400 LOC`, no lizard warnings in touched slice. |
| 50 | Settings manager/dialog | Settings parsing, migration, defaults, and UI section knowledge are still duplicated. | Move remaining settings sections behind section modules and metadata consumed by UI. | Settings tests, key binding/feedrate/needle calibration tests, full suite. | Completed: 50a extracted first-run default settings JSON normalization into `settings/default_file.py` and split `_load` orchestration (`settings/manager.py 1660 -> 1327 LOC`, Wily cyclomatic `214 -> 174`, `_ensure_default_file` CC `35 -> 4`, `_load` CC `27 -> 1`, full suite `1234 passed, 2 skipped`). 50b extracted `MeasurementSettingsWidget` to `dialogs/settings/measurement.py` and added direct UI characterization (`settings_dialog.py 1613 -> 1317 LOC`, Wily cyclomatic `267 -> 218`, `_update_lcr_control_state` CC `21 -> 4`, full suite `1237 passed, 2 skipped`, coverage `75%`). 50c extracted objective and coordinate-system widgets (`settings_dialog.py 1317 -> 1091 LOC`, Wily cyclomatic `218 -> 179`, touched-slice cyclomatic `218 -> 215`, full suite `1240 passed, 2 skipped`). 50d extracted remaining objective, feedrate, and needle calibration section parsers from `SettingsManager` (`settings/manager.py 1327 -> 1006 LOC`, Wily cyclomatic `174 -> 142`, `parse_needle_calibration_settings` CC `24 -> 2`, full suite `1240 passed, 2 skipped`, coverage `75%`). Target passed: `settings_dialog.py < 1100` and `settings/manager.py < 1100`. |

## Phase 3: Test Monolith Track

Goal: test files should be navigable by responsibility. This is not cosmetic: review and future refactor safety are slower while major suites are 3k-5k line files.

| Task | Scope | Current behavior | Structural improvement | Validation check | Target |
| --- | --- | --- | --- | --- | --- |
| 51 | `tests/app/test_main_coordinate_feedrate.py` | Many unrelated `Main` adapter behaviors lived in one 6585-line file. | Split by feature area: API coordinate moves, route API, manual jog prediction, stage panel, route control, Telegram/app adapter. | Run split files plus full suite; ensure no shared fixture behavior changes. | Completed: 51a extracted shared app-test support to `tests/app/main_coordinate_feedrate_support.py` (`test_main_coordinate_feedrate.py 6585 -> 5696 LOC`, Wily cyclomatic `389 -> 200`, new support module `947 LOC` / Wily cyclomatic `185`, full suite `1240 passed, 2 skipped`, direct unittest execution preserved). 51b split stage/coordinate/cancel/home tracking tests into `tests/app/test_main_stage_coordinate_controls.py` (`test_main_coordinate_feedrate.py 5696 -> 4941 LOC`, Wily cyclomatic `200 -> 158`, new file `766 LOC` / Wily cyclomatic `44`, 12 stale imports removed, full suite `1240 passed, 2 skipped`, direct unittest execution preserved). 51c split API Route Control tests into `tests/app/test_main_route_control.py` (`test_main_coordinate_feedrate.py 4941 -> 3711 LOC`, Wily cyclomatic `158 -> 99`, new file `1249 LOC` / Wily cyclomatic `61`, 4 stale imports removed, full suite `1240 passed, 2 skipped`, direct unittest execution preserved). 51d split route session/start tests into `tests/app/test_main_route_measurement_session.py` (`test_main_coordinate_feedrate.py 3711 -> 2292 LOC`, Wily cyclomatic `99 -> 65`, Wily MI `0 -> 5.41`, new file `1445 LOC` / Wily cyclomatic `36` / MI `13.42`, full suite `1240 passed, 2 skipped`, direct unittest execution preserved). 51e split raw meter/contact API action tests into `tests/app/test_main_meter_contact_actions.py` (`test_main_coordinate_feedrate.py 2292 -> 1002 LOC`, Wily cyclomatic `65 -> 31`, Wily MI `5.41 -> 20.60`, new file `1306 LOC` / Wily cyclomatic `36` / MI `16.48`, full suite `1240 passed, 2 skipped`, direct unittest execution preserved). Target passed: no app test file in this split is above 1800 lines; former app monolith is `1002 LOC`. |
| 52 | `tests/stage/test_controller.py` | Stage controller tests are not split by current module responsibilities. | Split by controller seam: status/session, motion commands, needle actions, connection state, cache/import. | Stage test subset and full suite. | Completed: 52a extracted shared stage controller test support and split A-axis/needles integration tests into `tests/stage/test_controller_axis_needles.py` (`test_controller.py 4056 -> 2751 LOC`, Wily cyclomatic `257 -> 141`, new axis/needles file `1112 LOC` / MI `11.25`, support file `267 LOC` / MI `35.18`, full suite `1240 passed, 2 skipped`). 52b split status refresh, startup sync, and reconnect/cache tests into `tests/stage/test_controller_status_session.py` (`test_controller.py 2751 -> 2063 LOC`, Wily cyclomatic `141 -> 110`, new status/session file `714 LOC` / MI `22.14`, full suite `1240 passed, 2 skipped`). 52c split jog queue and motion-safety-bypass tests into `tests/stage/test_controller_jog_motion.py` (`test_controller.py 2063 -> 1501 LOC`, Wily cyclomatic `110 -> 70`, Wily MI `0 -> 4.65`, new jog/motion file `589 LOC` / MI `21.23`, full suite `1240 passed, 2 skipped`). Target passed: no stage controller test file above 1600 lines. |
| 53 | `tests/route/test_measurement.py` | Route runner characterization was monolithic. | Split by lifecycle: start/finish, contact seek/placement, interrupt/pause, recording/artifacts, config. | Route test subset and full suite. | Completed: 53a extracted shared route test support and split API route control tests into `tests/route/test_measurement_api_route_control.py` (`test_measurement.py 3978 -> 3201 LOC`, Wily cyclomatic `299 -> 192`, new support `299 LOC` / MI `26.97`, new API route-control file `546 LOC` / MI `26.49`, touched-slice Wily cyclomatic `299 -> 294`, full suite `1240 passed, 2 skipped`). 53b split contact placement / auto-contact seek / runtime remeasure tests into `tests/route/test_measurement_contact_seek.py` (`test_measurement.py 3201 -> 2198 LOC`, new contact seek file `1031 LOC` / MI `16.71`, touched-slice Wily cyclomatic `192 -> 157`, full suite `1240 passed, 2 skipped`). 53c split readout / recording / contact-quality / interactive confirmation tests into `tests/route/test_measurement_readout_quality.py` (`test_measurement.py 2198 -> 1178 LOC`, new readout/quality file `1055 LOC` / MI `14.34`, touched-slice Wily cyclomatic `116 -> 113`, full suite `1240 passed, 2 skipped`). Target passed: no route measurement test file above 1600 lines. |
| 54 | Instrument tests | LCR and Keithley tests mixed capabilities, worker behavior, and driver behavior. | Split LCR worker/capability/adapter tests and Keithley driver tests by behavior. | Instrument test subset and full suite. | Completed: 54a extracted LCR test support and split RouteMeter / VISA / connect-now tests into `tests/instruments/meters/test_lcr_route_meter.py` (`test_lcr.py 1607 -> 861 LOC`, Wily cyclomatic `139 -> 28`, MI `1.96 -> 23.33`; new route-meter file `496 LOC` / MI `34.50`; support file `345 LOC` / MI `24.03`; touched-slice Wily cyclomatic `139 -> 138`, full suite `1240 passed, 2 skipped`). Target passed: no instrument test file above 900 lines. |

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

Continue Phase 4 with Task 55:

1. Run whole-branch status and metric comparison against `main` using Wily from a disposable UTF-8 temp clone/cache.
2. Compare largest files, largest functions, cyclomatic complexity, Maintainability Index, lizard warnings, vulture candidates, ruff, tests, and coverage.
3. Run final code review over the branch scope with subagents, focusing on behavior-preserving seams, compatibility wrappers, route-control safety, import/direct-execution assumptions, and newly introduced support modules.
4. Record final metrics, known residual risks, and any follow-up tasks in `CONTEXT.md` / SDD report before finishing the branch.
