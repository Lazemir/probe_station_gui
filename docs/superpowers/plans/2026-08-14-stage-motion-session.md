# Stage Motion Session Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use
> superpowers:subagent-driven-development to implement this plan task-by-task
> with an independent review after every task. Steps use checkbox (`- [ ]`)
> syntax for tracking.

**Goal:** Replace distributed Main-shaped Stage UI motion state with one
GUI-thread-confined `_StageMotionSession` while preserving Stage safety,
Coordinate System behavior, and Route Measurement safety semantics.

**Architecture:** Fix the rejected absolute-move return bug first. Then add a
concrete `QObject` session that owns transient motion state and timers, invokes
only public asynchronous `StageController` methods, and emits typed Qt
presentation/completion signals. Migrate one behavior slice at a time and
delete `stage/move_lifecycle.py`, `stage/position_update.py`, and the motion
mixin only after every real caller uses the session directly.

**Tech Stack:** Python 3.9+, PySide6, frozen dataclasses, existing Stage motion
models, pytest, Import Linter, Radon, Ruff, Lizard

## Global Constraints

- Work from exact clean baseline
  `5ca7da6738ad5e5b711d433d42fdab236720ed44` in the existing linked worktree.
- Use `C:\Users\Public\code\probe_station_gui\.venv\Scripts\python.exe`
  for every Python command.
- Do not create a nested worktree.
- Do not run hardware, network, a visible GUI, push, or merge.
- Set `QT_QPA_PLATFORM=offscreen` for GUI tests and preserve established
  process-local `QLocale.c()` isolation where required.
- Keep startup lightweight; do not eagerly construct optional panels, workers,
  web engines, hardware clients, or network clients.
- Keep widgets and session state on the GUI thread. Do not add blocking serial,
  hardware, filesystem, camera, or large processing work to the GUI thread.
- Every move continues through public StageController request/queue/cancel
  methods and the existing `_move_safety_check()` path.
- Preserve Needles Known Raised behavior, homing spinners/queue, keyboard jog
  stop/resend, coordinate confidence, and terminal invalidation.
- Preserve Route Pause Request, Pause Ack, pending-Pause Interrupt path,
  persistent Interrupt, autofocus safe-Z restoration, and downstream
  photo/contact/external-measurement suppression exactly.
- Do not modify Route mailbox/coordinator or route safety semantics as part of
  this refactor.
- Do not add a Main forwarding facade, compatibility fields/properties, a
  callback mega-port, generic `dispatch`, aliases, re-exports, `__getattr__`,
  localhost self-calls, or message-text operation identity.
- Migrated state has one owner immediately; never retain Main and session copies.
- Every changed Python file must have positive normal and AST
  comments/docstrings-stripped Radon MI. Production target is stripped MI >= 8
  for the new session; direct test-file target is stripped MI >= 5.
- Import Linter runs with `--no-cache`; no task basetemp or Import Linter cache
  remains after a task.
- Each task ends with an independent review. Fix every Critical or Important
  finding and obtain a clean re-review before the next task.

## Canonical File Map

**Create:**

- `probe_station_gui/application/stage_motion_session.py` — sole transient
  Stage UI motion owner and typed Qt interface.
- `tests/app/test_stage_motion_session_planned_position.py` — planned motion,
  position reports, pose publication, reconciliation, and correlation.
- `tests/app/test_stage_motion_session_coordinate.py` — coordinate-target and
  feedrate lifecycle.
- `tests/app/test_stage_motion_session_jog_step.py` — manual prediction and
  exact-step lifecycle.
- `tests/app/test_stage_motion_session_cancel_reset.py` — cancellation,
  action-state, and disconnect reset.
- `tests/app/test_stage_motion_session_architecture.py` — ownership, deletion,
  MRO, dependency, and no-compatibility contract.

**Modify by the task that owns the behavior:**

- `probe_station_gui/stage/click_move.py`
- `probe_station_gui/stage/connection_state.py`
- `probe_station_gui/stage/controller.py`
- `probe_station_gui/application/alignment.py`
- `probe_station_gui/application/api_stage_contact.py`
- `probe_station_gui/application/camera_pipeline.py`
- `probe_station_gui/application/manual_jog.py`
- `probe_station_gui/application/registration_focus.py`
- `probe_station_gui/application/scan_sample_meter.py`
- `probe_station_gui/application/stage_design_position.py`
- `probe_station_gui/application/status_coordinate_ui.py`
- `probe_station_gui/application/settings_apply.py`
- `probe_station_gui/views/main_window_connection_flow.py`
- `probe_station_gui/views/main_window_coordinate_entry.py`
- `probe_station_gui/views/main_window_coordinate_step.py`
- `probe_station_gui/views/main_window_homing.py`
- `probe_station_gui/views/main_window_stage_position_panel.py`
- `main.py`
- `pyproject.toml`
- `tests/app/test_repository_maintainability.py`
- only existing tests that directly construct or inspect migrated fields.

