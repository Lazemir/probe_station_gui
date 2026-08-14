# Route Run Execution Slot Design

**Date:** 2026-08-14

## Purpose

Give the application shell one owner for the currently active Route Measurement
runner, worker thread, run kind, and published safe-waiting checkpoint. Remove
the raw execution fields that are currently read and reset by launch, API,
control, finish, shutdown, stage-cancel, and presentation code.

This is an ownership refactor. It must not change route movement, contact,
autofocus, Pause Request, Pause Ack, Interrupt, confirmation, external-result,
or persistence behavior.

## Current Problem

Four raw fields are initialized in `main.py`:

- `_route_measurement_runner`
- `_route_measurement_thread`
- `_route_measurement_waiting`
- `_route_measurement_waiting_reason`

They are independently mutated in GUI launch, external API launch and takeover,
failed-start cleanup, API control UI updates, safe-waiting callbacks, and finish
cleanup. Read-only consumers in shutdown, stage cancellation, design guards,
Telegram status, dialogs, and API adapters also depend on the entire `Main`
shape. Run kind is inferred with `isinstance` or capability probing.

The result is distributed thread/runner identity and cleanup policy. A timeout,
stale finish signal, or waiting transition can leave callers disagreeing about
which run is active.

## Existing Owners That Remain

The new owner must not replace or wrap these established modules:

- `_RouteRunControlMailbox` remains the sole internal owner of stop, one-shot
  Pause Request, persistent Interrupt, pending confirmation, and waiting
  synchronization.
- `_RouteRunCoordinator` remains the owner of safe checkpoints, Pause
  consumption, Interrupt acknowledgement, safe-Z/contact cleanup, and route
  point policy.
- `RouteExternalMeasurementSessionRunner` retains its condition, pending
  action, external-result request/result handshake, history, contact-attention
  decisions, and external workflow state.
- `ApiRouteControlState` retains the external client control-plane state.
  Its `pause_requested` and `paused` fields are not runner mailbox state.
- Durable route-session metadata, result/photo state, pending resume point, and
  persisted `_route_measurement_session_active` remain outside the execution
  slot.

## New Owner

Add private standard-library-only module:

`probe_station_gui/application/route_run_execution.py`

Its canonical class is `_RouteRunExecutionSlot`.

### Owned state

- current runner, structurally typed as `object`;
- current worker thread, structurally typed as `object`;
- explicit `RouteRunKind`: `GUI` or `EXTERNAL_RESULT_SESSION`;
- published safe-waiting boolean;
- published waiting reason.

### Immutable values

- `RouteRunSnapshot` exposes the five state values and derived
  `active`/`thread_alive` facts.
- `RouteInterruptDirective` exposes the captured runner and whether the
  application must cancel the active stage task.
- `RouteRunReleaseRequest` selects the typed cause `FINISHED`,
  `TAKEOVER`, or `FAILED_START`, expected runner, join timeout, and the
  exact cause-specific guards.
- `RouteRunReleaseOutcome` reports released, stale, timed-out, rejected, and
  the prior/current snapshots.

### Operations

The slot has five public operations:

1. `snapshot()`
2. `activate(runner, thread, *, kind)`
3. `publish_waiting(waiting)`
4. `request_interrupt()`
5. `release(request)`

`activate` records state but never starts the thread. Existing application
code preserves presentation and signal ordering, then starts the returned or
locally held thread exactly where it does now.

`publish_waiting(True)` is the only runner-level Pause Ack. It derives a
non-empty reason from the active runner's status payload when available and
otherwise uses `paused`. `publish_waiting(False)` clears the reason. A
stale waiting signal after release cannot reactivate an idle slot.

`request_interrupt` invokes the runner's existing current-point correction
request. It reports whether application stage cancellation is required:
running point -> cancel active stage; safe waiting checkpoint -> do not cancel.
The slot does not clear Interrupt.

`release` owns:

