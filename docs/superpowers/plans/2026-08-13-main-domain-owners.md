# Main Domain Owners Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make every tracked Python file report Radon MI strictly greater than zero by replacing the monolithic `Main` implementation with cohesive direct domain owners while preserving runtime behavior.

**Architecture:** `main.Main` remains the canonical `QMainWindow`, retains every Qt signal, constant, public signature, `__init__`, `showEvent`, and `closeEvent`, and directly inherits private behavior owners. Each owner contains a coherent set of the existing method bodies and shares the already-canonical Main instance state through normal MRO lookup. There are no forwarding methods, aliases, re-exports, `__getattr__`, generic command buses, or reverse imports.

**Tech Stack:** Python 3.11, PySide6, pytest, Ruff, Radon, Lizard, AST characterization.

## Global Constraints

- Use `C:\Users\Public\code\probe_station_gui\.venv\Scripts\python.exe`.
- Work only in the existing linked worktree and branch; do not create a nested worktree.
- No hardware, network, visible GUI, push, merge, or unified order-sensitive full run.
- Keep `Main` canonical in `main.py`; preserve all Signal descriptors, constants, public signatures, startup laziness, GUI-thread-only widget updates, and shutdown ordering.
- Preserve route Pause/Pause Ack/Interrupt semantics and safe-Z restoration exactly.
- Do not add forwarding wrappers, aliases, re-exports, dynamic dispatch, compatibility facades, localhost self-calls, or metric-padding comments/docstrings.
- Move decorator-aware method bodies. Only owner-class qualification may change where a body currently spells `Main._method`.
- Every new or changed Python file must have Radon MI > 0; new owner files must also remain positive after comments and docstrings are removed.
- Existing behavior tests are migrated only when their canonical descriptor owner changes; do not weaken safety assertions.

## Baseline and quantitative acceptance

- [ ] Record clean HEAD and exact status.
- [ ] Run the all-tracked scan over `git ls-files '*.py'`. Expected baseline: 677 files, exactly one MI-zero file, `main.py`.
- [ ] Record `main.py`: LOC 10753, LLOC 5719, SLOC 10162, Radon complexity 1805, Halstead volume 39963.04, non-normalized MI -436.32.
- [ ] Pin all existing tracked test blobs before the first production move.
- [ ] Add a repository gate that fails while any tracked Python file has `mi_visit(source, multi=True) <= 0`.

## Owner map

Each owner is a private class in `probe_station_gui/application/`. Ranges refer to baseline `fe111ca`; extraction uses AST method names rather than mutable line numbers.

| Owner file / class | Baseline methods | Projected MI, comments stripped |
|---|---|---:|
| `bootstrap_api.py` / `_MainBootstrapApiMixin` | 1342-1818 | 24.88 |
| `api_stage_contact.py` / `_MainApiStageContactMixin` | 1819-2469 | 11.53 |
| `api_meter_visa.py` / `_MainApiMeterVisaMixin` | 2470-2933 | 16.92 |
| `api_route_scan.py` / `_MainApiRouteScanMixin` | 2934-3685 | 9.96 |
| `camera_pipeline.py` / `_MainCameraPipelineMixin` | 3687-4120 | 11.18 |
| `status_coordinate_ui.py` / `_MainStatusCoordinateUiMixin` | 4121-4417 | 21.50 |
| `settings_apply.py` / `_MainSettingsApplyMixin` | 4422-4894 | 11.63 |
| `objective_tools.py` / `_MainObjectiveToolsMixin` | 4895-5097 | 29.28 |
| `optical_calibration.py` / `_MainOpticalCalibrationMixin` | 5098-5701 | 8.96 |
| `alignment.py` / `_MainAlignmentMixin` | 5703-6180 | 10.07 |
| `manual_jog.py` / `_MainManualJogMixin` | 6181-6539 | 17.42 |
| `motion_prediction.py` / `_MainMotionPredictionMixin` | 6540-6788 | 23.93 |
| `design_load.py` / `_MainDesignLoadMixin` | 6790-7052 | 26.28 |
| `design_markup.py` / `_MainDesignMarkupMixin` | 7053-7450 | 12.58 |
| `design_edit_dialog.py` / `_MainDesignEditDialogMixin` | 7463-7808 | 13.52 |
| `route_launch_setup.py` / `_MainRouteLaunchSetupMixin` | 7810-8200 | 25.85 |
| `route_capture_run.py` / `_MainRouteCaptureRunMixin` | 8201-8654 | 19.81 |
| `route_control.py` / `_MainRouteControlMixin` | 8655-8877 | 25.97 |
| `route_results.py` / `_MainRouteResultsMixin` | 8878-9257 | 20.27 |
| `registration_focus.py` / `_MainRegistrationFocusMixin` | 9261-9810 | 8.68 |
| `stage_design_position.py` / `_MainStageDesignPositionMixin` | 9811-10292 | 18.41 |
| `scan_sample_meter.py` / `_MainScanSampleMeterMixin` | 10297-10679 | 20.82 |