**Delete in Task 6 after the deletion RED is green:**

- `probe_station_gui/stage/move_lifecycle.py`
- `probe_station_gui/stage/position_update.py`
- `probe_station_gui/application/motion_prediction.py`
- superseded owner-shaped tests after equivalent session-interface coverage
  exists.

---

## Task 1: Report Rejected Absolute XY Starts Correctly

**Files:**

- Modify: `probe_station_gui/stage/click_move.py`
- Modify: `tests/stage/test_controller_click_move.py`
- Modify: `tests/app/test_main_design_navigation.py`
- Modify: `tests/app/test_main_design_markup_navigation.py`

**Interfaces:**

- `StageController.request_move_to_xy(x_mm: float, y_mm: float) -> bool`
- No session or architecture migration in this task.

- [ ] **Step 1: Record the focused baseline**

Run:

```powershell
$env:QT_QPA_PLATFORM='offscreen'
$projectPython='C:\Users\Public\code\probe_station_gui\.venv\Scripts\python.exe'
& $projectPython -m pytest tests/stage/test_controller_click_move.py tests/app/test_main_design_markup_navigation.py -q -p no:cacheprovider --basetemp=.pytest-stage-motion-task1-baseline
```

Expected: current focused tests pass; record the exact count.

- [ ] **Step 2: Write the controller-return RED**

Add a test to `test_controller_click_move.py` that constructs a controller,
replaces `_start_background_task` with a recorder returning `False`, calls
`request_move_to_xy(4.0, 5.0)`, and asserts:

```python
assert accepted is False
assert submitted_target == controller._run_move_to_xy
assert submitted_args == (4.0, 5.0)
```

Shut down the controller in `finally`. Run only the test.

Expected RED: result is `None`, not `False`.

- [ ] **Step 3: Write the ordinary Design-call RED**

Change `_FakeStageController.request_move_to_xy()` in
`test_main_design_navigation.py` to return a configurable `move_accepted`
Boolean, defaulting to `True`. Add a regression beside
`test_move_to_design_coordinate_reports_busy_race_rejection` that sets
`move_accepted=False`, invokes `_move_to_design_coordinate()` without the
`move_request` override, and asserts:

```python
assert accepted is False
assert window._pending_planned_move_target_xy is None
assert window._pending_planned_move_source_label is None
```

Expected RED: the production controller contract returns `None` and the move is
reported accepted.

- [ ] **Step 4: Implement the minimal correctness fix**

Change only `request_move_to_xy`:

```python
def request_move_to_xy(self, target_x_mm: float, target_y_mm: float) -> bool:
    return self._start_background_task(
        target=self._run_move_to_xy,
        args=(float(target_x_mm), float(target_y_mm)),
        busy_message="Stage is busy. Ignoring absolute move request.",
    )
```

Do not change worker execution, signals, safety, or status text.

- [ ] **Step 5: Run focused GREEN and static gates**

Run the two new tests, both complete affected files, configured Ruff on four
paths, format check, compile, diff check, and normal/stripped MI. Expected:
all pass; every changed file has MI > 0.

- [ ] **Step 6: Independent review and commit**

Review specifically for unchanged `_start_background_task` ordering and no new
controller side effect. Remove `.pytest-stage-motion-task1-baseline` and task
basetemps. Commit:

```text
fix: report rejected absolute stage moves
```

---

## Task 2: Add the Pose Accessor and Minimal Planned/Position Session

**Files:**

- Create: `probe_station_gui/application/stage_motion_session.py`
- Create: `tests/app/test_stage_motion_session_planned_position.py`
- Modify: `probe_station_gui/stage/connection_state.py`
- Modify: `tests/stage/test_position_update.py`
- Modify: `main.py`
- Modify: `probe_station_gui/application/camera_pipeline.py`
- Modify: `probe_station_gui/application/registration_focus.py`
- Modify: `probe_station_gui/application/stage_design_position.py`
- Modify: `tests/app/test_main_planned_move_prediction.py`

**Interfaces produced:**

