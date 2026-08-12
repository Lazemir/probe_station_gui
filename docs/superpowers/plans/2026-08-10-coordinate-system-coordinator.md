# Coordinate System Coordinator Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development to implement this plan task-by-task. Every behavior change follows superpowers:test-driven-development. Use superpowers:verification-before-completion before every commit.

**Goal:** Replace Coordinate System policy distributed through `Main` and Qt helpers with one deep, Qt-free `CoordinateSystemCoordinator`, while preserving behavior and moving the selected MI-0 cluster above zero.

**Architecture:** The coordinator composes the existing frame and registration lifecycle modules, owns their state plus the registry/session/persistence journal, and returns immutable `CoordinateTransition(snapshot, intents, notices)` values. `Main` executes filesystem/hardware intents and renders snapshots. Each pass deletes the replaced `Main` policy immediately; no compatibility wrapper is retained.

**Tech Stack:** Python 3.11+, frozen dataclasses, existing coordinate/design domain modules, PySide6 adapters, pytest, Ruff, Radon/Wily.

## Global Constraints

- Work only in `C:\Users\Public\code\probe_station_gui\.worktrees\mi-zero-architecture` on `codex/mi-zero-architecture`.
- Use `C:\Users\Public\code\probe_station_gui\.venv\Scripts\python.exe` for Python commands.
- Do not access camera, FluidNC, serial, or instrument hardware.
- Keep GUI/package behavior and persisted schemas compatible.
- Preserve route Pause/Resume/Interrupt and stage motion safety semantics.
- No Qt, filesystem, serial, camera, or `SettingsManager` imports in coordinator files.
- Do not expose `CoordinateFrameRegistry`, `DesignSession`, lifecycle objects, or lifecycle effects through the final public interface.
- New files must have MI > 0. Record source/test LOC, CC, and MI after every pass.
- `main.py` must improve monotonically in LOC and aggregate complexity in this
  cluster. Its MI > 0 gate is global to the complete multi-cluster program: the
  frozen Radon inputs `(volume=52611.74, complexity=2205, logical LOC=6708)`
  remain MI 0 even at 10% proportional size and cross above zero only near 6%.
  Do not import unrelated domains merely to force this candidate's score.
- Use writable system-temp basetemp paths; never assume a worktree-local `.tmp` exists.

---

### Task 0: Freeze the architecture contract and baseline

**Files:**
- Modify: `CONTEXT.md`
- Create: `docs/superpowers/specs/2026-08-10-coordinate-system-coordinator-design.md`
- Create: `docs/superpowers/plans/2026-08-10-coordinate-system-coordinator.md`

- [ ] Add the approved Coordinate System / Coordinate Frame terminology to `CONTEXT.md`.
- [ ] Record Wily baseline for `main.py`, `tests/app/test_main_design_navigation.py`, and repository MI-0 count.
- [ ] Record full no-hardware pytest baseline.
- [ ] Review the spec against the three approved passes and commit documentation.

Use the direct Radon baseline plus a disposable UTF-8 Wily clone/cache. Never
run `wily build` in the active worktree:

```powershell
$env:PYTHONIOENCODING='utf-8'
$env:PYTHONUTF8='1'
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8
C:\Users\Public\code\probe_station_gui\.venv\Scripts\python.exe -X utf8 -m radon mi -s main.py tests\app\test_main_design_navigation.py
$metricRoot = Join-Path $env:TEMP ("probe-station-coordinator-wily-" + [guid]::NewGuid().ToString("N"))
$metricCache = $metricRoot + "-cache"
git clone --no-local --branch codex/mi-zero-architecture C:\Users\Public\code\probe_station_gui $metricRoot
Push-Location $metricRoot
C:\Users\Public\code\probe_station_gui\.venv\Scripts\python.exe -X utf8 -m wily -c $metricCache build main.py probe_station_gui\coordinates tests\app\test_main_design_navigation.py
C:\Users\Public\code\probe_station_gui\.venv\Scripts\python.exe -X utf8 -m wily -c $metricCache diff main.py probe_station_gui\coordinates tests\app\test_main_design_navigation.py --detail -r 8fc83259e07f48bf0f222e80bb3e1d6c3241fd0e --metrics cyclomatic.complexity,raw.loc,maintainability.mi --no-wrap
Pop-Location
```

```powershell
git add CONTEXT.md docs/superpowers/specs/2026-08-10-coordinate-system-coordinator-design.md docs/superpowers/plans/2026-08-10-coordinate-system-coordinator.md
git commit -m "docs: define coordinate coordinator refactor"
```

---

### Task 1: Move frame persistence orchestration behind the coordinator

**Files:**
- Create: `probe_station_gui/coordinates/coordinator.py`
- Create: `probe_station_gui/coordinates/coordinator_model.py`
- Create: `probe_station_gui/coordinates/coordinator_persistence.py`
- Create: `probe_station_gui/coordinates/store_model.py`
- Modify: `probe_station_gui/coordinates/__init__.py`
- Modify: `probe_station_gui/coordinates/persistence.py`
- Modify: `probe_station_gui/design/navigation_adapter.py`
- Modify: `probe_station_gui/design/registration_lifecycle.py`
- Modify: `probe_station_gui/views/main_window_connection_flow.py:26-166`
- Modify: `main.py:950-1075,7716-7959`
- Create: `tests/coordinates/test_coordinator_persistence.py`
- Modify: `tests/coordinates/test_public_api.py`
- Modify: `tests/design/test_navigation_adapter.py`
- Modify: `tests/design/test_registration_lifecycle.py`
- Modify: `tests/ui/test_main_window_connection_flow.py:502-670,887-930`
- Modify: `tests/app/test_main_design_navigation.py:1620-2260`
- Modify: `tests/app/test_main_software_coordinate_pivot.py`

**Owned after this task:** frame load generation, the single registry/session
instances for every frame publication, durable document, publication journal,
acknowledgements, rollback grouping/application, and persistence intents.
Store completion DTOs live in the Qt-free `store_model.py` seam and
`persistence.py` re-exports them for compatibility. Persistence rollback also
owns the minimal exact-operation release hook for the registration lifecycle's
first-contact write latch: a terminal rollback must permit that same A commit
to be retried, while stale or superseded failures must not release it.
Non-publication registration/selection behavior remains transitional until its
owning pass, but no publisher mutates registry/session outside the coordinator.

- [ ] **Step 1: Add interface RED tests**

Write tests against only `CoordinateSystemCoordinator` proving:

- `start(profile)` returns one `LoadCoordinateFramesIntent` and an unavailable
  snapshot before adapter submission;
- a stale load completion returns no intents/notices and cannot replace records;
- current load completion installs the document and runtime records;
- publication is journaled before `SaveCoordinateFramesIntent` is returned;
- older failed saves defer to newer saves;
- terminal failure rolls all affected frames back in deterministic frame-ID
  order to the earliest durable predecessor and restores exact registration
  effects once;
- newer success acknowledges every coalesced registration publication and only
  then returns success notices;
- repeated/late completions are inert.
- Main and coordinator share no duplicate registry/session state: a proposed
  publication installs and rolls back the one instances without a Main helper.

Use frozen public types:

```python
@dataclass(frozen=True)
class CoordinateTransition:
    snapshot: CoordinateSystemSnapshot
    intents: tuple[CoordinateAdapterIntent, ...] = ()
    notices: tuple[CoordinateNotice, ...] = ()

@dataclass(frozen=True)
class LoadCoordinateFramesIntent:
    intent_id: int
    machine_profile_id: str

@dataclass(frozen=True)
class SaveCoordinateFramesIntent:
    intent_id: int
    document: CoordinateFrameDocument

@dataclass(frozen=True)
class FrameRecordsPublication:
    records: tuple[CoordinateFrameRecord, ...]
    previous_record: CoordinateFrameRecord | None = None
    committed_record: CoordinateFrameRecord | None = None
    previous_session: DesignSessionCheckpoint | None = None
    proposed_session_link: DesignSessionFrameLink | None = None
    success_notice: CoordinateNotice | None = None
```

- [ ] **Step 2: Run the strict RED gate**

```powershell
C:\Users\Public\code\probe_station_gui\.venv\Scripts\python.exe -m pytest tests\coordinates\test_coordinator_persistence.py -q --basetemp $env:TEMP\coordinate-coordinator-task1-red
```

Expected: collection fails because `coordinates.coordinator` does not exist.

- [ ] **Step 3: Implement the smallest persistence coordinator**

Create `CoordinateSystemCoordinator` with explicit `start()`, `complete()`,
`snapshot()`, and transitional `publish_frame_records()` operations. The latter
is the only save/publication input in Task 1: it allocates the ID, journals the
exact publication/session checkpoint, installs the proposed records and session
link into the one adopted registry/session instances, updates the durable
document, and returns the save intent. Main creates those instances once and
passes them to the coordinator constructor; it does not maintain a second copy.
Compose `CoordinateFrameLifecycle`; absorb its effects inside the private
persistence reducer. Do not return `FrameLifecycleEffects`, use the snapshot as
an effects transport, or expose the lifecycle.

Translate store results at the adapter boundary:

```python
transition = coordinator.complete(
    CoordinateAdapterCompletion(intent_id=intent_id, result=frames_loaded)
)
```

- [ ] **Step 4: Replace the real load/save callbacks**

Make every current `connection_flow.publish_coordinate_frames()` caller invoke
`publish_frame_records()` directly and execute the returned transition. Delete
`connection_flow.publish_coordinate_frames`, `_next_coordinate_frame_request_id`,
its load/save ordering policy, and direct lifecycle calls. In `Main`, delete
`_coordinate_frame_request_id`, `_coordinate_frame_document`, direct
`finish_publication()` calls, and rollback-chain policy. Coordinator completion
applies registry/session rollback through its adopted object references. Every
old publisher is rewritten to construct an immutable proposed record/session
link first; it must not call registry `add/reset/replace` or mutate the session
before `publish_frame_records()`. `Main` only submits intents and renders
snapshots/notices for this slice. The transitional
publication input may remain until Tasks 2-3 replace its callers, but there is
no Main pass-through helper.

- [ ] **Step 5: Move persistence tests off private Main state**

Move pure coalescing/rollback cases out of
`tests/app/test_main_design_navigation.py` into
`tests/coordinates/test_coordinator_persistence.py`. Retain only thin adapter
tests in `test_main_window_connection_flow.py`: one intent submission, one
completion delegation, one transition application.

- [ ] **Step 6: Verify, measure, review, commit**

```powershell
C:\Users\Public\code\probe_station_gui\.venv\Scripts\python.exe -m pytest tests\coordinates\test_coordinator_persistence.py tests\coordinates\test_lifecycle.py tests\coordinates\test_persistence.py tests\ui\test_main_window_connection_flow.py tests\app\test_main_design_navigation.py -q --basetemp $env:TEMP\coordinate-coordinator-task1-green
C:\Users\Public\code\probe_station_gui\.venv\Scripts\python.exe -m ruff check probe_station_gui\coordinates\coordinator.py probe_station_gui\coordinates\coordinator_model.py probe_station_gui\coordinates\coordinator_persistence.py probe_station_gui\views\main_window_connection_flow.py main.py tests\coordinates\test_coordinator_persistence.py tests\ui\test_main_window_connection_flow.py tests\app\test_main_design_navigation.py
C:\Users\Public\code\probe_station_gui\.venv\Scripts\python.exe -m compileall -q probe_station_gui main.py
```

Record Wily/Radon delta and require an independent review with no Critical or
Important finding.

```powershell
git add docs/superpowers/plans/2026-08-10-coordinate-system-coordinator.md probe_station_gui/coordinates/coordinator.py probe_station_gui/coordinates/coordinator_model.py probe_station_gui/coordinates/coordinator_persistence.py probe_station_gui/coordinates/store_model.py probe_station_gui/coordinates/__init__.py probe_station_gui/coordinates/persistence.py probe_station_gui/design/navigation_adapter.py probe_station_gui/design/registration_lifecycle.py probe_station_gui/views/main_window_connection_flow.py main.py tests/coordinates/test_coordinator_persistence.py tests/coordinates/test_public_api.py tests/design/test_navigation_adapter.py tests/design/test_registration_lifecycle.py tests/ui/test_main_window_connection_flow.py tests/app/test_main_design_navigation.py tests/app/test_main_software_coordinate_pivot.py
git commit -m "refactor: centralize coordinate frame persistence"
```

---

### Task 2: Move registration, focus, and contact workflows

**Files:**
- Modify: `docs/superpowers/plans/2026-08-10-coordinate-system-coordinator.md`
- Modify: `main.py`
- Modify: `probe_station_gui/coordinates/coordinator.py`
- Modify: `probe_station_gui/coordinates/coordinator_model.py`
- Modify: `probe_station_gui/coordinates/coordinator_persistence.py`
- Create: `probe_station_gui/coordinates/coordinator_activation.py`
- Create: `probe_station_gui/coordinates/coordinator_contact.py`
- Create: `probe_station_gui/coordinates/coordinator_focus.py`
- Create: `probe_station_gui/coordinates/coordinator_registration.py`
- Create: `probe_station_gui/coordinates/coordinator_registration_capture.py`
- Modify: `probe_station_gui/design/registration_lifecycle.py`
- Modify: `probe_station_gui/design/session.py`
- Create: `probe_station_gui/design/session_state.py`
- Modify: `probe_station_gui/route/contact_lifecycle.py`
- Modify: `probe_station_gui/route/external_session.py`
- Modify: `probe_station_gui/route/measurement.py`
- Modify: `probe_station_gui/route/point_execution_adapters.py`
- Modify: `probe_station_gui/views/main_window_connection_flow.py`
- Modify: `probe_station_gui/views/main_window_shutdown.py`
- Modify: `tests/app/main_coordinate_feedrate_support.py`
- Modify: `tests/app/test_main_design_navigation.py`
- Modify: `tests/app/test_main_route_measurement_session.py`
- Create: `tests/coordinates/coordinator_registration_support.py`
- Create: `tests/coordinates/test_coordinator_registration_activation.py`
- Create: `tests/coordinates/test_coordinator_registration_capture.py`
- Create: `tests/coordinates/test_coordinator_registration_focus_contact.py`
- Create: `tests/coordinates/test_coordinator_registration_operator_cancel.py`
- Create: `tests/coordinates/test_coordinator_registration_rollback_rendering.py`
- Modify: `tests/coordinates/test_coordinator_persistence.py`
- Modify: `tests/coordinates/test_public_api.py`
- Modify: `tests/design/test_registration_lifecycle.py`
- Modify: `tests/design/test_workflow.py`
- Modify: `tests/route/test_contact_lifecycle_interface.py`
- Modify: `tests/ui/test_main_window_connection_flow.py`
- Modify: `tests/ui/test_main_window_docks.py`
- Modify: `tests/ui/test_main_window_shutdown.py`

