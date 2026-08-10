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
- Modify: `probe_station_gui/coordinates/coordinator.py`
- Modify: `probe_station_gui/coordinates/coordinator_model.py`
- Create: `probe_station_gui/coordinates/coordinator_registration.py`
- Modify: `probe_station_gui/design/registration_lifecycle.py`
- Modify: `probe_station_gui/design/navigation_adapter.py`
- Modify: `probe_station_gui/views/main_window_connection_flow.py:62-104,175-390`
- Modify: `main.py:4387-4446,4700-4815,6190-6505,8912-9006,10040-11238`
- Create: `tests/coordinates/test_coordinator_registration.py`
- Modify: `tests/design/test_registration_lifecycle.py`
- Modify: `tests/app/test_main_design_navigation.py`
- Modify: `tests/design/test_workflow.py`
- Modify: `tests/app/test_main_route_measurement_session.py`
- Modify: `tests/route/test_contact_lifecycle_interface.py`
- Modify: `tests/route/test_measurement_api_route_control.py`
- Modify: `tests/route/test_point_execution.py`

**Owned after this task:** active Design frame link, registration instance
selection, capture tokens/evidence/normalization, fit/commit, exact rollback,
focus candidate/move/autofocus/Z commit, and Z-gated first-contact/A commit.

- [ ] **Step 1: Add workflow RED tests**

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

- [ ] **Step 2: Run the strict RED gate**

```powershell
C:\Users\Public\code\probe_station_gui\.venv\Scripts\python.exe -m pytest tests\coordinates\test_coordinator_registration.py -q --basetemp $env:TEMP\coordinate-coordinator-task2-red
```

- [ ] **Step 3: Add explicit workflow operations**

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

- [ ] **Step 4: Replace Main registration policy**

Convert the Main methods in the listed ranges into signal/result translation
and intent execution. Delete `_apply_registration_effects()`,
`_discard_all_registration_evidence()`, direct lifecycle calls, registry
replace/add policy, publication enrichment, context-currentness policy, and
session baseline reconstruction. Do not leave private forwarding methods with
the deleted names.

- [ ] **Step 5: Move tests to the workflow boundary**

Move pure registration/focus/contact cases from the 5,008-line Main test into
`test_coordinator_registration.py`. Keep real Main adapter tests for one image
capture, one armed-crosshair capture, focus signal wiring, first-contact
callback, stale callback inertness, and route interrupt propagation.

- [ ] **Step 6: Verify, measure, review, commit**

```powershell
C:\Users\Public\code\probe_station_gui\.venv\Scripts\python.exe -m pytest tests\coordinates\test_coordinator_registration.py tests\design\test_registration_lifecycle.py tests\app\test_main_design_navigation.py tests\design\test_workflow.py tests\design\test_frame_references.py tests\app\test_main_route_measurement_session.py tests\route -q --basetemp $env:TEMP\coordinate-coordinator-task2-green
```

Run Ruff, compileall, diff-check, per-file MI/CC, and independent review.

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

- [ ] **Step 1: Add selection/projection RED tests**

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

- [ ] **Step 2: Run the strict RED gate**

```powershell
C:\Users\Public\code\probe_station_gui\.venv\Scripts\python.exe -m pytest tests\coordinates\test_coordinator_selection.py -q --basetemp $env:TEMP\coordinate-coordinator-task3-red
```

- [ ] **Step 3: Add explicit selection/observation operations**

Implement `select_system()`, `observe_authority()`,
`synchronize_custom_systems()`, `snapshot()`, and current Design lease/project
queries. Absorb `FrameSelectionDecision`, `DesignFrameUsabilitySnapshot`, and
presentation-plan policy; return finished snapshot and optional
`PersistCoordinateSelectionIntent`.

Selection persistence remains intentionally fire-and-forget. Main executes the
intent through the existing selection store; only failure is reported back as
an adapter notice. Do not add a fake durable-success acknowledgement.

- [ ] **Step 4: Finish ownership migration**

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

- [ ] **Step 5: Split the MI-0 test monolith by interface**

Move remaining coordinate-domain characterizations into:

- `tests/coordinates/test_coordinator_persistence.py`;
- `tests/coordinates/test_coordinator_registration.py`;
- `tests/coordinates/test_coordinator_selection.py`;
- a small `tests/app/test_main_coordinate_system_adapter.py` for Qt wiring.

Extract shared immutable builders to
`tests/coordinates/coordinator_support.py`. Leave only unrelated Design
navigation tests in `test_main_design_navigation.py`; do not preserve private
Main state fixtures.

- [ ] **Step 6: Verify, measure, review, commit**

```powershell
C:\Users\Public\code\probe_station_gui\.venv\Scripts\python.exe -m pytest tests\coordinates tests\ui\test_stage_position_panel.py tests\ui\test_main_window_connection_flow.py tests\app\test_main_coordinate_system_adapter.py tests\app\test_main_stage_coordinate_controls.py tests\app\test_main_software_coordinate_pivot.py tests\app\test_main_design_navigation.py tests\app\test_main_route_measurement_session.py tests\route -q --basetemp $env:TEMP\coordinate-coordinator-task3-green
```

Run Ruff, compileall, diff-check, per-file Wily/Radon, and independent review.

```powershell
git add probe_station_gui/coordinates/coordinator.py probe_station_gui/coordinates/coordinator_model.py probe_station_gui/coordinates/coordinator_selection.py probe_station_gui/coordinates/presentation.py probe_station_gui/coordinates/software_frames.py probe_station_gui/views/main_window_stage_position_panel.py probe_station_gui/views/main_window_connection_flow.py probe_station_gui/views/main_window_homing.py probe_station_gui/stage/position_update.py probe_station_gui/settings/software_coordinate_selection_store.py main.py tests/coordinates tests/ui/test_stage_position_panel.py tests/ui/test_main_window_connection_flow.py tests/ui/test_main_window_homing.py tests/stage/test_position_update.py tests/settings/test_software_coordinate_selection_store.py tests/app/test_main_coordinate_system_adapter.py tests/app/test_main_stage_coordinate_controls.py tests/app/test_main_software_coordinate_pivot.py tests/app/test_main_design_navigation.py
git commit -m "refactor: complete coordinate system coordinator"
```

---

### Task 4: Enforce the selected-cluster MI gate and run acceptance

**Files:**
- Modify only residual Design/coordinate adapter or test files identified by
  the metric gate.
- Create: `.superpowers/sdd/2026-08-10-coordinate-system-coordinator/final-report.md`

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