- expected-runner identity checks for stale finish signals;
- explicit run-kind checks for GUI takeover;
- stop-before-join ordering;
- the existing 2-second takeover/failed-start waits;
- takeover timeout retention;
- dead finished-thread join;
- atomic clearing of the five owned values;
- returning the prior runner so callers can preserve final API status ordering.

It does not clear unrelated route metadata or update UI.

## Application Integration

Construct one slot in `Main.__init__` and delete the four raw execution fields.
All application, view, stage, shutdown, dialog, and Telegram consumers use
`snapshot()` or one of the semantic operations directly. Do not add
compatibility properties or forwarding methods on `Main`.

The following duplicated helpers disappear when their behavior moves into the
slot:

- `_current_route_measurement_waiting_reason`
- `_join_finished_route_measurement_thread`
- `_clear_waiting_route_measurement_state`
- callback-heavy `restart_waiting_route_measurement`

`ApiRouteControlState.ui_state()` continues to drive API-control
presentation directly. It must not write a control-plane pause into the
physical-run slot. Consumers that need combined UI availability explicitly
combine the slot snapshot with `ApiRouteControlState`.

## State Model

```text
IDLE --activate--> RUNNING
RUNNING --Pause Request--> RUNNING
RUNNING --publish_waiting(true)--> WAITING
RUNNING --Interrupt--> RUNNING + cancel-stage directive
WAITING --Interrupt--> WAITING + no-cancel directive
WAITING --publish_waiting(false)--> RUNNING
RUNNING|WAITING --successful release--> IDLE
RUNNING|WAITING --timed-out takeover--> unchanged
FINISHED(stale runner) --> unchanged
```

Pause Request never changes the slot to waiting. Interrupt persistence and
acknowledgement remain inside the runner mailbox/coordinator.

## Dependency Direction

The slot is standard-library-only. It imports neither `main`, route policy,
Qt, dialogs, views, nor sibling application owners. Callers import the slot;
the route package never imports it.

Add a fifth Import Linter contract:

`route-run-execution-slot-leaf`

It forbids the slot from importing `main`, `probe_station_gui.route`,
`dialogs`, `views`, and every application owner that consumes it. No
`ignore_imports`, optional-module, or direct-only weakening is allowed.

## Verification

TDD direct tests cover activation, duplicate activation rejection, run kind,
waiting publication, stale waiting, running/waiting Interrupt directives,
takeover success, takeover timeout retention, failed-start cleanup, current and
stale finish, and returned prior snapshots.

Integration tests preserve:

- GUI and external launch ordering;
- waiting GUI takeover and timeout;
- failed external initial pause cleanup;
- Pause Request versus Pause Ack;
- pending Pause remains an Interrupt path;
- Interrupt before/inside autofocus/photo/contact;
- safe-Z restoration and contact cleanup;
- stale finish signals;
- final external status capture before metadata cleanup;
- shutdown and stage-cancel ordering.

The route mailbox/coordinator safety suites remain unchanged. Every changed
Python file must have positive normal and comments/docstrings-stripped MI.
Import Linter, Ruff, format, compile, diff, and the deterministic
hardware-free suite remain mandatory.

## Rejected Alternatives

- Replace or reuse `_RouteRunControlMailbox`: conflates the established
  internal safety mailbox with application thread registration or external
  results.
- Extend `_RouteRunCoordinator`: puts Qt/application lifecycle into route
  point policy and reverses dependency direction.
- Put external-result state in the slot: destroys the legitimate outer
  workflow owner.
- One mega Route lifecycle owner: merges durable persistence, control plane,
  Telegram/UI metadata, and physical execution.
- A facade over existing Main methods or compatibility properties for the four
  fields: preserves the distributed hidden-state interface and fails the
  deletion test.

Deleting the slot must force identity, stop/join/timeout retention, run-kind
classification, waiting publication, Interrupt routing, and release policy
back into at least four callers. That is the module-depth test.
