# Probe Station GUI

This context describes the probe-station control language and the maintenance language used when refactoring the application without changing external behaviour.

## Refactoring Workflow

For the current architecture refactoring branch, the user has explicitly requested subagents. Use `subagent-driven-development` as the main execution workflow for independent refactor tasks: dispatch a focused implementer subagent per task, run a task-scoped reviewer subagent after each implementation, and run a broad whole-branch review before finishing. This is an explicit authorization to use subagents for this refactoring work without asking again for each independent task, while still keeping conflicting implementation edits sequential and review-gated.

Use `wily` as the primary metrics comparison tool for refactor passes, especially when comparing between commits. On Windows, always force UTF-8 before Wily or other emoji/Unicode-heavy tooling: `$env:PYTHONIOENCODING='utf-8'; $env:PYTHONUTF8='1'; [Console]::OutputEncoding = [System.Text.Encoding]::UTF8; .\.venv\Scripts\python.exe -X utf8 -m wily ...`. The default cp1251 console can crash on Wily's emoji output or any non-ASCII metric/report text, and this has happened before in this project. Keep Wily cache outside the repo, such as under `%TEMP%`, unless the user explicitly asks to persist it. Run Wily build/diff from a clean temporary clone or disposable worktree, not the main checkout: Wily checks out historical revisions and has left this working copy detached with old `main.py` content before. Use `radon`/`lizard` as fallback or spot-check tools when Wily cannot produce the required detail.

`lizard` is installed in the shared virtual environment as `1.23.0`, which is the current PyPI latest as of 2026-06-30. Use `-Eduplicate` for duplicate-block detection; `-Elizardduplicate` is not a valid extension name. Treat lizard duplicate counts as a coarse signal: Task 55 found whole-branch duplicate blocks worse than `main` (`383 -> 406`) while warning-level functions improved (`69 -> 23`), so duplicate cleanup remains a follow-up rather than proof that the refactor failed.

Task 55 whole-branch hardening compared `main` with the current refactor branch. Key production reductions: `main.py` `13424 -> 7545 LOC` and Wily cyclomatic `2661 -> 1385`; old `probe_station_gui/stage_controller.py` `7173 LOC / CC 1530` is replaced by smaller stage modules; old `probe_station_gui/route_measurement.py` `4031 LOC / CC 747` is now centered on `route/measurement.py` `2479 LOC / CC 379`; `views/joystick_window.py` `3070 -> 2254 LOC`; `dialogs/route_measurement_dialog.py` `2363 -> 1387 LOC`; old `settings_manager.py` `3046 LOC / CC 479` is now `settings/manager.py` `1006 LOC / CC 142`; old `lcr_meter.py` `2224 LOC / CC 455` is now `instruments/meters/lcr.py` `1398 LOC / CC 273`. Full-suite verification for the hardening pass was `1240 passed, 2 skipped`, configured ruff passed, and coverage stayed at `75%`.

The refactor intentionally removed legacy compatibility wrappers at the user's direction. Treat public API changed as `yes` for those old module-level wrappers; package-level lazy exports and active application-facing APIs remain covered. Known residual risks after Task 55: duplicate-block cleanup is still needed in `api/server.py` and route tests; vulture candidates are suspicious rather than automatically true; the existing serial-terminal ordinary-command needles-state invalidation gap appears inherited from `main`; hardware smoke has not been run.

The active refactoring roadmap is `.superpowers/sdd/refactor-plan.md`; it tracks the `main.py`, production-monolith, test-monolith, and final hardening phases. The final branch handoff is `docs/superpowers/plans/2026-06-30-architecture-refactor-handoff.md`. The active `main.py` size contract is `.superpowers/sdd/main-loc-contract.md`. Treat it as the completed contract for this refactor track: baseline `main.py` was `8948 LOC` by `radon raw` at `19ccb0b`, and the target `<= 7550 LOC` was met at `7545 LOC`.

## Language

### Coordinate Systems

**Coordinate System**:
A user-selectable coordinate view for displayed positions and relative movement. Machine, Design, and Custom are Coordinate Systems.
_Avoid_: Coordinate Frame, WCO

**Coordinate Frame**:
A stored registration of transform, readiness, and provenance that relates a Coordinate System to the Stage.
_Avoid_: Coordinate System, WCO