`showEvent` and `closeEvent` remain physically in `Main`; `__init__` remains unchanged except direct base imports. Top-level entrypoint/icon/startup helpers and canonical Signals/constants remain in `main.py`. Domain-only DTOs move with their owner when first required.

## Task 1: Architecture gate and deterministic extraction tool

**Files:**
- Create: `tests/app/test_main_domain_ownership.py`
- Create ignored: `.scratch/extract-main-domain-owners.py`

**Interfaces:**
- Consumes the owner map above.
- Produces an exact direct-base order and descriptor identity contract.

- [x] Add a vertical architecture contract: each delivered owner group extends the exact direct-base tuple and descriptor-identity map; the final group requires all 22 modules/classes, every mapped method absent from `Main.__dict__`, and exact owner descriptors.
- [x] Assert baseline Main Signals/constants and all public signatures are unchanged for the delivered owner group.
- [x] Assert no delivered owner imports `main`, another owner, FastAPI/server transport, or package re-export; assert no `__getattr__`, alias assignment, or one-line delegate.
- [x] Run the first architecture RED before production changes: 7 expected failures for the missing package, four owner modules, and base order.
- [x] Build a deterministic AST/range extraction tool in `.scratch/`: preserve decorators and source text, generate explicit imports from the original import table, define a module logger locally, remove moved definitions from `Main`, and insert direct owner imports/bases.
- [x] Pin the tool to the baseline `main.py` SHA-256 so it refuses unexpected input.

## Task 2: External/API owners

**Files:**
- Create: the first four owner files in the map.
- Modify: `main.py`.
- Modify canonical API/App tests only to import the direct descriptor owner when they inspect `__globals__`.

- [x] Run the focused baseline covering camera API, route control, meter/contact actions, route session, and API request bridge: 141 passed plus 5 subtests.
- [x] Extract one owner at a time in table order; every vertical architecture/focused gate passed, ending at 162 passed plus 5 subtests.
- [x] Replace only canonical descriptor/mock targets whose globals moved to an owner; retained method bodies, decorators, signatures, `Main.__init__`, Signals, and constants remain AST-exact.
- [x] Verify all four owner files have positive normal and comment/docstring-stripped MI; `main.py` LOC fell 10753 to 8325 and aggregate Radon CC fell 1823 to 1472. The repository still has exactly one interim MI-zero file: `main.py`.
- [x] Commit `refactor: separate main external control domains` after independent task review C0/I0/M0 (READY on the frozen Task 2 scope).

## Task 3: Camera/settings/objective owners

**Files:**
- Create: `camera_pipeline.py`, `status_coordinate_ui.py`, `settings_apply.py`, `objective_tools.py`, `optical_calibration.py`.
- Modify: `main.py` and only canonical camera/settings/objective tests.

- [x] Run focused camera frame/distortion, optical calibration, settings transaction, objective alignment, and stage-coordinate baseline: 175 passed.
- [x] Extract each mapped owner vertically, preserving queued signal wiring and GUI-thread-only widgets; the final focused App gate passed 201 tests.
- [x] Keep `showEvent` in `Main`; status owner contains only its supporting private methods.
- [x] Verify no camera/GenICam read moved onto the GUI thread and no eager optional panel/network construction was introduced; all 106 moved method ASTs are exact.
- [x] Verify every changed/new non-`Main` Python file has MI > 0 without comment/docstring bonus; `main.py` strictly reduced to 6223 LOC / CC 1028 and remains the sole interim MI-zero file.
- [x] Commit `refactor: separate main imaging and settings domains` after independent task review C0/I0/M0 (READY on the frozen Task 3 scope).

## Task 4: Alignment/motion/design owners

**Files:**
- Create: `alignment.py`, `manual_jog.py`, `motion_prediction.py`, `design_load.py`, `design_markup.py`, `design_edit_dialog.py`.
- Modify: `main.py` and canonical alignment/motion/design tests.