```python
StageController.latest_physical_machine_pose(axes) -> PhysicalMachinePose

_StageMotionSession.snapshot() -> StageMotionSnapshot
_StageMotionSession.request_planned_xy_move(request) -> bool
_StageMotionSession.on_absolute_xy_move_started(x, y, feedrate) -> None
_StageMotionSession.on_stage_position_changed(position) -> None
_StageMotionSession.on_movement_finished(success, message) -> None
_StageMotionSession.tick() -> None
```

Signals delivered in this task: `presentation_changed`,
`action_state_changed`, `click_move_finished`, and `status_requested`.

- [ ] **Step 1: Record focused behavior and metric baselines**

Run current position-update, planned-prediction, Design-navigation, homing, and
connection-flow tests offscreen. Record exact counts plus Radon MI/CC for
`position_update.py`, `motion_prediction.py`, affected tests, and `main.py`.

- [ ] **Step 2: Write public-pose-accessor RED tests**

Move the behavior assertions, not implementation coupling, from
`test_position_update.py` into controller-facing tests:

- same-generation synchronized Machine position maps universal curves once;
- one out-of-domain calibrated axis is omitted while unaffected axes remain;
- missing synchronized position returns an empty `PhysicalMachinePose`;
- no serial query occurs.

Expected RED: the public method is absent.

- [ ] **Step 3: Implement the public cached accessor**

Implement in the Stage connection-state owner using the current synchronized
position and mapper. Normalize requested axes, use `AXIS_INDEX` rather than
enumeration, omit invalid/non-finite/out-of-domain values independently, and
return `PhysicalMachinePose.from_mapping(values)`. Do not expose the mapper.

Run accessor tests GREEN before session code.

- [ ] **Step 4: Write the absent-session RED**

Add direct tests requiring the canonical module/class and frozen config,
snapshot, request, presentation, and action-state values. Specify:

- idle snapshot;
- rejected planned request does not arm pending state;
- accepted request arms pending target/source only;
- matching actual-start signal begins interpolation;
- mismatched actual-start clears the arm;
- success waits at target until a newer status timestamp;
- failure clears planned prediction;
- unrelated completion does not consume an unowned purpose;
- unhomed fallback and valid-position publication ordering;
- partial physical Machine pose and B tracking;
- all mutation occurs on the object's Qt thread.

Expected RED: module absent.

- [ ] **Step 5: Implement only planned/position ownership**

Create `_StageMotionSession(QObject)` with frozen config/snapshot/request and
typed presentation/action-state dataclasses. Compose the existing
`ManualJogPredictionState`, `CoordinateTargetMoveState`, and exact-step objects
but do not migrate their workflows yet. Implement planned XY and position
report behavior using existing motion math and position-presenter functions.

The session accepts the concrete controller and Qt parent. It stores no Main,
view, panel, Design session, Coordinate System coordinator, or callback bag.

- [ ] **Step 6: Wire the minimal vertical path**

Construct the session after StageController. Connect controller position,
absolute-start, and movement-finished signals directly to session slots. Move
planned request calls in registration/design navigation to
`request_planned_xy_move`. Connect typed presentation back to existing Stage,
Coordinate System authority, and Design rendering adapters without adding Main
forwarders.

Leave coordinate/manual/exact/cancel state temporarily outside; do not create
duplicate planned or position state.

- [ ] **Step 7: GREEN, static/MI, review, commit**

Run direct session tests plus affected Stage/App/UI tests. Run Ruff, format,
compile, diff check, no-reverse-import smoke, and normal/stripped MI. Verify no
hardware/network/visible GUI. Independent review must inspect correlation,
same-generation pose, queued Qt delivery, and no dual ownership. Commit:

```text
refactor: add stage motion session
```

---

## Task 3: Migrate Coordinate-Target Lifecycle

**Files:**

- Modify: `probe_station_gui/application/stage_motion_session.py`
- Create: `tests/app/test_stage_motion_session_coordinate.py`
- Modify: `probe_station_gui/application/stage_design_position.py`
- Modify: `probe_station_gui/application/api_stage_contact.py`
- Modify: `probe_station_gui/views/main_window_coordinate_entry.py`
- Modify: `probe_station_gui/views/main_window_stage_position_panel.py`
- Modify: `probe_station_gui/views/main_window_homing.py`
- Modify: `main.py`
- Modify/migrate: `tests/stage/test_move_lifecycle.py`
- Modify/migrate: `tests/app/test_main_stage_coordinate_controls.py`
- Modify: `tests/app/test_main_coordinate_feedrate.py`

