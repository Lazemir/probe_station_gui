# Probe Station GUI

This context describes the probe-station control language and the maintenance language used when refactoring the application without changing external behaviour.

## Refactoring Workflow

For the current architecture refactoring branch, the user has explicitly requested subagents. Use `subagent-driven-development` as the main execution workflow for independent refactor tasks: dispatch a focused implementer subagent per task, run a task-scoped reviewer subagent after each implementation, and run a broad whole-branch review before finishing. This is an explicit authorization to use subagents for this refactoring work without asking again for each independent task, while still keeping conflicting implementation edits sequential and review-gated.

Use `wily` as the primary metrics comparison tool for refactor passes, especially when comparing between commits. On Windows, always force UTF-8 before Wily or other emoji/Unicode-heavy tooling: `$env:PYTHONIOENCODING='utf-8'; $env:PYTHONUTF8='1'; [Console]::OutputEncoding = [System.Text.Encoding]::UTF8; .\.venv\Scripts\python.exe -X utf8 -m wily ...`. The default cp1251 console can crash on Wily's emoji output or any non-ASCII metric/report text, and this has happened before in this project. Keep Wily cache outside the repo, such as under `%TEMP%`, unless the user explicitly asks to persist it. Run Wily build/diff from a clean temporary clone or disposable worktree, not the main checkout: Wily checks out historical revisions and has left this working copy detached with old `main.py` content before. Use `radon`/`lizard` as fallback or spot-check tools when Wily cannot produce the required detail.

`lizard` is installed in the shared virtual environment as `1.23.0`, which is the current PyPI latest as of 2026-06-30. Use `-Eduplicate` for duplicate-block detection; `-Elizardduplicate` is not a valid extension name. Treat lizard duplicate counts as a coarse signal: Task 55 found whole-branch duplicate blocks worse than `main` (`383 -> 406`) while warning-level functions improved (`69 -> 23`), so duplicate cleanup remains a follow-up rather than proof that the refactor failed.

Task 55 whole-branch hardening compared `main` with the current refactor branch. Key production reductions: `main.py` `13424 -> 7545 LOC` and Wily cyclomatic `2661 -> 1385`; old `probe_station_gui/stage_controller.py` `7173 LOC / CC 1530` is replaced by smaller stage modules; old `probe_station_gui/route_measurement.py` `4031 LOC / CC 747` is now centered on `route/measurement.py` `2479 LOC / CC 379`; `views/joystick_window.py` `3070 -> 2254 LOC`; `dialogs/route_measurement_dialog.py` `2363 -> 1387 LOC`; old `settings_manager.py` `3046 LOC / CC 479` is now `settings/manager.py` `1006 LOC / CC 142`; old `lcr_meter.py` `2224 LOC / CC 455` is now `instruments/meters/lcr.py` `1398 LOC / CC 273`. Full-suite verification for the hardening pass was `1240 passed, 2 skipped`, configured ruff passed, and coverage stayed at `75%`.

The refactor intentionally removed legacy compatibility wrappers at the user's direction. Treat public API changed as `yes` for those old module-level wrappers; package-level lazy exports and active application-facing APIs remain covered. Known residual risks after Task 55: duplicate-block cleanup is still needed in `api/server.py` and route tests; vulture candidates are suspicious rather than automatically true; the existing serial-terminal ordinary-command needles-state invalidation gap appears inherited from `main`; hardware smoke has not been run.

The active refactoring roadmap is `.superpowers/sdd/refactor-plan.md`; it tracks the remaining `main.py`, production-monolith, test-monolith, and final hardening phases. The active `main.py` size contract is `.superpowers/sdd/main-loc-contract.md`. Treat it as binding for the remaining refactor track: baseline `main.py` is `8948 LOC` by `radon raw` at `19ccb0b`, and the current target is `<= 7550 LOC` after the planned main-focused passes. If a pass misses its target by more than `50` LOC, the next pass must compensate or the contract must be amended with a concrete reason.

## Language

**Probe Station**:
A microscope station with a FluidNC-controlled stage, camera feedback, and electrical probes used to position needles on chip targets.
_Avoid_: CNC, microscope app

**Stage**:
The motion system controlled through FluidNC serial commands. It includes linear axes and the A-axis needle lift.
_Avoid_: CNC controller, machine

**Needles Known Raised**:
A safety state meaning the application knows the needles are raised before stage motion. Unknown needle state is not equivalent to raised.
_Avoid_: safe by default, needles ok

**Route Measurement**:
A guided sequence that moves through route points, captures optional photos, handles contact placement, and records measurement results.
_Avoid_: batch measurement, script run

**API Route Control**:
The external route-control workflow exposed through the local API. It is named after the route-control interface, not after any current client.
_Avoid_: Jupyter control, notebook control

**Pause Request**:
A request for route measurement to pause at the next safe waiting point. It is not the same as being paused.
_Avoid_: paused

**Pause Ack**:
The state where the route measurement loop has reached a safe waiting point and may show Resume controls.
_Avoid_: pause requested

**Interrupt**:
A request to stop the current route contact flow at the first regular opportunity without allowing later contact steps to continue as if nothing happened.
_Avoid_: cancel, emergency stop

**Behaviour Parity**:
The expected result of a refactor pass: external behaviour stays stable while the internal module shape changes.
_Avoid_: no-op refactor

**Refactor Pass**:
A small reviewable structural change with a named current behaviour, structural improvement, validation check, and metrics comparison against the previous pass and main.
_Avoid_: rewrite, cleanup batch

**Metrics Gate**:
The repeatable check used to decide whether a refactor pass improved maintainability without breaking behaviour. Prefer `wily` for commit-to-commit comparison, with `radon` and `lizard` used for fallback or deeper spot checks. Total LOC is a secondary signal, not an acceptance rule: LOC may grow when cyclomatic complexity, Maintainability Index, function length, locality, or test protection improve enough to justify the extra code.
_Avoid_: lint run, test run

**Regression Metric**:
A metric that becomes worse after a pass. It must either be explained as measurement noise, fixed by the pass, or split into an explicit cleanup pass before claiming improvement.
_Avoid_: acceptable churn
