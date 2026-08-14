# Stage Motion Session Design

**Date:** 2026-08-14

## Purpose

Give the GUI one deep module for transient Stage motion interpretation: accepted
motion identity, coordinate-target lifecycle, manual and planned prediction,
position reconciliation, exact-step continuation, cancellation, and displayed
motion state. Replace the current Main-shaped orchestration spread across
`stage/move_lifecycle.py`, `stage/position_update.py`, and several application
mixins without changing external behavior.

This is an ownership refactor. It must not change Stage motion safety, serial
execution, homing, Needles Known Raised semantics, Coordinate System or
registration policy, Route Measurement Pause Request/Pause Ack/Interrupt,
autofocus safe-Z restoration, camera work, or settings behavior.

## Current Problem

The application has no real owner for Stage UI motion. `Main.__init__` owns raw
fields for coordinate targets, manual-jog prediction, planned XY prediction,
pending coordinate edits, exact-step accumulation, displayed Stage XY, cached
physical Machine pose, B tracking, and active motion presentation. Their
ordering is implemented by free functions that accept `owner=self` and by
methods inherited from broad Main mixins.

The two largest nominal Stage owners are shallow:

- `stage/move_lifecycle.py` is 473 lines and requires a 48-member
  `StageMoveLifecycleOwner` interface spanning controller, route, scan, Design,
  widgets, homing, alignment, prediction, and status behavior.
- `stage/position_update.py` is 505 lines and requires another Main-shaped
  protocol while importing application-facing view helpers from the Stage
  package.

Understanding one position report or move completion requires following state
through `status_coordinate_ui.py`, `stage_design_position.py`,
`motion_prediction.py`, `manual_jog.py`, `registration_focus.py`, Main signal
wiring, and multiple view helpers. Tests recreate synthetic Main objects and
assert private field mutations instead of using the same interface as callers.

## Correctness Prerequisite

`StageController.request_move_to_xy()` currently declares `-> None` and
discards the Boolean returned by `_start_background_task()`. The Design move
adapter nevertheless treats an exact `False` return as a busy-race rejection.
The real controller can therefore reject a move while the application reports
acceptance and retains a pending planned-move target that will never receive an
`absolute_xy_move_started` signal.

Fix this before the structural migration:

1. Make `request_move_to_xy() -> bool` return `_start_background_task(...)`.
2. Prove a rejected controller start returns `False`.
3. Prove the ordinary Design-navigation call, without an injected request
   callback, clears its pending target/source and returns `False`.

This correctness commit is independent and precedes creation of the new owner.

## New Deep Module

Add:

`probe_station_gui/application/stage_motion_session.py`

Its canonical class is `_StageMotionSession`, a `QObject` created and confined
to the GUI thread. It accepts the concrete `StageController`, a frozen
`StageMotionConfig`, and a Qt parent. It owns its timers and uses public cached
controller observations and public asynchronous request/queue/cancel methods.
It never performs serial I/O or blocks for controller results.

The module is not a mixin and does not inherit or receive `Main`. It has no
callback bag, generic command bus, `__getattr__`, facade, compatibility
properties, aliases, or package re-exports. Existing application owners call
the session directly; Main only constructs it and connects typed signals.

### Deletion test

Deleting the session must force all of the following policy back into several
callers:

- accepted-start and completion identity;
- planned, coordinate, and manual prediction priority;
- stale and fresh status reconciliation;
- coordinate idle-finish and feedrate-reissue behavior;
- exact-step accumulation and continuation;
- partial physical Machine pose publication and B tracking;
- Stage cancellation priority and disconnect reset;
- action availability and displayed motion axes.

That concentrated complexity is the module's depth. A class that merely
forwards existing Main methods or exposes all raw fields fails this test.

## Exact State Ownership

The session exclusively owns:

- `CoordinateTargetMoveState`;
- `ManualJogPredictionState`;
- planned XY pending target and source label;
- planned XY origin, displayed Stage XY, target, start/end timestamps,
  fresh-status wait, and stop-status timestamp;