**Owned after this task:** active Design frame link, registration instance
selection, capture tokens/evidence/normalization, fit/commit, exact rollback,
focus candidate/move/autofocus/Z commit, and Z-gated first-contact/A commit.

- [x] **Step 1: Add workflow RED tests**

Through explicit coordinator methods, prove:

- stale capture success/failure is rejected before conversion, status, or
  provenance mutation;
- indexed mixed-B/pivot/objective captures normalize around slot 0 and commit
  once only after a complete batch;
- changed design/top-cell/frame/marks cancels and restores the exact baseline
  once;
- registration publication carries the exact rollback object through a
  deferred failure;
- focus requires current candidate, exact context, target-bound move, move
  success, autofocus success, then Z commit/publication;
- changing Z invalidates A;
- contact requires ready Z, missing A, exact frame lease, and first write wins;
- operator alignment never emits B motion and stale/re-armed image picks remain
  untouched.
- interrupt after contact intent creation but before A-read/completion commits no
  A, emits no save, and permits no later lower/contact-check/external measure;
- pause request remains Interrupt until `pause_ack`, then Resume preserves the
  existing safe contact checkpoint semantics.

- [x] **Step 2: Run the strict RED gate**

```powershell
C:\Users\Public\code\probe_station_gui\.venv\Scripts\python.exe -m pytest tests\coordinates\test_coordinator_registration.py -q --basetemp $env:TEMP\coordinate-coordinator-task2-red
```

- [x] **Step 3: Add explicit workflow operations**

Implement `activate_design()`, `close_design()`,
`capture_registration_mark()`, `offer_focus_candidate()`,
`use_focus_reference()`, `reset_focus_reference()`, `arm_first_contact()`, and
`cancel_registration()`. The coordinator builds lifecycle contexts, consumes
every `RegistrationEffects`, mutates its registry/session, journals persistence,
and returns only adapter intents/snapshot/notices.

Replace connection-flow load activation, connect/disconnect/reboot activation,
and frame-authority calls with coordinator activation/observation transitions;
do not retain `_activate_loaded_design_frame` or an authority forwarding shim.

Add typed intents for synchronized Machine pose, focus move, autofocus, and
physical A read. Only `complete()` accepts their results.

- [x] **Step 4: Replace Main registration policy**

Convert the Main methods in the listed ranges into signal/result translation
and intent execution. Delete `_apply_registration_effects()`,
`_discard_all_registration_evidence()`, direct lifecycle calls, registry
replace/add policy, publication enrichment, context-currentness policy, and
session baseline reconstruction. Do not leave private forwarding methods with
the deleted names.

- [x] **Step 5: Move tests to the workflow boundary**

Move pure registration/focus/contact cases from the 5,008-line Main test into
`test_coordinator_registration.py`. Keep real Main adapter tests for one image
capture, one armed-crosshair capture, focus signal wiring, first-contact
callback, stale callback inertness, and route interrupt propagation.

- [x] **Step 6: Verify, measure, review, commit**

```powershell
C:\Users\Public\code\probe_station_gui\.venv\Scripts\python.exe -m pytest tests\coordinates\test_coordinator_registration.py tests\design\test_registration_lifecycle.py tests\app\test_main_design_navigation.py tests\design\test_workflow.py tests\design\test_frame_references.py tests\app\test_main_route_measurement_session.py tests\route -q --basetemp $env:TEMP\coordinate-coordinator-task2-green
```

Run Ruff, compileall, diff-check, per-file MI/CC, and independent review.

**Outcome (2026-08-11):** Registration, activation, focus, and contact now
run through explicit coordinator workflows and typed adapter intents. The pure
workflow tests were split by concern so every new test module retains positive
maintainability. Final verification completed with 727 focused tests plus 5
subtests, and 2,905 no-hardware tests plus 18 subtests; the two pre-existing
camera-exposure failures were explicitly deselected. Ruff, compileall, both
package import orders, diff-check, and Radon passed. Every new production and
test module has MI greater than zero; production CC averaged A (4.61). An
independent final review reported no Critical or Important findings.

```powershell
git add probe_station_gui/coordinates/coordinator.py probe_station_gui/coordinates/coordinator_model.py probe_station_gui/coordinates/coordinator_registration.py probe_station_gui/design/registration_lifecycle.py probe_station_gui/design/navigation_adapter.py probe_station_gui/views/main_window_connection_flow.py main.py tests/coordinates/test_coordinator_registration.py tests/design/test_registration_lifecycle.py tests/app/test_main_design_navigation.py tests/design/test_workflow.py tests/app/test_main_route_measurement_session.py tests/route/test_contact_lifecycle_interface.py tests/route/test_measurement_api_route_control.py tests/route/test_point_execution.py
git commit -m "refactor: centralize design coordinate registration"
```

---

### Task 3: Move selection, authority, usability, and presentation

**Files:**
- Modify: `probe_station_gui/coordinates/coordinator.py`
- Modify: `probe_station_gui/coordinates/coordinator_model.py`
- Create: `probe_station_gui/coordinates/coordinator_selection.py`
- Modify: `probe_station_gui/coordinates/presentation.py`
- Modify: `probe_station_gui/coordinates/software_frames.py`
- Modify: `probe_station_gui/views/main_window_stage_position_panel.py:36-288`
- Modify: `probe_station_gui/views/main_window_connection_flow.py`
- Modify: `probe_station_gui/stage/position_update.py:344-361`
- Modify: `probe_station_gui/views/main_window_homing.py:179`
- Modify: `probe_station_gui/settings/software_coordinate_selection_store.py`
- Modify: `main.py:4387-4815,4930-5080,6539-6622,7842-8137,10773-10824,11256-11290,11370-11564`
- Create: `tests/coordinates/test_coordinator_selection.py`
- Modify: `tests/coordinates/test_presentation.py`
- Modify: `tests/ui/test_stage_position_panel.py`
- Modify: `tests/app/test_main_stage_coordinate_controls.py`
- Modify: `tests/app/test_main_software_coordinate_pivot.py`
- Modify: `tests/app/test_main_design_navigation.py`
- Modify: `tests/stage/test_position_update.py`
- Modify: `tests/ui/test_main_window_homing.py`
- Modify: `tests/settings/test_software_coordinate_selection_store.py`

**Owned after this task:** registry and coordinate-relevant DesignSession state,
Coordinate System selection/restore intent, custom materialization, runtime
authority blocks, Design usability lease, display plan, and coordinate motion
projection.

**Actual Task 3 implementation:**

- Created coordinator modules:
  `probe_station_gui/coordinates/application_runtime.py`,
  `coordinator_design_lease.py`, `coordinator_motion.py`,
  `coordinator_selection.py`, and `source_identity.py`.
- Created GUI adapters: `probe_station_gui/views/main_window_coordinate_entry.py`,
  `main_window_coordinate_motion.py`, and `main_window_coordinate_step.py`.
- Modified coordinator/domain files: `probe_station_gui/coordinates/__init__.py`,
  `coordinator.py`, `coordinator_activation.py`, `coordinator_contact.py`,
  `coordinator_focus.py`, `coordinator_model.py`, `coordinator_persistence.py`,
  `coordinator_registration.py`, `coordinator_registration_capture.py`,
  `design_calibration.py`, `lifecycle.py`, `presentation.py`, `registry.py`, and
  `software_frames.py`; plus `probe_station_gui/design/frame_registration.py`,
  `navigation_adapter.py`, `session.py`, and `session_state.py`.
- Modified runtime adapters: `main.py`,
  `probe_station_gui/stage/axis_coordinates.py`, `controller.py`,
  `coordinate_targets.py`, `machine_coordinates.py`, `move_lifecycle.py`, and
  `position_update.py`; plus `probe_station_gui/views/joystick_window.py`,
  `main_window_connection_flow.py`, `main_window_docks.py`,
  `main_window_homing.py`, `main_window_stage_position_panel.py`, and
  `stage_position_panel.py`.
- Created tests: `tests/coordinates/test_application_coordinate_runtime.py`,
  `test_coordinator_selection.py`,
  `tests/app/test_main_coordinate_system_adapter.py`,
  `test_main_design_markup_navigation.py`, and
  `test_main_design_registration_adapters.py`.
- Migrated/modified application tests:
  `tests/app/main_coordinate_feedrate_support.py`, `test_main_camera_api.py`,
  `test_main_coordinate_feedrate.py`, `test_main_design_navigation.py`,
  `test_main_meter_contact_actions.py`, `test_main_microscope_scan.py`,
  `test_main_objective_alignment.py`, `test_main_planned_move_prediction.py`,
  `test_main_route_measurement_session.py`,
  `test_main_software_coordinate_pivot.py`, and
  `test_main_stage_coordinate_controls.py`.
- Migrated/modified domain and adapter tests:
  `tests/coordinates/coordinator_registration_support.py`,
  `test_coordinator_persistence.py`, `test_coordinator_registration_activation.py`,
  `test_coordinator_registration_capture.py`,
  `test_coordinator_registration_operator_cancel.py`,
  `test_coordinator_registration_rollback_rendering.py`, `test_lifecycle.py`,
  `test_presentation.py`, and `test_public_api.py`;
  `tests/design/test_navigation_adapter.py`;
  `tests/stage/test_controller_universal_axis_calibration.py`,
  `test_machine_coordinate_snapshot.py`, `test_move_lifecycle.py`, and
  `test_position_update.py`; and `tests/ui/test_joystick_feedrate.py`,
  `test_main_window_connection_flow.py`, `test_main_window_docks.py`,
  `test_main_window_homing.py`, and `test_stage_position_panel.py`.

- [x] **Step 1: Add selection/projection RED tests**

Prove through the coordinator:

- Machine works before any pose;
- explicit temporary B/homing/pivot loss preserves the selected Coordinate
  System and shows unavailable/yellow axes without persisting Machine;
- permanent semantic corruption or deleted frame falls back and persists
  Machine;
- pending/blocked runtime provenance remains recoverable;
- Custom frame synchronization preserves unknown future payloads and does not
  resurrect deleted frames;
- Design usability rejects missing X/Y/B registration before navigation;
- a lease becomes stale on frame version, source/load identity, authority,
  objective, pivot, or document change;
- WASD/display/motion projection use the same selected snapshot;
- invalid pivot falls back once, renders once, and persists the final decision.

- [x] **Step 2: Run the strict RED gate**

```powershell
C:\Users\Public\code\probe_station_gui\.venv\Scripts\python.exe -m pytest tests\coordinates\test_coordinator_selection.py -q --basetemp $env:TEMP\coordinate-coordinator-task3-red
```

- [x] **Step 3: Add explicit selection/observation operations**

Implement `select_system()`, `observe_authority()`,
`synchronize_custom_systems()`, `snapshot()`, and current Design lease/project
queries. Absorb `FrameSelectionDecision`, `DesignFrameUsabilitySnapshot`, and
presentation-plan policy; return finished snapshot and optional
`PersistCoordinateSelectionIntent`.

Selection persistence remains intentionally fire-and-forget. Main executes the
intent through the existing selection store; only failure is reported back as
an adapter notice. Do not add a fake durable-success acknowledgement.

- [x] **Step 4: Finish ownership migration**

Move registry and coordinate-relevant session ownership fully behind the
coordinator. Stage-position and connection helpers render snapshots and execute
intents only. Delete Main fields `_coordinate_frame_registry`,
`_coordinate_frame_lifecycle`, `_design_registration_lifecycle`,
`_coordinate_frames_loaded`, `_coordinate_frame_authority_blocked_axes`, and
all private policy helpers replaced in Tasks 1-3. Do not add aliases or getters
returning mutable owners.

Replace post-home and live B/status call sites in `main_window_homing.py` and
`stage/position_update.py` with `CoordinateAuthorityObservation` transitions.
Their tests must prove authority loss and recovery refresh the retained selected
Coordinate System without a compatibility helper.

- [x] **Step 5: Split the MI-0 test monolith by interface**

Move remaining coordinate-domain characterizations into:

- `tests/coordinates/test_coordinator_persistence.py`;
- `tests/coordinates/test_coordinator_registration.py`;
- `tests/coordinates/test_coordinator_selection.py`;
- a small `tests/app/test_main_coordinate_system_adapter.py` for Qt wiring.

Extract shared immutable builders to
`tests/coordinates/coordinator_support.py`. Leave only unrelated Design
navigation tests in `test_main_design_navigation.py`; do not preserve private
Main state fixtures.

- [x] **Step 6: Verify, measure, review, commit**