**Interfaces produced:**

```python
start_coordinate_move(CoordinateMoveRequest) -> bool
set_coordinate_feedrate(feedrate_mm_min: float) -> None
coordinate_move_finished(CoordinateMoveCompletion)
continue_homing_requested()
```

- [ ] **Step 1: Record coordinate behavior baseline**

Run coordinate target, motion planning/execution, Stage coordinate controls,
coordinate feedrate, API stage command, homing, and current move-lifecycle
tests. Record exact count and current ordering assertions.

- [ ] **Step 2: Write direct session RED cases**

Specify through the session interface:

- start validation/status and controller rejection rollback;
- programmed/effective feedrate and mixed-axis common bounds;
- position publication immediately after accepted start;
- Idle before active-state observation does not finish;
- Idle after activity/tolerance acceptance emits typed completion;
- failed/skipped/already/unchanged disposition clears tracking as before;
- feedrate reissue success, rejection, and expected cancellation retention;
- completion preserves immutable display targets and basis;
- pending homing emits continuation only after coordinate completion;
- no StageController safety or request method is bypassed.

Expected RED: coordinate state still belongs to Main/free helpers.

- [ ] **Step 3: Implement coordinate workflow in the session**

Move the current target planning, prediction advance, finish-if-idle,
feedrate-reissue, clear-tracking, override reset, and completion correlation
behind the new methods. Store the accepted coordinate purpose before queued
completion can arrive. Do not interpret another purpose as coordinate
completion.

- [ ] **Step 4: Redirect all coordinate callers atomically**

Coordinate entry, API GUI bridge, feedrate changes, position reports, homing,
and panel action-state consume session methods/snapshot/signals directly.
Delete migrated Main fields and methods in this task. Preserve Coordinate Motion
lease projection outside the session and pass the resolved request/basis in.

Do not leave wrappers with the old method names solely for compatibility.

- [ ] **Step 5: Replace tests at the interface**

Move pure lifecycle assertions from `test_move_lifecycle.py` and private-field
assertions from `test_main_stage_coordinate_controls.py` into the direct session
test. Retain only integration tests that prove view/API/homing wiring. Fixtures
construct a real session, not deleted Main fields.

- [ ] **Step 6: GREEN, static/MI, review, commit**

Run the complete coordinate-focused selection, controller safety tests, Import
Linter, Ruff, format, compile, diff, and normal/stripped MI. Review must verify
accepted-start ordering, feedrate-reissue cancellation, idle finish, display
basis, and no compatibility surface. Commit:

```text
refactor: centralize coordinate move lifecycle
```

---

## Task 4: Migrate Manual Jog Prediction and Exact Steps

**Files:**

- Modify: `probe_station_gui/application/stage_motion_session.py`
- Create: `tests/app/test_stage_motion_session_jog_step.py`
- Modify: `probe_station_gui/application/manual_jog.py`
- Modify: `probe_station_gui/views/main_window_coordinate_step.py`
- Modify: `probe_station_gui/views/main_window_homing.py`
- Modify: `probe_station_gui/views/main_window_connection_flow.py`
- Modify: `main.py`
- Modify/migrate: `tests/stage/test_manual_jog_prediction.py`
- Modify/migrate: `tests/stage/test_exact_step.py`
- Modify/migrate: `tests/app/test_main_stage_coordinate_controls.py`
- Modify: relevant joystick/homing/connection UI tests.

**Interfaces produced:**

```python
on_manual_jog_command(commanded_distances, feedrate_mm_min) -> None
on_manual_jog_stopped() -> None
queue_exact_step(ExactStepRequest) -> ExactStepOutcome
dispatch_exact_steps() -> bool
```

- [ ] **Step 1: Record jog/step behavior baseline**

Run manual-jog prediction, exact-step, controller jog, joystick cached-state,
homing, connection, coordinate feedrate, and related Main controls. Record exact
counts, timer/status-refresh ordering, and terminal live-poll behavior.

- [ ] **Step 2: Write direct RED cases**

Specify:

- manual command seeds from coordinate, latest, or current presented position;
- zero command stops timer, clears motion axes, and schedules settle refreshes;
- manual jog clears active coordinate tracking but not unrelated state;
- stop-tail prediction, ignored fresh Idle sample, stale Idle reconciliation,
  smoothing, and learned tail;