- pending coordinate targets and their `CoordinateMotionLease`;
- `ExactStepAccumulator`, pending exact-step axes, frozen lease, pose-rebase
  permission, and accumulation-window state;
- current presented Stage position and current presented raw Stage XY;
- latest partial `PhysicalMachinePose`;
- last reported B position;
- active presented motion axes;
- the purpose and identity of an accepted motion whose completion may mutate
  session state.

The raw Main fields for migrated state are deleted in the same vertical task
that redirects their last caller. There is no transitional dual ownership.

## Explicit Non-Ownership

The session does not own or wrap:

- StageController serial sessions, worker threads, operation leases, motion
  safety, limits, homing, coordinate confidence, or Needles Known Raised;
- the homing queue or homing spinner presentation;
- Coordinate System records, registration lifecycle, frame persistence,
  Design projection, or Coordinate Motion lease policy;
- registration marks, alignment drafts, focus-search workers, autofocus
  policy, or focus-reference state;
- route runners, Route Control mailbox/coordinator, Pause Request, Pause Ack,
  Interrupt, confirmation, safe-Z restoration, or result persistence;
- click-interaction target state, surface-map capture, microscope scan,
  sample handling, camera acquisition, instrument work, widgets, status log,
  or settings persistence.

The registration workflow may give the session an opaque alignment-motion
correlation value. The session returns that value in a typed completion but
never applies registration policy itself.

## Public Interface

The interface is workflow-specific. There is no generic `dispatch(event)`.

```python
class _StageMotionSession(QObject):
    def snapshot(self) -> StageMotionSnapshot: ...

    def start_coordinate_move(
        self,
        request: CoordinateMoveRequest,
    ) -> bool: ...

    def set_coordinate_feedrate(self, feedrate_mm_min: float) -> None: ...

    def request_planned_xy_move(
        self,
        request: PlannedXYMoveRequest,
    ) -> bool: ...

    def on_manual_jog_command(
        self,
        commanded_distances: tuple[tuple[str, float], ...],
        feedrate_mm_min: float,
    ) -> None: ...

    def on_manual_jog_stopped(self) -> None: ...

    @Slot(object)
    def on_stage_position_changed(self, position: object) -> None: ...

    @Slot(float, float, float)
    def on_absolute_xy_move_started(
        self,
        target_x_mm: float,
        target_y_mm: float,
        feedrate_mm_min: float,
    ) -> None: ...

    @Slot(bool, str)
    def on_movement_finished(self, success: bool, message: str) -> None: ...

    def tick(self) -> None: ...
    def cancel_stage_motion(self) -> StageMotionCancelOutcome: ...
    def reset(self, reason: StageMotionResetReason) -> None: ...
```

Input values are frozen dataclasses. `CoordinateMoveRequest` contains resolved
raw/display targets, feedrate, source label, optional physical limit targets,
and optional display basis. `PlannedXYMoveRequest` contains a raw Stage target,
source label, and optional feedrate. Coordinate projection remains outside the
session and supplies a current immutable lease/basis.

`StageMotionSnapshot` is immutable and exposes only facts needed by callers:
presented position/XY, physical Machine pose, active axes, cancelability,
coordinate activity/display basis, pending edit facts, and prediction state.
It does not expose mutable collaborators.

## Typed Output Signals

The session emits:

- `presentation_changed(StageMotionPresentation)`;
- `action_state_changed(StageMotionActionState)`;
- `coordinate_move_finished(CoordinateMoveCompletion)`;
- `alignment_rotation_finished(AlignmentRotationCompletion)`;
- `click_move_finished(bool)`;
- `status_requested(str, int)`;
- `continue_homing_requested()`.

Presentation and completion values are frozen dataclasses. There is no generic
effect union that forces every consumer to understand every workflow. View
adapters render presentations; registration adapters consume only alignment
completion; homing consumes only continuation.

## Motion Correlation Semantics

The current global `movement_finished(bool, str)` signal is interpreted from
ambient pending fields. The session replaces that ambient correlation with one
accepted motion purpose.

1. A purpose is armed only after the corresponding controller request returns
   accepted, or after the controller publishes its actual start signal.