```powershell
C:\Users\Public\code\probe_station_gui\.venv\Scripts\python.exe -m pytest tests\coordinates tests\ui\test_stage_position_panel.py tests\ui\test_main_window_connection_flow.py tests\app\test_main_coordinate_system_adapter.py tests\app\test_main_stage_coordinate_controls.py tests\app\test_main_software_coordinate_pivot.py tests\app\test_main_design_navigation.py tests\app\test_main_route_measurement_session.py tests\route -q --basetemp $env:TEMP\coordinate-coordinator-task3-green
```

Run Ruff, compileall, diff-check, per-file Wily/Radon, and independent review.

Final Task 3 evidence:

- focused motion/API/Step cluster: `97 passed`;
- one-process full no-hardware suite with `QLocale.c()`: `2921 passed`,
  `14 subtests passed`;
- public facade/import-order/runtime purity cluster: `6 passed`;
- Ruff over every changed or created Python file, compileall, and diff-check:
  clean;
- forbidden legacy-owner/direct coordinate-session/filesystem-hot-path searches:
  no matches;
- `main.py`: LOC `10933`, LLOC `5950`, SLOC `10313`, average CC `3.5655`
  versus the Task 2 baseline LOC `11117`, average CC `3.69`;
- every created Python file has MI above zero (minimum `2.66`);
- independent frozen-snapshot review: `READY`, with no remaining Critical or
  Important findings.

```powershell
git add probe_station_gui/coordinates/coordinator.py probe_station_gui/coordinates/coordinator_model.py probe_station_gui/coordinates/coordinator_selection.py probe_station_gui/coordinates/presentation.py probe_station_gui/coordinates/software_frames.py probe_station_gui/views/main_window_stage_position_panel.py probe_station_gui/views/main_window_connection_flow.py probe_station_gui/views/main_window_homing.py probe_station_gui/stage/position_update.py probe_station_gui/settings/software_coordinate_selection_store.py main.py tests/coordinates tests/ui/test_stage_position_panel.py tests/ui/test_main_window_connection_flow.py tests/ui/test_main_window_homing.py tests/stage/test_position_update.py tests/settings/test_software_coordinate_selection_store.py tests/app/test_main_coordinate_system_adapter.py tests/app/test_main_stage_coordinate_controls.py tests/app/test_main_software_coordinate_pivot.py tests/app/test_main_design_navigation.py
git commit -m "refactor: centralize coordinate system presentation"
```

---

### Task 4: Split residual coordinate adapter responsibilities

**Files:**
- Modify: `probe_station_gui/design/navigation_adapter.py`
- Create: `probe_station_gui/design/route_editing.py`
- Create: `probe_station_gui/design/navigation_targeting.py`
- Modify: `probe_station_gui/views/main_window_connection_flow.py`
- Create: `probe_station_gui/views/main_window_coordinate_flow.py`
- Create: `probe_station_gui/views/main_window_design_workspace.py`
- Modify: `main.py`
- Modify: `probe_station_gui/stage/move_lifecycle.py`
- Modify: `probe_station_gui/stage/position_update.py`
- Modify: `probe_station_gui/views/main_window_homing.py`
- Modify: `probe_station_gui/views/main_window_stage_position_panel.py`
- Create: `tests/design/test_navigation_responsibility_modules.py`
- Create: `tests/ui/test_main_window_flow_responsibilities.py`
- Modify: navigation, coordinator, Main adapter, position-update, homing, and
  stage-position tests that imported or patched the previous owners.

**Owned after this task:** `navigation_adapter.py` owns only Design activation,
load, and persisted restore. `route_editing.py` owns route CRUD, arrays, mixed
edits, and route-point selection. `navigation_targeting.py` owns target
selection, movement plans, and widget-agnostic presentation. The connection
flow owns only serial/controller connection, reboot, feedrate, persistence, and
LCR orchestration; coordinate transition execution and Design workspace
checkpoint/restore live in separate view adapters. No compatibility re-export
facade remains.

- [x] Add strict RED architecture tests proving each extracted interface is
  implemented by its owning module and absent from the previous module.
- [x] Run the RED gate and confirm collection fails because the new modules do
  not exist.
- [x] Extract the Qt-free navigation responsibilities and migrate all callers
  and monkeypatch seams directly to the new owners.
- [x] Extract coordinate transition execution and Design workspace
  checkpoint/restore from the serial connection flow; migrate Main, homing,
  position-update, motion-lifecycle, and tests directly.
- [x] Preserve route-control and hardware behavior: no Pause/Resume/Interrupt,
  route runner, serial command, feedrate, or LCR implementation was changed.
- [x] Run final metrics, full process-local `QLocale.c()` no-hardware tests,
  Ruff, compileall, diff/ownership/import-order gates, and a fresh read-only
  review with no Critical/Important finding.

Final Task 4 evidence:

- strict RED: the two architecture test modules failed collection on the
  absent navigation and coordinate-flow modules;
- focused navigation GREEN: `34 passed`;
- focused connection-flow GREEN: `26 passed`;
- architecture GREEN: `4 passed`;
- process-local `QLocale.c()` broad gate across Design, coordinates, relevant
  Main/UI/stage adapters, and all route tests: `1056 passed`, `5 subtests passed`;
- full process-local `QLocale.c()` no-hardware suite: `2925 passed`,
  `14 subtests passed`; an earlier run exposed one unchanged camera-monitor
  scheduling race, while its isolated file and the fresh full rerun both passed;
- MI: `navigation_adapter.py` `14.67`, `route_editing.py` `25.77`,
  `navigation_targeting.py` `33.40`, `main_window_connection_flow.py` `27.66`,
  `main_window_coordinate_flow.py` `8.67`, and
  `main_window_design_workspace.py` `29.90`;
- scoped MI-0 count fell from `28` to `26`; neither original owner nor any new
  Python module is MI 0;
- `main.py`: LOC `10933`, LLOC `5949`, SLOC `10313`, aggregate CC `1904`,
  average CC `3.565543`; LOC/CC do not regress from Task 3 and LLOC decreases
  by one;
- Ruff on every changed Python file (with inherited startup/test `E402`
  ignored), whole-tree compileall, diff-check, import-order/public-API,
  direct-ownership, no-filesystem authority-path, Qt-free domain, and
  no-eager-re-export gates passed;
- a fresh independent frozen-diff review found no Critical, Important, or Minor
  findings and returned **READY**.

```powershell
git add docs/superpowers/plans/2026-08-10-coordinate-system-coordinator.md main.py probe_station_gui/design/navigation_adapter.py probe_station_gui/design/navigation_targeting.py probe_station_gui/design/route_editing.py probe_station_gui/stage/move_lifecycle.py probe_station_gui/stage/position_update.py probe_station_gui/views/main_window_connection_flow.py probe_station_gui/views/main_window_coordinate_flow.py probe_station_gui/views/main_window_design_workspace.py probe_station_gui/views/main_window_homing.py probe_station_gui/views/main_window_stage_position_panel.py tests/app/test_main_coordinate_system_adapter.py tests/app/test_main_design_markup_navigation.py tests/app/test_main_design_navigation.py tests/app/test_main_design_registration_adapters.py tests/app/test_main_planned_move_prediction.py tests/app/test_main_route_measurement_session.py tests/coordinates/test_coordinator_registration_operator_cancel.py tests/coordinates/test_coordinator_registration_rollback_rendering.py tests/design/test_navigation_adapter.py tests/design/test_navigation_responsibility_modules.py tests/design/test_selection_model.py tests/stage/test_position_update.py tests/ui/test_main_window_connection_flow.py tests/ui/test_main_window_flow_responsibilities.py tests/ui/test_main_window_homing.py tests/ui/test_stage_position_panel.py
git commit -m "refactor: split coordinate adapter responsibilities"
```

---

### Task 5: Enforce the selected-cluster MI gate and run acceptance

**Files:**
- Modify only residual Design/coordinate adapter or test files identified by
  the metric gate.
- Create: `.superpowers/sdd/2026-08-10-coordinate-system-coordinator/final-report.md`

#### Task 5a: Separate Design layout ownership

**Files:**
- Create: `probe_station_gui/design/layout_state.py`
- Create: `probe_station_gui/views/design_layout_window.py`
- Create: `tests/design/test_layout_state.py`
- Modify: `main.py`
- Modify: `probe_station_gui/views/__init__.py`
- Modify: `probe_station_gui/views/design_navigator_panel.py`
- Modify: `probe_station_gui/views/main_window_auxiliary.py`
- Modify: `tests/app/test_main_design_markup_navigation.py`
- Modify: `tests/design/test_click_navigation.py`
- Modify: `tests/ui/test_design_plot_klayout.py`
- Modify: `tests/ui/test_design_plot_selection.py`
- Modify: `tests/ui/test_main_window_menus.py`

- [x] Add strict RED tests for initial selection, preservation, pruning, hidden
  Markup, replace/add/invert, invalid IDs, mixed-edit projection, and retained
  document state. Collection first failed because `design.layout_state` did not
  exist; the planning RED then failed on the absent workflow method.
- [x] Extract `DesignLayoutWindow` to its own Qt module and migrate every direct
  import. The UI RED failed collection because `views.design_layout_window` did
  not yet exist; the old module has no compatibility re-export.
- [x] Make frozen `DesignLayoutState` the one owner of document, an isolated
  route snapshot, Markup, selectable projection, selection initialization,
  pruning, and mixed-edit planning. `Main` consumes workflow-specific plans
  rather than recomputing `project_entities`. RED/GREEN regressions prove that
  caller-supplied array IDs cannot override canonical selection and that later
  mutation of the source `MeasurementRoute` cannot desynchronize the snapshot.
- [x] Preserve close/reopen without reading `_DesignPlotPane._document`. A
  dedicated RED/GREEN regression proves ordinary hide/show does not reinstall
  an already attached document. A closed-window refresh remains worker-free,
  while reopening installs the latest retained document exactly once. Closing
  during a preview and then finishing it while hidden also clears preview
  bookkeeping without reattaching workers; the next preview starts cleanly.
- [x] Keep `design_plot_pane.py`, the KLayout/snap modules,
  `selection_model.py`, and the complete `route/` package byte-identical to
  `e914072`. The route Pause/Interrupt/Resume gates are included in the broad
  suite.
- [x] Focused Design/UI/Main Markup gate: `167 passed`; broad
  Design/UI/route-control gate: `531 passed`; full process-local `QLocale.c()`
  no-hardware gate: `2,939 passed` plus `14` subtests.
- [x] Import-order checks pass in both directions. Ruff passes on every changed
  Python file with inherited startup `E402` ignored; compileall and final review
  are recorded after the frozen diff.
- [x] Two frozen independent review rounds found four Important ownership and
  lifecycle gaps; each received a dedicated failing regression before its
  fix. A third fresh independent review returned READY with no Critical or
  Important findings and independently passed `129` affected tests.
- [x] Metrics: `main.py` LOC `10933 -> 10917`, LLOC `5949 -> 5946`, aggregate
  CC remains `1904` (average `3.565543`); `design_navigator_panel.py` LOC
  `2563 -> 2164`, aggregate CC `362 -> 312`. New MI values are
  `design_layout_window.py 31.25`, `layout_state.py 43.54`, and
  `test_layout_state.py 38.35`.

#### Task 5b: Centralize the Design tool session

**Files:**
- Create: `probe_station_gui/design/tool_context.py`
- Create: `probe_station_gui/design/tool_session.py`
- Create: `tests/design/test_tool_session.py`
- Modify: `probe_station_gui/views/design_navigator_panel.py`
- Modify: `tests/design/test_click_navigation.py`
- Modify: `tests/ui/test_design_navigator_panel.py`
- Modify: `tests/ui/test_design_plot_selection.py`
- Modify: `tests/ui/test_main_window_menus.py`
- Modify: `docs/superpowers/plans/2026-08-10-coordinate-system-coordinator.md`

- [x] Observe strict RED before production code: collection first failed with
  `ModuleNotFoundError` for `design.tool_session`. Later focused REDs rejected a
  stale-generation pick API, an accidental panel compatibility export, and a
  missing legacy Array activation effect before each corresponding fix. Review
  REDs then proved early terminal rendering, nested mutable metadata, repeated
  route capture on selection, and full-route deepcopy on every Array hover.
- [x] Make frozen, Qt-free `DesignToolSession` the one owner of active tool,
  ruler draft and durable segments, alignment draft, Array configuration and
  direction pick, detached preview context, and immutable effects/intents. Pure
  tests cover begin/update/finish/cancel, accept/discard, tool switches,
  repeated Escape, context replacement, detached route input, and stale hover
  and pick generations. `DesignToolContext` uses a separate Qt-free immutable
  support module with an indexed, deeply frozen route snapshot.
- [x] Reduce `DesignNavigatorPanel` to a Qt rendering/effect adapter. Remove the
  legacy mutable tool fields and policy helpers without a compatibility facade
  or eager package export. Preserve signal order and payload copies: alignment
  clears before discard/accept, Array intent precedes Select, and completed
  ruler segments survive cancellation until explicit Clear. Immutable staged
  checkpoints preserve observer-visible Align/Array state and rebase context
  changes made by synchronous intent handlers before continuing.
- [x] Capture the full route only when the document or route changes. Ordinary
  selection/edit-safety refreshes reuse the frozen snapshot, while Array
  previews materialize selected route sources only. Counter regressions prove
  selection adds no capture and two hover updates add no route metadata
  deepcopies; a 10k-point diagnostic reduced mean hover time from the reviewer's
  `191.6 ms` to `0.082 ms` (timing is evidence, not the test gate).