- terminal live polling pauses/resumes at existing times;
- exact steps accumulate during one fixed window;
- same-basis follow-up rebases from active target;
- opposite steps returning to reached target send no follow-up;
- multiple axes coalesce into one request;
- frozen Coordinate Motion lease and pose-rebase rules remain exact;
- failed move, mode change, homing, terminal command, and disconnect clear
  deferred exact steps.

Expected RED: behavior still crosses Main state and view free helpers.

- [ ] **Step 3: Implement manual prediction and exact-step ownership**

Move the timer and all manual/exact transient fields into the session. Reuse
existing state models; do not copy their algorithms. Session signals carry
presentation/action-state/status-refresh intent. Existing terminal and panel
adapters perform only their widget updates.

- [ ] **Step 4: Redirect real signal/caller paths**

Connect joystick command/stop and the session timer directly. Coordinate-step
projection remains in the Coordinate System adapter, which submits a resolved
`ExactStepRequest`. Homing, mode change, terminal command, and disconnect call
semantic session reset/clear operations, not Main wrappers.

Delete migrated Main fields, timers, and prediction/exact-step methods.

- [ ] **Step 5: GREEN, static/MI, review, commit**

Run direct and full affected Stage/App/UI selections, including keyboard jog
stop/resend. Run Import Linter, Ruff, format, compile, diff, normal/stripped MI,
and duplicate comparison. Review timer ownership, terminal polling, lease
identity, follow-up ordering, and GUI-thread confinement. Commit:

```text
refactor: centralize jog prediction and exact steps
```

---

## Task 5: Migrate Stage Cancellation, Action State, and Reset

**Files:**

- Modify: `probe_station_gui/application/stage_motion_session.py`
- Create: `tests/app/test_stage_motion_session_cancel_reset.py`
- Modify: `probe_station_gui/application/status_coordinate_ui.py`
- Modify: `probe_station_gui/application/scan_sample_meter.py`
- Modify: `probe_station_gui/views/main_window_connection_flow.py`
- Modify: `probe_station_gui/views/main_window_homing.py`
- Modify: `probe_station_gui/views/main_window_stage_position_panel.py`
- Modify: `main.py`
- Modify/migrate: `tests/stage/test_move_lifecycle.py`
- Modify: `tests/app/test_main_stage_coordinate_controls.py`
- Modify: `tests/ui/test_main_window_connection_flow.py`
- Modify: `tests/ui/test_main_window_homing.py`
- Keep route safety tests semantically unchanged.

**Interfaces produced:**

```python
cancel_stage_motion() -> StageMotionCancelOutcome
reset(reason: StageMotionResetReason) -> None
action_state_changed(StageMotionActionState)
```

- [ ] **Step 1: Record cancellation and route-safety baseline**

Run move lifecycle, Stage coordinate Cancel, connection, homing, route control,
route Interrupt safety, route session, and route point interrupt-checkpoint
tests. Hash the protected Route mailbox/coordinator and safety files that this
task must not modify.

- [ ] **Step 2: Write session cancellation/reset RED**

Specify:

- coordinate cancellation calls `cancel_active_motion`, clears coordinate and
  pending edit state, resets feed override, and wins over generic busy task;
- reported active motion cancels active motion and clears planned state;
- controller busy without active motion cancels the active task;
- stale Run/Jog status is not cancelable;
- reset clears every owned timer, purpose, prediction, coordinate, exact-step,
  pending edit, pose, B, and motion-axis value;
- action state combines pending edit availability with session cancelability;
- no route, scan, surface-map, click, sample, or homing state appears in the
  session snapshot.

Expected RED: global Stage cancellation still lives in `move_lifecycle.py`.

- [ ] **Step 3: Implement only the Stage cancellation slice**

Move controller/coordinate cancellation and reset into the session. Return a
typed outcome describing whether Stage motion was canceled and whether the
coordinate-priority early-return applies. Do not import or call Route, scan,
surface-map, click, sample, homing, views, or dialogs.

- [ ] **Step 4: Preserve global Cancel ordering in the shell**

Keep a narrow application/view adapter for global Cancel that performs the
existing order:

1. pending click/alignment/homing UI intents;
2. Route runner stop and route status;
3. surface-map and microscope-scan stop;
4. `session.cancel_stage_motion()`;
5. pending edit-only focus/status presentation.

This adapter owns application composition only and must not recreate session
state or expose an owner protocol. Route Pause/Interrupt code is not changed.

- [ ] **Step 5: Redirect disconnect/status action state**