## Microscope Distortion Calibration

The project-specific lens distortion calibration protocol is seam-debug based. Do not treat it as a generic grid-pitch calibration, and do not assume the physical grid pitch is known unless the whole 50 um grid calibration target fits in the current objective FOV.

Use the bright test structure as a reference object and move it through the camera field: center/control, left/right edge positions, top/bottom edge positions, and the four corner positions. Build the resulting seam mosaics from stage coordinates only, without feature-alignment shifts, and judge/optimize the distortion model by whether the same physical structure has matching contours on the vertical, horizontal, and corner seams. Stage coordinates are the first reference for stitching; if contours do not meet, fix geometry/distortion rather than hiding it with per-tile shifts.

The repeatable capture is the `stitch_debug` scan pattern around the current stage position. It records 9 frames labeled `control`, `vertical_left`, `vertical_right`, `horizontal_top`, `horizontal_bottom`, and the four `corner_*` tiles. The normal scan code then writes raw tiles, sidecars, `microscope-scan-manifest.json`, and three diagnostic mosaics: `vertical_seam`, `horizontal_seam`, and `corner_seam`. Camera lock and flat-field/reference-flat should be enabled for live calibration captures unless deliberately testing illumination.

The regression metric used for X5 was computed offline from these seam-debug artifacts:
- Rebuild tile placements from the manifest and the persisted `PixelToStageMatrix`; do not run overlap registration or feature matching.
- Normalize photometry in the overlap only, then measure local windows along each physical seam with phase correlation.
- Keep windows with enough bright feature pixels and good correlation response; summarize residual norm as RMS, median, p90, max, and total px.
- Fit an incremental Brown-Conrady stage geometry model (`center_px`, `k1`, `k2`, `p1`, `p2`, affine `pixels_to_mm`) against the seam residual field, then validate on a second independent seam-debug capture. Penalized/free optical-center fits were checked for X5 and did not improve over the frame center, so keep `(960, 600)` as the optical center unless new data clearly proves otherwise.
- Accept a candidate only when it improves full-resolution seam metrics and the seam contours look better on vertical, horizontal, and corner mosaics. Prefer the cross-scan portable candidate over the single-scan best if they differ.

The current X5 production payload is a manually selected seam-fit Brown model: `model_type=stage_geometry`, `center_px=(960,600)`, `k1=-0.0014808576600282065`, `k2=0.0022887010231724975`, `p1=-1.2684949874243132e-05`, `p2=8.4505485043518877e-05`, with the X5 affine pixel matrix unchanged. The X5 overlap sweep then selected `0.25` as the practical large-area default: `0.30` measured slightly better RMS, but `0.25` was the better time/quality tradeoff.

The current X20 payload was calibrated on 2026-07-16 with the same seam-debug method, not the 5x5 grid workflow. Fit scan: `.scratch/x20-seam-debug-20260716-0105`; validation scan: `.scratch/x20-seam-debug-20260716-validate-0125`; report/contact sheet: `.scratch/distortion-alpha-ab/x20-seam-debug-20260716-crossval`. The selected Brown model keeps the X20 affine matrix unchanged and uses `center_px=(960,600)`, `k1=-0.05598720106581039`, `k2=0.04317294938913951`, `p1=0.0006840800681355104`, `p2=0.0007191198756683749`. Independent validation residual changed from RMS `19.481 px`, median `11.183 px`, p90 `29.210 px` to RMS `4.107 px`, median `3.161 px`, p90 `6.423 px`, max `7.295 px` at overlap `0.25`.

X50 is a separate case: the 50 um grid mostly fits at high magnification, and the current X50 payload is the older point-grid/homography-style calibration with `grid_spacing_um=50`, residual mean `0.2248 px`, max `1.1027 px`, and calibrated pixel size about `0.11674 um/px`. Do not use the X50 grid-fit workflow as the default for X5/X20 seam calibration.

For small FOV objectives such as X20, the whole surrounding bright test pattern may fill or exceed the frame. That is not a reason to switch to a "structure fully inside every frame" 5x5 grid capture. Use the seam-debug capture path and the central structure/seam agreement instead. The missing production step is to promote the scratch seam-regression scripts into a supported calibration command that takes a `stitch_debug` manifest and writes a candidate `stage_geometry` payload for the active objective.

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