- [x] Keep `design_plot_pane.py`, `design_layout_window.py`, `layout_state.py`,
  and the complete `route/` package byte-identical to `965806d`. Route
  Pause/Interrupt/Resume controls remain outside the session and pass the broad
  and full route-control regressions.
- [x] Focused tool-session/Design UI gate: `149 passed`; broad
  Design/UI/route-control gate: `1,322 passed` plus `5` subtests; full
  process-local `QLocale.c()` no-hardware gate: `2,957 passed` plus `14`
  subtests.
- [x] Import-order checks pass in both directions without exposing
  `DesignToolSession` from the panel module. Changed-file Ruff, whole-tree
  compileall, deletion checks, and `git diff --check` pass.
- [x] Metrics: `main.py` remains LOC `10917`, LLOC `5946`, aggregate CC `1904`
  (average `3.565543`); `design_navigator_panel.py` decreases LOC
  `2164 -> 1938` and aggregate CC `312 -> 269`. New MI values are
  `tool_context.py 34.83`, `tool_session.py 6.01`, and
  `test_tool_session.py 18.18`; no new MI-0 file is introduced.
- [x] The first frozen independent review found three Important ordering,
  deep-immutability, and GUI-thread performance gaps. Each received a strict
  failing regression before the generalized staged-transition and indexed
  snapshot fixes.
- [x] A fresh post-fix independent read-only review rechecked all three prior
  findings, canonical preview geometry/stale-source behavior, protected bytes,
  imports, tests, and metrics, then returned READY with no Critical or
  Important findings. Its one Minor notes that preview-only generated IDs may
  differ from the canonical full-route plan; only geometry leaves the session,
  and `DesignLayoutState` remains the canonical commit authority.

#### Task 5c: Separate Design navigator Qt adapters

**Files:**
- Create: `probe_station_gui/views/design_document_controls.py`
- Create: `probe_station_gui/views/design_registration_controls.py`
- Create: `probe_station_gui/views/design_route_controls.py`
- Create: `probe_station_gui/views/design_route_run_controls.py`
- Create: `probe_station_gui/views/design_tool_controls.py`
- Create: `tests/ui/test_design_navigator_sections.py`
- Modify: `probe_station_gui/views/design_navigator_panel.py`
- Modify: `tests/design/test_click_navigation.py`
- Modify: `tests/ui/test_design_navigator_panel.py`
- Modify: `tests/ui/test_design_plot_klayout.py`
- Modify: `tests/ui/test_design_plot_selection.py`
- Modify: `tests/ui/test_main_window_menus.py`
- Modify: `docs/superpowers/plans/2026-08-10-coordinate-system-coordinator.md`

- [x] Observe strict RED before production code. Collection first failed with
  `ModuleNotFoundError` for `views.design_document_controls`; after the first
  three adapters existed, a second collection RED failed for the absent
  `views.design_route_controls` before route/tool production extraction.
- [x] Extract document, registration, route-file/table, route-run, and tool Qt
  adapters. `DesignNavigatorPanel` remains the public compositor/facade: an AST
  comparison proves that no public method signature or Qt signal changed, and
  production callers do not read child-private widget state. The panel has no
  compatibility widget aliases and neither the panel nor lazy `views` exports
  `DesignToolSession`.
- [x] Keep `DesignRouteRunControls` as the sole panel-side owner of one
  `RouteRunControlState`. Regressions prove Pause request -> Interrupt,
  Interrupt remains available until requested, only safe waiting/pause ack ->
  Resume, external-measurement waiting -> Interrupt, stopped state clears
  waiting/pending state, and waiting Measure emits the existing `measure`
  confirmation. API route-control names and the external workflow are
  unchanged.
- [x] Preserve tool-session effect order, shared route selection, detached
  toolbar/options widget identity, registration UUID/no-echo behavior, and
  document top-cell/layer no-echo behavior. Focused navigator/adapter gate:
  `39 passed`; focused Design/UI facade gate before the added adapter
  characterizations: `139 passed`; safety gate covering Main/API control,
  autofocus, photo/contact placement, contact lifecycle, and safe-Z recovery:
  `172 passed`.
- [x] Keep `main.py`, `design_plot_pane.py`, `design_layout_window.py`,
  `layout_state.py`, `tool_context.py`, `tool_session.py`, `stage/autofocus_flow.py`,
  and the complete `route/` production package byte-identical to `c659f56`.
  Import-order checks pass panel/layout and panel/route-dialog in both
  directions; lazy exports remain cycle-free. Changed-file Ruff, compileall,
  deletion/ownership searches, protected hashes, and `git diff --check` pass.
- [x] Metrics: `main.py` remains LOC `10917`, aggregate CC `1904`;
  `design_navigator_panel.py` decreases LOC `1938 -> 634` and aggregate CC
  `269 -> 73`, with MI `15.42`. New MI values are document `40.87`,
  registration `31.60`, route `24.92`, route-run `40.56`, tool `5.27`, and
  adapter tests `30.77`; no new MI-0 file is introduced.
- [x] The first frozen independent review found one Important parity gap:
  hiding Markup pruned the adapter selection without refreshing the owned tool
  session, leaving Array preview geometry stale. Two strict RED regressions
  reproduced both programmatic and user-toggle paths. The child adapter now
  refreshes its current immutable context inputs after hidden-Markup pruning
  and before Array direction capture, so preview clear is emitted before the
  outward visibility signal. Post-fix focused Design/UI and route-safety gates
  pass `143` and `172` tests respectively.
- [x] Broad Design/UI/App gate reached `1170 passed` plus `5` subtests. The
  raw post-fix full process-local `QLocale.c()` no-hardware run reached
  `2964 passed` plus `14` subtests; repeated broad/full runs exposed the same
  unrelated exact-timer miss in untouched `test_microscope_interaction.py`.
  The deterministic final split gate passes `2964` tests plus `14` subtests
  with that node deselected, then passes the node `1/1` in a clean process. No
  out-of-scope timing change was made.
- [x] A fresh post-fix independent read-only review rechecked the Markup
  context/effect ordering, complete extraction parity, route safety, protected
  bytes, imports, API surface, and metrics. Its independent gates passed `143`
  focused Design/UI tests and `148` route/autofocus/contact tests; it returned
  READY with no Critical or Important findings.

#### Task 5d: Isolate the Design plot input pipeline

**Files:**
- Create: `probe_station_gui/design/plot_interaction.py`
- Create: `probe_station_gui/design/snap_coordinator.py`
- Create: `probe_station_gui/design/snap_protocol.py`
- Create: `probe_station_gui/views/design_snap_runtime.py`
- Create: `tests/design/test_plot_interaction.py`
- Create: `tests/design/test_snap_coordinator.py`
- Create: `tests/ui/design_plot_klayout_fixtures.py`
- Create: `tests/ui/design_plot_klayout_support.py`
- Create: `tests/ui/test_design_plot_navigation.py`
- Create: `tests/ui/test_design_plot_pipeline_ownership.py`
- Create: `tests/ui/test_design_snap_runtime.py`
- Modify: `probe_station_gui/views/design_plot_pane.py`
- Modify: `tests/design/test_click_navigation.py`
- Modify: `tests/ui/test_design_plot_klayout.py`
- Modify: `tests/ui/test_design_plot_move.py`
- Modify: `tests/ui/test_design_plot_selection.py`
- Modify: `tests/ui/test_main_window_menus.py`
- Modify: `docs/superpowers/plans/2026-08-10-coordinate-system-coordinator.md`

- [x] Make Qt-free `PlotInteraction` the sole owner of input-session state:
  action classification, tool/context generation, guide gestures, selection
  rectangles, move-drag click suppression, and immutable effects. Pure tests
  prove modifier parity and that preview/document-generation changes clear
  transient gestures.
- [x] Make Qt-free `SnapCoordinator` the sole owner of request identity,
  local Markup/geometry arbitration, hover replacement, click FIFO ordering,
  action-scoped invalidation, and immutable commands/publications/notices.
  Dedicated regressions prove transient tool clicks are cancelled while queued
  Calibration and Route Point actions survive unrelated tool/context changes;
  a stale FIFO head is consumed exactly once and releases its ready tail.
- [x] Move worker construction, signal adaptation, non-blocking retirement,
  and finished-driven deletion behind `DesignSnapRuntime`. The pane is now a Qt
  renderer/effect adapter and does not own worker queues, request generations,
  click completions, or retired-worker lifetimes.
- [x] Split the former KLayout test monolith into fixtures, support fakes, and
  navigation/pipeline modules. A strict architecture RED then found three
  remaining test-only click facades in the pane; tests were migrated to typed
  `SnapClickIntent`/`ClickPublication` seams and the facades were deleted.
- [x] Fresh focused process-local `QLocale.c()` gate passes `125` tests. Fresh
  Design/UI/App/route/API gate passes `1697` tests plus `7` subtests. Fresh full
  no-hardware gate passes `2983` tests plus `14` subtests in one process.
- [x] Changed-file Ruff, whole-tree compileall, `git diff --check`, package
  import-order, Qt-free ownership, runtime retirement, and deletion gates pass.
  `klayout_types.py`, `klayout_workers.py`, `model.py`,
  `design_klayout_raster.py`, and the complete `route/` package remain
  byte-identical to `2272a67`.
- [x] Metrics: `design_plot_pane.py` decreases LOC `2786 -> 2374`, LLOC
  `1734 -> 1372`, and aggregate CC `633 -> 521`. New MI values are
  `plot_interaction.py 5.62`, `snap_coordinator.py 0.48`,
  `snap_protocol.py 39.53`, and `design_snap_runtime.py 27.82`; every new
  Python module remains above MI 0. The pane itself remains MI 0 and therefore
  stays in the residual selected-cluster queue for the next strict-TDD pass.
- [x] The first frozen independent review found three Important parity and
  lifecycle gaps. Strict RED/GREEN regressions now publish only one ready click
  per adapter checkpoint so synchronous document/Markup/snap invalidation can
  reject a ready FIFO tail; retire and delete a fatally stopped active worker,
  cancel its pending work, and lazily attach a fresh worker on the next request;
  and preserve the actual Euclidean Markup snap distance instead of reporting
  zero. All focused, broad, full, static, protected-byte, and MI gates above
  were rerun after these fixes.
- [x] A fresh post-fix independent read-only review reran the focused gate
  (`125` tests), the three prior-finding reproductions (`8` tests), real-worker
  fatal recovery, import/ownership/static/protected-byte gates, and metrics. It
  returned READY with no Critical, Important, or Minor finding.

#### Task 5e: Separate Design plot presentation

**Files:**
- Create: `probe_station_gui/design/plot_presentation.py`
- Create: `probe_station_gui/views/design_plot_viewport.py`
- Create: `probe_station_gui/views/design_plot_route_rendering.py`
- Create: `probe_station_gui/views/design_plot_rendering.py`
- Modify: `probe_station_gui/views/design_plot_pane.py`
- Create: `tests/design/test_plot_presentation.py`
- Create: `tests/ui/test_design_plot_viewport.py`
- Create: `tests/ui/test_design_plot_route_rendering.py`
- Create: `tests/ui/test_design_plot_rendering.py`
- Split/delete: `tests/ui/test_design_navigator_panel.py`
- Modify: focused Design plot ownership/parity tests
- Modify: `docs/superpowers/plans/2026-08-10-coordinate-system-coordinator.md`

- [x] Observe strict RED before production: collection failed independently for
  all four absent presentation/viewport/render modules. Later contract REDs
  covered normalized mutation/render application, navigation-before-render,
  viewport input/publication normalization, snap-geometry completion, and a
  synchronous hover callback invalidating a transient click before its effect.
- [x] Make Qt-free `PlotPresentation` the sole owner of retained content,
  identity matching, route/Markup navigation filtering, normalized overlay
  state, selection pruning/hit testing, nested preview state, snap-geometry
  completion, dirty regions, and immutable render plans.
- [x] Make `DesignPlotViewport` the sole ViewBox/navigation/input adapter. It
  owns cached bounds and limits, focus/capture/restore, resize without rescans,
  fixed-pixel metrics, Qt event and snap-publication normalization, cursor and
  plot visibility, and hover/click telemetry. Limits remain applied before
  focus and click completion is recomputed only after synchronous hover effects.
- [x] Make `DesignPlotRouteRenderer` own route/preview items, labels, detail
  gates, immediate arrow invalidation, coalesced zero-delay rendering, visible
  16-ms scale retry, and timer shutdown. `DesignPlotRenderer` composes it and
  owns every other non-raster scene item plus dynamic remove-before-clear and
  preview visibility; the pane retains raster ownership and emits all outward
  signals.
- [x] Preserve same-object/same-content document behavior, candidate preview
  capture and identity-only restore, matching route/Markup limits, fixed-pixel
  overlays, route detail thresholds, synchronous signal order, snap FIFO
  revalidation, and terminal timers -> snap/runtime -> raster shutdown order.
  AST gates prove no migrated navigation, presentation, non-raster item, label,
  timer, or redraw ownership remains in the pane, and four unused pane facades
  were deleted rather than forwarded.
- [x] Mechanically split MI-0 `test_design_navigator_panel.py` by adapter seam
  into enablement/document/registration, tool, route-selection, and route-run
  safety files. All `35` original tests and bodies are preserved; the split
  files pass `5 / 23 / 5 / 2` tests and have MI `34.00 / 18.07 / 45.52 / 44.55`.
- [x] Fresh process-local `QLocale.c()` gates pass `155` focused tests, `853`
  Design/UI tests, and the complete no-hardware suite at `3012 passed` plus
  `14` subtests. Changed-file Ruff, whole-tree compileall, `git diff --check`,
  forward/reverse imports, Qt-free import, deletion, and ownership gates pass.
  The production whitelist is exactly the pane plus four new modules; all
  protected bytes from `9bb7264` remain unchanged.