Connection detach calls `session.reset(DISCONNECT)` before detached rendering.
Panel action buttons consume `StageMotionActionState` plus the shell's
application-level cancelability. Remove Main controller-state-age and migrated
cancel fields/methods when their last caller moves.

- [ ] **Step 6: GREEN, protected-hash/static/MI, review, commit**

Run direct cancellation, all affected Stage/App/UI tests, and full protected
Route safety selection. Verify protected hashes, Import Linter, Ruff, format,
compile, diff, normal/stripped MI, and no Route semantic diff. Review global
ordering, coordinate priority, stale state, complete reset, and route
non-ownership. Commit:

```text
refactor: centralize stage motion cancellation
```

---

## Task 6: Migrate Registration/Design Adapters and Delete Shallow Owners

**Files:**

- Modify: `probe_station_gui/application/stage_motion_session.py`
- Create: `tests/app/test_stage_motion_session_architecture.py`
- Modify: `probe_station_gui/application/alignment.py`
- Modify: `probe_station_gui/application/camera_pipeline.py`
- Modify: `probe_station_gui/application/manual_jog.py`
- Modify: `probe_station_gui/application/registration_focus.py`
- Modify: `probe_station_gui/application/stage_design_position.py`
- Modify: `probe_station_gui/application/status_coordinate_ui.py`
- Modify: `probe_station_gui/application/settings_apply.py`
- Modify: `probe_station_gui/views/main_window_coordinate_flow.py`
- Modify: `probe_station_gui/views/main_window_design_workspace.py`
- Modify: `probe_station_gui/views/main_window_homing.py`
- Modify: `main.py`
- Modify: `pyproject.toml`
- Modify: `tests/app/test_repository_maintainability.py`
- Modify: `tests/app/test_main_domain_ownership.py`
- Modify only registration/design/motion tests that inspect removed ownership.
- Delete: `probe_station_gui/stage/move_lifecycle.py`
- Delete: `probe_station_gui/stage/position_update.py`
- Delete: `probe_station_gui/application/motion_prediction.py`
- Delete or shrink superseded owner-shaped tests only after equivalent direct
  session tests are present.

**Interfaces finalized:**

- `alignment_rotation_finished(AlignmentRotationCompletion)`
- `click_move_finished(bool)`
- exact `StageMotionSnapshot` and all methods/signals from Tasks 2-5.

- [ ] **Step 1: Write the strict deletion/architecture RED**

The architecture test must initially fail and then assert:

- Main composes exactly one `_stage_motion` session;
- every named migrated raw field is absent from `Main.__init__` and production;
- the three old modules are absent;
- `_MainMotionPredictionMixin` is absent from the exact direct-base order;
- no compatibility property, wrapper, alias, re-export, owner protocol,
  `owner=self` call, or dynamic facade recreates the deleted surface;
- all real consumers use the session interface directly;
- session imports no forbidden caller, view, route, camera, or dialog module;
- no operation identity is derived from status-message substrings outside the
  session's typed completion normalization;
- the sixth Import Linter contract is exact and has no weakening.

Record the absent/deletion RED before deleting production files.

- [ ] **Step 2: Finish alignment and click correlation**

Alignment starts bind an opaque correlation only after the controller accepts
the B rotation or emits its actual start. Completion emits the typed value to
the existing registration adapter, which applies coordinator transition,
draft/snap/panel/position/collapse/status behavior in the current order.

Token-bound focus move remains coordinator-intent-bound and is never inferred
from the global movement signal. Click interaction consumes only
`click_move_finished`.

- [ ] **Step 3: Reduce application owners without wrappers**

Move unrelated ruler/calibration/autofocus/status handlers out of
`motion_prediction.py` to the existing cohesive UI/objective owner. Remove
migrated methods from manual jog, Stage/Design position, status/coordinate, and
registration owners. Update the canonical MRO/descriptor ownership map to the
actual remaining owners.

- [ ] **Step 4: Delete old modules and migrate remaining tests**

Delete `move_lifecycle.py`, `position_update.py`, and `motion_prediction.py`.
Delete old direct owner tests whose behavior is now covered through the session
interface. Keep integration assertions for registration transition ordering,
Design rendering, API movement, homing, connection, and global Cancel.

- [ ] **Step 5: Add the sixth Import Linter contract**

Add exactly:

```toml
[[tool.importlinter.contracts]]
id = "stage-motion-session-leaf"
name = "Stage motion session stays independent of callers and unrelated workflows"
type = "forbidden"
source_modules = ["probe_station_gui.application.stage_motion_session"]
forbidden_modules = [
    "main",
    "probe_station_gui.camera",
    "probe_station_gui.dialogs",
    "probe_station_gui.route",
    "probe_station_gui.views",
    "probe_station_gui.application.alignment",
    "probe_station_gui.application.api_stage_contact",
    "probe_station_gui.application.camera_pipeline",
    "probe_station_gui.application.manual_jog",
    "probe_station_gui.application.registration_focus",
    "probe_station_gui.application.scan_sample_meter",
    "probe_station_gui.application.stage_design_position",
    "probe_station_gui.application.status_coordinate_ui",
]
```

Update the permanent test's exact summary from five to six kept contracts.
Inject one disposable forbidden import, verify this contract breaks with an
actionable chain, remove it, and verify `6 kept, 0 broken` with `--no-cache`.

- [ ] **Step 6: Focused GREEN and quality gates**

Run all direct session tests, affected Stage/App/UI/Coordinates/Design tests,
complete Route safety selection, six Import Linter contracts, Ruff, format,
compileall, diff check, owner-first/reverse-import smoke, tracked-plus-new
normal MI, changed-file stripped MI, and Lizard warnings/duplicates.

Expected: deletion/architecture test GREEN; no changed file MI <= 0; session
stripped MI >= 8 unless an independent review accepts a documented non-padding
reason; direct test files stripped MI >= 5; no unexplained CC/duplicate
regression.

- [ ] **Step 7: Independent review and commit**

Review deletion test, state exclusivity, correlation, Qt threading,
StageController safety, registration/focus ordering, global Cancel, protected
Route invariants, and Import Linter precision. Fix Critical/Important findings
and re-review. Commit:

```text
refactor: complete stage motion session migration
```

---

## Task 7: Deterministic Final Verification, Review, and Evidence

**Files:**

- Modify: `docs/superpowers/plans/2026-08-14-stage-motion-session.md` with
  factual evidence only.
- Create ignored: `.superpowers/sdd/stage-motion-session-final-report.md`

- [ ] **Step 1: Freeze scope and collect the complete suite**

Record HEAD, clean status, implementation diff paths, hashes of protected Route
safety files, and `pytest --collect-only` node/subtest totals. No production
file may change after the freeze except a review fix that restarts this task.

- [ ] **Step 2: Run the deterministic hardware-free matrix**

Use established offscreen, no-cache, process-local-locale partitions. Run all
collected nodes exactly once, isolating only a baseline-proven native-crash node
if necessary and accounting for it separately. Include Stage, App,
Coordinates, Design, Route, Settings, API, and UI directories plus subtests.

Do not start hardware, network, API server, camera thread, or a visible GUI.

- [ ] **Step 3: Run final architecture and quality gates**

Run:

- all six Import Linter contracts with `--no-cache`;
- permanent maintainability tests;
- architecture/deletion/MRO tests;
- configured whole Ruff and changed-file format check;
- whole compileall and diff check;
- owner-first and reverse-import smoke without QApplication or Python thread
  start;
- tracked normal and changed-file stripped MI;
- Radon raw/CC/MI and Lizard warning/duplicate comparison;
- protected Route file hash audit;
- task temp/cache/status/index audit.

- [ ] **Step 4: Independent final review**

The reviewer independently inspects:

- session depth and deletion test;
- no raw-field compatibility surface;
- accepted-start and completion correlation;
- partial physical Machine pose behavior;
- planned/manual/coordinate/exact prediction ordering;
- Qt queued delivery and no lock-held signals;
- StageController safety and serial non-ownership;
- global Cancel and disconnect reset ordering;
- registration/focus boundaries;
- Route Pause/Ack/Interrupt and safe-Z invariants;
- sixth Import Linter contract;
- MI/CC/duplicate evidence.

Require READY with Critical 0 / Important 0 before final evidence.

- [ ] **Step 5: Record factual evidence**

Update this plan with exact commit IDs, counts, metrics, hashes, review verdict,
and any baseline-proven isolated node. Write the ignored report and record its
SHA-256. Remove exact task basetemps and caches; do not delete unrelated caches.

- [ ] **Step 6: Commit the factual handoff**

Commit only the tracked plan update:

```text
docs: record stage motion session evidence
```

Verify tracked worktree and index are clean. Hand off without push or merge.

## Execution Evidence

Executed from clean design baseline
`5ca7da6738ad5e5b711d433d42fdab236720ed44`.