2. Planned XY prediction is first pending, then becomes active only when
   `absolute_xy_move_started` matches the pending target within the existing
   tolerance. A mismatch clears the pending arm without starting prediction.
3. Coordinate, click, alignment-rotation, and ordinary task completions are
   distinct purposes. One completion may consume only its current purpose.
4. Token-bound focus moves continue to use their exact coordinator intent ID
   and typed completion; they are never inferred from the global signal.
5. Feedrate-reissue cancellation is an explicit coordinate substate. Its
   expected cancellation retains coordinate tracking and suppresses the normal
   failure/status path.
6. A stale or unrelated completion cannot clear planned prediction,
   coordinate targets, alignment preparation, or exact-step continuation.
7. Message text remains presentation, not operation identity. Existing
   `skipped`/`already`/`unchanged` behavior is preserved during migration, then
   represented by typed completion disposition inside the session.

## Position and Prediction Ordering

The session preserves the established precedence:

1. active coordinate-move prediction;
2. active manual-jog prediction;
3. active planned XY prediction;
4. manual-jog fresh-status wait;
5. planned XY fresh-status wait;
6. latest homed controller position;
7. existing display-only fallback where Design display remains allowed.

For each valid Stage position signal it preserves current ordering:

1. build the physical Machine pose from the same cached status generation;
2. restore persisted Design state if applicable through the existing adapter;
3. build the Stage position signal plan;
4. update B tracking and contact calibration presentation;
5. apply unhomed/manual-stop deferral rules;
6. reconcile predicted versus actual XY;
7. update prediction state;
8. publish position/Coordinate System authority/Design presentation;
9. finish an eligible coordinate move on Idle;
10. clear motion-axis presentation and request queued homing continuation.

## Public Physical-Pose Accessor

`stage/position_update.py` currently calls the private
`StageController._axis_calibration_mapper()`. Add a public cached operation on
the controller:

```python
def latest_physical_machine_pose(
    self,
    axes: Iterable[str],
) -> PhysicalMachinePose:
    ...
```

It maps the latest synchronized raw Machine position through universal
calibration without serial I/O. It preserves partial-axis behavior: an absent,
invalid, non-finite, or out-of-domain axis is omitted while other axes remain
available. Do not replace this behavior with
`latest_machine_coordinate_snapshot()`, which intentionally rejects the whole
snapshot when a required same-generation or calibration invariant fails.

## Application Integration

Construct the session after `StageController` in `Main.__init__`. Move the
manual-prediction timer, planned-prediction timer behavior, and exact-step timer
under the session. Main connects controller position/start/finish signals
directly to session slots and connects typed session results to existing
rendering and registration adapters.

Consumers use `self._stage_motion` directly. Do not add Main forwarding
methods. When a current private method exists solely to forward into the
session, delete it and connect/call the session at the real call site.

After migration:

- `status_coordinate_ui.py` retains status log, objective widget, Coordinate
  System transformations, and finished-product status presentation;
- `stage_design_position.py` retains Design projection/rendering and target
  resolution, not motion state;
- `manual_jog.py` retains tool visibility and settings/feedrate persistence,
  not prediction or exact-step lifecycle;
- `registration_focus.py` retains registration/focus policy and consumes typed
  motion completions;
- unrelated ruler/calibration/status handlers move out of
  `motion_prediction.py`, allowing that mixin to be deleted.

## Qt and Concurrency Constraints

1. Construct and use the session on the GUI thread.
2. Parent every `QTimer` to the session; timer callbacks run on the GUI thread.
3. Worker-originated controller signals reach session slots through queued Qt
   delivery. Preserve explicit queued connections where currently required.
4. The session performs cached reads and tiny state/presentation updates only.
   It does no hardware I/O, camera/GenICam work, file parsing, image processing,
   thread joins, or blocking waits.
5. The session has no mutable-state lock because its state is GUI-thread
   confined. Consequently it cannot emit a Qt signal while holding a
   non-reentrant lock.
6. Controller operations remain asynchronous and retain StageController's
   operation lifecycle and safety checks.

## Safety Invariants