- [x] Metrics: `design_plot_pane.py` decreases LOC `2374 -> 663`, LLOC
  `1372 -> 444`, aggregate CC `521 -> 166`, and MI `0.00 -> 4.34`. New module
  MI values are presentation `3.54`, viewport `2.33`, route rendering `22.88`,
  and general rendering `7.56`; contract-test MI values are `34.84`, `26.06`,
  `44.17`, and `42.76`. Repository MI-0 count decreases `24 -> 22` with no new
  MI-0 file.
- [x] A fresh independent read-only review reran `61` focused tests and audited
  lifecycle/reentrancy, render ownership, route thresholds, viewport caching,
  API parity, protected bytes, split-test equivalence, imports, static gates,
  and MI. It returned READY with no Critical or Important finding; its sole
  Minor corrected the viewport MI evidence above from `2.52` to `2.33`.

- [ ] Run Wily/Radon for the complete branch and compare with the frozen
  baseline. Require:

  - `main.py` raw/logical LOC and aggregate complexity are below baseline and
    below the previous pass; its MI must not regress;
  - `tests/app/test_main_design_navigation.py` MI > 0;
  - every coordinator file MI > 0;
  - no new MI-0 file;
  - repository MI-0 count below 30.

- [ ] If a selected file remains at MI 0, perform another strict-TDD extraction
  limited to residual Design/coordinate adapter code. Do not move unrelated
  camera, route-control, serial, settings, or instrument policy merely to move
  the metric.

- [ ] Collect final Wily from a unique disposable UTF-8 temp clone and external
  cache, comparing the base SHA `8fc83259e07f48bf0f222e80bb3e1d6c3241fd0e`
  with final HEAD. Never initialize or clean a Wily cache in the active
  worktree. Run direct `radon mi -s` on every selected/new file as the pass/fail
  per-file gate.

- [ ] Run the full native-locale suite from a fresh basetemp. If only known
  locale-sensitive tests fail, verify them and the full suite under
  process-local `QLocale.c()` without changing unrelated camera code.

```powershell
C:\Users\Public\code\probe_station_gui\.venv\Scripts\python.exe -m pytest tests -qq -p no:cacheprovider --basetemp $env:TEMP\coordinate-coordinator-full
C:\Users\Public\code\probe_station_gui\.venv\Scripts\python.exe -m ruff check probe_station_gui main.py tests
C:\Users\Public\code\probe_station_gui\.venv\Scripts\python.exe -m compileall -q probe_station_gui main.py tests
git diff --check 8fc83259e07f48bf0f222e80bb3e1d6c3241fd0e..HEAD
```

- [ ] Launch `main.py` from the worktree with hardware disconnected. Verify:
  startup is non-blocking, Coordinate System selector opens, Design Window
  opens, Coordinate Systems settings open, close/reopen works, and shutdown
  leaves no worker/thread error. Inspect both configured logs after the run.

- [ ] Request a fresh whole-branch review. Fix all Critical/Important findings
  with strict RED/GREEN evidence.

- [ ] Write the final report with commit SHAs, test counts, metric deltas, GUI
  evidence, known environment-only failures, and tracked-clean status. Do not
  commit generated Wily caches or temporary HTML reports.

#### Task 6a: Separate optical geometry processing

**Files:**
- Create: `probe_station_gui/camera/geometry_segmentation.py`
- Create: `probe_station_gui/camera/geometry_feature_tracking.py`
- Create: `probe_station_gui/camera/geometry_alignment_preview.py`
- Delete: `probe_station_gui/camera/geometry_mask.py`
- Modify: `probe_station_gui/camera/optical_calibration_geometry.py`
- Create: `tests/camera/test_geometry_segmentation.py`
- Create: `tests/camera/test_geometry_feature_tracking.py`
- Create: `tests/camera/test_geometry_alignment_preview.py`
- Delete: `tests/camera/test_geometry_mask.py`
- Modify: `tests/camera/test_optical_calibration_geometry.py`
- Modify: `docs/superpowers/plans/2026-08-10-coordinate-system-coordinator.md`

- [x] Observe strict interface RED before each production owner. Collection
  failed first with the exact absent-module `ModuleNotFoundError` for
  `geometry_segmentation`, then `geometry_feature_tracking`, then
  `geometry_alignment_preview`. Each owner was made GREEN before the next
  interface was introduced.
- [x] Make segmentation the direct owner of RGB/QImage frame conversion,
  padded-row handling, illumination-invariant masks, connected components,
  skeleton topology, and real feature descriptors. Make tracking the Qt-free
  owner of Stage prediction, compatibility gates, one-to-one assignment, track
  spread, and the existing `StageFeatureObservation` result semantics. Make
  preview composition the owner of strict signed 3x3 roles, fitted correction
  maps, one shared crop, occupancy, valid-footprint RGB disagreement, and
  detached `QImage.Format_RGB888` copies.
- [x] Delete the old production and test monoliths without a compatibility
  facade, forwarding alias, package export, or re-export. The dependency graph
  is segmentation <- tracking and segmentation <- preview; preview never
  imports tracking. `optical_calibration_geometry.fit_lens_artifact()` retains
  function-local direct imports, so importing the optical orchestrator still
  loads none of the three owners, OpenCV, `scipy.optimize`, or `scipy.spatial`.
  The segment -> track -> fit -> restore persisted Y convention -> validate ->
  preview order and every failure short-circuit remain unchanged.
- [x] Preserve all `27` former geometry test items as `8 / 4 / 15` tests at the
  three new interfaces. Regressions cover QImage row stride/source ownership,
  mask stability and topology, cropped component work, unique Stage-predicted
  assignment, match/spread/compatibility gates, recoverable fit data, fitted-map
  reuse, signed affine grid roles, exact common crop, proportional RGB channels,
  valid-footprint exclusion, detached QImage ownership, and malformed preview
  failure. The inherited acceptance of arbitrary finite nonzero numeric mask
  values remains unchanged rather than being fixed incidentally.
- [x] Verification is fresh and no-hardware: exact geometry/orchestrator gate
  `37 passed`; optical/camera/Main/UI focused gate `190 passed`; broad camera
  and relevant App/UI gate `369 passed`; full process-local `QLocale.c()`
  offscreen suite `3012 passed` plus `14` subtests. Affected Ruff, configured
  whole-tree Ruff with inherited `E402/F401` ignored, whole-tree compileall,
  `git diff --check`, forward/reverse imports, optical import purity, AST
  deletion/ownership, and protected-byte gates pass. No GUI or hardware entry
  point was run.
- [x] Metrics: old production owner `1251 LOC / 730 LLOC / CC 250 / MI 0.00`
  becomes three owners totaling `1395 LOC / 769 LLOC / CC 262`, with MI
  `13.94 / 18.32 / 13.09`. The `+12` aggregate CC comprises `+8` for keeping
  matrix and Stage-offset validation inside both independent tracking and
  preview owners instead of adding a shallow shared module, plus `+4` function
  bases from decomposing the former CC-29 signed-grid role validator. Old
  tests `1192 LOC / 522 LLOC / CC 217 / MI 0.00` become three files totaling
  `1235 LOC / 533 LLOC / CC 216`, with MI `22.21 / 24.86 / 13.67`.
  Orchestrator/test MI remain positive at `26.63 / 33.69`; duplicate rate is
  `0.00%`; the moved tracking interface retains its prior Lizard CCN-16 warning
  without adding another warning. All-tracked MI-0 count decreases `24 -> 22`
  and the active GUI/test scope decreases `22 -> 20`, with no new MI-0 file.
- [x] Freeze the complete unstaged/untracked diff and require a fresh independent
  read-only review with no Critical or Important finding before staging or the
  exact commit `refactor: separate optical geometry processing`.

#### Task 6b: Separate optical distortion fitting

**Files:**
- Create: `probe_station_gui/camera/bright_grid_detection.py`
- Create: `probe_station_gui/camera/bright_grid_calibration.py`
- Create: `probe_station_gui/camera/stage_geometry_fit.py`
- Create: `probe_station_gui/camera/seam_radial_fit.py`
- Modify: `probe_station_gui/camera/distortion.py`
- Modify: `probe_station_gui/camera/optical_calibration_geometry.py`
- Modify: `tests/camera/test_distortion.py`
- Create: `tests/camera/test_distortion_fit_ownership.py`
- Modify: `tests/camera/test_geometry_feature_tracking.py`
- Modify: `docs/superpowers/plans/2026-08-10-coordinate-system-coordinator.md`

- [x] Observe strict collection RED before production: direct owner imports
  failed with the exact absent-module `ModuleNotFoundError` for
  `bright_grid_calibration`, `stage_geometry_fit`, and `seam_radial_fit`.
  After the first GREEN exposed a zero-MI combined grid owner, a second direct
  interface RED failed for absent `bright_grid_detection` before morphology
  and regular-grid policy were extracted from calibration.
- [x] Keep `camera.distortion` as the genuine lightweight runtime owner of
  persisted correction records, payload construction and fail-closed
  validation, radial and Stage coordinate application, precompiled axis maps,
  lazy cached Stage maps, and stride-aware detached QImage correction. Make
  detection own morphology and regular-grid selection, calibration own axis
  and grid fitting, Stage fitting own coverage/residual/coordinate policy, and
  seam fitting own optimizer scoring and placement.
- [x] Delete every moved definition from `camera.distortion` without a facade,
  alias, re-export, or package export. The direct DAG is
  `stage_geometry_fit -> distortion + function-local bright_grid_detection`,
  `bright_grid_calibration -> bright_grid_detection + distortion`, and
  `seam_radial_fit -> distortion + imaging`; detection is independent of the
  runtime and the runtime has no reverse edge. The Stage grid-frame entry point
  imports detection function-locally, so
  runtime, live correction, and optical-orchestrator imports remain free of
  OpenCV, SciPy, seam fitting, and eager geometry-processing owners.
- [x] Preserve payload schemas and numeric thresholds, optimizer calls,
  corrected-coordinate and persisted-Y conventions, grid spacing and regular
  subsets, seam score/placement, map-cache counts, QImage row stride, source
  ownership, and identity-copy behavior. Counter regressions prove axis maps
  compile once during payload compilation and Stage maps compile once lazily;
  padded RGB888 output remains detached from its source.
- [x] Verification is fresh, offscreen, and no-hardware: exact Pass 6a/6b
  geometry gate `65 passed`; focused owner/runtime/live-correction/Main lens and
  optical gate `153 passed`; broad camera/App/UI gate `401 passed`; complete
  process-local `QLocale.c()` suite `3016 passed` plus `14` subtests, with only
  the inherited `BuiltinImporter.module_repr()` deprecation warning. Affected
  and configured whole-tree Ruff, whole-tree compileall, `git diff --check`,
  definition/literal/schema parity, forward/reverse import, AST deletion,
  runtime/orchestrator/live import-purity, and protected-byte gates pass.
- [x] Metrics: old runtime owner `2542 LOC / 1442 LLOC / CC 462 / MI 0.00`
  becomes runtime plus four fitting/detection owners totaling
  `2673 LOC / 1473 LLOC / CC 462`; their MI values are respectively
  `5.70 / 9.30 / 8.08 / 17.73 / 23.80`. The exact `94` Radon blocks and
  aggregate CC are preserved. The two Lizard warnings remain the inherited
  bright-grid bounds and regular-subset hotspots; no warning is added. Every
  touched/new Python file has positive MI. All-tracked MI-0 count decreases
  `22 -> 21` and active GUI/test scope decreases `20 -> 19`, with no new MI-0.
- [x] Freeze the complete unstaged/untracked diff and require a fresh independent
  read-only review with no Critical or Important finding before staging or the
  exact commit `refactor: separate optical distortion fitting`.

#### Task 6c: Split optical calibration test monoliths

**Files:**
- Delete: `tests/app/test_main_lens_distortion.py`
- Create: `tests/app/lens_distortion_test_support.py`
- Create: `tests/app/test_main_lens_distortion_delivery.py`
- Create: `tests/app/test_main_lens_distortion_objective_updates.py`
- Create: `tests/app/test_main_lens_distortion_start_reset.py`
- Delete: `tests/camera/test_optical_calibration_runtime.py`
- Create: `tests/camera/optical_calibration_runtime_test_support.py`
- Create: `tests/camera/test_optical_calibration_runtime_capture.py`
- Create: `tests/camera/test_optical_calibration_runtime_lifecycle.py`
- Create: `tests/camera/test_optical_calibration_runtime_adapters.py`
- Modify: `docs/superpowers/plans/2026-08-10-coordinate-system-coordinator.md`

- [x] Capture the clean `134650e` baseline before editing: the app/runtime
  monoliths collected `26 / 38` items and passed independently, their SHA-256
  values were `F3BBD437...CE1DD / 361A2638...84571`, and Radon reported
  `1215 LOC / 777 LLOC / CC 194 / MI 0.00` and
  `1372 LOC / 767 LLOC / CC 280 / MI 0.00` respectively.
- [x] Split the app tests into delivery/persistence, click-calibration objective
  ownership, and start/reset/error/cache seams. Split the runtime tests into
  capture/cleanup/return-to-start, lifecycle/concurrency/cancellation, and
  production-adapter/persistence seams. Keep only two small non-test support
  modules, preserve the runtime error-surfacing fixture locally in each runtime
  owner, and delete seven unused app fakes instead of introducing a facade.
- [x] Prove mechanical parity against the original HEAD sources: all `21 / 32`
  top-level test-definition ASTs, every retained helper AST, and all three
  fixture ASTs compare exactly. Normalized collection suffix hashes remain
  app `8b9e707d...09561`, runtime `f62783c3...267de1`, and combined
  `d740e384...04170`; normal and reverse orders both pass all `64` items.