- Task commits:
  - `003f9a2fbe8f268afab9f1185eaff7286554319c`
  - `b14cbbbceb19fab21d6daac189e1e28794b0173e`
  - `85e4edeab854430ae4bd792da68b7f15f57c9bd1`
  - `5dc376bedb44363a8852a6d28ddffb3a268c8e03`
  - `ff9c5f9ced194797a16f91439488ad539f83f389`
  - `04641c551c078ddc7884c84dd655887871f94df2`
- The formatter review fix is
  `06e5fd7c71f5760d97a4534a8037e563241cf6ef`; this is the frozen
  implementation HEAD used for final verification.
- Final scope: 98 files changed, 11,668 insertions, 4,994 deletions.
- Collection found 3,531 tests. The final offscreen hardware-free run passed
  all 3,531 tests plus 16 subtests in 109.25 seconds.
- Import Linter analyzed 419 files and 2,656 dependencies:
  `6 kept, 0 broken`.
- Configured whole-tree Ruff, whole-tree `compileall`, diff/status/index,
  owner-first import, and reverse-order import gates passed. All 27 added
  Python files are formatted.
- Canonical Git-blob formatting debt improved from 20 baseline files to 2;
  no new formatter debt remains. The retained baseline files are
  `probe_station_gui/stage/motion_commands.py` and
  `tests/stage/test_precision_motion.py`.
- A supplemental strict Ruff run found 22 inherited F401 findings in 9 files;
  all 9 files are outside this implementation diff.
- All 730 tracked Python files have positive normal MI. All 91 changed Python
  files have positive normal and AST comments/docstrings-stripped MI.
  `stage_motion_session.py` is `1.996925 / 0.008137`; independent review
  accepted the honest positive value and rejected metric-only fragmentation.
- Three changed legacy test/support files remain below the soft stripped-MI
  target 5, but each was already below 5 at baseline and remains above the
  hard `>0` gate. Independent review accepted this evidence.
- Changed-production Lizard warnings stayed `16 -> 16`, maximum CC stayed
  `38 -> 38`, duplicate blocks stayed `1 -> 1`, and duplicate rate improved
  from `0.158212%` to `0.146123%`.
- Protected Route SHA-256 values remained exact:

  ```text
  1f20b1bd6f3d19f91f398ef43506e729187f20a3452711a319e4e5837254b668  probe_station_gui/route/run_control_mailbox.py
  796aa4876cc4d2c2cf62675a636e5b28dd40de1e6443ac2dd5cae46896ebaf55  probe_station_gui/route/run_coordinator.py
  c02f96b3d4c4f62038e43a3f705467f58e37b698ef0ae83c6863daabf76e8b68  probe_station_gui/route/external_session.py
  f7cb8d7dbc730cf6d893c4ae42f8c24b75db37b808210a7de2b0e15482e17f25  probe_station_gui/route/measurement.py
  e7c1efebb5d03631142114b3f125c08ee4087a10e693b0e4058bfb5187d6cde2  probe_station_gui/route/point_execution.py
  42044f6c37cef7f0fffd0918eab7a84ffc7fc0a81634acd44cd65942256f790c  probe_station_gui/route/contact_lifecycle.py
  751fa2eaa84b03094a388327d4246d137cdeaabb184bfb87dc209be595799e00  tests/route/test_run_control_mailbox.py
  af882b0e3fa40328dd8a16c1ab074ac79e83c16d8d0c3c5bc5cdb87affd4d4b8  tests/route/test_measurement_interrupt_checkpoint.py
  b26e3571e53679339c8caf7a866ccfa0d80da8e196eb93dd02f75a8d90af7bfc  tests/route/test_measurement_api_route_control.py
  ccd95aad1d7fcc07ad3b7202a5df2862c4f0b0d6bdce85cd7e06d7ff1591a91b  tests/route/test_point_execution.py
  08a047a31c1581ec7e332db5826b72061cf7fd17fe209c8d093f9886b9fc0b8c  tests/route/test_contact_lifecycle_interface.py
  ```

- Final independent review on `06e5fd7`: READY, Critical 0, Important 0,
  Minor 0.
- Ignored factual report:
  `.superpowers/sdd/stage-motion-session-final-report.md`, SHA-256
  `f8bf461a26bde842266788f6111371b3085ce30cd1f66e0f5198c250d6565d5b`.
- No task basetemp or Import Linter cache remained. No hardware, network,
  API server, camera thread, visible GUI, push, or merge was used.