- [x] Run focused alignment, planned move, coordinate, design load/markup/navigation/registration tests: corrected baseline 173 passed; final App/ownership selection 191 passed; proportional Coordinates/Stage/UI selection 409 passed.
- [x] Extract owners one at a time. All six vertical gates passed while preserving exact locks, timers, selection generations, rollback ordering, and coordinate-authority observations.
- [x] Migrate private DTO/mock/global tests to canonical owners only when needed; behavioral assertions remain unchanged.
- [x] Verify descriptor-aware AST equality for all 126 moved methods plus three support DTOs, 160 residual Main methods, and no duplicate definitions.
- [x] Verify every changed/new non-`Main` Python file has positive normal and comment/docstring-stripped MI; `main.py` strictly reduced to 4065 LOC and remains the sole interim MI-zero file.
- [x] Commit `refactor: separate main motion and design domains` after independent task review C0/I0/M0 (READY on the frozen Task 4 scope).

## Task 5: Route and remaining owners

**Files:**
- Create: the final seven owner files from `route_launch_setup.py` through `scan_sample_meter.py`.
- Modify: `main.py` and canonical route/registration/stage/scan tests.

- [ ] Run the full route safety baseline: Pause request/Ack, pending-Pause Interrupt path, checkpoint propagation, autofocus safe-Z restore, photo/contact/external-measurement stop, shutdown and optical session cleanup.
- [ ] Extract route owners vertically in launch/capture/control/result order. Run the safety selection after every owner, not only at the end.
- [ ] Extract registration, stage/design position, and scan/sample/meter owners; preserve BlockingQueuedConnection and QueuedConnection edges.
- [ ] Keep `closeEvent` in `Main` and preserve the existing ordered shutdown adapter.
- [ ] Verify exact mapped method count, all descriptor identities, no duplicates/facades, and MI > 0 without comment/docstring bonus.
- [ ] Commit `refactor: separate main route and runtime domains` after independent task review C0/I0/M0.

## Task 6: Global acceptance and final review

**Files:**
- Modify: this plan with factual evidence.
- Create ignored: `.scratch/main-domain-owners-report.md`.

- [ ] Run the all-tracked MI gate. Required exact result: zero files with MI <= 0; report `main.py` and the minimum repository MI.
- [ ] Run Radon raw/CC/MI and Lizard duplicate/warning gates for `main.py` plus all owners. Reject metric padding and new duplicate policy.
- [ ] Run configured whole Ruff, scoped format, whole compileall, diff-check, import-order/DAG/root identity, descriptor/signature/Signal/constants, AST/deletion, and protected-byte gates.
- [ ] Collect the exact suite count, then execute all top-level test directories in fresh QLocale.c()/offscreen/no-cache processes. Isolate only a baseline-proven native Qt order crash node and account for every collected node exactly once.
- [ ] Do not run hardware, network, a visible GUI, push, merge, or a unified order-sensitive full process.
- [ ] Freeze exact HEAD/scope/index/task-temp/hash evidence and obtain final independent review C0/I0/M0.
- [ ] Stage only reviewed paths, run cached scope/diff checks, and commit `refactor: separate main application domains` if a final documentation-only commit is needed.
- [ ] Verify clean status, empty index, zero task temp, and rerun the all-tracked MI gate after the final commit.

## Protected behavior surfaces

- Route safety: `tests/app/test_main_route_control.py`, `test_main_route_interrupt_safety.py`, `test_main_route_measurement_session.py`, `tests/route/test_measurement_interrupt_checkpoint.py`, `test_point_execution.py`, `test_contact_lifecycle_interface.py`.
- Shutdown: `tests/ui/test_main_window_shutdown.py`.
- Camera/optical/scan: all current App camera, distortion, flat-field, lens, optical calibration, microscope-scan tests.
- Stage/motion: stage coordinate controls, planned move, feedrate, homing, joystick stop, serial-terminal coordination.
- Design: document/session restore, markup, navigation, registration/focus, coordinate flow.
- Architecture: Telegram runtime, SettingsDialogTransaction, API stage runtime, canonical root export/identity.

## Execution evidence

- [x] Clean baseline `fe111ca`; 677 tracked Python files; exactly one MI-zero file (`main.py`).
- [x] Baseline `main.py`: 10753/5719/10162 LOC/LLOC/SLOC, CC 1805, Halstead 39963.04, non-normalized MI -436.32.
- [x] Three independent read-only designs rejected a single god-runtime and callback mega-port. The selected direct-owner plan preserves the established StageController/Joystick direct-owner pattern and gives projected comments-stripped MI 8.68-29.28 for owners and approximately 8.8-14.7 for residual Main.