- [x] Delete both original monoliths without a compatibility package facade,
  cross-test import, import-time purge, loader/sys.path workaround, production
  private alias, or filename-only architecture test. Direct deletion/import and
  single-definition gates pass, and all runtime tests share one canonical
  support `_InlineThread` identity.
- [x] Verification is fresh, offscreen, process-local `QLocale.c()`, and
  no-hardware: focused optical/camera/Main/UI `246 passed`; the timing and
  process-global monkeypatch selection passed `9` items three consecutive
  times; broad camera/App/UI `1143 passed` plus `5` subtests; full suite
  `3016 passed` plus `14` subtests with only the inherited
  `BuiltinImporter.module_repr()` deprecation warning. Affected and configured
  whole-tree Ruff, whole-tree compileall, `git diff --check`, Lizard warnings,
  AST/deletion/import gates, and exact production-byte/tree gates pass.
- [x] Metrics: the two old files totalled
  `2587 LOC / 1544 LLOC / CC 474`, while the eight direct owners/support files
  total `2585 LOC / 1507 LLOC / CC 437`; maximum CC stays `13`. Individual MI
  values are app `48.05 / 28.15 / 22.30 / 21.82` and runtime
  `27.90 / 21.55 / 12.72 / 32.53`. All-tracked MI-0 decreases `21 -> 19` and
  active GUI/test MI-0 decreases `19 -> 17`, with no new MI-0 file. Production
  remains byte-identical: `main.py` blob `6785640e...5853`,
  `probe_station_gui` tree `0bb6fb63...b05`, and scoped status/diff are empty.
- [x] Freeze the complete unstaged/untracked diff. A fresh independent read-only
  review returned `READY` with `0 Critical / 0 Important / 0 Minor`, repeated
  normal and reverse collection-order gates at `64 passed` each, and confirmed
  the frozen hashes before the exact commit
  `test: split optical calibration coverage`.

#### Task 7a: Separate probe station client domains

**Files:**
- Create: `probe_station_client/camera.py`
- Create: `probe_station_client/meter.py`
- Create: `probe_station_client/route.py`
- Modify: `probe_station_client/client.py`
- Modify: `probe_station_client/__init__.py`
- Create: `tests/api/test_client_domain_ownership.py`
- Modify: `docs/superpowers/plans/2026-08-10-coordinate-system-coordinator.md`

- [x] Capture the clean `95734dd` baseline before editing. The client had
  `1238 LOC / 1074 SLOC / 655 LLOC`, aggregate Radon CC `265` over `115`
  blocks, maximum CC `32`, and MI `0.00`. Its `34` direct tests passed; the
  client plus API-server baseline passed `68` tests plus `2` subtests. The
  all-tracked MI-0 count was `19`, while active GUI/test scope was `17`.
- [x] Observe strict collection RED independently for each new owner before its
  production extraction: `camera`, then `meter`, then `route` each failed with
  its exact absent-module `ModuleNotFoundError`. Each preceding domain was made
  GREEN before introducing the next interface.
- [x] Make `camera.py` the canonical owner of `CameraFrame` and camera settings,
  frame/header, exposure-policy, and one-shot exposure behavior. Make
  `meter.py` the canonical meter namespace while leaving `visa.py` as the
  unchanged owner of concrete remote VISA adapters. Make `route.py` the
  canonical owner of API route control, route sessions, ready contacts,
  polling/deduplication, result submission, and artifact bytes.
- [x] Keep `ProbeStationClient` in `client.py` as transport/auth/core
  stage/contact owner and composition root. It imports the three domains only
  through underscored module names; moved types have no definitions, aliases,
  forwarding facades, or wrappers in the old module. Route stopped/failed
  polling receives the exact `ProbeStationClientError` type from the core
  without a runtime reverse import.
- [x] Preserve the exact `22`-name package `__all__` and direct canonical root
  identities. `ProbeStationApiRouteControlClient` remains intentionally absent
  from the root surface. Fresh forward/reverse import gates preserve
  `ProbeStationClient` identity across QCoDeS and the package root, and prove
  domain modules do not retain a runtime `ProbeStationClient` reference.
- [x] Preserve request methods, paths, ordered query/payload construction,
  bearer headers, timeout overrides, status-to-error mapping, concrete VISA
  types, session/client identity, binary artifacts, ready-contact request-ID
  deduplication, and stop/timeout behavior. `pause`, `pause_ack`, and
  `interrupt` remain three distinct API route-control actions; no route name,
  payload schema, direct contact recipe, or server implementation changed.
- [x] Verification is fresh and no-hardware: client/ownership tests pass `41`;
  client/public-import/exposure tests pass `49`; client plus API-server
  contracts pass `75` plus `2` subtests; the complete offscreen,
  process-local-`QLocale.c()` suite passes `3023` plus `14` subtests with only
  the inherited `BuiltinImporter.module_repr()` warning. An earlier harness
  attempt used a nonexistent Public-user basetemp and produced setup-only
  errors; the corrected unique system-temp run above is the acceptance gate.
- [x] Affected Ruff, whole-tree client compileall, `git diff --check`, exact
  class-definition/deletion, one-way import, public-identity, protected-diff,
  and prospective tracked-file gates pass. `credentials.py`, `visa.py`,
  `qcodes_driver.py`, `tests/api/test_client.py`, `README.md`, `main.py`, the
  complete GUI/measure packages, and scripts remain byte-identical to the
  baseline.
- [x] Metrics: `client.py` decreases to
  `795 LOC / 715 SLOC / 421 LLOC / MI 1.81 / CC 148`; the new owners are
  camera `100 LOC / MI 52.36 / CC 17`, meter
  `79 LOC / MI 52.17 / CC 19`, and route
  `304 LOC / MI 24.42 / CC 81`. Aggregate CC remains exactly `265` over the
  same `115` blocks. No warning was added: the inherited `prepare_contact`
  CC-32 and `iter_ready` CC-16 warnings remain. All new/touched Python files
  have positive MI; prospective all-tracked MI-0 decreases `19 -> 18`, while
  active GUI/test MI-0 remains `17`.
- [x] Freeze the complete unstaged/untracked implementation snapshot. A first
  fresh independent read-only review returned `READY` with
  `0 Critical / 0 Important / 1 Minor`. A strict source-contract RED then
  reproduced its sole finding: the inherited local 2-tuple annotation did not
  describe the actual 3-tuple `iter_ready` deduplication key. The annotation-only
  GREEN preserves runtime behavior and passes the updated focused, API, static,
  metric, and complete-suite gates above. A final independent read-only review
  of the corrected evidence-bearing snapshot returned `READY` with
  `0 Critical / 0 Important / 0 Minor` before staging and the exact commit
  `refactor: separate probe station client domains`.

#### Task 8a: Separate settings persistence domains

**Files:**
- Create: `probe_station_gui/settings/document.py`
- Create: `probe_station_gui/settings/selection_persistence.py`
- Create: `probe_station_gui/settings/runtime_documents.py`
- Modify: `probe_station_gui/settings/manager.py`
- Modify: `probe_station_gui/settings/software_coordinate_selection_store.py`
- Create/modify: direct settings-owner and thin manager integration tests
- Modify: `docs/superpowers/plans/2026-08-10-coordinate-system-coordinator.md`

- [x] Capture the clean `a5d6393` baseline before editing. The settings manager
  had `1388 LOC / 1166 SLOC / 841 LLOC`, aggregate Radon CC `204` over `82`
  blocks, maximum CC `10`, and MI `0.00`; the selected focused baseline passed
  `119` tests. All-tracked MI-0 was `18` and active GUI/test MI-0 was `17`.
- [x] Observe independent absent-module collection RED for the document codec,
  selection persistence, and runtime documents before adding production code.
  Direct owner coverage then reached `25` cases; the owner plus Qt-store slice
  reached `31 passed` after review regressions were added.
- [x] Make `document.py` the canonical owner of `Settings`, deep cloning, the
  stable JSON payload, raw-section assembly, legacy migration, normalization,
  validation, and Telegram-token migration. Representative empty, packaged
  default, legacy, and future-schema payloads compare exactly with the baseline
  codec; camera exposure, logging, API, controls, software-coordinate degraded
  payloads, and design-directory shapes remain unchanged.
- [x] Make `selection_persistence.py` the canonical Qt-free owner of the frozen
  selection snapshot, monotonic generation, startup winner, stale rejection,
  and main/sidecar transaction. One persistence lock serializes ordinary main
  saves and selection saves; state capture uses the injected RLock while file
  I/O does not hold it, so GUI selection never waits for disk. Main settings are
  atomically replaced before exact-v1 sidecar validation/write; invalid/future
  sidecars are preserved on failure and restart resolves by greater generation
  with main winning ties. The Qt worker imports the canonical snapshot directly.
- [x] Make `runtime_documents.py` own controller, serial, and meter documents in
  the config directory. UTF-8-SIG reads, UTF-8 writes, filenames, malformed
  fallbacks, warnings, payloads, and auto-connect policy are preserved. A review
  RED restored the exact error boundary: config-directory creation errors
  propagate for all three documents; controller target-write errors propagate,
  while serial/meter target-write errors warn and return.
- [x] Keep `SettingsManager` as the platform/default/user-path composition root
  and public load/save/apply/accessor interface. All `39` public method
  signatures match the baseline AST. The manager contains no moved class or
  private persistence/codec definitions, settings siblings have no reverse
  manager import, fresh forward/reverse imports pass, and pure owner imports do
  not load PySide6. Deleting any owner prevents manager import as expected.
- [x] Replace moved-responsibility `SettingsManager.__new__`, private parser,
  and private atomic-writer test seams with direct owners and fully initialized
  temporary managers. Two App settings-integration tests were migrated without
  changing protected production. A second review RED proved the initial test
  environment could fall back to a user home on Linux; every real-manager test
  now fixes platform behavior or supplies APPDATA, LOCALAPPDATA,
  XDG_CONFIG_HOME, and XDG_STATE_HOME under its temporary fixture.
- [x] Verification is fresh and no-hardware: focused settings/selection/store/
  key-binding/connection/App tests pass `115`; broad settings/App/UI passes
  `1025` plus `5` subtests; the complete offscreen process-local-`QLocale.c()`
  suite passes `3021` plus `14` subtests with only the inherited
  `BuiltinImporter.module_repr()` warning. All APPDATA, LOCALAPPDATA, XDG,
  basetemp, and bytecode paths used unique system-temporary roots.
- [x] Configured whole-tree Ruff and compileall, affected Ruff, `git diff
  --check`, codec parity, canonical identity, normal/reverse import, Qt-free,
  AST deletion/DAG, Windows path/encoding, and protected-production diff gates
  pass. `main.py`, default settings, camera/API/stage/route/instrument/
  coordinate/design/view/dialog production, clients, measure package, and
  scripts remain byte-identical to the baseline.
- [x] Metrics: `manager.py` decreases to
  `552 LOC / 398 SLOC / 313 LLOC / MI 14.65 / CC 80` over `50` blocks with
  maximum CC `5`. The new owners are document
  `540 LOC / MI 24.84`, selection persistence `272 LOC / MI 26.83`, and runtime
  documents `171 LOC / MI 39.42`; every new/touched Python file has positive MI.
  Prospective all-tracked MI-0 decreases `18 -> 17` and active GUI/test MI-0
  decreases `17 -> 16`, with no new MI-0 file.
- [x] Freeze the corrected 16-file implementation snapshot and verify every
  per-file SHA-256 independently. The first fresh read-only review found two
  Important parity/isolation gaps; both were reproduced with strict temp-only
  RED and fixed as described above. A fresh corrected-byte re-review returned
  `READY` with `0 Critical / 0 Important / 0 Minor` before staging and the exact
  commit `refactor: separate settings persistence domains`.

#### Task 9a: Separate optical exposure sessions

**Files:**
- Create: `probe_station_gui/camera/optical_session.py`
- Create: `probe_station_gui/camera/exposure_adjustment.py`
- Modify: `probe_station_gui/camera/exposure_policy.py`
- Modify: `probe_station_gui/camera/__init__.py`
- Modify: `main.py` (direct owner import only)
- Delete: `tests/camera/test_exposure_policy.py`
- Create: `tests/camera/exposure_policy_test_support.py`
- Create: `tests/camera/test_exposure_adjustment.py`
- Create: `tests/camera/test_exposure_policy_transitions.py`
- Create: `tests/camera/test_exposure_policy_monitor.py`
- Create: `tests/camera/test_exposure_policy_lifecycle.py`
- Modify: `tests/camera/test_optical_session.py`
- Modify: `docs/superpowers/plans/2026-08-10-coordinate-system-coordinator.md`

- [x] Capture the clean `81e0dad` baseline before editing. The exposure-policy
  module had `1035 LOC / 735 LLOC / CC 233 / MI 0.00`; its monolithic test had
  `768 LOC / 544 LLOC / CC 169 / MI 0.00`. The policy plus optical-session
  baseline collected `48` items and passed six consecutive times; the broader
  exposure/session/API/server baseline passed `95` tests plus `2` subtests.
- [x] Observe strict absent-interface RED before each production extraction.
  Direct collection first failed with exact `ModuleNotFoundError` for
  `probe_station_gui.camera.optical_session`; after that owner became GREEN,
  the independent adjustment interface failed with exact
  `ModuleNotFoundError` for `probe_station_gui.camera.exposure_adjustment`.