- Every move continues through existing public StageController request/queue
  methods and `_move_safety_check()`; no serial or G-code moves into the
  session.
- Unknown needle state is never treated as raised.
- Manual serial commands continue to invalidate Needles Known Raised and
  Coordinate System confidence.
- Coordinate cancellation retains priority over generic busy-task
  cancellation and clears prediction/tracking in the established order.
- Global Cancel retains current application ordering: pending UI intents,
  Route Measurement stop, background captures, active coordinate motion,
  generic controller motion/task, then pending coordinate edits.
- Route Pause Request, Pause Ack, pending-Pause Interrupt path, persistent
  Interrupt, autofocus safe-Z restoration, contact/photo/external-measurement
  suppression, and route result behavior are unchanged.
- Homing queue/spinner semantics and the keyboard jog-stop resend remain
  unchanged.
- Connection reset clears all session-owned transient motion state before
  detached UI presentation is rendered.

## Dependency Direction

Add the sixth Import Linter contract:

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

No `ignore_imports`, optional module, direct-only switch, or wildcard weakening
is allowed. The existing domain-no-application contract prevents Stage,
Coordinates, Design, and Views from importing the session in reverse.

## Test Surface

Direct session tests replace owner-shaped tests and exercise the public methods,
snapshots, signals, and controller fake. They cover:

- rejected starts and target mismatch;
- stale/unrelated completion identity;
- planned success/failure and fresh-status wait;
- coordinate start, prediction, Idle finish, feedrate reissue, and failure;
- manual command/stop/tick, ignored Idle samples, smoothing, and stop-tail
  learning;
- exact-step accumulation, frozen lease, follow-up dispatch, and reset;
- unhomed fallback and publication ordering;
- partial physical pose and B tracking;
- coordinate/generic cancellation priority;
- complete disconnect reset.

A bounded set of Main, view, homing, connection, API, and Design integration
tests proves signal wiring and presentation. Tests must not inspect or recreate
deleted Main fields through compatibility fixtures.

## Maintainability Gates and Projection

Current normal Radon MI baselines are:

- `move_lifecycle.py`: 11.82;
- `position_update.py`: 16.03;
- `motion_prediction.py`: 28.60;
- `manual_jog.py`: 17.02;
- `stage_design_position.py`: 21.20;
- `registration_focus.py`: 8.46;
- `test_move_lifecycle.py`: 5.63;
- `test_main_stage_coordinate_controls.py`: 4.50;
- `main.py`: 13.83.

The new production module is projected at normal MI 10-18 with stripped MI at
least 8. Direct test files should remain above 5 normal and stripped MI. These
are planning targets, not padding incentives. Hard acceptance is positive
normal and AST comments/docstrings-stripped MI for every changed Python file,
with no new nonpositive tracked file, no regression in warning-level CC, and no
unexplained duplicate-block regression.

## Rejected Alternatives

- **Another Main mixin:** moves method text but preserves shared hidden state,
  MRO coupling, and the owner-shaped test interface.
- **Free helpers with `owner=self`:** reproduces the two shallow modules being
  deleted.
- **A callback mega-port:** makes the dependency interface as large as Main and
  forces tests to reconstruct every callback.
- **A facade or compatibility properties over raw fields:** retains dual
  ownership and fails the deletion test.
- **One god application runtime:** would absorb Route, camera, registration,
  settings, and workers that have independent owners.
- **Move the behavior into StageController:** mixes GUI prediction and
  presentation with serial/safety authority and complicates hardware code.
- **Route in-process behavior through localhost:** violates application rules
  and adds transport where direct Qt/controller calls already exist.
- **Use `latest_machine_coordinate_snapshot()` for display pose:** loses the
  current partial-axis behavior.

## Acceptance

The refactor is complete when the prerequisite bug is fixed, the session owns
all named state, old owner modules/motion mixin are deleted, no compatibility
surface remains, six Import Linter contracts pass, all changed files have
positive normal/stripped MI, the deterministic hardware-free test matrix is
fully accounted for, and independent review reports no Critical or Important
finding. No hardware, network, visible GUI, push, or merge is part of this
track.