- [x] Make `optical_session.py` the canonical owner of the session record,
  `OpticalSessionManager`, and `OpticalSessionLease`. It owns exclusive/nested
  open and close, policy/camera snapshots, fixed-manual restoration, rollback,
  stale-token and child ordering, idempotent lease close, and ordered cleanup
  error aggregation. Session operations keep the controller's blocking command
  gate while ordinary controller commands retain non-queued busy rejection.
- [x] Make `exposure_adjustment.py` a deep behavioral owner, not a helper bag.
  It owns software/native one-shot execution, recoverable nonconvergence and
  scene-change retry suppression, fresh-frame watermarks, monitor brightness
  decisions, normalized camera-node reads/writes, and camera snapshot/restore.
  The one-way runtime graph is `exposure_policy -> exposure_adjustment ->
  auto_exposure` and `optical_session -> exposure_policy`; no owner reverse
  imports the session manager or controller.
- [x] Keep `ExposurePolicyController`, policy/error identities, persistence,
  monitor publication/wake, shutdown, and state-callback orchestration canonical
  in `exposure_policy.py`. No callback is invoked under the command or state
  lock; callback order, fresh payload copies, exception swallowing, native-Off
  shutdown ordering, no acquisition stop/restart, API payload/error mapping,
  manual writes, policy rollback, and camera-node callback ordering remain
  unchanged.
- [x] Remove all session definitions, aliases, wrappers, and re-exports from the
  old policy module. The package root retains its exact seven-name `__all__`
  while directly exposing the canonical session identities. Fresh forward and
  reverse import orders pass; `main.py` changes only from the old import source
  to the canonical optical-session owner, and all other protected production
  paths are byte-identical to the baseline.
- [x] Split all `24` original policy test definitions by transitions,
  monitor/retry, and lifecycle/shutdown. Twenty function ASTs are byte-for-byte
  semantic matches; the other four differ only in deliberate private seam
  paths from the controller to its canonical adjustment owner. Both support
  class ASTs are exact matches and have one shared non-test identity. Four
  direct adjustment and two session regressions bring the focused collection
  to `54` items, including stale tokens and active-session shutdown.
- [x] Verification is fresh, offscreen, process-local `QLocale.c()`, and
  no-hardware: the final split passes `54`; all `54` timing/concurrency items
  pass in five consecutive clean processes; normal and reverse split order pass
  `54` each; focused exposure/session/auto/API broker/server/Qt/dialog/settings
  passes `133` plus `2` subtests; broad camera/App/UI passes `1148` plus `5`
  subtests before review. After the review correction, broad camera/App/UI
  passes `1149` plus `5` subtests and the final complete suite passes `3027`
  plus `14` subtests with
  only the inherited `BuiltinImporter.module_repr()` warning. The known
  load-sensitive monitor/retry case did not fail in any raw run, and no
  production timer was changed for scheduling.
- [x] Affected and configured whole-tree Ruff, whole-tree compileall, `git diff
  --check`, direct-definition/deletion/DAG/public-identity gates, protected-byte
  checks, and Radon/Wily/Lizard gates pass. Lizard retains exactly the moved
  `OpticalSessionManager.open` warning while improving its CCN `17 -> 16`; no
  warning is added.
- [x] Metrics: the controller is now
  `497 LOC / 338 LLOC / CC 100 / MI 12.18`; adjustment is
  `351 LOC / 214 LLOC / CC 84 / MI 18.23`; optical sessions are
  `268 LOC / 209 LLOC / CC 58 / MI 23.77`. The explicit collaborator boundary
  redistributes production CC from `233 -> 242` while removing both MI-zero
  monoliths. Every new architectural and split-test Python file has positive MI
  (minimum `12.18`); the pre-existing import-only `main.py` remains MI zero.
  Prospective all-tracked MI-zero decreases exactly `17 -> 15` and active
  GUI/test MI-zero decreases `16 -> 14`, with no new MI-zero file. A disposable
  fresh-Git Wily archive independently confirms controller MI `0 -> 12.1765`
  and CC `233 -> 100`, plus positive MI for every new owner and split test.
- [x] A first fresh independent review returned `READY` with
  `0 Critical / 0 Important / 1 Minor`. Its sole observation was an empty-error
  text drift in snapshot-restore aggregation. A strict direct RED reproduced
  `RuntimeError` where the baseline preserved an empty string; the corrected
  owner now uses exact baseline `str(exc)` aggregation, the dead normalization
  helper is deleted, and the new regression plus final gates above are GREEN.
- [x] Freeze the complete unstaged/untracked evidence-bearing source snapshot.
  A second fresh independent read-only review of the corrected owner and exact
  hashes returned `READY` with `0 Critical / 0 Important / 0 Minor`, repeated
  normal and reverse order at `54 passed` each, and independently confirmed the
  direct DAG, identities, AST migration, Radon/Lizard metrics, and protected
  scope before staging and the exact commit
  `refactor: separate optical exposure sessions`.

#### Task 10a: Separate camera imaging domains

**Files:**
- Create: `probe_station_gui/camera/flat_field_processing.py`
- Create: `probe_station_gui/camera/microscope_artifacts.py`
- Create: `probe_station_gui/camera/scan_scale_refinement.py`
- Create: `probe_station_gui/camera/scan_stitching.py`
- Modify: `probe_station_gui/camera/imaging.py`
- Modify: `main.py` (direct owner imports only)
- Modify: camera scan, runtime, flat-field, live-correction, and Telegram callers
  (direct owner imports only)
- Split: `tests/camera/test_imaging.py` into direct domain characterization files
- Modify: direct consumer tests for canonical owner imports
- Modify: `docs/superpowers/plans/2026-08-10-coordinate-system-coordinator.md`

- [x] Capture the clean `e75fdb1` baseline before editing. `imaging.py` had
  SHA-256 `e3d4c7f4...17a34e0`, Git blob `592e4132...5dca18`, and
  `1803 LOC / 1140 LLOC / 1567 SLOC / CC 280 / 77 blocks / MI 0.00`.
  Its monolithic test had `1086 LOC / MI 25.07`; the focused camera imaging,
  scan, runtime, optical, Main, and Telegram baseline passed `181` tests.
- [x] Observe strict absent-module RED before production for each new owner:
  direct collection failed with exact `ModuleNotFoundError` for
  `flat_field_processing`, then `microscope_artifacts`, then
  `scan_scale_refinement`, and finally `scan_stitching`. Each owner was made
  GREEN before the old definition was deleted.
- [x] Make `flat_field_processing.py` the canonical owner of profile building,
  median references, compiled gain caches, profile/self application, and the
  stride-aware detached QImage/RGB conversions. Padded RGB888 rows, detached
  array and QImage ownership, RGB32 correction output, odd blur-radius
  normalization, exact profile-versus-compiled pixels, and median truncation
  are characterized directly. Live correction preserves raw -> flat -> lens
  order and the compiled cache without acquisition restart.
- [x] Keep immutable scale, tile, and plan identities plus scan plan/bounds and
  shared stage-coordinate placement primitives canonical in `imaging.py`.
  Fallback conversion remains `(+dx, -dy)`, full 2x2 inversion and singular
  rejection are direct tests, overlap remains clamped to `[0, 0.95]`, and
  serpentine rows retain their original top-to-bottom and odd-row order.
- [x] Make `scan_scale_refinement.py` own overlap registration and robust scale
  fitting, including the deliberately dormant integer refinement helpers.
  Axis-neighbor filtering, `0.01` response, `96 px` shift, `12 px` similarity
  inliers, 50% consensus, full affine least-squares inversion, subpixel phase
  translation, implausible-shift rejection, and original-object fallback are
  preserved by direct characterization.
- [x] Make `scan_stitching.py` own photometry and blending while keeping
  `stitch_scan_tiles` strictly stage-coordinate-only: it never imports or calls
  the refinement owner or placement/phase registration. Signed placement,
  input-order sequential photometry, overlap masks and planes, float32 clipping,
  subpixel `INTER_LINEAR` warp, ceil extents, feature-preserving hard selection,
  and the `0.05` feather ramp retain their original implementation. The
  stage-only bright-column regression remains exactly `[6]`.
- [x] Make `microscope_artifacts.py` own capture records, overlay rendering,
  filenames, PNG/raw persistence, embedded metadata, and UTF-8 JSON sidecars.
  Direct tests preserve the exact top-level payload order
  `FileName / MicroscopeImage / Microscopy`, OME-style pixel fields, raw
  RGB888 copies, overlay separation, non-ASCII encoding, path deduplication,
  and route/scan filename numbering. Exact definition AST parity preserves the
  original annotated/raw/sidecar partial-failure order.
- [x] Use a direct acyclic owner graph: artifact/refinement/stitching depend on
  canonical scan identities in `imaging`, `imaging` and stitching consume the
  canonical flat-field image conversion owner, and no child owner is reverse
  imported. `imaging.py` contains no compatibility wrappers, aliases, or old
  moved interfaces; `camera/__init__.py` remains byte-identical with its exact
  seven canonical exports. Production consumers import moved identities from
  their canonical owners.
- [x] Preserve every original production definition. After normalizing only
  the two intentionally public QImage helper names and their added docstrings
  plus module-qualified calls to the canonical flat-field owner, all `70`
  original top-level class/function ASTs occur exactly once across the five
  owners; aggregate Radon complexity remains exactly `CC 280 / 77 blocks`.
  All `11` migrated caller modules, including `main.py`, have exact non-import
  AST parity with the baseline.
- [x] Split all `22` original imaging characterization tests by owner
  (`2` artifact, `5` refinement, `1` planning, `10` stitching, `4` flat-field).
  Their function ASTs are exact after normalizing only canonical import sources.
  New direct regressions cover QImage stride/copy/format, cache equivalence,
  artifact schemas/filenames, full-matrix scale conversion, diagonal-neighbor
  exclusion, and stage-only stitching. The six owner/integration files pass
  `33` tests and every split/new test file has positive MI (minimum `35.07`).
- [x] Verification is fresh, no-hardware, offscreen, process-local
  `QLocale.c()`, and uses unique temporary basetemps. The final focused owner
  and runtime-consumer gate passes `192`; broad camera/App/UI passes `1174`
  plus `5` subtests; the final complete suite passes `3038` plus `14` subtests
  with only the inherited `BuiltinImporter.module_repr()` warning. No timing
  threshold, hardware/session/cancellation/safe-Z behavior, acquisition stop,
  serial access, network access, or GUI entry point changed.
- [x] Affected Ruff and format checks, Main with inherited E402 ignored,
  whole-tree compileall, `git diff --check`, forward/reverse import order,
  deletion/direct-definition/DAG gates, and protected-byte checks pass. The
  original single Lizard warning is retained at its moved
  `refine_scan_scale_from_tile_overlaps` owner (`CCN 17`) with no new warning.
  `camera/__init__.py`, `seam_radial_fit.py`, and
  `optical_calibration_runtime.py` retain baseline blobs
  `dfec7a4...3375 / c3c8e30...a35f / f11e745...108`.
- [x] Metrics: the residual composition owner is
  `346 LOC / 213 LLOC / CC 53 / MI 24.10`; flat field is
  `297 / 159 / 40 / 30.70`; artifacts `387 / 253 / 63 / 18.36`; refinement
  `387 / 237 / 68 / 21.49`; stitching `487 / 314 / 56 / 14.23`. Every new or
  touched Python file has positive MI except the pre-existing import-only
  `main.py`. All-tracked MI-zero decreases exactly `15 -> 14` and active
  GUI/test MI-zero decreases `14 -> 13`, with no new MI-zero file.
- [x] Freeze the complete unstaged/untracked evidence-bearing source snapshot.
  A fresh fork-none independent read-only review returned `READY` with
  `0 Critical / 0 Important / 0 Minor`, independently repeated the exact
  focused selection at `192 passed`, and confirmed the `70` definition and
  `11` caller AST gates, all `22` migrated characterization tests, direct DAG,
  stage-only stitching, exact MI/CC/block counts, protected blobs, and unchanged
  production/test hashes before staging and the exact commit
  `refactor: separate camera imaging domains`.

#### Task 11a: Separate KLayout worker families

**Files:**
- Create: `probe_station_gui/design/klayout_render_worker.py`
- Create: `probe_station_gui/design/klayout_snap_worker.py`
- Create: `probe_station_gui/design/klayout_structure_bounds_worker.py`
- Create: `probe_station_gui/design/klayout_worker_runtime.py`
- Delete: `probe_station_gui/design/klayout_workers.py`
- Modify: direct production and test import owners only
- Modify: `tests/ui/test_design_plot_pipeline_ownership.py`
- Modify: `docs/superpowers/plans/2026-08-10-coordinate-system-coordinator.md`

- [x] Observe strict absent-module RED before each canonical owner. Direct
  collection failed independently with the exact `ModuleNotFoundError` for
  `klayout_render_worker`, `klayout_snap_worker`, the neutral shared
  `klayout_worker_runtime`, and `klayout_structure_bounds_worker`. A separate
  architecture RED then failed only because the replaced monolith still
  existed; after direct caller migration the old file and all old imports were
  deleted without a compatibility facade, alias, package export, or re-export.
- [x] Make the render family own newest-only submit coalescing, creator-thread
  lifecycle, queued unlocked publication, stale-generation suppression, and
  the thread-owned KLayout `LayoutView` cache, visible-layer configuration,
  inverse viewport rotation, detached image rotation, and dimension checks.
  Make the snap family own newest-only hover, FIFO-priority clicks, cooperative
  cancellation generations, stale-config suppression, the independent KLayout
  database cache, bounded contour/candidate/time collection, and rotated snap
  results. Make the structure-bounds family own newest-only requests, fixture
  bounds, visible recursive shape bounds, bounded nonblocking stop, parent
  detachment, strong retirement, creator-thread `finished`, and `deleteLater`.
- [x] Keep only genuine shared runtime knowledge in
  `klayout_worker_runtime.py`: one immutable render/snap publication record,
  the worker stop/thread contract, and the two-consumer KLayout box/polygon/
  path/edge contour adapter. The direct acyclic graph is render -> runtime +
  types, snap -> runtime + geometry + types + model, and structure -> runtime +
  types. KLayout itself remains function-locally imported only when a backend
  loads a file; fresh forward/reverse imports and worker construction leave
  `klayout`, `klayout.db`, and `klayout.lay` absent from `sys.modules`.
- [x] Preserve all worker and caller characterization. The core render/snap/
  structure/backend/real-KLayout selection passes `46` tests; the focused
  worker/raster/runtime/plot selection passes `111`; the architecture-adjacent
  Design/KLayout/minimap/shutdown selection passes `163`. Five fresh-process
  lifecycle stress runs pass `52` tests each. The broad Design/UI/App gate
  passes `1222` plus `5` subtests, and the complete offscreen process-local
  `QLocale.c()` no-hardware suite passes `3038` plus `14` subtests with only the
  inherited `BuiltinImporter.module_repr()` deprecation warning.
- [x] Affected and configured whole-tree Ruff, whole-tree compileall,
  `git diff --check`, direct-definition/deletion searches, forward/reverse lazy
  imports, and normalized non-import AST parity for `main.py` plus all six
  production callers pass. `klayout_types.py`, `klayout_geometry.py`,
  `model.py`, the snap coordinator, plot pane/render/presentation modules, and
  the complete route and stage production trees remain byte-identical to the
  baseline; protected view callers change only their canonical import source.
- [x] Metrics: the deleted owner was
  `1261 LOC / 826 LLOC / 1121 SLOC / CC 278 / 81 blocks / MI 0.00`.
  The render, snap, structure, and shared-runtime owners are respectively
  `330 / 220 / 288 / MI 20.11`, `604 / 373 / 544 / MI 9.44`,
  `330 / 223 / 287 / MI 19.19`, and `73 / 43 / 54 / MI 49.92` for
  LOC/LLOC/SLOC/MI. Aggregate production complexity remains exactly
  `CC 278 / 81 blocks`, with maximum CC `17`. Every new owner and every touched
  Python file has positive MI except the pre-existing import-only `main.py`;
  the lowest positive value remains `test_klayout_workers.py 0.37`.
  Prospective all-tracked MI-zero decreases exactly `14 -> 13`, and active
  GUI/client/test MI-zero decreases `13 -> 12`, with no new MI-zero file.
- [x] Freeze the complete unstaged/untracked evidence-bearing snapshot. A fresh
  fork-none independent read-only review compared all four owners with the full
  deleted monolith, independently repeated the `46` core and `7` ownership
  tests plus Ruff, diff, import, AST, Radon, and MI-zero gates, and returned
  `READY` with `0 Critical / 0 Important / 0 Minor`. No file was staged or
  committed before that verdict; the exact commit remains
  `refactor: separate klayout worker families`.

#### Task 12a: Separate camera settings controls

**Files:**
- Create: `probe_station_gui/dialogs/camera_feature_page.py`
- Create: `probe_station_gui/dialogs/camera_feature_editors.py`
- Create: `probe_station_gui/dialogs/camera_exposure_controls.py`
- Modify: `probe_station_gui/dialogs/camera_settings_dialog.py`
- Modify: direct camera-settings tests only
- Modify: `docs/superpowers/plans/2026-08-10-coordinate-system-coordinator.md`

- [x] Observe separate strict absent-module REDs before each canonical owner.
  Direct collection failed independently with the exact `ModuleNotFoundError`
  for `camera_feature_editors`, `camera_feature_page`, and
  `camera_exposure_controls`. A later completion-seam regression first failed
  because the old three-argument completion port still accepted a returned
  node; it passes after completion became latest-draft-only and the duplicate
  node route was removed.
- [x] Keep canonical `CameraSettingsWidget` in
  `camera_settings_dialog.py`. The residual owns snapshot request UUIDs and
  stale-response filtering, the `1500 ms` visible live-refresh timer, general
  pending settings, ordered apply delivery, synchronous completion/reentrancy,
  and final status/signal publication. It composes the two visible owners and
  contains no old exposure-widget compatibility aliases.
- [x] Make `CameraFeaturePage` own hidden-page lazy materialization and the
  latest node collection. Make `CameraFeatureEditors` own the grid, typed
  editors, sliders, value conversion, layout signatures, and focus-safe live
  updates. Direct tests prove that hidden pages create no editors until shown
  and same-layout refreshes neither rebuild nor overwrite a focused draft.
- [x] Make `CameraExposureControls` own exposure widgets, policy snapshots,
  pending mode/policy/time drafts, optical-session disablement, and the exact
  mode -> policy -> exposure-time action order. Direct and integration tests
  preserve TriggerWidth/software rules, manual exposure delivery, adjust-once,
  latest-pending-only completion, synchronous `apply_finished`, and reentrant
  advancement. The returned exposure node is routed once before the matching
  draft is cleared.
- [x] Use a direct acyclic owner graph: the residual imports exposure and page,
  page imports editors, and neither child imports the residual or
  `settings_dialog.py`. Each module exports only its canonical class; the old
  `_FeatureListPage` definition is absent. Fresh forward/reverse imports and
  exact definition/deletion/DAG/public-surface assertions pass.
- [x] Preserve protected consumers byte-for-byte. `settings_dialog.py` and
  `camera/worker.py` retain baseline blobs
  `5f613460416a65b2f1711014eddd474cde2b761c` and
  `d8da312589c4dc3cccd114af8c02db3d91e79d64`; no package export, camera worker,
  GUI-thread, hardware-I/O, or acquisition lifecycle changed.
- [x] Verification is fresh, offscreen, no-hardware, process-local
  `QLocale.c()`, and uses unique basetemps. The final focused owner/UI/Main API
  and settings integration gate passes `85`; broad camera/App/UI passes `1166`
  plus `5` subtests; the complete suite passes `3044` plus `14` subtests with
  only the inherited `BuiltinImporter.module_repr()` warning. Affected Ruff and
  format, whole-tree compileall, `git diff --check`, import-order, AST, and
  protected-byte gates pass.
- [x] Metrics: the former owner was
  `1268 LOC / 892 LLOC / 1157 SLOC / CC 421 / 73 blocks / max CC 28 / MI 0.00`.
  Residual, exposure, editors, and page are respectively
  `474 / 303 / 427 / CC 148 / MI 3.30`,
  `412 / 301 / 359 / CC 137 / MI 4.81`,
  `478 / 340 / 431 / CC 150 / MI 1.75`, and
  `99 / 61 / 78 / CC 28 / MI 49.77` for LOC/LLOC/SLOC/complexity/MI.
  Composition increases aggregate production complexity to
  `CC 463 / 97 blocks / max CC 29`; these extra blocks are the explicit hidden
  page and exposure lifecycle/query/delivery ports rather than duplicated
  policy bodies. Lizard warnings decrease from `5` to `4`. Every touched
  Python file has positive MI; all-tracked MI-zero decreases exactly
  `13 -> 12` and active GUI/client/test MI-zero decreases `12 -> 11` with no
  new MI-zero file.
- [x] Freeze the complete evidence-bearing snapshot. A fresh fork-none
  independent read-only review retained all `9` source/test/plan hashes,
  repeated the focused `85`, Ruff/format, in-memory compile, imports, AST,
  protected-byte, Radon, Lizard, and MI-zero gates, and returned `READY` with
  `0 Critical / 0 Important / 0 Minor`. It additionally verified a
  background-thread exposure completion reaches the GUI thread through the
  signal proxy and classified the `+42 CC / +24 blocks` as justified explicit
  composition ports with `0.00%` duplicated policy.

#### Task 13a: Separate design document domains

**Files:**
- Create: `probe_station_gui/design/document_source.py`
- Create: `probe_station_gui/design/document_snap.py`
- Modify: `probe_station_gui/design/model.py`
- Modify: `probe_station_gui/design/rigid_registration.py`
- Modify: direct registration import callers/tests only
- Modify: `docs/superpowers/plans/2026-08-10-coordinate-system-coordinator.md`

- [x] Observe independent strict REDs before each canonical production slice.
  Source and snap test collection each failed first with the exact absent-owner
  `ModuleNotFoundError`; delegation regressions then failed while
  `DesignDocument` still executed the old bodies. Registration tests failed
  with the exact missing-symbol `ImportError` before the value types moved.
  Later surface REDs caught the stale root-package registration export and the
  duplicate snap type aliases/dead projection seam before their deletion.
- [x] Keep `DesignDocument`, `DesignModelError`, `MeasurementTarget`,
  `SnapResult`, `LayerKey`, and `Point2D` canonical in `model.py`, with exactly
  those six public exports. The document retains immutable lifecycle changes,
  `_from_components`, top-cell/layer/rotation selection, plot paths, unit
  conversion, and the established snap API, while delegating source and snap
  policy instead of duplicating it.
- [x] Make `document_source.py` own file and fixture normalization, lazy
  function-local KLayout loading, fresh `source_load_id`, named-cell and layer
  metadata, database/user units, bounds, and fixture contour extraction. File
  documents retain no live KLayout layout/cell, and forward/reverse imports
  plus construction leave `klayout`, `klayout.db`, and `klayout.lay` absent
  from `sys.modules` until an actual file load.
- [x] Make `document_snap.py` own closed-contour array construction, the
  `256`-division spatial index, the `64`-cell long-segment budget, bounded
  candidate lookup, midpoint/vertex/segment/free priority, and an owner-local
  result DTO. Precomputed arrays remain caller-owned by identity; only a
  missing index is built. No duplicate `LayerKey`/`Point2D`, old private
  compatibility alias, or package export remains.
- [x] Move `ResidualSummary` and `DesignRegistration` canonically and with
  exact class AST into `rigid_registration.py`. Proper-rotation fitting,
  spacing-ratio diagnostics, immutable matrices, source/check residuals,
  inverse transforms, validation, and stale lifecycle semantics remain exact.
  All production/test callers import the new owner directly; `model.py`, the
  design package, and the root lazy package expose no registration alias.
- [x] Use the acyclic owner graph `model -> document_source + document_snap`
  and `rigid_registration -> model`; neither child imports its parent or its
  sibling. Five import-only callers retain exact normalized non-import AST.
  The root package changes only by deleting the stale lazy registration entry.
  Recent plot/presentation/tool/layout/route/KLayout worker and pane modules,
  and all other production trees, remain byte-identical to baseline.
- [x] Preserve document serialization, selection, registration, rotation,
  click/layout/plot/snap/KLayout, coordinate, and application behavior. The
  final owner/core gate passes `139`; complete Design passes `374`, coordinates
  `201`, and UI `495`. Deterministic App coverage passes `368` plus `5`
  subtests with one inherited Qt-order node deselected, and that node passes
  alone, for all `369` App tests. The combined Design/UI/App coverage is
  therefore `1238` plus `5` subtests.
- [x] Prove the App-only native Qt order failure is inherited rather than a
  Task 13a regression. The same six-module selection at a separately archived,
  blob-verified exact `78ef89ad` baseline and at the current tree both reach
  the identical access violation in unchanged
  `microscope_interaction.py:548`; the exact node and its full module pass in
  fresh processes. The final complete one-process offscreen `QLocale.c()`
  no-hardware suite passes `3054` plus `14` subtests with only the inherited
  `BuiltinImporter.module_repr()` warning.
- [x] Pass affected Ruff and format, whole-tree Ruff with only the inherited
  `E402`/`F401` classes excluded, whole-tree compileall, `git diff --check`,
  direct-definition/deletion/public-identity/DAG/import-order gates, caller
  AST parity, protected-byte checks, and prospective MI-zero accounting.
- [x] Metrics: the original model was
  `1248 LOC / 757 LLOC / 1096 SLOC / CC 238 / 53 blocks / max CC 20 / MI 0.00`;
  the original rigid owner was `166 / 101 / 128 / CC 26 / 9 blocks / max CC 10
  / MI 33.41`. Final model, source, snap, and rigid owners are respectively
  `659 / 367 / 568 / CC 114 / 33 blocks / MI 7.96`,
  `230 / 152 / 196 / CC 53 / 7 / MI 31.92`,
  `408 / 213 / 360 / CC 58 / 10 / MI 19.98`, and
  `340 / 220 / 268 / CC 53 / 20 / MI 21.38` for LOC/LLOC/SLOC/complexity/MI.
  Explicit source/snap composition ports change aggregate production
  complexity from `CC 264 / 62 blocks` to `CC 278 / 70 blocks`; maximum CC
  remains `20`. Every structural/new/touched file has positive MI except the
  explicitly allowed pre-existing MI-zero `design/session.py`, whose only
  change is the unavoidable direct import and whose non-import AST is exact.
  All-tracked MI-zero decreases exactly `12 -> 11`, active GUI/client/test
  MI-zero decreases `11 -> 10`, and no new MI-zero file is introduced.
- [x] Freeze the complete evidence-bearing source/test/plan snapshot. A fresh
  fork-none independent read-only review compared the full old and new owners,
  repeated the exact `139`, Ruff/format/diff, scope/hash/protected-byte,
  AST/DAG/import/lazy/array, Radon/Lizard, and MI-zero gates, and returned
  `READY` with `0 Critical / 0 Important / 0 Minor`. Its initial Minor found
  only the documented snap/aggregate CC arithmetic; after the plan-only
  `58 / 278` correction it re-ran the focused and metric gates and confirmed
  all `13` source/test hashes unchanged. Nothing was staged before the final
  verdict; the exact commit remains
  `refactor: separate design document domains`.
